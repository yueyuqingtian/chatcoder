"""fs_grep 分页（plan-548-2276 阶段 4.1）：offset / max_matches 不重不漏。

验收口径：同一份结果翻页取回的集合必须等于全量（不重、不漏），
截断时头部的「下一次调用」必须可直接复制执行，且参数按本次实际传入值回填。
两条搜索路径（纯 Python 遍历 / ripgrep 快路径）都要覆盖。
"""
import re
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.ripgrep_env import locate_ripgrep
from app.orchestration.tools import grep as grep_mod
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.grep import GrepTool

needs_rg = pytest.mark.skipif(locate_ripgrep() is None, reason="本机没有 ripgrep，跳过快路径测试")

_TOTAL = 120  # 单个文件里 needle 出现在 120 行上


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件不碰数据库。

    grep 分页只读写 tmp_path 下的文本文件；conftest 的同名 fixture 会为每个用例
    drop/create 全量表，与文件类测试集在一起跑时偶发 `no such table` teardown 报错（与用例无关）。
    """
    yield


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        workspace_root=str(root), session_id=1, task_id=1, agent_id=1, agent_name="tester",
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """一个含 120 条命中行的文件（纯 Python 与 rg 两条路径共用）。"""
    body = "\n".join(f"needle {i}" for i in range(1, _TOTAL + 1))
    (tmp_path / "hits.txt").write_text(body + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def py_only(monkeypatch):
    """强制走纯 Python 逐行匹配分支，专测这条路径的分页。"""
    monkeypatch.setattr(grep_mod, "locate_ripgrep", lambda: None)
    monkeypatch.setattr(settings, "enhanced_search", True)


def _line_numbers(output: str) -> list[int]:
    """从 `hits.txt:12: needle 12` 里取行号（头部说明行不以路径开头，天然被过滤）。"""
    return [int(ln.split(":")[1]) for ln in output.splitlines() if ln.startswith("hits.txt:")]


@pytest.mark.asyncio
async def test_python_path_pages_cover_everything_once(tree, py_only):
    collected: list[int] = []
    for offset in range(0, _TOTAL, 50):
        r = await GrepTool().run(
            {"pattern": "needle", "path": ".", "offset": offset, "max_matches": 50}, _ctx(tree),
        )
        assert r.ok
        assert r.data["offset"] == offset
        expected = list(range(offset + 1, min(offset + 50, _TOTAL) + 1))
        assert _line_numbers(r.output) == expected
        collected.extend(expected)

    assert sorted(collected) == list(range(1, _TOTAL + 1))
    assert len(collected) == len(set(collected)), "翻页出现重复项"


@needs_rg
@pytest.mark.asyncio
async def test_rg_path_pages_cover_everything_once(tree):
    collected: list[int] = []
    for offset in range(0, _TOTAL, 50):
        r = await GrepTool().run(
            {"pattern": "needle", "path": ".", "offset": offset, "max_matches": 50}, _ctx(tree),
        )
        assert r.ok
        assert "(rg)" in r.output, "本机有 ripgrep 却走了纯 Python 回退，分页快路径未被覆盖"
        expected = list(range(offset + 1, min(offset + 50, _TOTAL) + 1))
        assert _line_numbers(r.output) == expected
        collected.extend(expected)

    assert sorted(collected) == list(range(1, _TOTAL + 1))
    assert len(collected) == len(set(collected)), "翻页出现重复项"


@pytest.mark.asyncio
async def test_truncation_gives_copyable_next_call(tree, py_only):
    """截断时头部必须给出可直接复制的下一次调用（参数按实际传入值回填）。"""
    r = await GrepTool().run({"pattern": "needle", "path": ".", "max_matches": 50}, _ctx(tree))
    assert r.ok
    assert r.data["has_more"] is True
    assert r.data["returned"] == 50
    assert 'fs_grep(pattern="needle", path=".", offset=50)' in r.output


@pytest.mark.asyncio
async def test_next_call_keeps_include_filter(tree, py_only):
    """include 也要回填，否则续读会搜到别的文件类型、与第一页对不上。"""
    r = await GrepTool().run(
        {"pattern": "needle", "path": ".", "include": "*.txt", "max_matches": 10}, _ctx(tree),
    )
    assert r.ok
    assert 'include="*.txt"' in r.output
    assert 'offset=10' in r.output


@pytest.mark.asyncio
async def test_last_page_has_no_continuation(tree, py_only):
    r = await GrepTool().run({"pattern": "needle", "path": ".", "offset": 100}, _ctx(tree))
    assert r.ok
    assert r.data["has_more"] is False
    assert r.data["returned"] == 20
    assert "如需继续请调用" not in r.output
    assert "已跳过前 100 条" in r.output


@pytest.mark.asyncio
async def test_max_matches_clamped(tree, py_only):
    """上限 200：超过就夹到 200，不能让一次调用把上下文灌满。"""
    r = await GrepTool().run({"pattern": "needle", "path": ".", "max_matches": 5000}, _ctx(tree))
    assert r.ok
    assert r.data["max_matches"] == 200
    assert r.data["returned"] == _TOTAL  # 全部命中一次取完，未截断


@pytest.mark.asyncio
async def test_offset_past_end(tree, py_only):
    r = await GrepTool().run({"pattern": "needle", "path": ".", "offset": 500}, _ctx(tree))
    assert r.ok
    assert r.output.startswith("本页无匹配")


@pytest.mark.asyncio
async def test_paging_with_context_lines(tree, py_only):
    """上下文行模式下也要跳过 offset 之前的命中（不能把上下文当匹配计数）。"""
    r = await GrepTool().run(
        {"pattern": "needle", "path": ".", "offset": 1, "max_matches": 1, "context_lines": 1},
        _ctx(tree),
    )
    assert r.ok
    assert r.data["returned"] == 1
    assert ">> 2:" in r.output
    assert ">> 1:" not in r.output


@pytest.mark.asyncio
@pytest.mark.parametrize("use_rg", [False, True])
async def test_exact_fill_has_no_bogus_limit_note(tree, monkeypatch, use_rg):
    """plan-549-2281 复盘修复：命中数恰好等于「本页最后一条」时，页头与 notes 口径必须一致。

    此前 _page_notes 用 `scanned >= window` 判定，末条恰好填满窗口就追加「已达本次上限，
    如需继续请调用 …」，而页头写的是「找到 120 个匹配」（无 +）。模型照着提示翻页，
    得到的却是「本页无匹配」，白耗一次调用。
    """
    if not use_rg:
        monkeypatch.setattr(grep_mod, "locate_ripgrep", lambda: None)
    elif locate_ripgrep() is None:
        pytest.skip("本机没有 ripgrep，跳过快路径用例")

    # offset=20 + max_matches=100 → window=120 == 总命中数，正好取到末条
    r = await GrepTool().run(
        {"pattern": "needle", "path": ".", "offset": 20, "max_matches": 100}, _ctx(tree),
    )
    assert r.ok
    assert r.data["has_more"] is False
    assert r.data["returned"] == 100
    assert "如需继续请调用" not in r.output, "没有更多结果就不该提示续读（翻页会得到空页）"
    assert f"找到 {_TOTAL} 个匹配" in r.output


@pytest.mark.asyncio
async def test_scan_cap_note_when_offset_past_end(tmp_path, py_only, monkeypatch):
    """plan-549-2281 复盘修复：撞上文件扫描上限时空结果必须说清，不能让模型以为已到底。"""
    monkeypatch.setattr(grep_mod, "_MAX_FILES_SCAN", 1)
    # 两个文件：扫完第一个就顶到上限，total 只是「已扫描部分」的计数
    for name in ("a.txt", "b.txt"):
        body = "\n".join(f"needle {name}-{i}" for i in range(1, 21))
        (tmp_path / name).write_text(body + "\n", encoding="utf-8")

    r = await GrepTool().run(
        {"pattern": "needle", "path": ".", "offset": 500}, _ctx(tmp_path),
    )
    assert r.ok
    assert r.output.startswith("本页无匹配")
    assert "文件扫描上限" in r.output
    assert "可能仍有未覆盖的文件" in r.output


@pytest.mark.asyncio
async def test_no_scan_cap_note_when_scan_completed(tree, py_only):
    """没有撞上限（自然扫完）时不得出现扫描上限提示，否则等于换了个方向的误导。"""
    r = await GrepTool().run({"pattern": "needle", "path": ".", "offset": 500}, _ctx(tree))
    assert r.ok
    assert "文件扫描上限" not in r.output



@pytest.mark.asyncio
async def test_rg_multifile_paging_not_amplified(tmp_path, monkeypatch):
    """plan-549-2281 复盘修复：命中分散在多个文件时，--max-count 不随 offset 放大。

    此前固定传 offset+max_matches+1（offset=10000 → 每个文件 10051 条），
    而这些命中在 Python 侧解析到 stop_at 就被丢弃。现在先按「一页的量 + 1」取，
    只有真的出现单文件截断才按需要的上限重跑。
    """
    if locate_ripgrep() is None:
        pytest.skip("本机没有 ripgrep，跳过快路径用例")

    # 每个文件只有 2 条命中 → 永远不会顶满单文件上限，不该触发重跑
    for i in range(20):
        body = f"needle {i}-1\npadding\nneedle {i}-2\n"
        (tmp_path / f"part{i:02d}.txt").write_text(body, encoding="utf-8")

    seen_caps: list[int] = []
    real_exec = grep_mod.asyncio.create_subprocess_exec

    async def _spy(*cmd, **kwargs):
        for idx, part in enumerate(cmd):
            if part == "--max-count":
                seen_caps.append(int(cmd[idx + 1]))
        return await real_exec(*cmd, **kwargs)

    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", _spy)

    for offset in (0, 10, 30):
        r = await GrepTool().run(
            {"pattern": "needle", "path": ".", "offset": offset, "max_matches": 10},
            _ctx(tmp_path),
        )
        assert r.ok
        assert "(rg)" in r.output, "本机有 ripgrep 却走了纯 Python 回退"

    assert seen_caps, "未观察到 rg 调用"
    # offset=30 时旧写法会给 41，新写法固定 11
    assert max(seen_caps) == 11, f"单文件上限被 offset 放大: {seen_caps}"


@pytest.mark.asyncio
async def test_rg_single_file_hits_still_page_exactly(tmp_path, monkeypatch):
    """命中全集中在一个大文件时，翻页仍然不重不漏（截断必须触发重跑，不能漏页）。"""
    if locate_ripgrep() is None:
        pytest.skip("本机没有 ripgrep，跳过快路径用例")

    body = "\n".join(f"needle {i}" for i in range(1, 61))
    (tmp_path / "big.txt").write_text(body + "\n", encoding="utf-8")

    collected: list[int] = []
    for offset in (0, 20, 40):
        r = await GrepTool().run(
            {"pattern": "needle", "path": ".", "offset": offset, "max_matches": 20},
            _ctx(tmp_path),
        )
        assert r.ok
        assert "(rg)" in r.output
        rows = [
            int(ln.split(":")[1]) for ln in r.output.splitlines()
            if re.match(r"^big\.txt:\d+:", ln)
        ]
        # 每页都必须是这一页该有的连续区间：不重不漏
        assert rows == list(range(offset + 1, offset + 21)), f"offset={offset} 页内容错位: {rows}"
        collected.extend(rows)

    assert sorted(collected) == list(range(1, 61))


