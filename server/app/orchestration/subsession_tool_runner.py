"""子会话工具的执行分发（plan-156-619 S4）。

【为什么单独一个文件】
`agent_loop._run_subagent_tool` 已经是一个覆盖 6 个探索子代理工具的巨型分支；
再把子会话的 4 个工具塞进去，会让"探索 / 执行"两套语义在同一条 if 链里交错，
既难读也容易把归属写错。这里按机制分文件：探索仍在 `_run_subagent_tool`，
执行一律走本模块。

【返回约定】
与其它特判工具一致：返回**字符串**（直接作为 tool 结果回给模型）。
业务错误也转成可读文本，而不是抛异常中断整个 turn——
派发失败是主代理可以自行纠正的（换标题、少建一个、改用探索代理）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 进度汇报里单次最多列出的子会话条数（防止一次性灌爆工具结果）
_MAX_REPORT_ITEMS = 8

_STATUS_TEXT: dict[str, str] = {
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
}


def _format_brief(brief) -> str:
    """把进度视图格式化为一段可读文本。"""
    status = _STATUS_TEXT.get(brief.status, brief.status)
    lines = [f"### 子会话 #{brief.subsession_id} 「{brief.title}」— {status}（模式 {brief.mode}）"]
    if brief.last_output:
        lines.append(f"- 进度：{brief.last_output}")
    if brief.files:
        lines.append("- 涉及文件：" + "、".join(brief.files[:8]))
    if brief.risks:
        lines.append("- 风险 / 未决：" + "；".join(brief.risks[:5]))
    return "\n".join(lines)


def _format_result(header: str, briefs: list) -> str:
    if not briefs:
        return f"{header}\n\n（本会话还没有子会话）"
    shown = briefs[:_MAX_REPORT_ITEMS]
    body = "\n\n".join(_format_brief(b) for b in shown)
    tail = ""
    if len(briefs) > len(shown):
        tail = f"\n\n（另有 {len(briefs) - len(shown)} 个子会话未列出，可用 subsession_id 单独查询）"
    return f"{header}\n\n{body}{tail}"


async def run_subsession_tool(tool_name: str, args: dict, *, session_id: int, db) -> str:
    """执行一个子会话工具。`session_id` 是**调用方**所在会话（主会话或子会话）。"""
    from app.services import subsession_service

    try:
        if tool_name == "create_subsession":
            sid = await subsession_service.create_subsession(
                db,
                parent_session_id=session_id,
                title=str(args.get("title") or ""),
                task=str(args.get("task") or ""),
                context=str(args.get("context") or ""),
                mode=args.get("mode") or None,
            )
            return (
                f"已创建子会话 #{sid}「{args.get('title') or ''}」，任务已下发。\n"
                "它会显示在用户左侧会话列表中；完成后结果会自动回到你的上下文，"
                "需要更细的进展用 get_subsession_progress 查询。"
                "注意：不要自己再做一遍这项任务。"
            )

        if tool_name == "send_to_subsession":
            target = args.get("subsession_id")
            if target is None:
                return "Error: subsession_id 不能为空"
            res = await subsession_service.send_message(
                db, int(target), str(args.get("message") or ""),
            )
            how = "已注入到它当前的执行" if res.get("delivered") == "injected" else "已作为新一轮任务下发"
            return f"消息{how}（子会话 #{res['subsession_id']}，turn {res.get('turn_id')}）。"

        if tool_name == "get_subsession_progress":
            target = args.get("subsession_id")
            briefs = await subsession_service.collect(
                db,
                parent_session_id=session_id,
                subsession_id=int(target) if target is not None else None,
                wait=bool(args.get("wait")),
            )
            header = "子会话进度" if target is None else f"子会话 #{int(target)} 进度"
            return _format_result(header, briefs)

        if tool_name == "interrupt_subsession":
            target = args.get("subsession_id")
            if target is None:
                return "Error: subsession_id 不能为空"
            res = await subsession_service.interrupt(db, int(target))
            if res.get("interrupted"):
                return f"已中断子会话 #{res['subsession_id']}（turn {res.get('turn_id')}）。"
            return f"子会话 #{res['subsession_id']} 当前没有在跑的任务（{res.get('reason') or ''}），无需中断。"

    except subsession_service.SubsessionError as e:
        # 业务错误是主代理可以自行纠正的，转成可读文本而不是中断 turn
        return f"Error: {e}"
    except Exception:  # noqa: BLE001 — 未预期异常记日志，仍把可读原因给模型
        logger.exception("[subsession] 工具执行失败 tool=%s", tool_name)
        return f"Error: 子会话操作失败（{tool_name}），请查看服务端日志或换个方式继续。"

    return f"Error: 未知的子会话工具 {tool_name}"


async def run_own_tool(tool_name: str, args: dict, *, session_id: int, db) -> str:
    """子会话侧的通信工具：向主代理汇报 / 向同级子会话发消息。"""
    from app.services import subsession_service

    if tool_name == "message_main":
        message = str(args.get("message") or "").strip()
        if not message:
            return "Error: message 不能为空"
        await subsession_service.notify_main(
            db, subsession_id=session_id, message=message,
            kind=str(args.get("kind") or "progress"),
        )
        return "已送达主代理（会进入它下一次模型调用的上下文）。"

    if tool_name == "message_peer":
        target = args.get("subsession_id")
        if target is None:
            return "Error: subsession_id 不能为空"
        try:
            await subsession_service.message_peer(
                db, from_subsession=session_id, to_subsession=int(target),
                message=str(args.get("message") or ""),
            )
        except subsession_service.SubsessionError as e:
            return f"Error: {e}"
        return f"已发送给同级子会话 #{int(target)}。"

    return f"Error: 未知的子会话工具 {tool_name}"
