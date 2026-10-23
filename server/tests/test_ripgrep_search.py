"""增强搜索（ripgrep）快路径：参数合法、上下文行不被误计为匹配、找不到 rg 时回退纯 Python。"""
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.ripgrep_env import locate_ripgrep
from app.orchestration.tools import grep as grep_mod
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.grep import GrepTool

needs_rg = pytest.mark.skipif(locate_ripgrep() is None, reason="本机没有 ripgrep，跳过快路径测试")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("alpha\nfoo here\nbeta\n", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("x\ny:3:z\nfoo:9:bar\n", encoding="utf-8")
    return tmp_path


def _ctx(root: Path) -> ToolContext:
    return ToolContext(workspace_root=str(root), session_id=1, task_id=1, agent_id=1, agent_name="tester")


@needs_rg
@pytest.mark.asyncio
async def test_rg_fast_path_really_runs(tree):
    """回归：--max-filesize 带 B 后缀会让 rg 报错 rc=2，快路径被静默回退，等于从未生效。"""
    rg = locate_ripgrep()
    r = await GrepTool()._rg_search(
        root=tree, ws_root=tree, pattern="foo", include_glob="", case_sensitive=True,
        context_lines=0, rg_path=rg.path,
    )
    assert r is not None, "rg 快路径返回 None 说明 rg 调用失败，已回退纯 Python"
    assert "(rg)" in r.output
    assert "a.txt:2:foo here" in r.output
    assert r.data["matches"] == 2


@needs_rg
@pytest.mark.asyncio
async def test_rg_context_lines_not_counted_as_matches(tree):
    """上下文行即使内容里含 ':数字:' 也不能计入匹配数，路径也不带 ./ 前缀。"""
    rg = locate_ripgrep()
    r = await GrepTool()._rg_search(
        root=tree, ws_root=tree, pattern="foo", include_glob="", case_sensitive=True,
        context_lines=1, rg_path=rg.path,
    )
    assert r is not None
    assert r.data["matches"] == 2  # foo 只在两处出现，y:3:z 只是上下文
    assert "y:3:z" in r.output
    assert "b.txt:3:foo:9:bar" in r.output
    assert ".\\" not in r.output and "./" not in r.output


@pytest.mark.asyncio
async def test_grep_falls_back_to_python_when_rg_missing(tree, monkeypatch):
    monkeypatch.setattr(grep_mod, "locate_ripgrep", lambda: None)
    monkeypatch.setattr(settings, "enhanced_search", True)
    r = await GrepTool().run({"pattern": "foo", "path": "."}, _ctx(tree))
    assert r.ok
    assert "(rg)" not in r.output
    assert "a.txt:2: foo here" in r.output  # 纯 Python 分支的输出格式（行号后带空格）
