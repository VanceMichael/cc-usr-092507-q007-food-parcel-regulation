"""线索处置职权分离、包裹暂停/冻结互斥与决定时序测试。"""

import unittest
from datetime import timedelta

from world import UNREGISTERED_GEO, build_world, observation

from food_parcel_regulation.errors import (
    AuthorizationError,
    ConflictError,
    StaleDecisionError,
    ValidationError,
)
from food_parcel_regulation import aggregates as ag
from food_parcel_regulation.timemodel import FakeClock


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = build_world(self.clock)
        self.clock.advance(timedelta(days=1))
        result = self.b.pickups.accept(
            observation("L-1", lat=UNREGISTERED_GEO[0], lng=UNREGISTERED_GEO[1]),
            "courier:k1",
        )
        self.parcel_id = result["parcel_id"]
        self.lead_id = result["lead_ids"][0]

    # ----- 职权分离 -----------------------------------------------------

    def test_reporter_cannot_decide_or_close_own_lead(self):
        for action in ("accept_for_check", "release", "file_case", "close_case"):
            with self.assertRaises(AuthorizationError):
                getattr(self.b.leads, action)(self.lead_id, "courier:k1")

    def test_courier_role_cannot_handle_lead(self):
        with self.assertRaises(AuthorizationError):
            self.b.leads.accept_for_check(self.lead_id, "courier:k9")

    def test_lead_lifecycle_and_filing_closure(self):
        lead = self.b.repo.get(self.lead_id, "lead")
        self.assertEqual(lead.status, ag.LEAD_REPORTED)
        self.b.leads.accept_for_check(self.lead_id, "regulator:r2")
        self.assertEqual(self.b.repo.get(self.lead_id, "lead").status, ag.LEAD_CHECKING)
        # 已受理的线索不能直接关闭，必须先立案。
        with self.assertRaises(ConflictError):
            self.b.leads.close_case(self.lead_id, "regulator:r2")
        self.b.leads.file_case(self.lead_id, "regulator:r2")
        self.b.leads.close_case(self.lead_id, "regulator:r2")
        self.assertEqual(self.b.repo.get(self.lead_id, "lead").status, ag.LEAD_CLOSED)

    def test_release_transitions_lead(self):
        self.b.leads.accept_for_check(self.lead_id, "regulator:r2")
        self.b.leads.release(self.lead_id, "regulator:r2")
        self.assertEqual(self.b.repo.get(self.lead_id, "lead").status, ag.LEAD_RELEASED)
        # 已放行不能再立案。
        with self.assertRaises(ConflictError):
            self.b.leads.file_case(self.lead_id, "regulator:r2")

    # ----- 企业暂停 -----------------------------------------------------

    def test_enterprise_can_hold_unshipped_and_release(self):
        self.b.parcels.enterprise_hold(self.parcel_id, "courier:k1", "等待复核")
        parcel = self.b.repo.get(self.parcel_id, "parcel")
        self.assertEqual(parcel.hold.kind, ag.HOLD_ENTERPRISE)
        with self.assertRaises(ConflictError):
            self.b.parcels.scan(self.parcel_id, "depart", "courier:k1")
        self.b.parcels.enterprise_release(self.parcel_id, "courier:k1")
        self.assertIsNone(self.b.repo.get(self.parcel_id, "parcel").hold)

    def test_enterprise_cannot_hold_in_transit(self):
        self.b.parcels.scan(self.parcel_id, "depart", "courier:k1")
        with self.assertRaises(ValidationError):
            self.b.parcels.enterprise_hold(self.parcel_id, "courier:k1")

    def test_enterprise_cannot_release_regulatory_freeze(self):
        self.b.parcels.enterprise_hold(self.parcel_id, "courier:k1")
        self.b.leads.freeze_parcel(self.parcel_id, self.lead_id, "regulator:r2")
        with self.assertRaises((AuthorizationError, ValidationError)):
            self.b.parcels.enterprise_release(self.parcel_id, "courier:k1")
        parcel = self.b.repo.get(self.parcel_id, "parcel")
        self.assertEqual(parcel.hold.kind, ag.HOLD_REGULATORY)

    # ----- 冻结与扫描 ---------------------------------------------------

    def test_frozen_parcel_rejects_scans_and_delivery(self):
        self.b.leads.freeze_parcel(self.parcel_id, self.lead_id, "regulator:r2")
        for scan in ("depart", "arrive", "transfer"):
            with self.assertRaises(ConflictError):
                self.b.parcels.scan(self.parcel_id, scan, "courier:k1")

    def test_no_hold_freeze_coexistence_record(self):
        self.b.parcels.enterprise_hold(self.parcel_id, "courier:k1")
        self.b.leads.freeze_parcel(self.parcel_id, self.lead_id, "regulator:r2")
        parcel = self.b.repo.get(self.parcel_id, "parcel")
        # 单一 hold 槽位：任何时刻至多一个生效的暂停/冻结。
        self.assertEqual(parcel.hold.kind, ag.HOLD_REGULATORY)
        self.assertIsNotNone(parcel.last_decided_at)

    def test_delivered_parcel_cannot_be_frozen(self):
        self.b.parcels.scan(self.parcel_id, "depart", "courier:k1")
        self.clock.advance(timedelta(hours=10))
        self.b.parcels.scan(self.parcel_id, "arrive", "courier:k1")
        self.b.parcels.scan(self.parcel_id, "delivered", "courier:k1")
        with self.assertRaises(ValidationError):
            self.b.leads.freeze_parcel(self.parcel_id, self.lead_id, "regulator:r2")

    def test_stale_decision_cannot_overwrite_new_state(self):
        self.b.leads.freeze_parcel(
            self.parcel_id, self.lead_id, "regulator:r2",
            decided_at=self.clock.now(),
        )
        self.clock.advance(timedelta(hours=5))
        # 一个时间戳更早的“放行/解冻”不能覆盖之后的冻结状态。
        with self.assertRaises(StaleDecisionError):
            self.b.leads.unfreeze_parcel(
                self.parcel_id, self.lead_id, "regulator:r2",
                decided_at=self.clock.now() - timedelta(hours=10),
            )
        self.assertEqual(
            self.b.repo.get(self.parcel_id, "parcel").hold.kind,
            ag.HOLD_REGULATORY,
        )

    def test_release_lead_unfreezes_parcel_atomically(self):
        self.b.leads.accept_for_check(self.lead_id, "regulator:r2")
        self.b.leads.freeze_parcel(self.parcel_id, self.lead_id, "regulator:r2")
        self.b.leads.release(self.lead_id, "regulator:r2")
        self.assertIsNone(self.b.repo.get(self.parcel_id, "parcel").hold)
        self.assertEqual(self.b.repo.get(self.lead_id, "lead").status, ag.LEAD_RELEASED)
        # 放行后包裹可以继续流转。
        self.b.parcels.scan(self.parcel_id, "depart", "courier:k1")
        self.assertEqual(
            self.b.repo.get(self.parcel_id, "parcel").lifecycle, ag.IN_TRANSIT
        )


if __name__ == "__main__":
    unittest.main()
