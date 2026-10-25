"""「先读后改」闸门单测（plan-548-2277）。

守护点：
1. read_cache 能记住实际读过的行区间，并正确判断「目标行是否被覆盖」；
2. apply_replacement 把命中区间带回（没有它就无法判断"要改的那几行看过没"）；
3. 闸门默认 warn（放行 + 警告，data.read_verified=False），strict 才拒绝且不写盘。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.orchestration.tools import read_cache as read_cache_mod
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.editor import EditorApplyDiffTool
from app.orchestration.tools.multi_edit import MultiFileEditTool
from app.orchestration.tools.read_cache import ReadWindowCache
from app.orchestration.tools.text_edit import apply_replacement


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件不碰数据库。

    这些用例只操作 tmp_path 下的文本文件与 ReadWindowCache。conftest 的同名 fixture
    每个用例都 drop/create 全量表，与其它文件类测试集一起跑时会偶发
    `no such table`（表名随机）的 teardown 报错，与用例内容无关（同 test_grep_paging.py）。
    """
    yield


def _ctx(tmp_path: Path, cache: ReadWindowCache | None = None) -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path), session_id=1, task_id=1,
        agent_id=1, agent_name="test", read_cache=cache if cache is not None else ReadWindowCache(),
    )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ── read_cache.covers ──────────────────────────────────────────────────────────
def test_covers_requires_full_coverage():
    c = ReadWindowCache()
    p = Path("/tmp/gate_a.py")  # 不要求真实存在：只测区间判定
    assert c.covers(p, 10, 20) is False
    c.record_range(p, 1, 50)
    assert c.covers(p, 10, 20) is True
    assert c.covers(p, 10, 60) is False  # 超出已读区间
    assert c.has_ranges(p) is True


def test_covers_after_adjacent_ranges_merge():
    # plan-549-2281 复盘修复：相邻区间（1-30 与 31-60）合并后应覆盖跨段目标 20-40——
    # 此前 covers 要求单个已登记区间完整覆盖，模型先读 1-30 再读 31-60 后改 20-40 行
    # 会被误判「未读」（strict 误伤）；本用例曾把误伤固化成断言，现在反转为正确行为。
    c = ReadWindowCache()
    p = Path("/tmp/gate_b.py")
    c.record_range(p, 1, 30)
    c.record_range(p, 31, 60)
    assert c.covers(p, 20, 40) is True
    assert c.covers(p, 20, 61) is False  # 合并后的整段是 1-60，超出仍不覆盖


def test_known_file_always_covered():
    c = ReadWindowCache()
    p = Path("/tmp/gate_c.py")
    assert c.covers(p, 5, 9) is False
    c.mark_known(p)  # 本会话自己写过的文件，内容已知
    assert c.covers(p, 5, 9) is True


def test_record_range_ignores_invalid():
    c = ReadWindowCache()
    p = Path("/tmp/gate_d.py")
    c.record_range(p, 0, 10)   # 起始行非法
    c.record_range(p, 10, 5)   # end < start
    assert c.has_ranges(p) is False


# ── apply_replacement 带回命中区间 ───────────────────────────────────────────
def test_replace_result_reports_hit_lines():
    r = apply_replacement("line1\nline2\nline3\nline4\n", "line2\nline3", "X")
    assert r.ok
    assert (r.start_line, r.end_line) == (2, 3)


def test_replace_result_failure_has_no_lines():
    r = apply_replacement("a\nb\n", "zzz", "y")
    assert not r.ok
    assert r.start_line == 0 and r.end_line == 0


def test_replace_all_reports_every_hit_span():
    # plan-549-2281 复盘修复：replace_all 必须带回全部命中区间（spans）——
    # 此前只有第一处，闸门对其余替换位置静默放行。
    content = "a = 1\nkeep\na = 1\nkeep\na = 1\n"
    r = apply_replacement(content, "a = 1", "a = 2", replace_all=True)
    assert r.ok and r.count == 3
    assert [tuple(s) for s in r.spans] == [(1, 1), (3, 3), (5, 5)]
    assert (r.start_line, r.end_line) == (1, 1)  # 首处仍写进 start/end


def test_whitespace_strategy_reports_real_file_span():
    # plan-549-2281 复盘修复：空白归一化策略此前用 old_text 的换行数估算 end_line，
    # 文件实际换行数与 old_text 不同时区间会偏移；现在按原文实际行号计算
    # （文件里 2 行，old_text 却写了 3 行——旧算法会算出 1-3）。
    r = apply_replacement("alpha beta\ngamma\n", "alpha\nbeta\ngamma", "x y z")
    assert r.ok and r.strategy == "whitespace"
    assert (r.start_line, r.end_line) == (1, 2)
    assert [tuple(s) for s in r.spans] == [(1, 2)]


# ── editor 闸门：默认 warn ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_editor_warn_allows_with_warning(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "edit_gate_mode", "warn", raising=False)
    p = _write(tmp_path, "a.py", "x = 1\ny = 2\n")
    res = await EditorApplyDiffTool().run(
        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 10"}, _ctx(tmp_path),
    )
    assert res.ok, res.error
    assert res.data["read_verified"] is False
    assert "未读取" in res.output
    assert "fs_read(" in res.output          # 给出可直接复制的核对调用
    assert "x = 10" in p.read_text(encoding="utf-8")  # 放行了，改动已落盘


@pytest.mark.asyncio
async def test_editor_strict_rejects_unread_region(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "edit_gate_mode", "strict", raising=False)
    p = _write(tmp_path, "a.py", "x = 1\n")
    cache = ReadWindowCache()
    cache.record_range(p, 100, 200)  # 有读取记录，但没覆盖目标行
    res = await EditorApplyDiffTool().run(
        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, _ctx(tmp_path, cache),
    )
    assert not res.ok
    assert res.error_code == "STALE_READ"
    assert "fs_read(" in res.error
    assert p.read_text(encoding="utf-8") == "x = 1\n"  # 一个字都没写


@pytest.mark.asyncio
async def test_editor_strict_allows_when_covered(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "edit_gate_mode", "strict", raising=False)
    p = _write(tmp_path, "a.py", "x = 1\n")
    cache = ReadWindowCache()
    cache.record_range(p, 1, 1)
    res = await EditorApplyDiffTool().run(
        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, _ctx(tmp_path, cache),
    )
    assert res.ok, res.error
    assert res.data["read_verified"] is True
    assert p.read_text(encoding="utf-8") == "x = 2\n"


@pytest.mark.asyncio
async def test_editor_off_mode_has_no_warning(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "edit_gate_mode", "off", raising=False)
    _write(tmp_path, "a.py", "x = 1\n")
    res = await EditorApplyDiffTool().run(
        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, _ctx(tmp_path),
    )
    assert res.ok
    assert res.data["read_verified"] is True
    assert "未读取" not in res.output


# ── 会话缓存清理（plan-549-2281）──────────────────────────────────────────────
def test_clear_cache_for_session_drops_reused_id():
    """删除会话 A 后新会话 B 复用同一 id：不得继承 A 的已读区间与已知文件。

    SQLite 主键没有 AUTOINCREMENT（见 persistence/database.py），会话 id 会被复用；
    若不清缓存，B 的 editor strict 闸门与 fs_write「先读后写」都会被 A 的残留记录放行。
    """
    p = Path("/tmp/gate_reuse.py")
    cache_a = read_cache_mod.read_cache_for(7, lambda: [])
    cache_a.record_range(p, 1, 50)
    cache_a.mark_known(p)
    assert cache_a.covers(p, 10, 20) is True
    assert cache_a.has_read(p) is True

    read_cache_mod.clear_cache_for_session(7)

    cache_b = read_cache_mod.read_cache_for(7, lambda: [])  # 同一 id，新会话
    assert cache_b is not cache_a
    assert cache_b.covers(p, 10, 20) is False
    assert cache_b.has_read(p) is False
    assert cache_b.has_any() is False


def test_clear_all_empties_every_session():
    read_cache_mod.read_cache_for(1, lambda: []).mark_known(Path("/tmp/gate_x.py"))
    read_cache_mod.read_cache_for(2, lambda: []).mark_known(Path("/tmp/gate_y.py"))

    read_cache_mod.clear_all()

    assert read_cache_mod.read_cache_for(1, lambda: []).has_any() is False
    assert read_cache_mod.read_cache_for(2, lambda: []).has_any() is False


def test_clear_cache_for_unknown_session_is_noop():
    """清一个不存在的会话不能抛错（网关层会无条件调用）。"""
    read_cache_mod.clear_cache_for_session(999999)


# ── multi_file_edit 也要 mark_known（与 editor / fs_write 同口径）─────────────
@pytest.mark.asyncio
async def test_multi_edit_marks_known_after_write(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "edit_gate_mode", "strict", raising=False)
    p = _write(tmp_path, "m.py", "a = 1\nb = 2\n")
    cache = ReadWindowCache()
    cache.record_range(p, 1, 2)  # 先读过，否则 strict 会拒绝这次 multi_file_edit

    ctx = _ctx(tmp_path, cache)
    res = await MultiFileEditTool().run(
        {"edits": [{"path": "m.py", "old_text": "a = 1", "new_text": "a = 10"}]}, ctx,
    )
    assert res.ok, res.error
    # 写盘成功后必须登记为已知：否则随后改同一区域会被 strict 误拒
    assert cache.covers(p, 1, 1) is True
    assert cache.has_read(p) is True

    second = await EditorApplyDiffTool().run(
        {"path": "m.py", "old_text": "a = 10", "new_text": "a = 100"}, ctx,
    )
    assert second.ok, second.error
    assert second.data["read_verified"] is True
    assert "未读取" not in second.output
    assert "a = 100" in p.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_multi_edit_does_not_mark_known_when_write_fails(tmp_path, monkeypatch):
    """写盘回滚时不登记：文件内容仍是原样，模型并未掌握新内容。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "edit_gate_mode", "off", raising=False)
    p = _write(tmp_path, "n.py", "keep = 1\n")
    cache = ReadWindowCache()
    ctx = _ctx(tmp_path, cache)

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    res = await MultiFileEditTool().run(
        {"edits": [{"path": "n.py", "old_text": "keep = 1", "new_text": "keep = 2"}]}, ctx,
    )
    assert not res.ok
    assert cache.has_read(p) is False

