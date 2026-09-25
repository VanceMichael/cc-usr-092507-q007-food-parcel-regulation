"""应用服务：把命令翻译成原子事件批次。

每个命令在提交前都用 :mod:`aggregates` 的同一套 reducer 对“当前状态 + 拟提交事件”
做预演：任何不变量被破坏就整批不写。因此磁盘上不会留下放行与冻结并存、
旧决定覆盖新状态之类的半成品记录；一次业务动作涉及的多个聚合同进同退。
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from . import aggregates as ag
from .errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    QuarantineError,
    ValidationError,
)
from .eventstore import EventStore, Event
from .masterdata import (
    ADDRESS_MISMATCH,
    LEVEL_MAJOR,
    LICENSE_EXPIRED,
    PACKAGING_ABNORMAL,
    TEMP_BREAK,
    MasterDataView,
)
from .timemodel import Clock, at


# --------------------------------------------------------------------- 仓储


class Repository:
    """从事件流物化所有聚合的当前状态。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.states: dict[tuple[str, str], Any] = {}
        for event in store.all_events():
            key = (event.aggregate_id, event.aggregate_type)
            self.states[key] = ag.reduce(
                event.aggregate_id, event.aggregate_type, self.states.get(key), event
            )

    def get(self, aggregate_id: str, aggregate_type: str) -> Any:
        key = (aggregate_id, aggregate_type)
        if key not in self.states:
            raise NotFoundError(f"{aggregate_type}/{aggregate_id} 不存在")
        return self.states[key]

    def find(self, aggregate_id: str, aggregate_type: str) -> Any | None:
        return self.states.get((aggregate_id, aggregate_type))

    def all_of(self, aggregate_type: str) -> list[Any]:
        return [s for (_, t), s in self.states.items() if t == aggregate_type]

    def propose(self, entries: list[tuple]) -> list[Event]:
        """预演一批事件；通过后真正原子提交，返回已提交事件。"""
        provisional: list[Event] = []
        tentative = copy.deepcopy(self.states)
        next_seq = self.store.global_seq + 1
        normalized: list[tuple] = []
        for entry in entries:
            normalized.append(entry if len(entry) == 7 else (*entry, None))
        for i, (agg_id, agg_type, event_type, payload, actor, occurred_at, causation) in enumerate(normalized):
            event = Event(next_seq + i, agg_id, agg_type, -1, event_type,
                          dict(payload), occurred_at, actor, causation)
            key = (agg_id, agg_type)
            tentative[key] = ag.reduce(agg_id, agg_type, tentative.get(key), event)
            provisional.append(event)
        committed = self.store.append_batch([
            (e.aggregate_id, e.aggregate_type, e.event_type, e.payload, e.actor,
             e.occurred_at, e.causation_id)
            for e in provisional
        ])
        for event in committed:
            key = (event.aggregate_id, event.aggregate_type)
            self.states[key] = ag.reduce(
                event.aggregate_id, event.aggregate_type, self.states.get(key), event
            )
        return committed


def role_of(actor: str) -> str:
    return actor.split(":", 1)[0]


def require_role(actor: str, role: str) -> None:
    if role_of(actor) != role:
        raise AuthorizationError(f"该操作需要 {role} 角色")


def request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- 数据传输对象


@dataclass
class PickupObservation:
    """揽件当时所见。只保存摘要与条件，不保存证照影像原件。"""

    serial_no: str
    code: str
    license_digest: str
    goods_category: str
    temp_required: bool
    temp_range: tuple[float, float] | None = None
    observed_temp: float | None = None
    lat: float = 0.0
    lng: float = 0.0
    observed_address: str = ""
    packaging_ok: bool = True
    packaging_note: str = ""
    waybill_no: str = ""
    batch_id: str | None = None
    device_id: str = ""


# --------------------------------------------------------------------- 揽件服务


class PickupService:
    def __init__(self, store: EventStore, clock: Clock, repo: Repository, master: MasterDataView) -> None:
        self.store = store
        self.clock = clock
        self.repo = repo
        self.master = master

    def accept(self, observation: PickupObservation, actor: str,
               observed_at: Any | None = None) -> dict[str, Any]:
        """网点设备上报一次揽件验视（含离线重传）。

        相同流水 + 相同报文：幂等返回首次揽件，不生成第二次揽件；
        相同流水 + 不同报文：异文进入隔离，不生成揽件。
        """
        require_role(actor, ag.ROLE_COURIER)
        when = at(observed_at or self.clock.now())
        canonical = self._canonical(observation)
        digest = request_hash(canonical)

        existing = self.repo.find(observation.serial_no, "pickup")
        if existing is not None and existing.accepted:
            if existing.request_hash == digest:
                return {"parcel_id": existing.parcel_id, "idempotent": True, "lead_ids": []}
            entries = [(
                observation.serial_no, "pickup", "PickupConflictQuarantined",
                {"serial_no": observation.serial_no, "request_hash": digest,
                 "original_hash": existing.request_hash, "device_id": observation.device_id,
                 "reason": "相同流水附带不同报文"},
                actor, when.isoformat(),
            )]
            self.repo.propose(entries)
            raise QuarantineError(
                f"流水 {observation.serial_no} 报文与首次揽件不一致，已进入隔离"
            )
        if existing is not None and existing.quarantined:
            raise QuarantineError(f"流水 {observation.serial_no} 已在隔离中")

        rulebook = self.master.active_rulebook(when)
        code = self.master.active_code(observation.code, when)
        customer = self.master.customer(code.customer_id)

        # 时态核验：资质、场所、仓库、核验码都按 observed_at 取生效版本。
        findings = self._evaluate(observation, when, rulebook.version, code.customer_id)

        parcel_id = f"parcel-{uuid.uuid4().hex[:12]}"
        lead_ids: list[str] = []
        entries: list[tuple] = []
        for finding in findings:
            lead_id = f"lead-{uuid.uuid4().hex[:12]}"
            lead_ids.append(lead_id)
            entries.append((lead_id, "lead", "LeadReported", {
                "lead_id": lead_id,
                "lead_type": finding["lead_type"],
                "level": finding["level"],
                "rulebook_version": rulebook.version,
                "parcel_id": parcel_id,
                "customer_id": code.customer_id,
                "reporter": actor,
                "reported_at": when.isoformat(),
                "evidence": finding["evidence"],
            }, actor, when.isoformat()))
            entries.append((parcel_id, "parcel", "ParcelRiskFlagged", {
                "lead_id": lead_id, "at": when.isoformat(),
            }, actor, when.isoformat(), lead_id))

        cold_chain = None
        if observation.temp_required:
            cold_chain = {
                "required": True,
                "temp_range": list(observation.temp_range) if observation.temp_range else None,
                "observed_temp": observation.observed_temp,
            }
        entries.append((parcel_id, "parcel", "ParcelAccepted", {
            "parcel_id": parcel_id,
            "waybill_no": observation.waybill_no or observation.serial_no,
            "serial_no": observation.serial_no,
            "customer_id": code.customer_id,
            "batch_id": observation.batch_id,
            "rulebook_version": rulebook.version,
            # 验视只保存当时所见：证照摘要、货物类别、温控条件、位置。
            "inspection_snapshot": {
                "license_digest": observation.license_digest,
                "goods_category": observation.goods_category,
                "cold_chain": cold_chain,
                "location": {"lat": observation.lat, "lng": observation.lng,
                             "address": observation.observed_address},
                "observed_at": when.isoformat(),
            },
            "cold_chain": cold_chain,
        }, actor, when.isoformat()))
        if observation.batch_id:
            entries.append((observation.batch_id, "batch", "ParcelAddedToBatch", {
                "batch_id": observation.batch_id, "parcel_id": parcel_id,
            }, actor, when.isoformat()))
        entries.append((observation.serial_no, "pickup", "PickupAccepted", {
            "serial_no": observation.serial_no,
            "request_hash": digest,
            "observed_at": when.isoformat(),
            "parcel_id": parcel_id,
            "device_id": observation.device_id,
        }, actor, when.isoformat()))
        self.repo.propose(entries)
        return {"parcel_id": parcel_id, "idempotent": False, "lead_ids": lead_ids}

    @staticmethod
    def _canonical(o: PickupObservation) -> dict[str, Any]:
        return {
            "serial_no": o.serial_no, "code": o.code,
            "license_digest": o.license_digest, "goods_category": o.goods_category,
            "temp_required": o.temp_required, "temp_range": list(o.temp_range) if o.temp_range else None,
            "observed_temp": o.observed_temp,
            "lat": o.lat, "lng": o.lng, "observed_address": o.observed_address,
            "packaging_ok": o.packaging_ok, "packaging_note": o.packaging_note,
            "waybill_no": o.waybill_no, "device_id": o.device_id,
        }

    def _evaluate(self, o: PickupObservation, when, rb_version: str, customer_id: str) -> list[dict[str, Any]]:
        rb = self.master.rulebook(rb_version)
        findings: list[dict[str, Any]] = []

        # 1) 地址不符：现场位置不在任何生效的经营场所或外设仓库范围内。
        place = self.master.location_registered(customer_id, (o.lat, o.lng), when)
        if place is None:
            findings.append({
                "lead_type": ADDRESS_MISMATCH, "level": rb.levels[ADDRESS_MISMATCH],
                "evidence": {
                    "observed_location": {"lat": o.lat, "lng": o.lng, "address": o.observed_address},
                    "code_address": self.master.active_code(o.code, when).declared_address,
                },
            })

        # 2) 证照过期：时点无生效资质，或证照有效期已过。只比对摘要，不留原件。
        try:
            qual = self.master.active_qualification(customer_id, when)
            expired = qual.license_expiry is not None and when >= qual.license_expiry
            digest_mismatch = qual.license_digest != o.license_digest
            if expired or digest_mismatch:
                findings.append({
                    "lead_type": LICENSE_EXPIRED, "level": rb.levels[LICENSE_EXPIRED],
                    "evidence": {
                        "reason": "expired" if expired else "digest_mismatch",
                        "license_digest_seen": o.license_digest,
                        "license_digest_registered": qual.license_digest,
                        "license_expiry": qual.license_expiry.isoformat() if qual.license_expiry else None,
                    },
                })
        except NotFoundError:
            findings.append({
                "lead_type": LICENSE_EXPIRED, "level": LEVEL_MAJOR,
                "evidence": {"reason": "no_active_qualification"},
            })

        # 3) 包装异常。
        if not o.packaging_ok:
            findings.append({
                "lead_type": PACKAGING_ABNORMAL, "level": rb.levels[PACKAGING_ABNORMAL],
                "evidence": {"note": o.packaging_note},
            })

        # 4) 温控中断：揽件时实测温度超出客户申报的温控区间。
        if o.temp_required and o.observed_temp is not None and o.temp_range is not None:
            low, high = o.temp_range
            if not (low <= o.observed_temp <= high):
                findings.append({
                    "lead_type": TEMP_BREAK, "level": rb.levels[TEMP_BREAK],
                    "evidence": {"observed_temp": o.observed_temp, "temp_range": [low, high],
                                 "stage": "pickup"},
                })
        return findings


# --------------------------------------------------------------------- 包裹服务


class ParcelService:
    def __init__(self, store: EventStore, clock: Clock, repo: Repository, master: MasterDataView) -> None:
        self.store = store
        self.clock = clock
        self.repo = repo
        self.master = master

    def _parcel(self, parcel_id: str) -> ag.ParcelState:
        return self.repo.get(parcel_id, "parcel")

    def enterprise_hold(self, parcel_id: str, actor: str, reason: str = "",
                        decided_at: Any | None = None) -> None:
        """寄递企业暂停未发出的包裹。"""
        require_role(actor, ag.ROLE_COURIER)
        when = at(decided_at or self.clock.now())
        self.repo.propose([(parcel_id, "parcel", "ParcelHeld", {
            "by": actor, "at": when.isoformat(), "reason": reason,
            "decided_at": when.isoformat(),
        }, actor, when.isoformat())])

    def enterprise_release(self, parcel_id: str, actor: str,
                           decided_at: Any | None = None) -> None:
        """企业只能解除自己的暂停；监管冻结须市场监管解除。"""
        require_role(actor, ag.ROLE_COURIER)
        parcel = self._parcel(parcel_id)
        if parcel.hold is not None and parcel.hold.by != actor:
            raise AuthorizationError("只能解除本企业作出的暂停")
        when = at(decided_at or self.clock.now())
        self.repo.propose([(parcel_id, "parcel", "ParcelReleased", {
            "by": actor, "at": when.isoformat(), "decided_at": when.isoformat(),
        }, actor, when.isoformat())])

    def scan(self, parcel_id: str, scan_type: str, actor: str, node: str = "",
             at_time: Any | None = None) -> None:
        """转运/妥投扫描。冻结期扫描会被拒绝，防止旧决定与新状态并存。"""
        require_role(actor, ag.ROLE_COURIER)
        when = at(at_time or self.clock.now())
        if scan_type == "delivered":
            event_type = "ParcelDelivered"
        else:
            event_type = "ParcelScanned"
        self.repo.propose([(parcel_id, "parcel", event_type, {
            "scan_type": scan_type, "at": when.isoformat(), "node": node,
        }, actor, when.isoformat())])

    def report_temp_interruption(self, parcel_id: str, actor: str, duration_min: int,
                                 detail: str = "", at_time: Any | None = None,
                                 lead_id: str | None = None) -> str:
        """在途温控中断：按生效规则版本生成温控线索，并计算温控时限。"""
        require_role(actor, ag.ROLE_COURIER)
        when = at(at_time or self.clock.now())
        parcel = self._parcel(parcel_id)
        rb = self.master.rulebook(parcel.rulebook_version)
        tolerance = int(rb.params.get("temp_break_tolerance_min", 120))
        deadline = when + timedelta(minutes=tolerance)
        new_lead_id = lead_id or f"lead-{uuid.uuid4().hex[:12]}"
        entries: list[tuple] = []
        if lead_id is None:
            entries.append((new_lead_id, "lead", "LeadReported", {
                "lead_id": new_lead_id, "lead_type": TEMP_BREAK,
                "level": rb.levels[TEMP_BREAK], "rulebook_version": rb.version,
                "parcel_id": parcel_id, "customer_id": parcel.customer_id,
                "reporter": actor, "reported_at": when.isoformat(),
                "evidence": {"duration_min": duration_min, "detail": detail, "stage": "transit"},
            }, actor, when.isoformat()))
        entries.append((parcel_id, "parcel", "TempInterruptionReported", {
            "at": when.isoformat(), "duration_min": duration_min, "detail": detail,
            "lead_id": new_lead_id, "deadline_at": deadline.isoformat(), "resolved": False,
        }, actor, when.isoformat(), new_lead_id))
        self.repo.propose(entries)
        return new_lead_id

    def resolve_temp_interruption(self, parcel_id: str, lead_id: str, actor: str,
                                  at_time: Any | None = None) -> None:
        """温控恢复：关闭该包裹上对应线索的温控时限计时（不改变线索处置状态）。"""
        when = at(at_time or self.clock.now())
        parcel = self._parcel(parcel_id)
        unresolved = [i for i, x in enumerate(parcel.temp_interruptions)
                      if x.get("lead_id") == lead_id and not x.get("resolved")]
        if not unresolved:
            raise NotFoundError("没有待处置的温控中断")
        self.repo.propose([(parcel_id, "parcel", "TempInterruptionResolved", {
            "lead_id": lead_id, "at": when.isoformat(),
            "indexes": unresolved,
        }, actor, when.isoformat(), lead_id)])

    def request_return(self, parcel_id: str, actor: str, reason: str = "",
                       at_time: Any | None = None) -> None:
        """退回必须能归入原包裹链路；监管冻结中的包裹须先由监管解除。"""
        require_role(actor, ag.ROLE_COURIER)
        parcel = self._parcel(parcel_id)
        if parcel.hold is not None and parcel.hold.kind == ag.HOLD_REGULATORY:
            raise ConflictError("监管冻结中的包裹不得退回，须由市场监管解除冻结")
        when = at(at_time or self.clock.now())
        self.repo.propose([(parcel_id, "parcel", "ParcelReturnRequested", {
            "at": when.isoformat(), "reason": reason,
        }, actor, when.isoformat())])

    def confirm_returned(self, parcel_id: str, actor: str,
                         at_time: Any | None = None) -> None:
        require_role(actor, ag.ROLE_COURIER)
        when = at(at_time or self.clock.now())
        self.repo.propose([(parcel_id, "parcel", "ParcelReturned", {
            "at": when.isoformat(),
        }, actor, when.isoformat())])


# --------------------------------------------------------------------- 批次


class BatchService:
    def __init__(self, store: EventStore, clock: Clock, repo: Repository) -> None:
        self.store = store
        self.clock = clock
        self.repo = repo

    def create_batch(self, batch_id: str, customer_id: str, actor: str,
                     at_time: Any | None = None) -> None:
        when = at(at_time or self.clock.now())
        if self.repo.find(batch_id, "batch") is not None:
            raise ConflictError(f"批次已存在：{batch_id}")
        self.repo.propose([(batch_id, "batch", "BatchCreated", {
            "batch_id": batch_id, "customer_id": customer_id,
        }, actor, when.isoformat())])

    def add_parcel(self, batch_id: str, parcel_id: str, actor: str,
                   at_time: Any | None = None) -> None:
        when = at(at_time or self.clock.now())
        entries = [
            (batch_id, "batch", "ParcelAddedToBatch", {
                "batch_id": batch_id, "parcel_id": parcel_id,
            }, actor, when.isoformat()),
            (parcel_id, "parcel", "ParcelAddedToBatch", {
                "batch_id": batch_id,
            }, actor, when.isoformat()),
        ]
        self.repo.propose(entries)


# --------------------------------------------------------------------- 线索处置


class LeadService:
    """市场监管对线索的独立处置；寄递企业只能上报，不能核查/放行/立案/关闭。"""

    def __init__(self, store: EventStore, clock: Clock, repo: Repository,
                 master: MasterDataView) -> None:
        self.store = store
        self.clock = clock
        self.repo = repo
        self.master = master

    def _lead(self, lead_id: str) -> ag.LeadState:
        return self.repo.get(lead_id, "lead")

    def _decide(self, lead_id: str, event_type: str, actor: str,
                decided_at: Any, note: str, extra_payload: dict[str, Any] | None = None) -> None:
        require_role(actor, ag.ROLE_REGULATOR)
        lead = self._lead(lead_id)
        if actor == lead.reporter:
            raise AuthorizationError("原上报人不能关闭或处置自己上报的线索")
        when = at(decided_at or self.clock.now())
        payload: dict[str, Any] = {
            "decided_at": when.isoformat(), "decider_role": ag.ROLE_REGULATOR,
            "note": note,
        }
        if event_type == "LeadAcceptedForCheck":
            rb = self.master.rulebook(lead.rulebook_version)
            hours = int(rb.params.get("freeze_followup_hours", 48))
            payload["check_due_at"] = (when + timedelta(hours=hours)).isoformat()
        if extra_payload:
            payload.update(extra_payload)
        self.repo.propose([(lead_id, "lead", event_type, payload, actor, when.isoformat())])

    def accept_for_check(self, lead_id: str, actor: str, note: str = "",
                         decided_at: Any | None = None) -> None:
        self._decide(lead_id, "LeadAcceptedForCheck", actor, decided_at, note)

    def release(self, lead_id: str, actor: str, note: str = "",
                decided_at: Any | None = None) -> None:
        """放行：线索放行，并解除仅因该线索施加的监管冻结（同一原子批次）。"""
        require_role(actor, ag.ROLE_REGULATOR)
        lead = self._lead(lead_id)
        if actor == lead.reporter:
            raise AuthorizationError("原上报人不能处置自己上报的线索")
        when = at(decided_at or self.clock.now())
        entries: list[tuple] = [(lead_id, "lead", "LeadReleased", {
            "decided_at": when.isoformat(), "decider_role": ag.ROLE_REGULATOR, "note": note,
        }, actor, when.isoformat())]
        for parcel in self.repo.all_of("parcel"):
            if (parcel.hold is not None and parcel.hold.kind == ag.HOLD_REGULATORY
                    and parcel.hold.lead_id == lead_id):
                entries.append((parcel.parcel_id, "parcel", "ParcelUnfrozen", {
                    "by": actor, "at": when.isoformat(), "decided_at": when.isoformat(),
                    "lead_id": lead_id,
                }, actor, when.isoformat(), lead_id))
        self.repo.propose(entries)

    def file_case(self, lead_id: str, actor: str, note: str = "",
                  decided_at: Any | None = None) -> None:
        self._decide(lead_id, "LeadFiled", actor, decided_at, note)

    def close_case(self, lead_id: str, actor: str, note: str = "",
                   decided_at: Any | None = None) -> None:
        """只有立案后的线索可以关闭；原上报人永远不能关闭自己的线索。"""
        self._decide(lead_id, "LeadCaseClosed", actor, decided_at, note)

    def freeze_parcel(self, parcel_id: str, lead_id: str, actor: str,
                      reason: str = "", decided_at: Any | None = None) -> None:
        """监管依据线索冻结包裹（在途也可冻结，冻结期间不得扫描/妥投/退回）。"""
        require_role(actor, ag.ROLE_REGULATOR)
        parcel = self.repo.get(parcel_id, "parcel")
        if parcel.lifecycle == ag.DELIVERED:
            raise ValidationError("已妥投包裹不得冻结，应转为通知责任")
        if parcel.lifecycle == ag.RETURNED:
            raise ValidationError("已退回终结的包裹不得冻结")
        when = at(decided_at or self.clock.now())
        entries: list[tuple] = [(parcel_id, "parcel", "ParcelFrozen", {
            "by": actor, "at": when.isoformat(), "reason": reason,
            "lead_id": lead_id, "decided_at": when.isoformat(),
        }, actor, when.isoformat(), lead_id)]
        if lead_id not in parcel.linked_leads:
            entries.append((parcel_id, "parcel", "ParcelRiskFlagged", {
                "lead_id": lead_id, "at": when.isoformat(),
            }, actor, when.isoformat(), lead_id))
        self.repo.propose(entries)

    def unfreeze_parcel(self, parcel_id: str, lead_id: str, actor: str,
                        decided_at: Any | None = None) -> None:
        require_role(actor, ag.ROLE_REGULATOR)
        when = at(decided_at or self.clock.now())
        self.repo.propose([(parcel_id, "parcel", "ParcelUnfrozen", {
            "by": actor, "at": when.isoformat(), "lead_id": lead_id,
            "decided_at": when.isoformat(),
        }, actor, when.isoformat(), lead_id)])

    def expand_risk(self, lead_id: str, actor: str, batch_ids: list[str],
                    decided_at: Any | None = None) -> dict[str, list[str]]:
        """风险扩大只波及实际关联批次中的未决（未发出/在途）件。

        已妥投件不被冻结，保留妥投事实并生成通知责任；已退回件不再处置。
        批次关联在同一次原子决定中固化在线索上。
        """
        require_role(actor, ag.ROLE_REGULATOR)
        lead = self._lead(lead_id)
        if actor == lead.reporter:
            raise AuthorizationError("原上报人不能处置自己上报的线索")
        when = at(decided_at or self.clock.now())
        for b in batch_ids:
            if self.repo.find(b, "batch") is None:
                raise NotFoundError(f"批次不存在：{b}")

        affected: set[str] = set()
        for batch in self.repo.all_of("batch"):
            if batch.batch_id in batch_ids:
                affected.update(batch.parcel_ids)

        entries: list[tuple] = [(lead_id, "lead", "LeadRiskExpanded", {
            "batch_ids": list(batch_ids), "decided_at": when.isoformat(),
            "decider_role": ag.ROLE_REGULATOR,
        }, actor, when.isoformat())]
        notified: list[str] = []
        frozen: list[str] = []
        for parcel_id in sorted(affected):
            parcel = self.repo.get(parcel_id, "parcel")
            if parcel.lifecycle in (ag.DELIVERED,):
                entries.append((parcel_id, "parcel", "ParcelNotificationDutyCreated", {
                    "lead_id": lead_id, "at": when.isoformat(),
                    "reason": "已妥投件受风险扩围波及，保留妥投事实，转为通知责任",
                    "duty_to": ag.ROLE_REGULATOR,
                }, actor, when.isoformat(), lead_id))
                notified.append(parcel_id)
            elif parcel.lifecycle in (ag.PENDING, ag.IN_TRANSIT):
                if parcel.hold is not None and parcel.hold.kind == ag.HOLD_REGULATORY:
                    continue
                # 企业暂停被监管冻结有序替换。
                entries.append((parcel_id, "parcel", "ParcelFrozen", {
                    "by": actor, "at": when.isoformat(), "reason": "风险扩围",
                    "lead_id": lead_id, "decided_at": when.isoformat(),
                }, actor, when.isoformat(), lead_id))
                if lead_id not in parcel.linked_leads:
                    entries.append((parcel_id, "parcel", "ParcelRiskFlagged", {
                        "lead_id": lead_id, "at": when.isoformat(),
                    }, actor, when.isoformat(), lead_id))
                frozen.append(parcel_id)
        self.repo.propose(entries)
        return {"frozen": frozen, "notified": notified}
