"""分层上下文构建器（v2）。

统一入口 build(agent, turn, session, project) → ContextBundle。
分层片段（注意力递减）：
1. Current Goal  2. Working Directory & Tool Rules  3. Git Repos  4. Project Rules
5. Project Structure  6. Session Memory  7. Subagent Handoff  8. Skills/MCP
9. Global Context  10. Token Budget
"""
import base64
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import ChatMessage
from app.orchestration.prompts import build_main_system_prompt, build_subagent_system_prompt
from app.orchestration.rules_loader import load_session_rules, project_structure_brief

logger = logging.getLogger(__name__)

# v14: 附件类型中文标签（前端上传返回的 type 字段）
_ATT_TYPE_LABEL = {
    "image": "图片",
    "text": "文本",
    "spreadsheet": "表格",
    "document": "文档",
    "unsupported": "附件",
}


@dataclass
class ContextBundle:
    system: str
    developer_parts: list[str] = field(default_factory=list)
    history: list[ChatMessage] = field(default_factory=list)
    instruction: str = ""
    # v15: 多模态指令内容块（图片附件直接以 image_url 注入当前用户消息）
    instruction_blocks: list[dict] | None = None

    def to_messages(self) -> list[ChatMessage]:
        """组装 system + 合并 developer + 历史 + user 指令。"""
        messages = [ChatMessage(role="system", content=self.system)]
        if self.developer_parts:
            messages.append(ChatMessage(role="developer", content="\n\n".join(self.developer_parts)))
        messages.extend(self.history)
        if self.instruction or self.instruction_blocks:
            messages.append(ChatMessage(
                role="user", content=self.instruction,
                content_blocks=self.instruction_blocks,
            ))
        return messages


async def _load_skills_and_mcp(db: AsyncSession) -> tuple[str, str]:
    """全局技能/MCP 摘要（尽力而为，失败返回空）。"""
    skills_text = mcp_text = ""
    try:
        from app.services.skill_service import get_global_skills
        skills = await get_global_skills(db)
        if skills:
            parts = [f"- {s.display_name or s.name}: {(s.description or '')[:200]}" for s in skills[:10]]
            skills_text = "\n".join(parts)
    except Exception:
        logger.warning("[context] 全局技能加载失败", exc_info=True)
    try:
        from app.services.skill_service import get_global_mcp_servers
        servers = await get_global_mcp_servers(db)
        if servers:
            mcp_text = "\n".join(f"- {s.display_name or s.name}" for s in servers[:10])
    except Exception:
        logger.warning("[context] MCP 服务器加载失败", exc_info=True)
    return skills_text, mcp_text


async def _load_memories(db: AsyncSession, session_id: int) -> str:
    """注入记忆条目（D8）。"""
    try:
        from app.services.memory_service import load_memories
        entries = await load_memories(db, session_id)
        if entries:
            return "\n".join(f"- {e.text}" for e in entries)
    except Exception:
        logger.warning("[context] 记忆加载失败 session=%s", session_id, exc_info=True)
    return ""


async def _session_memory_summary(db: AsyncSession, session_id: int) -> str:
    """turn 级分层记忆摘要（最近 N 轮）。"""
    try:
        from sqlalchemy import select
        from app.persistence.models.turn import Turn
        res = await db.execute(
            select(Turn).where(Turn.session_id == session_id, Turn.status == "completed")
            .order_by(Turn.id.desc()).limit(5)
        )
        turns = list(res.scalars().all())
        lines = [f"- Turn {t.id}: {(t.summary or '')[:150]}" for t in reversed(turns) if t.summary]
        return "\n".join(lines) if lines else ""
    except Exception:
        logger.warning("[context] 会话记忆摘要失败 session=%s", session_id, exc_info=True)
        return ""


# v15: 多模态图片注入上限（防单次请求过大）
_MAX_INLINE_IMAGES = 4
_MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024  # 4MB/张


def _load_inline_image_blocks(attachments: list[dict]) -> tuple[list[dict], list[str]]:
    """把图片附件读成 image_url 内容块（供多模态模型直接看图）。

    返回 (blocks, notes)：notes 记录被跳过图片的原因，注入上下文让 AI 知情。
    """
    from app.core.config import settings
    from app.services.doc_parser import is_image

    try:
        root = Path(settings.uploads_dir).resolve()
    except (OSError, ValueError):
        return [], []
    blocks: list[dict] = []
    notes: list[str] = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        rel = str(a.get("path") or "")
        filename = str(a.get("filename") or "")
        if not rel or not is_image(filename or rel):
            continue
        if len(blocks) >= _MAX_INLINE_IMAGES:
            notes.append(f"- {filename}: 超出单次注入图片上限({_MAX_INLINE_IMAGES}张)，未直接附带")
            continue
        try:
            target = (root / rel).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            notes.append(f"- {filename}: 路径非法({rel})，未直接附带")
            continue
        if not target.is_file():
            notes.append(f"- {filename}: 文件不存在({rel})，未直接附带")
            continue
        size = target.stat().st_size
        if size > _MAX_INLINE_IMAGE_BYTES:
            notes.append(f"- {filename}: 图片过大({size // 1024 // 1024}MB)，未直接附带")
            continue
        try:
            b64 = base64.b64encode(target.read_bytes()).decode("ascii")
        except OSError:
            notes.append(f"- {filename}: 读取失败，未直接附带")
            continue
        mime = str(a.get("mime_type") or "") or mimetypes.guess_type(target.name)[0] or "image/png"
        blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return blocks, notes


async def build_main_context(
    db: AsyncSession, *, agent, session, project, turn, user_message: str,
    attachments: list[dict] | None = None,
    multimodal: bool = False,
) -> ContextBundle:
    """构建主代理上下文。"""
    workspace = session.worktree_path or (project.path if project else "")
    bundle = ContextBundle(
        system=build_main_system_prompt(),
        instruction=user_message,
    )
    # 1. Current Goal
    bundle.developer_parts.append(f"## Current Goal\n{user_message[:2000]}")
    # 2. Working Directory & Tool Rules
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    bundle.developer_parts.append(ws_ctx)
    # 3. Git Repos（并入结构摘要）
    structure = await project_structure_brief(workspace)
    if structure:
        bundle.developer_parts.append(f"## Project Structure\n{structure}")
    # 4. Project Rules
    rules = await load_session_rules(workspace, project.rules_docs if project else None)
    if rules:
        bundle.developer_parts.append(f"## Project Rules\n{rules}")
    # 5. Session Memory（turn 摘要 + 记忆条目）
    mem_summary = await _session_memory_summary(db, session.id)
    if mem_summary:
        bundle.developer_parts.append(f"## Session Memory\n{mem_summary}")
    memories = await _load_memories(db, session.id)
    if memories:
        bundle.developer_parts.append(f"## Your Memory (from previous tasks)\n{memories}")

    # v6.4: 注入历史消息窗口 —— 修复上下文丢失问题
    # 根因：build_main_context 原本只注入 turn 摘要，不注入历史消息，
    # 导致 AI 每个 turn 都看不到之前的完整对话，只能看到摘要片段。
    # 现在直接取未摘要的历史消息，用 token 预算贪心选取，转成 ChatMessage 注入。
    try:
        from app.orchestration.context_memory import (
            _fetch_main_messages, _resolve_leader_context_window,
        )
        from app.orchestration.token_counter import (
            select_messages_by_token_budget, get_main_window_budget, MIN_MESSAGES_KEEP,
        )
        from app.models.schemas import ChatMessage as _CM

        context_window = await _resolve_leader_context_window(db, session)
        window_budget = get_main_window_budget(context_window)

        # v6.4: shared_context 是动态属性，可能不存在，用 getattr 安全访问
        ctx = getattr(session, "shared_context", None) or {}
        if not isinstance(ctx, dict):
            ctx = {}
        summarized_ids = set(ctx.get("summarized_ids") or [])

        # v6.4: 提高limit到2000，覆盖全部历史消息（原来200条会丢失早期对话）
        all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
        unsummarized = [m for m in all_msgs if m.id not in summarized_ids]

        # v6.5: 保留 text + tool_call + tool_result（过滤 thinking/plan 等非对话类型）。
        # 旧版只保留 text，导致 AI 看不到工具调用历史，上下文严重偏低且无法复用工具结果。
        # v1.2: thinking 也保留——thinking 模式网关要求工具调用回合把历史
        # reasoning_content 回传，历史重建时用 thinking 消息补回该字段。
        from app.core.enums import MsgType as _MsgType
        _keep_types = {_MsgType.TEXT.value, _MsgType.TOOL_CALL.value,
                       _MsgType.TOOL_RESULT.value, _MsgType.THINKING.value}
        unsummarized = [m for m in unsummarized if m.msg_type in _keep_types]

        # Token-budget 选取：从最新向前贪心，直到预算耗尽
        recent, _ = select_messages_by_token_budget(
            unsummarized, window_budget, min_keep=MIN_MESSAGES_KEEP,
        )

        # v6.5: 在历史前插入明确标记，让AI知道这是完整历史对话而非摘要
        if recent:
            bundle.history.append(_CM(
                role="user",
                content="## Conversation History\nBelow is our conversation history. Text messages are shown in full; tool results are the actual outputs as recorded (very long tool outputs may be truncated). You can reference all of it."
            ))
            bundle.history.append(_CM(
                role="assistant",
                content="Understood. I can see the conversation history below and will reference it as needed."
            ))

        # v6.5: 转成 ChatMessage，正确处理 text/tool_call/tool_result 三种类型。
        # tool_call -> assistant 带 tool_calls；tool_result -> tool 角色消息。
        # 保证 OpenAI tool_calls/tool 结果配对，避免网关 400 报错。
        #
        # v8: agent_loop 把"同一轮 assistant(文本+tool_calls)"落库为 text + tool_call 两条消息。
        # 若照旧各转成独立消息，会出现 assistant(tool_calls) 与 tool 结果之间夹着
        # assistant(文本) 的顺序，违反 OpenAI 协议（assistant(tool_calls) 后必须紧跟 tool），
        # 网关报 "An assistant message with 'tool_calls' must be followed by tool messages"。
        # 因此将紧随 tool_call 之前的 agent 文本合并进同一条 assistant(tool_calls) 消息。
        _pending_agent_text = ""
        # v1.2: 暂存紧随 tool_call 之前的思考内容，回传为 assistant 的 reasoning_content
        # （thinking 模式网关要求，缺失会 400）
        _pending_thinking = ""

        def _flush_agent_text() -> None:
            """把暂存的 agent 文本（及其思考内容）落为 assistant 消息并清空暂存。"""
            nonlocal _pending_agent_text, _pending_thinking
            if _pending_agent_text:
                _cm = _CM(role="assistant", content=_pending_agent_text)
                if _pending_thinking:
                    _cm.reasoning_content = _pending_thinking
                bundle.history.append(_cm)
            _pending_agent_text = ""
            _pending_thinking = ""

        for m in recent:
            if m.msg_type == _MsgType.THINKING.value:
                # v1.2: 思考块不直接转成消息，暂存到紧随的 assistant 消息
                _pending_thinking = m.content.get("text") or _pending_thinking
                continue
            if m.msg_type == _MsgType.TEXT.value:
                text = m.content.get("text") or m.content.get("note") or ""
                atts = m.content.get("attachments") or []
                att_note = ""
                if atts and isinstance(atts, list):
                    att_note = "\n".join(
                        f"- {a.get('filename') or '(未命名)'}: path=`{a.get('path') or ''}`"
                        for a in atts if isinstance(a, dict) and a.get("path")
                    )
                    if att_note:
                        att_note = "（该消息附带附件：\n" + att_note + "\n如需内容请调用 read_attachment 读取 path）"
                if m.sender_type == "user":
                    # v14: 历史用户消息若带附件（文件地址），把路径一并注入，
                    # AI 可随时通过 read_attachment 回读附件内容；仅附件无文字的消息也注入
                    if not text and not att_note:
                        continue
                    text = f"{text}\n{att_note}" if att_note else text
                    _flush_agent_text()
                    bundle.history.append(_CM(role="user", content=text))
                else:
                    # 暂存 agent 文本，等待下一个 tool_call 合并
                    _pending_agent_text = text
            elif m.msg_type == _MsgType.TOOL_CALL.value:
                # 工具调用作为 assistant 消息（带 tool_calls），合并暂存的 agent 文本
                tool_name = m.content.get("tool", "")
                args = m.content.get("args", {}) or {}
                call_key = m.content.get("call_key", "") or f"call_{m.id}"
                if isinstance(args, str):
                    try:
                        import json as _json
                        args = _json.loads(args)
                    except Exception:
                        args = {"_raw": args}
                bundle.history.append(_CM(
                    role="assistant",
                    content=_pending_agent_text or None,
                    tool_calls=[{
                        "id": call_key,
                        "name": tool_name,
                        "arguments": args,
                    }],
                    # v1.2: 回传思考内容（thinking 模式网关要求）
                    reasoning_content=_pending_thinking or None,
                ))
                _pending_agent_text = ""
                _pending_thinking = ""
            elif m.msg_type == _MsgType.TOOL_RESULT.value:
                # 工具结果作为 tool 角色消息
                _flush_agent_text()
                tool_name = m.content.get("tool", "")
                call_key = m.content.get("call_key", "") or f"call_{m.id}"
                output = m.content.get("output", "") or ""
                error = m.content.get("error", "") or ""
                result_text = output or error or "(无输出)"
                # 截断过长的工具输出，避免单条结果撑爆窗口（与落库截断 MAX_TOOL_OUTPUT_CHARS 对齐）
                if len(result_text) > 16000:
                    result_text = result_text[:16000] + "\n...(工具输出已截断)"
                bundle.history.append(_CM(
                    role="tool",
                    content=result_text,
                    name=tool_name,
                    tool_call_id=call_key,
                ))
        # 循环结束：暂存的 agent 文本（无后续 tool_call）作为独立 assistant 消息
        _flush_agent_text()

        logger.info(
            "[context] session=%s 注入历史消息 %d 条 (window=%dK, budget=%d tokens, summarized=%d, recent=%d)",
            session.id, len(bundle.history),
            context_window // 1000, window_budget, len(summarized_ids), len(recent),
        )
        # v6.4 诊断：打印前3条和后3条历史消息的摘要
        for _i, _m in enumerate(recent[:3] + recent[-3:]):
            _text = (_m.content.get("text") or _m.content.get("note") or "")[:80]
            logger.info("[context] recent[%d] sender=%s text=%s", _i, _m.sender_type, _text)
    except Exception:
        logger.warning("[context] 注入历史消息失败(非阻塞)", exc_info=True)

    # 6. Skills / MCP
    skills, mcp = await _load_skills_and_mcp(db)
    if skills:
        bundle.developer_parts.append(f"## Available Skills\n{skills}")
    if mcp:
        bundle.developer_parts.append(f"## Available MCP Servers\n{mcp}")
    # 附件注入（v14: 附件已统一为文件地址，注入路径清单 + 读取工具说明，
    # AI 通过 read_attachment 工具按 path 读取图片/文档内容）
    # v15: 多模态模型时图片直接以 image_url 内容块注入当前用户消息，
    # AI 无需工具调用即可看图；非图片/非多模态仍走 read_attachment。
    if attachments:
        att_lines = [
            f"- {a.get('filename') or '(未命名)'}（{_ATT_TYPE_LABEL.get(a.get('type'), a.get('mime_type') or '附件')}）: "
            f"path=`{a.get('path') or ''}`"
            for a in attachments if isinstance(a, dict) and a.get("path")
        ]
        if att_lines:
            hint = (
                "## 用户上传的附件\n"
                "用户消息附带了以下文件（已保存到服务器，path 为附件实际地址）：\n"
                + "\n".join(att_lines)
            )
            if multimodal:
                blocks, notes = _load_inline_image_blocks(attachments)
                if blocks:
                    bundle.instruction_blocks = blocks
                    hint += (
                        f"\n\n其中 {len(blocks)} 张图片已直接附带在用户消息中，"
                        "请直接查看图片内容回答，无需调用任何工具。"
                    )
                if notes:
                    hint += "\n以下图片未能直接附带：\n" + "\n".join(notes)
                hint += (
                    "\n\n其他文件（docx/pdf/xlsx/txt 等）阅读方法：调用 read_attachment 工具读取，"
                    "参数 path 使用上面的附件路径，返回解析文本。"
                )
            else:
                hint += (
                    "\n\n阅读方法：调用 read_attachment 工具读取，参数 path 使用上面的附件路径"
                    "（如 `1a2b3c/报告.docx`，不要自行猜测绝对路径）。"
                    "docx/pdf/xlsx/txt 等返回解析文本。"
                )
            bundle.developer_parts.append(hint)
    return bundle


async def build_subagent_context(
    db: AsyncSession, *, agent, session, project, task,
    handoff_summary: str,
) -> ContextBundle:
    """构建子代理上下文（独立，仅看交接摘要 + 项目规则）。"""
    workspace = session.worktree_path or (project.path if project else "")
    bundle = ContextBundle(
        system=build_subagent_system_prompt(task.title or "", task.acceptance_criteria or ""),
        instruction=f"Start working on: {task.title}",
    )
    bundle.developer_parts.append(f"## Current Task\nTitle: {task.title}")
    if task.description:
        bundle.developer_parts.append(f"Description: {task.description}")
    if handoff_summary:
        bundle.developer_parts.append(f"## Handoff Summary (from main agent)\n{handoff_summary}")
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    bundle.developer_parts.append(ws_ctx)
    rules = await load_session_rules(workspace, project.rules_docs if project else None)
    if rules:
        bundle.developer_parts.append(f"## Project Rules\n{rules}")
    return bundle
