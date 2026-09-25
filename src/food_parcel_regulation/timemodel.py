"""时钟与生效区间。

所有主数据（资质、经营场所、外设仓库、核验码、规则版本）都按半开生效区间
``[valid_from, valid_to)`` 查询：时点落在起点上有效，落在终点上已失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .errors import ConflictError, NotFoundError


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """真实墙上时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """测试用可控时钟，可显式推进。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须带时区")
        self._now = value


def at(value) -> datetime:
    """归一化为带 UTC 时区的时间；接受 ``datetime`` 或 ISO 字符串。"""
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        value = parsed
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Interval:
    """半开生效区间 ``[valid_from, valid_to)``，``valid_to`` 为空表示至今有效。"""

    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        start = at(self.valid_from)
        end = at(self.valid_to) if self.valid_to is not None else None
        if end is not None and end <= start:
            raise ValueError("生效区间终点必须晚于起点")
        object.__setattr__(self, "valid_from", start)
        object.__setattr__(self, "valid_to", end)

    def contains(self, moment: datetime) -> bool:
        moment = at(moment)
        if moment < self.valid_from:
            return False
        return self.valid_to is None or moment < self.valid_to


def active(records: list, moment: datetime):
    """返回在 ``moment`` 时点生效的记录列表。"""
    return [r for r in records if r.interval.contains(at(moment))]


def sole(records: list, moment: datetime, label: str):
    """返回唯一生效记录；缺失或多版本重叠都视为资料错误。"""
    hits = active(records, moment)
    if not hits:
        raise NotFoundError(f"{label}在该时点无生效版本")
    if len(hits) > 1:
        raise ConflictError(f"{label}在该时点存在多个重叠生效版本")
    return hits[0]
