"""食品寄递协同后端（服务门面）。

把时态档案、揽件幂等、线索分级、包裹状态机、跨部门交接、风险传播
与持久化调度编排成一套协同后端。关键边界：

- 网点**只发现、只上报**：系统不替网点判违法，异常一律生成对应级别
  线索交给市场监管；网点能暂停本企业未发出包裹，但不能决定放行/立案。
- 监管**独立处置**：核查、放行、立案只由市场监管角色作出，原上报人
  不能关闭自己的线索。
- 一切处置都走版本号/序号 CAS：移交决定与扫描并发时，后提交的过期
  决定直接冲突，不会覆盖新状态，也不会留下放行与冻结并存。
- 风险扩大只作用于实际关联批次的实际在途件；已妥投是保留事实，
  只转化为通知/召回责任。
- ``tick`` 驱动温控时限、待核查到点和回执催办；状态整体可快照，
  重启后未竟事项继续。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import store as store_mod
from . import timeutils
from .actors import Actor, Org, Role
from .clues import Clue, ClueRegistry, ClueStatus
from .complaints import Complaint, ComplaintChain, ComplaintRegistry
from .handoff import Handoff, HandoffRegistry, TimelineEntry, TimelineKind
from .parcels import (
    ColdChainState,
    Parcel,
    ParcelRegistry,
    ParcelStatus,
)
from .pickup import (
    InspectionSnapshot,
    PickupOutcome,
    PickupRecord,
    PickupRegistry,
    TempRequirement,
)
from .rules import RuleBook, RuleType, RuleVersion
from .scheduler import Job, JobKind, Scheduler
from .temporal import (
    CustomerProfile,
    Premise,
    Qualification,
    VerificationCode,
    Warehouse,
)

# 冷链要求对应的最大允许连续中断秒数（可按部署配置覆盖）。
DEFAULT_TEMP_LIMITS = {
    TempRequirement.REFRIGERATED.value: 30 * 60,      # 冷藏 30 分钟
    TempRequirement.FROZEN.value: 60 * 60,           # 冷冻 60 分钟
}
# 回执默认催办周期与期限
DEFAULT_RECEIPT_WINDOW_SECONDS = 24 * 3600
REMINDER_INTERVAL_SECONDS = 12 * 3600


@dataclass
class PickupResult:
    record: PickupRecord
    parcel: Parcel
    clues: list[Clue]
    replayed: bool


class CoordinationService:
    def __init__(self, *, clock=timeutils.now,
                 temp_limits: dict[str, float] | None = None) -> None:
        self._clock = clock
        self._profiles: dict[str, CustomerProfile] = {}
        self.rules = RuleBook()
        self.pickups = PickupRegistry()
        self.clues = ClueRegistry()
        self.parcels = ParcelRegistry()
        self.complaints = ComplaintRegistry()
        self.handoffs = HandoffRegistry()
        self.scheduler = Scheduler()
        self.temp_limits = dict(temp_limits or DEFAULT_TEMP_LIMITS)

    # ============ 客户时态档案 ============
    def profile(self, customer_id: str) -> CustomerProfile:
        if customer_id not in self._profiles:
            self._profiles[customer_id] = CustomerProfile(customer_id)
        return self._profiles[customer_id]

    def register_qualification(self, q: Qualification) -> None:
        self.profile(q.customer_id).qualifications.add(q)

    def register_premise(self, p: Premise) -> None:
        self.profile(p.customer_id).premises.add(p)

    def register_warehouse(self, w: Warehouse) -> None:
        self.profile(w.customer_id).warehouses.add(w)

    def register_code(self, c: VerificationCode) -> None:
        self.profile(c.customer_id).codes.add(c)

    def publish_rule(self, rule: RuleVersion) -> None:
        self.rules.publish(rule)

    # ============ 揽件验视（幂等 + 当时所见快照 + 自动线索）============
    def report_pickup(
        self,
        actor: Actor,
        *,
        serial: str,
        branch_id: str,
        customer_id: str,
        snapshot: InspectionSnapshot,
        tracking_no: str,
        batch_id: str,
        verification_code: str | None = None,
        observed_at: datetime | None = None,
    ) -> PickupResult:
        """网点上报一次揽件验视。

        同一流水离线重传：同文幂等返回原结果，绝不生成第二次揽件；
        异文直接抛 ``IdempotencyConflictError`` 并进入隔离。
        """

        actor.require(Role.COURIER_STAFF)
        observed_at = timeutils.effective_at(observed_at)
        received_at = self._clock()

        record = self.pickups.register(
            serial=serial, branch_id=branch_id, courier_id=actor.actor_id,
            customer_id=customer_id, verification_code=verification_code,
            snapshot=snapshot, observed_at=observed_at, received_at=received_at,
            tracking_no=tracking_no, batch_id=batch_id,
        )
        if record.outcome is PickupOutcome.REPLAYED:
            return PickupResult(
                record=record, parcel=self.parcels.get(tracking_no),
                clues=[c for c in self.clues.all() if c.serial == serial],
                replayed=True,
            )

        found_clues = self._evaluate_at_pickup(
            actor=actor, customer_id=customer_id, serial=serial,
            tracking_no=tracking_no, batch_id=batch_id,
            snapshot=snapshot, code=verification_code, at=observed_at,
        )

        cold: ColdChainState | None = None
        req = snapshot.temperature.requirement
        if req in (TempRequirement.REFRIGERATED, TempRequirement.FROZEN):
            cold = ColdChainState(
                requirement=req.value,
                max_interruption_seconds=self.temp_limits[req.value],
            )
            if not snapshot.temperature.chain_ok_at_pickup:
                cold.interrupted_since = observed_at
                cold.last_reading_at = observed_at
                self.scheduler.schedule(
                    JobKind.TEMP_DEADLINE,
                    timeutils.deadline(observed_at, cold.max_interruption_seconds),
                    tracking_no,
                    payload={"batch_id": batch_id, "serial": serial,
                             "customer_id": customer_id},
                )

        parcel = Parcel(
            tracking_no=tracking_no, serial=serial, batch_id=batch_id,
            customer_id=customer_id, branch_id=branch_id,
            status=ParcelStatus.PICKED_UP, created_at=received_at,
            cold_chain=cold,
        )
        self.parcels.add(parcel)
        self._timeline_parcel(parcel, actor, f"揽件验视 {serial}")
        return PickupResult(record=record, parcel=parcel, clues=found_clues, replayed=False)

    def _evaluate_at_pickup(
        self, *, actor: Actor, customer_id: str, serial: str,
        tracking_no: str, batch_id: str, snapshot: InspectionSnapshot,
        code: str | None, at: datetime,
    ) -> list[Clue]:
        profile = self.profile(customer_id)
        raised: list[Clue] = []

        def raise_clue(rule_type: RuleType, title: str, evidence: str,
                       seconds: float | None = None) -> Clue:
            rule = self.rules.effective(rule_type, at)
            clue = self.clues.create(
                rule_type=rule_type, level=rule.level_for(seconds),
                rule_version=rule.version, customer_id=customer_id,
                serial=serial, tracking_no=tracking_no, batch_id=batch_id,
                title=title, evidence=evidence, reporter=actor, reported_at=at,
            )
            self._timeline_clue(clue, actor)
            raised.append(clue)
            return clue

        # 1) 核验码时态核验：码在揽件当时必须生效，且绑定同一客户。
        code_ok = False
        registered_address: str | None = None
        if code:
            try:
                code_version = profile.codes.as_of(code, at)
                registered_address = code_version.registered_address
                code_ok = code_version.customer_id == customer_id
            except Exception:
                code_ok = False
        if not code_ok:
            raise_clue(
                RuleType.ADDRESS_MISMATCH,
                "专属核验码无效、失效或与客户不匹配",
                f"上报核验码：{code or '（空）'}",
            )

        # 2) 资质时态核验：当时没有有效资质版本即线索（网点不判违法）。
        qual = None
        try:
            qual = profile.qualifications.as_of(customer_id, at)
        except Exception:
            qual = None
        if qual is None or not qual.effective(at):
            shown = snapshot.license.shown_expiry
            raise_clue(
                RuleType.LICENSE_EXPIRED,
                "食品经营资质缺失或已过有效期",
                f"证面：{snapshot.license.license_no}，"
                f"证面有效期至 {shown:%Y-%m-%d}" if shown else
                f"证面：{snapshot.license.license_no}，未见有效期限",
            )

        # 3) 地址不符：码面经营地址与现场仓库不同。现场地址必须等于
        #    码面地址，或属于当时生效的经营场所/外设仓库之一。
        if code_ok:
            known = profile.known_addresses_at(at)
            site = snapshot.location.address
            if not _address_match(registered_address or "", site, known):
                raise_clue(
                    RuleType.ADDRESS_MISMATCH,
                    "核验码经营地址与现场揽收仓库不符",
                    f"码面地址：{registered_address or '（无）'}；现场地址：{site}；"
                    f"登记在案地址：{sorted(known)}",
                )

        # 4) 包装异常
        if not snapshot.packaging_ok:
            raise_clue(
                RuleType.PACKAGING_ABNORMAL,
                "验视发现食品包装异常",
                f"货物类别：{snapshot.goods_category}；备注：{snapshot.remarks or '（无）'}",
            )

        # 5) 揽收当时温控链已中断：按 0 秒落当时版本最低档，同时
        #    时限任务已在外层挂起，到点再升级。
        if not snapshot.temperature.chain_ok_at_pickup:
            raise_clue(
                RuleType.TEMP_INTERRUPTION,
                "揽收时温控链已中断",
                f"要求：{snapshot.temperature.requirement.value}；"
                f"测温：{snapshot.temperature.temp_celsius}℃",
                seconds=0,
            )
        return raised

    # ============ 温控 ============
    def report_temperature(self, actor: Actor, tracking_no: str, *,
                           at: datetime, temp_celsius: float,
                           chain_ok: bool) -> Parcel:
        """上报温控读数；chain_ok=False 开始/持续中断，True 恢复。"""

        actor.require(Role.COURIER_STAFF, Role.POSTAL_ADMIN)
        parcel = self.parcels.get(tracking_no)
        cc = parcel.cold_chain
        if cc is None:
            from .errors import ValidationError
            raise ValidationError(f"{tracking_no} 非冷链件，不跟踪温控")
        cc.last_temp_celsius = temp_celsius
        cc.last_reading_at = at
        if not chain_ok and cc.interrupted_since is None:
            cc.interrupted_since = at
            self.scheduler.schedule(
                JobKind.TEMP_DEADLINE,
                timeutils.deadline(at, cc.max_interruption_seconds),
                tracking_no,
                payload={"batch_id": parcel.batch_id, "serial": parcel.serial,
                         "customer_id": parcel.customer_id},
            )
        elif chain_ok and cc.interrupted_since is not None:
            cc.accumulated_interruption_seconds += (
                at - cc.interrupted_since
            ).total_seconds()
            cc.interrupted_since = None
            self.scheduler.cancel(tracking_no, JobKind.TEMP_DEADLINE)
        return parcel

    # ============ 企业包裹处置（只到“暂停未发出”）============
    def pause_unshipped(self, actor: Actor, tracking_no: str, *,
                        at: datetime | None = None, reason: str = "",
                        expected_seq: int | None = None) -> Parcel:
        parcel = self.parcels.pause_unshipped(
            tracking_no, actor=actor, at=timeutils.effective_at(at),
            expected_seq=expected_seq, reason=reason,
        )
        self._timeline_parcel(parcel, actor, f"企业暂停（未发出）：{reason}")
        return parcel

    def resume_unshipped(self, actor: Actor, tracking_no: str, *,
                         at: datetime | None = None,
                         expected_seq: int | None = None) -> Parcel:
        return self.parcels.resume_unshipped(
            tracking_no, actor=actor, at=timeutils.effective_at(at),
            expected_seq=expected_seq,
        )

    def dispatch(self, actor: Actor, tracking_no: str, *,
                 at: datetime | None = None,
                 expected_seq: int | None = None) -> Parcel:
        return self.parcels.dispatch(
            tracking_no, actor=actor, at=timeutils.effective_at(at),
            expected_seq=expected_seq,
        )

    def scan(self, actor: Actor, tracking_no: str, *, node: str, kind: str,
             at: datetime | None = None,
             expected_seq: int | None = None) -> Parcel:
        """转运/退回/妥投扫描。冻结中扫描会被状态机拒绝。"""

        parcel = self.parcels.scan(
            tracking_no, actor=actor, at=timeutils.effective_at(at),
            node=node, kind=kind, expected_seq=expected_seq,
        )
        self._timeline_parcel(parcel, actor, f"{kind} @ {node}")
        return parcel

    def confirm_returned(self, actor: Actor, tracking_no: str, *,
                         at: datetime | None = None,
                         expected_seq: int | None = None) -> Parcel:
        return self.parcels.confirm_returned(
            tracking_no, actor=actor, at=timeutils.effective_at(at),
            expected_seq=expected_seq,
        )

    # ============ 监管独立处置 ============
    def review_clue(self, actor: Actor, clue_id: str, *,
                    at: datetime | None = None, detail: str = "",
                    due: datetime | None = None,
                    expected_version: int | None = None) -> Clue:
        at = timeutils.effective_at(at)
        clue = self.clues.decide(
            clue_id, actor=actor, target=ClueStatus.UNDER_REVIEW, at=at,
            expected_version=expected_version, detail=detail,
        )
        self._timeline_clue(clue, actor, "监管受理核查")
        if due is not None:
            self.scheduler.schedule(JobKind.REVIEW_DUE, due, clue_id,
                                    payload={"detail": detail})
        return clue

    def _decide(self, actor: Actor, clue_id: str, target: ClueStatus,
                detail: str, at: datetime | None,
                expected_version: int | None) -> Clue:
        at = timeutils.effective_at(at)
        clue = self.clues.decide(
            clue_id, actor=actor, target=target, at=at,
            expected_version=expected_version, detail=detail,
        )
        self._timeline_clue(clue, actor, detail)
        return clue

    def release_clue(self, actor: Actor, clue_id: str, *,
                     at: datetime | None = None, detail: str = "监管放行",
                     expected_version: int | None = None,
                     expected_parcel_seq: int | None = None) -> Clue:
        """监管放行：线索放行，并解除**仅因该线索**而冻结的包裹。

        冻结解除同样带序号 CAS；若包裹刚被扫描推进，旧序号放行冲突，
        绝不会把新状态回滚成旧状态，也不会与新冻结并存。
        """

        clue = self._decide(actor, clue_id, ClueStatus.RELEASED, detail,
                            at, expected_version)
        at = timeutils.effective_at(at)
        for parcel in self.parcels.all():
            if parcel.frozen_by_clue == clue_id:
                released = self.parcels.release_freeze(
                    parcel.tracking_no, actor=actor, at=at,
                    expected_seq=expected_parcel_seq, reason=detail,
                )
                self._timeline_parcel(released, actor,
                                      f"依线索 {clue_id} 放行解锢")
        return clue

    def file_case(self, actor: Actor, clue_id: str, *,
                  at: datetime | None = None, detail: str = "监管立案",
                  expected_version: int | None = None) -> Clue:
        clue = self._decide(actor, clue_id, ClueStatus.CASE_FILED, detail,
                            at, expected_version)
        # 立案：保持/落实冻结（在途可处置件），妥投件转通知。
        at = timeutils.effective_at(at)
        self._enforce_for_clue(clue, actor, at, reason=f"立案 {clue_id}")
        return clue

    def dismiss_clue(self, actor: Actor, clue_id: str, *,
                     at: datetime | None = None, detail: str = "不予处置",
                     expected_version: int | None = None) -> Clue:
        return self._decide(actor, clue_id, ClueStatus.DISMISSED, detail,
                            at, expected_version)

    def _enforce_for_clue(self, clue: Clue, actor: Actor, at: datetime,
                          *, reason: str) -> None:
        """立案措施：冻结该线索批次内可处置件，妥投件转通知责任。"""

        affected = self.parcels.propagate_batch(
            clue.batch_id or "", actor=actor, at=at,
            clue_id=clue.clue_id, reason=reason,
        )
        for tracking_no in affected["frozen"]:
            self._timeline_parcel(self.parcels.get(tracking_no), actor,
                                  f"依{reason}冻结")
        for tracking_no in affected["notified"]:
            self._timeline_parcel(self.parcels.get(tracking_no), actor,
                                  f"已妥投件 {tracking_no} 转通知责任")

    # ============ 跨部门移交 ============
    def transfer_clue(self, actor: Actor, clue_id: str, *, to_org: str,
                      at: datetime | None = None,
                      receipt_due: datetime | None = None,
                      expected_version: int | None = None,
                      note: str = "") -> Handoff:
        """发起跨部门移交：线索挂起 + 交接记录 + 回执期限与催办。

        与包裹扫描并发安全：线索迁移用版本 CAS，包裹冻结用序号 CAS，
        互不覆盖；移交完成不自动放行或冻结，处置权移交给接收方。
        """

        at = timeutils.effective_at(at)
        clue = self.clues.transfer(
            clue_id, actor=actor, at=at, to_dept=to_org,
            expected_version=expected_version,
        )
        refs = [clue.tracking_no] if clue.tracking_no else []
        refs += [p.tracking_no for p in self.parcels.batch(clue.batch_id or "")]
        due = receipt_due or timeutils.deadline(at, DEFAULT_RECEIPT_WINDOW_SECONDS)
        handoff = self.handoffs.initiate(
            clue_id=clue_id, tracking_refs=sorted(set(refs)), actor=actor,
            to_org=to_org, at=at, receipt_due=due, note=note,
        )
        self.scheduler.schedule(
            JobKind.RECEIPT_REMINDER, due, handoff.handoff_id,
            payload={"to_org": to_org},
            recurring_seconds=REMINDER_INTERVAL_SECONDS,
        )
        self._timeline_clue(clue, actor, f"移交至 {to_org}")
        return handoff

    def acknowledge_handoff(self, actor: Actor, handoff_id: str, *,
                            at: datetime | None = None, accepted: bool = True,
                            note: str = "") -> Handoff:
        at = timeutils.effective_at(at)
        handoff = self.handoffs.acknowledge(
            handoff_id, actor=actor, at=at, accepted=accepted, note=note,
        )
        self.scheduler.cancel(handoff_id, JobKind.RECEIPT_REMINDER)
        return handoff

    # ============ 风险扩大：只波及关联批次，妥投转通知 ============
    def expand_risk_to_batch(self, actor: Actor, clue_id: str, *,
                             at: datetime | None = None,
                             reason: str = "") -> dict:
        """风险扩大到线索所属批次。

        - 在途/未发出且未终态、未冻结的实际关联件 → 冻结；
        - 已妥投件状态不动（事实保留），生成通知/召回责任；
        - 已退回等终态件不受影响，不关联批次的件绝不波及。
        """

        at = timeutils.effective_at(at)
        actor.require(Role.MARKET_REGULATOR, Role.POSTAL_ADMIN,
                      Role.COURIER_STAFF)
        clue = self.clues.get(clue_id)
        result = self.parcels.propagate_batch(
            clue.batch_id or "", actor=actor, at=at, clue_id=clue_id,
            reason=reason or f"线索 {clue_id} 风险扩大",
        )
        for tracking_no in result["frozen"]:
            self._timeline_parcel(self.parcels.get(tracking_no), actor,
                                  f"风险扩大冻结（线索 {clue_id}）")
        for tracking_no in result["notified"]:
            self._timeline_parcel(self.parcels.get(tracking_no), actor,
                                  f"妥投件 {tracking_no} 转通知责任（线索 {clue_id}）")
        return result

    def fulfill_notification(self, actor: Actor, duty_id: str, *,
                             at: datetime | None = None):
        actor.require(Role.COURIER_STAFF, Role.POSTAL_ADMIN)
        return self.parcels.fulfill_duty(
            duty_id, actor=actor, at=timeutils.effective_at(at))

    # ============ 投诉归入正确链路 ============
    def open_complaint(self, customer_id: str, *, chain: ComplaintChain,
                       anchor: str, summary: str,
                       at: datetime | None = None) -> Complaint:
        at = timeutils.effective_at(at)
        exists = self._anchor_exists(chain, anchor)
        return self.complaints.open(
            chain=chain, anchor=anchor, anchor_exists=exists,
            customer_id=customer_id, summary=summary, at=at,
        )

    def _anchor_exists(self, chain: ComplaintChain, anchor: str) -> bool:
        if chain is ComplaintChain.PICKUP:
            return anchor in self.pickups._records
        if chain is ComplaintChain.PARCEL:
            return any(p.tracking_no == anchor for p in self.parcels.all())
        return any(c.clue_id == anchor for c in self.clues.all())

    def answer_complaint(self, actor: Actor, complaint_id: str, *, answer: str,
                         at: datetime | None = None, reject: bool = False) -> Complaint:
        actor.require(Role.COURIER_STAFF, Role.POSTAL_ADMIN,
                      Role.MARKET_REGULATOR)
        return self.complaints.process(
            complaint_id, actor_id=actor.actor_id,
            at=timeutils.effective_at(at), answer=answer, reject=reject,
        )

    # ============ 时间线与责任视图（按权限）============
    def view_timeline(self, actor: Actor, ref_id: str | None = None):
        """权限范围内的完整交接时间线。客户只看与自己相关的包裹视图。"""

        if actor.role is Role.CUSTOMER:
            return self._customer_timeline(actor, ref_id)
        return self.handoffs.view(org=actor.org.value, ref_id=ref_id)

    def _customer_timeline(self, actor: Actor, ref_id: str | None):
        entries = []
        for e in self.handoffs.view(org=Org.CUSTOMER_ORG.value, ref_id=ref_id):
            entries.append(e)
        # 客户可见自己包裹的物流事实（不含线索处置细节）
        for parcel in self.parcels.all():
            if parcel.customer_id != actor.actor_id:
                continue
            if ref_id and parcel.tracking_no != ref_id and parcel.serial != ref_id:
                continue
            for ev in parcel.events:
                entries.append(TimelineEntry(
                    at=ev.at, kind=TimelineKind.PARCEL,
                    ref_id=parcel.tracking_no, actor_name=ev.actor_name,
                    org=Org.EXPRESS.value,
                    summary=f"{ev.action}：{ev.new_status.value if ev.new_status else ''}",
                    visible_orgs=frozenset({Org.CUSTOMER_ORG.value}),
                ))
        return sorted(entries, key=lambda e: e.at)

    def view_responsibilities(self, actor: Actor) -> dict:
        """按角色返回权限范围内的待办责任。"""

        if actor.role is Role.MARKET_REGULATOR:
            return {
                "pending_clues": [c.clue_id for c in self.clues.all()
                                  if c.status in (ClueStatus.REPORTED,
                                                  ClueStatus.UNDER_REVIEW,
                                                  ClueStatus.TRANSFERRED)],
                "pending_reviews": [j.job_id for j in self.scheduler.pending(
                    kind=JobKind.REVIEW_DUE)],
            }
        if actor.role is Role.COURIER_STAFF:
            return {
                "pausable_unshipped": [
                    p.tracking_no for p in self.parcels.all()
                    if p.status is ParcelStatus.PICKED_UP
                ],
                "pending_notifications": [
                    d.duty_id for d in self.parcels.duties(pending_only=True)
                ],
                "quarantine": [q.quarantine_id for q in self.pickups.quarantine()],
            }
        if actor.role is Role.POSTAL_ADMIN:
            moment = self._clock()
            return {
                "awaiting_receipts": [
                    h.handoff_id for h in self.handoffs.all()
                    if h.receipt_due <= moment and not h.receipt_at
                ],
                "open_clues": [c.clue_id for c in self.clues.open_clues()],
            }
        # 客户
        return {
            "parcels": [p.tracking_no for p in self.parcels.all()
                        if p.customer_id == actor.actor_id],
            "complaints": [t.complaint_id for t in self.complaints.all()
                           if t.customer_id == actor.actor_id],
        }

    # ============ 时间驱动：温控时限 / 待核查 / 回执催办 ============
    def tick(self, now: datetime | None = None) -> list[Clue]:
        """推进所有到点事项；重启后调用即可继续未竟工作。

        返回本次因温控时限到点而升级生成的线索。
        """

        now = timeutils.effective_at(now)
        escalated: list[Clue] = []
        for job in self.scheduler.due(now):
            if job.kind is JobKind.TEMP_DEADLINE:
                clue = self._escalate_temp(job, now)
                if clue:
                    escalated.append(clue)
            elif job.kind is JobKind.REVIEW_DUE:
                self.handoffs.append_timeline(TimelineEntry(
                    at=now, kind=TimelineKind.CLUE, ref_id=job.ref_id,
                    actor_name="调度器", org="system",
                    summary=f"待核查事项 {job.ref_id} 已到核查期限，请尽快处置",
                    visible_orgs=frozenset({Org.MARKET.value, Org.POSTAL.value}),
                ))
            elif job.kind is JobKind.RECEIPT_REMINDER:
                handoff = self.handoffs.get(job.ref_id)
                if not handoff.receipt_at:
                    self.handoffs.mark_reminder(handoff, now)
                    self.handoffs.append_timeline(TimelineEntry(
                        at=now, kind=TimelineKind.RECEIPT,
                        ref_id=handoff.handoff_id, actor_name="调度器",
                        org=handoff.from_org.value,
                        summary=f"催办第 {handoff.reminders_sent} 次："
                                f"{handoff.to_org} 请尽快签收回执",
                        visible_orgs=frozenset(
                            {handoff.from_org.value, handoff.to_org}),
                        payload={"reminders_sent": handoff.reminders_sent},
                    ))
        return escalated

    def _escalate_temp(self, job: Job, now: datetime) -> Clue | None:
        parcel = self.parcels.get(job.ref_id)
        cc = parcel.cold_chain
        if cc is None or cc.interrupted_since is None:
            return None  # 已恢复，时限作废（正常情况下 job 已被取消）
        seconds = (now - cc.interrupted_since).total_seconds()
        rule = self.rules.effective(RuleType.TEMP_INTERRUPTION, now)
        reporter = Actor("system", "温控监测", Role.POSTAL_ADMIN, Org.POSTAL)
        clue = self.clues.create(
            rule_type=RuleType.TEMP_INTERRUPTION,
            level=rule.level_for(seconds), rule_version=rule.version,
            customer_id=job.payload.get("customer_id", parcel.customer_id),
            serial=job.payload.get("serial", parcel.serial),
            tracking_no=parcel.tracking_no, batch_id=parcel.batch_id,
            title=f"温控中断超过时限（{int(seconds)} 秒）",
            evidence=f"要求 {cc.requirement}，允许中断 "
                     f"{int(cc.max_interruption_seconds)} 秒，测温 "
                     f"{cc.last_temp_celsius}℃",
            reporter=reporter, reported_at=now,
        )
        self._timeline_clue(clue, reporter,
                            f"温控时限到点升级（级别 {clue.level.name}）")
        return clue

    # ============ 持久化 ============
    def save(self, path: str | Path) -> None:
        store_mod.save(str(path), self)

    @classmethod
    def load(cls, path: str | Path, *, clock=timeutils.now) -> "CoordinationService":
        service = cls(clock=clock)
        store_mod.load(str(path), service)
        return service

    # ---- 时间线小工具 ----
    def _timeline_clue(self, clue: Clue, actor: Actor, summary: str = "") -> None:
        self.handoffs.append_timeline(TimelineEntry(
            at=clue.reported_at if not clue.events else clue.events[-1].at,
            kind=TimelineKind.CLUE, ref_id=clue.clue_id,
            actor_name=actor.name, org=actor.org.value,
            summary=summary or f"线索 {clue.level.name}：{clue.title}",
            visible_orgs=frozenset({Org.MARKET.value, Org.POSTAL.value,
                                    Org.EXPRESS.value}),
            payload={"rule_type": clue.rule_type.value,
                     "rule_version": clue.rule_version,
                     "level": int(clue.level), "status": clue.status.value},
        ))

    def _timeline_parcel(self, parcel: Parcel, actor: Actor, summary: str) -> None:
        at = parcel.events[-1].at if parcel.events else parcel.created_at
        self.handoffs.append_timeline(TimelineEntry(
            at=at, kind=TimelineKind.PARCEL, ref_id=parcel.tracking_no,
            actor_name=actor.name, org=actor.org.value, summary=summary,
            visible_orgs=frozenset({Org.MARKET.value, Org.POSTAL.value,
                                    Org.EXPRESS.value}),
            payload={"status": parcel.status.value, "seq": parcel.status_seq,
                     "batch_id": parcel.batch_id},
        ))


def _address_match(registered: str, site: str, known: set[str]) -> bool:
    """地址比对：现场地址等于码面地址，或属于登记在案的场所/外设仓库。"""

    return site == registered or site in known
