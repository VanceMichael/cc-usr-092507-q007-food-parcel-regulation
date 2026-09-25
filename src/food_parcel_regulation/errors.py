"""领域错误类型。

所有错误都携带中文说明，便于网点与监管界面直接展示；
按错误类别区分调用方可恢复性，而不是用异常文本做流程判断。
"""

from __future__ import annotations


class DomainError(Exception):
    """全部领域错误的基类。"""


class ValidationError(DomainError):
    """输入不满足领域前置条件（时间区间、枚举值、必填项）。"""


class NotFoundError(DomainError):
    """查询对象不存在或在指定时点不生效。"""


class IdempotencyConflictError(DomainError):
    """相同流水再次上报但内容不同，进入隔离而非重复受理。"""

    def __init__(self, serial: str, quarantine_id: str):
        super().__init__(f"流水 {serial} 已有不同内容的上报，已隔离为 {quarantine_id}")
        self.serial = serial
        self.quarantine_id = quarantine_id


class PermissionDeniedError(DomainError):
    """角色无权执行该操作。"""


class ConflictError(DomainError):
    """对象当前状态不允许该操作（版本冲突或状态机冲突）。"""


class RuleError(DomainError):
    """规则版本不存在或规则内容无效。"""
