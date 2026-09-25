"""风险扩围、退回、投诉归链与部门移交测试。"""

import unittest
from datetime import timedelta

from world import build_world, observation

from food_parcel_regulation import aggregates as ag
from food_parcel_regulation.errors import ConflictError, ValidationError
from food_parcel_regulation.timemodel import FakeClock


class PropagationTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = build_world(self.clock)
        self.clock.advance(timedelta(days=1))
        r1 = self.b.pickups.accept(observation("P-1"), "courier:k1")
        r2 = self.b.pickups.accept(observation("P-2"), "courier:k1")
        r3 = self.b.pickups.accept(observation("P-3"), "courier:k1")
        self.p_pending = r1["parcel_id"]
        self.p_transit = r2["parcel_id"]
        self.p_delivered = r3["parcel_id"]
        # P-2 在途，P-3 已妥投，P-1 保持待发。
        self.b.parcels.scan(self.p_transit, "depart", "courier:k1")
        self.b.parcels.scan(self.p_delivered, "depart", "courier:k1")
        self.clock.advance(timedelta(hours=8))
        self.b.parcels.scan(self.p_transit, "arrive", "courier:k1")
        self.b.parcels.scan(self.p_delivered, "arrive", "courier:k1")
        self.b.parcels.scan(self.p_delivered, "delivered", "courier:k1")

    def test_expansion_only_affects_related_batch_open_parcels(self):
        lead_id = self.b.parcels.report_temp_interruption(
            self.p_transit, "courier:k1", 45, "冷机故障"
        )
        self.b.leads.accept_for_check(lead_id, "regulator:r2")
        result = self.b.leads.expand_risk(lead_id, "regulator:r2", ["batch-1"])

        self.assertIn(self.p_pending, result["frozen"])
        self.assertIn(self.p_transit, result["frozen"])
        # 已妥投件不冻结。
        self.assertNotIn(self.p_delivered, result["frozen"])
        self.assertIn(self.p_delivered, result["notified"])

        delivered = self.b.repo.get(self.p_delivered, "parcel")
        self.assertEqual(delivered.lifecycle, ag.DELIVERED)
        self.assertEqual(len(delivered.notification_duties), 1)
        self.assertIsNone(delivered.hold)

        lead = self.b.repo.get(lead_id, "lead")
        self.assertEqual(lead.propagation_batches, ["batch-1"])

    def test_expansion_does_not_touch_unrelated_batch(self):
        self.b.batches.create_batch("batch-other", "c1", "courier:k1")
        lead_id = self.b.parcels.report_temp_interruption(
            self.p_transit, "courier:k1", 45, "冷机故障"
        )
        self.b.leads.accept_for_check(lead_id, "regulator:r2")
        # 只扩到 batch-other（空批次）——batch-1 的件不受波及。
        result = self.b.leads.expand_risk(lead_id, "regulator:r2", ["batch-other"])
        self.assertEqual(result["frozen"], [])
        self.assertEqual(result["notified"], [])
        self.assertIsNone(self.b.repo.get(self.p_pending, "parcel").hold)

    def test_frozen_parcel_cannot_return_until_unfrozen(self):
        lead_id = self.b.parcels.report_temp_interruption(
            self.p_transit, "courier:k1", 45
        )
        self.b.leads.accept_for_check(lead_id, "regulator:r2")
        self.b.leads.freeze_parcel(self.p_transit, lead_id, "regulator:r2")
        with self.assertRaises(ConflictError):
            self.b.parcels.request_return(self.p_transit, "courier:k1", "拒收")
        self.b.leads.unfreeze_parcel(self.p_transit, lead_id, "regulator:r2")
        self.b.parcels.request_return(self.p_transit, "courier:k1", "客户拒收")
        self.b.parcels.confirm_returned(self.p_transit, "courier:k1")
        parcel = self.b.repo.get(self.p_transit, "parcel")
        self.assertEqual(parcel.lifecycle, ag.RETURNED)
        # 退回终结后不再接受扫描。
        with self.assertRaises((ConflictError, ValidationError)):
            self.b.parcels.scan(self.p_transit, "arrive", "courier:k1")

    def test_complaint_is_routed_into_existing_lead_chain(self):
        lead_id = self.b.parcels.report_temp_interruption(
            self.p_transit, "courier:k1", 45
        )
        complaint_id = self.b.complaints.file(
            self.p_transit, "courier:k1", "客户反映食品变质"
        )
        complaint = self.b.repo.get(complaint_id, "complaint")
        self.assertEqual(complaint.lead_id, lead_id)
        self.assertEqual(complaint.routed_to, ["regulator"])


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = build_world(self.clock)
        self.clock.advance(timedelta(days=1))
        result = self.b.pickups.accept(observation("H-1"), "courier:k1")
        self.parcel_id = result["parcel_id"]
        self.lead_id = self.b.parcels.report_temp_interruption(
            self.parcel_id, "courier:k1", 30
        )

    def test_handover_lifecycle_and_full_timeline(self):
        handover_id = self.b.handovers.initiate(
            self.lead_id, "postal:o1", [self.parcel_id]
        )
        handover = self.b.repo.get(handover_id, "handover")
        self.assertEqual(handover.status, "initiated")
        self.assertIsNotNone(handover.due_at)

        # 冻结与部门移交同时发生：冻结先落，扫描被拒，时间线同时保留两者。
        self.b.leads.accept_for_check(self.lead_id, "regulator:r2")
        self.b.leads.freeze_parcel(self.parcel_id, self.lead_id, "regulator:r2")
        with self.assertRaises(ConflictError):
            self.b.parcels.scan(self.parcel_id, "depart", "courier:k1")

        timeline = self.b.view.handover_timeline(handover_id, "regulator:r2")
        kinds = [e["event"] for e in timeline["timeline"]]
        self.assertIn("HandoverInitiated", kinds)
        self.assertIn("parcel:regulatory_freeze", kinds)
        # 被拒的扫描不会进入时间线——不存在放行与冻结并存。
        self.assertNotIn("parcel:depart", kinds)

        self.b.handovers.receive(handover_id, "regulator:r2")
        self.assertEqual(
            self.b.repo.get(handover_id, "handover").status, "received"
        )

    def test_receipt_reminder_only_after_due_and_once_pending(self):
        handover_id = self.b.handovers.initiate(
            self.lead_id, "postal:o1", [self.parcel_id]
        )
        self.assertFalse(self.b.handovers.remind(handover_id, "postal:o1"))
        self.clock.advance(timedelta(hours=25))
        self.assertTrue(self.b.handovers.remind(handover_id, "postal:o1"))
        self.b.handovers.receive(handover_id, "regulator:r2")
        # 回执后不再催办。
        self.assertFalse(self.b.handovers.remind(handover_id, "postal:o1"))


if __name__ == "__main__":
    unittest.main()
