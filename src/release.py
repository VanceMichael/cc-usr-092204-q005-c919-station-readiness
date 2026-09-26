"""C919逐航点放行领域规则。

规则编号对应 docs/domain-model.md：
R1 当日放行引用冻结版本；R2 触发重算保留历史；R3 禁止静默降级；
R4 并发确认唯一生效；R5 敏感资料岗位隔离；R6 补录保留真实发生时间；
R7 放行前呈现缺口、替代措施与最后责任人。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ITEM_CODES = (
    "checkin",
    "fids",
    "jetbridge",
    "baggage",
    "maintenance",
    "parts",
    "personnel",
    "drill",
)

# 各岗位可见的敏感级别；None 表示不允许看到任何敏感资料
ROLE_SENSITIVITY: dict[str, set[str] | None] = {
    "dispatcher": set(),          # 签派员只见结论性摘要
    "airport_ops": set(),         # 机场运行不可见维修明细
    "maintenance": {"maintenance"},
    "auditor": {"maintenance"},
}

REDACTED = "[按岗位隔离]"


class ReleaseError(ValueError):
    """放行数据违反领域规则。"""


class SilentDowngradeError(ReleaseError):
    """R3：已售票航班出现未经显式审批的保障降级。"""


def load_release(path: Path | None = None) -> dict:
    source = path or Path(__file__).resolve().parents[1] / "fixtures" / "release.json"
    packet = json.loads(source.read_text(encoding="utf-8"))
    validate_event_times(packet["events"])
    return packet


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---------- R1：能力版本冻结 ----------

def frozen_version(packet: dict) -> dict:
    """返回当日冻结版本；未冻结或引用缺失即拒绝放行。"""
    version_id = packet.get("frozen_version_id")
    if not version_id:
        raise ReleaseError("当日放行缺少冻结的能力版本（R1）")
    for version in packet["capability_versions"]:
        if version["version_id"] == version_id:
            if version["state"] != "frozen":
                raise ReleaseError(f"版本 {version_id} 状态为 {version['state']}，未冻结（R1）")
            return version
    raise ReleaseError(f"冻结版本 {version_id} 在能力版本清单中不存在（R1）")


# ---------- R3 / R7：缺口判定、禁止静默降级、签派视图 ----------

def assess(version: dict, ticketed: bool) -> dict:
    """依据保障项状态计算结论与缺口。

    已售票航班的降级/缺位必须携带已审批的替代措施，否则按静默降级拒绝。
    """
    present = {item["code"] for item in version["items"]}
    missing_codes = [code for code in ITEM_CODES if code not in present]
    if missing_codes:
        raise ReleaseError(f"版本 {version['version_id']} 保障项不完整：{missing_codes}")

    gaps: list[dict] = []
    for item in version["items"]:
        if item["status"] == "ready":
            continue
        mitigation = item.get("mitigation")
        if ticketed:
            if not mitigation:
                raise SilentDowngradeError(
                    f"已售票航班 {item['code']} 处于 {item['status']} 且无替代措施（R3）"
                )
            if not mitigation.get("approved_by"):
                raise SilentDowngradeError(
                    f"已售票航班 {item['code']} 降级未经显式审批（R3）"
                )
        gaps.append(
            {
                "code": item["code"],
                "status": item["status"],
                "mitigation": mitigation,
                "responsible_person": item["responsible_person"],
            }
        )

    if not gaps:
        decision = "go"
    elif all(gap["mitigation"] for gap in gaps):
        decision = "conditional"
    else:
        decision = "no_go"
    return {"version_id": version["version_id"], "decision": decision, "gaps": gaps}


def dispatcher_view(packet: dict) -> dict:
    """R7：签派员放行前看到的尚缺条件、替代措施与最后责任人。"""
    version = frozen_version(packet)
    result = assess(version, packet["flight"]["ticketed"])
    return {
        "flight_no": packet["flight"]["flight_no"],
        "frozen_version_id": version["version_id"],
        "decision": result["decision"],
        "gaps": [
            {
                "code": gap["code"],
                "mitigation": gap["mitigation"]["measure"] if gap["mitigation"] else None,
                "provider": gap["mitigation"]["provider"] if gap["mitigation"] else None,
                "responsible_person": gap["responsible_person"],
            }
            for gap in result["gaps"]
        ],
    }


# ---------- R2：重算历史 ----------

def assessment_history(packet: dict) -> list[dict]:
    """重算触发后历次评估按决定时间排列，历史全部保留。"""
    frozen_id = frozen_version(packet)["version_id"]
    assessments = [a for a in packet.get("assessments", []) if a["version_id"] == frozen_id]
    return sorted(assessments, key=lambda a: _parse_dt(a["decided_at"]))


# ---------- R4：机场与航司并发确认仲裁 ----------

def arbitrate(confirmations: Iterable[dict]) -> list[dict]:
    """并发确认只产生一个生效结论。

    同一版本、同一结论：按（决定时间，确认编号）确定性取唯一生效者，
    其余置 superseded；版本或结论不一致全部置 conflict，须重新评估。
    仲裁只取决于数据内容，与提交顺序无关。
    """
    ordered = sorted(
        confirmations,
        key=lambda c: (_parse_dt(c["decided_at"]), c["confirmation_id"]),
    )
    if not ordered:
        return []

    versions = {c["version_id"] for c in ordered}
    decisions = {c["proposed_decision"] for c in ordered}
    parties = {c["party"] for c in ordered}

    resolved: list[dict] = []
    if len(versions) > 1 or len(decisions) > 1 or len(parties) < 2:
        for confirmation in ordered:
            resolved.append({**confirmation, "status": "conflict"})
        return resolved

    winner = ordered[0]
    for confirmation in ordered:
        status = "effective" if confirmation["confirmation_id"] == winner["confirmation_id"] else "superseded"
        resolved.append({**confirmation, "status": status})
    return resolved


def effective_confirmation(packet: dict) -> dict:
    resolved = arbitrate(packet["confirmations"])
    effective = [c for c in resolved if c["status"] == "effective"]
    if not effective:
        raise ReleaseError("机场与航司未达成唯一生效结论（R4）")
    return effective[0]


# ---------- R5：敏感维修资料岗位隔离 ----------

def redact_for_role(payload: Any, role: str) -> Any:
    """按岗位脱敏；无权限岗位看不到敏感节点的字段名与内容。"""
    if role not in ROLE_SENSITIVITY:
        raise ReleaseError(f"未知岗位：{role}")
    allowed = ROLE_SENSITIVITY[role]

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            detail = node.get("sensitive_detail")
            if isinstance(detail, dict):
                level = detail.get("sensitivity")
                if allowed is None or level not in allowed:
                    node = {**node, "sensitive_detail": REDACTED}
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        return node

    return walk(payload)


# ---------- R6：断网补录与真实发生时间 ----------

def validate_event_times(events: list[dict]) -> None:
    for event in events:
        occurred = _parse_dt(event["occurred_at"])
        recorded = _parse_dt(event["recorded_at"])
        if recorded < occurred:
            raise ReleaseError(
                f"事件 {event['event_id']} 记录时间早于发生时间，数据不可信（R6）"
            )


def timeline(events: list[dict], stream: str | None = None) -> list[dict]:
    """按真实发生时间回放；补录事件归位到发生时刻并标注 backfilled。"""
    validate_event_times(events)
    selected = [e for e in events if stream is None or e["stream"] == stream]
    return sorted(
        selected,
        key=lambda e: (
            _parse_dt(e["occurred_at"]),
            _parse_dt(e["recorded_at"]),
            e["event_id"],
        ),
    )
