"""跨部门移交、并发一致性、投诉链路、时间线与重启恢复测试。"""

import os
import tempfile
import unittest
from datetime import timedelta

from support import T0, build_service, courier, postal, regulator, seed_clean_customer, snapshot
from food_parcel_regulation.clues import ClueStatus
from food_parcel_regulation.complaints import ComplaintChain, ComplaintStatus
from food_parcel_regulation.errors import ConflictError, NotFoundError
from food_parcel_regulation.parcels import ParcelStatus
from food_parcel_regulation.rules import RuleType


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_clean_customer(self.svc)
        self.courier = courier()
        self.regulator = regulator()
        self.post = postal()
        self.result = self.svc.report_pickup(
            self.courier, serial="S1", branch_id="B1", customer_id="K1",
            snapshot=snapshot(address="陌生仓库X"),
            tracking_no="T-S1", batch_id="BAT1",
            verification_code="CODE1", observed_at=T0)
        self.clue = next(c for c in self.result.clues
                         if c.rule_type is RuleType.ADDRESS_MISMATCH)

    def test_transfer_then_receipt_and_reminders(self):
        self.svc.review_clue(self.regulator, self.clue.clue_id, at=T0)
        handoff = self.svc.transfer_clue(
            self.post, self.clue.clue_id, to_org="market",
            at=T0 + timedelta(hours=1),
            receipt_due=T0 + timedelta(hours=2))
        self.assertEqual(self.svc.clues.get(self.clue.clue_id).status,
                         ClueStatus.TRANSFERRED)
        # 到期未签收：催办
        self.svc.tick(T0 + timedelta(hours=2, minutes=1))
        self.assertEqual(self.svc.handoffs.get(handoff.handoff_id).reminders_sent, 1)
        # 接收方签收（监管组织即接收方组织）后不再催办
        self.svc.acknowledge_handoff(
            self.regulator, handoff.handoff_id, at=T0 + timedelta(hours=3))
        self.svc.tick(T0 + timedelta(days=2))
        self.assertEqual(self.svc.handoffs.get(handoff.handoff_id).reminders_sent, 1)

    def test_transfer_and_scan_cannot_overwrite_each_other(self):
        # 监管受理后，线索版本为 1；移交使其变成 2。
        self.svc.review_clue(self.regulator, self.clue.clue_id, at=T0)
        self.svc.transfer_clue(
            self.post, self.clue.clue_id, to_org="agriculture",
            at=T0 + timedelta(hours=1))
        # 与移交“同时”到达的旧版本立案决定（基于 version=0 或 1）必须失败
        with self.assertRaises(ConflictError):
            self.svc.file_case(
                self.regulator, self.clue.clue_id,
                at=T0 + timedelta(hours=1), expected_version=0)
        # 移交状态没有被旧决定覆盖
        self.assertEqual(self.svc.clues.get(self.clue.clue_id).status,
                         ClueStatus.TRANSFERRED)

    def test_freeze_and_scan_mutex_under_concurrent_handoff(self):
        # 包裹先发出在途；立案冻结
        self.svc.dispatch(self.courier, "T-S1", at=T0 + timedelta(hours=1))
        self.svc.scan(self.courier, "T-S1", node="中心", kind="transit",
                      at=T0 + timedelta(hours=2))
        # 另一监管接管后立案冻结
        self.svc.file_case(self.regulator, self.clue.clue_id,
                           at=T0 + timedelta(hours=3))
        parcel = self.svc.parcels.get("T-S1")
        self.assertEqual(parcel.status, ParcelStatus.FROZEN)
        # 与立案“同时”在途的扫描必须被拒绝，绝不并存
        with self.assertRaises(ConflictError):
            self.svc.scan(self.courier, "T-S1", node="迟到的中心",
                          kind="transit", at=T0 + timedelta(hours=3),
                          expected_seq=parcel.status_seq - 1)

    def test_full_timeline_visible_to_both_parties(self):
        self.svc.transfer_clue(
            self.post, self.clue.clue_id, to_org="market",
            at=T0 + timedelta(hours=1), receipt_due=T0 + timedelta(hours=2))
        post_view = self.svc.view_timeline(self.post)
        mkt_view = self.svc.view_timeline(self.regulator)
        self.assertTrue(any("移交" in e.summary for e in post_view))
        self.assertTrue(any("移交" in e.summary for e in mkt_view))
        # 时间线条目只增：长度与按时间排序一致
        times = [e.at for e in post_view]
        self.assertEqual(times, sorted(times))


class ComplaintTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_clean_customer(self.svc)
        self.courier = courier()
        self.svc.report_pickup(
            self.courier, serial="S1", branch_id="B1", customer_id="K1",
            snapshot=snapshot(), tracking_no="T-S1", batch_id="BAT1",
            verification_code="CODE1", observed_at=T0)

    def test_complaint_attaches_to_existing_parcel_chain(self):
        complaint = self.svc.open_complaint(
            "K1", chain=ComplaintChain.PARCEL, anchor="T-S1",
            summary="温控担忧", at=T0)
        self.assertEqual(complaint.anchor, "T-S1")
        answered = self.svc.answer_complaint(
            self.courier, complaint.complaint_id, answer="已核实", at=T0)
        self.assertEqual(answered.status, ComplaintStatus.ANSWERED)

    def test_complaint_cannot_attach_to_missing_chain(self):
        with self.assertRaises(NotFoundError):
            self.svc.open_complaint(
                "K1", chain=ComplaintChain.PARCEL, anchor="NOPE",
                summary="悬空投诉", at=T0)

    def test_complaint_does_not_close_clue_or_move_parcel(self):
        result = self.svc.report_pickup(
            self.courier, serial="S2", branch_id="B1", customer_id="K1",
            snapshot=snapshot(address="陌生仓库Y"), tracking_no="T-S2",
            batch_id="BAT1", verification_code="CODE1", observed_at=T0)
        clue = next(c for c in result.clues
                    if c.rule_type is RuleType.ADDRESS_MISMATCH)
        complaint = self.svc.open_complaint(
            "K1", chain=ComplaintChain.CLUE, anchor=clue.clue_id,
            summary="异议", at=T0)
        self.svc.answer_complaint(self.courier, complaint.complaint_id,
                                  answer="答复", at=T0)
        # 投诉办结不改变线索状态
        self.assertEqual(self.svc.clues.get(clue.clue_id).status,
                         ClueStatus.REPORTED)


class RestartTest(unittest.TestCase):
    def test_state_and_pending_jobs_survive_restart(self):
        svc = build_service()
        seed_clean_customer(svc)
        actor = courier()
        # 揽收时温控已中断，挂有时限任务
        svc.report_pickup(
            actor, serial="S1", branch_id="B1", customer_id="K1",
            snapshot=snapshot(chain_ok=False, temp=10.0),
            tracking_no="T-S1", batch_id="BAT1",
            verification_code="CODE1", observed_at=T0)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            svc.save(path)
            restored = type(svc).load(path)
            pending = restored.scheduler.pending()
            self.assertTrue(any(j.ref_id == "T-S1" for j in pending))
            # 重启后温控时限仍然到点升级
            escalated = restored.tick(T0 + timedelta(minutes=31))
            self.assertEqual(len(escalated), 1)
            self.assertEqual(escalated[0].tracking_no, "T-S1")
            # 冷链计时状态保留
            self.assertIsNotNone(
                restored.parcels.get("T-S1").cold_chain.interrupted_since)

    def test_review_and_receipt_jobs_survive_restart(self):
        from food_parcel_regulation.scheduler import JobKind
        svc = build_service()
        seed_clean_customer(svc)
        co, reg, post = courier(), regulator(), postal()
        result = svc.report_pickup(
            co, serial="S1", branch_id="B1", customer_id="K1",
            snapshot=snapshot(address="陌生仓库X"), tracking_no="T-S1",
            batch_id="BAT1", verification_code="CODE1", observed_at=T0)
        clue = next(c for c in result.clues
                    if c.rule_type is RuleType.ADDRESS_MISMATCH)
        svc.review_clue(reg, clue.clue_id, at=T0,
                        due=T0 + timedelta(hours=8))
        handoff = svc.transfer_clue(
            post, clue.clue_id, to_org="market", at=T0 + timedelta(hours=1),
            receipt_due=T0 + timedelta(hours=2))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            svc.save(path)
            restored = type(svc).load(path)
            kinds = {j.kind for j in restored.scheduler.pending()}
            self.assertIn(JobKind.REVIEW_DUE, kinds)
            self.assertIn(JobKind.RECEIPT_REMINDER, kinds)
            # 重启后 tick 仍会催办并标记逾期
            restored.tick(T0 + timedelta(hours=2, minutes=1))
            self.assertEqual(
                restored.handoffs.get(handoff.handoff_id).reminders_sent, 1)

    def test_responsibilities_are_role_scoped(self):
        svc = build_service()
        seed_clean_customer(svc)
        co = courier()
        reg = regulator()
        post = postal()
        svc.report_pickup(
            co, serial="S1", branch_id="B1", customer_id="K1",
            snapshot=snapshot(address="陌生仓库X"), tracking_no="T-S1",
            batch_id="BAT1", verification_code="CODE1", observed_at=T0)
        reg_duties = svc.view_responsibilities(reg)
        co_duties = svc.view_responsibilities(co)
        self.assertIn("pending_clues", reg_duties)
        self.assertIn("pausable_unshipped", co_duties)
        self.assertIn("T-S1", co_duties["pausable_unshipped"])
        # 快递员看不到监管待核查队列
        self.assertNotIn("pending_clues", co_duties)
        self.assertIn("awaiting_receipts", svc.view_responsibilities(post))


if __name__ == "__main__":
    unittest.main()
