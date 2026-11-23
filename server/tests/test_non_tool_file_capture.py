"""非工具（命令行/Python脚本/后台进程等）生成与修改文件的检测捕获与撤销测试（plan-20261120-non-tool-capture）。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.schemas import ChatMessage, ChatResponse
from app.persistence.database import Base
from app.persistence.models.agent import Agent
from app.persistence.models.message import Message, Session
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.persistence.models.turn import Turn
from app.services import rollback_service as rs


# ── 纯函数：工作区扫描 / 差异 / 脚本路径提取 / 触发过滤 ─────────────────────────────


def test_scan_workspace_stat_skips_noise_and_tracks_changes(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".git" / "objects").mkdir(parents=True)
    (ws / ".chatcoder" / "checkpoints").mkdir(parents=True)
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "__pycache__").mkdir(parents=True)
    (ws / "output").mkdir(parents=True)
    # 构建产物目录与构建缓存：`npm run build` 会整批重建，不能被记成用户改动
    (ws / "client" / "dist" / "assets").mkdir(parents=True)
    (ws / "build").mkdir(parents=True)

    (ws / ".git" / "objects" / "ab").write_bytes(b"git")
    (ws / ".chatcoder" / "checkpoints" / "ck").write_bytes(b"ckpt")
    (ws / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (ws / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"pyc")
    (ws / "output" / "~$report.xlsx").write_bytes(b"lock")
    (ws / "client" / "dist" / "assets" / "index-abc.js").write_text("built", encoding="utf-8")
    (ws / "client" / "tsconfig.tsbuildinfo").write_text("{}", encoding="utf-8")
    (ws / "build" / "artifact.bin").write_bytes(b"bin")
    (ws / "keep.txt").write_text("v1", encoding="utf-8")

    pre, _, trunc = rs.scan_workspace_stat(str(ws))
    assert not trunc
    assert list(pre.keys()) == ["keep.txt"]

    # 模拟 Python 脚本生成新 xlsx 与修改既有文件，同时构建产物整批重建
    (ws / "output" / "sales.xlsx").write_bytes(b"PK\x03\x04fake-xlsx-v1")
    (ws / "keep.txt").write_text("v2-longer", encoding="utf-8")
    (ws / "client" / "dist" / "assets" / "index-def.js").write_text("rebuilt", encoding="utf-8")
    (ws / "client" / "tsconfig.tsbuildinfo").write_text('{"v":2}', encoding="utf-8")
    post, _, _ = rs.scan_workspace_stat(str(ws))
    diff = rs.diff_workspace_scans(pre, post)
    assert diff == ["keep.txt", "output/sales.xlsx"]


def test_extract_script_candidate_paths(tmp_path):
    ws = tmp_path / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "template.xlsx").write_bytes(b"PK\x03\x04tpl")
    (ws / "scripts" / "update.py").write_text(
        'import openpyxl\nwb = openpyxl.load_workbook("data/template.xlsx")\nwb.save("output/new.xlsx")\n',
        encoding="utf-8",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    cands = rs.extract_script_candidate_paths("python scripts/update.py", str(ws))
    assert "data/template.xlsx" in cands
    assert all("outside" not in c for c in cands)


def test_should_scan_workspace_tool():
    from app.orchestration.agent_loop import _should_scan_workspace_tool

    assert _should_scan_workspace_tool("fs_write", {"path": "a.txt"}, "/ws") is False
    assert _should_scan_workspace_tool("fs_read", {"path": "a.txt"}, "/ws") is False
    assert _should_scan_workspace_tool("terminal_exec", {"command": "git status"}, "/ws") is False
    assert _should_scan_workspace_tool("terminal_exec", {"command": "dir"}, "/ws") is False
    assert _should_scan_workspace_tool("terminal_exec", {"command": "python make_excel.py"}, "/ws") is True
    assert _should_scan_workspace_tool("ci_run", {"check": "build"}, "/ws") is True
    assert _should_scan_workspace_tool("git", {"command": "status"}, "/ws") is False
    assert _should_scan_workspace_tool("git", {"command": "checkout"}, "/ws") is True
    assert _should_scan_workspace_tool("mcp_demo_export", {}, "/ws") is True


# ── 数据库与完整撤销/回滚场景 ────────────────────────────────────────────────────


@pytest.fixture
async def db(monkeypatch, tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/non_tool_capture.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    from app.persistence import write_engine as _we

    _we.configure(db_url, foreign_keys=False)
    async with factory() as s:
        yield s
    await engine.dispose()
    _we.configure(None)


async def _seed_turn(db, ws, *, prompt: str = "做一个 excel"):
    sess = Session(project_id=None, worktree_path=str(ws), title="excel_test", status="active")
    db.add(sess)
    await db.flush()
    agent = Agent(session_id=sess.id, name="主代理", kind="main", status="idle")
    db.add(agent)
    await db.flush()
    turn = Turn(session_id=sess.id, status="completed")
    db.add(turn)
    await db.flush()
    msg = Message(session_id=sess.id, turn_id=turn.id, sender_type="user", msg_type="text", content={"text": prompt})
    db.add(msg)
    await db.flush()
    turn.user_message_id = msg.id
    snap = TurnSnapshot(
        session_id=sess.id, turn_id=turn.id, user_message_id=msg.id,
        git_head=None, file_list=[], new_files=[],
    )
    db.add(snap)
    await db.commit()
    return sess, agent, turn


@pytest.mark.asyncio
async def test_detected_binary_xlsx_supports_multi_edit_revert_and_rollback(db, tmp_path):
    """非工具生成新建 .xlsx、同轮二次修改、手动修改冲突保护，以及撤销文件 / 撤销消息。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess, _, turn = await _seed_turn(db, ws)

    # 第一步：脚本新建 output/sales.xlsx（命令中未显式出现该路径）
    pre1, _, _ = rs.scan_workspace_stat(str(ws))
    xlsx_path = ws / "output" / "sales.xlsx"
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path.write_bytes(b"PK\x03\x04excel-v1")
    post1, _, _ = rs.scan_workspace_stat(str(ws))
    diff1 = rs.diff_workspace_scans(pre1, post1)
    assert diff1 == ["output/sales.xlsx"]

    detected1, _, _ = await rs.record_detected_file_changes(
        db, session_id=sess.id, turn_id=turn.id, workspace=str(ws),
        tool_name="terminal_exec", call_key="call_1",
        candidates=diff1, pre_scan=pre1,
    )
    assert detected1 == ["output/sales.xlsx"]

    # 第二步：同轮内再次用 Python 修改同一个二进制 .xlsx
    pre2, _, _ = rs.scan_workspace_stat(str(ws))
    pre_bytes2 = rs.capture_pre_bytes(str(ws), pre2)
    xlsx_path.write_bytes(b"PK\x03\x04excel-v2-updated")
    post2, _, _ = rs.scan_workspace_stat(str(ws))
    diff2 = rs.diff_workspace_scans(pre2, post2)
    detected2, _, _ = await rs.record_detected_file_changes(
        db, session_id=sess.id, turn_id=turn.id, workspace=str(ws),
        tool_name="terminal_exec", call_key="call_2",
        candidates=diff2, pre_scan=pre2, pre_bytes=pre_bytes2,
    )
    assert detected2 == ["output/sales.xlsx"]

    # 变更卡片与右侧摘要产物均可见
    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert len(changes) == 1
    assert changes[0]["path"] == "output/sales.xlsx"
    assert changes[0]["action"] == "added"
    assert await rs.session_product_paths(db, sess.id, str(ws)) == ["output/sales.xlsx"]

    # 若用户在轮次结束后手动修改该 Excel，撤销应严格拒绝
    xlsx_path.write_bytes(b"PK\x03\x04user-edited-in-excel")
    blocked = await rs.revert_turn_files(db, turn_id=turn.id)
    assert blocked["ok"] is False
    assert blocked["code"] == "conflict"
    assert xlsx_path.exists()

    # 恢复到 AI 产出内容后，撤销文件成功删除新建的 .xlsx
    xlsx_path.write_bytes(b"PK\x03\x04excel-v2-updated")
    reverted = await rs.revert_turn_files(db, turn_id=turn.id)
    assert reverted["ok"] is True, reverted
    assert not xlsx_path.exists()
    assert await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws)) == []
    assert await rs.session_product_paths(db, sess.id, str(ws)) == []


@pytest.mark.asyncio
async def test_detected_preexisting_file_modified_by_script_restores_on_rollback(db, tmp_path):
    """工作区既有二进制文件被非工具脚本修改时，捕获并通过小工作区写前缓存实现完整回滚。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    tpl = ws / "template.xlsx"
    tpl.write_bytes(b"PK\x03\x04original-template")
    sess, _, turn = await _seed_turn(db, ws)

    pre, _, _ = rs.scan_workspace_stat(str(ws))
    pre_bytes = rs.capture_pre_bytes(str(ws), pre)
    tpl.write_bytes(b"PK\x03\x04mutated-by-python")
    post, _, _ = rs.scan_workspace_stat(str(ws))

    detected, _, _ = await rs.record_detected_file_changes(
        db, session_id=sess.id, turn_id=turn.id, workspace=str(ws),
        tool_name="terminal_exec", call_key="call_mod",
        candidates=rs.diff_workspace_scans(pre, post),
        pre_scan=pre, pre_bytes=pre_bytes,
    )
    assert detected == ["template.xlsx"]

    rb = await rs.rollback_turn(db, turn_id=turn.id)
    assert rb["ok"] is True, rb
    assert tpl.read_bytes() == b"PK\x03\x04original-template"
    assert await rs.session_product_paths(db, sess.id, str(ws)) == []


@pytest.mark.asyncio
async def test_concurrent_subagent_state_is_not_duplicated(db, tmp_path):
    """并发子代理已登记某文件且写后指纹一致时，另一协程的扫描不会重复记录同一文件。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess, _, turn = await _seed_turn(db, ws)

    pre, _, _ = rs.scan_workspace_stat(str(ws))
    f_b = ws / "from_subagent_b.xlsx"
    f_b.write_bytes(b"PK\x03\x04sub-b")
    await rs.record_turn_write(
        db, session_id=sess.id, turn_id=turn.id, tool="fs_write",
        path="from_subagent_b.xlsx", before=None, after=None, binary=True,
        op=rs.OP_CREATE, after_hash=rs.file_digest(str(ws), "from_subagent_b.xlsx"),
    )
    f_a = ws / "from_subagent_a.xlsx"
    f_a.write_bytes(b"PK\x03\x04sub-a")
    post, _, _ = rs.scan_workspace_stat(str(ws))

    detected, _, _ = await rs.record_detected_file_changes(
        db, session_id=sess.id, turn_id=turn.id, workspace=str(ws),
        tool_name="terminal_exec", call_key="call_sub_a",
        candidates=rs.diff_workspace_scans(pre, post),
        pre_scan=pre,
    )
    assert detected == ["from_subagent_a.xlsx"]
    rows = (await db.execute(select(RollbackWrite).where(RollbackWrite.turn_id == turn.id))).scalars().all()
    assert sorted(r.path for r in rows) == ["from_subagent_a.xlsx", "from_subagent_b.xlsx"]


@pytest.mark.asyncio
async def test_agent_loop_captures_python_generated_excel_and_supports_rollback(db, tmp_path, monkeypatch):
    """端到端：run_agent_loop 中模型用 terminal_exec 执行 Python 写出 report.xlsx（即使命令未匹配正则且脚本/非工具写出），
    产物消息、变更卡片、摘要产物均可见，并支持撤销消息删除文件。"""
    from app.orchestration import agent_loop
    from app.orchestration.tools.base import ToolResult

    ws = tmp_path / "ws"
    ws.mkdir()
    sess, agent, turn = await _seed_turn(db, ws)

    # 模拟真实场景：模型先用 terminal_exec 运行一段不包含 open(...,'w') 的命令生成 report.xlsx
    class _FakeExecutor:
        async def execute(self, *, tool_name, args, call_key, agent, ctx, on_approval_request=None, llm_call_id=None):
            # 产物目录刻意不用 dist/build 这类构建产物名：扫描口径会跳过它们（见 _SCAN_SKIP_DIRS）
            (ws / "reports").mkdir(exist_ok=True)
            (ws / "reports" / "report.xlsx").write_bytes(b"PK\x03\x04xlsx-binary-content")
            return ToolResult(ok=True, output="generated reports/report.xlsx")

    responses = [
        ChatResponse(
            content="",
            tool_calls=[{
                "id": "llm_1",
                "name": "terminal_exec",
                "arguments": {"command": "python -c \"import pathlib; pathlib.Path('reports').mkdir()\""},
            }],
            finish_reason="tool_calls",
        ),
        ChatResponse(
            content="已生成 reports/report.xlsx。",
            tool_calls=[],
            finish_reason="stop",
        ),
    ]
    step_idx = {"n": 0}

    async def _fake_stream(*args, **kwargs):
        res = responses[step_idx["n"]]
        step_idx["n"] += 1
        return res

    provider = SimpleNamespace(supports_thinking=lambda: False, credential_row_id=1)
    registry = SimpleNamespace(get_provider_for_agent=AsyncMock(return_value=(provider, None)))
    monkeypatch.setattr(agent_loop, "tool_executor", _FakeExecutor())
    monkeypatch.setattr(agent_loop, "broadcast", AsyncMock())
    monkeypatch.setattr(agent_loop, "_emit_agent_msg", AsyncMock())
    monkeypatch.setattr(agent_loop, "_report_credential", AsyncMock())
    monkeypatch.setattr(agent_loop, "_stream_chat_and_broadcast", _fake_stream)
    monkeypatch.setattr(agent_loop, "_compact_persistent_or_fallback",
                        AsyncMock(side_effect=lambda *a, **kw: (None, kw["messages"])))
    monkeypatch.setattr(agent_loop.hook_service, "run_hooks", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_loop, "get_model_registry", lambda: registry)

    out = await agent_loop.run_agent_loop(
        db,
        session_id=sess.id,
        turn_id=turn.id,
        agent=agent,
        context_messages=[
            ChatMessage(role="system", content="规则"),
            ChatMessage(role="user", content="做一个 excel", context_kind="current_task"),
        ],
        tool_schemas=[],
        workspace=str(ws),
    )
    assert out.kind == "message"
    assert (ws / "reports" / "report.xlsx").is_file()

    # 1. 变更卡片数据可见
    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert [(c["path"], c["action"]) for c in changes] == [("reports/report.xlsx", "added")]

    # 2. 右侧摘要产物接口可见
    assert await rs.session_product_paths(db, sess.id, str(ws)) == ["reports/report.xlsx"]

    # 3. MsgType.ARTIFACT 消息携带 files（切换历史会话可识别）
    art_msgs = (await db.execute(
        select(Message).where(Message.turn_id == turn.id, Message.msg_type == "artifact")
    )).scalars().all()
    assert len(art_msgs) == 1
    assert art_msgs[0].content.get("files") == ["reports/report.xlsx"]

    # 4. 撤销消息：消息软删 + 新建 Excel 文件被删除 + 摘要清空
    rb = await rs.rollback_turn(db, turn_id=turn.id)
    assert rb["ok"] is True, rb
    assert not (ws / "reports" / "report.xlsx").exists()
    assert await rs.session_product_paths(db, sess.id, str(ws)) == []


@pytest.mark.asyncio
async def test_bg_process_reconcile_captures_async_created_file(db, tmp_path, monkeypatch):
    """后台命令退出后，reconcile_entry_writes 自动补记其生成的文件并支持撤销。"""
    from app.orchestration.tools.bg_process import BgProcessRegistry, _BgEntry

    ws = tmp_path / "ws"
    ws.mkdir()
    sess, _, turn = await _seed_turn(db, ws)
    pre, _, _ = rs.scan_workspace_stat(str(ws))

    (ws / "async_report.xlsx").write_bytes(b"PK\x03\x04bg-excel")

    reg = BgProcessRegistry()
    fake_proc = SimpleNamespace(stdin=None, pid=99999, returncode=0)
    entry = _BgEntry("bg_test1", fake_proc, "python bg.py", str(ws), sess.id, turn_id=turn.id)
    entry.write_baseline = {
        "workspace": str(ws),
        "call_key": "call_bg",
        "candidates": [],
        "pre_scan": pre,
        "pre_bytes": {},
        "pre_ckpts": {},
    }
    reg._entries["bg_test1"] = entry
    monkeypatch.setattr("app.orchestration.agent_events.broadcast", AsyncMock())

    await reg.reconcile_entry_writes(entry)
    assert entry.write_baseline is None  # 幂等消费

    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert [(c["path"], c["action"]) for c in changes] == [("async_report.xlsx", "added")]
    assert await rs.session_product_paths(db, sess.id, str(ws)) == ["async_report.xlsx"]

    reverted = await rs.revert_turn_files(db, turn_id=turn.id)
    assert reverted["ok"] is True, reverted
    assert not (ws / "async_report.xlsx").exists()


@pytest.mark.asyncio
async def test_agent_loop_captures_partial_write_when_command_fails(db, tmp_path, monkeypatch):
    """命令在写完文件后中途报错退出（result.ok=False），仍捕获该文件以便用户查看和撤销。"""
    from app.orchestration import agent_loop
    from app.orchestration.tools.base import ToolResult

    ws = tmp_path / "ws"
    ws.mkdir()
    sess, agent, turn = await _seed_turn(db, ws)

    class _FakeExecutor:
        async def execute(self, *, tool_name, args, call_key, agent, ctx, on_approval_request=None, llm_call_id=None):
            (ws / "partial.xlsx").write_bytes(b"PK\x03\x04partial")
            return ToolResult(ok=False, output="", error="退出码 1")

    responses = [
        ChatResponse(
            content="",
            tool_calls=[{
                "id": "llm_1",
                "name": "terminal_exec",
                "arguments": {"command": "python fail_after_write.py"},
            }],
            finish_reason="tool_calls",
        ),
        ChatResponse(content="脚本中途出错。", tool_calls=[], finish_reason="stop"),
    ]
    step_idx = {"n": 0}

    async def _fake_stream(*args, **kwargs):
        res = responses[step_idx["n"]]
        step_idx["n"] += 1
        return res

    provider = SimpleNamespace(supports_thinking=lambda: False, credential_row_id=1)
    registry = SimpleNamespace(get_provider_for_agent=AsyncMock(return_value=(provider, None)))
    monkeypatch.setattr(agent_loop, "tool_executor", _FakeExecutor())
    monkeypatch.setattr(agent_loop, "broadcast", AsyncMock())
    monkeypatch.setattr(agent_loop, "_emit_agent_msg", AsyncMock())
    monkeypatch.setattr(agent_loop, "_report_credential", AsyncMock())
    monkeypatch.setattr(agent_loop, "_stream_chat_and_broadcast", _fake_stream)
    monkeypatch.setattr(agent_loop, "_compact_persistent_or_fallback",
                        AsyncMock(side_effect=lambda *a, **kw: (None, kw["messages"])))
    monkeypatch.setattr(agent_loop.hook_service, "run_hooks", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_loop, "get_model_registry", lambda: registry)

    out = await agent_loop.run_agent_loop(
        db,
        session_id=sess.id,
        turn_id=turn.id,
        agent=agent,
        context_messages=[
            ChatMessage(role="system", content="规则"),
            ChatMessage(role="user", content="运行脚本", context_kind="current_task"),
        ],
        tool_schemas=[],
        workspace=str(ws),
    )
    assert out.kind == "message"
    changes = await rs.list_turn_changes(db, session_id=sess.id, turn_id=turn.id, workspace=str(ws))
    assert [(c["path"], c["action"]) for c in changes] == [("partial.xlsx", "added")]
    reverted = await rs.revert_turn_files(db, turn_id=turn.id)
    assert reverted["ok"] is True, reverted
    assert not (ws / "partial.xlsx").exists()
