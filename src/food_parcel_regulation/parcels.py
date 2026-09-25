"""包裹状态机、扫描链路与风险传播。

状态迁移（``status_seq`` 单调递增，任何迁移都要比对调用方持有的
序号——比较并设置，CAS）：

    PICKED_UP ──暂停──> PAUSED ──恢复──> PICKED_UP
    PICKED_UP/PAUSED ──发出──> DISPATCHED ──扫描──> IN_TRANSIT ...
    任何未终态 ──风险/监管冻结──> FROZEN ──监管放行──> 冻结前状态
    DISPATCHED/IN_TRANSIT ──退回──> RETURNING ──> RETURNED
    IN_TRANSIT ──妥投──> DELIVERED

一致性约束：

- 寄递企业只能暂停/恢复**尚未发出**的包裹；已发出件不能被企业
  自行暂停，只能因风险被冻结。
- 冻结与放行互斥且都走 CAS：部门移交决定与包裹扫描即使同时到达，
  序号较小的一方提交后，另一方立即版本冲突，不会出现“放行与冻结
  并存”或旧决定覆盖新扫描状态。
- 已妥投是不可变事实：风险扩大时 DELIVERED 不回改，只生成通知
  责任（通知/召回），事实保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .actors import Actor, Role
from .errors import ConflictError, NotFoundError


class ParcelStatus(StrEnum):
    PICKED_UP = "picked_up"     # 已揽收，未发出
    PAUSED = "paused"           # 企业暂停（仅未发出可进入）
    DISPATCHED = "dispatched"   # 已发出
    IN_TRANSIT = "in_transit"   # 转运中
    FROZEN = "frozen"           # 风险/监管冻结
    RETURNING = "returning"     # 退回中
    RETURNED = "returned"       # 已退回
    DELIVERED = "delivered"     # 已妥投（终态事实，不可回改）


_TERMINAL = {ParcelStatus.RETURNED, ParcelStatus.DELIVERED}
_MOVABLE = {ParcelStatus.DISPATCHED, ParcelStatus.IN_TRANSIT, ParcelStatus.RETURNING}
_UNSHIPPED = {ParcelStatus.PICKED_UP, ParcelStatus.PAUSED}


@dataclass(frozen=True)
class ParcelEvent:
    seq: int
    at: datetime
    actor_id: str
    actor_name: str
    action: str
    old_status: ParcelStatus | None
    new_status: ParcelStatus | None
    node: str | None = None         # 转运节点
    detail: str = ""


@dataclass
class ColdChainState:
    """冷链温控计时状态，重启后据此继续计时。"""

    requirement: str
    max_interruption_seconds: float     # 允许的累计/单次中断上限
    interrupted_since: datetime | None = None  # 当前中断起点（None=链正常）
    accumulated_interruption_seconds: float = 0.0
    last_temp_celsius: float | None = None
    last_reading_at: datetime | None = None


@dataclass
class Parcel:
    tracking_no: str
    serial: str                  # 揽件流水（链路根）
    batch_id: str
    customer_id: str
    branch_id: str
    status: ParcelStatus
    created_at: datetime
    status_seq: int = 0
    frozen_from: ParcelStatus | None = None   # 冻结前状态，放行后回到它
    frozen_by_clue: str | None = None
    events: list[ParcelEvent] = field(default_factory=list)
    cold_chain: ColdChainState | None = None
    delivered_at: datetime | None = None
    notified: bool = False       # 妥投后被风险波及，是否已转通知责任

    def require_seq(self, expected: int | None) -> None:
        if expected is not None and expected != self.status_seq:
            raise ConflictError(
                f"包裹 {self.tracking_no} 状态已更新（序号 {self.status_seq}，"
                f"提交基于序号 {expected}），该决定已过期，请刷新后重试"
            )


@dataclass(frozen=True)
class ScanEvent:
    """转运/退回扫描，归入包裹链路。"""

    tracking_no: str
    at: datetime
    node: str
    kind: str                     # transit / out_for_delivery / return / delivered
    actor_id: str


@dataclass
class NotificationDuty:
    """已妥投件被风险波及时：妥投事实保留，转为通知/召回责任。"""

    duty_id: str
    tracking_no: str
    batch_id: str
    customer_id: str
    clue_id: str
    created_at: datetime
    reason: str
    fulfilled_at: datetime | None = None
    fulfilled_by: str | None = None


class ParcelRegistry:
    def __init__(self) -> None:
        self._parcels: dict[str, Parcel] = {}
        self._scans: list[ScanEvent] = []
        self._duties: list[NotificationDuty] = []
        self._duty_seq = 0

    def _next_duty(self) -> int:
        self._duty_seq += 1
        return self._duty_seq

    # ---- 基础 ----
    def add(self, parcel: Parcel) -> None:
        if parcel.tracking_no in self._parcels:
            raise ConflictError(f"运单号 {parcel.tracking_no} 已存在")
        self._parcels[parcel.tracking_no] = parcel
        parcel.events.append(ParcelEvent(
            seq=parcel.status_seq, at=parcel.created_at, actor_id="system",
            actor_name="系统", action="create", old_status=None,
            new_status=parcel.status,
        ))

    def get(self, tracking_no: str) -> Parcel:
        if tracking_no not in self._parcels:
            raise NotFoundError(f"运单 {tracking_no} 不存在")
        return self._parcels[tracking_no]

    def batch(self, batch_id: str) -> list[Parcel]:
        return [p for p in self._parcels.values() if p.batch_id == batch_id]

    def in_transit_for_customer(self, customer_id: str) -> list[Parcel]:
        return [p for p in self._parcels.values()
                if p.customer_id == customer_id and p.status in _MOVABLE]

    def scans_for(self, tracking_no: str) -> list[ScanEvent]:
        return [s for s in self._scans if s.tracking_no == tracking_no]

    def duties(self, *, pending_only: bool = False) -> list[NotificationDuty]:
        return [d for d in self._duties if not (pending_only and d.fulfilled_at)]

    def _transition(self, parcel: Parcel, new_status: ParcelStatus, *,
                    actor: Actor, at: datetime, expected_seq: int | None,
                    action: str, node: str | None = None, detail: str = "") -> None:
        parcel.require_seq(expected_seq)
        old = parcel.status
        parcel.status = new_status
        parcel.status_seq += 1
        parcel.events.append(ParcelEvent(
            seq=parcel.status_seq, at=at, actor_id=actor.actor_id,
            actor_name=actor.name, action=action, old_status=old,
            new_status=new_status, node=node, detail=detail,
        ))
        if new_status is ParcelStatus.DELIVERED:
            parcel.delivered_at = at

    # ---- 企业：暂停/恢复（仅未发出）/发出 ----
    def pause_unshipped(self, tracking_no: str, *, actor: Actor, at: datetime,
                        expected_seq: int | None = None, reason: str = "") -> Parcel:
        actor.require(Role.COURIER_STAFF, Role.POSTAL_ADMIN)
        parcel = self.get(tracking_no)
        if parcel.status is not ParcelStatus.PICKED_UP:
            raise ConflictError(
                f"只有未发出的包裹可以暂停，{tracking_no} 当前 {parcel.status.value}"
            )
        self._transition(parcel, ParcelStatus.PAUSED, actor=actor, at=at,
                         expected_seq=expected_seq, action="pause", detail=reason)
        return parcel

    def resume_unshipped(self, tracking_no: str, *, actor: Actor, at: datetime,
                         expected_seq: int | None = None) -> Parcel:
        actor.require(Role.COURIER_STAFF, Role.POSTAL_ADMIN)
        parcel = self.get(tracking_no)
        if parcel.status is not ParcelStatus.PAUSED:
            raise ConflictError(f"{tracking_no} 未处于暂停状态")
        self._transition(parcel, ParcelStatus.PICKED_UP, actor=actor, at=at,
                         expected_seq=expected_seq, action="resume")
        return parcel

    def dispatch(self, tracking_no: str, *, actor: Actor, at: datetime,
                 expected_seq: int | None = None) -> Parcel:
        actor.require(Role.COURIER_STAFF, Role.POSTAL_ADMIN)
        parcel = self.get(tracking_no)
        if parcel.status not in _UNSHIPPED:
            raise ConflictError(f"{tracking_no} 当前 {parcel.status.value}，不能发出")
        self._transition(parcel, ParcelStatus.DISPATCHED, actor=actor, at=at,
                         expected_seq=expected_seq, action="dispatch")
        return parcel

    # ---- 扫描：转运/退回/妥投，冻结中一律拒绝推进 ----
    def scan(self, tracking_no: str, *, actor: Actor, at: datetime, node: str,
             kind: str, expected_seq: int | None = None) -> Parcel:
        parcel = self.get(tracking_no)
        if parcel.status is ParcelStatus.FROZEN:
            raise ConflictError(
                f"包裹 {tracking_no} 已冻结，节点 {node} 扫描不得推进，"
                "须等待监管放行"
            )
        if parcel.status in _TERMINAL:
            raise ConflictError(f"包裹 {tracking_no} 已 {parcel.status.value}，不能再扫描")

        if kind == "delivered":
            target = ParcelStatus.DELIVERED
            action = "deliver"
        elif kind == "return":
            target = ParcelStatus.RETURNING
            action = "return"
        elif kind in ("transit", "out_for_delivery"):
            target = ParcelStatus.IN_TRANSIT
            action = "transit_scan"
        else:
            raise ConflictError(f"未知扫描类型 {kind}")

        self._transition(parcel, target, actor=actor, at=at,
                         expected_seq=expected_seq, action=action, node=node)
        self._scans.append(ScanEvent(
            tracking_no=tracking_no, at=at, node=node, kind=kind,
            actor_id=actor.actor_id,
        ))
        return parcel

    def confirm_returned(self, tracking_no: str, *, actor: Actor, at: datetime,
                         expected_seq: int | None = None) -> Parcel:
        parcel = self.get(tracking_no)
        if parcel.status is not ParcelStatus.RETURNING:
            raise ConflictError(f"{tracking_no} 不在退回中")
        self._transition(parcel, ParcelStatus.RETURNED, actor=actor, at=at,
                         expected_seq=expected_seq, action="return_confirmed")
        return parcel

    # ---- 冻结/放行（风险与监管措施，互斥且原子）----
    def freeze(self, tracking_no: str, *, actor: Actor, at: datetime,
               clue_id: str, expected_seq: int | None = None,
               reason: str = "") -> Parcel:
        parcel = self.get(tracking_no)
        if parcel.status is ParcelStatus.FROZEN:
            # 已冻结：幂等，不产生并存记录；但证据线索要挂在调用方线索链
            return parcel
        if parcel.status in _TERMINAL:
            raise ConflictError(
                f"包裹 {tracking_no} 已{parcel.status.value}，终态事实不能冻结"
            )
        parcel.frozen_from = parcel.status
        parcel.frozen_by_clue = clue_id
        self._transition(parcel, ParcelStatus.FROZEN, actor=actor, at=at,
                         expected_seq=expected_seq, action="freeze",
                         detail=reason or f"依据线索 {clue_id}")
        return parcel

    def release_freeze(self, tracking_no: str, *, actor: Actor, at: datetime,
                       expected_seq: int | None = None, reason: str = "") -> Parcel:
        """监管放行后解除冻结，回到冻结前状态。

        CAS 必须先于任何字段修改：序号过期时直接失败，不留下
        “已冻结但 frozen_by_clue 被清空”的半放行记录。
        """

        actor.require(Role.MARKET_REGULATOR)
        parcel = self.get(tracking_no)
        if parcel.status is not ParcelStatus.FROZEN:
            raise ConflictError(f"包裹 {tracking_no} 未冻结，不能放行解锢")
        parcel.require_seq(expected_seq)
        target = parcel.frozen_from or ParcelStatus.PICKED_UP
        clue_id = parcel.frozen_by_clue
        self._transition(parcel, target, actor=actor, at=at,
                         expected_seq=expected_seq, action="release_freeze",
                         detail=reason or f"依据线索 {clue_id} 放行")
        # CAS 通过、状态迁移完成后才清理冻结锚点
        parcel.frozen_from = None
        parcel.frozen_by_clue = None
        return parcel

    # ---- 风险扩大：只波及实际关联件，妥投转通知 ----
    def propagate_batch(self, batch_id: str, *, actor: Actor, at: datetime,
                        clue_id: str, reason: str) -> dict:
        """把风险扩大到一个批次：可处置件冻结，妥投件转通知责任。

        企业自身可因在途风险申请扩大范围；此处要求发起方承担记录，
        实际冻结/放行仍受各方法律角色约束，通知责任只针对事实妥投件。
        """

        frozen: list[str] = []
        notified: list[str] = []
        for parcel in self.batch(batch_id):
            if parcel.status is ParcelStatus.DELIVERED:
                # 妥投事实保留，不改状态，转通知/召回责任。
                if not parcel.notified:
                    parcel.notified = True
                    duty = NotificationDuty(
                        duty_id=f"N{self._next_duty():08d}",
                        tracking_no=parcel.tracking_no, batch_id=batch_id,
                        customer_id=parcel.customer_id, clue_id=clue_id,
                        created_at=at, reason=reason,
                    )
                    self._duties.append(duty)
                    parcel.events.append(ParcelEvent(
                        seq=parcel.status_seq, at=at, actor_id=actor.actor_id,
                        actor_name=actor.name, action="notify_duty",
                        old_status=parcel.status, new_status=parcel.status,
                        detail=f"妥投事实保留，转通知责任 {duty.duty_id}",
                    ))
                    notified.append(parcel.tracking_no)
                continue
            if parcel.status in _TERMINAL or parcel.status is ParcelStatus.FROZEN:
                continue
            self.freeze(parcel.tracking_no, actor=actor, at=at,
                        clue_id=clue_id, reason=reason)
            frozen.append(parcel.tracking_no)
        return {"frozen": frozen, "notified": notified}

    def fulfill_duty(self, duty_id: str, *, actor: Actor, at: datetime) -> NotificationDuty:
        for duty in self._duties:
            if duty.duty_id == duty_id:
                if duty.fulfilled_at is not None:
                    raise ConflictError(f"通知责任 {duty_id} 已履行")
                duty.fulfilled_at = at
                duty.fulfilled_by = actor.actor_id
                return duty
        raise NotFoundError(f"通知责任 {duty_id} 不存在")

    def all(self) -> list[Parcel]:
        return list(self._parcels.values())
