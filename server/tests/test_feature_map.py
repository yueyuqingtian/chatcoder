"""feature_map（plan-548-2276 阶段 4.4）：功能 → 模块/文件/符号/坑点/计划文档。

验收口径（方案阶段 4.5「能从计划文档标题反查到模块」）：
- 计划文档标题 + 文中路径 → 功能条目，中文描述能反查到模块；
- 项目记忆只读、只取当前项目（project 作用域）且作为坑点进入地图；
- 符号索引目录聚类作为第三源；
- feature_lookup 工具端到端可用，并给出可直接执行的 fs_read 调用。
"""
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.feature_lookup import FeatureLookupTool
from app.persistence.database import Base
from app.persistence.models import MemoryEntry  # noqa: F401 注册全部模型
from app.services import feature_map_service as fms
from app.services import symbol_index_service as sis


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件只用独立临时库（见下方 db fixture）。

    conftest 的同名 fixture 为每个用例 drop/create 全量表；与纯文件类用例集在一起跑时
    偶发 `no such table` 的 teardown 报错（表名随机），与用例断言无关。
    本文件需要库的用例自带 tmp 库；feature_lookup 反查项目失败时内部已降级（返回 None）。
    """
    yield


@pytest.fixture
async def db(tmp_path):
    """独立临时库：不污染全局测试库，也不需要 write_engine（本模块只读记忆）。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/feature_map.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _ctx(root: Path, db=None) -> ToolContext:
    return ToolContext(
        workspace_root=str(root), session_id=1, task_id=1, agent_id=1, agent_name="tester", db=db,
    )


def _write_plan(ws: Path, name: str, title: str, body: str) -> None:
    plans = ws / "ai"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / name).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")


async def test_plan_docs_map_feature_to_modules(workspace):
    _write_plan(
        workspace, "chatcoder-plan-1-1.md", "上下文压缩与 checkpoint",
        "改动 server/app/orchestration/context_compressor.py，"
        "前端展示在 client/src/components/chat/CompactCard.tsx。",
    )
    entries = fms.build_map(workspace)
    assert entries

    hits = fms.query(entries, "上下文压缩", limit=3)
    assert hits, "无法从计划文档标题反查到功能"
    top = hits[0]
    assert top["plans"] == ["chatcoder-plan-1-1.md"]
    assert "server/app/orchestration" in top["modules"]
    assert "client/src/components" in top["modules"]
    assert "context_compressor.py" in top["files"]


async def test_project_memory_becomes_pitfall(db, workspace):
    db.add(MemoryEntry(
        text=(
            "上下文压缩：摘要不短于原文时会放弃压缩"
            "（见 server/app/orchestration/context_compressor.py）"
        ),
        kind="pitfall", scope="project", project_id=7, session_id=None,
    ))
    await db.commit()

    result = await fms.rebuild(workspace, db=db, project_id=7)
    assert result["memory_count"] == 1
    assert result["entry_count"] >= 1

    entries = fms.load_map(workspace)
    hits = fms.query(entries, "压缩", limit=5)
    memory_hits = [e for e in hits if e["source"] == "memory"]
    assert memory_hits, "记忆条目未进入功能地图"
    assert memory_hits[0]["pitfalls"], "坑点类记忆必须作为 pitfalls 保留"
    assert "context_compressor.py" in memory_hits[0]["files"]


async def test_other_project_memories_not_used(db, workspace):
    db.add(MemoryEntry(
        text="别的项目：发布流程改动 server/deploy/other.py",
        kind="architecture", scope="project", project_id=99, session_id=None,
    ))
    await db.commit()

    assert await fms.load_project_memories(db, 7) == []
    result = await fms.rebuild(workspace, db=db, project_id=7)
    assert result["memory_count"] == 0, "跨项目记忆被串进来"


async def test_rebuild_with_unresolved_project_keeps_previous_memory_entries(db, workspace):
    """plan-549-2281 复盘修复：只给 session_id 且反查项目失败时，不得清空已有记忆条目。

    旧实现把 project_id=None 当成「项目无记忆」→ 新地图不含 memory 条目 → save_map
    覆盖后，上一次地图里的记忆条目被静默丢掉（用户看不到任何报错）。
    """
    db.add(MemoryEntry(
        text="上下文压缩：摘要不短于原文时会放弃压缩（server/app/services/memory_service.py）",
        kind="pitfall", scope="project", project_id=7, session_id=None,
    ))
    await db.commit()

    first = await fms.rebuild(workspace, db=db, project_id=7)
    assert first["memory_count"] == 1
    before = [e for e in fms.load_map(workspace) if e["source"] == "memory"]
    assert before

    # 只给一个反查不到项目的会话（会话不存在 → resolve_project_id 返回 None）
    second = await fms.rebuild(workspace, db=db, session_id=999_999)
    assert second["project_id"] is None
    after = [e for e in fms.load_map(workspace) if e["source"] == "memory"]
    assert after == before, "项目解析失败时旧记忆条目被清空"
    assert second["memory_count"] == len(after), "返回值应如实反映地图里的记忆条数"


async def test_rebuild_with_resolved_project_uses_fresh_memories(db, workspace):
    """反查项目成功时以新读取结果为准，不混入上一次地图的旧记忆条目。"""
    from app.persistence.models import Session

    db.add(MemoryEntry(
        text="旧记忆：已下线的老实现 server/app/services/legacy.py",
        kind="pitfall", scope="project", project_id=7, session_id=None,
    ))
    row = Session(project_id=7, title="s")
    db.add(row)
    await db.commit()
    await fms.rebuild(workspace, db=db, project_id=7)

    # 项目记忆被清空后，按 session_id 反查成功 → 记忆源确实为空，旧条目不应被沿用
    from sqlalchemy import delete as sa_delete

    await db.execute(sa_delete(MemoryEntry))
    await db.commit()

    result = await fms.rebuild(workspace, db=db, session_id=int(row.id))
    assert result["project_id"] == 7
    assert result["memory_count"] == 0
    assert [e for e in fms.load_map(workspace) if e["source"] == "memory"] == []


async def test_symbol_clusters_as_third_source(workspace):
    pkg = workspace / "server" / "app" / "orchestration"
    pkg.mkdir(parents=True)
    (pkg / "alpha.py").write_text(
        "def alpha_one():\n    return 1\n\n\ndef alpha_two():\n    return 2\n", encoding="utf-8",
    )
    (pkg / "beta.py").write_text(
        "class Beta:\n    def method(self):\n        return 3\n", encoding="utf-8",
    )
    sis.index_workspace(str(workspace))

    entries = fms.build_map(workspace)
    clusters = [e for e in entries if e["source"] == "symbols"]
    assert clusters, "符号索引目录聚类未进入功能地图"
    top = clusters[0]
    assert top["modules"] == ["server/app/orchestration"]
    assert "alpha_one" in top["symbols"]


async def test_rebuild_writes_map_file(workspace):
    _write_plan(
        workspace, "chatcoder-plan-2-3.md", "记忆检索升级",
        "涉及 server/app/services/memory_service.py。",
    )
    path = fms.map_path(workspace)
    assert not path.exists()

    await fms.rebuild(workspace)
    assert path.exists()
    assert fms.load_map(workspace)
    assert fms.map_stale(workspace) is False


async def test_feature_lookup_tool_end_to_end(workspace):
    _write_plan(
        workspace, "chatcoder-plan-3-4.md", "功能地图与检索工具改造",
        "改动 server/app/services/feature_map_service.py 与 "
        "server/app/orchestration/tools/feature_lookup.py。",
    )
    r = await FeatureLookupTool().run({"query": "功能地图"}, _ctx(workspace))
    assert r.ok
    assert "server/app/services" in r.output
    assert "fs_read(path=" in r.output
    assert r.data["count"] >= 1


async def test_feature_lookup_reports_empty_map(workspace):
    """没有任何数据源时给替代方案，而不是报错。"""
    r = await FeatureLookupTool().run({"query": "随便什么功能"}, _ctx(workspace))
    assert r.ok
    assert "功能地图为空" in r.output
    assert "fs_grep" in r.output


async def test_new_tools_registered():
    from app.orchestration.tools.registry import get_tool_registry

    reg = get_tool_registry()
    assert reg.get("feature_lookup") is not None
    enable = reg.get("symbol_index_enable")
    assert enable is not None
    assert enable.risk_level == "low"
    assert enable.concurrency_safe is False
    assert enable.function_schema()["function"]["parameters"]["required"] == []
