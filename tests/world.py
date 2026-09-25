"""测试共用构造：一个已登记客户、场所、外设仓库、资质、核验码和两版规则书的世界。"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from food_parcel_regulation import Backend, PickupObservation  # noqa: E402
from food_parcel_regulation.masterdata import (  # noqa: E402
    ADDRESS_MISMATCH,
    LICENSE_EXPIRED,
    LEVEL_GENERAL,
    LEVEL_INFO,
    LEVEL_MAJOR,
    PACKAGING_ABNORMAL,
    TEMP_BREAK,
)
from food_parcel_regulation.timemodel import FakeClock  # noqa: E402

LEVELS_V1 = {
    ADDRESS_MISMATCH: LEVEL_INFO,
    LICENSE_EXPIRED: LEVEL_GENERAL,
    PACKAGING_ABNORMAL: LEVEL_INFO,
    TEMP_BREAK: LEVEL_INFO,
}
LEVELS_V2 = {
    ADDRESS_MISMATCH: LEVEL_MAJOR,
    LICENSE_EXPIRED: LEVEL_MAJOR,
    PACKAGING_ABNORMAL: LEVEL_GENERAL,
    TEMP_BREAK: LEVEL_MAJOR,
}
PARAMS = {
    "temp_break_tolerance_min": 120,
    "receipt_due_hours": 24,
    "freeze_followup_hours": 48,
}

PREMISES_GEO = (30.0, 120.0)
WAREHOUSE_GEO = (30.01, 120.01)
UNREGISTERED_GEO = (31.0, 121.0)


def build_world(clock: FakeClock | None = None, path: str | None = None) -> Backend:
    clock = clock or FakeClock()
    backend = Backend(clock=clock, path=path)
    t0 = clock.now()
    md = backend.master_data
    md.register_customer("c1", "某食品协议客户", "regulator:r1", t0)
    md.record_place("premises", "premises-1", "c1", "登记经营地址A", PREMISES_GEO, t0,
                    "regulator:r1", radius_m=500)
    md.record_place("warehouse", "warehouse-1", "c1", "外设仓库B", WAREHOUSE_GEO, t0,
                    "regulator:r1", radius_m=500)
    md.record_qualification("qual-1", "c1", "LIC-001", "digest-ok", "预包装冷藏食品",
                            t0, "regulator:r1", license_expiry=t0 + timedelta(days=365))
    md.issue_code("VC-1", "c1", "登记经营地址A", t0, "regulator:r1")
    md.publish_rulebook("2026.v1", LEVELS_V1, t0, "regulator:r1", params=PARAMS)
    backend.batches.create_batch("batch-1", "c1", "courier:k1")
    return backend


def observation(serial: str, **overrides) -> PickupObservation:
    base = dict(
        serial_no=serial,
        code="VC-1",
        license_digest="digest-ok",
        goods_category="冷藏熟食",
        temp_required=True,
        temp_range=(0.0, 8.0),
        observed_temp=5.0,
        lat=PREMISES_GEO[0],
        lng=PREMISES_GEO[1],
        observed_address="登记经营地址A",
        packaging_ok=True,
        packaging_note="",
        waybill_no=f"WB-{serial}",
        batch_id="batch-1",
        device_id="PDA-7",
    )
    base.update(overrides)
    return PickupObservation(**base)
