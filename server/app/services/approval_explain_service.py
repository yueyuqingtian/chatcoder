"""审批卡「解释」服务（plan-75-332）。

用户在审批卡上点「解释」时，由本服务调用 LLM 生成该操作的中文说明：
用途 / 影响范围 / 风险，逐条要点，前端把文字填进骨架。

模型与思考深度的解析顺序（用户明确要求「默认使用会话模型与会话思考深度，
可在设置中配置专用模型与专用思考深度」）：

1. 设置中心「执行策略 → 审批解释」的专用模型 / 专用思考深度（非空才生效）；
2. 回落到**会话模型**与**本 turn 的会话思考深度**（经审批 detail 透传）；
3. 思考深度再兜底全局 `agent_reasoning_effort`。

输出经会话 WebSocket 推送（与审批请求同通道）：
- `approval.explain.delta` 流式增量；
- `approval.explain.done`  完成（带模型与档位，供前端展示"用什么解释的"）；
- `approval.explain.error` 失败（前端在卡片内提示，不影响审批按钮可用性）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 解释输出的条目上限（前端按行渲染；提示词已约束条数，此处兜底截断防跑飞）
_MAX_CHARS = 4000

_SYSTEM_PROMPT = """你是开发工具里的命令与文件操作解释器。用户正在决定是否批准一个操作，
你的任务是用简明中文说明这个操作"要做什么、会影响什么、有什么风险"，帮他做判断。

输出要求（严格遵守）：
1. 只输出 2~4 条要点，每条一行，以 "- " 开头，不要标题、不要编号、不要代码块围栏。
2. 每条不超过 60 字，用词面向不懂技术的用户也能看懂。
3. 第 1 条说用途（这个操作做什么）；中间条说影响范围（会改动/读取哪些东西）；
   最后一条必须说明风险等级与后果（只读无副作用 / 仅影响项目内文件 / 不可逆等）。
4. 只用事实推断，不要编造路径或参数；信息不足时如实说明"仅从命令本身无法完全确定"。
5. 不要重复命令原文，不要输出建议用户如何操作的话。"""


async def _load_session(db, detail: dict):
    """按 detail.session_id 读会话行（解释服务独立于 turn 上下文，需自行取会话）。"""
    sid = detail.get("session_id")
    if sid is None:
        return None
    try:
        from app.persistence.models.message import Session

        return await db.get(Session, int(sid))
    except Exception:
        logger.debug("[explain] 读取会话失败 %s", sid, exc_info=True)
        return None


def _resolve_effort(detail: dict) -> str | None:
    """解析解释用思考深度：设置专用档位优先，否则本 turn 档位，再兜底全局默认。"""
    from app.core.config import settings

    dedicated = str(getattr(settings, "approval_explain_reasoning_effort", "") or "").strip()
    if dedicated:
        return dedicated
    turn_effort = str(detail.get("reasoning_effort") or "").strip()
    if turn_effort:
        return turn_effort
    return str(getattr(settings, "agent_reasoning_effort", "") or "").strip() or None


def _build_user_prompt(detail: dict) -> str:
    """把被审批的工具调用描述成给模型看的文本。"""
    tool = str(detail.get("tool") or "unknown")
    args = detail.get("args") or {}
    risk_note = str(detail.get("risk_note") or "")
    action_label = str(detail.get("action_label") or "")
    cwd = str(detail.get("workspace_root") or "")

    lines = [
        f"被审批的工具：{tool}",
    ]
    if action_label:
        lines.append(f"动作概述：{action_label}")
    if risk_note:
        lines.append(f"系统已识别的风险特征：{risk_note}")
    if cwd:
        lines.append(f"工作目录：{cwd}")

    # 参数按值逐条列出（命令类给全文，其余截断，避免超长内容淹没重点）
    lines.append("调用参数：")
    if isinstance(args, dict):
        for k, v in args.items():
            text = v if isinstance(v, str) else _json_text(v)
            if len(text) > 1200:
                text = text[:1200] + "…（已截断）"
            lines.append(f"- {k}: {text}")
    else:
        lines.append(f"- {_json_text(args)[:1200]}")

    lines.append("")
    lines.append("请按系统要求输出要点。")
    return "\n".join(lines)


def _json_text(value) -> str:
    try:
        import json

        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


async def explain_approval(db, approval_id: str) -> None:
    """生成解释并流式推送。异常一律转成 approval.explain.error，不向上抛。"""
    from app.orchestration.approval import approval_manager

    detail = approval_manager.get_detail(approval_id)
    if not detail:
        await _push_error(detail, approval_id, "该审批已处理，无需再解释")
        return

    sid = detail.get("session_id")
    if sid is None:
        await _push_error(detail, approval_id, "无法定位审批所属会话")
        return
    session_id = int(sid)

    # 1. 解析模型与思考深度
    session = await _load_session(db, detail)
    model_id = None
    if session is not None:
        from app.core.config import settings

        dedicated_model = getattr(settings, "approval_explain_model_id", None)
        model_id = int(dedicated_model) if dedicated_model else getattr(session, "model_id", None)
    effort = _resolve_effort(detail)

    # 2. 解析 provider
    provider = None
    try:
        from app.models.registry import get_model_registry
        from app.persistence.models.model_reg import Model

        registry = get_model_registry()
        model_row = await db.get(Model, int(model_id)) if model_id else None
        provider, reason = await registry.get_provider_for_model(db, model_row)
        if provider is None:
            await _push_error(detail, approval_id, f"解释所用模型不可用（{reason}）")
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("[explain] provider 解析失败 %s", approval_id, exc_info=True)
        await _push_error(detail, approval_id, f"解释服务初始化失败：{type(exc).__name__}")
        return

    # 3. 流式调用
    from app.models.schemas import ChatMessage, ChatRequest

    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=_build_user_prompt(detail)),
        ],
        model="",
        reasoning_effort=effort,
        max_tokens=800,
    )

    collected = ""
    try:
        async for event in provider.stream_structured(request):
            etype = event.get("type")
            if etype == "content":
                delta = event.get("delta") or ""
                if not delta:
                    continue
                collected += delta
                if len(collected) > _MAX_CHARS:
                    collected = collected[:_MAX_CHARS]
                    break
                await _push(session_id, "approval.explain.delta", {
                    "approval_id": approval_id, "delta": delta,
                })
            elif etype == "error":
                raise RuntimeError(str(event.get("message") or event.get("error") or "模型返回错误"))
    except Exception as exc:  # noqa: BLE001 —— 解释失败只影响该卡片，不阻断审批
        logger.warning("[explain] 生成失败 approval=%s", approval_id, exc_info=True)
        # 已有部分内容时按完成处理，避免用户看到的内容被整体丢弃
        if collected.strip():
            await _push(session_id, "approval.explain.done", {
                "approval_id": approval_id, "text": collected.strip(),
                "model": _model_label(model_id), "reasoning_effort": effort or "",
                "truncated": True,
            })
            return
        await _push_error(detail, approval_id, f"解释生成失败：{type(exc).__name__}: {exc}")
        return

    text = collected.strip()
    if not text:
        await _push_error(detail, approval_id, "模型没有返回解释内容，请重试")
        return

    await _push(session_id, "approval.explain.done", {
        "approval_id": approval_id, "text": text,
        "model": _model_label(model_id), "reasoning_effort": effort or "",
    })


def _model_label(model_id) -> str:
    return f"#{model_id}" if model_id else ""


async def _push(session_id: int, event: str, payload: dict) -> None:
    """向会话通道推事件（失败静默：解释属辅助能力，不得影响审批主流程）。"""
    try:
        from app.gateway.ws import manager as ws_manager

        await ws_manager.broadcast(session_id, {"event": event, "payload": payload})
    except Exception:
        logger.debug("[explain] 事件推送失败 %s", event, exc_info=True)


async def _push_error(detail: dict | None, approval_id: str, message: str) -> None:
    sid = (detail or {}).get("session_id")
    if sid is None:
        logger.info("[explain] 无法推送错误（无会话）%s: %s", approval_id, message)
        return
    await _push(int(sid), "approval.explain.error", {
        "approval_id": approval_id, "message": message,
    })
