import unittest

from src import release


class ReleaseModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.capability = release.load_json("capability.json")
        self.ops = release.load_json("operations.json")
        self.flight = release.get_flight(self.ops, "CZ2381")

    def evaluate(self, at: str) -> dict:
        return release.evaluate_release(self.flight, self.capability, self.ops, at)

    def gap(self, result: dict, condition: str):
        for gap in result["gaps"]:
            if gap["condition"] == condition:
                return gap
        return None

    def test_frozen_capability_version_is_used_for_the_day(self) -> None:
        # 当日 00:00 已发布加强版本 002，但当日航班仍按冻结的 001 评估：
        # 人员缺口阈值是 001 的 2 人，而不是 002 的 3 人。
        result = self.evaluate("2026-09-26T05:20:00+08:00")
        self.assertEqual(result["frozen_version_id"], "WCV-CKG-C919-2026Q3-001")
        personnel = self.gap(result, "personnel")
        self.assertIsNotNone(personnel)
        self.assertIn("授权机务2人", personnel["commitment"])

    def test_personnel_shortfall_is_nogo_before_reinforcement_plan(self) -> None:
        result = self.evaluate("2026-09-26T05:20:00+08:00")
        self.assertEqual(result["decision"], "NOGO")
        personnel = self.gap(result, "personnel")
        self.assertEqual(personnel["status"], "GAP")
        self.assertEqual(personnel["owner"], "机务班组长")
        # 联动拖带：人员失位连带复核备件与演练
        linked = {g["condition"] for g in result["gaps"] if g["status"] == "LINKED_GAP"}
        self.assertIn("spares", linked)
        self.assertIn("drill", linked)

    def test_recompute_reason_tracks_latest_trigger(self) -> None:
        self.assertEqual(
            self.evaluate("2026-09-26T05:20:00+08:00")["recompute_reason"],
            "人员调班",
        )
        self.assertEqual(
            self.evaluate("2026-09-26T07:20:00+08:00")["recompute_reason"],
            "临时机位变化",
        )

    def test_offline_backfill_uses_real_occurred_time(self) -> None:
        # 补录事件 recorded_at=08:20，但 occurred_at=05:50；
        # 在 06:00 评估时，增援计划（08:35 到场）必须已被计入，结论为 DELAY 而非 NOGO。
        result = self.evaluate("2026-09-26T06:00:00+08:00")
        personnel = self.gap(result, "personnel")
        self.assertIsNotNone(personnel)
        self.assertEqual(personnel["recovery_at"], "2026-09-26T08:35:00+08:00")
        self.assertEqual(result["decision"], "DELAY")

    def test_personnel_gap_recovers_when_reinforcement_arrives(self) -> None:
        before = self.evaluate("2026-09-26T08:30:00+08:00")
        self.assertIsNotNone(self.gap(before, "personnel"))
        after = self.evaluate("2026-09-26T08:35:00+08:00")
        self.assertIsNone(self.gap(after, "personnel"))

    def test_sold_flight_cannot_silently_accept_remote_stand_downgrade(self) -> None:
        # 10:30 天气好转、10:10 转借工具到场，仅剩廊桥：207 故障改 211 远机位，
        # 客梯车方案低于"廊桥"冻结承诺且无恢复路径 → NOGO，静默降级被拦截。
        result = self.evaluate("2026-09-26T10:30:00+08:00")
        bridge = self.gap(result, "bridge")
        self.assertIsNotNone(bridge)
        self.assertIsNone(bridge["recovery_at"])
        self.assertTrue(result["silent_downgrade_blocked"])
        self.assertEqual(result["decision"], "NOGO")
        # 签派员仍能看到替代措施与最后责任人
        self.assertTrue(any("远机位" in w for w in result["workarounds"]))
        self.assertIn("机坪管制室", result["last_owners"])

    def test_release_go_after_bridge_restored_to_frozen_standard(self) -> None:
        result = self.evaluate("2026-09-26T11:20:00+08:00")
        self.assertEqual(result["decision"], "GO")
        self.assertEqual(result["gaps"], [])
        self.assertFalse(result["silent_downgrade_blocked"])

    def test_concurrent_confirmations_yield_single_effective_decision(self) -> None:
        winner = release.effective_confirmation(self.ops, "CZ2381")
        self.assertEqual(winner["confirmation_id"], "CONF-A-001")
        self.assertEqual(winner["party"], "机场")
        self.assertIn("CONF-H-001", winner["reason"])

    def test_confirmation_tie_broken_by_received_sequence(self) -> None:
        ops = {
            "confirmations": [
                {"confirmation_id": "X", "party": "航司", "flight_no": "F1",
                 "decision": "CONFIRMED", "occurred_at": "2026-09-26T07:10:00+08:00",
                 "received_seq": 202},
                {"confirmation_id": "Y", "party": "机场", "flight_no": "F1",
                 "decision": "CONFIRMED", "occurred_at": "2026-09-26T07:10:00+08:00",
                 "received_seq": 201},
            ]
        }
        winner = release.effective_confirmation(ops, "F1")
        self.assertEqual(winner["confirmation_id"], "Y")

    def test_maintenance_document_isolated_by_role(self) -> None:
        denied = release.view_maintenance_document(self.ops, "MRO-C919-2609-17", "签派员")
        self.assertEqual(denied["access"], "DENIED")
        self.assertNotIn("content", denied)
        self.assertEqual(denied["status"], "RESTRICTED")

        full = release.view_maintenance_document(self.ops, "MRO-C919-2609-17", "机务维修岗")
        self.assertEqual(full["access"], "FULL")
        self.assertIn("受限正文", full["content"])

    def test_replay_orders_by_real_time_and_covers_three_streams(self) -> None:
        report = release.replay(self.flight, self.capability, self.ops)
        self.assertEqual(len(report["streams"]["aircraft"]), 6)
        self.assertEqual(len(report["streams"]["passenger"]), 1)
        self.assertEqual(len(report["streams"]["baggage"]), 1)
        # 补录事件按真实发生时间落在 05:50，而非接收时间 08:20
        backfill = next(
            item for item in report["timeline"]
            if item.get("type") == "断网补录"
        )
        self.assertEqual(backfill["occurred_at"], "2026-09-26T05:50:00+08:00")
        self.assertTrue(backfill["offline_backfill"])
        timeline_times = [item["occurred_at"] for item in report["timeline"]]
        self.assertEqual(timeline_times, sorted(timeline_times))
        # 决策结论随时间演进：NOGO → DELAY → NOGO → GO
        decisions = [e["decision"] for e in report["evaluations"]]
        self.assertEqual(decisions[0], "NOGO")
        self.assertIn("DELAY", decisions)
        self.assertEqual(decisions[-1], "GO")


if __name__ == "__main__":
    unittest.main()
