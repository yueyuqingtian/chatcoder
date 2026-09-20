"""子代理管理（v2：主代理按需 spawn，独立上下文 + 结果收集）。

简化实现：spawn 启动后台 agent_loop 任务；结果按 subagent_id 缓存于内存，
主代理通过 collect_results 工具轮询获取。持久化子代理线程消息于 messages(thread_id=agent.id)。
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.agent_loop import run_agent_loop
from app.orchestration.tools.registry import tool_registry
from app.persistence.database import async_session_factory, commit_with_retry

logger = logging.getLogger(__name__)


@dataclass
class SubagentHandle:
    agent_id: int
    task: object = None
    status: str = "running"  # running / done / failed
    summary: str = ""
    error: str = ""
    artifact_ids: list[int] = field(default_factory=list)
    # v20: 探索子代理的"结论"文本（只读探索任务的最终输出，主代理据此整合，不落线程消息）
    findings: str = ""
    # plan-248-1258 M6: 主代理可检视子代理上下文与轨迹（subagent_inspect）
    task_title: str = ""
    task_description: str = ""
    handoff_summary: str = ""
    # 执行轨迹：工具调用序列（tool name + 参数摘要 + 输出摘要），由 _run 回填
    trajectory: list[dict] = field(default_factory=list)
    # 结构化汇报字段（由子代理最终输出解析）
    files_touched: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    # 主代理可见的上下文快照（关键事实/约束，spawn 时传入）
    context_snapshot: dict = field(default_factory=dict)


class SubagentManager:
    """会话级子代理管理器。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self._handles: dict[int, SubagentHandle] = {}

    def spawn(self, db, *, agent, turn_id: int, task, handoff_summary: str,
              context_bundle, tool_schemas: list[dict], workspace: str,
              cancel_event: asyncio.Event | None = None, token_budget: int | None = None,
              task_title: str = "", task_description: str = "",
              context_snapshot: dict | None = None) -> int:
        """异步启动子代理 agent_loop，返回 subagent id。"""
        handle = SubagentHandle(
            agent_id=agent.id,
            task_title=task_title or getattr(task, "title", "") or "",
            task_description=task_description,
            handoff_summary=handoff_summary,
            context_snapshot=context_snapshot or {},
        )
        self._handles[agent.id] = handle

        async def _run():
            try:
                # 问题3: 子代理用独立 AsyncSession —— 不再与主代理共享 session，
                # 避免多任务交错 flush/commit 踩踏产生 SQLite database is locked / PendingRollbackError。
                async with async_session_factory() as s:
                    # v9: 子任务启动即广播 in_progress（提交任务创建 + 状态），
                    # 前端任务面板/右上角卡片实时展示新任务的拆分步骤与执行情况。
                    # 此前子任务创建后从不更新状态，前端看不到子任务步骤执行进度。
                    await _sync_task_status(s, self.session_id, task.id, "in_progress", None)
                    out = await run_agent_loop(
                        s, session_id=self.session_id, turn_id=turn_id,
                        agent=agent, context_messages=context_bundle.to_messages(),
                        tool_schemas=tool_schemas, workspace=workspace,
                        cancel_event=cancel_event, token_budget=token_budget,
                        task_id=getattr(task, "id", None), model_id=getattr(agent, "model_id", None),
                        # plan-19-82: 子代理压缩摘要同样跟随用户语言
                        reply_language=getattr(context_bundle, "reply_language", "auto"),
                    )
                    handle.status = "done" if out.kind == "message" else "failed"
                    handle.summary = out.text or ""
                    handle.error = out.error or ""
                    handle.artifact_ids = list(out.artifact_ids or [])
                    # v20: 探索子代理最终输出即结论文本（供主代理 wait 后直接整合）
                    handle.findings = out.text or ""
                    # plan-248-1258 M6: 解析结构化汇报（结果/变更文件/关键发现/风险）
                    _parse_structured_report(handle, out.text or "")
                    await _sync_task_status(
                        s, self.session_id, task.id,
                        "done" if handle.status == "done" else "failed",
                        (out.text or "")[:300] or None,
                        agent_id=agent.id,
                    )
            except asyncio.CancelledError:
                handle.status = "cancelled"
                handle.error = "用户中断"
                async with async_session_factory() as s:
                    await _sync_task_status(s, self.session_id, task.id, "cancelled", "用户中断", agent_id=agent.id)
                raise
            except Exception as e:
                handle.status = "failed"
                handle.error = str(e)
                async with async_session_factory() as s:
                    await _sync_task_status(s, self.session_id, task.id, "failed", f"执行异常: {str(e)[:200]}", agent_id=agent.id)
                logger.exception("[subagent] %s 异常", agent.id)
            # v956：子代理 loop 结束前 flush 其消息（与主代理共用 turn buffer）
            try:
                from app.persistence.write_behind import write_behind
                await write_behind.get(self.session_id, turn_id).flush()
            except Exception:
                logger.debug("[subagent] flush turn buffer failed agent=%s", agent.id, exc_info=True)
            logger.info("[subagent] %s 完成 status=%s", agent.id, handle.status)

        handle.task = asyncio.create_task(_run())
        return agent.id

    async def spawn_and_wait(self, db, *, agent, turn_id: int, task, handoff_summary: str,
                             context_bundle, tool_schemas: list[dict], workspace: str,
                             cancel_event: asyncio.Event | None = None,
                             token_budget: int | None = None,
                             task_title: str = "", task_description: str = "",
                             context_snapshot: dict | None = None) -> SubagentHandle:
        """同步启动并等待子代理完成，返回已填充结果的 handle。

        v20: 探索子代理（只读调研）用——主代理调用 spawn_subagent(explore=true) 后
        直接拿到结论文本（handle.findings），不必再轮询 collect_results。
        """
        handle_id = self.spawn(
            db, agent=agent, turn_id=turn_id, task=task,
            handoff_summary=handoff_summary, context_bundle=context_bundle,
            tool_schemas=tool_schemas, workspace=workspace,
            cancel_event=cancel_event, token_budget=token_budget,
            task_title=task_title, task_description=task_description,
            context_snapshot=context_snapshot,
        )
        handle = self._handles.get(handle_id)
        if handle is not None and handle.task is not None:
            await handle.task
        return handle

    def get(self, agent_id: int) -> SubagentHandle | None:
        return self._handles.get(agent_id)

    def results(self) -> list[dict]:
        """已完成的子代理结构化结果列表（plan-248-1258 M6 加强汇报）。"""
        out = []
        for aid, h in self._handles.items():
            if h.status in ("done", "failed"):
                out.append({
                    "agent_id": aid, "status": h.status,
                    "title": h.task_title,
                    "summary": h.summary, "error": h.error,
                    "findings": h.findings,
                    "files_touched": list(h.files_touched),
                    "risks": list(h.risks),
                })
        return out

    def pending_count(self) -> int:
        return sum(1 for h in self._handles.values() if h.status == "running")

    async def wait_all(self) -> None:
        """等待所有仍在运行的子代理结束（collect_results(wait=true) 用）。"""
        pending = [h.task for h in self._handles.values() if h.task is not None and not h.task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def inspect(self, agent_id: int, section: str = "all") -> dict | None:
        """返回某子代理的上下文/轨迹/结果（subagent_inspect 用）。"""
        h = self._handles.get(agent_id)
        if h is None:
            return None
        data: dict = {"agent_id": agent_id, "status": h.status}
        if section in ("all", "context"):
            data["context"] = {
                "title": h.task_title,
                "task_description": h.task_description,
                "handoff_summary": h.handoff_summary,
                "inherited_context": h.context_snapshot,
            }
        if section in ("all", "transcript"):
            data["transcript"] = h.trajectory
        if section in ("all", "result"):
            data["result"] = {
                "summary": h.summary,
                "findings": h.findings,
                "files_touched": h.files_touched,
                "risks": h.risks,
                "error": h.error,
            }
        return data

    def agent_ids(self) -> list[int]:
        return list(self._handles.keys())

    async def cancel_all(self) -> None:
        """取消并等待该会话所有尚未结束的子代理任务。"""
        pending = [h.task for h in self._handles.values() if h.task is not None and not h.task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for handle in self._handles.values():
            if handle.status == "running":
                handle.status = "cancelled"



# 会话级 manager 注册表
_managers: dict[int, SubagentManager] = {}


def get_subagent_manager(session_id: int) -> SubagentManager:
    if session_id not in _managers:
        _managers[session_id] = SubagentManager(session_id)
    return _managers[session_id]


def cleanup(session_id: int) -> None:
    _managers.pop(session_id, None)


# ── plan-248-1258 M6: 结构化汇报解析 ──

# 标题行识别：去 # 后为短行（<=72 字符）且命中关键词即视为分节标题。
# 用宽松匹配（而非整行精确）以兼容 "### Risks / Open Questions"、"## 变更文件：" 等变体。
_FILES_HEAD = re.compile(r"(变更文件|改动文件|files?\s*(?:touched|changed|modified)|files\s*[:：])", re.IGNORECASE)
_RISK_HEAD = re.compile(r"(风险|未决|待确认|risks?|blockers?|open\s+questions?)", re.IGNORECASE)
_OTHER_HEADS = re.compile(
    r"(result|summary|results|结论|关键发现|findings|key\s+findings|后续建议|next\s+steps)",
    re.IGNORECASE,
)
_FILE_RE = re.compile(r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|cs|cpp|c|h|rb|php|sql|md|json|yaml|yml|toml)")


def _parse_structured_report(handle: "SubagentHandle", text: str) -> None:
    """从子代理最终输出解析结构化字段（变更文件 / 风险）。

    子代理被引导输出分节汇报（结果/变更文件/关键发现/风险）；此处做宽容解析：
    识别到「变更文件」节时收集其中的文件路径，识别到「风险/未决」节时收集条目。
    解析失败不影响主流程（summary/findings 已保留全文）。
    """
    if not text:
        return
    try:
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            # 标题行：以 # 开头，或短行且以冒号结尾
            _is_head = line.startswith("#") or (len(line) <= 72 and line.endswith((":", "：")))
            _head_text = line.lstrip("#").strip()
            if _is_head and len(_head_text) <= 72:
                if _FILES_HEAD.search(_head_text):
                    section = "files"
                    continue
                if _RISK_HEAD.search(_head_text):
                    section = "risks"
                    continue
                if _OTHER_HEADS.search(_head_text):
                    section = "other"
                    continue
            if section == "files":
                for f in _FILE_RE.findall(line):
                    if f not in handle.files_touched:
                        handle.files_touched.append(f)
            elif section == "risks":
                item = line.lstrip("-*·0123456789.) ").strip()
                if item and len(item) > 3 and item != "None":
                    handle.risks.append(item[:200])
        # 未识别到「变更文件」节时，兜底从全文提取文件路径（限量，避免噪声）
        if not handle.files_touched:
            for f in _FILE_RE.findall(text)[:20]:
                if f not in handle.files_touched:
                    handle.files_touched.append(f)
    except Exception:  # noqa: BLE001
        logger.debug("[subagent] 结构化汇报解析失败(非阻塞)", exc_info=True)


async def _sync_task_status(db, session_id: int, task_id: int, status: str, note: str | None,
                            agent_id: int | None = None) -> None:
    """更新子任务状态并广播，前端任务面板据此实时刷新步骤与执行情况。

    先提交（子任务与主代理共享 session，创建任务的 flush 需随 commit 落库，
    否则前端 refreshTasks 通过 HTTP 查询时看不到新任务），再广播 task.updated。
    agent_id 传入时在终态（done/failed）附带 agent.completed 事件，
    前端消息流子代理卡片据此摘除转圈（此前仅靠 task.updated，探索期任务被置 running
    导致已完成卡片被 REST 回退成转圈）。
    """
    try:
        from app.orchestration.agent_events import broadcast
        from app.services import task_service
        # update_task_status 已经 WriteEngine 单写线程提交（无锁单写者），
        # 此处不再需要 async commit_with_retry（db 无未决写）。
        await task_service.update_task_status(db, task_id, status, note=note)
        await broadcast(session_id, {
            "event": "task.updated",
            "payload": {"task_id": task_id, "status": status, "note": note or ""},
        })
        if agent_id is not None and status in ("done", "failed", "cancelled"):
            await broadcast(session_id, {
                "event": "agent.completed",
                "payload": {"agent_id": agent_id, "status": status},
            })
    except Exception:
        logger.debug("[subagent] 子任务状态同步失败(非阻塞)", exc_info=True)
