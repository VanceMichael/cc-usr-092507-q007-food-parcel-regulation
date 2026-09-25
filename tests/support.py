"""测试场景构造辅助。"""

import sys
from datetime import datetime
from pathlib import Path

# 让测试直接 import 领域包与本辅助模块
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from food_parcel_regulation.actors import Actor, Role  # noqa: E402
from food_parcel_regulation.pickup import (  # noqa: E402
    InspectionSnapshot,
    LicenseSnapshot,
    LocationSnapshot,
    TemperatureSnapshot,
    TempRequirement,
)
from food_parcel_regulation.rules import (  # noqa: E402
    ClueLevel,
    RuleType,
    RuleVersion,
)
from food_parcel_regulation.service import CoordinationService  # noqa: E402
from food_parcel_regulation.temporal import (  # noqa: E402
    Premise,
    Qualification,
    VerificationCode,
    Warehouse,
)

T0 = datetime(2026, 9, 1, 8, 0, 0)


def courier(cid="U1", name="快递员甲"):
    return Actor(cid, name, Role.COURIER_STAFF)


def regulator(cid="R1", name="监管员乙"):
    return Actor(cid, name, Role.MARKET_REGULATOR)


def postal(cid="P1", name="邮政管理员丙"):
    return Actor(cid, name, Role.POSTAL_ADMIN)


def customer(cid="K1"):
    return Actor(cid, f"客户{cid}", Role.CUSTOMER)


def publish_standard_rules(service, *, temp_levels=True):
    service.publish_rule(RuleVersion(
        RuleType.ADDRESS_MISMATCH, "2026.1", datetime(2026, 1, 1),
        ClueLevel.MEDIUM))
    service.publish_rule(RuleVersion(
        RuleType.LICENSE_EXPIRED, "2026.1", datetime(2026, 1, 1),
        ClueLevel.HIGH))
    service.publish_rule(RuleVersion(
        RuleType.PACKAGING_ABNORMAL, "2026.1", datetime(2026, 1, 1),
        ClueLevel.LOW))
    levels = ((1800, ClueLevel.MEDIUM), (3600, ClueLevel.HIGH)) if temp_levels else ()
    service.publish_rule(RuleVersion(
        RuleType.TEMP_INTERRUPTION, "2026.1", datetime(2026, 1, 1),
        ClueLevel.INFO, duration_levels=levels))


def snapshot(*, address="登记地址A", license_no="SP123",
             requirement=TempRequirement.REFRIGERATED, chain_ok=True,
             temp=4.0, packaging_ok=True, expiry=datetime(2027, 1, 1),
             goods="冷藏预制食品", remarks="", fp="fp-1", device="DEV1"):
    return InspectionSnapshot(
        license=LicenseSnapshot(license_no, "冷藏食品", expiry, fp),
        goods_category=goods,
        temperature=TemperatureSnapshot(requirement, chain_ok, temp, device),
        location=LocationSnapshot(address),
        packaging_ok=packaging_ok,
        remarks=remarks,
    )


def seed_clean_customer(service, customer_id="K1", *, code="CODE1",
                        registered="登记地址A", sites=("登记地址A",),
                        warehouses=()):
    service.register_qualification(Qualification(
        "Q1", customer_id, "SP123", "冷藏食品", datetime(2025, 1, 1)))
    for i, addr in enumerate(sites):
        service.register_premise(Premise(
            f"P{i + 1}", customer_id, addr, datetime(2025, 1, 1)))
    for i, addr in enumerate(warehouses):
        service.register_warehouse(Warehouse(
            f"W{i + 1}", customer_id, addr, datetime(2025, 1, 1)))
    service.register_code(VerificationCode(
        code, customer_id, registered, datetime(2025, 1, 1)))


def build_service(clock=None):
    service = CoordinationService(clock=clock or (lambda: T0))
    publish_standard_rules(service)
    return service
