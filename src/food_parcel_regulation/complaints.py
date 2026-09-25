"""投诉受理：投诉必须归入一条已存在的正确链路。

链路锚点三选一，决定投诉挂在哪里、随哪条时间线流转：

- ``serial``：揽件验视链路（对验视、暂停有异议）
- ``tracking_no``：包裹链路（转运、退回、温控、妥投问题）
- ``clue_id``：线索处置链路（对核查/立案措施有异议）

投诉有自己的处置状态，但不产生包裹处置权，也不能用来关闭线索；
它只是把外部声音接到正确事实上，避免“投诉进来却找不到对应件”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .errors import ConflictError, NotFoundError, ValidationError


class ComplaintChain(StrEnum):
    PICKUP = "pickup"        # 揽件验视链路
    PARCEL = "parcel"        # 包裹转运/退回链路
    CLUE = "clue"            # 线索处置链路


class ComplaintStatus(StrEnum):
    OPEN = "open"
    RESPONDING = "responding"
    ANSWERED = "answered"      # 已答复
    REJECTED = "rejected"      # 不予受理（锚点不存在等）


@dataclass(frozen=True)
class ComplaintEvent:
    at: datetime
    actor_id: str
    action: str
    detail: str = ""


@dataclass
class Complaint:
    complaint_id: str
    chain: ComplaintChain
    anchor: str                       # serial / tracking_no / clue_id
    customer_id: str
    summary: str
    opened_at: datetime
    status: ComplaintStatus = ComplaintStatus.OPEN
    handler_id: str | None = None
    events: list[ComplaintEvent] = field(default_factory=list)


class ComplaintRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Complaint] = {}
        self._seq = 0

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def open(
        self,
        *,
        chain: ComplaintChain,
        anchor: str,
        anchor_exists: bool,
        customer_id: str,
        summary: str,
        at: datetime,
    ) -> Complaint:
        """登记投诉。``anchor_exists`` 由服务层用各注册表核验后传入。"""

        if not anchor:
            raise ValidationError("投诉必须指定链路锚点")
        if not anchor_exists:
            raise NotFoundError(
                f"投诉锚点 {chain.value}:{anchor} 不存在，不能归入空链路"
            )
        complaint = Complaint(
            complaint_id=f"T{self._next_id():08d}",
            chain=chain,
            anchor=anchor,
            customer_id=customer_id,
            summary=summary,
            opened_at=at,
        )
        complaint.events.append(ComplaintEvent(at=at, actor_id=customer_id,
                                               action="open", detail=summary))
        self._items[complaint.complaint_id] = complaint
        return complaint

    def process(self, complaint_id: str, *, actor_id: str, at: datetime,
                answer: str, reject: bool = False) -> Complaint:
        complaint = self._get(complaint_id)
        if complaint.status in (ComplaintStatus.ANSWERED, ComplaintStatus.REJECTED):
            raise ConflictError(f"投诉 {complaint_id} 已办结")
        complaint.status = ComplaintStatus.REJECTED if reject else ComplaintStatus.ANSWERED
        complaint.handler_id = actor_id
        complaint.events.append(ComplaintEvent(
            at=at, actor_id=actor_id,
            action="reject" if reject else "answer", detail=answer,
        ))
        return complaint

    def _get(self, complaint_id: str) -> Complaint:
        if complaint_id not in self._items:
            raise NotFoundError(f"投诉 {complaint_id} 不存在")
        return self._items[complaint_id]

    def get(self, complaint_id: str) -> Complaint:
        return self._get(complaint_id)

    def for_anchor(self, chain: ComplaintChain, anchor: str) -> list[Complaint]:
        return [c for c in self._items.values()
                if c.chain is chain and c.anchor == anchor]

    def all(self) -> list[Complaint]:
        return list(self._items.values())
