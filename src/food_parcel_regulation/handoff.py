"""跨部门交接、回执与统一交接时间线。

每次移交都产生一条交接记录，带：

- 发起方/接收方组织、操作人、线索与包裹范围；
- 要求回执期限与回执状态——逾期由调度器持续催办，重启不丢；
- 双方在接口上只能看到权限范围内的责任，但交接时间线本身对
  交接双方完整可见（谁、何时、移交了什么、对方何时签收回执）。

时间线条目是只增的事实流，不允许修改，保证“完整交接时间线”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .actors import Actor, Org
from .errors import ConflictError, NotFoundError, ValidationError


class ReceiptStatus(StrEnum):
    PENDING = "pending"     # 待接收方回执
    RECEIVED = "received"   # 已签收
    RETURNED = "returned"   # 退回补正
    OVERDUE = "overdue"     # 逾期（催办中，不改变签收事实）


class TimelineKind(StrEnum):
    CLUE = "clue"
    HANDOFF = "handoff"
    RECEIPT = "receipt"
    PARCEL = "parcel"
    DUTY = "duty"


@dataclass(frozen=True)
class TimelineEntry:
    at: datetime
    kind: TimelineKind
    ref_id: str
    actor_name: str
    org: str
    summary: str
    visible_orgs: frozenset[str]      # 权限范围：可见组织
    payload: dict = field(default_factory=dict)


@dataclass
class Handoff:
    handoff_id: str
    clue_id: str
    tracking_refs: list[str]
    from_org: Org
    to_org: str
    initiated_by: str
    initiated_at: datetime
    receipt_due: datetime
    receipt_status: ReceiptStatus = ReceiptStatus.PENDING
    receipt_at: datetime | None = None
    receipt_by: str | None = None
    reminders_sent: int = 0
    last_reminder_at: datetime | None = None
    note: str = ""


class HandoffRegistry:
    def __init__(self) -> None:
        self._handoffs: dict[str, Handoff] = {}
        self._seq = 0
        self.timeline: list[TimelineEntry] = []

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def initiate(self, *, clue_id: str, tracking_refs: list[str], actor: Actor,
                 to_org: str, at: datetime, receipt_due: datetime,
                 note: str = "") -> Handoff:
        if receipt_due <= at:
            raise ValidationError("回执期限必须晚于移交时间")
        handoff_id = f"H{self._next_id():08d}"
        handoff = Handoff(
            handoff_id=handoff_id, clue_id=clue_id,
            tracking_refs=list(tracking_refs), from_org=actor.org,
            to_org=to_org, initiated_by=actor.actor_id, initiated_at=at,
            receipt_due=receipt_due, note=note,
        )
        self._handoffs[handoff_id] = handoff
        orgs = frozenset({actor.org.value, to_org})
        self.timeline.append(TimelineEntry(
            at=at, kind=TimelineKind.HANDOFF, ref_id=handoff_id,
            actor_name=actor.name, org=actor.org.value,
            summary=f"{actor.org.value} → {to_org} 移交线索 {clue_id}",
            visible_orgs=orgs,
            payload={"clue_id": clue_id, "tracking_refs": list(tracking_refs),
                     "receipt_due": receipt_due.isoformat()},
        ))
        return handoff

    def acknowledge(self, handoff_id: str, *, actor: Actor, at: datetime,
                    accepted: bool = True, note: str = "") -> Handoff:
        handoff = self._get(handoff_id)
        if actor.org.value != handoff.to_org:
            raise ValidationError("只有接收方组织可以签收回执")
        if handoff.receipt_status in (ReceiptStatus.RECEIVED, ReceiptStatus.RETURNED):
            raise ConflictError(f"交接 {handoff_id} 已回执")
        handoff.receipt_status = ReceiptStatus.RECEIVED if accepted else ReceiptStatus.RETURNED
        handoff.receipt_at = at
        handoff.receipt_by = actor.actor_id
        self.timeline.append(TimelineEntry(
            at=at, kind=TimelineKind.RECEIPT, ref_id=handoff_id,
            actor_name=actor.name, org=actor.org.value,
            summary="接收方签收" if accepted else "接收方退回补正",
            visible_orgs=frozenset({handoff.from_org.value, handoff.to_org}),
            payload={"accepted": accepted, "note": note},
        ))
        return handoff

    def mark_reminder(self, handoff: Handoff, at: datetime) -> None:
        handoff.reminders_sent += 1
        handoff.last_reminder_at = at
        if handoff.receipt_status is ReceiptStatus.PENDING:
            handoff.receipt_status = ReceiptStatus.OVERDUE

    def pending_receipts(self, moment: datetime) -> list[Handoff]:
        return [h for h in self._handoffs.values()
                if h.receipt_status not in (ReceiptStatus.RECEIVED, ReceiptStatus.RETURNED)
                and h.receipt_due <= moment]

    def for_clue(self, clue_id: str) -> list[Handoff]:
        return [h for h in self._handoffs.values() if h.clue_id == clue_id]

    def _get(self, handoff_id: str) -> Handoff:
        if handoff_id not in self._handoffs:
            raise NotFoundError(f"交接 {handoff_id} 不存在")
        return self._handoffs[handoff_id]

    def get(self, handoff_id: str) -> Handoff:
        return self._get(handoff_id)

    def append_timeline(self, entry: TimelineEntry) -> None:
        self.timeline.append(entry)

    def view(self, *, org: str, ref_id: str | None = None) -> list[TimelineEntry]:
        """按权限范围返回时间线；交接双方看完整交接链条。"""

        entries = [e for e in self.timeline if org in e.visible_orgs]
        if ref_id:
            entries = [e for e in entries if e.ref_id == ref_id
                       or (e.payload.get("clue_id") == ref_id)]
        return sorted(entries, key=lambda e: e.at)

    def all(self) -> list[Handoff]:
        return list(self._handoffs.values())
