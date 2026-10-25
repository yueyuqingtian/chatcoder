"""归档永久删除的挂靠子会话与工具输出目录回归用例（plan-560-2311 E1）。

缺陷原貌（两处，都在「批量永久删除」这条链上）：

1. `archive_service._resolve_targets` 只解析「项目 → 会话」与自己选中的会话，没有把
   `side_parent_id` 指向这些主会话的子会话纳入删除集合。主会话一删，未归档的历史子会话
   `side_parent_id` 悬空：`list_all_children` 用 inner join 过滤掉它、`list_sessions` 又
   过滤 `side_parent_id IS NULL`，界面上既看不到也删不掉——永久孤儿数据。
2. `archive_service` 与 `worktree_service` 的批量删除绕开 `delete_session_permanent`
   直接调 `purge_session_children`，顺带漏掉了 `_purge_tool_outputs`：`uploads/tool-outputs/{sid}`
   按已删会话 id 一直累积（每次超预算输出最多 2MB）。

下面的用例钉住「子会话一并删除」与「工具输出目录被清理」两件事。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.orchestration.output_paging import SPILL_DIRNAME  # noqa: E402
from app.persistence.database import async_session_factory, run_write_locked  # noqa: E402
from app.persistence.models.message import Session  # noqa: E402
from app.persistence.models.project import Project  # noqa: E402
from app.services import archive_service, session_service  # noqa: E402


@pytest.fixture
def uploads(tmp_path, monkeypatch) -> Path:
    """把上传目录指向临时目录，工具输出目录也随之落在里面。"""
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

    return await run_write_locked(_op, label="test.archive.children.add")


async def _project(path: Path, **kw) -> int:
    path.mkdir(parents=True, exist_ok=True)
    (pid,) = await _add(Project(name=path.name, path=str(path), **kw))
    return pid


async def _session(project_id: int | None, **kw) -> int:
    (sid,) = await _add(Session(project_id=project_id, title="会话", **kw))
    return sid


async def _count_sessions(ids: list[int]) -> int:
    async with async_session_factory() as db:
        return int((await db.execute(
            select(func.count()).select_from(Session).where(Session.id.in_(ids))
        )).scalar() or 0)


# ── 1. _resolve_targets 纳入挂靠子会话 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_targets_includes_side_children(tmp_path):
    """plan-560-2311 E1：主会话被选中时，按 `side_parent_id` 挂靠的子会话必须一起进入集合。"""
    pid = await _project(tmp_path / "p")
    parent = await _session(pid, status="archived")
    child = await _session(pid, side_parent_id=parent, side_kind="side")
    other = await _session(pid, status="archived")

    targets = await archive_service._resolve_targets([], [parent])

    assert set(targets["sessions"]) == {parent, child}, "挂靠子会话必须随主会话一起删除"
    assert other not in targets["sessions"], "不相关会话不得被卷入"


@pytest.mark.asyncio
async def test_resolve_targets_includes_children_of_project_sessions(tmp_path):
    """按项目删除时同样要带上项目内会话的挂靠子会话（孤儿走的是同一条出口）。"""
    pid = await _project(tmp_path / "p2")
    parent = await _session(pid)
    child = await _session(pid, side_parent_id=parent, side_kind="sub")

    targets = await archive_service._resolve_targets([pid], [])

    assert set(targets["sessions"]) == {parent, child}


@pytest.mark.asyncio
async def test_resolve_targets_walks_multi_level_children(tmp_path):
    """多层挂靠（子会话自己又被挂靠）也要收敛到不动点，不能只加一层。"""
    pid = await _project(tmp_path / "p3")
    root = await _session(pid)
    mid = await _session(pid, side_parent_id=root, side_kind="side")
    leaf = await _session(pid, side_parent_id=mid, side_kind="side")

    targets = await archive_service._resolve_targets([], [root])

    assert set(targets["sessions"]) == {root, mid, leaf}


@pytest.mark.asyncio
async def test_resolve_targets_reports_missing_without_children(tmp_path):
    """不存在的 id 仍计入 missing，且不会因为遍历子会话而误报。"""
    pid = await _project(tmp_path / "p4")
    parent = await _session(pid)

    targets = await archive_service._resolve_targets([], [parent, 987654])

    assert targets["missing_sessions"] == [987654]
    assert targets["sessions"] == [parent]


# ── 2. purge 真正删掉子会话 + 工具输出目录 ────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_removes_side_children(tmp_path, uploads):
    """端到端：删除主会话后，挂靠子会话不留孤儿。"""
    pid = await _project(tmp_path / "proj")
    parent = await _session(pid, status="archived")
    child = await _session(pid, side_parent_id=parent, side_kind="side")

    result = await archive_service.purge(project_ids=[], session_ids=[parent])

    assert result["ok"] is True
    assert result["deleted_sessions"] == 2
    assert await _count_sessions([parent, child]) == 0


@pytest.mark.asyncio
async def test_purge_removes_tool_output_dirs(tmp_path, uploads):
    """plan-560-2311 E1：批量删除必须清掉 `uploads/tool-outputs/{sid}`。

    旧实现绕开 `delete_session_permanent` 直接调 `purge_session_children`，这两个目录
    按已删会话 id 一直留在磁盘上（每次超预算输出最多 2MB）。
    """
    pid = await _project(tmp_path / "proj2")
    kept_pid = await _project(tmp_path / "keep")
    sid = await _session(pid, status="archived")
    kept = await _session(kept_pid)

    root = uploads / SPILL_DIRNAME
    for target in (sid, kept):
        d = root / str(target)
        d.mkdir(parents=True)
        (d / "out.txt").write_text("x" * 100, encoding="utf-8")

    result = await archive_service.purge(project_ids=[pid], session_ids=[])

    assert result["tool_output_dirs_removed"] == 1, "只算被删会话的目录数"
    assert not (root / str(sid)).exists(), "已删会话的工具输出目录必须清理"
    assert (root / str(kept)).is_dir(), "未删会话的目录不得被误删"


# ── 3. 公开入口存在且行为正确 ─────────────────────────────────────────────────

def test_public_tool_output_purge_entry_exists():
    """跨模块清理走 `session_service.purge_session_tool_outputs` 这个公开入口。"""
    assert callable(session_service.purge_session_tool_outputs)


def test_public_tool_output_purge_removes_only_that_session(tmp_path, monkeypatch):
    """公开入口只删指定会话的目录，且对不存在的目录是幂等的。"""
    root = tmp_path / "uploads"
    monkeypatch.setattr(settings, "uploads_dir", str(root))

    target = root / SPILL_DIRNAME / "11"
    other = root / SPILL_DIRNAME / "12"
    for d in (target, other):
        d.mkdir(parents=True)
        (d / "a.txt").write_text("x", encoding="utf-8")

    session_service.purge_session_tool_outputs(11)

    assert not target.exists()
    assert other.is_dir()
    session_service.purge_session_tool_outputs(11)  # 再清一次不抛错
