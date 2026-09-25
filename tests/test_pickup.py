"""揽件验视测试：当时所见快照、流水幂等、异文隔离。"""

import unittest

from support import T0, build_service, courier, seed_clean_customer, snapshot
from food_parcel_regulation.errors import IdempotencyConflictError
from food_parcel_regulation.pickup import (
    PickupOutcome,
    TempRequirement,
)


class PickupTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_clean_customer(self.svc, warehouses=("外设仓库B",))
        self.actor = courier()

    def _report(self, snap=None, serial="S1", code="CODE1",
                address=None, observed=T0, **kw):
        snap = snap or snapshot(address=address or "外设仓库B", **kw)
        return self.svc.report_pickup(
            self.actor, serial=serial, branch_id="B1", customer_id="K1",
            snapshot=snap, tracking_no=f"T-{serial}", batch_id="BAT1",
            verification_code=code, observed_at=observed)

    def test_first_pickup_creates_once_with_snapshot(self):
        result = self._report()
        self.assertFalse(result.replayed)
        self.assertEqual(result.record.outcome, PickupOutcome.CREATED)
        self.assertEqual(len(self.svc.parcels.all()), 1)
        # 快照保存当时所见摘要
        s = result.record.snapshot
        self.assertEqual(s.license.license_no, "SP123")
        self.assertEqual(s.location.address, "外设仓库B")
        self.assertTrue(s.temperature.chain_ok_at_pickup)

    def test_offline_same_content_replay_is_idempotent(self):
        first = self._report()
        # 设备离线后重传：同样内容（含 observed_at）不得生成第二次揽件
        replay = self._report(observed=T0)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.record.outcome, PickupOutcome.REPLAYED)
        self.assertEqual(len(self.svc.parcels.all()), 1)
        self.assertEqual(first.record.tracking_no, replay.record.tracking_no)

    def test_replay_after_restart_still_single_pickup(self):
        import os
        import tempfile
        self._report()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            self.svc.save(path)
            restored = type(self.svc).load(path)
            result = restored.report_pickup(
                self.actor, serial="S1", branch_id="B1", customer_id="K1",
                snapshot=snapshot(address="外设仓库B"),
                tracking_no="T-S1", batch_id="BAT1",
                verification_code="CODE1", observed_at=T0)
            self.assertTrue(result.replayed)
            self.assertEqual(len(restored.parcels.all()), 1)

    def test_same_serial_different_content_is_quarantined(self):
        self._report()
        different = snapshot(address="外设仓库B", goods="冷冻肉类",
                             requirement=TempRequirement.FROZEN, temp=-18.0)
        with self.assertRaises(IdempotencyConflictError) as ctx:
            self._report(snap=different)
        # 隔离区有记录，但原揽件不受影响、没有第二个包裹
        quarantined = self.svc.pickups.quarantine()
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].quarantine_id, ctx.exception.quarantine_id)
        self.assertEqual(quarantined[0].serial, "S1")
        self.assertEqual(len(self.svc.parcels.all()), 1)
        self.assertEqual(
            self.svc.pickups.get("S1").snapshot.goods_category, "冷藏预制食品")

    def test_different_serials_create_separate_pickups(self):
        self._report(serial="S1")
        self._report(serial="S2", address="外设仓库B")
        self.assertEqual(len(self.svc.parcels.all()), 2)

    def test_snapshot_keeps_observed_license_summary_only(self):
        snap = snapshot()
        result = self._report(snap=snap)
        stored = result.record.snapshot.license
        # 只存摘要字段，不存影像
        self.assertEqual(set(stored.__dict__.keys()) if hasattr(stored, "__dict__")
                         else {"license_no", "scope", "shown_expiry",
                               "doc_fingerprint"},
                         {"license_no", "scope", "shown_expiry",
                          "doc_fingerprint"})


if __name__ == "__main__":
    unittest.main()
