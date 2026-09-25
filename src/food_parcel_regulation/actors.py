"""参与方与角色权限。

四类参与方对应 README 中的领域角色。网点负责发现与上报、可暂停
本企业未发出的包裹；市场监管独立决定核查、放行与立案；邮政管理
负责跨部门协调与见证；客户只能查看与自身相关的材料。

关键分离：线索的关闭权只属于监管处置人，上报人（即使是本企业的
邮政管理人员）不能关闭自己上报的线索。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import PermissionDeniedError


class Role(StrEnum):
    COURIER_STAFF = "courier_staff"        # 快递网点人员（揽件/上报/暂停）
    POSTAL_ADMIN = "postal_admin"          # 邮政管理人员（协调/移交发起）
    MARKET_REGULATOR = "market_regulator"  # 市场监管人员（核查/放行/立案/关闭）
    CUSTOMER = "customer"                  # 食品协议客户


class Org(StrEnum):
    EXPRESS = "express"        # 寄递企业
    POSTAL = "postal"          # 邮政管理部门
    MARKET = "market"          # 市场监管部门
    CUSTOMER_ORG = "customer"  # 客户主体


# 各角色默认所属组织
ROLE_ORG: dict[Role, Org] = {
    Role.COURIER_STAFF: Org.EXPRESS,
    Role.POSTAL_ADMIN: Org.POSTAL,
    Role.MARKET_REGULATOR: Org.MARKET,
    Role.CUSTOMER: Org.CUSTOMER_ORG,
}


@dataclass(frozen=True)
class Actor:
    """操作人：身份 + 角色 + 所属组织。"""

    actor_id: str
    name: str
    role: Role
    org: Org | None = None

    def __post_init__(self) -> None:
        if self.org is None:
            object.__setattr__(self, "org", ROLE_ORG[self.role])

    def require(self, *roles: Role) -> None:
        if self.role not in roles:
            raise PermissionDeniedError(
                f"{self.name} 的角色 {self.role.value} 无权执行该操作"
            )

    def is_regulator(self) -> bool:
        return self.role == Role.MARKET_REGULATOR
