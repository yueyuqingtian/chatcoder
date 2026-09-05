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

from app.core.config import settings
from app.models.schemas import ChatMessage
from app.orchestration.prompts import build_main_system_prompt, build_subagent_system_prompt
from app.orchestration.rules_loader import load_session_rules, project_structure_brief
from app.orchestration.tools.shell_env import shell_hint

logger = logging.getLogger(__name__)

# v14: 附件类型中文标签（前端上传返回的 type 字段）
_ATT_TYPE_LABEL = {
    "image": "图片",
    "text": "文本",
    "spreadsheet": "表格",
    "document": "文档",
    "unsupported": "附件",
}


def _attachment_abs_path(rel: str) -> str:
    """把附件相对路径（`{file_id}/{filename}`）转换为服务器绝对路径。

    v33: AI 看到附件时直接给出磁盘绝对路径，避免模型对相对路径/上传目录
    的猜测（read_attachment 同时兼容绝对与相对两种入参）。
    """
    if not rel:
        return ""
    try:
        return str((Path(settings.uploads_dir).resolve() / rel).resolve())
    except (OSError, ValueError):
        return rel


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
    """turn 级分层记忆摘要（最近 N 轮）。

    v21: 条数 5→8、单条 150→300 字符 —— 旧值信息量太少，
    早期上下文被预算截断后模型几乎只剩"最近几行"可依，多轮任务失忆。
    对齐 deepseek-harness 长会话保持策略。
    """
    try:
        from sqlalchemy import select
        from app.persistence.models.turn import Turn
        res = await db.execute(
            select(Turn).where(Turn.session_id == session_id, Turn.status == "completed")
            .order_by(Turn.id.desc()).limit(8)
        )
        turns = list(res.scalars().all())
        lines = [f"- Turn {t.id}: {(t.summary or '')[:300]}" for t in reversed(turns) if t.summary]
        return "\n".join(lines) if lines else ""
    except Exception:
        logger.warning("[context] 会话记忆摘要失败 session=%s", session_id, exc_info=True)
        return ""


# plan-644: 计划轮状态 -> 注入文本语义标注（直接写进 Plan History，模型据此
# 累积未完成项、剔除已完成项；数据库 plan_status 为真值源）
_PLAN_STATUS_LABELS = {
    "proposed": "待确认（未执行，其中未完成需求必须完整纳入新方案）",
    "confirmed": "已确认执行（未完成部分仍需纳入新方案）",
    "done": "已执行完成（已完成项禁止重复列入新方案；未竟事项仍需继承）",
    "cancelled": "已取消（除非用户明确重提，不再列入新方案）",
    "superseded": "已被更新方案取代（若含未被后续方案继承的条目，需并入新方案）",
}


async def _collect_plan_history(db: AsyncSession, session, workspace: str) -> str:
    """plan-644: 收集本会话此前各轮计划需求全集（仅 plan 模式注入）。

    每轮输出：turn id / plan_status 语义 / 文档路径 / 该轮用户原始需求 / 文档正文。
    正文策略：未完结轮（proposed/confirmed/superseded）最近 3 轮给全文，更早
    与已完结轮（done/cancelled）仅首个 # 标题；总量上限
    settings.plan_history_inject_chars，超限时从最早轮次开始降级（保留
    状态行与需求行，正文提示用 fs_read 读取）。失败返回空串（非阻塞）。
    """
    if not workspace or session is None:
        return ""
    try:
        from pathlib import Path as _Path

        from sqlalchemy import select

        from app.persistence.models.task import Task
        from app.persistence.models.turn import Turn as _Turn
        res = await db.execute(
            select(_Turn).where(
                _Turn.session_id == session.id,
                _Turn.plan_doc_path.is_not(None),
            ).order_by(_Turn.id.asc())
        )
        plan_turns = list(res.scalars().all())
        if not plan_turns:
            return ""

        # 各轮用户原始需求：request task（title+description），缺则 turn 内首条用户消息
        req_map: dict[int, str] = {}
        task_res = await db.execute(
            select(Task).where(
                Task.session_id == session.id,
                Task.kind == "request",
                Task.turn_id.in_([t.id for t in plan_turns]),
            ).order_by(Task.id.asc())
        )
        for tk in task_res.scalars().all():
            if tk.turn_id is not None and tk.turn_id not in req_map:
                req_map[tk.turn_id] = f"{tk.title or ''}\n{tk.description or ''}".strip()[:1500]
        for t in plan_turns:
            if t.id in req_map:
                continue
            req_map[t.id] = ""
            try:
                from app.persistence.models.message import Message
                m_res = await db.execute(
                    select(Message).where(
                        Message.session_id == session.id,
                        Message.turn_id == t.id,
                        Message.sender_type == "user",
                        Message.deleted == False,  # noqa: E712 问题14: 排除已回滚软删
                    ).order_by(Message.id.asc()).limit(1)
                )
                m = m_res.scalars().first()
                if m is not None and isinstance(m.content, dict):
                    req_map[t.id] = str(m.content.get("text") or "")[:1500]
            except Exception:
                logger.debug("[context] Plan History 用户需求兜底失败 turn=%s", t.id, exc_info=True)

        root = _Path(workspace).resolve()
        # 未完结轮最近 3 轮给全文
        open_turns = [t for t in plan_turns if (t.plan_status or "") in ("proposed", "confirmed", "superseded")]
        full_idx = {t.id for t in open_turns[-3:]}

        blocks: list[str] = []
        for t in plan_turns:
            status = t.plan_status or "unknown"
            label = _PLAN_STATUS_LABELS.get(status, status)
            header = f"### Turn {t.id} [{status}] {t.plan_doc_path}（{label}）"
            req = req_map.get(t.id, "")
            req_part = f"用户需求：{req}" if req else "用户需求：（未能恢复）"
            body = ""
            try:
                target = (root / str(t.plan_doc_path)).resolve()
                if target.is_file() and root in target.parents:
                    text = target.read_text(encoding="utf-8", errors="replace")
                    if t.id in full_idx:
                        body = text[:3000]
                    else:
                        first = next((ln for ln in text.splitlines() if ln.startswith("#")), "")
                        body = first[:200] if first else "(无标题)"
            except OSError:
                body = "(读取失败)"
            blocks.append(f"{header}\n{req_part}\n文档：{body}" if body else f"{header}\n{req_part}")

        # 预算：从最新轮次向前保留完整块；更早轮次降级为状态行+需求行
        budget = max(1000, int(getattr(settings, "plan_history_inject_chars", 8000) or 8000))
        kept: set[int] = set()
        total = 0
        for i in range(len(blocks) - 1, -1, -1):
            if total + len(blocks[i]) <= budget:
                kept.add(i)
                total += len(blocks[i])
            else:
                break
        parts: list[str] = []
        for i, blk in enumerate(blocks):
            if i in kept:
                parts.append(blk)
            else:
                lines = blk.splitlines()
                header = lines[0] if lines else ""
                req_line = next((ln for ln in lines if ln.startswith("用户需求")), "")
                parts.append(f"{header}\n{req_line}\n（正文因注入预算截断，可 fs_read 该文档路径查看全文）")
        return "\n\n".join(parts)
    except Exception:
        logger.warning("[context] Plan History 收集失败(非阻塞)", exc_info=True)
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


async def _resolve_ta3_model_meta(db: AsyncSession, agent, session) -> dict | None:
    """会话/代理绑定的模型为 ta3 供应商时返回其远端元数据，否则 None。

    ta3 模型使用还原版系统提示词（远端 baseAgentSystemMessage + ta3 纪律段落 +
    当前项目流程规范）；其余模型沿用当前项目英文系统提示词。
    """
    try:
        model_id = getattr(session, "model_id", None) or getattr(agent, "model_id", None)
        if not model_id:
            return None
        from app.persistence.models.model_reg import Model, Provider

        model = await db.get(Model, model_id)
        if model is None or (getattr(model, "api_format", None) or "") != "ta3":
            return None
        provider = await db.get(Provider, model.provider_id) if model.provider_id else None
        if provider is None or (provider.api_format or "") != "ta3":
            return None
        return model.ta3_meta or {}
    except Exception:
        logger.debug("[context] ta3 模型元数据解析失败(非阻塞)", exc_info=True)
        return None


async def build_main_context(
    db: AsyncSession, *, agent, session, project, turn, user_message: str,
    attachments: list[dict] | None = None,
    multimodal: bool = False,
    enable_subagents: bool = True,
    plan_history: str = "",
    goal: dict | None = None,
    available_tools: set[str] | None = None,
    # plan-166-767: 请求携带的权威模型（切换模型后立即发送时优先），
    # 决定摘要阈值/注入预算按「目标模型窗口」计算。
    effective_model_id: int | None = None,
) -> ContextBundle:
    """构建主代理上下文。

    plan_history（plan-644）：本会话此前各轮计划需求全集，仅 plan 模式
    由调用方收集传入；非空时作为 developer 片段注入（多轮迭代零丢失的
    机制保证）。

    goal（plan-671）：会话目标快照 {text, turns_used}；目标激活时
    Current Goal 段为持久目标文本，本轮用户消息降级为 Current Task 段。

    available_tools（plan-147-674）：当前会话实际暴露给模型的工具名集合
    （经模式白名单与供应商伪装层过滤后）。None 表示未知（按全量处理）；
    集合中无 read_attachment 时附件引导文案降级为通用表述，避免提示词
    引导模型调用不存在的工具。
    """
    workspace = session.worktree_path or (project.path if project else "")
    # 沙箱模式解析（与 engine/run_agent_loop 同口径：项目配置 > 全局设置 > 默认）
    _sandbox = "workspace-write"
    try:
        from app.services import config_service
        _eff = await config_service.effective_config(db, project_path=workspace)
        _sandbox = str(_eff.get("sandbox_mode") or "workspace-write")
        from app.core.config import settings as _st
        if _sandbox == "workspace-write" and _st.sandbox_mode != "workspace-write":
            _sandbox = _st.sandbox_mode
    except Exception:
        logger.debug("[context] 沙箱模式读取失败，用 workspace-write", exc_info=True)
    # v23: ta3 供应商模型 → 还原式系统提示词（远端主体 + ta3 纪律 + 当前项目规范）
    ta3_meta = await _resolve_ta3_model_meta(db, agent, session)
    if ta3_meta is not None:
        from app.orchestration.prompts.ta3_fusion import build_ta3_system_prompt
        system_prompt = build_ta3_system_prompt(
            ta3_meta, workspace=workspace, enable_subagents=enable_subagents,
            sandbox_mode=_sandbox,
        )
        logger.info("[context] 会话 %s 使用 ta3 还原式系统提示词", session.id if session else "-")
    else:
        system_prompt = build_main_system_prompt(enable_subagents=enable_subagents)
    bundle = ContextBundle(
        system=system_prompt,
        instruction=user_message,
    )
    # v21: 主路径接入摘要系统 —— 未摘要历史超过窗口阈值(0.35×ctx)时先压缩为
    # LLM 摘要并落库（session.shared_context），再往下做窗口截断。
    # 修复：此前 maybe_summarize_main_session 只在旧群聊路径被调用，主 turn 路径
    # 完全无摘要兜底，超过窗口预算(0.30×ctx)的旧消息被静默丢弃。
    try:
        from app.orchestration.context_memory import (
            _resolve_leader_context_window, maybe_summarize_main_session,
        )
        _summary_window = await _resolve_leader_context_window(db, session, model_id=effective_model_id)
        await maybe_summarize_main_session(db, session, context_window=_summary_window)
    except Exception:
        logger.warning("[context] 主会话摘要更新失败(非阻塞)", exc_info=True)
    # 1. Current Goal（plan-671：目标激活时为持久目标，本轮消息降级为 Current Task）
    if goal and goal.get("text"):
        bundle.developer_parts.append(
            f"## Current Goal\n{goal['text'][:2000]}\n"
            f"（目标模式激活：持续朝该目标工作，完成时调用 goal_complete；已续跑 {goal.get('turns_used', 0)} 轮）"
        )
        bundle.developer_parts.append(f"## Current Task\n{user_message[:2000]}")
    else:
        bundle.developer_parts.append(f"## Current Goal\n{user_message[:2000]}")
    # 2. Working Directory & Tool Rules
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    # v1.2: 注入 shell 环境说明，避免 agent 用错 shell 语法（Get-ChildItem/grep/… 报错）
    ws_ctx += "\n\n" + shell_hint()
    bundle.developer_parts.append(ws_ctx)
    # 3. Git Repos（并入结构摘要）
    structure = await project_structure_brief(workspace)
    if structure:
        bundle.developer_parts.append(f"## Project Structure\n{structure}")
    # 4. Project Rules（工作区规则文档 + 用户设置的工作目录规则）
    rules_parts: list[str] = []
    _docs = await load_session_rules(workspace, project.rules_docs if project else None)
    if _docs:
        rules_parts.append(_docs)
    try:
        from app.orchestration.user_rules_loader import load_workdir_rules
        _wd = load_workdir_rules(workspace)
        if _wd:
            rules_parts.append(_wd)
    except Exception:
        logger.debug("[context] 工作目录规则加载失败(非阻塞)", exc_info=True)
    if rules_parts:
        bundle.developer_parts.append(f"## Project Rules\n{'\n\n'.join(rules_parts)}")
    # 4.1 Global Rules（用户全局规则，对所有项目生效，优先级最高）
    try:
        from app.orchestration.user_rules_loader import load_global_rules
        _gr = load_global_rules()
        if _gr:
            bundle.developer_parts.append(f"## Global Rules\n{_gr}")
    except Exception:
        logger.debug("[context] 全局规则加载失败(非阻塞)", exc_info=True)
    # 5. Session Memory（turn 摘要 + 记忆条目）
    # plan-644: Plan History（会话级计划需求全集）置于 Session Memory 之前--
    # 多轮 /plan 迭代时模型不可能遗忘未完成需求（机制保证，非纯提示词）
    if plan_history.strip():
        bundle.developer_parts.append(
            "## Plan History (all previous plan rounds of this session)\n"
            "The new plan document MUST cover every unexecuted item below and MUST NOT "
            "repeat items already implemented and delivered.\n\n" + plan_history
        )
    mem_summary = await _session_memory_summary(db, session.id)
    if mem_summary:
        bundle.developer_parts.append(f"## Session Memory\n{mem_summary}")
    memories = await _load_memories(db, session.id)
    if memories:
        bundle.developer_parts.append(f"## Your Memory (from previous tasks)\n{memories}")
    # v21: 注入主会话 LLM 摘要（被压缩的早期历史）——此前 shared_context 不落库
    # 且主路径不注入，压缩产物对主代理不可见；现在落库并注入，窗口截断的信息不丢失。
    _ctx = getattr(session, "shared_context", None) or {}
    if isinstance(_ctx, dict):
        _summary = (_ctx.get("summary") or "").strip()
        if _summary:
            bundle.developer_parts.append(
                f"## Session Summary (earlier conversation compressed)\n{_summary[:4000]}"
            )
        # v30: 注入压缩 checkpoint（context_compressor 落库的 SUMMARY 消息摘要）。
        # 与 shared_context.summary（context_memory 后台渐进摘要）不同，checkpoint 是
        # 按 token 预算选定范围的压缩产物，按压缩发生顺序注入，且只注入一次
        # （已注入的 compaction_id 记录在 _injected_compactions，跨轮不重复）。
        try:
            from app.persistence.models.message import Message as _Msg
            _compactions = _ctx.get("compactions") or []
            _injected = set(_ctx.get("injected_compactions") or [])
            _checkpoint_parts: list[str] = []
            _new_injected: list[str] = []
            for _cmp in _compactions:
                _cid = str(_cmp.get("compaction_id") or "")
                # v33: 已还原的压缩块不再注入 checkpoint（原文已回到上下文，重复注入冗余）
                if not _cid or _cid in _injected or _cmp.get("restored"):
                    continue
                _msg_id = _cmp.get("summary_message_id")
                if not _msg_id:
                    continue
                _m = await db.get(_Msg, _msg_id)
                if _m is None or not isinstance(_m.content, dict):
                    continue
                _text = (_m.content.get("text") or "").strip()
                if not _text:
                    continue
                _checkpoint_parts.append(_text[:3000])
                _new_injected.append(_cid)
            if _checkpoint_parts:
                bundle.developer_parts.append(
                    "## Conversation Checkpoints (compacted spans)\n"
                    + "\n\n".join(_checkpoint_parts)
                )
                if _new_injected:
                    _ctx = dict(_ctx)
                    _ctx["injected_compactions"] = list(_injected) + _new_injected
                    session.shared_context = _ctx
                    await db.flush()
                    logger.info(
                        "[context] session=%s 注入压缩 checkpoint %d 条并标记已注入",
                        session.id, len(_new_injected),
                    )
        except Exception:
            logger.debug("[context] 压缩 checkpoint 注入失败(非阻塞)", exc_info=True)

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

        context_window = await _resolve_leader_context_window(db, session, model_id=effective_model_id)
        window_budget = get_main_window_budget(context_window)

        # v6.4: shared_context 是动态属性，可能不存在，用 getattr 安全访问
        ctx = getattr(session, "shared_context", None) or {}
        if not isinstance(ctx, dict):
            ctx = {}
        summarized_ids = set(ctx.get("summarized_ids") or [])
        # v30: 压缩遮蔽消息（context_compressor 落库的 compacted_ids）不注入历史——
        # 其内容已被 checkpoint 摘要承载，重复注入等于未压缩。
        compacted_ids = set(ctx.get("compacted_ids") or [])

        # v6.4: 提高limit到2000，覆盖全部历史消息（原来200条会丢失早期对话）
        all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
        unsummarized = [m for m in all_msgs
                        if m.id not in summarized_ids and m.id not in compacted_ids]

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

        # v21: 移除 v6.5 注入的假历史标记消息（user "## Conversation History" +
        # assistant "Understood..."）——它们浪费 token 且干扰模型对消息角色的理解，
        # 对齐 deepseek-harness：历史直接以真实 user/assistant/tool 消息回放。

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
                    # v33: 历史附件同样注入绝对路径（read_attachment 对绝对/相对路径均可解析）
                    att_note = "\n".join(
                        f"- {a.get('filename') or '(未命名)'}: path=`{_attachment_abs_path(str(a.get('path') or ''))}`"
                        for a in atts if isinstance(a, dict) and a.get("path")
                    )
                    if att_note:
                        # plan-147-674: 工具集不含 read_attachment 时降级为通用表述
                        _read_hint = (
                            "如需内容请将附件 path 交给当前可用的文件读取工具读取"
                            if available_tools is not None and "read_attachment" not in available_tools
                            else "如需内容请调用 read_attachment 读取 path"
                        )
                        att_note = "（该消息附带附件：\n" + att_note + f"\n{_read_hint}）"
                if m.sender_type == "user":
                    # v14: 历史用户消息若带附件（文件地址），把路径一并注入，
                    # AI 可随时通过 read_attachment 回读附件内容；仅附件无文字的消息也注入
                    if not text and not att_note:
                        continue
                    # plan-156-739: 多模态模型历史消息重建时恢复图片 image_url 块——
                    # 否则首轮图片可见、第二轮起历史只剩路径文本，图片"消失"，
                    # 模型被迫再次走工具读图却只拿到元信息。限制复用注入常量。
                    _img_blocks = None
                    if multimodal and atts and isinstance(atts, list):
                        _img_blocks, _ = _load_inline_image_blocks(atts)
                        if not _img_blocks:
                            _img_blocks = None
                    text = f"{text}\n{att_note}" if att_note else text
                    _flush_agent_text()
                    _cm = _CM(role="user", content=text)
                    if _img_blocks:
                        _cm.content_blocks = _img_blocks
                    bundle.history.append(_cm)
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
            "[context] session=%s 注入历史消息 %d 条 (window=%dK, budget=%d tokens, summarized=%d, compacted=%d, recent=%d)",
            session.id, len(bundle.history),
            context_window // 1000, window_budget, len(summarized_ids), len(compacted_ids), len(recent),
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
    # v33: 注入绝对路径（_attachment_abs_path）——AI 直接拿到磁盘真实路径，
    # 不再猜测相对路径/上传目录；read_attachment 对绝对/相对路径均可解析。
    if attachments:
        att_lines = [
            f"- {a.get('filename') or '(未命名)'}（{_ATT_TYPE_LABEL.get(a.get('type'), a.get('mime_type') or '附件')}）: "
            f"path=`{_attachment_abs_path(str(a.get('path') or ''))}`"
            for a in attachments if isinstance(a, dict) and a.get("path")
        ]
        if att_lines:
            # plan-147-674: 工具集不含 read_attachment 时引导文案降级（不硬编码工具存在性）
            _has_ra = available_tools is None or "read_attachment" in available_tools
            hint = (
                "## 用户上传的附件\n"
                "用户消息附带了以下文件（path 为服务器磁盘绝对路径，可直接传给 read_attachment 读取）：\n"
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
                    "参数 path 使用上面的附件绝对路径，返回解析文本。"
                    if _has_ra else
                    "\n\n其他文件（docx/pdf/xlsx/txt 等）：请将附件 path 交给当前可用的文件读取工具读取。"
                )
            else:
                # plan-156-739: 非多模态模型收到图片附件时明确告知，避免模型
                # "假装看图 / 只报元信息 / 猜测尺寸"，并引导用户开启模型多模态。
                _has_img = any(
                    isinstance(a, dict) and a.get("type") == "image"
                    for a in attachments
                )
                if _has_img:
                    hint += (
                        "\n\n【注意】当前模型未启用多模态（模型设置 → 编辑模型 → 多模态开关），"
                        "图片仅提供上面的路径，无法直接查看像素内容；如需看图请在模型设置开启多模态后重试。"
                    )
                hint += (
                    "\n\n阅读方法：调用 read_attachment 工具读取，参数 path 直接使用上面的"
                    "附件绝对路径（不要改写、不要加引号），docx/pdf/xlsx/txt 等返回解析文本。"
                    if _has_ra else
                    "\n\n阅读方法：请将附件 path 交给当前可用的文件读取工具读取"
                    "（不要改写、不要加引号）。"
                )
            bundle.developer_parts.append(hint)
    return bundle


async def build_subagent_context(
    db: AsyncSession, *, agent, session, project, task,
    handoff_summary: str,
    original_request: str = "",
) -> ContextBundle:
    """构建子代理上下文（交接摘要 + 用户原始请求 + 主会话摘要 + 项目规则）。

    v19: 修复上下文继承断裂——子代理此前仅能看到 handoff 摘要，不知道用户
    原始诉求与主会话进展；现注入 original_request 与主会话 shared_context 摘要。
    """
    workspace = session.worktree_path or (project.path if project else "")
    bundle = ContextBundle(
        system=build_subagent_system_prompt(task.title or "", task.acceptance_criteria or ""),
        instruction=f"Start working on: {task.title}",
    )
    if original_request:
        bundle.developer_parts.append(f"## Original User Request\n{original_request[:2000]}")
    bundle.developer_parts.append(f"## Current Task\nTitle: {task.title}")
    if task.description:
        bundle.developer_parts.append(f"Description: {task.description}")
    if handoff_summary:
        bundle.developer_parts.append(f"## Handoff Summary (from main agent)\n{handoff_summary}")
    # v19: 主会话摘要（历史对话压缩产物），让子代理了解整体进展
    try:
        _ctx = getattr(session, "shared_context", None) or {}
        if isinstance(_ctx, dict):
            _summary = (_ctx.get("summary") or "").strip()
            if _summary:
                bundle.developer_parts.append(f"## Main Session Summary\n{_summary[:2000]}")
    except Exception:
        logger.debug("[context] 读取主会话摘要失败(非阻塞)", exc_info=True)
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    # v1.2: 注入 shell 环境说明（与主代理一致，防止用错 shell 语法）
    ws_ctx += "\n\n" + shell_hint()
    bundle.developer_parts.append(ws_ctx)
    rules = await load_session_rules(workspace, project.rules_docs if project else None)
    if rules:
        bundle.developer_parts.append(f"## Project Rules\n{rules}")
    try:
        from app.orchestration.user_rules_loader import load_global_rules, load_workdir_rules
        _wd = load_workdir_rules(workspace)
        if _wd:
            bundle.developer_parts.append(f"## Project Rules (workdir)\n{_wd}")
        _gr = load_global_rules()
        if _gr:
            bundle.developer_parts.append(f"## Global Rules\n{_gr}")
    except Exception:
        logger.debug("[context] 用户规则加载失败(非阻塞)", exc_info=True)
    return bundle
