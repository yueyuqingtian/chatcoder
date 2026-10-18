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

from app.orchestration.subsession_report import (
    REPORT_PAGE_CHARS, REPORT_PREVIEW_CHARS, continuation_hint, page_of,
)

logger = logging.getLogger(__name__)

# 进度汇报里单次最多列出的子会话条数（防止一次性灌爆工具结果）
_MAX_REPORT_ITEMS = 8

_STATUS_TEXT: dict[str, str] = {
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
}


def _format_brief(brief, *, report_offset: int | None = None) -> str:
    """把进度视图格式化为一段可读文本。

    plan-156-620：结论原文不再静默截断——
    · 单个子会话（report_offset 非 None）：从 offset 起给一整段（REPORT_PAGE_CHARS），未读完附续读提示；
    · 汇总多个子会话：每个给开头 REPORT_PREVIEW_CHARS，并明确标出总字数与续读方式。
    """
    status = _STATUS_TEXT.get(brief.status, brief.status)
    lines = [f"### 子会话 #{brief.subsession_id} 「{brief.title}」— {status}（模式 {brief.mode}）"]
    if brief.last_output:
        lines.append(f"- 步骤进度：{brief.last_output}")
    if brief.files:
        lines.append("- 涉及文件：" + "、".join(brief.files[:8]))
    if brief.risks:
        lines.append("- 风险 / 未决：" + "；".join(brief.risks[:5]))
    if brief.report:
        size = REPORT_PAGE_CHARS if report_offset is not None else REPORT_PREVIEW_CHARS
        page = page_of(brief.report, report_offset or 0, size)
        lines.append("- 结论原文：")
        lines.append(page.text + continuation_hint(page, brief.subsession_id))
    elif brief.status != "running":
        lines.append("- 结论原文：（最近一轮没有输出文字结论）")
    return "\n".join(lines)


def _format_result(header: str, briefs: list, *, report_offset: int | None = None) -> str:
    if not briefs:
        return f"{header}\n\n（本会话还没有子会话）"
    shown = briefs[:_MAX_REPORT_ITEMS]
    body = "\n\n".join(_format_brief(b, report_offset=report_offset) for b in shown)
    tail = ""
    if len(briefs) > len(shown):
        tail = f"\n\n（另有 {len(briefs) - len(shown)} 个子会话未列出，可用 subsession_id 单独查询）"
    return f"{header}\n\n{body}{tail}"


def _target_id(args: dict, error_cls: type[Exception]) -> int:
    """解析工具参数里的 subsession_id（模型可能给字符串或空值）。"""
    raw = args.get("subsession_id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise error_cls("subsession_id 必须是子会话的数字 id") from None
    if value <= 0:
        raise error_cls("subsession_id 必须是子会话的数字 id")
    return value


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
                purpose=str(args.get("purpose") or ""),
            )
            created = (
                f"已创建子会话 #{sid}「{args.get('title') or ''}」，任务已下发。\n"
                "它会显示在用户左侧会话列表中；完成后结果会自动回到你的上下文，"
                "需要更细的进展用 get_subsession_progress 查询。"
                "注意：不要自己再做一遍这项任务。"
            )
            # plan-156-621：schema 声明 background=false 会阻塞等待，此前 runner 完全忽略该参数，
            # 模型按文档要结果却只得到「任务已下发」。这里补齐：等它跑完并把结论随结果一起返回。
            if args.get("background") is False:
                briefs = await subsession_service.collect(
                    db, parent_session_id=session_id, subsession_id=sid, wait=True,
                )
                body = _format_result(f"子会话 #{sid} 已结束", briefs, report_offset=0)
                return f"{created}\n\n(background=false：已等待该子会话结束)\n\n{body}"
            return created

        if tool_name == "send_to_subsession":
            target = _target_id(args, subsession_service.SubsessionError)
            # plan-156-620：只能操作本主会话名下的子会话
            await subsession_service.require_owned(db, session_id, target)
            res = await subsession_service.send_message(
                db, target, str(args.get("message") or ""),
            )
            how = "已注入到它当前的执行" if res.get("delivered") == "injected" else "已作为新一轮任务下发"
            return f"消息{how}（子会话 #{res['subsession_id']}，turn {res.get('turn_id')}）。"

        if tool_name == "get_subsession_progress":
            target = (_target_id(args, subsession_service.SubsessionError)
                      if args.get("subsession_id") is not None else None)
            briefs = await subsession_service.collect(
                db,
                parent_session_id=session_id,
                subsession_id=target,
                wait=bool(args.get("wait")),
            )
            header = "子会话进度" if target is None else f"子会话 #{target} 进度"
            # 指定单个子会话时按 report_offset 分段返回结论全文；汇总时只给开头 + 续读提示
            offset = None
            if target is not None:
                try:
                    offset = max(0, int(args.get("report_offset") or 0))
                except (TypeError, ValueError):
                    offset = 0
            return _format_result(header, briefs, report_offset=offset)

        if tool_name == "interrupt_subsession":
            target = _target_id(args, subsession_service.SubsessionError)
            await subsession_service.require_owned(db, session_id, target)
            res = await subsession_service.interrupt(db, target)
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
        try:
            res = await subsession_service.notify_main(
                db, subsession_id=session_id, message=message,
                kind=str(args.get("kind") or "progress"),
            )
        except subsession_service.SubsessionError as e:
            return f"Error: {e}"
        if res.get("delivered") == "injected":
            return "已送达主代理（会进入它下一次模型调用的上下文）。"
        return "主代理当前空闲，汇报已记录在主会话中，主代理下一轮会看到。"

    if tool_name == "message_peer":
        try:
            target = _target_id(args, subsession_service.SubsessionError)
            await subsession_service.message_peer(
                db, from_subsession=session_id, to_subsession=target,
                message=str(args.get("message") or ""),
            )
        except subsession_service.SubsessionError as e:
            return f"Error: {e}"
        return f"已发送给同级子会话 #{target}。"

    return f"Error: 未知的子会话工具 {tool_name}"
