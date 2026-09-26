"""plan-330-1648：子代理体系整改单测。

覆盖点：
- M1 运行模式按类型锁定：子代理不继承会话 permission_mode；计划文档纠偏只对主代理。
- M2 模型 / 思考深度配置链：SubagentProfile.reasoning_effort 列 + API 出入参 + 迁移声明。
- M4 双向通信：主代理→子代理 inbox 注入、子代理→主代理上报、提问等待与超时、同步禁用 wait。
- M5-a 落库时序：spawn 的 TOOL_CALL 在工具执行前落库并排空（卡片位置正确的充要条件）。
- M7 保活：未送达通知阻止 manager 被销毁。
"""
import asyncio
import inspect
from pathlib import Path

from app.orchestration import agent_loop as agent_loop_mod
from app.orchestration import subagent as subagent_mod
from app.orchestration.subagent import SubagentHandle, SubagentManager
from app.orchestration.subagent_tools import (
    SUBAGENT_OWN_TOOLS,
    SUBAGENT_TOOL_NAMES,
    SUBAGENT_TOOL_SCHEMAS,
    append_subagent_tools,
)

_ORCH = Path(__file__).resolve().parent.parent / "app" / "orchestration"


def _names(schemas: list[dict]) -> list[str]:
    return [s["function"]["name"] for s in schemas]


# ── M4: 工具可见性（主代理 vs 子代理）─────────────────────────

def test_send_to_subagent_visible_to_main_only():
    """send_to_subagent 属主代理工具；report_to_leader 属子代理工具（主代理不可见）。"""
    assert "send_to_subagent" in _names(SUBAGENT_TOOL_SCHEMAS)
    assert "report_to_leader" not in _names(SUBAGENT_TOOL_SCHEMAS)
    assert "report_to_leader" in _names(SUBAGENT_OWN_TOOLS)
    # 权限面板/白名单识别：两者都在工具名集合内（配置可被识别，不出现"配了不生效"）
    assert {"send_to_subagent", "report_to_leader"} <= SUBAGENT_TOOL_NAMES


def test_append_subagent_tools_includes_send_to_subagent():
    out = append_subagent_tools([], None)
    names = _names(out)
    assert "spawn_subagent" in names
    assert "send_to_subagent" in names
    assert "report_to_leader" not in names  # 主代理绝不可见子代理专用工具


def test_append_subagent_tools_idempotent():
    out = append_subagent_tools(append_subagent_tools([], None), None)
    assert _names(out).count("send_to_subagent") == 1


def test_explore_only_schema_keeps_background_only():
    """只读/计划模式（plan-64-289）：explore 仍隐藏（运行时强制只读），但保留 background——
    否则该模式下唯一路径必然同步阻塞（用户反馈：AI 倾向同步子代理，主 turn 被长时间挂住）。"""
    out = append_subagent_tools([], None, explore_only=True)
    spawn = next(s for s in out if s["function"]["name"] == "spawn_subagent")
    props = spawn["function"]["parameters"]["properties"]
    assert "explore" not in props
    assert "background" in props


# ── plan-64-289: 派发异步化与续跑正文固化 ────────────────────

def test_explore_and_background_are_composable():
    """plan-64-289：explore 只锁读写范围、background 只锁调度方式，两者可自由组合——
    旧「background 强制 explore=false」互斥已移除，同步分支只在 explore 且非 background 时走。"""
    text = (_ORCH / "agent_loop.py").read_text(encoding="utf-8")
    assert "background = bool(args.get(\"background\", False))" in text
    assert "if explore and not background:" in text
    assert "\n        if bool(args.get(\"background\", False)):\n            explore = False\n" not in text


def test_continuation_paths_persist_step_text():
    """plan-64-289：两条「无工具调用 + 续跑」路径必须先把本步正文固化落库再 continue——
    否则该段正文只留在前端流式缓冲、被下一轮 token.done 覆盖（等待汇报消失/错位）。"""
    text = (_ORCH / "agent_loop.py").read_text(encoding="utf-8")
    assert "plan-64-289: 本步正文先固化落库再续跑" in text
    assert "plan-64-289: 与子代理续跑同理" in text


# ── M1: 运行模式按类型锁定 ────────────────────────────────────

def test_run_agent_loop_accepts_mode_and_comm_channels():
    """run_agent_loop 必须提供"按类型锁定的运行模式"与"双向通信上下文"两个入口。"""
    sig = inspect.signature(agent_loop_mod.run_agent_loop)
    assert "subagent_mode" in sig.parameters
    assert sig.parameters["subagent_mode"].default is None
    assert "subagent_comm" in sig.parameters
    assert sig.parameters["subagent_comm"].default is None


def test_subagent_spawn_passes_mode_comm_and_inbox():
    """spawn 路径：按类型锁定模式 + 通信上下文 + 运行中指令注入通道，三者都要接上。"""
    text = (_ORCH / "subagent.py").read_text(encoding="utf-8")
    assert "subagent_mode=subagent_mode" in text
    assert "subagent_comm=" in text
    assert "injected_inputs_provider=" in text


def test_plan_correction_is_main_agent_only():
    """计划文档纠偏只对主代理生效——子代理不再被反复催写 ai/chatcoder-plan-*.md。"""
    text = (_ORCH / "agent_loop.py").read_text(encoding="utf-8")
    assert 'if (agent_kind == "main" and permission_mode == "plan"' in text


# ── M5-a: spawn 调用消息落库时序 ──────────────────────────────

def test_spawn_tool_call_persisted_before_dispatch():
    """spawn 的 TOOL_CALL 必须在 _run_subagent_tool 之前落库并排空（write-behind 屏障）。

    否则前端先收到 agent.started、却还没有调用消息，卡片只能走"未落位兜底"渲染
    —— 即用户反馈的"子代理卡片显示在 AI 文本上方"。
    """
    text = (_ORCH / "agent_loop.py").read_text(encoding="utf-8")
    marker = '── 子代理工具（主代理专用）──'
    assert marker in text
    seg = text[text.index(marker):]
    seg = seg[: seg.index("await _run_subagent_tool(")]
    assert "MsgType.TOOL_CALL.value" in seg
    assert "flush()" in seg


# ── M2: 模型 / 思考深度配置链 ─────────────────────────────────

def test_subagent_profile_has_reasoning_effort_column():
    from app.persistence.models.subagent_profile import SubagentProfile

    assert "reasoning_effort" in SubagentProfile.__table__.columns


def test_profile_api_exposes_reasoning_effort():
    from app.gateway.routers.subagents import SubagentProfileIn, _to_out
    from app.persistence.models.subagent_profile import SubagentProfile

    p = SubagentProfile(name="explore", reasoning_effort="high")
    assert _to_out(p)["reasoning_effort"] == "high"
    assert SubagentProfileIn(name="x", reasoning_effort="low").reasoning_effort == "low"
    # 留空 = 跟随会话（前端"跟随会话"选项对应 None）
    assert SubagentProfileIn(name="x").reasoning_effort is None


def test_migrations_declare_reasoning_effort():
    from app.persistence import migrations

    table = getattr(migrations, "_MIGRATIONS", None) or getattr(migrations, "MIGRATIONS")
    assert ("subagent_profiles", "reasoning_effort", "VARCHAR(20)") in table


def test_spawn_resolution_prefers_profile_then_session():
    """解析链：设置（profile）优先 → 会话 → 主代理；effort 同链路（None 回落全局默认）。"""
    text = (_ORCH / "agent_loop.py").read_text(encoding="utf-8")
    assert "_profile_model_id" in text
    assert "subagent_context.get(\"reasoning_effort\")" in text


# ── M4: 双向通信（manager 行为）───────────────────────────────

class _NeverDoneTask:
    """仅用于让 handle 看起来"仍在运行"（push_agent_input 的运行态判定）。"""

    def done(self) -> bool:
        return False


def _running_handle(mgr: SubagentManager, agent_id: int = 7) -> SubagentHandle:
    h = SubagentHandle(agent_id=agent_id, status="running")
    h.task = _NeverDoneTask()
    mgr._handles[agent_id] = h
    return h


def test_push_agent_input_requires_running_subagent():
    mgr = SubagentManager(session_id=1)
    assert mgr.push_agent_input(999, "hi") is False  # 不存在的子代理
    h = _running_handle(mgr, 7)
    assert mgr.push_agent_input(7, "narrow the scope") is True
    items = mgr.drain_agent_inputs(7)
    assert items and "narrow the scope" in items[0]["content"]
    assert mgr.drain_agent_inputs(7) == []  # 取走即清空，不重复注入
    h.status = "done"
    assert mgr.push_agent_input(7, "again") is False  # 已结束不可投递


async def test_report_to_leader_sync_refuses_wait():
    """同步子代理（主代理正阻塞等待）不得 wait=true——否则死锁，工具层直接拒绝。"""
    comm = {"manager": None, "agent_id": 1, "sync": True}
    out = await agent_loop_mod._run_report_to_leader(
        comm, {"message": "which API?", "kind": "question", "wait": True},
        session_id=1, turn_id=1, thread_id=1, agent_id=1, agent_name="sub", db=None,
    )
    assert "cannot wait for a reply" in out


async def test_wait_for_reply_timeout_then_delivery():
    mgr = SubagentManager(session_id=1)
    _running_handle(mgr, 7)
    # 无人答复 → 超时返回 None（调用方提示"按最佳判断继续"）
    assert await mgr.wait_for_reply(7, 0.05) is None

    async def _push_later() -> None:
        await asyncio.sleep(0.01)
        mgr.push_agent_input(7, "use approach B")

    task = asyncio.create_task(_push_later())
    reply = await mgr.wait_for_reply(7, 2.0)
    await task
    assert reply and "approach B" in reply


def test_notify_leader_uses_registered_callback():
    mgr = SubagentManager(session_id=1)
    seen: list[tuple[int, str, str]] = []
    mgr.set_leader_notify(lambda aid, msg, kind: seen.append((aid, msg, kind)))
    mgr.notify_leader(3, "halfway", "progress")
    assert seen == [(3, "halfway", "progress")]
    # 回调抛错不得影响子代理（静默降级）
    def _boom(aid, msg, kind):
        raise RuntimeError("boom")

    mgr.set_leader_notify(_boom)
    mgr.notify_leader(3, "again", "progress")  # 不抛异常即通过


# ── M7: 保活判定与失败可见 ────────────────────────────────────

def test_queue_leader_note_blocks_cleanup():
    """有未送达通知 → has_unclaimed_notes 为真，engine 据此保活 manager（跨 turn 送达）。"""
    mgr = SubagentManager(session_id=1)
    assert mgr.has_unclaimed_notes() is False
    mgr.queue_leader_note("【子代理进度上报 subagent#1】half done")
    assert mgr.has_unclaimed_notes() is True
    notes = mgr.drain_completions()
    assert notes and "half done" in notes[0]
    assert mgr.has_unclaimed_notes() is False


def test_cleanup_manager_if_idle_keeps_busy_manager():
    """M7: 仍有运行中子代理时保活 manager；空闲时才销毁（避免后台结果丢失）。"""
    from app.orchestration.engine import _cleanup_manager_if_idle

    session_id = 991001
    mgr = subagent_mod.get_subagent_manager(session_id)
    try:
        _running_handle(mgr, 501)
        _cleanup_manager_if_idle(session_id, mgr)
        assert subagent_mod.peek_subagent_manager(session_id) is mgr  # 忙碌 → 保留

        mgr._handles[501].status = "done"
        mgr._handles.pop(501)
        _cleanup_manager_if_idle(session_id, mgr)
        assert subagent_mod.peek_subagent_manager(session_id) is None  # 空闲 → 清理
    finally:
        subagent_mod.cleanup(session_id)


async def test_failed_broadcast_event_registered():
    """M7: 失败/取消要广播 subagent.failed（前端据此展示原因）。"""
    text = (_ORCH / "subagent.py").read_text(encoding="utf-8")
    assert '"subagent.failed"' in text
    assert "_broadcast_subagent_failed(" in text
