# -*- coding: utf-8 -*-
"""plan-20261019-diff: 编辑工具 diff 计算与展示口径回归。

覆盖：
1. UTF-8 采样恰好在 8192 字节截断中文多字节字符时，不再误判二进制；
2. 超限文本与真二进制区分：前者仍能生成 +N -M 与行级 diff 窗口；
3. 同一次调用对同一文件的多条写盘记录合并为一次变更；
4. call_key 未命中且记录都带 key 时不回退整轮（避免展开第一次看到第二次改动）；
5. 连续编辑：第一次只显示第二行改动，第二次只显示第三行改动（before 已含第一次）；
6. list_turn_changes 对超限文本按窗口统计，不再固定 +0 -0。
"""
import pytest

from app.persistence.database import async_session_factory
from app.persistence.models.message import Session
from app.persistence.models.rollback import RollbackWrite
from app.services import rollback_service


def test_sample_is_text_handles_utf8_boundary():
    """8192 字节处必然落在某个中文字符中间（每字 3 字节），旧实现会误判二进制。"""
    raw = ("中" * 4000).encode("utf-8")
    assert len(raw) > rollback_service._BINARY_SNIFF_BYTES
    sample = raw[: rollback_service._BINARY_SNIFF_BYTES]
    assert rollback_service._sample_is_text(sample, "docs/a.md") is True


def test_inspect_write_target_distinguishes_oversize_text(workspace):
    text = "".join(f"line {i}\n" for i in range(500))
    (workspace / "big.md").write_text(text, encoding="utf-8")
    kind, _size = rollback_service.inspect_write_target(str(workspace), "big.md", max_bytes=100)
    assert kind == rollback_service.FILE_KIND_OVERSIZE
    # 回滚口径仍是「不可存全文」，走 checkpoint
    assert rollback_service._is_binary_path(str(workspace), "big.md", 100) is True

    (workspace / "a.bin").write_bytes(b"PK\x03\x04\x00\x00")
    kind2, _ = rollback_service.inspect_write_target(str(workspace), "a.bin", max_bytes=10_000)
    assert kind2 == rollback_service.FILE_KIND_BINARY


def test_build_diff_window_keeps_change_region():
    before = "\n".join(f"line {i}" for i in range(200))
    after = before.replace("line 100", "line 100 changed")
    b, a = rollback_service.build_diff_window(before, after)
    assert b is not None and a is not None
    assert "line 100 changed" in a
    assert "line 0" not in b  # 远端内容被裁掉，只保留变更区周边
    assert rollback_service.diff_stats(b, a) == (1, 1)


async def _make_session(db) -> Session:
    session = Session(project_id=None, permission_mode="agent")
    db.add(session)
    await db.flush()
    return session


@pytest.mark.asyncio
async def test_get_file_diff_oversize_uses_window():
    async with async_session_factory() as db:
        s = await _make_session(db)
        before = "\n".join(f"line {i}" for i in range(300))
        after = before.replace("line 150", "line 150 changed")
        b, a = rollback_service.build_diff_window(before, after)
        db.add(RollbackWrite(
            session_id=s.id, turn_id=11, tool="editor_apply_diff", path="notes.md",
            old_content=None, new_content=None, binary=True, call_key="tc_big",
            diff_before=b, diff_after=a,
        ))
        await db.flush()
        diff = await rollback_service.get_file_diff(
            db, session_id=s.id, turn_id=11, workspace=".", path="notes.md", call_key="tc_big")
        assert diff is not None
        assert diff["lines"]
        assert (diff["additions"], diff["deletions"]) == (1, 1)
        assert "变更窗口" in (diff["reason"] or "")

        changes = await rollback_service.list_turn_changes(
            db, session_id=s.id, turn_id=11, workspace=".")
        assert len(changes) == 1
        assert (changes[0]["additions"], changes[0]["deletions"]) == (1, 1)
        await db.rollback()


@pytest.mark.asyncio
async def test_call_key_merges_multiple_edits_same_file():
    """multi_file_edit 对同一文件的两处编辑属于同一次变更，不能只取第一条。"""
    async with async_session_factory() as db:
        s = await _make_session(db)
        db.add_all([
            RollbackWrite(session_id=s.id, turn_id=12, tool="multi_file_edit",
                          path="a.md", old_content="a\nb\nc\n", new_content="a\nB\nc\n",
                          binary=False, call_key="tc_multi"),
            RollbackWrite(session_id=s.id, turn_id=12, tool="multi_file_edit",
                          path="a.md", old_content="a\nB\nc\n", new_content="a\nB\nc\nd\n",
                          binary=False, call_key="tc_multi"),
        ])
        await db.flush()
        diff = await rollback_service.get_file_diff(
            db, session_id=s.id, turn_id=12, workspace=".", path="a.md", call_key="tc_multi")
        assert diff is not None and diff["single_edit"] is True
        assert diff["before"] == "a\nb\nc\n"
        assert diff["after"] == "a\nB\nc\nd\n"
        assert (diff["additions"], diff["deletions"]) == (2, 1)
        await db.rollback()


@pytest.mark.asyncio
async def test_sequential_edits_only_show_own_change():
    """第一次改第二行、第二次改第三行：各自展开只看自己的改动，且第二次 before 含第一次结果。"""
    async with async_session_factory() as db:
        s = await _make_session(db)
        db.add_all([
            RollbackWrite(session_id=s.id, turn_id=13, tool="editor_apply_diff",
                          path="doc.md", old_content="l1\nl2\nl3\n", new_content="l1\nL2\nl3\n",
                          binary=False, call_key="tc_1"),
            RollbackWrite(session_id=s.id, turn_id=13, tool="editor_apply_diff",
                          path="doc.md", old_content="l1\nL2\nl3\n", new_content="l1\nL2\nL3\n",
                          binary=False, call_key="tc_2"),
        ])
        await db.flush()
        first = await rollback_service.get_file_diff(
            db, session_id=s.id, turn_id=13, workspace=".", path="doc.md", call_key="tc_1")
        second = await rollback_service.get_file_diff(
            db, session_id=s.id, turn_id=13, workspace=".", path="doc.md", call_key="tc_2")
        assert first is not None and second is not None
        assert first["before"] == "l1\nl2\nl3\n" and first["after"] == "l1\nL2\nl3\n"
        assert second["before"] == "l1\nL2\nl3\n" and second["after"] == "l1\nL2\nL3\n"

        def changed_text(diff):
            return [ln["text"] for ln in diff["lines"] if ln["type"] in ("add", "del")]

        assert changed_text(first) == ["l2", "L2"]
        assert changed_text(second) == ["l3", "L3"]
        await db.rollback()


@pytest.mark.asyncio
async def test_call_key_miss_does_not_fall_back_to_turn_accumulation():
    """记录都带 call_key 但都不匹配时返回 None，让前端用调用参数兜底。"""
    async with async_session_factory() as db:
        s = await _make_session(db)
        db.add(RollbackWrite(session_id=s.id, turn_id=14, tool="editor_apply_diff",
                             path="x.md", old_content="a\n", new_content="a\nb\n",
                             binary=False, call_key="tc_real"))
        await db.flush()
        assert await rollback_service.get_file_diff(
            db, session_id=s.id, turn_id=14, workspace=".", path="x.md",
            call_key="tc_other") is None

        # 老记录（无 call_key）仍可回退整轮，避免历史消息展开空白
        db.add(RollbackWrite(session_id=s.id, turn_id=15, tool="fs_write",
                             path="old.md", old_content="a\n", new_content="a\nb\n",
                             binary=False, call_key=None))
        await db.flush()
        legacy = await rollback_service.get_file_diff(
            db, session_id=s.id, turn_id=15, workspace=".", path="old.md",
            call_key="tc_other")
        assert legacy is not None and legacy["single_edit"] is False
        await db.rollback()
