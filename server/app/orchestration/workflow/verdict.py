"""评估结论（verdict）解析与返工决策（设计 §4.2 / §5.6）。纯逻辑，无数据库与运行时副作用。

评估角色的最终输出中应包含一个 verdict JSON 对象。解析时取最后一个含 decision 字段的对象，
并按契约逐字段校验；不合规返回 None，由运行时决定是否追问修复（最多一次）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

VERDICT_DECISIONS = frozenset({"pass", "rework", "escalate"})
CRITERION_RESULTS = frozenset({"met", "unmet", "unverified"})
ISSUE_SEVERITIES = frozenset({"blocker", "major", "minor"})


def _json_objects(text: str) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start < 0:
            return
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            yield obj
        idx = end


def validate_verdict(obj: dict[str, Any]) -> dict[str, Any] | None:
    """按契约校验并规范化 verdict；任一字段不合规返回 None。"""
    decision = obj.get("decision")
    if decision not in VERDICT_DECISIONS:
        return None
    criteria_raw = obj.get("criteria") or []
    issues_raw = obj.get("issues") or []
    if not isinstance(criteria_raw, list) or not isinstance(issues_raw, list):
        return None
    criteria: list[dict[str, str]] = []
    for item in criteria_raw:
        if not isinstance(item, dict) or item.get("result") not in CRITERION_RESULTS:
            return None
        criteria.append(
            {
                "id": str(item.get("id", "")),
                "result": item["result"],
                "evidence": str(item.get("evidence", "")),
            }
        )
    issues: list[dict[str, Any]] = []
    for item in issues_raw:
        if not isinstance(item, dict) or item.get("severity") not in ISSUE_SEVERITIES:
            return None
        issues.append(
            {
                "severity": item["severity"],
                "file": str(item.get("file", "")),
                "line": item.get("line"),
                "problem": str(item.get("problem", "")),
                "fix_hint": str(item.get("fix_hint", "")),
            }
        )
    return {
        "decision": decision,
        "summary": str(obj.get("summary", "")),
        "criteria": criteria,
        "issues": issues,
    }


def parse_verdict(text: str) -> dict[str, Any] | None:
    """从评估者的最终输出中取出 verdict；没有或不合规返回 None。"""
    candidates = [obj for obj in _json_objects(text or "") if "decision" in obj]
    if not candidates:
        return None
    return validate_verdict(candidates[-1])


@dataclass(frozen=True)
class ReviewDecision:
    action: str  # pass / rework / gate
    reason: str


def decide_review(
    verdict: dict[str, Any] | None,
    *,
    checks_passed: bool,
    rework_round: int,
    max_rework: int,
) -> ReviewDecision:
    """依据 verdict 与确定性检查结果决定评估节点的去向（§5.6 运行时覆盖规则）。

    - 检查未通过、存在 blocker、存在 unmet 验收项：不允许 pass，改判 rework；
    - 返工次数用尽仍不能通过：进入人工闸门（gate）；
    - 评估者主动 escalate：直接进入闸门。
    """
    exhausted = rework_round >= max_rework

    def _rework_or_gate(reason: str) -> ReviewDecision:
        if exhausted:
            return ReviewDecision("gate", f"{reason}；返工次数已用尽")
        return ReviewDecision("rework", reason)

    if verdict is None:
        return _rework_or_gate("评估输出不符合契约")
    decision = verdict["decision"]
    if decision == "escalate":
        return ReviewDecision("gate", "评估者请求人工决定")
    blockers = [i for i in verdict["issues"] if i["severity"] == "blocker"]
    unmet = [c for c in verdict["criteria"] if c["result"] == "unmet"]
    if decision == "pass" and checks_passed and not blockers and not unmet:
        return ReviewDecision("pass", "评估通过")
    reasons: list[str] = []
    if not checks_passed:
        reasons.append("确定性检查未通过")
    if blockers:
        reasons.append(f"存在 {len(blockers)} 个阻断问题")
    if unmet:
        reasons.append(f"{len(unmet)} 条验收标准未满足")
    if not reasons:
        reasons.append("评估要求整改")
    return _rework_or_gate("；".join(reasons))


def issues_as_feedback(verdict: dict[str, Any] | None, *, limit: int = 12) -> str:
    """把评估问题整理成返工说明，交给实现节点逐条修复。"""
    if not verdict:
        return "评估输出不符合契约，请按要求逐条复核并修复后重新给出结果。"
    lines = []
    for issue in verdict["issues"][:limit]:
        where = f"{issue['file']}:{issue['line']}" if issue.get("file") else "（未指定位置）"
        hint = f"；建议：{issue['fix_hint']}" if issue.get("fix_hint") else ""
        lines.append(f"- [{issue['severity']}] {where} {issue['problem']}{hint}")
    for criterion in verdict["criteria"]:
        if criterion["result"] == "unmet":
            lines.append(f"- [验收未满足] {criterion['id']}：{criterion.get('evidence', '')}")
    return "\n".join(lines) or "评估要求整改，请复核验收标准后修复。"
