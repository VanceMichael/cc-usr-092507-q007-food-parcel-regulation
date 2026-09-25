"""仅追加的事件存储。

- 每个事件有全局自增序号 ``seq``，同一聚合按 ``aggregate_version`` 乐观并发；
- 一次提交的多个事件以单条 JSONL 记录写出（原子批次），重放时要么整组可见、要么不可见；
- 追加采用临时文件 + ``os.replace`` 原子替换，进程重启后事件仍在；
- 聚合状态由纯函数 :func:`food_parcel_regulation.aggregates.reduce` 重放得到。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Event:
    seq: int
    aggregate_id: str
    aggregate_type: str
    aggregate_version: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: str
    actor: str
    causation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "seq": self.seq,
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_version": self.aggregate_version,
            "event_type": self.event_type,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
        }
        if self.causation_id is not None:
            data["causation_id"] = self.causation_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            seq=data["seq"],
            aggregate_id=data["aggregate_id"],
            aggregate_type=data["aggregate_type"],
            aggregate_version=data["aggregate_version"],
            event_type=data["event_type"],
            payload=data.get("payload", {}),
            occurred_at=data["occurred_at"],
            actor=data["actor"],
            causation_id=data.get("causation_id"),
        )


class EventStore:
    """JSONL 仅追加事件存储。

    每行是一个原子批次（一个或多个事件）。进程内保存全部事件以支持重放，
    同时把批次持久化到磁盘，重启后 :meth:`replay` 可完整恢复。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[Event] = []
        self._batches: list[list[Event]] = []
        if self._path is not None and self._path.exists():
            self._load()

    # ----- 持久化 -------------------------------------------------------

    def _load(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            batch = [Event.from_dict(e) for e in json.loads(line)]
            self._ingest(batch)

    def _ingest(self, batch: list[Event]) -> None:
        self._batches.append(batch)
        self._events.extend(batch)

    def _persist(self, batch: list[Event]) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps([e.to_dict() for e in batch], ensure_ascii=False)
        existing = (
            self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        )
        # 读改写 + 原子替换：一个批次就是一行，重放时整组可见或整组不可见。
        fd, tmp = tempfile.mkstemp(
            prefix=self._path.name, suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(existing)
                fh.write(record + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ----- 读取 ---------------------------------------------------------

    @property
    def global_seq(self) -> int:
        return self._events[-1].seq if self._events else 0

    def all_events(self) -> list[Event]:
        return list(self._events)

    def events_for(self, aggregate_id: str) -> list[Event]:
        return [e for e in self._events if e.aggregate_id == aggregate_id]

    def replay(
        self,
        reducer: Callable[[str, str, int | None, Event], Any],
    ) -> dict[str, Any]:
        """按全局顺序重放，返回 ``aggregate_id -> 状态``。

        ``reducer(aggregate_id, aggregate_type, current_state, event) -> new_state``
        """
        states: dict[str, Any] = {}
        types: dict[str, str] = {}
        for event in self._events:
            states[event.aggregate_id] = reducer(
                event.aggregate_id,
                event.aggregate_type,
                states.get(event.aggregate_id),
                event,
            )
            types[event.aggregate_id] = event.aggregate_type
        return states

    def aggregate_versions(self) -> dict[str, int]:
        versions: dict[str, int] = {}
        for event in self._events:
            versions[event.aggregate_id] = event.aggregate_version
        return versions

    # ----- 写入 ---------------------------------------------------------

    def append_batch(
        self,
        entries: list[tuple[str, str, str, dict[str, Any], str, str, str | None]],
    ) -> list[Event]:
        """原子追加一批事件。

        每个条目为 ``(aggregate_id, aggregate_type, event_type, payload,
        actor, occurred_at_iso, causation_id)`` 七元组（causation_id 可传
        六元组省略）。同一聚合在批次内必须给出连续的新版本号。
        """
        if not entries:
            return []
        normalized: list[tuple[str, str, str, dict, str, str, str | None]] = []
        for entry in entries:
            normalized.append(entry if len(entry) == 7 else (*entry, None))  # type: ignore[arg-type]

        current = self.aggregate_versions()
        pending: dict[str, int] = {}
        batch: list[Event] = []
        next_seq = self.global_seq + 1
        for agg_id, agg_type, event_type, payload, actor, occurred_at, causation in normalized:
            expected_base = current.get(agg_id, 0)
            if agg_id in pending:
                base = pending[agg_id]
            else:
                base = expected_base
            new_version = base + 1
            event = Event(
                seq=next_seq,
                aggregate_id=agg_id,
                aggregate_type=agg_type,
                aggregate_version=new_version,
                event_type=event_type,
                payload=payload,
                occurred_at=occurred_at,
                actor=actor,
                causation_id=causation,
            )
            batch.append(event)
            pending[agg_id] = new_version
            next_seq += 1
        self._persist(batch)
        self._ingest(batch)
        return batch
