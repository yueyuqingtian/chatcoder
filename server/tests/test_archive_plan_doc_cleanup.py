"""归档永久删除的计划文档清理与备份空目录收口（plan-20261020-archive-plan-doc）。

缺陷原貌：在「设置 → 归档恢复」永久删除会话时，只删了库里的行，磁盘上两类残留一直累积——

1. `{workspace}/ai/chatcoder-plan-<sid>[-<tid>].md`：模型用 fs_write 写出的方案文档，
   路径登记在 `turns.plan_doc_path`（兜底来源是 `rollback_writes.path`）。用户点名要清理。
2. `.chatcoder/checkpoints/session-{sid}/{turn_id}/`：备份文件被删掉后留下的空目录树，
   几百个会话批量删除后会在工作区里留下一整片空目录。

下面的用例钉住四道安全约束（按工作区解析、白名单校验、引用检查、越界跳过）与空目录收口。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.persistence.database import async_session_factory, run_write_locked  # noqa: E402
from app.persistence.models.message import Session  # noqa: E402
from app.persistence.models.project import Project  # noqa: E402
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot  # noqa: E402
from app.persistence.models.turn import Turn  # noqa: E402
from app.services import archive_service, session_service  # noqa: E402


@pytest.fixture
def uploads(tmp_path, monkeypatch) -> Path:
    """上传目录指向临时目录，避免清理逻辑碰到真实 uploads。"""
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(settings, "uploads_dir", str(root))
    return root


async def _add(*rows) -> list[int]:
    def _op(s):
        for row in rows:
            s.add(row)
        s.flush()
        ids = [int(r.id) for r in rows]
        s.commit()
        return ids

    return await run_write_locked(_op, label="test.archive.plan.add")


async def _project(path: Path, **kw) -> int:
    path.mkdir(parents=True, exist_ok=True)
    (pid,) = await _add(Project(name=path.name, path=str(path), **kw))
    return pid


async def _session(project_id: int | None, **kw) -> int:
    (sid,) = await _add(Session(project_id=project_id, title="会话", **kw))
    return sid


def _write_plan(workspace: Path, name: str) -> Path:
    (workspace / "ai").mkdir(parents=True, exist_ok=True)
    doc = workspace / "ai" / name
    doc.write_text("# 方案\n正文", encoding="utf-8")
    return doc


# ── A. 计划文档：正常删除 ────────────────────────────────────────────────────

async def test_purge_removes_turn_registered_plan_doc(tmp_path, uploads):
    """`turns.plan_doc_path` 登记的方案文档随会话删除一起清理。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    sid = await _session(pid, status="archived")
    doc = _write_plan(ws, f"chatcoder-plan-{sid}-1.md")
    await _add(Turn(session_id=sid, status="completed", plan_doc_path=f"ai/{doc.name}"))

    result = await archive_service.purge(project_ids=[], session_ids=[sid])

    assert result["ok"] is True
    assert not doc.exists(), "登记在 turns.plan_doc_path 的方案文档必须删除"
    assert result["files_removed"] >= 1


async def test_purge_removes_plan_doc_registered_only_in_rollback_writes(tmp_path, uploads):
    """模型自改名（计划轮没登记路径）时，由 rollback_writes 写盘记录兜底清理。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    sid = await _session(pid, status="archived")
    # 沿用了历史编号的文件名，与 sid 无关
    doc = _write_plan(ws, "chatcoder-plan-9-1.md")
    (turn_id,) = await _add(Turn(session_id=sid, status="completed"))
    await _add(RollbackWrite(session_id=sid, turn_id=turn_id, tool="fs_write",
                             path=f"ai/{doc.name}", old_content=None, new_content="# 方案"))

    await archive_service.purge(project_ids=[], session_ids=[sid])

    assert not doc.exists(), "写盘记录登记的方案文档也要清理"


async def test_plan_doc_uses_session_worktree_workspace(tmp_path, uploads):
    """按**会话自己的工作区**解析：有 worktree 时用 worktree，不用项目路径。"""
    project_dir = tmp_path / "main"
    worktree = tmp_path / "wt"
    pid = await _project(project_dir)
    worktree.mkdir(parents=True)
    sid = await _session(pid, status="archived", worktree_path=str(worktree))
    doc = _write_plan(worktree, f"chatcoder-plan-{sid}-1.md")
    await _add(Turn(session_id=sid, status="completed", plan_doc_path=f"ai/{doc.name}"))

    await archive_service.purge(project_ids=[], session_ids=[sid])

    assert not doc.exists(), "worktree 下的方案文档必须按 worktree 解析并删除"


# ── A. 计划文档：四道安全约束 ────────────────────────────────────────────────

async def test_plan_doc_outside_workspace_is_skipped(tmp_path, uploads):
    """越界路径（`../` 逃逸或绝对路径指向工作区外）一律跳过，文件必须原样保留。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    sid = await _session(pid, status="archived")
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = outside / "chatcoder-plan-1.md"
    escaped.write_text("# 工作区外的文档", encoding="utf-8")
    await _add(
        Turn(session_id=sid, status="completed", plan_doc_path="../outside/chatcoder-plan-1.md"),
        Turn(session_id=sid, status="completed", plan_doc_path=str(escaped)),
    )

    result = await archive_service.purge(project_ids=[], session_ids=[sid])

    assert result["ok"] is True
    assert escaped.exists(), "工作区外的文件绝不能被删除"


async def test_plan_doc_outside_ai_dir_is_skipped(tmp_path, uploads):
    """白名单只认工作区 `ai/` 目录下的文件；其它目录里的同名文件不删。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    sid = await _session(pid, status="archived")
    (ws / "docs").mkdir(parents=True)
    stray = ws / "docs" / "chatcoder-plan-1.md"
    stray.write_text("# 不在 ai 目录", encoding="utf-8")
    await _add(Turn(session_id=sid, status="completed",
                    plan_doc_path="docs/chatcoder-plan-1.md"))

    await archive_service.purge(project_ids=[], session_ids=[sid])

    assert stray.exists(), "只有 ai/ 目录下的方案文档属于清理范围"


async def test_non_plan_file_is_skipped(tmp_path, uploads):
    """非 `chatcoder-plan*.md` 的文件不删（模型写在 ai/ 下的其它笔记属于用户资产）。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    sid = await _session(pid, status="archived")
    (ws / "ai").mkdir(parents=True)
    notes = ws / "ai" / "notes.md"
    notes.write_text("# 笔记", encoding="utf-8")
    suffix = ws / "ai" / "chatcoder-plan-1.txt"
    suffix.write_text("不是 md", encoding="utf-8")
    await _add(
        Turn(session_id=sid, status="completed", plan_doc_path="ai/notes.md"),
        Turn(session_id=sid, status="completed", plan_doc_path="ai/chatcoder-plan-1.txt"),
    )

    await archive_service.purge(project_ids=[], session_ids=[sid])

    assert notes.exists(), "非 chatcoder-plan 前缀的文件不得删除"
    assert suffix.exists(), "后缀不是 .md 的文件不得删除"


async def test_plan_doc_still_referenced_by_other_turn_is_kept(tmp_path, uploads):
    """分支会话继承同一份文档时保留：还有其它 turn 登记它就不能删。

    引用检查必须在**删除写事务提交之后**做——分支会话的行还在库里，按删完之后的库判断。
    """
    ws = tmp_path / "proj"
    pid = await _project(ws)
    branch = await _session(pid, status="archived")
    sid = await _session(pid)
    doc = _write_plan(ws, "chatcoder-plan-shared.md")
    await _add(
        Turn(session_id=sid, status="completed", plan_doc_path=f"ai/{doc.name}"),
        Turn(session_id=branch, status="completed", plan_doc_path=f"ai/{doc.name}"),
    )

    result = await archive_service.purge(project_ids=[], session_ids=[sid])

    assert result["ok"] is True
    assert doc.exists(), "仍被其它 turn 登记的方案文档必须保留"


async def test_plan_doc_reference_check_accepts_backslash_path(tmp_path, uploads):
    """登记值为反斜杠写法（Windows）时同样算「有引用」，不能误删。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    branch = await _session(pid, status="archived")
    sid = await _session(pid)
    doc = _write_plan(ws, "chatcoder-plan-back.md")
    await _add(
        Turn(session_id=sid, status="completed", plan_doc_path=f"ai/{doc.name}"),
        Turn(session_id=branch, status="completed", plan_doc_path=f"ai\\{doc.name}"),
    )

    await archive_service.purge(project_ids=[], session_ids=[sid])

    assert doc.exists(), "反斜杠写法的引用同样要拦住删除"


async def test_single_session_permanent_delete_also_cleans_plan_doc(tmp_path, uploads):
    """单会话永久删除（非归档批量路径）走同一套清理。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    sid = await _session(pid)
    doc = _write_plan(ws, f"chatcoder-plan-{sid}-7.md")
    await _add(Turn(session_id=sid, status="completed", plan_doc_path=f"ai/{doc.name}"))

    async with async_session_factory() as db:
        result = await session_service.delete_session_permanent(db, sid)

    assert result is not None
    assert not doc.exists()
    assert result["files_removed"] >= 1


# ── B. 备份快照的空目录收口 ──────────────────────────────────────────────────

async def test_checkpoint_empty_dirs_are_pruned(tmp_path, uploads):
    """备份文件删完后，`session-{sid}/{turn_id}/` 与 `session-{sid}/` 一并收口。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    sid = await _session(pid, status="archived")
    turn_id = 42
    turn_dir = ws / ".chatcoder" / "checkpoints" / f"session-{sid}" / str(turn_id)
    turn_dir.mkdir(parents=True)
    ckpt = turn_dir / "a.py.bak"
    ckpt.write_text("old", encoding="utf-8")
    await _add(TurnSnapshot(session_id=sid, turn_id=turn_id,
                            file_list=[{"ckpt": str(ckpt), "path": "a.py"}]))

    result = await archive_service.purge(project_ids=[], session_ids=[sid])

    assert result["ok"] is True
    assert not ckpt.exists()
    assert not turn_dir.exists(), "{turn_id} 空目录必须收口"
    assert not (ws / ".chatcoder" / "checkpoints" / f"session-{sid}").exists(), \
        "session-{sid} 空目录必须收口"
    assert (ws / ".chatcoder" / "checkpoints").is_dir(), "checkpoints 根目录必须保留"


async def test_checkpoint_non_empty_dir_is_kept(tmp_path, uploads):
    """目录里还有别的快照（仍被引用）时不动它——`rmdir` 非空即失败，直接停手。"""
    ws = tmp_path / "proj"
    pid = await _project(ws)
    other_pid = await _project(tmp_path / "other")
    sid = await _session(pid, status="archived")
    keep_sid = await _session(other_pid)
    turn_id = 7
    turn_dir = ws / ".chatcoder" / "checkpoints" / f"session-{sid}" / str(turn_id)
    turn_dir.mkdir(parents=True)
    doomed = turn_dir / "a.py.bak"
    kept = turn_dir / "b.py.bak"
    for p in (doomed, kept):
        p.write_text("old", encoding="utf-8")
    await _add(
        TurnSnapshot(session_id=sid, turn_id=turn_id,
                     file_list=[{"ckpt": str(doomed), "path": "a.py"}]),
        # 同一目录下的另一份备份仍被别的会话引用
        TurnSnapshot(session_id=keep_sid, turn_id=99,
                     file_list=[{"ckpt": str(kept), "path": "b.py"}]),
    )

    await archive_service.purge(project_ids=[], session_ids=[sid])

    assert not doomed.exists()
    assert kept.exists(), "仍被引用的备份文件必须保留"
    assert turn_dir.is_dir(), "非空目录不得被收口"


def test_prune_empty_dirs_never_removes_checkpoint_root(tmp_path):
    """收口函数止于 checkpoints 根：即使整个 session 目录都空了也不删根。"""
    root = tmp_path / ".chatcoder" / "checkpoints"
    leaf = root / "session-1" / "2"
    leaf.mkdir(parents=True)

    removed = session_service._prune_empty_dirs(leaf, root, levels=5)

    assert removed == 2  # 只删 2 与 session-1
    assert root.is_dir()
