"""后台命令生命周期单测（plan-20261020-bgtask）。

覆盖：
- 过期的邮箱打断信号不再误杀下一个工具（「已让出等待」误触发的根因）；
- 打断档位只对真正在等的工具开放，其余一律不打断；
- 后台命令完成 / 停止的邮箱消息不打断运行中的主代理；
- 完成回报：模型已读到终态的不再回报；用户手动停止的单独成类；
- 注册表：按会话列出运行中的命令、按来源终止、启动 / 退出事件。
"""
import asyncio
import sys

import pytest

from app.orchestration import agent_loop, bg_notify, engine, mailbox
from app.orchestration.tools.bg_process import bg_process_registry


@pytest.fixture(autouse=True)
def _clean():
    bg_notify.reset_for_tests()
    mailbox.reset_for_tests()
    yield
    bg_notify.reset_for_tests()
    mailbox.reset_for_tests()


# ── 打断：过期信号与档位 ─────────────────────────────────────────────────────

def test_request_interrupt_defers_when_no_tool_running(monkeypatch):
    """模型正在思考（没有工具在跑）时不直接置位——否则信号残留到下一个工具启动瞬间把它误杀。
    请求被记成延后项，由下一个工具启动时认领；认领一次即清除。"""
    engine._running_turns.add(901)
    ev = engine.wake_event_for(901)
    try:
        assert engine.request_interrupt(901, reason="mailbox", source="subsession_question") is False
        assert not ev.is_set(), "不能在没有工具等待时留下置位的信号"
        assert engine.claim_pending_interrupt(901) == "subsession_question"
        assert engine.claim_pending_interrupt(901) is None, "认领一次即清除"
    finally:
        engine._running_turns.discard(901)
        engine._wake_events.pop(901, None)
        engine._pending_interrupts.pop(901, None)


def test_draining_injected_inputs_voids_deferred_interrupt():
    """模型即将读到注入消息（下一次 LLM 调用前 drain）时，延后的打断请求随之作废。"""
    engine._running_turns.add(904)
    try:
        engine.wake_event_for(904)
        engine.request_interrupt(904, reason="mailbox", source="main_reply")
        engine.drain_injected_inputs(904)
        assert engine.claim_pending_interrupt(904) is None
    finally:
        engine._running_turns.discard(904)
        engine._wake_events.pop(904, None)
        engine._pending_interrupts.pop(904, None)


def test_request_interrupt_sets_event_for_waiting_tool_and_records_source(monkeypatch):
    engine._running_turns.add(902)
    ev = engine.wake_event_for(902)
    try:
        engine.set_tool_state(902, tool="terminal_bg_status", interruptible="abort")
        assert engine.request_interrupt(902, reason="mailbox", source="subsession_question") is True
        assert ev.is_set()
        assert engine.interrupt_source(902) == "subsession_question"
    finally:
        engine.clear_tool_state(902)
        engine._running_turns.discard(902)
        engine._wake_events.pop(902, None)


def test_request_interrupt_skips_never_tool(monkeypatch):
    engine._running_turns.add(903)
    ev = engine.wake_event_for(903)
    try:
        engine.set_tool_state(903, tool="todo_write", interruptible="never")
        assert engine.request_interrupt(903, reason="mailbox", source="main_reply") is False
        assert not ev.is_set()
    finally:
        engine.clear_tool_state(903)
        engine._running_turns.discard(903)
        engine._wake_events.pop(903, None)


@pytest.mark.parametrize("tool,args,expected", [
    ("todo_write", {}, "never"),
    ("fs_read", {"path": "a.py"}, "never"),
    ("browser_click", {}, "never"),
    ("desktop_click", {}, "never"),
    ("fs_write", {}, "never"),
    ("terminal_exec", {"command": "npm test"}, "detach"),
    ("terminal_exec", {"command": "npm run dev", "waitForCompletion": False}, "never"),
    ("terminal_exec", {"command": "npm run dev", "waitForCompletion": "false"}, "never"),
    ("terminal_bg_status", {"shell_id": "bg_1"}, "never"),
    ("terminal_bg_status", {"shell_id": "bg_1", "wait_until_done": True}, "abort"),
    ("terminal_bg_status", {"shell_id": "bg_1", "wait_until_done": "false"}, "never"),
    ("terminal_bg_kill", {"shell_id": "bg_1"}, "never"),
])
def test_interruptible_for_only_opens_waiting_tools(tool, args, expected):
    assert agent_loop._interruptible_for(tool, args) == expected


def test_interrupt_label_names_the_real_source():
    assert "子代理提问" in agent_loop._interrupt_source_label("subagent_question")
    assert "后台命令" in agent_loop._interrupt_source_label("bg_command_done")
    # 未知来源也不能硬说成「子会话消息」
    assert "子会话" not in agent_loop._interrupt_source_label("")


def test_bg_running_reminder_names_commands_and_asks_to_stop_services():
    text = agent_loop._build_bg_running_reminder([
        {"shell_id": "bg_x1", "command": "npm run dev\n  --port 5173", "elapsed": 125.0},
    ])
    assert "bg_x1" in text and "npm run dev --port 5173" in text, "多行命令压成单行展示"
    assert "terminal_bg_kill" in text, "要提醒长驻服务用完主动停止"
    assert "2 分钟" in text


@pytest.mark.asyncio
async def test_bg_command_done_injects_without_interrupt(monkeypatch):
    injected: list[tuple[int, dict]] = []
    interrupts: list[tuple[int, str]] = []
    monkeypatch.setattr(engine, "running_turn_id", lambda sid: 77)
    monkeypatch.setattr(engine, "inject_input", lambda tid, item: injected.append((tid, item)) or True)
    monkeypatch.setattr(engine, "request_interrupt",
                        lambda tid, reason="", source="": interrupts.append((tid, source)) or True)

    res = await mailbox.publish(
        mailbox.MailKind.BG_COMMAND_DONE, session_id=11, text="[后台命令完成] ...", persist=False,
    )
    assert res["delivered"] == "injected"
    assert injected, "完成消息仍要注入当前轮"
    assert interrupts == [], "后台命令完成不急，不应打断主代理的等待"


@pytest.mark.asyncio
async def test_question_interrupt_carries_its_kind(monkeypatch):
    interrupts: list[tuple[int, str]] = []
    monkeypatch.setattr(engine, "running_turn_id", lambda sid: 78)
    monkeypatch.setattr(engine, "inject_input", lambda tid, item: True)
    monkeypatch.setattr(engine, "request_interrupt",
                        lambda tid, reason="", source="": interrupts.append((tid, source)) or True)

    await mailbox.publish(
        mailbox.MailKind.SUBSESSION_QUESTION, session_id=12, text="要不要改？", persist=False,
    )
    assert interrupts == [(78, "subsession_question")]


@pytest.mark.asyncio
async def test_user_stop_notice_does_not_wake_idle_session(monkeypatch):
    woke: list[int] = []

    async def fake_wakeup(session_id, *, prompt, reason=""):
        woke.append(session_id)
        return 1

    monkeypatch.setattr(engine, "running_turn_id", lambda sid: None)
    monkeypatch.setattr(engine, "wakeup_session", fake_wakeup)

    res = await mailbox.publish(
        mailbox.MailKind.BG_COMMAND_STOPPED, session_id=13, text="[后台命令已被用户停止]", persist=False,
    )
    assert res["delivered"] == "queued"
    assert woke == []
    assert mailbox.pending_count(13) == 1


# ── 完成回报：已读去重 / 用户停止分类 ─────────────────────────────────────────

def _payload(shell_id, *, stopped_by=None, rc=0):
    return {
        "shell_id": shell_id, "session_id": 21, "turn_id": 5,
        "command": "npm run build", "returncode": rc, "elapsed": 61.0,
        "tail": "ok", "stopped_by": stopped_by,
    }


@pytest.mark.asyncio
async def test_notify_skips_commands_the_model_already_saw(monkeypatch):
    monkeypatch.setattr(bg_notify, "MERGE_WINDOW_S", 0.02)
    sent: list[tuple[str, str]] = []

    async def fake_publish(kind, *, session_id, text, **kw):
        sent.append((kind.value, text))
        return {"delivered": "queued"}

    monkeypatch.setattr(mailbox, "publish", fake_publish)
    monkeypatch.setattr(bg_process_registry, "is_observed", lambda sid: sid == "bg_seen")

    await bg_notify.on_bg_exit(_payload("bg_seen"))
    await asyncio.sleep(0.1)
    assert sent == [], "模型已经读到终态，不该再回报一遍"

    await bg_notify.on_bg_exit(_payload("bg_seen"))
    await bg_notify.on_bg_exit(_payload("bg_new"))
    await asyncio.sleep(0.1)
    assert len(sent) == 1
    assert "bg_new" in sent[0][1] and "bg_seen" not in sent[0][1]


@pytest.mark.asyncio
async def test_notify_separates_user_stopped_commands(monkeypatch):
    monkeypatch.setattr(bg_notify, "MERGE_WINDOW_S", 0.02)
    sent: list[tuple[str, str, dict | None]] = []

    async def fake_publish(kind, *, session_id, text, extra=None, **kw):
        sent.append((kind.value, text, extra))
        return {"delivered": "queued"}

    monkeypatch.setattr(mailbox, "publish", fake_publish)
    monkeypatch.setattr(bg_process_registry, "is_observed", lambda sid: False)

    await bg_notify.on_bg_exit(_payload("bg_done"))
    await bg_notify.on_bg_exit(_payload("bg_user", stopped_by="user", rc=1))
    await asyncio.sleep(0.1)

    kinds = {k: (t, e) for k, t, e in sent}
    assert set(kinds) == {"bg_command_done", "bg_command_stopped"}
    assert "用户停止" in kinds["bg_command_stopped"][0]
    assert kinds["bg_command_stopped"][1]["bg_items"][0]["stopped_by"] == "user"
    assert kinds["bg_command_done"][1]["bg_items"][0]["shell_id"] == "bg_done"


# ── 注册表：列表 / 终止来源 / 事件 ─────────────────────────────────────────────

async def _spawn_sleeper(seconds: int = 30):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", f"import time; time.sleep({seconds})",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.asyncio
async def test_registry_lists_running_and_user_stop_flow(monkeypatch):
    events: list[tuple[str, dict]] = []

    async def fake_broadcast(session_id, event):
        events.append((event["event"], event["payload"]))

    import app.orchestration.agent_events as agent_events
    monkeypatch.setattr(agent_events, "broadcast", fake_broadcast)
    exits: list[dict] = []

    proc = await _spawn_sleeper()
    shell_id = bg_process_registry.register(
        proc, "python sleeper", None, 31, turn_id=9, on_exit=lambda p: exits.append(p),
    )
    try:
        await asyncio.sleep(0.2)
        rows = bg_process_registry.list_session(31)
        assert [r["shell_id"] for r in rows] == [shell_id]
        assert rows[0]["command"] == "python sleeper" and rows[0]["elapsed"] >= 0
        assert bg_process_registry.list_session(32) == [], "不能串到别的会话"
        assert bg_process_registry.owner_session(shell_id) == 31

        info = await bg_process_registry.kill(shell_id, by="user")
        assert info is not None and info["killed"] is True
        await asyncio.sleep(0.5)

        assert bg_process_registry.list_session(31) == [], "停止后不再列出"
        names = [n for n, _ in events]
        assert "bg.process.started" in names and "bg.process.exited" in names
        exited = next(p for n, p in events if n == "bg.process.exited")
        assert exited["reason"] == "stopped_by_user" and exited["session_id"] == 31
        assert exits and exits[0]["stopped_by"] == "user", "用户停止要带着来源回报给会话"
    finally:
        if proc.returncode is None:
            proc.kill()


@pytest.mark.asyncio
async def test_agent_kill_marks_observed_and_system_kill_is_silent(monkeypatch):
    import app.orchestration.agent_events as agent_events

    async def noop(session_id, event):
        return None

    monkeypatch.setattr(agent_events, "broadcast", noop)
    exits: list[dict] = []

    p1 = await _spawn_sleeper()
    s1 = bg_process_registry.register(p1, "a", None, 41, on_exit=lambda p: exits.append(p))
    p2 = await _spawn_sleeper()
    s2 = bg_process_registry.register(p2, "b", None, 41, on_exit=lambda p: exits.append(p))
    try:
        await asyncio.sleep(0.2)
        await bg_process_registry.kill(s1, by="agent")
        assert bg_process_registry.is_observed(s1), "模型自己停的它已经知道，不必再回报"

        assert await bg_process_registry.kill_session(41) == 1
        await asyncio.sleep(0.5)
        assert bg_process_registry.list_session(41) == []
        assert all(p["shell_id"] != s2 for p in exits), "会话被删除 / 应用退出时没有会话可通知"
    finally:
        for p in (p1, p2):
            if p.returncode is None:
                p.kill()


@pytest.mark.asyncio
async def test_started_event_precedes_exited_even_for_instant_exit(monkeypatch):
    """极快退出的进程：exited 不能抢在 started 前面（否则卡片里留下永远不消失的假运行中行）。"""
    order: list[str] = []

    async def slow_broadcast(session_id, event):
        if event["event"] == "bg.process.started":
            await asyncio.sleep(0.15)  # 启动广播故意拖慢，制造抢跑机会
        order.append(event["event"])

    import app.orchestration.agent_events as agent_events
    monkeypatch.setattr(agent_events, "broadcast", slow_broadcast)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    bg_process_registry.register(proc, "python -c pass", None, 51)
    await asyncio.sleep(1.0)
    assert order == ["bg.process.started", "bg.process.exited"]


# ── plan-20261025-bgturn：撤回该轮时后台通知不能残留 ─────────────────────────

def test_batch_turn_id_only_when_all_items_share_one_turn():
    """同轮才落 turn_id：跨轮合并的通知不能误挂到其中一轮，否则撤回会带走不该撤的那条。"""
    assert bg_notify._batch_turn_id([_payload("bg_a"), _payload("bg_b")]) == 5
    assert bg_notify._batch_turn_id(
        [_payload("bg_a"), {**_payload("bg_b"), "turn_id": 6}]
    ) is None
    assert bg_notify._batch_turn_id([{**_payload("bg_a"), "turn_id": None}]) is None


@pytest.mark.asyncio
async def test_flush_carries_turn_id_so_rollback_can_delete_it(monkeypatch):
    """落库必须带 turn 归属——撤回按 `turn_id >= 目标轮` 软删，归属为空就永远删不掉。"""
    monkeypatch.setattr(bg_notify, "MERGE_WINDOW_S", 0.02)
    sent: list[tuple[str, int | None]] = []

    async def fake_publish(kind, *, session_id, text, turn_id=None, **kw):
        sent.append((kind.value, turn_id))
        return {"delivered": "queued"}

    async def none_revoked(turn_ids):
        return set()

    monkeypatch.setattr(mailbox, "publish", fake_publish)
    monkeypatch.setattr(bg_notify, "_turn_revoked", none_revoked)
    monkeypatch.setattr(bg_process_registry, "is_observed", lambda sid: False)

    await bg_notify.on_bg_exit(_payload("bg_user", stopped_by="user", rc=1))
    await asyncio.sleep(0.1)
    assert sent == [("bg_command_stopped", 5)]


@pytest.mark.asyncio
async def test_flush_skips_commands_of_revoked_turn(monkeypatch):
    """合并窗口内该轮已被撤回：不再投递，否则消息流里会重新冒出一条已撤销轮的提示。"""
    monkeypatch.setattr(bg_notify, "MERGE_WINDOW_S", 0.02)
    sent: list[tuple[str, str]] = []

    async def fake_publish(kind, *, session_id, text, **kw):
        sent.append((kind.value, text))
        return {"delivered": "queued"}

    async def revoked(turn_ids):
        return {5}

    monkeypatch.setattr(mailbox, "publish", fake_publish)
    monkeypatch.setattr(bg_notify, "_turn_revoked", revoked)
    monkeypatch.setattr(bg_process_registry, "is_observed", lambda sid: False)

    await bg_notify.on_bg_exit(_payload("bg_user", stopped_by="user", rc=1))
    await asyncio.sleep(0.1)
    assert sent == [], "已撤回轮的命令停止提示不该再投递"


def test_drop_from_turn_discards_queued_notices_of_that_turn():
    """队列里的待投递通知也要清：只软删落库副本的话，下一轮 take_pending 会把它带走。"""
    mailbox._push(mailbox.MailItem(
        kind="bg_command_stopped", session_id=61, text="stop", turn_id=7))
    mailbox._push(mailbox.MailItem(
        kind="subagent_question", session_id=61, text="ask", turn_id=None))
    mailbox._push(mailbox.MailItem(
        kind="bg_command_done", session_id=61, text="done", turn_id=8))

    assert mailbox.drop_from_turn(61, 7) == 2
    rest = mailbox.take_pending(61)
    assert [it["content"] for it in rest] == ["ask"], "与轮次无关的消息不受影响到"


@pytest.mark.asyncio
async def test_kill_since_only_stops_processes_from_that_turn(monkeypatch):
    """撤回只终止该轮及之后启动的后台命令，更早轮次仍在跑的服务不动。"""
    import app.orchestration.agent_events as agent_events

    async def noop(session_id, event):
        return None

    monkeypatch.setattr(agent_events, "broadcast", noop)
    exits: list[dict] = []

    p_old = await _spawn_sleeper()
    s_old = bg_process_registry.register(
        p_old, "old", None, 71, turn_id=3, on_exit=lambda p: exits.append(p))
    p_new = await _spawn_sleeper()
    s_new = bg_process_registry.register(
        p_new, "new", None, 71, turn_id=9, on_exit=lambda p: exits.append(p))
    try:
        await asyncio.sleep(0.2)
        assert await bg_process_registry.kill_since(71, 9) == [s_new]
        await asyncio.sleep(0.5)
        assert [r["shell_id"] for r in bg_process_registry.list_session(71)] == [s_old]
        assert all(p["shell_id"] != s_new for p in exits), "撤回按 system 终止，不回报会话"
    finally:
        for p in (p_old, p_new):
            if p.returncode is None:
                p.kill()

