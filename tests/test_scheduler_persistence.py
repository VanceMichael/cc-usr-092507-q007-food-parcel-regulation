"""到期事项调度与重启恢复测试。"""

import os
import tempfile
import unittest
from datetime import timedelta

from world import build_world, observation

from food_parcel_regulation.backend import Backend
from food_parcel_regulation.timemodel import FakeClock


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.b = build_world(self.clock)
        self.clock.advance(timedelta(days=1))

    def test_temp_check_receipt_due_items(self):
        result = self.b.pickups.accept(observation("D-1"), "courier:k1")
        parcel_id = result["parcel_id"]
        lead_id = self.b.parcels.report_temp_interruption(
            parcel_id, "courier:k1", 30, "冷机故障"
        )
        # 受理核查，产生 48 小时待核查时限。
        self.b.leads.accept_for_check(lead_id, "regulator:r2")
        # 发起移交，产生 24 小时回执时限。
        handover_id = self.b.handovers.initiate(lead_id, "postal:o1", [parcel_id])

        # 温控容忍 120 分钟内：没有到期事项。
        self.clock.advance(timedelta(minutes=100))
        self.assertEqual(self.b.scheduler.due_items(), [])

        # 超过 120 分钟：温控时限到期。
        self.clock.advance(timedelta(minutes=30))
        kinds = {item["kind"] for item in self.b.scheduler.due_items()}
        self.assertIn("temp_deadline", kinds)
        self.assertNotIn("receipt_reminder", kinds)
        self.assertNotIn("check_due", kinds)

        # 24 小时后：回执催办到期；温控仍未恢复，继续挂账。
        self.clock.advance(timedelta(hours=23))
        kinds = {item["kind"] for item in self.b.scheduler.due_items()}
        self.assertEqual(kinds, {"temp_deadline", "receipt_reminder"})

        # 恢复温控后，温控事项消失。
        self.b.parcels.resolve_temp_interruption(parcel_id, lead_id, "courier:k1")
        kinds = {item["kind"] for item in self.b.scheduler.due_items()}
        self.assertNotIn("temp_deadline", kinds)

        # 回执后催办消失。
        self.b.handovers.receive(handover_id, "regulator:r2")
        kinds = {item["kind"] for item in self.b.scheduler.due_items()}
        self.assertNotIn("receipt_reminder", kinds)

        # 48 小时后：待核查事项到期。
        self.clock.advance(timedelta(hours=25))
        kinds = {item["kind"] for item in self.b.scheduler.due_items()}
        self.assertIn("check_due", kinds)

        # 立案/放行后待核查事项消失。
        self.b.leads.file_case(lead_id, "regulator:r2")
        self.assertEqual(self.b.scheduler.due_items(), [])


class RestartRecoveryTest(unittest.TestCase):
    def test_state_and_due_items_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "events.jsonl")
            clock = FakeClock()
            backend = build_world(clock, path=path)
            clock.advance(timedelta(days=1))
            result = backend.pickups.accept(observation("R-1"), "courier:k1")
            parcel_id = result["parcel_id"]
            lead_id = backend.parcels.report_temp_interruption(
                parcel_id, "courier:k1", 30
            )
            backend.leads.accept_for_check(lead_id, "regulator:r2")
            handover_id = backend.handovers.initiate(
                lead_id, "postal:o1", [parcel_id]
            )
            # 推进到所有时限都已过期，再重启。
            clock.advance(timedelta(hours=49))

            reopened = Backend.reopen(path, clock=clock)
            kinds = {item["kind"] for item in reopened.scheduler.due_items()}
            self.assertEqual(
                kinds, {"temp_deadline", "receipt_reminder", "check_due"}
            )

            # 聚合状态完整恢复：揽件幂等台账仍在，重传不会生成第二次揽件。
            replay = reopened.pickups.accept(observation("R-1"), "courier:k1")
            self.assertTrue(replay["idempotent"])
            self.assertEqual(replay["parcel_id"], parcel_id)

            # 主数据时态查询同样恢复。
            self.assertEqual(
                reopened.master.active_rulebook(clock.now()).version, "2026.v1"
            )
            # 线索与移交处于中断时的状态，处置可继续。
            self.assertEqual(reopened.repo.get(lead_id, "lead").status, "checking")
            self.assertEqual(
                reopened.repo.get(handover_id, "handover").status, "initiated"
            )
            reopened.handovers.receive(handover_id, "regulator:r2")
            self.assertEqual(
                reopened.repo.get(handover_id, "handover").status, "received"
            )

    def test_event_log_is_human_readable_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "events.jsonl")
            clock = FakeClock()
            backend = build_world(clock, path=path)
            clock.advance(timedelta(days=1))
            backend.pickups.accept(observation("J-1"), "courier:k1")
            import json
            with open(path, encoding="utf-8") as fh:
                lines = [ln for ln in fh if ln.strip()]
            self.assertGreaterEqual(len(lines), 1)
            # 每行是一个原子批次（至少一个事件），均可独立解析。
            for line in lines:
                batch = json.loads(line)
                self.assertIsInstance(batch, list)
                self.assertGreaterEqual(len(batch), 1)
                self.assertIn("event_type", batch[0])


if __name__ == "__main__":
    unittest.main()
