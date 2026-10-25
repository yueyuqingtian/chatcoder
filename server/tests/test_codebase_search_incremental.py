"""codebase_search（plan-548-2276 阶段 4.2）：按文件指纹增量、中文二元切、offset 分页。

验收口径：
- 只改一个文件时，其它文件的块原样复用（内容相等）；
- 增量结果与「全量重建」一致（不因复用而丢块或重复）；
- 旧格式索引（只有 meta.sha1）读取时视为全部失效并写回新格式；
- 中文查询能命中中文注释（旧实现按空格切词，中文恒为 0 命中）；
- 分页不重不漏。
"""
import json
from pathlib import Path

import pytest

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.codebase_search import CodebaseSearchTool, _tokenize


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件不碰数据库。

    这些用例只操作 tmp_path 下的文件与 .chatcoder 索引（无 SQLAlchemy）。conftest 的同名
    fixture 每个用例都 drop/create 全量表，与文件类测试集在一起跑时偶发
    `no such table`（表名随机）的 teardown 报错，与用例内容无关。同名覆盖即为本文件免除该噪音。
    """
    yield


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        workspace_root=str(root), session_id=1, task_id=1, agent_id=1, agent_name="tester",
    )


def _write(ws: Path, name: str, text: str) -> None:
    (ws / name).write_text(text, encoding="utf-8")


def _seed(ws: Path) -> None:
    """三个互不相关但都含关键词的文件；每个文件内容都 > 20 字符，能成为独立块。"""
    _write(ws, "alpha.py", "def alpha_handler():\n    return 'alpha payload context manager'\n")
    _write(ws, "beta.py", "def beta_handler():\n    return 'beta payload'\n")
    _write(ws, "gamma.py", "GAMMA_PAYLOAD = 42\nVALUE = GAMMA_PAYLOAD + 1\n")


def _by_file(chunks: list[dict], name: str) -> list[dict]:
    return [c for c in chunks if c["file"] == name]


async def test_incremental_rebuild_only_changed_files(tmp_path):
    """只改一个文件：其余文件的块必须原样复用（内容相等），不是重新解析出来的同值。"""
    tool = CodebaseSearchTool()
    _seed(tmp_path)

    first = tool._load_or_build_index(tmp_path)
    assert _by_file(first, "beta.py")

    # 改 beta（size 也变了，指纹必然不同）：只有它应被重建
    _write(tmp_path, "beta.py", "def beta_handler():\n    return 'beta payload v2'\n")
    second = tool._load_or_build_index(tmp_path)

    beta_after = _by_file(second, "beta.py")
    assert beta_after != _by_file(first, "beta.py")
    assert any("v2" in c["text"] for c in beta_after)
    assert _by_file(second, "alpha.py") == _by_file(first, "alpha.py")
    assert _by_file(second, "gamma.py") == _by_file(first, "gamma.py")


async def test_incremental_matches_full_rebuild(tmp_path):
    tool = CodebaseSearchTool()
    _seed(tmp_path)
    tool._load_or_build_index(tmp_path)

    _write(tmp_path, "beta.py", "def beta_handler():\n    return 'beta payload v2'\n")
    _write(tmp_path, "delta.py", "def delta_handler():\n    return 'delta payload'\n")
    incremental = tool._load_or_build_index(tmp_path)

    full = tool._build_index(tmp_path, tool._collect_source_files(tmp_path))

    def key(c: dict) -> tuple:
        return (c["file"], c["start_line"], c["end_line"], c["text"])

    assert sorted(map(key, incremental)) == sorted(map(key, full))


async def test_removed_file_chunks_dropped(tmp_path):
    tool = CodebaseSearchTool()
    _seed(tmp_path)
    assert _by_file(tool._load_or_build_index(tmp_path), "beta.py")

    (tmp_path / "beta.py").unlink()
    chunks = tool._load_or_build_index(tmp_path)
    assert _by_file(chunks, "beta.py") == []
    assert _by_file(chunks, "alpha.py")


async def test_legacy_index_format_rebuilt(tmp_path):
    """旧格式（只有 meta.sha1）不可信：全部当失效，重建后写 meta.files。"""
    tool = CodebaseSearchTool()
    _write(tmp_path, "legacy.py", "def legacy_handler():\n    return 'legacy payload'\n")

    index_path = tmp_path / ".chatcoder" / "codebase_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({
            "meta": {"sha1": "deadbeef"},
            "chunks": [
                {"file": "ghost.py", "start_line": 1, "end_line": 2, "text": "ghost payload"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    chunks = tool._load_or_build_index(tmp_path)
    assert _by_file(chunks, "ghost.py") == []
    assert _by_file(chunks, "legacy.py")
    data = json.loads(index_path.read_text(encoding="utf-8"))
    assert data["meta"]["files"], "新格式必须存每文件指纹，否则下一轮又会整体重建"


async def test_tokenize_chinese_bigrams():
    terms = _tokenize("上下文压缩")
    assert {"上下文", "下文压", "文压缩"} <= terms
    mixed = _tokenize("Compaction 压缩 the")
    assert "compaction" in mixed
    assert "压缩" in mixed
    assert "the" not in mixed  # 英文停用词不参与打分


@pytest.mark.asyncio
async def test_chinese_query_hits_chinese_comment(tmp_path):
    _write(
        tmp_path, "compress.py",
        'def compact_session(session):\n'
        '    """会话上下文压缩：超限时生成 checkpoint 摘要。"""\n'
        '    return session\n',
    )
    _write(tmp_path, "unrelated.py", "UNRELATED = 'nothing here at all'\n")

    r = await CodebaseSearchTool().run({"query": "上下文压缩", "top_k": 5}, _ctx(tmp_path))
    assert r.ok
    assert "compress.py" in r.output
    assert "unrelated.py" not in r.output


@pytest.mark.asyncio
async def test_pagination_no_overlap(tmp_path):
    for i in range(6):
        _write(tmp_path, f"mod{i}.py", f"def handler_{i}():\n    return 'shared payload token'\n")

    seen: list[str] = []
    for offset in (0, 3):
        r = await CodebaseSearchTool().run(
            {"query": "payload", "top_k": 3, "offset": offset}, _ctx(tmp_path),
        )
        assert r.ok
        assert r.data["offset"] == offset
        assert len(r.data["results"]) == 3
        seen.extend(x["file"] for x in r.data["results"])

    assert len(seen) == len(set(seen)) == 6, "两页应覆盖 6 条且互不重复"

    # 越过分页末页：如实说「已到底」，不给假的下一批
    r = await CodebaseSearchTool().run(
        {"query": "payload", "top_k": 3, "offset": 6}, _ctx(tmp_path),
    )
    assert r.ok
    assert r.data["results"] == []
    assert "已到底" in r.output


@pytest.mark.asyncio
async def test_next_offset_and_top_k_clamp(tmp_path):
    for i in range(4):
        _write(tmp_path, f"n{i}.py", f"def n_{i}():\n    return 'clamp payload'\n")

    r = await CodebaseSearchTool().run({"query": "payload", "top_k": 2}, _ctx(tmp_path))
    assert r.ok
    assert r.data["top_k"] == 2
    assert r.data["has_more"] is True
    assert r.data["next_offset"] == 2
    assert "继续请传 offset=2" in r.output

    clamped = await CodebaseSearchTool().run({"query": "payload", "top_k": 99}, _ctx(tmp_path))
    assert clamped.data["top_k"] == 20


# ── 先排序再截断（plan-549-2281）──────────────────────────────────────────────
def test_collect_source_files_sorted_and_stable(tmp_path, monkeypatch):
    """收录哪些文件必须确定性：按路径排序后再截断，不依赖 rglob 的返回顺序。

    此前 `files[:3000]` 直接用 rglob 顺序（文件系统 scandir），而增量索引以
    「在 fingerprints 里 = 仍存在」推断 removed：超上限时被截掉的既有索引文件会被
    当成已删除丢块，rglob 顺序一变就在「收录 / 删除」间来回抖动。
    """
    from app.orchestration.tools import codebase_search as cs_mod

    for name in ("zeta.py", "alpha.py", "mike.py", "bravo.py"):
        _write(tmp_path, name, f"def {name[:-3]}_fn():\n    return '{name}'\n")

    monkeypatch.setattr(cs_mod, "_MAX_SOURCE_FILES", 2)
    tool = CodebaseSearchTool()
    kept = [p.name for p in tool._collect_source_files(tmp_path)]
    assert kept == ["alpha.py", "bravo.py"], "截断结果必须按路径排序，可复现"

    # 连调两次结果一致（不随目录遍历顺序浮动）
    assert [p.name for p in tool._collect_source_files(tmp_path)] == kept


def test_collect_source_files_order_independent_of_rglob(tmp_path, monkeypatch):
    """即使 rglob 给出反序，收集结果仍是同一批文件且已排序。"""
    from app.orchestration.tools import codebase_search as cs_mod

    for name in ("c.py", "a.py", "b.py"):
        _write(tmp_path, name, f"VALUE_{name[0].upper()} = 1\n")

    tool = CodebaseSearchTool()
    normal = [p.name for p in tool._collect_source_files(tmp_path)]
    assert normal == ["a.py", "b.py", "c.py"]

    real_rglob = Path.rglob

    def _reverse_rglob(self, pattern, **kwargs):
        return iter(sorted(real_rglob(self, pattern, **kwargs), reverse=True))

    monkeypatch.setattr(Path, "rglob", _reverse_rglob)
    again = [p.name for p in tool._collect_source_files(tmp_path)]
    assert again == normal

