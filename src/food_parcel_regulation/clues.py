"""风险线索及其处置生命周期。

职责分离是这里的核心：

- 网点/邮政人员只能“上报”和补充材料，是上报人；
- 是否核查、放行（风险不成立/无需处置）、立案，只由市场监管人员
  独立决定；上报人不能关闭自己上报的线索，也不能替监管放行；
- 线索状态机的每次迁移都记录操作人、时间、依据版本，形成可审计
  链条；线索对象带版本号，处置决定与包裹扫描并发时用乐观锁，
  拒绝旧决定覆盖新状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .actors import Actor, Role
from .errors import ConflictError, NotFoundError, PermissionDeniedError
from .rules import ClueLevel, RuleType


class ClueStatus(StrEnum):
    REPORTED = "reported"        # 已上报，待监管受理
    UNDER_REVIEW = "under_review"  # 监管核查中
    RELEASED = "released"        # 监管放行（认定无风险或风险解除）
    CASE_FILED = "case_filed"    # 立案查处
    DISMISSED = "dismissed"      # 不予处置（如重复、不实）
    TRANSFERRED = "transferred"  # 已移交其他主管部门（外部回执前挂起）


# 只有市场监管可以进入的处置终态/决定
_TERMINAL = {ClueStatus.RELEASED, ClueStatus.CASE_FILED, ClueStatus.DISMISSED}

# 允许的状态迁移
_TRANSITIONS: dict[ClueStatus, set[ClueStatus]] = {
    ClueStatus.REPORTED: {ClueStatus.UNDER_REVIEW, ClueStatus.RELEASED,
                          ClueStatus.CASE_FILED, ClueStatus.DISMISSED,
                          ClueStatus.TRANSFERRED},
    ClueStatus.UNDER_REVIEW: {ClueStatus.RELEASED, ClueStatus.CASE_FILED,
                              ClueStatus.DISMISSED, ClueStatus.TRANSFERRED},
    ClueStatus.TRANSFERRED: {ClueStatus.UNDER_REVIEW, ClueStatus.RELEASED,
                             ClueStatus.CASE_FILED, ClueStatus.DISMISSED},
    ClueStatus.RELEASED: set(),
    ClueStatus.CASE_FILED: set(),
    ClueStatus.DISMISSED: set(),
}


@dataclass(frozen=True)
class ClueEvent:
    at: datetime
    actor_id: str
    actor_name: str
    action: str
    detail: str = ""
    expected_version: int | None = None


@dataclass
class Clue:
    clue_id: str
    rule_type: RuleType
    level: ClueLevel
    rule_version: str
    customer_id: str
    serial: str | None            # 关联揽件流水
    tracking_no: str | None
    batch_id: str | None
    title: str
    evidence: str                 # 证据摘要（来自验视快照，非影像）
    reporter_id: str
    reporter_org: str
    reported_at: datetime
    status: ClueStatus = ClueStatus.REPORTED
    assignee_id: str | None = None
    events: list[ClueEvent] = field(default_factory=list)
    version: int = 0              # 乐观并发版本：每次迁移 +1
    linked_clue_ids: list[str] = field(default_factory=list)
    external_ref: str | None = None  # 移交后外部部门案号/回执号

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def require_version(self, expected: int | None) -> None:
        if expected is not None and expected != self.version:
            raise ConflictError(
                f"线索 {self.clue_id} 已被他人更新（版本 {self.version}，"
                f"提交基于版本 {expected}），请刷新后重试"
            )


class ClueRegistry:
    def __init__(self) -> None:
        self._clues: dict[str, Clue] = {}
        self._seq = 0

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def create(
        self,
        *,
        rule_type: RuleType,
        level: ClueLevel,
        rule_version: str,
        customer_id: str,
        serial: str | None,
        tracking_no: str | None,
        batch_id: str | None,
        title: str,
        evidence: str,
        reporter: Actor,
        reported_at: datetime,
        linked_clue_ids: list[str] | None = None,
    ) -> Clue:
        clue_id = f"C{self._next_id():08d}"
        clue = Clue(
            clue_id=clue_id,
            rule_type=rule_type,
            level=level,
            rule_version=rule_version,
            customer_id=customer_id,
            serial=serial,
            tracking_no=tracking_no,
            batch_id=batch_id,
            title=title,
            evidence=evidence,
            reporter_id=reporter.actor_id,
            reporter_org=reporter.org.value,
            reported_at=reported_at,
            linked_clue_ids=list(linked_clue_ids or []),
        )
        clue.events.append(ClueEvent(
            at=reported_at, actor_id=reporter.actor_id, actor_name=reporter.name,
            action="report", detail=title,
        ))
        self._clues[clue_id] = clue
        return clue

    def get(self, clue_id: str) -> Clue:
        if clue_id not in self._clues:
            raise NotFoundError(f"线索 {clue_id} 不存在")
        return self._clues[clue_id]

    def decide(
        self,
        clue_id: str,
        *,
        actor: Actor,
        target: ClueStatus,
        at: datetime,
        expected_version: int | None = None,
        detail: str = "",
        external_ref: str | None = None,
    ) -> Clue:
        """市场监管作出处置决定（核查/放行/立案/不予处置/接收移交）。"""

        clue = self.get(clue_id)
        actor.require(Role.MARKET_REGULATOR)
        clue.require_version(expected_version)
        if target not in _TRANSITIONS[clue.status]:
            raise ConflictError(
                f"线索 {clue_id} 不能从 {clue.status.value} 迁移到 {target.value}"
            )
        clue.status = target
        clue.version += 1
        clue.assignee_id = actor.actor_id
        if external_ref:
            clue.external_ref = external_ref
        clue.events.append(ClueEvent(
            at=at, actor_id=actor.actor_id, actor_name=actor.name,
            action=target.value, detail=detail, expected_version=expected_version,
        ))
        return clue

    def transfer(self, clue_id: str, *, actor: Actor, at: datetime,
                 to_dept: str, expected_version: int | None = None) -> Clue:
        """跨部门移交：邮政管理可发起挂起，处置权随移交转移。"""

        clue = self.get(clue_id)
        actor.require(Role.POSTAL_ADMIN, Role.MARKET_REGULATOR)
        clue.require_version(expected_version)
        if ClueStatus.TRANSFERRED not in _TRANSITIONS[clue.status]:
            raise ConflictError(f"线索 {clue_id} 已终态，不能移交")
        clue.status = ClueStatus.TRANSFERRED
        clue.version += 1
        clue.events.append(ClueEvent(
            at=at, actor_id=actor.actor_id, actor_name=actor.name,
            action="transfer", detail=f"移交至 {to_dept}",
            expected_version=expected_version,
        ))
        return clue

    def supplement(self, clue_id: str, *, actor: Actor, at: datetime, detail: str) -> Clue:
        """上报人/任何参与方补充材料；不改变状态，且永远不能关闭线索。"""

        clue = self.get(clue_id)
        if actor.role == Role.CUSTOMER:
            raise PermissionDeniedError("客户不能直接向处置线索补充材料")
        clue.events.append(ClueEvent(
            at=at, actor_id=actor.actor_id, actor_name=actor.name,
            action="supplement", detail=detail,
        ))
        return clue

    def close(self, clue_id: str, *, actor: Actor, at: datetime, detail: str = "") -> Clue:
        """关闭线索。显式拒绝上报人关闭自己的线索——即使他也是邮政管理。"""

        clue = self.get(clue_id)
        if actor.actor_id == clue.reporter_id:
            raise PermissionDeniedError(
                f"上报人 {actor.name} 不能关闭自己上报的线索 {clue_id}"
            )
        actor.require(Role.MARKET_REGULATOR)
        if clue.is_terminal:
            raise ConflictError(f"线索 {clue_id} 已终态")
        return self.decide(clue_id, actor=actor, target=ClueStatus.RELEASED,
                           at=at, detail=detail or "监管关闭")

    def for_customer(self, customer_id: str) -> list[Clue]:
        return [c for c in self._clues.values() if c.customer_id == customer_id]

    def for_batch(self, batch_id: str) -> list[Clue]:
        return [c for c in self._clues.values() if c.batch_id == batch_id]

    def open_clues(self) -> list[Clue]:
        return [c for c in self._clues.values() if not c.is_terminal
                and c.status is not ClueStatus.TRANSFERRED]

    def all(self) -> list[Clue]:
        return list(self._clues.values())
