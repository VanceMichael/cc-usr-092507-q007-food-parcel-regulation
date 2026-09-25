"""时态档案测试：资质、场所、仓库、核验码按生效区间查询。"""

import unittest
from datetime import datetime

from support import T0, build_service
from food_parcel_regulation.temporal import (
    Premise,
    Qualification,
    VerificationCode,
    Warehouse,
)
from food_parcel_regulation.errors import NotFoundError, ValidationError


class TemporalTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.cid = "K1"

    def test_as_of_picks_version_effective_at_moment(self):
        # 两张相邻资质：旧证 2025 年有效，2026-06-01 换新证。
        self.svc.register_qualification(Qualification(
            "Q1", self.cid, "OLD", "旧范围",
            datetime(2025, 1, 1), datetime(2026, 6, 1)))
        self.svc.register_qualification(Qualification(
            "Q2", self.cid, "NEW", "新范围", datetime(2026, 6, 1)))

        old = self.svc.profile(self.cid).qualifications.as_of(
            self.cid, datetime(2026, 5, 31))
        new = self.svc.profile(self.cid).qualifications.as_of(
            self.cid, datetime(2026, 6, 1))
        boundary = self.svc.profile(self.cid).qualifications.as_of(
            self.cid, datetime(2026, 6, 1, 0, 0, 1))

        self.assertEqual(old.license_no, "OLD")
        self.assertEqual(new.license_no, "NEW")
        self.assertEqual(boundary.license_no, "NEW")  # 半开区间末端归新版
        self.assertTrue(self.svc.profile(self.cid).qualifications.is_valid(
            self.cid, datetime(2026, 7, 1)))

    def test_overlapping_version_rejected(self):
        self.svc.register_qualification(Qualification(
            "Q1", self.cid, "A", "x", datetime(2025, 1, 1),
            datetime(2026, 6, 1)))
        with self.assertRaisesRegex(ValidationError, "重叠"):
            self.svc.register_qualification(Qualification(
                "Q2", self.cid, "B", "x", datetime(2026, 5, 1)))

    def test_no_effective_version_before_first_or_after_last(self):
        self.svc.register_qualification(Qualification(
            "Q1", self.cid, "A", "x", datetime(2026, 1, 1),
            datetime(2026, 6, 1)))
        with self.assertRaises(NotFoundError):
            self.svc.profile(self.cid).qualifications.as_of(
                self.cid, datetime(2025, 12, 31))
        with self.assertRaises(NotFoundError):
            self.svc.profile(self.cid).qualifications.as_of(
                self.cid, datetime(2026, 7, 1))

    def test_revoked_qualification_is_not_valid(self):
        self.svc.register_qualification(Qualification(
            "Q1", self.cid, "A", "x", datetime(2025, 1, 1),
            revoked=True))
        self.assertFalse(self.svc.profile(self.cid).qualifications.is_valid(
            self.cid, T0))

    def test_warehouse_and_premise_addresses_union(self):
        self.svc.register_premise(Premise(
            "P1", self.cid, "门店A", datetime(2025, 1, 1)))
        self.svc.register_warehouse(Warehouse(
            "W1", self.cid, "外设仓库B", datetime(2025, 6, 1)))
        # 仓库设立之前，已知地址只有门店
        self.assertEqual(
            self.svc.profile(self.cid).known_addresses_at(datetime(2025, 5, 1)),
            {"门店A"},
        )
        # 设立之后两者都在
        self.assertEqual(
            self.svc.profile(self.cid).known_addresses_at(T0),
            {"门店A", "外设仓库B"},
        )

    def test_verification_code_versions(self):
        # 旧码绑定旧地址，2026-03-01 换码绑定新地址
        self.svc.register_code(VerificationCode(
            "CODE", self.cid, "旧址", datetime(2025, 1, 1),
            datetime(2026, 3, 1)))
        self.svc.register_code(VerificationCode(
            "CODE", self.cid, "新址", datetime(2026, 3, 1)))
        self.assertEqual(
            self.svc.profile(self.cid).codes.as_of(
                "CODE", datetime(2026, 2, 1)).registered_address,
            "旧址",
        )
        self.assertEqual(
            self.svc.profile(self.cid).codes.as_of("CODE", T0).registered_address,
            "新址",
        )

    def test_far_future_default_open_ended(self):
        self.svc.register_premise(Premise(
            "P1", self.cid, "A", datetime(2025, 1, 1)))
        # 默认开放区间一直有效到远端（不含末端本身）
        p = self.svc.profile(self.cid).premises.as_of(
            self.cid, datetime(9999, 6, 1))
        self.assertEqual(p.address, "A")


if __name__ == "__main__":
    unittest.main()
