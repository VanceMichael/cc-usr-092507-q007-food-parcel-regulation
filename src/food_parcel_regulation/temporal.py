"""时态档案：资质、经营场所、外设仓库、核验码都按生效区间管理。

设计要点：

- 每类档案都是一条不可变的“版本”，用 ``[valid_from, valid_to)`` 半开
  区间表示有效期；新版本插入时把上一版本末端截断到新版本起点，
  相邻版本无缝衔接、不重叠。
- 查询一律 ``as_of(时刻)`` 取当时生效版本——历史验视事实不会因为
  客户事后换证、迁址或换码而被改写。
- 核验码（专属核验码）本身也版本化，且其当版登记的经营地址必须在
  同一时点能对应到一处生效经营场所或外设仓库；首次揽件时现场地址
  与之比对，不符即产生地址线索。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .errors import NotFoundError, ValidationError
from .timeutils import FAR_FUTURE, contains, overlaps


def _check_interval(valid_from: datetime, valid_to: datetime) -> None:
    if valid_to <= valid_from:
        raise ValidationError("生效区间起点必须早于终点")


@dataclass(frozen=True)
class Qualification:
    """食品经营许可证等资质的一个生效版本。"""

    qual_id: str
    customer_id: str
    license_no: str
    scope: str                      # 许可经营项目（摘要用）
    valid_from: datetime
    valid_to: datetime = FAR_FUTURE
    revoked: bool = False           # 吊销/注销随新版本或标记体现

    def effective(self, moment: datetime) -> bool:
        return not self.revoked and contains(self.valid_from, self.valid_to, moment)


@dataclass(frozen=True)
class Premise:
    """登记经营场所的一个生效版本。"""

    premise_id: str
    customer_id: str
    address: str
    valid_from: datetime
    valid_to: datetime = FAR_FUTURE


@dataclass(frozen=True)
class Warehouse:
    """外设仓库（可以在经营场所之外）的一个生效版本。"""

    warehouse_id: str
    customer_id: str
    address: str
    valid_from: datetime
    valid_to: datetime = FAR_FUTURE


@dataclass(frozen=True)
class VerificationCode:
    """专属核验码的一个生效版本，绑定当版经营地址。"""

    code: str
    customer_id: str
    registered_address: str        # 码面显示的经营地址
    valid_from: datetime
    valid_to: datetime = FAR_FUTURE


class _VersionedRegistry:
    """按主体聚合的时态版本集合。"""

    def __init__(self) -> None:
        self._versions: dict[str, list] = {}

    def _add(self, key: str, version) -> None:
        _check_interval(version.valid_from, version.valid_to)
        bucket = self._versions.setdefault(key, [])
        for existing in bucket:
            if overlaps(
                existing.valid_from, existing.valid_to,
                version.valid_from, version.valid_to,
            ):
                raise ValidationError(
                    f"{key} 的生效区间与既有版本重叠："
                    f"{existing.valid_from:%Y-%m-%d %H:%M} 起"
                )
        bucket.append(version)
        bucket.sort(key=lambda v: v.valid_from)

    def _as_of(self, key: str, moment: datetime):
        for version in sorted(self._versions.get(key, []), key=lambda v: v.valid_from, reverse=True):
            if contains(version.valid_from, version.valid_to, moment):
                return version
        raise NotFoundError(f"{key} 在 {moment:%Y-%m-%d %H:%M} 没有生效档案")

    def _all_effective(self, key: str, moment: datetime) -> list:
        return [
            v for v in self._versions.get(key, [])
            if contains(v.valid_from, v.valid_to, moment)
        ]


class QualificationRegistry(_VersionedRegistry):
    def add(self, q: Qualification) -> None:
        self._add(q.customer_id, q)

    def as_of(self, customer_id: str, moment: datetime) -> Qualification:
        return self._as_of(customer_id, moment)

    def is_valid(self, customer_id: str, moment: datetime) -> bool:
        try:
            return self.as_of(customer_id, moment).effective(moment)
        except NotFoundError:
            return False


class PremiseRegistry(_VersionedRegistry):
    def add(self, p: Premise) -> None:
        self._add(p.customer_id, p)

    def as_of(self, customer_id: str, moment: datetime) -> Premise:
        return self._as_of(customer_id, moment)

    def addresses_at(self, customer_id: str, moment: datetime) -> set[str]:
        return {p.address for p in self._all_effective(customer_id, moment)}


class WarehouseRegistry(_VersionedRegistry):
    def add(self, w: Warehouse) -> None:
        self._add(w.customer_id, w)

    def as_of(self, warehouse_id: str, moment: datetime) -> Warehouse:
        # 仓库按仓库号直查：在全量中找
        for bucket in self._versions.values():
            for v in bucket:
                if v.warehouse_id == warehouse_id and contains(v.valid_from, v.valid_to, moment):
                    return v
        raise NotFoundError(f"外设仓库 {warehouse_id} 在该时点不生效")

    def addresses_at(self, customer_id: str, moment: datetime) -> set[str]:
        return {w.address for w in self._all_effective(customer_id, moment)}


class VerificationCodeRegistry(_VersionedRegistry):
    def add(self, c: VerificationCode) -> None:
        self._add(c.code, c)

    def as_of(self, code: str, moment: datetime) -> VerificationCode:
        return self._as_of(code, moment)

    def code_effective(self, code: str, moment: datetime) -> bool:
        try:
            self.as_of(code, moment)
            return True
        except NotFoundError:
            return False


@dataclass
class CustomerProfile:
    """一个协议客户在系统中的时态档案集合句柄。"""

    customer_id: str
    qualifications: QualificationRegistry = field(default_factory=QualificationRegistry)
    premises: PremiseRegistry = field(default_factory=PremiseRegistry)
    warehouses: WarehouseRegistry = field(default_factory=WarehouseRegistry)
    codes: VerificationCodeRegistry = field(default_factory=VerificationCodeRegistry)

    def known_addresses_at(self, moment: datetime) -> set[str]:
        """该客户在某时点全部登记经营场所 + 外设仓库地址。"""

        return self.premises.addresses_at(self.customer_id, moment) | self.warehouses.addresses_at(
            self.customer_id, moment
        )
