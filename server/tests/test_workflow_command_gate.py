"""检查命令的权限门控（plan-20261014-fix4）：跟随会话的执行模式与权限模式，而不是只认内置白名单。

覆盖用户反馈的场景：完全访问下 `git status --short` 等非内置检查必须直接运行，规划期也不再提示「需要审批」。
"""

import asyncio

from app.orchestration.approval import approval_manager
from app.orchestration.workflow import store
from app.orchestration.workflow.commands import command_note_for_session, gate_check_command
from app.orchestration.workflow.graph import parse_plan, validate_plan
from app.persistence.database import run_write_locked
from app.persistence.models.message import Session as SessionRow
from tests.test_workflow_runtime import ScriptedExecutor, _program_status, _runtime


async def _session(approval_mode: str = "ask", permission_mode: str = "agent") -> int:
    def _op(s):
        row = SessionRow(title="门控测试", approval_mode=approval_mode, permission_mode=permission_mode)
        s.add(row)
        s.flush()
        sid = row.id
        s.commit()
        return sid

    return await run_write_locked(_op, label="test.gate.session")


def _answer_pending(approved: bool) -> asyncio.Task:
    """模拟用户在审批卡上作答：等审批请求出现后逐个处理。"""

    async def answer():
        for _ in range(100):
            await asyncio.sleep(0.01)
            pending = list(approval_manager._pending.items())
            if pending:
                for aid, _pa in pending:
                    approval_manager.resolve(aid, approved)
                return

    return asyncio.create_task(answer())


async def test_full_access_runs_non_builtin_commands_without_asking(tmp_path):
    sid = await _session("full")
    gate = await gate_check_command("git status --short", session_id=sid, workspace=str(tmp_path))
    assert gate.allowed
    assert approval_manager.pending_count == 0


async def test_builtin_checks_never_ask_even_in_ask_mode(tmp_path):
    sid = await _session("ask")
    gate = await gate_check_command("pytest -q", session_id=sid, workspace=str(tmp_path))
    assert gate.allowed
    assert approval_manager.pending_count == 0


async def test_ask_mode_requests_approval_and_runs_once_approved(tmp_path):
    sid = await _session("ask")
    seen: list[dict] = []

    async def capture():
        for _ in range(100):
            await asyncio.sleep(0.01)
            for aid, pa in list(approval_manager._pending.items()):
                seen.append(dict(pa.detail))
                approval_manager.resolve(aid, True)
                return

    task = asyncio.create_task(capture())
    gate = await gate_check_command(
        "git status --short", session_id=sid, workspace=str(tmp_path), origin="工作流步骤『a/b』"
    )
    await task
    assert gate.allowed
    assert seen and seen[0]["tool"] == "terminal_exec"
    assert seen[0]["args"]["command"] == "git status --short"
    assert seen[0]["session_id"] == sid
    assert seen[0]["agent_name"] == "工作流检查"


async def test_ask_mode_rejection_blocks_the_command(tmp_path):
    sid = await _session("ask")
    task = _answer_pending(False)
    gate = await gate_check_command("git status --short", session_id=sid, workspace=str(tmp_path))
    await task
    assert not gate.allowed
    assert "审批未通过" in gate.reason


async def test_plan_mode_boundary_denies_writing_commands_without_asking(tmp_path):
    sid = await _session("full", permission_mode="plan")
    gate = await gate_check_command("npm install", session_id=sid, workspace=str(tmp_path))
    assert not gate.allowed
    assert "当前模式不允许执行" in gate.reason
    assert approval_manager.pending_count == 0


async def test_gate_follows_the_latest_session_mode(tmp_path):
    sid = await _session("ask")

    def _op(s):
        s.get(SessionRow, sid).approval_mode = "full"
        s.commit()

    await run_write_locked(_op, label="test.gate.switch")
    gate = await gate_check_command("git status --short", session_id=sid, workspace=str(tmp_path))
    assert gate.allowed


def _check_plan(command: str) -> dict:
    return {
        "title": "冒烟",
        "goal": "检查",
        "flows": [
            {
                "key": "smoke",
                "nodes": [
                    {"key": "explore", "kind": "explore", "task": "探索"},
                    {"key": "verify", "kind": "check", "inputs": ["explore"], "commands": [command]},
                ],
            }
        ],
    }


async def test_plan_time_notes_follow_the_session_mode(tmp_path):
    spec, _ = parse_plan(_check_plan("git status --short"))

    full = await _session("full")
    res = validate_plan(spec, command_note=await command_note_for_session(full, str(tmp_path)))
    assert res.ok and res.warnings == [], "完全访问下不应出现任何命令提示"

    ask = await _session("ask")
    res = validate_plan(spec, command_note=await command_note_for_session(ask, str(tmp_path)))
    assert [w.code for w in res.warnings] == ["command_needs_approval"]
    assert "询问审批" in res.warnings[0].message

    auto = await _session("auto")
    res = validate_plan(spec, command_note=await command_note_for_session(auto, str(tmp_path)))
    assert res.warnings == [], "自动审批下只读命令直接运行，不应提示"


async def test_plan_workflow_reply_has_no_approval_hint_in_full_access(tmp_path, monkeypatch):
    """用户反馈原文：完全访问下 plan_workflow 仍回『[command_needs_approval] … 不在白名单内』。"""
    from app.orchestration.workflow import service
    from app.orchestration.workflow.tools import run_workflow_tool

    monkeypatch.setattr(service, "_runtime", None)
    plan = _check_plan("git status --short")
    args = {"title": plan["title"], "goal": plan["goal"], "flows": plan["flows"]}

    full = await _session("full")
    text = await run_workflow_tool(
        "plan_workflow", args, session_id=full, turn_id=None, workspace=str(tmp_path), readonly_only=False
    )
    assert "已生成工作流计划" in text
    assert "command_needs_approval" not in text and "白名单" not in text

    ask = await _session("ask")
    text = await run_workflow_tool(
        "plan_workflow", args, session_id=ask, turn_id=None, workspace=str(tmp_path), readonly_only=False
    )
    assert "command_needs_approval" in text and "询问审批" in text


async def test_check_node_runs_non_builtin_command_in_full_access(tmp_path):
    ran: list[str] = []

    async def runner(command: str, cwd: str) -> dict:
        ran.append(command)
        return {"cmd": command, "ok": True, "exit_code": 0, "output": ""}

    rt = _runtime(ScriptedExecutor(), command_runner=runner, command_gate=gate_check_command)
    sid = await _session("full")
    pid = await rt.create_program(
        session_id=sid, plan=_check_plan("git status --short"), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    assert ran == ["git status --short"]


async def test_check_node_reports_a_blocked_command_instead_of_a_failed_check(tmp_path):
    ran: list[str] = []

    async def runner(command: str, cwd: str) -> dict:
        ran.append(command)
        return {"cmd": command, "ok": True, "exit_code": 0, "output": ""}

    rt = _runtime(ScriptedExecutor(), command_runner=runner, command_gate=gate_check_command)
    sid = await _session("ask")
    pid = await rt.create_program(
        session_id=sid, plan=_check_plan("git status --short"), workspace=str(tmp_path)
    )
    task = _answer_pending(False)
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)
    await task

    assert ran == [], "审批被拒绝时命令不应执行"
    snap = await store.load_program(pid)
    verify = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "verify")
    assert verify["status"] == "failed"
    assert verify["reason"].startswith("检查未执行：git status --short")
    assert verify["output"]["checks"][0]["blocked"] is True
