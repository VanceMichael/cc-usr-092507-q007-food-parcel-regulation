"""时态主数据与规则版本测试。"""

import unittest
from datetime import timedelta

from world import LEVELS_V1, LEVELS_V2, PARAMS, WAREHOUSE_GEO, build_world

from food_parcel_regulation.errors import NotFoundError, RuleUnavailableError
from food_parcel_regulation.masterdata import ADDRESS_MISMATCH, TEMP_BREAK
from food_parcel_regulation.timemodel import FakeClock, Interval


class TemporalMasterDataTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = build_world(self.clock)
        self.t0 = self.clock.now()

    def test_half_open_interval_boundaries(self):
        iv = Interval(self.t0, self.t0 + timedelta(days=1))
        self.assertTrue(iv.contains(self.t0))
        self.assertTrue(iv.contains(self.t0 + timedelta(hours=23)))
        self.assertFalse(iv.contains(self.t0 + timedelta(days=1)))

    def test_queries_use_effective_interval(self):
        # 第 5 天：旧址与 v1 规则生效。
        day5 = self.t0 + timedelta(days=5)
        self.assertEqual(
            self.b.master.active_premises("c1", day5)[0].record_id, "premises-1"
        )
        self.assertEqual(self.b.master.active_code("VC-1", day5).code, "VC-1")
        self.assertEqual(self.b.master.active_rulebook(day5).version, "2026.v1")

        # 第 10 天：搬迁场所、停用旧码、换发新码，规则升级 v2。
        self.clock.set(self.t0 + timedelta(days=10))
        day10 = self.clock.now()
        self.b.master_data.close_record("premises", "premises-1", day10, "regulator:r1")
        self.b.master_data.record_place(
            "premises", "premises-2", "c1", "新经营地址C", (32.0, 122.0),
            day10, "regulator:r1", radius_m=500,
        )
        self.b.master_data.close_record("verification_code", "VC-1", day10, "regulator:r1")
        self.b.master_data.issue_code("VC-2", "c1", "新经营地址C", day10, "regulator:r1")
        self.b.master_data.close_record("rulebook", "2026.v1", day10, "regulator:r1")
        self.b.master_data.publish_rulebook(
            "2026.v2", LEVELS_V2, day10, "regulator:r1", params=PARAMS
        )

        # 历史时点仍看到旧址、旧码、v1。
        self.assertEqual(
            self.b.master.active_premises("c1", day5)[0].record_id, "premises-1"
        )
        self.assertEqual(self.b.master.active_code("VC-1", day5).customer_id, "c1")
        self.assertEqual(self.b.master.active_rulebook(day5).version, "2026.v1")
        # 新时点看到新址、新码、v2，旧码已不可用。
        self.assertEqual(
            self.b.master.active_premises("c1", day10)[0].record_id, "premises-2"
        )
        self.assertEqual(self.b.master.active_code("VC-2", day10).code, "VC-2")
        with self.assertRaises(NotFoundError):
            self.b.master.active_code("VC-1", day10)
        self.assertEqual(self.b.master.active_rulebook(day10).version, "2026.v2")

    def test_warehouse_is_separate_registered_location(self):
        place = self.b.master.location_registered("c1", WAREHOUSE_GEO, self.t0 + timedelta(days=1))
        self.assertIsNotNone(place)
        self.assertEqual(place.record_id, "warehouse-1")

    def test_no_rulebook_at_time_raises(self):
        with self.assertRaises(RuleUnavailableError):
            self.b.master.active_rulebook(self.t0 - timedelta(days=365))

    def test_qualification_expiry_is_point_in_time(self):
        qual = self.b.master.active_qualification("c1", self.t0 + timedelta(days=364))
        self.assertEqual(qual.license_digest, "digest-ok")
        # 资质记录的生效区间与证照本身有效期分开：记录仍在生效，
        # 但证照有效期已过——是否形成线索由揽件规则按 license_expiry 判定。
        still = self.b.master.active_qualification("c1", self.t0 + timedelta(days=400))
        self.assertLess(still.license_expiry, self.t0 + timedelta(days=400))

    def test_rulebook_levels_frozen_per_version(self):
        v1 = self.b.master.rulebook("2026.v1")
        self.assertEqual(v1.levels[ADDRESS_MISMATCH], "info")
        self.assertEqual(v1.params["temp_break_tolerance_min"], 120)


if __name__ == "__main__":
    unittest.main()
