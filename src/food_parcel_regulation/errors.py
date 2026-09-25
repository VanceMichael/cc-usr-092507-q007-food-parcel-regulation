"""食品寄递协同后端的领域错误。"""


class DomainError(Exception):
    """所有领域错误的基类。"""


class ValidationError(DomainError):
    """请求不满足领域约束。"""


class NotFoundError(DomainError):
    """聚合或时态记录不存在。"""


class AuthorizationError(DomainError):
    """角色无权执行该操作。"""


class ConflictError(DomainError):
    """聚合版本已变化，调用方依据的是旧状态。"""


class StaleDecisionError(DomainError):
    """决定时间早于该聚合上已生效的最新决定，不得覆盖新状态。"""


class QuarantineError(DomainError):
    """相同流水附带不同报文，已进入隔离，不得生成揽件。"""


class RuleUnavailableError(DomainError):
    """所查询时点没有生效的规则版本。"""
