"""工作流过程报告（plan-workflow-collab）：把各步骤的产出、每轮评估、返工原因与整改情况整理成主代理可读的文本。

主代理据此向用户汇报“做了什么、是否返工、为什么返工、怎么改的、还剩什么问题”，
而不是只知道“工作流完成了”。纯函数，输入为 store.load_program 的快照。
"""

from __future__ import annotations

from typing import Any

KIND_LABEL = {
    "explore": "探索",
    "execute": "实现",
    "review": "评估",
    "check": "检查",
    "gate": "决定",
    "merge": "合并",
    "integrate": "并入",
}
STATUS_LABEL = {
    "pending": "未开始",
    "queued": "排队中",
    "running": "进行中",
    "recovering": "网络重试中",
    "waiting_human": "待确认",
    "awaiting_merge": "等待合并",
    "done": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
    "cancelled": "已取消",
    "skipped": "已跳过",
}
FLOW_STATUS_LABEL = {
    "running": "进行中",
    "waiting_human": "待确认",
    "paused": "已暂停",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已中止",
}
SEVERITY_LABEL = {"blocker": "阻断", "major": "重要", "minor": "次要"}
OUTCOME_LABEL = {
    "passed": "通过",
    "rework": "已返工",
    "accepted": "已采纳当前结果",
    "aborted": "已中止",
    "pending": "待确认",
    "recorded": "问题已记入汇报",
}
REPORT_LIMIT = 7000
# 返工说明里用户补充的起始标记（runtime._schedule_rework 写入）
USER_NOTE_MARK = "用户补充说明（优先遵循）："


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def issue_brief(issue: dict[str, Any]) -> str:
    sev = SEVERITY_LABEL.get(str(issue.get("severity") or ""), str(issue.get("severity") or "问题"))
    file = str(issue.get("file") or "")
    line = issue.get("line")
    where = f"{file}:{line} " if file and line else (f"{file} " if file else "")
    return f"[{sev}] {where}{_clip(issue.get('problem'), 160)}"


def open_issues(node: dict[str, Any]) -> list[dict[str, Any]]:
    """评估节点最终仍未解决的问题：最后一轮结论中的问题（通过时只剩次要问题，采纳时可能含重要问题）。"""
    if node.get("kind") != "review" or node.get("status") != "done":
        return []
    issues = (node.get("verdict") or {}).get("issues") or []
    return [i for i in issues if isinstance(i, dict)]


def _node_lines(node: dict[str, Any]) -> list[str]:
    kind = str(node.get("kind") or "")
    output = node.get("output") or {}
    status = STATUS_LABEL.get(str(node.get("status")), str(node.get("status")))
    head = f"  · [{KIND_LABEL.get(kind, kind)}] {node.get('title') or node.get('key')}：{status}"
    reworks = output.get("reworks") or []
    if kind == "execute" and reworks:
        head += f"（返工 {len(reworks)} 轮）"
    if node.get("agent_id"):
        head += f"｜子代理 #{node['agent_id']}"
    lines = [head]
    if node.get("status") in ("failed", "waiting_human", "interrupted") and node.get("reason"):
        lines.append(f"    原因：{_clip(node['reason'], 200)}")
    summary = output.get("summary") or output.get("text")
    if kind in ("explore", "execute", "merge") and summary:
        lines.append(f"    结果：{_clip(summary, 320)}")
    if kind == "execute":
        files = [str(f) for f in output.get("files_touched") or []]
        if files:
            more = f" 等 {len(files)} 个" if len(files) > 8 else ""
            lines.append(f"    改动文件：{'、'.join(files[:8])}{more}")
        verification = [str(v) for v in output.get("verification") or []]
        if verification:
            lines.append(f"    验证：{_clip('；'.join(verification), 240)}")
        risks = [str(r) for r in output.get("risks") or []]
        if risks:
            lines.append(f"    风险：{_clip('；'.join(risks), 200)}")
        for item in reworks[-4:]:
            feedback = str(item.get("feedback") or "")
            body, _, user_note = feedback.partition(USER_NOTE_MARK)
            count = sum(1 for line in body.splitlines() if line.lstrip().startswith("- ["))
            text = (
                f"    第 {item.get('round')} 轮返工：处理 {count} 个问题"
                if count
                else (f"    第 {item.get('round')} 轮返工")
            )
            if user_note.strip():
                text += f"（用户要求：{_clip(user_note, 160)}）"
            text += f"；整改说明：{_clip(item.get('summary'), 240)}"
            lines.append(text)
    if kind == "check":
        for check in (output.get("checks") or [])[:6]:
            mark = "通过" if check.get("ok") else ("未执行" if check.get("blocked") else "未通过")
            lines.append(f"    {check.get('command')}：{mark}")
    if kind == "review":
        for item in (output.get("rounds") or [])[-5:]:
            issues = item.get("issues") or []
            outcome = OUTCOME_LABEL.get(str(item.get("outcome") or ""), "")
            text = f"    第 {item.get('round')} 轮评估：{item.get('reason') or ''}"
            if item.get("summary"):
                text += f" — {_clip(item['summary'], 160)}"
            if outcome:
                text += f"（{outcome}）"
            lines.append(text)
            if item.get("checks_ok") is False:
                lines.append("      确定性检查未通过")
            for issue in issues[:5]:
                lines.append(f"      {issue_brief(issue)}")
            if len(issues) > 5:
                lines.append(f"      …还有 {len(issues) - 5} 个问题")
        if output.get("user_note"):
            lines.append(f"    用户说明：{_clip(output['user_note'], 200)}")
        remaining = open_issues(node)
        if remaining:
            lines.append(f"    遗留问题 {len(remaining)} 个（未返工）：")
            lines.extend(f"      {issue_brief(i)}" for i in remaining[:6])
    return lines


def program_report(snap: dict[str, Any], *, limit: int = REPORT_LIMIT) -> str:
    """程序的过程报告：每个子工作流、每个步骤的产出与评估返工历史。"""
    run = snap["run"]
    lines = [
        f"工作流『{run.get('title') or '未命名'}』（program_id={run['id']}）",
        f"目标：{_clip(run.get('goal'), 300)}",
    ]
    if run.get("rework_total"):
        lines.append(f"累计返工 {run['rework_total']} 次")
    for flow in snap["flows"]:
        frun = flow["run"]
        title = frun.get("title") or frun.get("package_key")
        status = FLOW_STATUS_LABEL.get(str(frun.get("status")), str(frun.get("status")))
        lines.append(f"- 子工作流『{title}』（{frun.get('package_key')}）：{status}")
        for node in flow["nodes"]:
            lines.extend(_node_lines(node))
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 40] + "\n…（报告较长已截断，可用 workflow_status 查询）"
    return text


def remaining_issue_count(snap: dict[str, Any]) -> int:
    return sum(len(open_issues(n)) for f in snap["flows"] for n in f["nodes"])


def pending_gates(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """等待确认的步骤：主代理需要向用户说明并在确认后调用 decide_workflow。"""
    out: list[dict[str, Any]] = []
    for flow in snap["flows"]:
        for node in flow["nodes"]:
            if node.get("status") != "waiting_human":
                continue
            gate = node.get("conflict") or {}
            kind = str(gate.get("kind") or "review")
            out.append(
                {
                    "program_id": snap["run"]["id"],
                    "program_title": snap["run"].get("title") or "",
                    "flow_key": flow["run"].get("package_key") or "",
                    "flow_title": flow["run"].get("title") or flow["run"].get("package_key") or "",
                    "node_key": node["key"],
                    "node_title": node.get("title") or node["key"],
                    "kind": kind,
                    "stage": gate.get("stage") or "",
                    "reason": node.get("reason") or "",
                    "options": gate_options(kind, node),
                    "issues": ((node.get("verdict") or {}).get("issues") or [])
                    if kind == "review"
                    else [],
                }
            )
    return out


def gate_options(kind: str, node: dict[str, Any]) -> list[str]:
    if kind == "scope_violation":
        return ["include", "abort"]
    if kind == "gate":
        return ["approve", "abort"]
    if kind == "integration":
        return ["rework", "abort"]
    if kind == "review" and not node.get("rework_to"):
        return ["approve", "abort"]
    return ["approve", "rework", "abort"]


_DECISION_TEXT = {
    "approve": "approve（采纳当前结果 / 继续）",
    "rework": "rework（返工，可附说明）",
    "abort": "abort（中止该子工作流）",
    "include": "include（把越界文件纳入范围并继续）",
}


def gate_block(gate: dict[str, Any]) -> str:
    """单个待确认步骤的说明（通知与上下文注入共用）。"""
    lines = [
        f"- program_id={gate['program_id']} flow_key={gate['flow_key']} node_key={gate['node_key']}"
        f"（『{gate['flow_title']}』·『{gate['node_title']}』）：{_clip(gate['reason'], 200)}",
    ]
    for issue in gate["issues"][:8]:
        lines.append(f"    {issue_brief(issue)}")
        if issue.get("fix_hint"):
            lines.append(f"      建议：{_clip(issue['fix_hint'], 160)}")
    lines.append("    可选决定：" + "；".join(_DECISION_TEXT[o] for o in gate["options"]))
    return "\n".join(lines)
