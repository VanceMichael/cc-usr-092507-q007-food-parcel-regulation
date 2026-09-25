"""线索规则版本。

四类异常各自独立发布规则版本：地址不符、证照过期、包装异常、
温控中断。每个版本给出该异常在该版本下的线索级别与判定参数；
揽件当时适用哪个版本，就按哪个版本定级——规则升级不溯及既往，
历史线索保留其生成时的 ``rule_version`` 与级别。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum

from .errors import NotFoundError, RuleError, ValidationError


class RuleType(StrEnum):
    ADDRESS_MISMATCH = "address_mismatch"     # 核验码地址与现场仓库不符
    LICENSE_EXPIRED = "license_expired"       # 证照过期/失效
    PACKAGING_ABNORMAL = "packaging_abnormal"  # 包装异常
    TEMP_INTERRUPTION = "temp_interruption"   # 温控中断


class ClueLevel(IntEnum):
    """线索级别，数值越大越严重。"""

    INFO = 10     # 提示
    LOW = 20      # 一般
    MEDIUM = 30   # 较重
    HIGH = 40     # 重大


@dataclass(frozen=True)
class RuleVersion:
    rule_type: RuleType
    version: str                       # 规则版本标识，如 "2026.1"
    effective_from: datetime
    level: ClueLevel
    # 温控中断的级别按中断时长分档；其余规则直接用 level。
    duration_levels: tuple[tuple[float, ClueLevel], ...] = ()
    description: str = ""

    def level_for(self, interruption_seconds: float | None = None) -> ClueLevel:
        """按当时规则版本定级。温控按时长落档，其余取固定级别。"""

        if self.rule_type is not RuleType.TEMP_INTERRUPTION:
            return self.level
        chosen = self.level
        for threshold, tier in self.duration_levels:
            if interruption_seconds is not None and interruption_seconds >= threshold:
                chosen = tier
        return chosen


class RuleBook:
    """按规则类型聚合的版本册，按时点取生效版本。"""

    def __init__(self) -> None:
        self._rules: dict[RuleType, list[RuleVersion]] = {}

    def publish(self, rule: RuleVersion) -> None:
        bucket = self._rules.setdefault(rule.rule_type, [])
        for existing in bucket:
            if existing.version == rule.version:
                raise RuleError(f"{rule.rule_type} 版本 {rule.version} 已存在")
            if existing.effective_from == rule.effective_from:
                raise ValidationError(
                    f"{rule.rule_type} 在同一时点存在两个生效版本"
                )
        bucket.append(rule)
        bucket.sort(key=lambda r: r.effective_from)

    def effective(self, rule_type: RuleType, moment: datetime) -> RuleVersion:
        bucket = self._rules.get(rule_type, [])
        for rule in reversed(bucket):
            if rule.effective_from <= moment:
                return rule
        raise NotFoundError(f"{rule_type.value} 在 {moment:%Y-%m-%d %H:%M} 没有生效规则版本")

    def all_versions(self, rule_type: RuleType) -> list[RuleVersion]:
        return list(self._rules.get(rule_type, []))
