"""聚合状态与纯函数重放规则。

不变量全部编码在 reducer 中，任何命令在提交前都会用同一套规则预演，
因此不会写出入库后才发现非法的事件；重启重放得到完全一致的状态。

关键互斥不变量（包裹）：
- ``enterprise_hold``（企业暂停）与 ``regulatory_freeze``（监管冻结）任一时刻至多一个生效；
- 决定类事件带 ``decided_at``，早于该聚合最新决定的旧事件一律拒绝（旧决定不覆盖新状态）；
- 企业只能暂停尚未发出（PENDING）的包裹，且只能解除自己的暂停；
- 监管冻结期间不允许发出/妥投扫描；已妥投的包裹不再被冻结，转为通知责任。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ConflictError, DomainError, NotFoundError, StaleDecisionError, ValidationError
from .eventstore import Event
from .timemodel import at

# 包裹生命周期
PENDING = "pending"          # 已揽收未发出
IN_TRANSIT = "in_transit"
DELIVERED = "delivered"
RETURNED = "returned"

# 暂停/冻结
HOLD_ENTERPRISE = "enterprise_hold"
HOLD_REGULATORY = "regulatory_freeze"

# 线索状态
LEAD_REPORTED = "reported"
LEAD_CHECKING = "checking"
LEAD_RELEASED = "released"
LEAD_FILED = "filed"
LEAD_CLOSED = "closed"

ROLE_COURIER = "courier"
ROLE_POSTAL = "postal"
ROLE_REGULATOR = "regulator"


def _dt(value: str):
    return at(value)


# --------------------------------------------------------------------- 揽件


@dataclass
class PickupState:
    serial_no: str = ""
    request_hash: str = ""
    first_observed_at: str = ""
    parcel_id: str | None = None
    accepted: bool = False
    quarantined: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _reduce_pickup(state: PickupState | None, event: Event) -> PickupState:
    state = state or PickupState()
    p = event.payload
    if event.event_type == "PickupAccepted":
        if state.accepted:
            raise ConflictError("相同流水已生成揽件，不得二次揽件")
        state.serial_no = p["serial_no"]
        state.request_hash = p["request_hash"]
        state.first_observed_at = p["observed_at"]
        state.parcel_id = p["parcel_id"]
        state.accepted = True
    elif event.event_type == "PickupConflictQuarantined":
        state.quarantined = True
        state.conflicts.append({"hash": p["request_hash"], "at": event.occurred_at})
    return state


# --------------------------------------------------------------------- 包裹


@dataclass
class Hold:
    kind: str
    lead_id: str | None
    by: str
    at: str
    reason: str = ""
    decided_at: str = ""


@dataclass
class ParcelState:
    parcel_id: str = ""
    waybill_no: str = ""
    serial_no: str = ""
    customer_id: str = ""
    batch_id: str | None = None
    rulebook_version: str = ""
    lifecycle: str = PENDING
    hold: Hold | None = None
    last_decided_at: str | None = None
    cold_chain: dict[str, Any] = field(default_factory=dict)
    temp_interruptions: list[dict[str, Any]] = field(default_factory=list)
    temp_leads: list[str] = field(default_factory=list)
    linked_leads: list[str] = field(default_factory=list)
    propagation_flags: list[dict[str, Any]] = field(default_factory=list)
    notification_duties: list[dict[str, Any]] = field(default_factory=list)
    scans: list[dict[str, Any]] = field(default_factory=list)
    returned: bool = False
    return_info: dict[str, Any] | None = None


def _check_decision_order(state: ParcelState, decided_at: str) -> None:
    if state.last_decided_at is not None and _dt(decided_at) < _dt(state.last_decided_at):
        raise StaleDecisionError(
            f"决定时间 {decided_at} 早于该包裹最新决定 {state.last_decided_at}，旧决定不得覆盖新状态"
        )


def _reduce_parcel(state: ParcelState | None, event: Event) -> ParcelState:
    state = state or ParcelState()
    p = event.payload
    t = event.event_type

    if t == "ParcelAccepted":
        if state.parcel_id:
            raise ConflictError("包裹已存在")
        state.parcel_id = p["parcel_id"]
        state.waybill_no = p["waybill_no"]
        state.serial_no = p["serial_no"]
        state.customer_id = p["customer_id"]
        state.batch_id = p.get("batch_id")
        state.rulebook_version = p["rulebook_version"]
        cold = p.get("cold_chain")
        if cold:
            state.cold_chain = dict(cold)

    elif t == "ParcelHeld":
        _check_decision_order(state, p["decided_at"])
        if state.lifecycle != PENDING:
            raise ValidationError("企业只能暂停尚未发出的包裹")
        if state.hold is not None:
            raise ConflictError("包裹已处于暂停/冻结状态，不得叠加暂停")
        state.hold = Hold(HOLD_ENTERPRISE, p.get("lead_id"), p["by"], p["at"],
                          p.get("reason", ""), p["decided_at"])
        state.last_decided_at = p["decided_at"]

    elif t == "ParcelReleased":
        _check_decision_order(state, p["decided_at"])
        if state.hold is None or state.hold.kind != HOLD_ENTERPRISE:
            raise ValidationError("企业只能解除自己作出的暂停，监管冻结须由市场监管解除")
        state.hold = None
        state.last_decided_at = p["decided_at"]

    elif t == "ParcelFrozen":
        _check_decision_order(state, p["decided_at"])
        if state.lifecycle in (DELIVERED, RETURNED):
            raise ValidationError("已妥投或已退回终结的包裹不得冻结，应转为通知责任")
        if state.hold is not None and state.hold.kind == HOLD_REGULATORY:
            raise ConflictError("包裹已被监管冻结，不得重复冻结")
        # 企业暂停被监管冻结有序替换：时间线上先放行企业暂停再冻结，不存在并存。
        state.hold = Hold(HOLD_REGULATORY, p.get("lead_id"), p["by"], p["at"],
                          p.get("reason", ""), p["decided_at"])
        state.last_decided_at = p["decided_at"]
        if p.get("lead_id") and p["lead_id"] not in state.linked_leads:
            state.linked_leads.append(p["lead_id"])

    elif t == "ParcelUnfrozen":
        _check_decision_order(state, p["decided_at"])
        if state.hold is None or state.hold.kind != HOLD_REGULATORY:
            raise ValidationError("包裹未处于监管冻结状态")
        state.hold = None
        state.last_decided_at = p["decided_at"]

    elif t == "ParcelScanned":
        scan_type = p["scan_type"]
        if state.returned:
            raise ValidationError("已退回终结的包裹不再接受转运扫描")
        # 暂停/冻结优先于流转阶段校验：冻结期间任何扫描都不允许，
        # 避免“部门移交决定与包裹扫描同时发生”时留下继续移动的记录。
        if state.hold is not None and scan_type != "attempt_deliver":
            raise ConflictError("包裹处于暂停/冻结状态，不得继续扫描流转")
        if scan_type == "depart":
            if state.lifecycle not in (PENDING, IN_TRANSIT):
                raise ValidationError("当前状态不允许发出扫描")
            state.lifecycle = IN_TRANSIT
        elif scan_type in ("arrive", "transfer"):
            if state.lifecycle != IN_TRANSIT:
                raise ValidationError("包裹尚未进入在途状态")
        elif scan_type == "attempt_deliver":
            if state.lifecycle != IN_TRANSIT:
                raise ValidationError("包裹不在在途状态")
            if state.hold is not None and state.hold.kind == HOLD_REGULATORY:
                raise ConflictError("监管冻结中的包裹不得妥投")
        state.scans.append({"scan_type": scan_type, "at": p["at"], "node": p.get("node", "")})

    elif t == "ParcelDelivered":
        if state.hold is not None and state.hold.kind == HOLD_REGULATORY:
            raise ConflictError("监管冻结中的包裹不得妥投")
        if state.lifecycle != IN_TRANSIT:
            raise ValidationError("只有在途包裹可以妥投")
        state.lifecycle = DELIVERED
        state.hold = None
        state.scans.append({"scan_type": "delivered", "at": p["at"], "node": p.get("node", "")})

    elif t == "TempInterruptionReported":
        state.temp_interruptions.append({
            "at": p["at"], "duration_min": p.get("duration_min", 0),
            "detail": p.get("detail", ""), "lead_id": p.get("lead_id"),
            "deadline_at": p.get("deadline_at"), "resolved": False,
        })
        if p.get("lead_id") and p["lead_id"] not in state.temp_leads:
            state.temp_leads.append(p["lead_id"])
            if p["lead_id"] not in state.linked_leads:
                state.linked_leads.append(p["lead_id"])

    elif t == "TempInterruptionResolved":
        hit = False
        for idx in p.get("indexes", []):
            if 0 <= idx < len(state.temp_interruptions):
                state.temp_interruptions[idx]["resolved"] = True
                state.temp_interruptions[idx]["resolved_at"] = p["at"]
                hit = True
        if not hit:
            raise NotFoundError("没有待处置的温控中断")

    elif t == "ParcelRiskFlagged":
        state.propagation_flags.append({"lead_id": p["lead_id"], "at": p["at"]})
        if p["lead_id"] not in state.linked_leads:
            state.linked_leads.append(p["lead_id"])

    elif t == "ParcelNotificationDutyCreated":
        state.notification_duties.append({
            "lead_id": p["lead_id"], "reason": p["reason"], "at": p["at"],
            "duty_to": p["duty_to"],
        })
        if p["lead_id"] not in state.linked_leads:
            state.linked_leads.append(p["lead_id"])

    elif t == "ParcelReturnRequested":
        if state.lifecycle not in (PENDING, IN_TRANSIT):
            raise ValidationError("当前状态不允许退回")
        state.return_info = {"requested_at": p["at"], "reason": p.get("reason", "")}

    elif t == "ParcelReturned":
        if state.return_info is None:
            raise ValidationError("退回必须先有退回请求，保证归入原链路")
        state.lifecycle = RETURNED
        state.returned = True
        state.hold = None
        state.return_info.update(returned_at=p["at"])

    elif t == "ParcelAddedToBatch":
        state.batch_id = p["batch_id"]

    else:
        raise DomainError(f"包裹聚合无法处理事件：{t}")
    return state


# --------------------------------------------------------------------- 批次


@dataclass
class BatchState:
    batch_id: str = ""
    customer_id: str = ""
    parcel_ids: list[str] = field(default_factory=list)


def _reduce_batch(state: BatchState | None, event: Event) -> BatchState:
    state = state or BatchState()
    p = event.payload
    if event.event_type == "BatchCreated":
        state.batch_id = p["batch_id"]
        state.customer_id = p["customer_id"]
    elif event.event_type == "ParcelAddedToBatch":
        if p["parcel_id"] not in state.parcel_ids:
            state.parcel_ids.append(p["parcel_id"])
    else:
        raise DomainError(f"批次聚合无法处理事件：{event.event_type}")
    return state


# --------------------------------------------------------------------- 线索


@dataclass
class LeadState:
    lead_id: str = ""
    lead_type: str = ""
    level: str = ""
    rulebook_version: str = ""
    parcel_id: str = ""
    customer_id: str = ""
    reporter: str = ""
    status: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    last_decided_at: str | None = None
    propagation_batches: list[str] = field(default_factory=list)
    check_due_at: str | None = None


_LEAD_TRANSITIONS = {
    LEAD_REPORTED: {LEAD_CHECKING},
    LEAD_CHECKING: {LEAD_RELEASED, LEAD_FILED},
    LEAD_FILED: {LEAD_CLOSED},
    LEAD_RELEASED: set(),
    LEAD_CLOSED: set(),
}


def _reduce_lead(state: LeadState | None, event: Event) -> LeadState:
    state = state or LeadState()
    p = event.payload
    t = event.event_type

    if t == "LeadReported":
        state.lead_id = p["lead_id"]
        state.lead_type = p["lead_type"]
        state.level = p["level"]
        state.rulebook_version = p["rulebook_version"]
        state.parcel_id = p["parcel_id"]
        state.customer_id = p["customer_id"]
        state.reporter = p["reporter"]
        state.evidence = dict(p.get("evidence", {}))
        state.status = LEAD_REPORTED
        state.history.append({"status": LEAD_REPORTED, "at": p["reported_at"], "by": p["reporter"]})
        return state

    if not state.status:
        raise DomainError("线索尚未上报")
    # 职权分离：原上报人不能对自己的线索作核查/放行/立案/关闭/扩围等任何处置决定。
    if event.actor == state.reporter:
        raise ValidationError("原上报人不能处置自己上报的线索")
    if p.get("decider_role") != ROLE_REGULATOR:
        raise ValidationError("只有市场监管人员可以决定核查、放行、立案或扩大风险范围")

    # 风险扩围不改变线索状态，只登记实际波及的批次；线索终结后不得再扩围。
    if t == "LeadRiskExpanded":
        if state.status in (LEAD_RELEASED, LEAD_CLOSED):
            raise ConflictError("线索已终结，不得再扩大风险范围")
        for b in p["batch_ids"]:
            if b not in state.propagation_batches:
                state.propagation_batches.append(b)
        state.history.append({"status": state.status, "at": p["decided_at"],
                              "by": event.actor, "note": "风险扩围",
                              "batch_ids": list(p["batch_ids"])})
        return state

    target = {
        "LeadAcceptedForCheck": LEAD_CHECKING,
        "LeadReleased": LEAD_RELEASED,
        "LeadFiled": LEAD_FILED,
        "LeadCaseClosed": LEAD_CLOSED,
    }.get(t)
    if target is None:
        raise DomainError(f"线索聚合无法处理事件：{t}")
    if target not in _LEAD_TRANSITIONS[state.status]:
        raise ConflictError(f"线索不能从 {state.status} 转为 {target}")
    decided_at = p["decided_at"]
    if state.last_decided_at is not None and _dt(decided_at) < _dt(state.last_decided_at):
        raise StaleDecisionError("旧的处置决定不得覆盖线索新状态")
    state.status = target
    state.last_decided_at = decided_at
    state.history.append({"status": target, "at": decided_at, "by": event.actor,
                          "note": p.get("note", "")})
    if t == "LeadAcceptedForCheck" and p.get("check_due_at"):
        state.check_due_at = p["check_due_at"]
    return state


# --------------------------------------------------------------------- 移交


@dataclass
class HandoverState:
    handover_id: str = ""
    lead_id: str = ""
    parcel_ids: list[str] = field(default_factory=list)
    from_org: str = ""
    to_org: str = ""
    initiated_at: str = ""
    due_at: str | None = None
    status: str = ""  # initiated / received
    received_at: str | None = None
    receiver: str | None = None
    reminders: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)


def _reduce_handover(state: HandoverState | None, event: Event) -> HandoverState:
    state = state or HandoverState()
    p = event.payload
    t = event.event_type
    if t == "HandoverInitiated":
        if state.handover_id:
            raise ConflictError("移交已发起")
        state.handover_id = p["handover_id"]
        state.lead_id = p["lead_id"]
        state.parcel_ids = list(p.get("parcel_ids", []))
        state.from_org = p["from_org"]
        state.to_org = p["to_org"]
        state.initiated_at = p["at"]
        state.due_at = p.get("due_at")
        state.status = "initiated"
        state.timeline.append({"event": t, "at": p["at"], "by": event.actor})
    elif t == "HandoverReceived":
        if state.status != "initiated":
            raise ConflictError("只有待接收的移交可以出具回执")
        state.status = "received"
        state.received_at = p["at"]
        state.receiver = event.actor
        state.timeline.append({"event": t, "at": p["at"], "by": event.actor})
    elif t == "ReceiptReminded":
        state.reminders.append({"at": p["at"], "level": p.get("level", 1)})
        state.timeline.append({"event": t, "at": p["at"], "by": event.actor})
    else:
        raise DomainError(f"移交聚合无法处理事件：{t}")
    return state


# --------------------------------------------------------------------- 投诉


@dataclass
class ComplaintState:
    complaint_id: str = ""
    parcel_id: str = ""
    lead_id: str | None = None
    summary: str = ""
    filed_at: str = ""
    routed_to: list[str] = field(default_factory=list)
    resolved: bool = False
    resolution: dict[str, Any] | None = None


def _reduce_complaint(state: ComplaintState | None, event: Event) -> ComplaintState:
    state = state or ComplaintState()
    p = event.payload
    t = event.event_type
    if t == "ComplaintFiled":
        state.complaint_id = p["complaint_id"]
        state.parcel_id = p["parcel_id"]
        state.lead_id = p.get("lead_id")
        state.summary = p.get("summary", "")
        state.filed_at = p["at"]
    elif t == "ComplaintRouted":
        if p["to"] not in state.routed_to:
            state.routed_to.append(p["to"])
    elif t == "ComplaintResolved":
        state.resolved = True
        state.resolution = {"at": p["at"], "note": p.get("note", "")}
    else:
        raise DomainError(f"投诉聚合无法处理事件：{t}")
    return state


def _reduce_master(state: Any, event: Event) -> dict[str, Any]:
    """主数据聚合（客户/资质/场所/仓库/核验码/规则书）的占位状态。

    时态查询由 :class:`MasterDataView` 负责；仓储只需跳过这些聚合，
    让事件日志能被统一重放（含重启恢复）。
    """
    return state if isinstance(state, dict) else {"_master": True}


_REDUCERS = {
    "pickup": _reduce_pickup,
    "parcel": _reduce_parcel,
    "batch": _reduce_batch,
    "lead": _reduce_lead,
    "handover": _reduce_handover,
    "complaint": _reduce_complaint,
    "customer": _reduce_master,
    "qualification": _reduce_master,
    "premises": _reduce_master,
    "warehouse": _reduce_master,
    "verification_code": _reduce_master,
    "rulebook": _reduce_master,
}


def reduce(aggregate_id: str, aggregate_type: str, state: Any, event: Event) -> Any:
    reducer = _REDUCERS.get(aggregate_type)
    if reducer is None:
        raise DomainError(f"未知聚合类型：{aggregate_type}")
    return reducer(state, event)
