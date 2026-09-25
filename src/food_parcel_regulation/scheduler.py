"""调度器：时间驱动的待办，重启后继续。

管理三类“到点要做事”的工作：

1. 温控时限：包裹进入中断时挂一个截止时刻，到点未恢复就升级为
   温控中断线索（按当时规则版本定级）；
2. 待核查事项：监管承诺/系统要求在某时刻前完成核查，到期提醒；
3. 回执催办：跨部门交接逾期未签收，周期性催办，直到签收。

调度器只保存可持久化的“事项”，不自己决定业务后果；``due(now)``
返回到期事项，由服务层执行实际升级/催办（这样重启后只要重新
tick，未完成的事项仍会继续）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .errors import ConflictError, NotFoundError


class JobKind(StrEnum):
    TEMP_DEADLINE = "temp_deadline"       # 温控时限到点
    REVIEW_DUE = "review_due"             # 待核查到期提醒
    RECEIPT_REMINDER = "receipt_reminder" # 回执催办


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: JobKind
    due_at: datetime
    ref_id: str                 # tracking_no / clue_id / handoff_id
    payload: dict = field(default_factory=dict)
    recurring_seconds: float | None = None  # 催办类周期事项
    done: bool = False
    fired_count: int = 0


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"J{self._seq:08d}"

    def schedule(self, kind: JobKind, due_at: datetime, ref_id: str,
                 payload: dict | None = None,
                 recurring_seconds: float | None = None) -> Job:
        job = Job(
            job_id=self._next_id(), kind=kind, due_at=due_at, ref_id=ref_id,
            payload=dict(payload or {}), recurring_seconds=recurring_seconds,
        )
        self._jobs[job.job_id] = job
        return job

    def cancel(self, ref_id: str, kind: JobKind) -> int:
        """取消某实体的某类未完成事项（如温控恢复后撤时限）。返回取消数。"""

        count = 0
        for job in self._jobs.values():
            if job.ref_id == ref_id and job.kind is kind and not job.done:
                job.__dict__["done"] = True
                count += 1
        return count

    def due(self, moment: datetime) -> list[Job]:
        """到期且未完成的事项；周期事项触发后顺延到下一周期。"""

        ready: list[Job] = []
        for job in self._jobs.values():
            if not job.done and job.due_at <= moment:
                ready.append(job)
        for job in ready:
            job.__dict__["fired_count"] = job.fired_count + 1
            if job.recurring_seconds:
                from datetime import timedelta
                # 以当前时刻为基准顺延，避免停机积压时连发一串。
                job.__dict__["due_at"] = moment + timedelta(seconds=job.recurring_seconds)
            else:
                job.__dict__["done"] = True
        return ready

    def pending(self, *, kind: JobKind | None = None) -> list[Job]:
        jobs = [j for j in self._jobs.values() if not j.done]
        if kind:
            jobs = [j for j in jobs if j.kind is kind]
        return sorted(jobs, key=lambda j: j.due_at)

    def complete(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise NotFoundError(f"调度事项 {job_id} 不存在")
        job = self._jobs[job_id]
        if job.done:
            raise ConflictError(f"调度事项 {job_id} 已完成")
        job.__dict__["done"] = True
        return job

    def state(self) -> dict:
        return {"seq": self._seq, "jobs": list(self._jobs.values())}

    def restore_state(self, raw: dict) -> None:
        self._seq = raw.get("seq", 0)
        self._jobs = {j.job_id: j for j in raw.get("jobs", [])}
