"""逐航点放行领域模型。

把机场承诺的八类保障条件、冻结的能力版本、运行事件和并发确认
计算为一次可解释的放行结论；并提供敏感资料岗位隔离与三流回放。

本模块只做纯领域计算，不依赖第三方库，便于在服务层与单元测试中复用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

CONDITION_KEYS = (
    "airport",
    "aircraft_type",
    "route",
    "service_unit",
    "personnel",
    "spares",
    "bridge",
    "drill",
)

# 触发可用方案重新计算的事件类型
RECOMPUTE_TRIGGERS = {"人员调班", "设备故障", "强对流运行限制", "临时机位变化"}


def load_json(name: str) -> dict[str, Any]:
    source = ROOT / "fixtures" / name
    return json.loads(source.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Event:
    raw: dict[str, Any]

    @property
    def occurred_at(self) -> str:
        # 断网补录也以真实发生时间参与排序，而不是接收时间
        return self.raw["occurred_at"]

    @property
    def recorded_at(self) -> str:
        return self.raw.get("recorded_at", self.raw["occurred_at"])


def _events_up_to(ops: dict[str, Any], flight_no: str, at: str) -> list[Event]:
    events = [
        Event(e)
        for e in ops["events"]
        if e["flight_no"] == flight_no and e["occurred_at"] <= at
    ]
    # 稳定排序：先按真实发生时间，再按接收时间，保证补录事件落在真实时点
    return sorted(events, key=lambda e: (e.occurred_at, e.recorded_at))


def frozen_version(capability: dict[str, Any], version_id: str) -> dict[str, Any]:
    """按冻结版本号取回基线；即使当日发布了更新版本也不自动采用。"""
    for version in capability["capability_versions"]:
        if version["version_id"] == version_id:
            return version
    raise LookupError(f"冻结的能力版本不存在: {version_id}")


def _condition_events(condition: str, events: list[Event]) -> list[Event]:
    return [e for e in events if e.raw.get("condition") == condition]


def _linked_rule(capability: dict[str, Any], trigger: str, target: str) -> dict[str, Any] | None:
    for rule in capability.get("linkage_rules", []):
        if rule["trigger"] == trigger and target in rule["targets"]:
            return rule
    return None


def evaluate_release(
    flight: dict[str, Any],
    capability: dict[str, Any],
    ops: dict[str, Any],
    at: str,
) -> dict[str, Any]:
    """计算某航班在 at 时刻的放行结论。

    评估始终对照“冻结版本”的承诺；当日新发布的能力版本不参与，
    只保留为缺口背景，避免静默改变当日基线。
    """
    version = frozen_version(capability, flight["frozen_capability_version"])
    conditions = version["conditions"]
    events = _events_up_to(ops, flight["flight_no"], at)

    gaps: list[dict[str, Any]] = []
    direct_gap_conditions: set[str] = set()

    # 1) 直接缺口：逐条件把截至评估时刻的最新运行状态与冻结承诺比较
    for key in CONDITION_KEYS:
        spec = conditions[key]
        cond_events = _condition_events(key, events)
        record = _direct_gap(key, spec, cond_events, at)
        if record is not None:
            gaps.append(record)
            if record["status"] == "GAP":
                direct_gap_conditions.add(key)

    # 2) 联动缺口：失效条件按联动规则拖带相关条件复核
    for key in CONDITION_KEYS:
        if any(g["condition"] == key for g in gaps):
            continue
        for trigger in direct_gap_conditions:
            rule = _linked_rule(capability, trigger, key)
            if rule is None:
                continue
            state = _condition_events(key, events)
            latest = state[-1].raw if state else None
            # 被拖带条件若自身已有事件证明恢复，则不再记联动复核项
            if latest is not None and _direct_gap(key, conditions[key], state, at) is None:
                continue
            gaps.append(
                {
                    "condition": key,
                    "label": conditions[key]["label"],
                    "commitment": conditions[key]["commitment"],
                    "status": "LINKED_GAP",
                    "owner": (latest or {}).get("owner") or conditions[key]["owner"],
                    "linked_from": trigger,
                    "workaround": rule.get("note"),
                    "recovery_at": None,
                    "downgrade": False,
                    "review_required": True,
                    "event_refs": [latest["event_id"]] if latest else [],
                }
            )
            break

    workarounds = _dedupe(g["workaround"] for g in gaps if g["workaround"])
    last_owners = _dedupe(g["owner"] for g in gaps)
    sold = bool(flight.get("tickets_sold", 0))

    # 3) 结论以直接缺口为准；联动项提示复核，不单独硬阻断
    blocking = [g for g in gaps if g["status"] == "GAP"]
    decision = _decide(blocking)
    # 已售票航班存在未决缺口时拦截静默降级：系统绝不自动给 GO
    silent_downgrade_blocked = bool(sold and blocking)

    # 4) 并发确认裁决：只产生一个生效结论
    effective = effective_confirmation(ops, flight["flight_no"])

    recompute_reason = _last_recompute_trigger(events)

    return {
        "flight_no": flight["flight_no"],
        "at": at,
        "frozen_version_id": version["version_id"],
        "decision": decision,
        "gaps": gaps,
        "workarounds": workarounds,
        "last_owners": last_owners,
        "silent_downgrade_blocked": silent_downgrade_blocked,
        "effective_confirmation": effective,
        "recompute_reason": recompute_reason,
    }


def _direct_gap(
    key: str,
    spec: dict[str, Any],
    cond_events: list[Event],
    at: str,
) -> dict[str, Any] | None:
    """无缺口返回 None；有缺口返回缺口记录（含恢复时刻与降级标记）。"""
    if not cond_events:
        return None
    latest = cond_events[-1].raw

    def record(status: str, recovery_at: str | None) -> dict[str, Any]:
        return {
            "condition": key,
            "label": spec["label"],
            "commitment": spec["commitment"],
            "status": status,
            "owner": latest.get("owner") or spec["owner"],
            "linked_from": None,
            "workaround": latest.get("workaround"),
            "recovery_at": recovery_at,
            "downgrade": bool(latest.get("workaround_downgrade")),
            "review_required": False,
            "event_refs": [e.raw["event_id"] for e in cond_events],
        }

    if key == "personnel":
        required = spec.get("required_authorized")
        on_duty = latest.get("authorized_mechanics_on_duty")
        recovery_at = latest.get("reinforcement_arrives_at")
        if required is not None and on_duty is not None and on_duty < required:
            if recovery_at and at >= recovery_at:
                return None  # 增援已到场，冻结标准恢复
            return record("GAP", recovery_at)
        return None

    if key == "spares":
        recovery_at = latest.get("expected_available_at")
        if latest.get("available") is False:
            if recovery_at and at >= recovery_at:
                return None  # 转借工具已到场，备件能力恢复
            return record("GAP", recovery_at)
        return None

    if key == "route":
        recovery_at = latest.get("weather_ok_at")
        if recovery_at and at < recovery_at:
            return record("GAP", recovery_at)
        return None

    if key == "bridge":
        if latest.get("bridge_compatible") is False:
            # 远机位客梯车方案低于“廊桥”承诺，属降级且无恢复时刻 → 保持缺口
            return record("GAP", latest.get("bridge_restored_at"))
        return None

    return None


def _decide(blocking_gaps: list[dict[str, Any]]) -> str:
    if not blocking_gaps:
        return "GO"
    # 每个硬缺口都有按冻结标准恢复的预期时刻 → 延误等待恢复；
    # 只要存在无恢复路径（仅能靠降级替代）的缺口 → 不具备放行条件，
    # 取消或显式接受降级必须由签派员决策，系统不自动放行。
    if all(g["recovery_at"] for g in blocking_gaps):
        return "DELAY"
    return "NOGO"


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _last_recompute_trigger(events: list[Event]) -> str | None:
    for event in reversed(events):
        if event.raw.get("type") in RECOMPUTE_TRIGGERS:
            return event.raw["type"]
    return None


def effective_confirmation(
    ops: dict[str, Any], flight_no: str
) -> dict[str, Any] | None:
    """机场与航司并发确认的裁决：先到（真实发生时间，再接收序号）唯一生效。"""
    candidates = [
        c
        for c in ops.get("confirmations", [])
        if c["flight_no"] == flight_no and c["decision"] == "CONFIRMED"
    ]
    if not candidates:
        return None
    winner = min(candidates, key=lambda c: (c["occurred_at"], c["received_seq"]))
    losers = [c for c in candidates if c["confirmation_id"] != winner["confirmation_id"]]
    reason = "occurred_at 最早，先到生效"
    if losers:
        reason += "；其余并发确认不覆盖生效结论: " + ", ".join(
            f"{c['party']}/{c['confirmation_id']}" for c in losers
        )
    return {
        "confirmation_id": winner["confirmation_id"],
        "party": winner["party"],
        "decision": winner["decision"],
        "occurred_at": winner["occurred_at"],
        "reason": reason,
    }


def view_maintenance_document(
    ops: dict[str, Any], doc_id: str, role: str
) -> dict[str, Any]:
    """敏感维修资料按岗位隔离：非授权岗位只见状态字与文档ID。"""
    for doc in ops.get("maintenance_documents", []):
        if doc["doc_id"] == doc_id:
            if role == doc["required_role"]:
                return {
                    "doc_id": doc["doc_id"],
                    "title": doc["title"],
                    "content": doc["content"],
                    "access": "FULL",
                    "role": role,
                }
            return {
                "doc_id": doc["doc_id"],
                "status": "RESTRICTED",
                "required_role": doc["required_role"],
                "access": "DENIED",
                "role": role,
            }
    raise LookupError(f"维修文档不存在: {doc_id}")


def replay(
    flight: dict[str, Any],
    capability: dict[str, Any],
    ops: dict[str, Any],
    checkpoints: list[str] | None = None,
) -> dict[str, Any]:
    """按真实发生时间重放飞机流、旅客流、行李流与各时点评估结论。"""
    flight_no = flight["flight_no"]
    events = sorted(
        (
            Event(e)
            for e in ops["events"]
            if e["flight_no"] == flight_no
        ),
        key=lambda e: (e.occurred_at, e.recorded_at),
    )

    streams: dict[str, list[dict[str, Any]]] = {
        "aircraft": [],
        "passenger": [],
        "baggage": [],
    }
    timeline: list[dict[str, Any]] = []
    for event in events:
        raw = event.raw
        entry = {
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "kind": "event",
            "stream": raw.get("stream"),
            "type": raw.get("type"),
            "detail": raw.get("detail"),
            "offline_backfill": raw.get("offline_backfill", False),
            "source": raw.get("source"),
        }
        timeline.append(entry)
        if raw.get("stream") in streams:
            streams[raw["stream"]].append(entry)

    evaluations = []
    for at in checkpoints or sorted({e.occurred_at for e in events}):
        result = evaluate_release(flight, capability, ops, at)
        snapshot = {
            "occurred_at": at,
            "kind": "evaluation",
            "stream": None,
            "type": "放行评估",
            "detail": f"{result['decision']}，缺口{len(result['gaps'])}项",
            "decision": result["decision"],
            "recompute_reason": result["recompute_reason"],
        }
        evaluations.append(snapshot)
        timeline.append(snapshot)

    timeline.sort(key=lambda item: (item["occurred_at"], 0 if item["kind"] == "event" else 1))

    return {
        "flight_no": flight_no,
        "frozen_version_id": flight["frozen_capability_version"],
        "streams": streams,
        "evaluations": evaluations,
        "timeline": timeline,
    }


def get_flight(ops: dict[str, Any], flight_no: str) -> dict[str, Any]:
    for flight in ops["flights"]:
        if flight["flight_no"] == flight_no:
            return flight
    raise LookupError(f"航班不存在: {flight_no}")
