"""v39 子代理完成唤醒：终态回调 / 会话运行计数 / 空闲唤醒调度单测。

覆盖点：
- notify_finished 调用注册回调；无回调 / 回调异常均静默降级（不影响子代理收尾）。
- 会话运行 turn 计数：enter/leave 配对、归零移除（防注册表累积）。
- _on_subagent_finished：仍有子代理在跑不调度；会话忙不调度；空闲且有待送达
  通知才调度（在途防重）；开关关闭不调度。
- _wakeup_turn_for_subagents：无 manager（无待送达通知）时直接退出、占位清理。
- 广播载荷与前端契约：session.completed 携带 subagent_pending；新事件登记。
"""
import asyncio
import inspect
from pathlib import Path

from app.orchestration import engine as engine_mod
from app.orchestration import subagent as subagent_mod
from app.orchestration.subagent import SubagentHandle, SubagentManager

_ROOT = Path(__file__).resolve().parent.parent.parent


# ── 终态回调 ─────────────────────────────────────────────

def test_notify_finished_invokes_callback():
    mgr = SubagentManager(session_id=1)
    seen: list[int] = []
    mgr.set_finish_notify(lambda h: seen.append(h.agent_id))
    mgr.notify_finished(SubagentHandle(agent_id=7, status="done"))
    assert seen == [7]


def test_notify_finished_without_callback_safe():
    mgr = SubagentManager(session_id=1)
    mgr.notify_finished(SubagentHandle(agent_id=8))  # 无回调：不抛异常即通过


def test_notify_finished_callback_error_swallowed():
    mgr = SubagentManager(session_id=1)

    def _boom(_h):
        raise RuntimeError("boom")

    mgr.set_finish_notify(_boom)
    mgr.notify_finished(SubagentHandle(agent_id=9))  # 回调抛错不影响子代理收尾


def test_subagent_run_notifies_finished_on_terminal_state():
    """子代理 _run 收尾必须调用 notify_finished（否则完成通知无人接力调度唤醒）。"""
    src = inspect.getsource(SubagentManager.spawn)
    assert "self.notify_finished(handle)" in src


# ── 会话运行计数 ─────────────────────────────────────────

def test_session_turn_counter_pairs_and_cleans():
    sid = 987654
    assert engine_mod._session_turn_busy(sid) is False
    engine_mod._session_turn_enter(sid)
    assert engine_mod._session_turn_busy(sid) is True
    engine_mod._session_turn_enter(sid)
    engine_mod._session_turn_leave(sid)
    assert engine_mod._session_turn_busy(sid) is True
    engine_mod._session_turn_leave(sid)
    assert engine_mod._session_turn_busy(sid) is False
    assert sid not in engine_mod._running_session_turns  # 归零即移除
    engine_mod._session_turn_enter(None)   # None 安全（execute 路径 _session_id 兜底）
    engine_mod._session_turn_leave(None)
    engine_mod._session_turn_leave(sid)    # 多余 leave 不得残留负数键
    assert sid not in engine_mod._running_session_turns


# ── 空闲唤醒调度 ─────────────────────────────────────────

async def test_on_subagent_finished_no_schedule_while_pending():
    """仍有子代理在跑 → 只广播等待数，不调度唤醒（最后一个结束后才唤醒）。"""
    sid = 987655
    mgr = SubagentManager(session_id=sid)
    mgr._handles[11] = SubagentHandle(agent_id=11, status="running")
    done = SubagentHandle(agent_id=12, status="done", task_title="T")
    mgr._handles[12] = done
    engine_mod._subagent_wakeups.pop(sid, None)
    engine_mod._on_subagent_finished(mgr, sid, done)
    await asyncio.sleep(0)
    assert sid not in engine_mod._subagent_wakeups


async def test_on_subagent_finished_schedules_when_idle(monkeypatch):
    """会话空闲 + 有未送达通知 → 调度唤醒任务（完成后自动创建新一轮送达报告）。"""
    sid = 987656
    mgr = SubagentManager(session_id=sid)
    h = SubagentHandle(agent_id=21, status="done", task_title="T2", findings="done A")
    mgr._handles[21] = h
    mgr._enqueue_completion(h)
    assert mgr.has_unclaimed_notes() is True
    monkeypatch.setattr(engine_mod.settings, "subagent_wakeup_enabled", True)
    started: list[int] = []

    async def _fake(session_id: int) -> None:
        started.append(session_id)

    monkeypatch.setattr(engine_mod, "_wakeup_turn_for_subagents", _fake)
    engine_mod._subagent_wakeups.pop(sid, None)
    try:
        engine_mod._on_subagent_finished(mgr, sid, h)
        task = engine_mod._subagent_wakeups.get(sid)
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        assert started == [sid]
    finally:
        engine_mod._subagent_wakeups.pop(sid, None)


async def test_on_subagent_finished_not_scheduled_when_busy():
    """会话仍有主 turn 在跑 → 完成通知随该轮注入通道送达，不调度唤醒。"""
    sid = 987657
    mgr = SubagentManager(session_id=sid)
    h = SubagentHandle(agent_id=31, status="done", task_title="T3")
    mgr._handles[31] = h
    mgr._enqueue_completion(h)
    engine_mod._session_turn_enter(sid)
    engine_mod._subagent_wakeups.pop(sid, None)
    try:
        engine_mod._on_subagent_finished(mgr, sid, h)
        await asyncio.sleep(0)
        assert sid not in engine_mod._subagent_wakeups
    finally:
        engine_mod._session_turn_leave(sid)


async def test_on_subagent_finished_disabled_by_settings(monkeypatch):
    sid = 987658
    mgr = SubagentManager(session_id=sid)
    h = SubagentHandle(agent_id=41, status="done", task_title="T4")
    mgr._handles[41] = h
    mgr._enqueue_completion(h)
    monkeypatch.setattr(engine_mod.settings, "subagent_wakeup_enabled", False)
    engine_mod._subagent_wakeups.pop(sid, None)
    engine_mod._on_subagent_finished(mgr, sid, h)
    await asyncio.sleep(0)
    assert sid not in engine_mod._subagent_wakeups


async def test_on_subagent_finished_dedup_inflight(monkeypatch):
    """已有唤醒在途 → 不重复调度（期间新完成的通知随该次唤醒一并送达）。"""
    sid = 987659
    mgr = SubagentManager(session_id=sid)
    h = SubagentHandle(agent_id=51, status="done", task_title="T5")
    mgr._handles[51] = h
    mgr._enqueue_completion(h)
    monkeypatch.setattr(engine_mod.settings, "subagent_wakeup_enabled", True)
    engine_mod._subagent_wakeups.pop(sid, None)
    release = asyncio.Event()

    async def _hold(session_id: int) -> None:
        await release.wait()

    monkeypatch.setattr(engine_mod, "_wakeup_turn_for_subagents", _hold)
    try:
        engine_mod._on_subagent_finished(mgr, sid, h)
        first = engine_mod._subagent_wakeups.get(sid)
        assert first is not None
        engine_mod._on_subagent_finished(mgr, sid, h)
        assert engine_mod._subagent_wakeups.get(sid) is first  # 未新建第二个任务
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    finally:
        release.set()
        engine_mod._subagent_wakeups.pop(sid, None)


async def test_wakeup_turn_exits_without_manager(monkeypatch):
    """无 manager（无待送达通知）→ 唤醒协程直接退出并清理占位键。"""
    sid = 987660
    monkeypatch.setattr(engine_mod.settings, "subagent_wakeup_delay_sec", 0.0)
    subagent_mod.cleanup(sid)
    engine_mod._subagent_wakeups[sid] = asyncio.current_task()  # 模拟在途占位
    try:
        await engine_mod._wakeup_turn_for_subagents(sid)
        assert sid not in engine_mod._subagent_wakeups
    finally:
        engine_mod._subagent_wakeups.pop(sid, None)


# ── 广播载荷与前端契约 ───────────────────────────────────

def test_broadcast_session_completed_carries_pending():
    """session.completed 载荷携带 subagent_pending（前端保持等待子代理态的数据源）。"""
    from app.orchestration.agent_events import broadcast_session_completed
    src = inspect.getsource(broadcast_session_completed)
    assert "subagent_pending" in src


def test_engine_broadcasts_subagent_pending_and_wakeup():
    src = Path(engine_mod.__file__).read_text(encoding="utf-8")
    assert '"event": "subagent.pending"' in src
    assert '"event": "subagent.wakeup"' in src


def test_frontend_contract_registered():
    """前端契约：等待行组件 / 分隔线条目 / 新事件登记（构建期穷举检查依赖）。"""
    flow = (_ROOT / "client" / "src" / "components" / "chat" / "MessageFlow.tsx").read_text(encoding="utf-8")
    assert "SubagentWaitingLine" in flow
    timeline = (_ROOT / "client" / "src" / "components" / "chat" / "timeline.ts").read_text(encoding="utf-8")
    assert '"subagent-wakeup"' in timeline
    events = (_ROOT / "packages" / "shared" / "src" / "events.ts").read_text(encoding="utf-8")
    assert '"subagent.pending"' in events and '"subagent.wakeup"' in events
    store = (_ROOT / "client" / "src" / "store" / "chat.ts").read_text(encoding="utf-8")
    assert "pendingSubagents" in store


# ── v44: 空闲兜底收尾（子代理结束后摘除侧栏运行标记） ──────

async def test_on_subagent_finished_broadcasts_idle_completion_without_wakeup(monkeypatch):
    """v44: 空闲且无唤醒接管（报告已被读走）→ 补发 session.completed(pending=0)。

    否则侧栏「运行中」标记在子代理全部结束后无人摘除（转圈残留）。
    """
    sid = 987661
    mgr = SubagentManager(session_id=sid)
    h = SubagentHandle(agent_id=61, status="done", task_title="T6")
    mgr._handles[61] = h  # 不入队完成通知：模拟报告已被 collect_results 读走
    monkeypatch.setattr(engine_mod.settings, "subagent_wakeup_enabled", True)
    calls: list[tuple[int, int]] = []

    async def _fake_completed(session_id: int, db=None, subagent_pending: int = 0) -> None:
        calls.append((session_id, subagent_pending))

    monkeypatch.setattr(engine_mod, "broadcast_session_completed", _fake_completed)
    engine_mod._subagent_wakeups.pop(sid, None)
    engine_mod._on_subagent_finished(mgr, sid, h)
    await asyncio.sleep(0)
    assert calls == [(sid, 0)]
    assert sid not in engine_mod._subagent_wakeups  # 无唤醒轮接管


async def test_on_subagent_finished_disabled_settings_still_broadcasts_idle(monkeypatch):
    """v44: 唤醒开关关闭 → 不调度唤醒，但同样补发空闲收尾（否则转圈残留）。

    此前 pending>0 或开关关闭都直接 return，开关关闭时空闲会话的
    「运行中」标记永远摘不掉。
    """
    sid = 987662
    mgr = SubagentManager(session_id=sid)
    h = SubagentHandle(agent_id=71, status="done", task_title="T7")
    mgr._handles[71] = h
    mgr._enqueue_completion(h)
    monkeypatch.setattr(engine_mod.settings, "subagent_wakeup_enabled", False)
    calls: list[int] = []

    async def _fake_completed(session_id: int, db=None, subagent_pending: int = 0) -> None:
        calls.append(subagent_pending)

    monkeypatch.setattr(engine_mod, "broadcast_session_completed", _fake_completed)
    engine_mod._subagent_wakeups.pop(sid, None)
    engine_mod._on_subagent_finished(mgr, sid, h)
    await asyncio.sleep(0)
    assert calls == [0]
    assert sid not in engine_mod._subagent_wakeups


async def test_on_subagent_finished_wakeup_takes_over_no_idle_broadcast(monkeypatch):
    """v44: 有未送达报告且唤醒可用 → 唤醒轮接管，不补发空闲收尾（避免双事件互相干扰）。"""
    sid = 987663
    mgr = SubagentManager(session_id=sid)
    h = SubagentHandle(agent_id=81, status="done", task_title="T8")
    mgr._handles[81] = h
    mgr._enqueue_completion(h)
    monkeypatch.setattr(engine_mod.settings, "subagent_wakeup_enabled", True)
    started: list[int] = []
    idle_calls: list[int] = []

    async def _fake(session_id: int) -> None:
        started.append(session_id)

    async def _record_completed(session_id: int, db=None, subagent_pending: int = 0) -> None:
        idle_calls.append(subagent_pending)

    monkeypatch.setattr(engine_mod, "_wakeup_turn_for_subagents", _fake)
    monkeypatch.setattr(engine_mod, "broadcast_session_completed", _record_completed)
    engine_mod._subagent_wakeups.pop(sid, None)
    try:
        engine_mod._on_subagent_finished(mgr, sid, h)
        task = engine_mod._subagent_wakeups.get(sid)
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert started == [sid]
        assert idle_calls == []  # 唤醒轮已接管 → 不补发空闲收尾
    finally:
        engine_mod._subagent_wakeups.pop(sid, None)
