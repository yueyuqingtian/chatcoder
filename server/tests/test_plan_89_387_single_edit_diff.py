# -*- coding: utf-8 -*-
"""plan-89-387: 写操作卡片「本次编辑」diff 与路径别名的回归测试。

背景：同一轮内同一文件被多次编辑时，展开的 diff 曾是整轮累积口径
（before=首条写盘前内容、after=当前磁盘内容），既与卡片 +N -M 对不上，
也无法区分第几次编辑的变更。此处锁定两个修复点：
1. get_file_diff 传 call_key 时只返回该次写盘的前后内容；
2. resolve_write_paths 兼容 file_path / filepath 路径别名。
"""
import pytest
from app.persistence.database import async_session_factory
from app.persistence.models.message import Session
from app.persistence.models.rollback import RollbackWrite
from app.services import rollback_service


@pytest.mark.asyncio
async def test_get_file_diff_by_call_key_returns_single_edit():
    """同轮同文件两次编辑：传 call_key 各取本次变更；不传回退整轮累积口径。"""
    async with async_session_factory() as db:
        session = Session(project_id=None, permission_mode="agent")
        db.add(session)
        await db.flush()
        db.add_all([
            RollbackWrite(session_id=session.id, turn_id=7, tool="fs_write",
                          path="src/App.tsx", old_content="a\nb\nc\n", new_content="a\nB\nc\n",
                          binary=False, call_key="tc_first"),
            RollbackWrite(session_id=session.id, turn_id=7, tool="fs_write",
                          path="src/App.tsx", old_content="a\nB\nc\n", new_content="a\nB\nc\nd\n",
                          binary=False, call_key="tc_second"),
        ])
        await db.flush()

        first = await rollback_service.get_file_diff(
            db, session_id=session.id, turn_id=7, workspace=".", path="src/App.tsx",
            call_key="tc_first",
        )
        assert first is not None
        assert first["single_edit"] is True
        assert (first["additions"], first["deletions"]) == (1, 1)
        # before/after 取记录自身内容，不是该轮首条写盘前内容或当前磁盘
        assert first["before"] == "a\nb\nc\n"
        assert first["after"] == "a\nB\nc\n"

        second = await rollback_service.get_file_diff(
            db, session_id=session.id, turn_id=7, workspace=".", path="src/App.tsx",
            call_key="tc_second",
        )
        assert second is not None
        assert (second["additions"], second["deletions"]) == (1, 0)
        assert second["before"] == "a\nB\nc\n"
        assert second["after"] == "a\nB\nc\nd\n"
        # 两次编辑的 diff 不同——这是用户反馈「每次应只展示本次变更」的核心
        assert first["lines"] != second["lines"]

        # 未命中 call_key（老数据）时回退整轮累积口径，仍能拿到内容
        fallback = await rollback_service.get_file_diff(
            db, session_id=session.id, turn_id=7, workspace=".", path="src/App.tsx",
            call_key="tc_missing",
        )
        assert fallback is not None
        assert fallback["single_edit"] is False

        await db.rollback()


@pytest.mark.asyncio
async def test_get_file_diff_call_key_matches_by_binary_record():
    """二进制记录按 call_key 命中时给出说明而非空白（前端据此展示原因）。"""
    async with async_session_factory() as db:
        session = Session(project_id=None, permission_mode="agent")
        db.add(session)
        await db.flush()
        db.add(RollbackWrite(session_id=session.id, turn_id=8, tool="fs_write",
                             path="logo.png", old_content=None, new_content=None,
                             binary=True, call_key="tc_bin"))
        await db.flush()

        diff = await rollback_service.get_file_diff(
            db, session_id=session.id, turn_id=8, workspace=".", path="logo.png",
            call_key="tc_bin",
        )
        assert diff is not None
        assert diff["single_edit"] is True
        assert diff["lines"] is None
        assert diff["reason"]

        await db.rollback()


def test_resolve_write_paths_accepts_aliases():
    """plan-89-387: 路径键名别名（file_path/filepath）也要能解析出写盘目标。

    此前只认 args.path，模型用别名传参时不落写盘记录、不生成 change_stat，
    表现为「工具卡没有 +N -M、展开看不到变更」。
    """
    assert rollback_service.resolve_write_paths("fs_write", {"file_path": "a.ts"}) == ["a.ts"]
    assert rollback_service.resolve_write_paths("editor_apply_diff", {"filepath": "b.ts"}) == ["b.ts"]
    assert rollback_service.resolve_write_paths("fs_write", {"path": "c.ts"}) == ["c.ts"]
    assert rollback_service.resolve_write_paths(
        "multi_file_edit", {"edits": [{"file_path": "d.ts"}, {"path": "e.ts"}]},
    ) == ["d.ts", "e.ts"]
    assert rollback_service.resolve_write_paths("fs_write", {}) == []
