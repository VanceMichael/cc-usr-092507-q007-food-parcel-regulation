"""时间工具。

业务时间全部采用朴素 ``datetime`` 并约定为同一时区（部署时统一），
``valid_from`` 闭区间、``valid_to`` 开区间：``[valid_from, valid_to)``。
开区间末端保证同一证照在相邻两个生效版本之间没有缝隙或重叠歧义。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 表示“至今仍有效”的远端时间，避免各处散落魔术值。
FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59)


def now() -> datetime:
    """当前时间。集中封装便于测试注入时钟。"""

    return datetime.now()


def utcnow() -> datetime:
    """当前 UTC 时间（带时区），跨部门接口时间线使用。"""

    return datetime.now(timezone.utc)


def effective_at(moment: datetime | None = None) -> datetime:
    """把 None 归一化为当前业务时间。"""

    return moment if moment is not None else now()


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """两个半开区间是否重叠。"""

    return start_a < end_b and start_b < end_a


def contains(start: datetime, end: datetime, moment: datetime) -> bool:
    """半开区间 ``[start, end)`` 是否包含某时刻。"""

    return start <= moment < end


def deadline(base: datetime, seconds: float) -> datetime:
    return base + timedelta(seconds=seconds)


def is_overdue(due: datetime, moment: datetime | None = None) -> bool:
    return effective_at(moment) >= due
