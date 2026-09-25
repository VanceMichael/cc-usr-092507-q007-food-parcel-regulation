"""线索分级与规则版本测试。"""

import unittest
from datetime import datetime, timedelta

from support import T0, build_service, courier, regulator, seed_clean_customer, snapshot
from food_parcel_regulation.clues import ClueStatus
from food_parcel_regulation.errors import (
    ConflictError,
    PermissionDeniedError,
)
from food_parcel_regulation.pickup import TempRequirement
from food_parcel_regulation.rules import ClueLevel, RuleType, RuleVersion


class ClueRulesTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_clean_customer(self.svc, warehouses=("外设仓库B",))
        self.courier = courier()
        self.regulator = regulator()

    def _pickup_with(self, snap, serial="S1"):
        return self.svc.report_pickup(
            self.courier, serial=serial, branch_id="B1", customer_id="K1",
            snapshot=snap, tracking_no=f"T-{serial}", batch_id="BAT1",
            verification_code="CODE1", observed_at=T0)

    def test_address_mismatch_generates_medium_clue(self):
        # 现场是未登记仓库，码面是登记地址 A
        result = self._pickup_with(snapshot(address="来路不明仓库Z"))
        types = {c.rule_type for c in result.clues}
        self.assertIn(RuleType.ADDRESS_MISMATCH, types)
        clue = next(c for c in result.clues
                    if c.rule_type is RuleType.ADDRESS_MISMATCH)
        self.assertEqual(clue.level, ClueLevel.MEDIUM)
        self.assertEqual(clue.rule_version, "2026.1")
        self.assertEqual(clue.status, ClueStatus.REPORTED)

    def test_warehouse_site_is_known_address_no_clue(self):
        # 码面 A，现场在外设仓库 B，B 已登记 -> 不算地址不符
        seed_clean_customer.__wrapped__ if False else None
        result = self._pickup_with(snapshot(address="外设仓库B"))
        self.assertFalse(any(
            c.rule_type is RuleType.ADDRESS_MISMATCH for c in result.clues))

    def test_license_expired_uses_temporal_registry(self):
        # 情况一：时态档案仍有效，仅快照证面日期偏旧，以档案为准，不报过期
        result = self._pickup_with(snapshot(
            address="登记地址A", expiry=datetime(2026, 8, 1)))
        self.assertFalse(any(
            c.rule_type is RuleType.LICENSE_EXPIRED for c in result.clues))

        # 情况二：档案资质版本已过期，产生 HIGH 线索（全新客户演示）
        from food_parcel_regulation.temporal import (
            Premise, Qualification, VerificationCode)
        self.svc.register_qualification(Qualification(
            "QX", "K2", "SP999", "冷藏食品",
            datetime(2024, 1, 1), datetime(2026, 8, 1)))
        self.svc.register_premise(Premise(
            "PX", "K2", "地址A", datetime(2024, 1, 1)))
        self.svc.register_code(VerificationCode(
            "CODE2", "K2", "地址A", datetime(2024, 1, 1)))
        expired = self.svc.report_pickup(
            self.courier, serial="S2", branch_id="B1", customer_id="K2",
            snapshot=snapshot(address="地址A"),
            tracking_no="T-S2", batch_id="BAT2",
            verification_code="CODE2", observed_at=T0)
        clue = next(c for c in expired.clues
                    if c.rule_type is RuleType.LICENSE_EXPIRED)
        self.assertEqual(clue.level, ClueLevel.HIGH)

    def test_packaging_abnormal_low_clue(self):
        result = self._pickup_with(snapshot(
            address="登记地址A", packaging_ok=False, remarks="外箱破损"))
        clue = next(c for c in result.clues
                    if c.rule_type is RuleType.PACKAGING_ABNORMAL)
        self.assertEqual(clue.level, ClueLevel.LOW)
        self.assertIn("外箱破损", clue.evidence)

    def test_temp_interruption_level_by_duration_version(self):
        result = self._pickup_with(snapshot(
            address="登记地址A", requirement=TempRequirement.REFRIGERATED,
            chain_ok=False, temp=12.0))
        clue = next(c for c in result.clues
                    if c.rule_type is RuleType.TEMP_INTERRUPTION)
        # 揽收当时中断按 0 秒落最低档
        self.assertEqual(clue.level, ClueLevel.INFO)
        # 30 分钟后到点升级为 MEDIUM
        escalated = self.svc.tick(T0 + timedelta(minutes=30, seconds=1))
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0].level, ClueLevel.MEDIUM)
        # 60 分钟不会再生成（一次性时限事项）
        again = self.svc.tick(T0 + timedelta(minutes=61))
        self.assertEqual(again, [])

    def test_new_rule_version_does_not_retroactively_change_old_clue(self):
        # 先在旧版本下产生包装线索（LOW）
        result = self._pickup_with(
            snapshot(address="登记地址A", packaging_ok=False), serial="S1")
        old = next(c for c in result.clues
                   if c.rule_type is RuleType.PACKAGING_ABNORMAL)
        self.assertEqual(old.level, ClueLevel.LOW)
        # 之后发布更严的新版本 HIGH（2026-02-01 起生效，晚于旧版）
        self.svc.publish_rule(RuleVersion(
            RuleType.PACKAGING_ABNORMAL, "2026.2", datetime(2026, 2, 1),
            ClueLevel.HIGH))
        # 历史线索级别保持旧版本
        self.assertEqual(self.svc.clues.get(old.clue_id).level, ClueLevel.LOW)
        self.assertEqual(self.svc.clues.get(old.clue_id).rule_version, "2026.1")
        # 新发生的异常按新版本定级
        later = self._pickup_with(
            snapshot(address="外设仓库B", packaging_ok=False), serial="S2")
        new_clue = next(c for c in later.clues
                        if c.rule_type is RuleType.PACKAGING_ABNORMAL)
        self.assertEqual(new_clue.level, ClueLevel.HIGH)
        self.assertEqual(new_clue.rule_version, "2026.2")


class ClueLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_clean_customer(self.svc)
        self.courier = courier()
        self.regulator = regulator()
        result = self.svc.report_pickup(
            self.courier, serial="S1", branch_id="B1", customer_id="K1",
            snapshot=snapshot(address="陌生仓库X"),
            tracking_no="T-S1", batch_id="BAT1",
            verification_code="CODE1", observed_at=T0)
        self.clue = next(c for c in result.clues
                         if c.rule_type is RuleType.ADDRESS_MISMATCH)

    def test_reporter_cannot_close_own_clue(self):
        with self.assertRaises(PermissionDeniedError):
            self.svc.clues.close(self.clue.clue_id, actor=self.courier, at=T0)

    def test_courier_cannot_decide_review_or_file(self):
        with self.assertRaises(PermissionDeniedError):
            self.svc.review_clue(self.courier, self.clue.clue_id, at=T0)
        with self.assertRaises(PermissionDeniedError):
            self.svc.file_case(self.courier, self.clue.clue_id, at=T0)

    def test_regulator_independently_reviews_files(self):
        self.svc.review_clue(self.regulator, self.clue.clue_id, at=T0)
        self.assertEqual(self.svc.clues.get(self.clue.clue_id).status,
                         ClueStatus.UNDER_REVIEW)
        self.svc.file_case(self.regulator, self.clue.clue_id,
                           at=T0 + timedelta(hours=1))
        self.assertEqual(self.svc.clues.get(self.clue.clue_id).status,
                         ClueStatus.CASE_FILED)

    def test_independent_regulator_not_reporter_can_release(self):
        # 另一监管（非上报人）可以放行
        other = regulator("R2", "监管员丁")
        self.svc.release_clue(other, self.clue.clue_id, at=T0)
        self.assertEqual(self.svc.clues.get(self.clue.clue_id).status,
                         ClueStatus.RELEASED)

    def test_optimistic_version_blocks_stale_decision(self):
        self.svc.review_clue(self.regulator, self.clue.clue_id, at=T0)
        # version 已推进；仍基于 version=0 的立案必须失败
        with self.assertRaises(ConflictError):
            self.svc.file_case(self.regulator, self.clue.clue_id,
                               at=T0 + timedelta(hours=1), expected_version=0)

    def test_terminal_clue_cannot_transition_again(self):
        self.svc.release_clue(self.regulator, self.clue.clue_id, at=T0)
        with self.assertRaises(ConflictError):
            self.svc.file_case(self.regulator, self.clue.clue_id,
                               at=T0 + timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
