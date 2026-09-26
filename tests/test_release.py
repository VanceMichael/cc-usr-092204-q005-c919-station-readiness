import unittest

from src.release import (
    REDACTED,
    ReleaseError,
    SilentDowngradeError,
    arbitrate,
    assess,
    dispatcher_view,
    frozen_version,
    load_release,
    redact_for_role,
    timeline,
)


def make_version(items, version_id="V-T", state="frozen"):
    return {"version_id": version_id, "state": state, "items": items}


def item(code, status="ready", mitigation=None):
    return {
        "code": code,
        "status": status,
        "owner": "示例单位",
        "responsible_person": f"责任人-{code}",
        "mitigation": mitigation,
        "sensitive_detail": None,
    }


READY_ITEMS = [item(code) for code in (
    "checkin", "fids", "jetbridge", "baggage",
    "maintenance", "parts", "personnel", "drill",
)]


class RuleEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = load_release()

    def test_r1_release_uses_frozen_version(self):
        version = frozen_version(self.packet)
        self.assertEqual(version["version_id"], "V-20260926-01")
        self.assertEqual(version["state"], "frozen")

        draft_packet = {**self.packet, "frozen_version_id": "V-20261025-01"}
        with self.assertRaises(ReleaseError):
            frozen_version(draft_packet)

    def test_r2_recomputation_keeps_full_history(self):
        history = [a["assessment_id"] for a in __import__("src.release", fromlist=["assessment_history"]).assessment_history(self.packet)]
        self.assertEqual(history, ["A-0600-01", "A-0750-02", "A-0805-03", "A-0840-04"])

    def test_r3_ticketed_silent_downgrade_rejected(self):
        items = [dict(i) for i in READY_ITEMS]
        items[3] = item("baggage", "degraded", {
            "measure": "人工分拣", "provider": "行李室", "approved_by": None,
        })
        with self.assertRaises(SilentDowngradeError):
            assess(make_version(items), ticketed=True)

        # 未售票航班允许无审批降级，但结论为 conditional
        result = assess(make_version(items), ticketed=False)
        self.assertEqual(result["decision"], "conditional")

        # 已售票且完成显式审批后可条件放行
        items[3]["mitigation"]["approved_by"] = "运行控制经理E"
        result = assess(make_version(items), ticketed=True)
        self.assertEqual(result["decision"], "conditional")

    def test_gap_without_mitigation_is_no_go(self):
        items = [dict(i) for i in READY_ITEMS]
        items[5] = item("parts", "absent", None)
        with self.assertRaises(SilentDowngradeError):
            assess(make_version(items), ticketed=True)
        result = assess(make_version(items), ticketed=False)
        self.assertEqual(result["decision"], "no_go")

    def test_r4_concurrent_confirmation_single_effective(self):
        confirmations = list(self.packet["confirmations"])
        forward = arbitrate(confirmations)
        # 打乱提交顺序，仲裁结论必须一致
        backward = arbitrate(list(reversed(confirmations)))
        self.assertEqual(
            [(c["confirmation_id"], c["status"]) for c in forward],
            [(c["confirmation_id"], c["status"]) for c in backward],
        )
        effective = [c for c in forward if c["status"] == "effective"]
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["confirmation_id"], "CF-0900-01")

    def test_r4_version_mismatch_is_conflict(self):
        confirmations = [
            {"confirmation_id": "C1", "version_id": "V1", "party": "airport",
             "decided_at": "2026-09-26T09:00:00+08:00", "proposed_decision": "go"},
            {"confirmation_id": "C2", "version_id": "V2", "party": "airline",
             "decided_at": "2026-09-26T09:00:00+08:00", "proposed_decision": "go"},
        ]
        self.assertTrue(all(c["status"] == "conflict" for c in arbitrate(confirmations)))

    def test_r5_sensitive_detail_role_isolation(self):
        version = frozen_version(self.packet)
        for role in ("dispatcher", "airport_ops"):
            masked = redact_for_role(version, role)
            detail = next(i for i in masked["items"] if i["code"] == "maintenance")
            self.assertEqual(detail["sensitive_detail"], REDACTED)
        full = redact_for_role(version, "maintenance")
        detail = next(i for i in full["items"] if i["code"] == "maintenance")
        self.assertIn("构型补丁", detail["sensitive_detail"]["content"])

    def test_r6_backfill_keeps_occurred_time(self):
        events = timeline(self.packet["events"])
        ids = [e["event_id"] for e in events]
        # 06:55 发生、10:32 才补录的事件按发生时间归位
        self.assertEqual(ids.index("E-0655"), 1)
        backfilled = next(e for e in events if e["event_id"] == "E-0655")
        self.assertTrue(backfilled["backfilled"])

        with self.assertRaises(ReleaseError):
            timeline([{
                "event_id": "E-BAD", "stream": "aircraft",
                "occurred_at": "2026-09-26T10:00:00+08:00",
                "recorded_at": "2026-09-26T09:00:00+08:00",
                "summary": "记录早于发生",
            }])

    def test_r6_stream_filter(self):
        self.assertEqual(
            {e["stream"] for e in timeline(self.packet["events"], "baggage")},
            {"baggage"},
        )

    def test_r7_dispatcher_sees_gaps_mitigation_and_owner(self):
        view = dispatcher_view(self.packet)
        self.assertEqual(view["decision"], "conditional")
        codes = {gap["code"] for gap in view["gaps"]}
        self.assertEqual(codes, {"baggage"})
        gap = view["gaps"][0]
        self.assertTrue(gap["mitigation"])
        self.assertTrue(gap["provider"])
        self.assertEqual(gap["responsible_person"], "行李主管D")


if __name__ == "__main__":
    unittest.main()
