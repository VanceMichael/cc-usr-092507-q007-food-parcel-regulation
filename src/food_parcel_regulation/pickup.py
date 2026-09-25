"""揽件验视：只保存当时所见的快照，并对网点流水做幂等。

- 验视快照是“当时所见”：证照只留摘要（证号、范围、表面有效期、
  摘要指纹），不留原件影像；货物类别、温控条件、揽收位置同样按
  上报瞬间冻结，之后档案变化不回写快照。
- 每条网点流水（``serial``）至多产生一次揽件：
  同流水同文重传 → 幂等返回原记录（设备离线重传场景）；
  同流水异文 → 不覆盖、不二次受理，原样进入隔离区并上报冲突。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .errors import IdempotencyConflictError


class TempRequirement(StrEnum):
    AMBIENT = "ambient"   # 常温
    REFRIGERATED = "refrigerated"  # 冷藏
    FROZEN = "frozen"     # 冷冻


@dataclass(frozen=True)
class LicenseSnapshot:
    """揽件时所见证照的摘要，不含影像与原件。"""

    license_no: str
    scope: str
    shown_expiry: datetime | None     # 证面有效期
    doc_fingerprint: str              # 原件摘要指纹（脱敏后哈希）


@dataclass(frozen=True)
class TemperatureSnapshot:
    requirement: TempRequirement
    chain_ok_at_pickup: bool          # 揽收瞬间温控链是否正常
    temp_celsius: float | None
    carrier_device: str | None = None # 温控设备编号（时限计时关联）


@dataclass(frozen=True)
class LocationSnapshot:
    address: str                      # 现场仓库/揽收地址（所见）
    geohash: str | None = None        # 可选粗粒度位置，不保存精确轨迹


@dataclass(frozen=True)
class InspectionSnapshot:
    license: LicenseSnapshot
    goods_category: str               # 货物类别（如：冷藏预制食品）
    temperature: TemperatureSnapshot
    location: LocationSnapshot
    packaging_ok: bool = True         # 包装是否异常（验视所见）
    remarks: str = ""


@dataclass(frozen=True)
class QuarantineEntry:
    """同流水异文的隔离记录：两次上报都保留、互不覆盖。"""

    quarantine_id: str
    serial: str
    branch_id: str
    first_received_at: datetime
    conflict_received_at: datetime
    first_fingerprint: str
    conflict_fingerprint: str
    conflict_payload: dict


class PickupOutcome(StrEnum):
    CREATED = "created"     # 首次受理
    REPLAYED = "replayed"   # 同文重传，幂等返回


@dataclass
class PickupRecord:
    serial: str                           # 网点流水号（幂等键）
    branch_id: str
    courier_id: str
    customer_id: str
    verification_code: str | None         # 首次揽件使用的专属核验码
    snapshot: InspectionSnapshot
    observed_at: datetime                 # 验视发生时间（设备时钟）
    received_at: datetime                 # 后端受理时间
    tracking_no: str
    batch_id: str
    content_fingerprint: str
    outcome: PickupOutcome = PickupOutcome.CREATED


def canonical_fingerprint(snapshot: InspectionSnapshot, *, code: str | None, observed_at: datetime) -> str:
    """对验视内容做规范指纹，用于识别同文/异文重传。"""

    payload = _snapshot_to_dict(snapshot)
    payload["code"] = code
    payload["observed_at"] = observed_at.isoformat()
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_to_dict(s: InspectionSnapshot) -> dict:
    return {
        "license": {
            "license_no": s.license.license_no,
            "scope": s.license.scope,
            "shown_expiry": s.license.shown_expiry.isoformat() if s.license.shown_expiry else None,
            "doc_fingerprint": s.license.doc_fingerprint,
        },
        "goods_category": s.goods_category,
        "temperature": {
            "requirement": s.temperature.requirement.value,
            "chain_ok_at_pickup": s.temperature.chain_ok_at_pickup,
            "temp_celsius": s.temperature.temp_celsius,
            "carrier_device": s.temperature.carrier_device,
        },
        "location": {"address": s.location.address, "geohash": s.location.geohash},
        "packaging_ok": s.packaging_ok,
        "remarks": s.remarks,
    }


class PickupRegistry:
    """流水 → 揽件记录；异文进隔离。"""

    def __init__(self) -> None:
        self._records: dict[str, PickupRecord] = {}
        self._quarantine: list[QuarantineEntry] = []
        self._seq = 0
    def register(
        self,
        *,
        serial: str,
        branch_id: str,
        courier_id: str,
        customer_id: str,
        verification_code: str | None,
        snapshot: InspectionSnapshot,
        observed_at: datetime,
        received_at: datetime,
        tracking_no: str,
        batch_id: str,
    ) -> PickupRecord:
        fingerprint = canonical_fingerprint(
            snapshot, code=verification_code, observed_at=observed_at
        )
        existing = self._records.get(serial)
        if existing is not None:
            if existing.content_fingerprint == fingerprint:
                # 离线重传、内容一致：不生成第二次揽件，原样回执。
                existing.outcome = PickupOutcome.REPLAYED
                return existing
            # 同流水异文：隔离本次上报，原揽件保持不变。
            self._seq += 1
            entry = QuarantineEntry(
                quarantine_id=f"Q{self._seq:08d}",
                serial=serial,
                branch_id=branch_id,
                first_received_at=existing.received_at,
                conflict_received_at=received_at,
                first_fingerprint=existing.content_fingerprint,
                conflict_fingerprint=fingerprint,
                conflict_payload=_snapshot_to_dict(snapshot),
            )
            self._quarantine.append(entry)
            raise IdempotencyConflictError(serial, entry.quarantine_id)

        record = PickupRecord(
            serial=serial,
            branch_id=branch_id,
            courier_id=courier_id,
            customer_id=customer_id,
            verification_code=verification_code,
            snapshot=snapshot,
            observed_at=observed_at,
            received_at=received_at,
            tracking_no=tracking_no,
            batch_id=batch_id,
            content_fingerprint=fingerprint,
        )
        self._records[serial] = record
        return record

    def get(self, serial: str) -> PickupRecord:
        return self._records[serial]

    def all_records(self) -> list[PickupRecord]:
        return list(self._records.values())

    def quarantine(self) -> list[QuarantineEntry]:
        return list(self._quarantine)
