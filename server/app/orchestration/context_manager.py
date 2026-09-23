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
    # plan-248-1258 M7: 任务边界提示（历史与新指令之间的 system 分隔，防旧任务复述）
    task_boundary: str = ""
    # plan-19-82: 本轮回复语言（zh/en/auto），由 build_*_context 按用户消息检测后填充；
    # engine 透传给 run_agent_loop，使压缩摘要 / checkpoint 文案与回复语言一致。
    reply_language: str = "auto"
    # 语言来源：global / project / user。提醒与系统提示必须用同一份裁决。
    reply_language_source: str = "user"
    # plan-19-82 增强（对齐 ZCode ContextBuilder 的通道化注入）：规则段独立成通道。
    # 此前规则与工具说明/结构摘要/记忆全部拼进同一条 developer 消息，规则被噪声稀释；
    # ZCode 把 userInstructions（AGENTS.md 等）放进独立的 meta_user 通道并加 OVERRIDE 裁决。
    # 这里把 Global/Project Rules 单独承载，避免与其它上下文挤在同一段落里。
    rules_parts: list[str] = field(default_factory=list)
    # 本轮规则锚点（紧贴用户消息）。单独保留一份：engine 的「确认执行」路径会整体
    # 重写 instruction（换成方案文档指令），需要把锚点原样拼回去而不是重新构造。
    rules_anchor: str = ""

    def to_messages(self) -> list[ChatMessage]:
        """组装 system + developer 段 + 历史 + user 指令。

        plan-19-82 增强：规则段独立为一条 developer 消息（紧随首条 developer 之后），
        避免与工具说明/结构/记忆平铺在同一条消息里被稀释——对齐 ZCode 把
        userInstructions 放进独立 meta_user 通道的做法。
        """
        messages = [ChatMessage(role="system", content=self.system)]
        # 规则通道优先落位：模型对"紧邻 system 的独立段落"注意力高于长段落中段。
        if self.rules_parts:
            messages.append(ChatMessage(
                role="developer",
                content="\n\n".join(self.rules_parts),
            ))
        if self.developer_parts:
            messages.append(ChatMessage(role="developer", content="\n\n".join(self.developer_parts)))
        messages.extend(self.history)
        # plan-248-1258 M7: 任务边界标记——历史与本轮新指令之间插入轻量 system 分隔。
        # 修复 grok 类模型"新任务时复述上一任务执行结果"：旧的长执行记录紧贴新短指令，
        # 模型倾向续写旧内容。显式声明"上一任务已结束、以下是新指令"可显著降低该倾向。
        if self.task_boundary and self.history:
            messages.append(ChatMessage(role="system", content=self.task_boundary))
        if self.instruction or self.instruction_blocks:
            messages.append(ChatMessage(
                role="user", content=self.instruction,
                content_blocks=self.instruction_blocks,
            ))
        return messages


# 注入 prompt 的技能清单上限（旧值 10 会静默截断更多技能；
# plan-230-1144 M1.2 放宽到 50，配合 skill_view 按需加载正文，列表本身只占少量 token）
_SKILLS_LIST_MAX = 50

# 行为引导段：此前 AI 只看得到技能名、拿不到正文（Skill.content 从未送达模型），
# 也没有任何读取手段（全仓库无 skill_view 类工具），"主动使用技能"无从谈起。
# 现配套 skill_view 工具，显式要求 AI 在任务匹配时先加载正文再动手。
_SKILLS_GUIDANCE = (
    "当任务与下列某个技能的描述或触发条件匹配时，必须先调用 skill_view(name=技能名) "
    "加载该技能的完整指令并遵照执行，不要凭技能名称猜测做法；"
    "不确定该用哪个技能时，先不带参数调用 skill_view 浏览全部技能。"
)


async def _load_skills_and_mcp(db: AsyncSession) -> tuple[str, str]:
    """全局技能/MCP 摘要（尽力而为，失败返回空）。"""
    skills_text = mcp_text = ""
    try:
        from app.services.skill_service import get_global_skills
        skills = await get_global_skills(db)
        if skills:
            parts = []
            for s in skills[:_SKILLS_LIST_MAX]:
                line = f"- {s.name}: {(s.description or '')[:200]}"
                if s.trigger:
                    line += f" [触发条件: {str(s.trigger)[:120]}]"
                # plan-308-1542 需求2：注入**绝对路径与父目录**——此前只有「名称: 描述」，
                # AI 即便想看技能正文/附带脚本，也无从知道技能装在哪（用户反馈"找不到技能目录"）。
                _path = (getattr(s, "path", None) or "").strip()
                if _path:
                    try:
                        from pathlib import Path as _P
                        _dir = str(_P(_path).parent)
                    except Exception:  # noqa: BLE001
                        _dir = ""
                    line += f" [路径: {_path}]"
                    if _dir:
                        line += f" [目录: {_dir}]"
                parts.append(line)
            if len(skills) > _SKILLS_LIST_MAX:
                parts.append(f"（另有 {len(skills) - _SKILLS_LIST_MAX} 个技能，用 skill_view 浏览）")
            skills_text = _SKILLS_GUIDANCE + "\n\n" + "\n".join(parts)
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


async def _load_memories(db: AsyncSession, session_id: int, project_id: int | None = None) -> str:
    """注入记忆条目（D8；plan-230-1144 M4.1 三层化渲染）。

    读取顺序 session → project → global；渲染时按作用域加前缀标签，
    让模型知道"这是本会话事实 / 本项目约定 / 全局规范"，据此调整遵循优先级。
    """
    try:
        from app.services.memory_service import load_memories
        entries = await load_memories(db, session_id, project_id=project_id)
        if entries:
            _tag = {"global": "[全局]", "project": "[项目]"}
            return "\n".join(
                f"- {_tag.get(e.scope or 'session', '')}{e.text}"
                if _tag.get(e.scope or "session") else f"- {e.text}"
                for e in entries
            )
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

# plan-1075: 未完结状态集合——正文全文优先注入的轮次
_PLAN_OPEN_STATUSES = ("proposed", "confirmed", "superseded")

# plan-1075: 注入头部继承规则行——继承与合并交给 AI 语义理解完成，
# 系统不做任何计划文档内容识别/文本匹配
_PLAN_HISTORY_RULE_HEADER = (
    "【继承规则】新方案必须完整覆盖以下所有未完结轮的未完成内容，"
    "并与本轮新增需求合并重新完整规划；已完结轮仅作背景参考，"
    "其已完成条目禁止重复列入新方案。条目表述可改写，语义必须在。"
)


async def _collect_plan_history(
    db: AsyncSession, session, workspace: str, *,
    summary_only: bool = False, exclude_turn_id: int | None = None,
) -> str:
    """plan-644: 收集本会话此前各轮计划需求全集（仅 plan 模式注入）。

    plan-1075 重构——"AI 语义继承"策略：系统不解析、不校验计划文档内容，
    只负责把历史材料完整喂进上下文；找未完成项、并入新需求、合并重规划
    全部由模型在生成新文档时完成。注入策略：

    - 未完结轮（proposed/confirmed/superseded）：状态行 + 用户需求 + 文档正文
      全文，仅受单轮上限 settings.plan_history_open_doc_chars（尾部截断并
      标注 fs_read 补齐）；预算不足时突破 settings.plan_history_inject_chars
      整体注入（上限提高为未完结轮全集 + 1000）——未完成内容零丢失优先于
      token 预算。
    - 已完结轮（done/cancelled）：状态行 + 用户需求 + 首个 # 标题（现状保留）；
      预算放不下时从最早的已完结轮开始整块丢弃。
    - summary_only=True（确认执行阶段复用）：所有轮仅状态行+用户需求行，
      不含文档正文，防止执行上下文被历史正文撑爆。

    每轮输出：turn id / plan_status 语义 / 文档路径 / 用户需求 / 正文。
    失败返回空串（非阻塞）。
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
        plan_turns = [
            t for t in res.scalars().all()
            if exclude_turn_id is None or t.id != exclude_turn_id
        ]
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
        open_doc_chars = max(
            2000, int(getattr(settings, "plan_history_open_doc_chars", 12000) or 12000)
        )

        def _read_doc(t) -> str | None:
            """读文档全文；不可读返回 None。"""
            try:
                target = (root / str(t.plan_doc_path)).resolve()
                if target.is_file() and root in target.parents:
                    return target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            return None

        # 组块（按 turn id 升序保持时间线）：未完结轮全文，其余摘要
        blocks: dict[int, str] = {}
        for t in plan_turns:
            status = t.plan_status or "unknown"
            label = _PLAN_STATUS_LABELS.get(status, status)
            header = f"### Turn {t.id} [{status}] {t.plan_doc_path}（{label}）"
            req = req_map.get(t.id, "")
            req_part = f"用户需求：{req}" if req else "用户需求：（未能恢复）"
            is_open = status in _PLAN_OPEN_STATUSES
            if summary_only or not is_open:
                # 摘要块：已完结轮附首个 # 标题；summary_only 一律只给状态行+需求行
                first = ""
                if not summary_only:
                    doc = _read_doc(t)
                    if doc is not None:
                        found = next((ln for ln in doc.splitlines() if ln.startswith("#")), "")
                        first = found[:200] if found else "(无标题)"
                blocks[t.id] = f"{header}\n{req_part}" + (f"\n文档：{first}" if first else "")
            else:
                # plan-1075: 未完结轮全文（单轮上限内），超限尾部截断并标注
                doc = _read_doc(t)
                if doc is None:
                    body = "(文档不存在或读取失败，可 fs_read 该路径确认)"
                elif len(doc) > open_doc_chars:
                    body = doc[:open_doc_chars] + "\n（正文因超限截断，可 fs_read 原文档补齐）"
                else:
                    body = doc
                blocks[t.id] = f"{header}\n{req_part}\n文档：\n{body}"

        if summary_only:
            parts = [_PLAN_HISTORY_RULE_HEADER] + [blocks[t.id] for t in plan_turns]
            return "\n\n".join(parts)

        # 预算分配（plan-1075 降级顺序反转）：未完结轮全文无条件保留；
        # 剩余空间给已完结轮摘要（最新优先保留），放不下的从最早的开始整块丢弃；
        # 未完结轮全集本身超出预算时，注入上限直接提高为"未完结轮全集 + 1000"
        open_set = {t.id for t in plan_turns if (t.plan_status or "") in _PLAN_OPEN_STATUSES}
        closed_turns = [t for t in plan_turns if t.id not in open_set]
        open_total = sum(len(blocks[t.id]) for t in plan_turns if t.id in open_set)
        budget = max(1000, int(getattr(settings, "plan_history_inject_chars", 8000) or 8000))
        if open_total + 1000 > budget:
            # plan-1075: 未完结轮全文优先于 token 预算——注入上限直接提高为
            # "未完结轮全集 + 1000"，剩余空间仍尽量给已完结轮摘要
            logger.info(
                "[context] Plan History 未完结轮全文 %d 字符超出预算 %d，突破预算整体注入",
                open_total, budget,
            )
            effective_budget = open_total + 1000
        else:
            effective_budget = budget
        remaining = effective_budget - open_total
        kept_closed: set[int] = set()
        for t in reversed(closed_turns):
            blk = blocks[t.id]
            if len(blk) <= remaining:
                kept_closed.add(t.id)
                remaining -= len(blk)
        parts = [_PLAN_HISTORY_RULE_HEADER]
        for t in plan_turns:
            if t.id in open_set or t.id in kept_closed:
                parts.append(blocks[t.id])
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


async def _symbol_index_hint(workspace: str) -> str:
    """plan-248-1258 M3.3: 生成符号索引状态提示（注入 developer 段）。

    - 已开启：报告文件数/符号数与更新时间，指示主动使用 symbol_search/outline；
    - 未开启：说明未索引，建议用户在设置-索引库开启（不自动建库）。
    """
    if not workspace:
        return ""
    import asyncio

    from app.services import symbol_index_manager as sim

    state = await asyncio.to_thread(sim.get_state, workspace)
    if not state.get("enabled"):
        return (
            "## Code Symbol Index\n"
            "Status: NOT ENABLED for this working directory.\n"
            "symbol_search / outline are unavailable until the user enables indexing "
            "(Settings → Index Library / 索引库). If the user asks for fast symbol lookup, "
            "tell them to enable it there."
        )
    status = state.get("status")
    if status == "indexing":
        return (
            "## Code Symbol Index\n"
            f"Status: INDEXING (workspace: {state.get('workspace')}). "
            "Prefer symbol_search/outline once ready; meanwhile use fs_grep/fs_read."
        )
    return (
        "## Code Symbol Index\n"
        f"Status: READY — {state.get('files', 0)} files / {state.get('symbols', 0)} symbols indexed.\n"
        "When locating functions, classes, or exploring a file's structure, PREFER "
        "symbol_search (by symbol name) and outline (file skeleton) before fs_grep/fs_read; "
        "they return file:line ranges you can feed directly into fs_read."
    )


async def _resolve_rule_language(workspace: str, project, user_lang: str) -> tuple[str, str]:
    """全局规则 > 项目规则 > 用户消息。规则读取失败时退回用户消息语言。"""
    from app.orchestration.prompts.language import resolve_reply_language
    from app.orchestration.user_rules_loader import load_global_rules, load_workdir_rules

    global_rules = ""
    workdir_rules = ""
    project_rules = ""
    try:
        global_rules = load_global_rules()
        workdir_rules = load_workdir_rules(workspace)
    except Exception:
        logger.debug("[context] 设置规则读取失败，语言退回用户消息", exc_info=True)
    try:
        project_rules = await load_session_rules(workspace, project.rules_docs if project else None)
    except Exception:
        logger.debug("[context] 项目规则文档读取失败，语言不采用该层", exc_info=True)
    return resolve_reply_language(
        user_lang,
        global_rules=global_rules,
        project_rules=project_rules,
        workdir_rules=workdir_rules,
    )


async def _resolve_session_language(db: AsyncSession, session_id: int | None,
                                    primary_text: str) -> str:
    """解析本轮回复语言（plan-19-82 步骤 7 边界处理）。

    规则：以本轮最新用户消息为准；本轮消息为空/纯附件（无文字）时，
    回退检索会话内**最近一条含文本的 user 消息**判定，避免误判为英文。
    """
    from app.orchestration.prompts.language import LANG_AUTO, detect_reply_language

    lang = detect_reply_language(primary_text)
    if lang != LANG_AUTO or not session_id:
        return lang
    try:
        from sqlalchemy import select

        from app.core.enums import MsgType
        from app.persistence.models.message import Message

        res = await db.execute(
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.thread_id.is_(None),
                Message.sender_type == "user",
                Message.msg_type == MsgType.TEXT.value,
                Message.deleted == False,  # noqa: E712 —— 排除已回滚软删
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(10)
        )
        for m in res.scalars().all():
            c = m.content if isinstance(m.content, dict) else {}
            text = str(c.get("text") or "")
            # 跳过目标续跑轮的系统提醒（其语言不代表用户语言）
            if c.get("goal_continuation"):
                continue
            lang = detect_reply_language(text)
            if lang != LANG_AUTO:
                return lang
    except Exception:
        logger.debug("[context] 语言回退检索失败(非阻塞)", exc_info=True)
    return LANG_AUTO


async def build_main_context(
    db: AsyncSession, *, agent, session, project, turn, user_message: str,
    attachments: list[dict] | None = None,
    multimodal: bool = False,
    enable_subagents: bool = True,
    # plan-19-82 步骤7: 语言判定专用文本。默认 None=用 user_message；
    # 目标续跑轮的系统提醒是写死中文，不能代表用户语言，此时传 "" 触发
    # 回退检索「最近一条真实用户消息」判定。
    language_text: str | None = None,
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
    # v7: 非 plan 模式下裁剪系统提示词的规划工作流，避免模型自发写计划文档索要确认
    _perm_mode = str(getattr(session, "permission_mode", None) or "default")
    _is_plan_mode = _perm_mode == "plan"
    # plan-19-82: 回复语言由「本轮最新用户消息」决定（与界面语言设置无关）。
    # 空消息/纯附件场景回退检索最近一条含文本的 user 消息，避免误判。
    _lang_src = user_message if language_text is None else language_text
    _user_lang = await _resolve_session_language(db, getattr(session, "id", None), _lang_src)
    _reply_lang, _lang_source = await _resolve_rule_language(workspace, project, _user_lang)

    if ta3_meta is not None:
        from app.orchestration.prompts.ta3_fusion import build_ta3_system_prompt
        system_prompt = build_ta3_system_prompt(
            ta3_meta, workspace=workspace, enable_subagents=enable_subagents,
            sandbox_mode=_sandbox, language=_reply_lang,
            language_source=_lang_source,
        )
        logger.info("[context] 会话 %s 使用 ta3 还原式系统提示词", session.id if session else "-")
    else:
        system_prompt = build_main_system_prompt(enable_subagents=enable_subagents,
                                                 plan_flow_enabled=_is_plan_mode,
                                                 language=_reply_lang,
                                                 language_source=_lang_source)
    bundle = ContextBundle(
        system=system_prompt,
        instruction=user_message,
        reply_language=_reply_lang,
        reply_language_source=_lang_source,
    )
    # v16（用户要求）：移除重建时的静默渐进摘要 —— 重建不再改写历史。
    # 此前按 0.85×窗口（且窗口口径可能与实际调用模型不一致）静默摘要并落库
    # summarized_ids，用户发新任务时看到占用莫名回退（52%→48%）、历史被悄悄改写。
    # 现在压缩只保留一条通道：占用超过「设置-常规设置」的压缩阈值后，由
    # agent_loop 走落库式压缩（前端压缩卡片、可恢复）。
    # 1. Current Goal（plan-671：目标激活时为持久目标，本轮消息降级为 Current Task）
    if goal and goal.get("text"):
        bundle.developer_parts.append(
            f"## Current Goal\n{goal['text'][:2000]}\n"
            f"（目标模式激活：持续朝该目标工作，完成时调用 goal_complete；已续跑 {goal.get('turns_used', 0)} 轮）"
        )
        bundle.developer_parts.append(f"## Current Task\n{user_message[:2000]}")
    else:
        bundle.developer_parts.append(f"## Current Goal\n{user_message[:2000]}")
    # 1.1 Reply Language（plan-19-82）：显式语言锚点，紧随 Current Goal。
    # 语言纪律已作为系统提示首尾双锚注入，这里再在注意力最高处落一行单锚，降低漂移。
    from app.orchestration.prompts.language import build_language_pin_line
    bundle.developer_parts.append(build_language_pin_line(_reply_lang, source=_lang_source))
    # 1.2 Rule Documents（plan-19-82 步骤 4）：规则段**前移**到 developer 段最前部并加 MANDATORY 语义。
    # 现状（本轮改造前）规则在第 4/4.1 位且标题无强制语义，模型容易忽略 → 遵循度不足。
    # 加载提前到此，顺序：Global Rules（优先级最高）→ Project Rules（工作区文档 + 工作目录规则）。
    # plan-19-82 增强：规则改走独立通道（bundle.rules_parts → 单独一条 developer 消息），
    # 避免与工具说明/结构摘要/记忆平铺在同一条消息里被稀释；并附上冲突优先级裁决
    # （对齐 ZCode `# agentsMd` 的 "OVERRIDE any default behavior" 语义）。
    _rule_fragments: list[str] = []
    # 冲突优先级裁决：用户全局规则 > 项目规则文档 > 内置方法论（与系统提示口径一致）
    _rule_fragments.append(
        "## Rule Documents — MANDATORY\n"
        "The rules below OVERRIDE default behavior and MUST be followed exactly as written. "
        "Priority when they conflict: user Global Rules > project rule documents "
        "(AGENTS.md / CLAUDE.md / .cursorrules / CODEBUDDY.md / QODER.md / .trae/rules / "
        "GEMINI.md / .windsurfrules / .github/instructions …) > the built-in methodology."
    )
    try:
        from app.orchestration.user_rules_loader import load_global_rules_labeled
        _gr = load_global_rules_labeled()
        if _gr:
            _rule_fragments.append(f"## Global Rules (MANDATORY — highest priority)\n{_gr}")
    except Exception:
        logger.debug("[context] 全局规则加载失败(非阻塞)", exc_info=True)
    _rules_parts: list[str] = []
    try:
        _docs = await load_session_rules(workspace, project.rules_docs if project else None)
        if _docs:
            _rules_parts.append(_docs)
    except Exception:
        logger.debug("[context] 工作区规则文档加载失败(非阻塞)", exc_info=True)
    try:
        from app.orchestration.user_rules_loader import load_workdir_rules_labeled
        _wd = load_workdir_rules_labeled(workspace)
        if _wd:
            _rules_parts.append(_wd)
    except Exception:
        logger.debug("[context] 工作目录规则加载失败(非阻塞)", exc_info=True)
    if _rules_parts:
        _rule_fragments.append(
            "## Project Rules (MANDATORY)\n" + "\n\n".join(_rules_parts)
        )
    else:
        _rule_fragments.append(
            "## Project Rules\n(未检测到 AGENTS.md / CLAUDE.md 等项目规则文档。"
            "如工作区存在约定，请按既有代码风格与目录结构执行。)"
        )
    bundle.rules_parts.extend(_rule_fragments)
    # 1.3 本轮规则锚点：让规则**紧贴本轮用户消息**出现。
    # 用户反馈"规则遵循度不够"——规则段虽在 developer 段最前部，但与本轮指令之间隔着
    # 工具说明/结构摘要/记忆/历史消息，长会话下模型读到用户消息时规则已被稀释。
    # 这里把一条轻量锚点放进 instruction 前置，点名本轮**实际加载**的规则文档，
    # 使"读指令"与"遵循规则"在同一注意力窗口内完成。
    try:
        from app.orchestration.prompts import build_rules_anchor
        from app.orchestration.rules_loader import list_rules_doc_names
        _doc_names = await list_rules_doc_names(workspace, project.rules_docs if project else None)
        bundle.rules_anchor = build_rules_anchor(
            _reply_lang, _doc_names, language_source=_lang_source,
        )
        if bundle.instruction:
            bundle.instruction = bundle.rules_anchor + "\n\n" + bundle.instruction
    except Exception:
        logger.debug("[context] 规则锚点注入失败(非阻塞)", exc_info=True)
    # 2. Working Directory & Tool Rules
    ws_ctx = f"Working directory: {workspace}"
    ws_ctx += (
        "\n\n## Tool Usage Rules\n"
        "Use tools via structured function calls. Never describe tool actions in natural language.\n"
        f"All paths are relative to the working directory: {workspace}."
    )
    # v1.2: 注入 shell 环境说明，避免 agent 用错 shell 语法（Get-ChildItem/grep/… 报错）
    ws_ctx += "\n\n" + shell_hint()
    # plan-248-1258 M3.3: 注入代码符号索引状态——让 AI 在会话中自动识别项目是否已索引，
    # 并在探索代码/文件/函数时主动使用 symbol_search / outline。
    try:
        _idx_hint = await _symbol_index_hint(workspace)
        if _idx_hint:
            ws_ctx += "\n\n" + _idx_hint
    except Exception:
        logger.debug("[context] 符号索引状态注入失败(非阻塞)", exc_info=True)
    bundle.developer_parts.append(ws_ctx)
    # 3. Git Repos（并入结构摘要）
    structure = await project_structure_brief(workspace)
    if structure:
        bundle.developer_parts.append(f"## Project Structure\n{structure}")
    # plan-19-82: 原「4. Project Rules / 4.1 Global Rules」已前移至 developer 段最前部
    # （见上方 1.2 Rule Documents，标题带 MANDATORY 并统一命名），此处不再重复注入。
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
    # plan-230-1144 M4.1: 传 project_id——三层记忆（会话→项目→全局）合并注入
    memories = await _load_memories(db, session.id, project_id=getattr(project, "id", None))
    if memories:
        bundle.developer_parts.append(f"## Your Memory (from previous tasks)\n{memories}")
    # v21: 注入主会话 LLM 摘要（被压缩的早期历史）——此前 shared_context 不落库
    # 且主路径不注入，压缩产物对主代理不可见；现在落库并注入，窗口截断的信息不丢失。
    _ctx = getattr(session, "shared_context", None) or {}
    if isinstance(_ctx, dict):
        _summary = (_ctx.get("summary") or "").strip()
        if _summary:
            # plan-19-82: 标题按本轮语言生成，避免英文标题污染中文会话的回复语言
            _sum_title = ("## Session Summary（较早对话已压缩）" if _reply_lang == "zh"
                          else "## Session Summary (earlier conversation compressed)")
            bundle.developer_parts.append(f"{_sum_title}\n{_summary[:4000]}")
        # v30: 注入压缩 checkpoint（context_compressor 落库的 SUMMARY 消息摘要）。
        # 与 shared_context.summary（context_memory 后台渐进摘要）不同，checkpoint 是
        # 按 token 预算选定范围的压缩产物，按压缩发生顺序注入，且只注入一次
        # （已注入的 compaction_id 记录在 _injected_compactions，跨轮不重复）。
        # plan-19-82: ①标题按本轮语言生成（中文会话用中文标题，避免英文标题污染回复语言）；
        #             ②追加「按需回看」指引（含块 index/compaction_id），让 AI 知道可按需检索；
        #             ③跳过 merged（已滚动合并）的块——其内容已并入较新 checkpoint，
        #               原文仍可经 compaction_view 按需回看，不重复注入以防固定开销膨胀。
        try:
            from app.persistence.models.message import Message as _Msg
            _compactions = _ctx.get("compactions") or []
            _injected = set(_ctx.get("injected_compactions") or [])
            _checkpoint_parts: list[str] = []
            _new_injected: list[str] = []
            _index_lines: list[str] = []
            for _cmp in _compactions:
                _cid = str(_cmp.get("compaction_id") or "")
                # v33: 已还原的压缩块不再注入 checkpoint（原文已回到上下文，重复注入冗余）
                if not _cid or _cid in _injected or _cmp.get("restored") or _cmp.get("merged"):
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
                _ci = _cmp.get("index") or "-"
                _index_lines.append(f"#{_ci} (compaction_id={_cid})")
            if _checkpoint_parts:
                if _reply_lang == "zh":
                    _title = "## Conversation Checkpoints（已压缩的历史片段）"
                    _howto = (
                        "以上检查点只是压缩摘要。若需要其中被压缩段落的细节，**按需**回看："
                        "先调用 `compaction_index` 列出压缩块，再用 `compaction_view` 传入 index 或 "
                        "compaction_id（可带 keyword / offset / limit，单条全文用 full=true），"
                        "或用 `memory_search` 检索压缩源。**严禁一次性全量拉取压缩前历史**——"
                        "那会重新撑爆上下文。可用块：" + "、".join(_index_lines)
                    )
                else:
                    _title = "## Conversation Checkpoints (compacted spans)"
                    _howto = (
                        "The checkpoints above are summaries only. For details inside a compacted "
                        "span, recover **on demand**: call `compaction_index` to list blocks, then "
                        "`compaction_view` with `index` or `compaction_id` (optionally `keyword` / "
                        "`offset` / `limit`; single full text via `full=true`), or use `memory_search` "
                        "against the compaction source. **Never bulk-load the pre-compaction "
                        "history** — that defeats compaction. Available blocks: "
                        + ", ".join(_index_lines)
                    )
                bundle.developer_parts.append(_title + "\n" + "\n\n".join(_checkpoint_parts)
                                              + "\n\n" + _howto)
                if _new_injected:
                    _ctx = dict(_ctx)
                    _ctx["injected_compactions"] = list(_injected) + _new_injected
                    # checkpoint 注入状态经写引擎单写线程提交（不持有 async 写事务）
                    from app.persistence.database import run_write_locked

                    def _p(s):
                        from app.persistence.models.message import Session as _Sess
                        row = s.get(_Sess, session.id)
                        if row is not None:
                            row.shared_context = _ctx
                        s.commit()

                    await run_write_locked(_p, label="context.inject_checkpoint")
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

        # plan-248-1258 M7: 任务边界判定——上一任务留下大段执行记录且本轮指令较短时，
        # 插入分隔提示，抑制 grok 类模型"复述上一任务结果"的倾向。
        try:
            _last_asst = next((m for m in reversed(bundle.history) if m.role == "assistant"), None)
            _prev_len = len(_last_asst.content or "") if _last_asst is not None else 0
            _cur_len = len((user_message or "").strip())
            if _prev_len > 1200 and 0 < _cur_len < 600:
                bundle.task_boundary = (
                    "The previous task has ENDED. The user message below starts a NEW task. "
                    "Do NOT restate, summarize, or continue the previous task's results — "
                    "act only on the new instruction."
                )
        except Exception:  # noqa: BLE001
            logger.debug("[context] 任务边界判定失败(非阻塞)", exc_info=True)

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
    """构建子代理上下文（plan-330-1648 M3：子代理 = 受限的新会话）。

    与主会话上下文（build_main_context）**逐项对齐**：同一规则通道（Global / Project
    MANDATORY）、同一语言锚点与语言纪律、同一工作目录/工具规则/shell 提示/符号索引提示、
    同一项目结构摘要。隔离边界仅体现在替代项：
      - 「原始用户请求」替代主代理的 Current Goal；
      - 「交接摘要」说明子任务来源与范围；
      - 「主会话摘要」替代历史消息窗口（子代理不加载完整对话历史）。

    v19: 修复上下文继承断裂——子代理此前仅能看到 handoff 摘要，不知道用户
    原始诉求与主会话进展；现注入 original_request 与主会话 shared_context 摘要。
    plan-19-82: ①语言跟随用户消息（子代理汇报不得被英文任务描述带偏）；
                ②规则段前移并与主代理统一为 MANDATORY 命名。
    """
    workspace = session.worktree_path or (project.path if project else "")
    # plan-19-82: 语言以用户原始请求为准（缺则回退任务标题/描述）
    _lang_text = original_request or f"{task.title or ''}\n{task.description or ''}"
    _user_lang = await _resolve_session_language(db, getattr(session, "id", None), _lang_text)
    _reply_lang, _lang_source = await _resolve_rule_language(workspace, project, _user_lang)
    bundle = ContextBundle(
        system=build_subagent_system_prompt(task.title or "", task.acceptance_criteria or "",
                                            language=_reply_lang, language_source=_lang_source),
        # plan-19-82: instruction 由写死英文改为语言中立，避免把子代理汇报语言带向英文
        instruction=f"Start working on the assigned task: {task.title}",
        reply_language=_reply_lang,
        reply_language_source=_lang_source,
    )
    # plan-330-1648 M3: 与主代理同口径——本轮规则锚点前置到 instruction，让“读指令”
    # 与“遵循规则”落在同一注意力窗口（锚点会点名本轮实际加载的规则文档）。
    try:
        from app.orchestration.prompts import build_rules_anchor
        from app.orchestration.rules_loader import list_rules_doc_names
        _doc_names = await list_rules_doc_names(workspace, project.rules_docs if project else None)
        bundle.rules_anchor = build_rules_anchor(
            _reply_lang, _doc_names, language_source=_lang_source,
        )
        if bundle.instruction:
            bundle.instruction = bundle.rules_anchor + "\n\n" + bundle.instruction
    except Exception:
        logger.debug("[context] 子代理规则锚点注入失败(非阻塞)", exc_info=True)
    # plan-330-1648 M3: 语言锚点行（与主代理同一行文与位置语义——紧贴任务指令之前）
    from app.orchestration.prompts.language import build_language_pin_line
    bundle.developer_parts.append(build_language_pin_line(_reply_lang, source=_lang_source))
    if original_request:
        bundle.developer_parts.append(f"## Original User Request\n{original_request[:2000]}")
    bundle.developer_parts.append(f"## Current Task\nTitle: {task.title}")
    if task.description:
        bundle.developer_parts.append(f"Description: {task.description}")
    if handoff_summary:
        bundle.developer_parts.append(f"## Handoff Summary (from main agent)\n{handoff_summary}")
    # plan-19-82: 规则段前移并统一命名（与主代理同口径：Global → Project）
    # plan-19-82 增强：改走独立规则通道（与主代理一致），避免与工具说明平铺稀释。
    _rule_fragments: list[str] = [
        "## Rule Documents — MANDATORY\n"
        "The rules below OVERRIDE default behavior and MUST be followed exactly as written. "
        "Priority when they conflict: user Global Rules > project rule documents "
        "(AGENTS.md / CLAUDE.md / .cursorrules / CODEBUDDY.md / QODER.md / .trae/rules / "
        "GEMINI.md / .windsurfrules / .github/instructions …) > the built-in methodology."
    ]
    try:
        from app.orchestration.user_rules_loader import load_global_rules_labeled, load_workdir_rules_labeled
        _gr = load_global_rules_labeled()
        if _gr:
            _rule_fragments.append(f"## Global Rules (MANDATORY — highest priority)\n{_gr}")
        _rules_parts: list[str] = []
        _docs = await load_session_rules(workspace, project.rules_docs if project else None)
        if _docs:
            _rules_parts.append(_docs)
        _wd = load_workdir_rules_labeled(workspace)
        if _wd:
            _rules_parts.append(_wd)
        if _rules_parts:
            _rule_fragments.append(
                "## Project Rules (MANDATORY)\n" + "\n\n".join(_rules_parts)
            )
        else:
            # plan-330-1648 M3: 与主代理同口径的兑底文案（无规则文档时明确告知）
            _rule_fragments.append(
                "## Project Rules\n(未检测到 AGENTS.md / CLAUDE.md 等项目规则文档。"
                "如工作区存在约定，请按既有代码风格与目录结构执行。)"
            )
    except Exception:
        logger.debug("[context] 子代理规则加载失败(非阻塞)", exc_info=True)
    bundle.rules_parts.extend(_rule_fragments)
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
    # plan-330-1648 M3: 符号索引状态提示（与主代理一致，鼓励用 symbol_search / outline 探索）
    try:
        _idx_hint = await _symbol_index_hint(workspace)
        if _idx_hint:
            ws_ctx += "\n\n" + _idx_hint
    except Exception:
        logger.debug("[context] 子代理符号索引提示注入失败(非阻塞)", exc_info=True)
    bundle.developer_parts.append(ws_ctx)
    # plan-330-1648 M3: 项目结构摘要（与主代理一致，子代理不必自行摸索目录布局）
    try:
        structure = await project_structure_brief(workspace)
        if structure:
            bundle.developer_parts.append(f"## Project Structure\n{structure}")
    except Exception:
        logger.debug("[context] 子代理项目结构注入失败(非阻塞)", exc_info=True)
    # plan-19-82: 原「子代理规则块尾部重复注入（Project Rules (workdir) / Global Rules）」
    # 已统一到上方前移的 MANDATORY 规则段，此处不再重复注入。
    # plan-248-1258 M6: 结构化汇报要求——主代理据此精准整合（此前 free-form 汇报信息量不足）。
    # v36 (plan-321-1600 M1): 契约统一为六节——新增 Verification（验证动作与结果）与
    # Acceptance（逐条对照验收标准），补上此前“缺验收闭环/缺验证证据”的缺口；
    # 原文案不得再出现不存在的 report_to_leader 工具（子代理工具集里没有它）。
    bundle.developer_parts.append(
        "## Report Back (required)\n"
        "When you finish, reply with a STRUCTURED report so the main agent can integrate precisely. "
        "Use exactly these section headings, in this order:\n"
        "### Result\nOne-paragraph outcome of the subtask.\n"
        "### Files Touched\nBullet list of every file you created or modified (full relative paths). "
        "Write 'None' if you changed nothing.\n"
        "### Key Findings\nBullet list of the concrete facts the main agent must know "
        "(with file:line evidence where relevant).\n"
        "### Verification\nWhat you actually ran or checked to validate the work "
        "(commands, tests, diff review) and its outcome. Write 'None' if nothing was verifiable.\n"
        "### Acceptance\nGo through the acceptance criteria one by one and state met / not met, with evidence.\n"
        "### Risks / Open Questions\nBullet list of blockers, uncertainties, or follow-ups "
        "(write 'None' if clean).\n"
        "Be specific and concise — no filler. Never invent results you did not verify."
    )
    return bundle
