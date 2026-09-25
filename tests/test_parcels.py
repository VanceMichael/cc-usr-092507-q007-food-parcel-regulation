"""包裹状态机、风险扩大与通知责任测试。"""

import unittest
from datetime import timedelta

from support import T0, build_service, courier, postal, regulator, seed_clean_customer, snapshot
from food_parcel_regulation.errors import ConflictError, PermissionDeniedError
from food_parcel_regulation.parcels import ParcelStatus
from food_parcel_regulation.rules import ClueLevel, RuleType


def _pickup(svc, actor, cid, serial, tn, batch, *, address="登记地址A", code=None):
    code = code or f"CODE-{tn}"
    return svc.report_pickup(
        actor, serial=serial, branch_id="B1", customer_id=cid,
        snapshot=snapshot(address=address), tracking_no=tn, batch_id=batch,
        verification_code=code, observed_at=T0)


class ParcelFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_clean_customer(self.svc, code="CODE-TA")
        self.courier = courier()
        self.regulator = regulator()
        self.post = postal()
        _pickup(self.svc, self.courier, "K1", "SA", "TA", "BAT", code="CODE-TA")

    def test_enterprise_pauses_only_unshipped(self):
        self.svc.pause_unshipped(self.courier, "TA", at=T0, reason="等核验")
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.PAUSED)
        self.svc.resume_unshipped(self.courier, "TA", at=T0 + timedelta(minutes=1))
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.PICKED_UP)
        self.svc.dispatch(self.courier, "TA", at=T0 + timedelta(hours=1))
        # 已发出不能再由企业暂停
        with self.assertRaises(ConflictError):
            self.svc.pause_unshipped(self.courier, "TA", at=T0 + timedelta(hours=2))

    def test_regulator_can_freeze_in_transit(self):
        self.svc.dispatch(self.courier, "TA", at=T0 + timedelta(hours=1))
        self.svc.scan(self.courier, "TA", node="分拨中心", kind="transit",
                      at=T0 + timedelta(hours=2))
        self.svc.parcels.freeze("TA", actor=self.regulator,
                                at=T0 + timedelta(hours=3), clue_id="CDEMO")
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.FROZEN)
        # 冻结中扫描不得推进
        with self.assertRaises(ConflictError):
            self.svc.scan(self.courier, "TA", node="下一中心", kind="transit",
                          at=T0 + timedelta(hours=4))
        # 监管放行解锢后回到冻结前状态
        self.svc.parcels.release_freeze("TA", actor=self.regulator,
                                        at=T0 + timedelta(hours=5))
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.IN_TRANSIT)

    def test_courier_cannot_release_freeze(self):
        self.svc.parcels.freeze("TA", actor=self.regulator, at=T0,
                                clue_id="CDEMO")
        with self.assertRaises(PermissionDeniedError):
            self.svc.parcels.release_freeze("TA", actor=self.courier, at=T0)

    def test_delivered_is_immutable_fact(self):
        self.svc.dispatch(self.courier, "TA", at=T0 + timedelta(hours=1))
        self.svc.scan(self.courier, "TA", node="派送", kind="out_for_delivery",
                      at=T0 + timedelta(hours=2))
        self.svc.scan(self.courier, "TA", node="收件人", kind="delivered",
                      at=T0 + timedelta(hours=3))
        # 妥投后不能冻结成 FROZEN
        with self.assertRaises(ConflictError):
            self.svc.parcels.freeze("TA", actor=self.regulator,
                                    at=T0 + timedelta(hours=4), clue_id="CX")
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.DELIVERED)
        self.assertIsNotNone(self.svc.parcels.get("TA").delivered_at)

    def test_return_flow(self):
        self.svc.dispatch(self.courier, "TA", at=T0 + timedelta(hours=1))
        self.svc.scan(self.courier, "TA", node="分拨", kind="return",
                      at=T0 + timedelta(hours=2))
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.RETURNING)
        self.svc.confirm_returned(self.courier, "TA", at=T0 + timedelta(days=1))
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.RETURNED)


class RiskPropagationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        for cid, code in (("K1", "C1"), ("K2", "C2"), ("K3", "C3")):
            seed_clean_customer(self.svc, cid, code=code)
        self.courier = courier()
        self.regulator = regulator()
        # 同批 BAT：TA 在途、TB 已妥投；另批 OTHER：TC 在途
        _pickup(self.svc, self.courier, "K1", "SA", "TA", "BAT", code="C1")
        _pickup(self.svc, self.courier, "K2", "SB", "TB", "BAT", code="C2")
        _pickup(self.svc, self.courier, "K3", "SC", "TC", "OTHER", code="C3")
        for tn in ("TA", "TB", "TC"):
            self.svc.dispatch(self.courier, tn, at=T0 + timedelta(hours=1))
        self.svc.scan(self.courier, "TA", node="分拨", kind="transit",
                      at=T0 + timedelta(hours=2))
        self.svc.scan(self.courier, "TB", node="收件人", kind="delivered",
                      at=T0 + timedelta(hours=3))

        self.clue = self.svc.clues.create(
            rule_type=RuleType.ADDRESS_MISMATCH, level=ClueLevel.HIGH,
            rule_version="2026.1", customer_id="K1", serial="SA",
            tracking_no="TA", batch_id="BAT", title="批次风险",
            evidence="x", reporter=self.courier, reported_at=T0)

    def test_expansion_hits_only_related_in_transit(self):
        result = self.svc.expand_risk_to_batch(
            self.regulator, self.clue.clue_id, at=T0 + timedelta(hours=4),
            reason="风险扩大")
        self.assertEqual(result["frozen"], ["TA"])
        self.assertEqual(result["notified"], ["TB"])
        self.assertEqual(self.svc.parcels.get("TA").status, ParcelStatus.FROZEN)
        # 妥投事实保留
        self.assertEqual(self.svc.parcels.get("TB").status, ParcelStatus.DELIVERED)
        # 不关联批次绝不受影响
        self.assertEqual(self.svc.parcels.get("TC").status, ParcelStatus.DISPATCHED)

    def test_delivered_becomes_notification_duty(self):
        self.svc.expand_risk_to_batch(
            self.regulator, self.clue.clue_id, at=T0 + timedelta(hours=4))
        duties = self.svc.parcels.duties()
        self.assertEqual(len(duties), 1)
        self.assertEqual(duties[0].tracking_no, "TB")
        self.assertIsNone(duties[0].fulfilled_at)
        # 企业履行通知责任
        done = self.svc.fulfill_notification(self.courier, duties[0].duty_id,
                                             at=T0 + timedelta(hours=5))
        self.assertIsNotNone(done.fulfilled_at)
        # 妥投状态依旧
        self.assertEqual(self.svc.parcels.get("TB").status, ParcelStatus.DELIVERED)

    def test_no_release_and_freeze_coexistence(self):
        # 冻结 TA
        self.svc.parcels.freeze("TA", actor=self.regulator, at=T0,
                                clue_id=self.clue.clue_id)
        # 监管拿着过期序号放行：必须冲突，不能产生半放行半冻结
        with self.assertRaises(ConflictError):
            self.svc.parcels.release_freeze(
                "TA", actor=self.regulator, at=T0 + timedelta(hours=5),
                expected_seq=0)
        parcel = self.svc.parcels.get("TA")
        self.assertEqual(parcel.status, ParcelStatus.FROZEN)
        self.assertEqual(parcel.frozen_by_clue, self.clue.clue_id)


if __name__ == "__main__":
    unittest.main()
