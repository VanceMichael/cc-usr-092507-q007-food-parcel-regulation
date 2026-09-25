"""揽件幂等、离线重传隔离与验视快照测试。"""

import unittest
from datetime import timedelta

from world import UNREGISTERED_GEO, WAREHOUSE_GEO, build_world, observation

from food_parcel_regulation.errors import QuarantineError
from food_parcel_regulation.masterdata import (
    ADDRESS_MISMATCH,
    LICENSE_EXPIRED,
    PACKAGING_ABNORMAL,
    TEMP_BREAK,
)
from food_parcel_regulation.timemodel import FakeClock


class PickupIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = build_world(self.clock)
        self.clock.advance(timedelta(days=1))

    def test_retransmission_same_payload_does_not_create_second_pickup(self):
        first = self.b.pickups.accept(observation("S-1"), "courier:k1")
        self.assertFalse(first["idempotent"])

        replay = self.b.pickups.accept(observation("S-1"), "courier:k1")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["parcel_id"], first["parcel_id"])

        pickups = [p for p in self.b.repo.all_of("pickup") if p.serial_no == "S-1"]
        self.assertEqual(len(pickups), 1)
        parcels = [p for p in self.b.repo.all_of("parcel") if p.serial_no == "S-1"]
        self.assertEqual(len(parcels), 1)

    def test_divergent_payload_same_serial_is_quarantined(self):
        first = self.b.pickups.accept(observation("S-2", observed_temp=5.0), "courier:k1")
        divergent = observation("S-2", observed_temp=9.0, packaging_ok=False)
        with self.assertRaises(QuarantineError):
            self.b.pickups.accept(divergent, "courier:k1")

        pickup = self.b.repo.get("S-2", "pickup")
        self.assertTrue(pickup.quarantined)
        self.assertTrue(pickup.accepted)
        # 隔离不产生第二个包裹，首次揽件仍然有效。
        self.assertEqual(len(self.b.repo.all_of("parcel")), 1)
        parcels = [p for p in self.b.repo.all_of("parcel") if p.serial_no == "S-2"]
        self.assertEqual(len(parcels), 1)

        # 隔离后任何同流水重传都继续被拒绝。
        with self.assertRaises(QuarantineError):
            self.b.pickups.accept(divergent, "courier:k1")

    def test_inspection_snapshot_keeps_only_seen_facts(self):
        result = self.b.pickups.accept(observation("S-3"), "courier:k1")
        events = self.b.store.events_for(result["parcel_id"])
        accepted = next(e for e in events if e.event_type == "ParcelAccepted")
        snap = accepted.payload["inspection_snapshot"]
        self.assertEqual(set(snap), {"license_digest", "goods_category",
                                     "cold_chain", "location", "observed_at"})
        self.assertEqual(snap["license_digest"], "digest-ok")
        self.assertEqual(snap["goods_category"], "冷藏熟食")
        self.assertEqual(snap["location"]["lat"], 30.0)
        self.assertTrue(snap["cold_chain"]["required"])
        # 不保存证照影像原件或客户隐私扩展字段。
        self.assertNotIn("license_image", accepted.payload)
        self.assertNotIn("id_card", accepted.payload)


class PickupFindingsTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = build_world(self.clock)
        self.clock.advance(timedelta(days=1))

    def _lead_types(self, result):
        return sorted(
            self.b.repo.get(lid, "lead").lead_type for lid in result["lead_ids"]
        )

    def _levels(self, result):
        return {
            self.b.repo.get(lid, "lead").lead_type:
                self.b.repo.get(lid, "lead").level
            for lid in result["lead_ids"]
        }

    def test_address_mismatch_at_unregistered_site(self):
        result = self.b.pickups.accept(
            observation("A-1", lat=UNREGISTERED_GEO[0], lng=UNREGISTERED_GEO[1],
                        observed_address="现场仓库X", temp_required=False),
            "courier:k1",
        )
        self.assertIn(ADDRESS_MISMATCH, self._lead_types(result))

    def test_registered_external_warehouse_is_not_mismatch(self):
        result = self.b.pickups.accept(
            observation("A-2", lat=WAREHOUSE_GEO[0], lng=WAREHOUSE_GEO[1],
                        observed_address="外设仓库B"),
            "courier:k1",
        )
        self.assertNotIn(ADDRESS_MISMATCH, self._lead_types(result))

    def test_expired_license_lead(self):
        t = self.clock.now()
        self.b.master_data.close_record("qualification", "qual-1", t, "regulator:r1")
        self.b.master_data.record_qualification(
            "qual-2", "c1", "LIC-002", "digest-new", "冷藏食品", t,
            "regulator:r1", license_expiry=t - timedelta(days=1),
        )
        result = self.b.pickups.accept(
            observation("A-3", license_digest="digest-new"), "courier:k1"
        )
        self.assertIn(LICENSE_EXPIRED, self._lead_types(result))

    def test_packaging_abnormal_lead(self):
        result = self.b.pickups.accept(
            observation("A-4", packaging_ok=False, packaging_note="箱体外漏"),
            "courier:k1",
        )
        self.assertIn(PACKAGING_ABNORMAL, self._lead_types(result))

    def test_temp_break_lead_at_pickup(self):
        result = self.b.pickups.accept(
            observation("A-5", observed_temp=12.5), "courier:k1"
        )
        self.assertIn(TEMP_BREAK, self._lead_types(result))
        lead = next(self.b.repo.get(lid, "lead")
                    for lid in result["lead_ids"]
                    if self.b.repo.get(lid, "lead").lead_type == TEMP_BREAK)
        self.assertEqual(lead.evidence["stage"], "pickup")

    def test_multiple_findings_form_separate_leads_with_levels(self):
        result = self.b.pickups.accept(
            observation("A-6", lat=UNREGISTERED_GEO[0], lng=UNREGISTERED_GEO[1],
                        packaging_ok=False, observed_temp=20.0),
            "courier:k1",
        )
        types = self._lead_types(result)
        self.assertEqual(types, sorted([ADDRESS_MISMATCH, PACKAGING_ABNORMAL, TEMP_BREAK]))
        levels = self._levels(result)
        # 2026.v1: 地址不符 info、包装异常 info、温控中断 info。
        self.assertEqual(set(levels.values()), {"info"})

    def test_historical_pickup_uses_rule_version_at_that_time(self):
        # 第 30 天升级规则：温控中断升为 major。
        t0 = self.clock.now() - timedelta(days=1)
        self.clock.set(t0 + timedelta(days=30))
        day30 = self.clock.now()
        self.b.master_data.close_record("rulebook", "2026.v1", day30, "regulator:r1")
        from world import LEVELS_V2, PARAMS
        self.b.master_data.publish_rulebook(
            "2026.v2", LEVELS_V2, day30, "regulator:r1", params=PARAMS
        )
        # 发生在第 5 天的揽件按 v1 判定（info）。
        old = self.b.pickups.accept(
            observation("A-7", observed_temp=20.0), "courier:k1",
            observed_at=t0 + timedelta(days=5),
        )
        old_levels = self._levels(old)
        self.assertEqual(old_levels[TEMP_BREAK], "info")
        # 现在的揽件按 v2 判定（major）。
        new = self.b.pickups.accept(
            observation("A-8", observed_temp=20.0), "courier:k1"
        )
        self.assertEqual(self._levels(new)[TEMP_BREAK], "major")


if __name__ == "__main__":
    unittest.main()
