"""部门移交、投诉归链、到期调度与权限责任视图。

移交与包裹扫描即使同时发生也不会互相覆盖：它们是对各自聚合的原子事件批次，
扫描侧依据冻结状态被接受或拒绝，决定侧带 ``decided_at`` 单调约束；
任何接口读到的都是同一事件流物化出的一致状态，时间线完整可追溯。

所有“待办”都不单独持久化计时器，而是在每次（含重启后）从事件状态重建：
温控时限、待核查事项、回执催办到期后继续生效。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from . import aggregates as ag
from .application import Repository, require_role, role_of
from .errors import AuthorizationError
from .masterdata import MasterDataView
from .timemodel import Clock, at


# --------------------------------------------------------------------- 移交


class HandoverService:
    """寄递企业（邮政管理侧转交）与市场监管之间的线索/包裹移交。"""

    def __init__(self, store, clock: Clock, repo: Repository, master: MasterDataView) -> None:
        self.store = store
        self.clock = clock
        self.repo = repo
        self.master = master

    def initiate(self, lead_id: str, actor: str, parcel_ids: list[str] | None = None,
                 to_org: str = "市场监管", due_hours: int | None = None,
                 at_time: Any | None = None) -> str:
        require_role(actor, ag.ROLE_POSTAL)
        when = at(at_time or self.clock.now())
        lead = self.repo.get(lead_id, "lead")
        parcel_ids = parcel_ids or [lead.parcel_id]
        for pid in parcel_ids:
            self.repo.get(pid, "parcel")
        rb = self.master.rulebook(lead.rulebook_version)
        due = None
        hours = due_hours if due_hours is not None else int(rb.params.get("receipt_due_hours", 24))
        due = (when + timedelta(hours=hours)).isoformat()
        handover_id = f"handover-{uuid.uuid4().hex[:12]}"
        self.repo.propose([(handover_id, "handover", "HandoverInitiated", {
            "handover_id": handover_id, "lead_id": lead_id,
            "parcel_ids": parcel_ids, "from_org": "寄递企业/邮政管理",
            "to_org": to_org, "at": when.isoformat(), "due_at": due,
        }, actor, when.isoformat(), lead_id)])
        return handover_id

    def receive(self, handover_id: str, actor: str, at_time: Any | None = None) -> None:
        """接收方出具回执。"""
        require_role(actor, ag.ROLE_REGULATOR)
        when = at(at_time or self.clock.now())
        self.repo.propose([(handover_id, "handover", "HandoverReceived", {
            "at": when.isoformat(),
        }, actor, when.isoformat())])

    def remind(self, handover_id: str, actor: str, level: int = 1,
               at_time: Any | None = None) -> bool:
        """催办：仅当超过回执时限且未回执时登记一次催办；返回是否需要催办。"""
        require_role(actor, ag.ROLE_POSTAL)
        handover = self.repo.get(handover_id, "handover")
        when = at(at_time or self.clock.now())
        if handover.status == "received":
            return False
        if handover.due_at is None or when < at(handover.due_at):
            return False
        self.repo.propose([(handover_id, "handover", "ReceiptReminded", {
            "at": when.isoformat(), "level": level,
        }, actor, when.isoformat())])
        return True


# --------------------------------------------------------------------- 投诉


class ComplaintService:
    def __init__(self, store, clock: Clock, repo: Repository) -> None:
        self.store = store
        self.clock = clock
        self.repo = repo

    def file(self, parcel_id: str, actor: str, summary: str = "",
             at_time: Any | None = None) -> str:
        """投诉必须归入正确链路：按包裹找到关联线索，无包裹不受理。"""
        self.repo.get(parcel_id, "parcel")
        when = at(at_time or self.clock.now())
        parcel = self.repo.get(parcel_id, "parcel")
        lead_id = parcel.linked_leads[0] if parcel.linked_leads else None
        complaint_id = f"complaint-{uuid.uuid4().hex[:12]}"
        entries: list[tuple] = [(complaint_id, "complaint", "ComplaintFiled", {
            "complaint_id": complaint_id, "parcel_id": parcel_id,
            "lead_id": lead_id, "summary": summary, "at": when.isoformat(),
        }, actor, when.isoformat(), lead_id)]
        # 投诉按既有风险链路自动路由到市场监管；无关联线索时留在邮政管理侧。
        entries.append((complaint_id, "complaint", "ComplaintRouted", {
            "to": ag.ROLE_REGULATOR if lead_id else ag.ROLE_POSTAL,
            "at": when.isoformat(),
        }, actor, when.isoformat()))
        self.repo.propose(entries)
        return complaint_id

    def resolve(self, complaint_id: str, actor: str, note: str = "",
                at_time: Any | None = None) -> None:
        when = at(at_time or self.clock.now())
        self.repo.propose([(complaint_id, "complaint", "ComplaintResolved", {
            "at": when.isoformat(), "note": note,
        }, actor, when.isoformat())])


# --------------------------------------------------------------------- 调度


class Scheduler:
    """从事件状态重建到期事项。进程重启后重建结果不变，时限继续计算。"""

    def __init__(self, clock: Clock, repo: Repository) -> None:
        self.clock = clock
        self.repo = repo

    def due_items(self, now: Any | None = None) -> list[dict[str, Any]]:
        moment = at(now or self.clock.now())
        items: list[dict[str, Any]] = []

        # 1) 温控时限：未恢复且已过容忍截止时间的在途温控中断。
        for parcel in self.repo.all_of("parcel"):
            for inter in parcel.temp_interruptions:
                if inter.get("resolved"):
                    continue
                deadline = inter.get("deadline_at")
                if deadline and moment >= at(deadline):
                    items.append({
                        "kind": "temp_deadline",
                        "parcel_id": parcel.parcel_id,
                        "lead_id": inter.get("lead_id"),
                        "deadline_at": deadline,
                        "overdue_minutes": int((moment - at(deadline)).total_seconds() // 60),
                    })

        # 2) 待核查事项：已受理核查但超过 check_due_at 仍未放行/立案。
        for lead in self.repo.all_of("lead"):
            if lead.status == ag.LEAD_CHECKING and lead.check_due_at and moment >= at(lead.check_due_at):
                items.append({
                    "kind": "check_due",
                    "lead_id": lead.lead_id,
                    "parcel_id": lead.parcel_id,
                    "deadline_at": lead.check_due_at,
                    "overdue_hours": int((moment - at(lead.check_due_at)).total_seconds() // 3600),
                })

        # 3) 回执催办：已到回执时限但未接收的移交。
        for handover in self.repo.all_of("handover"):
            if handover.status == "initiated" and handover.due_at and moment >= at(handover.due_at):
                items.append({
                    "kind": "receipt_reminder",
                    "handover_id": handover.handover_id,
                    "lead_id": handover.lead_id,
                    "deadline_at": handover.due_at,
                    "reminders_sent": len(handover.reminders),
                })
        return items


# --------------------------------------------------------------------- 视图


class ResponsibilityView:
    """按角色返回权限范围内的责任清单与完整交接时间线。"""

    #: 每类事项的可见/负责角色
    SCOPE = {
        ag.ROLE_COURIER: {"parcel", "pickup", "complaint", "handover_out"},
        ag.ROLE_POSTAL: {"parcel", "pickup", "lead", "complaint", "handover"},
        ag.ROLE_REGULATOR: {"lead", "parcel", "handover", "notification_duty", "complaint"},
    }

    def __init__(self, repo: Repository, master: MasterDataView) -> None:
        self.repo = repo
        self.master = master

    def responsibilities(self, actor: str) -> dict[str, Any]:
        role = role_of(actor)
        allowed = self.SCOPE.get(role, set())
        result: dict[str, Any] = {"role": role, "parcels": [], "leads": [],
                                  "handovers": [], "notification_duties": [],
                                  "complaints": []}

        if "parcel" in allowed:
            for p in self.repo.all_of("parcel"):
                # 网点只看到与本企业动作相关的包裹状态，不看监管内部核查结论；
                # 监管看到全部与其线索关联的包裹。
                result["parcels"].append(self._parcel_dto(p, role))

        if "lead" in allowed:
            for lead in self.repo.all_of("lead"):
                dto = self._lead_dto(lead)
                if role == ag.ROLE_REGULATOR:
                    result["leads"].append(dto)
                elif role == ag.ROLE_POSTAL:
                    # 邮政侧可见线索级别与状态，但处置决定的内部备注不开放。
                    dto.pop("history", None)
                    result["leads"].append(dto)

        if "handover" in allowed or "handover_out" in allowed:
            for h in self.repo.all_of("handover"):
                result["handovers"].append({
                    "handover_id": h.handover_id, "lead_id": h.lead_id,
                    "parcel_ids": h.parcel_ids, "from_org": h.from_org,
                    "to_org": h.to_org, "status": h.status,
                    "initiated_at": h.initiated_at, "due_at": h.due_at,
                    "received_at": h.received_at,
                    "timeline": h.timeline if role in (ag.ROLE_POSTAL, ag.ROLE_REGULATOR) else None,
                })

        if "notification_duty" in allowed:
            for p in self.repo.all_of("parcel"):
                for duty in p.notification_duties:
                    result["notification_duties"].append({
                        "parcel_id": p.parcel_id, **duty,
                        "lifecycle": p.lifecycle,
                    })

        if "complaint" in allowed:
            for c in self.repo.all_of("complaint"):
                result["complaints"].append({
                    "complaint_id": c.complaint_id, "parcel_id": c.parcel_id,
                    "lead_id": c.lead_id, "routed_to": c.routed_to,
                    "resolved": c.resolved,
                })
        return result

    def handover_timeline(self, handover_id: str, actor: str) -> dict[str, Any]:
        """完整交接时间线：发起、催办、回执及涉及包裹的决定/扫描按时间合并。"""
        role = role_of(actor)
        if role not in (ag.ROLE_POSTAL, ag.ROLE_REGULATOR):
            raise AuthorizationError("仅交接双方可查看完整交接时间线")
        h = self.repo.get(handover_id, "handover")
        events: list[dict[str, Any]] = list(h.timeline)
        for pid in h.parcel_ids:
            parcel = self.repo.get(pid, "parcel")
            for scan in parcel.scans:
                events.append({"event": f"parcel:{scan['scan_type']}", "at": scan["at"],
                               "parcel_id": pid, "node": scan.get("node", "")})
            if parcel.hold is not None:
                events.append({"event": f"parcel:{parcel.hold.kind}", "at": parcel.hold.at,
                               "parcel_id": pid, "by": parcel.hold.by})
        events.sort(key=lambda e: e["at"])
        return {
            "handover_id": h.handover_id, "lead_id": h.lead_id,
            "status": h.status, "timeline": events,
        }

    @staticmethod
    def _parcel_dto(p: ag.ParcelState, role: str) -> dict[str, Any]:
        dto = {
            "parcel_id": p.parcel_id, "waybill_no": p.waybill_no,
            "customer_id": p.customer_id, "batch_id": p.batch_id,
            "lifecycle": p.lifecycle,
            "hold": None if p.hold is None else {
                "kind": p.hold.kind, "lead_id": p.hold.lead_id, "by": p.hold.by,
                "at": p.hold.at, "reason": p.hold.reason,
            },
            "linked_lead_ids": list(p.linked_leads),
        }
        if role in (ag.ROLE_POSTAL, ag.ROLE_REGULATOR):
            dto["temp_interruptions"] = list(p.temp_interruptions)
            dto["notification_duties"] = list(p.notification_duties)
        return dto

    @staticmethod
    def _lead_dto(lead: ag.LeadState) -> dict[str, Any]:
        return {
            "lead_id": lead.lead_id, "lead_type": lead.lead_type, "level": lead.level,
            "rulebook_version": lead.rulebook_version, "parcel_id": lead.parcel_id,
            "customer_id": lead.customer_id, "reporter": lead.reporter,
            "status": lead.status, "history": list(lead.history),
            "propagation_batches": list(lead.propagation_batches),
            "check_due_at": lead.check_due_at,
        }
