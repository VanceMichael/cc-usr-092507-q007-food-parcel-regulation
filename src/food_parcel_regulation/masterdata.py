"""时态主数据：客户、资质、经营场所、外设仓库、专属核验码与规则书。

所有记录按半开生效区间 ``[valid_from, valid_to)`` 保存，业务只按时点查询；
规则书同样版本化，线索级别与温控、催办等参数都取事件发生时点的生效版本，
并把版本号固化在线索上，历史判定不随规则升级而改变。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .errors import NotFoundError, RuleUnavailableError, ValidationError
from .eventstore import EventStore, Event
from .timemodel import Interval, at, sole

EARTH_RADIUS_M = 6371000.0

# 线索类型
ADDRESS_MISMATCH = "address_mismatch"
LICENSE_EXPIRED = "license_expired"
PACKAGING_ABNORMAL = "packaging_abnormal"
TEMP_BREAK = "temp_break"
LEAD_TYPES = (ADDRESS_MISMATCH, LICENSE_EXPIRED, PACKAGING_ABNORMAL, TEMP_BREAK)

# 线索级别
LEVEL_INFO = "info"
LEVEL_GENERAL = "general"
LEVEL_MAJOR = "major"
LEVEL_ORDER = {LEVEL_INFO: 0, LEVEL_GENERAL: 1, LEVEL_MAJOR: 2}


def geo_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点间球面距离（米）。"""
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Qualification:
    record_id: str
    customer_id: str
    license_no: str
    license_digest: str  # 只保存证照摘要，不保存影像原件
    scope: str
    license_expiry: datetime | None
    interval: Interval


@dataclass(frozen=True)
class Place:
    record_id: str
    customer_id: str
    address: str
    geo: tuple[float, float]
    radius_m: float
    interval: Interval


@dataclass(frozen=True)
class VerificationCode:
    code: str
    customer_id: str
    declared_address: str
    interval: Interval


@dataclass(frozen=True)
class Rulebook:
    version: str
    levels: dict[str, str]
    # temp_break_tolerance_min: 温控中断容忍分钟；receipt_due_hours: 回执催办时限；
    # freeze_followup_hours: 冻结后待核查时限
    params: dict[str, Any]
    interval: Interval


@dataclass
class Customer:
    customer_id: str
    name: str


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    return at(value) if isinstance(value, str) else at(value)


def _interval_payload(payload: dict[str, Any]) -> Interval:
    return Interval(
        valid_from=_as_dt(payload["valid_from"]),
        valid_to=_as_dt(payload.get("valid_to")),
    )


class MasterDataView:
    """从事件流重放得到的时态主数据只读视图。

    视图按事件存储的全局序号缓存：任何提交推进序号后，下一次查询自动重建，
    因此长期持有的服务对象总能读到最新主数据。
    """

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._loaded_seq = -1
        self._customers: dict[str, Customer] = {}
        self.qualifications: list[Qualification] = []
        self.premises: list[Place] = []
        self.warehouses: list[Place] = []
        self.codes: list[VerificationCode] = []
        self.rulebooks: list[Rulebook] = []
        self._code_index: dict[str, VerificationCode] = {}
        self._ensure_fresh()

    def _ensure_fresh(self) -> None:
        if self._loaded_seq == self.store.global_seq:
            return
        self._customers = {}
        self.qualifications = []
        self.premises = []
        self.warehouses = []
        self.codes = []
        self.rulebooks = []
        self._code_index = {}
        for event in self.store.all_events():
            if event.aggregate_type == "customer":
                self._apply_customer(event)
            elif event.aggregate_type in ("qualification", "premises", "warehouse", "verification_code", "rulebook"):
                self._apply_record(event)
        self._loaded_seq = self.store.global_seq

    # ----- 重放 ---------------------------------------------------------

    def _apply_customer(self, event: Event) -> None:
        if event.event_type == "CustomerRegistered":
            p = event.payload
            self._customers[event.aggregate_id] = Customer(p["customer_id"], p["name"])

    def _apply_record(self, event: Event) -> None:
        p = event.payload
        if event.event_type.endswith("Recorded") or event.event_type == "RulebookPublished":
            interval = _interval_payload(p)
            if event.aggregate_type == "qualification":
                rec = Qualification(
                    p["record_id"], p["customer_id"], p["license_no"],
                    p["license_digest"], p["scope"],
                    at(p["license_expiry"]) if p.get("license_expiry") else None,
                    interval,
                )
                self.qualifications.append(rec)
            elif event.aggregate_type in ("premises", "warehouse"):
                rec = Place(
                    p["record_id"], p["customer_id"], p["address"],
                    (p["lat"], p["lng"]), float(p["radius_m"]), interval,
                )
                (self.premises if event.aggregate_type == "premises" else self.warehouses).append(rec)
            elif event.aggregate_type == "verification_code":
                code = VerificationCode(
                    p["code"], p["customer_id"], p["declared_address"], interval
                )
                self.codes.append(code)
                self._code_index[code.code] = code
            else:
                rb = Rulebook(p["version"], dict(p["levels"]), dict(p.get("params", {})), interval)
                self.rulebooks.append(rb)
        elif event.event_type.endswith("Closed"):
            self._close(event.aggregate_type, event.aggregate_id, at(p["valid_to"]))

    def _close(self, kind: str, record_id: str, valid_to: datetime) -> None:
        pool = {
            "qualification": self.qualifications,
            "premises": self.premises,
            "warehouse": self.warehouses,
            "verification_code": self.codes,
            "rulebook": self.rulebooks,
        }[kind]
        for idx, rec in enumerate(pool):
            rid = rec.code if kind == "verification_code" else (
                rec.version if kind == "rulebook" else rec.record_id
            )
            if rid == record_id and rec.interval.valid_to is None:
                pool[idx] = rec.__class__(**{**rec.__dict__, "interval": Interval(rec.interval.valid_from, valid_to)})
                if kind == "verification_code":
                    self._code_index[rec.code] = pool[idx]
                return
        raise NotFoundError(f"待关闭的时态记录不存在：{kind}/{record_id}")

    # ----- 查询 ---------------------------------------------------------

    def customer(self, customer_id: str, moment: datetime | None = None) -> Customer:
        self._ensure_fresh()
        if customer_id not in self._customers:
            raise NotFoundError(f"客户不存在：{customer_id}")
        return self._customers[customer_id]

    def customer_by_code(self, code: str, moment: datetime) -> Customer:
        return self.customer(self.active_code(code, moment).customer_id)

    def active_code(self, code: str, moment: datetime) -> VerificationCode:
        self._ensure_fresh()
        return sole([c for c in self.codes if c.code == code], moment, f"核验码 {code}")

    def active_premises(self, customer_id: str, moment: datetime) -> list[Place]:
        self._ensure_fresh()
        return [p for p in self.premises if p.customer_id == customer_id and p.interval.contains(at(moment))]

    def active_warehouses(self, customer_id: str, moment: datetime) -> list[Place]:
        self._ensure_fresh()
        return [p for p in self.warehouses if p.customer_id == customer_id and p.interval.contains(at(moment))]

    def active_qualification(self, customer_id: str, moment: datetime) -> Qualification:
        self._ensure_fresh()
        recs = [q for q in self.qualifications if q.customer_id == customer_id]
        return sole(recs, moment, f"客户 {customer_id} 的资质")

    def rulebook(self, version: str) -> Rulebook:
        self._ensure_fresh()
        for rb in self.rulebooks:
            if rb.version == version:
                return rb
        raise NotFoundError(f"规则版本不存在：{version}")

    def active_rulebook(self, moment: datetime) -> Rulebook:
        self._ensure_fresh()
        try:
            return sole(self.rulebooks, moment, "规则书")
        except NotFoundError:
            raise RuleUnavailableError(f"{at(moment).isoformat()} 时点没有生效的规则版本")

    def location_registered(self, customer_id: str, geo: tuple[float, float], moment: datetime) -> Place | None:
        """现场位置是否落在任一登记的经营场所或外设仓库范围内，返回命中的登记点。"""
        self._ensure_fresh()
        for place in self.active_premises(customer_id, moment) + self.active_warehouses(customer_id, moment):
            if geo_distance(geo, place.geo) <= place.radius_m:
                return place
        return None


class MasterDataService:
    """登记/更替时态主数据的命令服务。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def _emit(self, entries: list[tuple]) -> None:
        self.store.append_batch(entries)  # type: ignore[arg-type]

    def register_customer(self, customer_id: str, name: str, actor: str, now: datetime) -> None:
        view = MasterDataView(self.store)
        try:
            view.customer(customer_id)
            raise ValidationError(f"客户已存在：{customer_id}")
        except NotFoundError:
            pass
        self._emit([(customer_id, "customer", "CustomerRegistered",
                     {"customer_id": customer_id, "name": name}, actor, at(now).isoformat())])

    def record_qualification(self, record_id: str, customer_id: str, license_no: str,
                             license_digest: str, scope: str, valid_from: datetime,
                             actor: str, license_expiry: datetime | None = None,
                             valid_to: datetime | None = None, now: datetime | None = None) -> None:
        moment = at(now or valid_from)
        view = MasterDataView(self.store)
        view.customer(customer_id)
        if any(q.record_id == record_id for q in view.qualifications):
            raise ValidationError(f"资质记录已存在：{record_id}")
        self._emit([(record_id, "qualification", "QualificationRecorded", {
            "record_id": record_id, "customer_id": customer_id,
            "license_no": license_no, "license_digest": license_digest,
            "scope": scope,
            "license_expiry": at(license_expiry).isoformat() if license_expiry else None,
            "valid_from": at(valid_from).isoformat(),
            "valid_to": at(valid_to).isoformat() if valid_to else None,
        }, actor, moment.isoformat())])

    def record_place(self, kind: str, record_id: str, customer_id: str, address: str,
                     geo: tuple[float, float], valid_from: datetime, actor: str,
                     radius_m: float = 200.0, valid_to: datetime | None = None,
                     now: datetime | None = None) -> None:
        if kind not in ("premises", "warehouse"):
            raise ValidationError("场所类型只能是 premises 或 warehouse")
        moment = at(now or valid_from)
        view = MasterDataView(self.store)
        view.customer(customer_id)
        pool = view.premises if kind == "premises" else view.warehouses
        if any(p.record_id == record_id for p in pool):
            raise ValidationError(f"场所记录已存在：{record_id}")
        event_type = "PremisesRecorded" if kind == "premises" else "WarehouseRecorded"
        self._emit([(record_id, kind, event_type, {
            "record_id": record_id, "customer_id": customer_id, "address": address,
            "lat": geo[0], "lng": geo[1], "radius_m": radius_m,
            "valid_from": at(valid_from).isoformat(),
            "valid_to": at(valid_to).isoformat() if valid_to else None,
        }, actor, moment.isoformat())])

    def issue_code(self, code: str, customer_id: str, declared_address: str,
                   valid_from: datetime, actor: str, valid_to: datetime | None = None,
                   now: datetime | None = None) -> None:
        moment = at(now or valid_from)
        view = MasterDataView(self.store)
        view.customer(customer_id)
        if any(c.code == code for c in view.codes):
            raise ValidationError(f"核验码已存在：{code}")
        self._emit([(code, "verification_code", "VerificationCodeRecorded", {
            "code": code, "customer_id": customer_id,
            "declared_address": declared_address,
            "valid_from": at(valid_from).isoformat(),
            "valid_to": at(valid_to).isoformat() if valid_to else None,
        }, actor, moment.isoformat())])

    def publish_rulebook(self, version: str, levels: dict[str, str],
                         valid_from: datetime, actor: str,
                         params: dict[str, Any] | None = None,
                         valid_to: datetime | None = None, now: datetime | None = None) -> None:
        moment = at(now or valid_from)
        unknown = set(levels) - set(LEAD_TYPES)
        if unknown:
            raise ValidationError(f"规则书包含未知线索类型：{sorted(unknown)}")
        bad_levels = {lv for lv in levels.values() if lv not in LEVEL_ORDER}
        if bad_levels:
            raise ValidationError(f"规则书包含未知级别：{sorted(bad_levels)}")
        view = MasterDataView(self.store)
        if any(rb.version == version for rb in view.rulebooks):
            raise ValidationError(f"规则版本已存在：{version}")
        self._emit([(version, "rulebook", "RulebookPublished", {
            "version": version, "levels": levels, "params": params or {},
            "valid_from": at(valid_from).isoformat(),
            "valid_to": at(valid_to).isoformat() if valid_to else None,
        }, actor, moment.isoformat())])

    def close_record(self, kind: str, record_id: str, valid_to: datetime, actor: str,
                     now: datetime | None = None) -> None:
        moment = at(now or valid_to)
        event_type = {
            "qualification": "QualificationClosed",
            "premises": "PremisesClosed",
            "warehouse": "WarehouseClosed",
            "verification_code": "VerificationCodeClosed",
            "rulebook": "RulebookClosed",
        }[kind]
        # 先校验记录存在且开放。
        MasterDataView(self.store)._close(kind, record_id, at(valid_to))
        self._emit([(record_id if kind != "verification_code" else record_id, kind, event_type,
                     {"valid_to": at(valid_to).isoformat()}, actor, moment.isoformat())])
