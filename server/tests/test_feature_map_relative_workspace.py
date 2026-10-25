"""相对工作区下功能地图的回归用例。

缺陷原貌：`feature_map_service._load_symbol_clusters` 用 `db.as_uri()` 拼 sqlite 只读 URI。
`Path.as_uri()` 对**相对路径**抛 `ValueError("relative path can't be expressed as a file URI")`，
而 `app/core/config.py` 的 `workspace_root` 默认值正是相对路径 `./workspace`。紧邻的
`except sqlite3.Error` 接不住 `ValueError`，于是未绑定项目的会话里功能地图的**符号源**
永远为空——`feature_lookup` 一直回「地图为空」，而符号库其实就在磁盘上。

本文件钉住三件事：
1. `build_map` 传**相对路径**不抛异常，且确实读到了 symbols.db 里的符号聚类；
2. `map_path` / `save_map` / `load_map` / `map_stale` 在相对路径下读写同一份缓存；
3. `rebuild` 传相对路径时能把地图落到该工作区的 `.chatcoder/` 下。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.services import feature_map_service as fms


@pytest.fixture(autouse=True)
def _reset_shared_test_db():
    """覆盖 conftest 的全局库隔离 fixture：本文件是纯文件类用例，不碰共享库。

    同 `test_feature_map.py` 的做法：conftest 的 autouse fixture 会在每个用例前后对共享库
    执行 DDL/DELETE，与纯文件用例混跑时偶发与断言无关的 `no such table` teardown 报错。
    """
    yield


# 目录名刻意带**中文与空格**：Windows 下 URI 编码最常出问题的就是这两类字符
_WS_NAME = "相对 工作区"


def _make_symbols_db(workspace, rows: list[tuple[str, str]]) -> None:
    """造最小符号库：表结构对齐 `symbol_index_service._SCHEMA`。

    `_load_symbol_clusters` 只读 `file_path / name / parent_id / kind` 四列，这里按真实
    索引库建全表，避免「测试库结构比生产窄」掩盖真实的兼容问题。
    """
    db_dir = workspace / ".chatcoder"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "symbols.db")
    try:
        conn.executescript(
            """
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT,
                line_start INTEGER,
                line_end INTEGER,
                signature TEXT,
                doc TEXT,
                parent_id INTEGER,
                hash TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO symbols(file_path, name, parent_id, kind) VALUES(?, ?, NULL, ?)",
            [(path, name, kind) for path, name, kind in rows],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def relative_ws(tmp_path, monkeypatch):
    """在 tmp_path 下造工作区并 chdir 到其父目录，返回 (相对路径, 绝对路径)。

    返回的相对路径正是 `settings.workspace_root` 的形态（`./workspace` 这类）。
    """
    parent = tmp_path / "parent"
    ws = parent / _WS_NAME
    ws.mkdir(parents=True)
    monkeypatch.chdir(parent)
    return _WS_NAME, ws


def test_build_map_reads_symbols_from_relative_workspace(relative_ws):
    """相对路径工作区下必须读到符号库（此前 as_uri() 抛 ValueError，符号源整段消失）。"""
    rel, ws = relative_ws
    _make_symbols_db(ws, [
        ("server/app/orchestration/agent_loop.py", "run_loop", "function"),
        ("server/app/orchestration/agent_loop.py", "decide", "function"),
        ("server/app/orchestration/engine.py", "Engine", "class"),
        ("server/app/services/memory_service.py", "save_memories", "function"),
        ("server/app/services/memory_service.py", "load_memories", "function"),
        ("server/app/services/memory_service.py", "entry_keywords", "function"),
    ])

    entries = fms.build_map(rel)

    assert entries, "相对工作区下功能地图为空——符号库没有被读到"
    clusters = [e for e in entries if e.get("source") == "symbols"]
    assert clusters, "符号索引目录聚类未进入功能地图（as_uri 对相对路径抛 ValueError 的旧缺陷）"
    modules = {m for e in clusters for m in (e.get("modules") or [])}
    assert "server/app/orchestration" in modules
    assert "server/app/services" in modules
    symbols = {s for e in clusters for s in (e.get("symbols") or [])}
    assert "run_loop" in symbols


def test_symbol_cluster_skipped_when_db_file_missing(relative_ws):
    """对照组：没有 symbols.db 时正常返回（不因路径转换而抛错）。"""
    rel, _ws = relative_ws
    assert fms.build_map(rel) == []


def test_relative_workspace_map_cache_roundtrip(relative_ws):
    """`map_path` / `save_map` / `load_map` / `map_stale` 在相对路径下指向同一份缓存。"""
    rel, ws = relative_ws

    path = fms.map_path(rel)
    assert path.is_absolute(), "相对工作区的缓存路径必须解析成绝对路径（否则 cwd 一变就换文件）"
    assert path.name == "feature_map.json"
    assert path.parent == ws / ".chatcoder"

    entries = [{"feature": "记忆检索", "source": "plan", "paths": [], "modules": []}]
    fms.save_map(rel, entries)

    assert path.exists()
    assert fms.load_map(rel) == entries
    assert fms.map_stale(rel) is False
    assert not list(path.parent.glob("*.tmp")), "原子落盘不得残留临时文件"


async def test_rebuild_with_relative_workspace_writes_map(relative_ws):
    """`rebuild` 传相对路径时把地图落到该工作区的 `.chatcoder/` 下。"""
    rel, ws = relative_ws
    plans = ws / "ai"
    plans.mkdir(parents=True)
    (plans / "chatcoder-plan-900-9001.md").write_text(
        "# 相对工作区功能地图\n\n改动 server/app/services/feature_map_service.py。\n",
        encoding="utf-8",
    )

    result = await fms.rebuild(rel)

    assert result["entry_count"] >= 1
    saved = json.loads((ws / ".chatcoder" / "feature_map.json").read_text(encoding="utf-8"))
    assert saved["entries"], "重建后地图为空"
    assert fms.load_map(rel), "相对路径读取地图失败"
