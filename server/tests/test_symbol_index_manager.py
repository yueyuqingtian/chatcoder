"""plan-248-1258 M3: 符号索引每工作区开关 / 自动增量 / AI 感知提示 单测。"""
import asyncio
from pathlib import Path

from app.services import symbol_index_manager as sim
from app.services import symbol_index_service as sis


def _mk_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "a.py").write_text(
        "def alpha():\n    return 1\n\n\nclass Beta:\n    def method(self):\n        return 2\n",
        encoding="utf-8",
    )
    (ws / "b.ts").write_text(
        "export function gamma(x) { return x }\nexport interface Delta { a: number }\n",
        encoding="utf-8",
    )
    return ws


def test_workspace_default_disabled(tmp_path):
    ws = _mk_ws(tmp_path)
    st = sim.get_state(ws)
    assert st["enabled"] is False
    assert st["status"] == "off"


def test_enable_indexes_and_reports_ready(tmp_path):
    ws = _mk_ws(tmp_path)

    async def _run():
        await sim.enable(str(ws))
        # enable 内部 create_task；等待完成
        await asyncio.sleep(0.4)
        return sim.get_state(ws)

    st = asyncio.get_event_loop().run_until_complete(_run())
    assert st["enabled"] is True
    assert st["status"] in ("ready", "indexing")
    # 索引数据已建
    stats = sis.index_stats(ws)
    assert stats["available"] is True
    assert stats["symbols"] >= 3  # alpha / Beta / method / gamma / Delta
    hits = sis.search_symbols(ws, "alpha")
    assert any(h["name"] == "alpha" for h in hits)
    outline = sis.outline_file(ws, "a.py")
    names = {s["name"] for s in outline}
    assert "alpha" in names and "Beta" in names


def test_disable_keeps_data_but_marks_off(tmp_path):
    ws = _mk_ws(tmp_path)

    async def _run():
        await sim.enable(str(ws))
        await asyncio.sleep(0.4)
        await sim.disable(str(ws))
        return sim.get_state(ws)

    st = asyncio.get_event_loop().run_until_complete(_run())
    assert st["enabled"] is False
    assert st["status"] == "off"


def test_notify_file_changed_marks_dirty_only_when_enabled(tmp_path):
    ws = _mk_ws(tmp_path)
    # 未开启：不应产生脏标记
    sim.notify_file_changed(ws, "a.py")
    assert not sim._dirty

    async def _run():
        await sim.enable(str(ws))
        await asyncio.sleep(0.4)

    asyncio.get_event_loop().run_until_complete(_run())
    sim.notify_file_changed(ws, "a.py")
    assert any(k.endswith("proj") for k in sim._dirty)


def test_incremental_scan_updates_new_symbol(tmp_path):
    """写盘钩子 + 增量扫描后，新函数可被检索到（需求：改动文件自动更新索引）。"""
    ws = _mk_ws(tmp_path)

    async def _run():
        await sim.enable(str(ws))
        await asyncio.sleep(0.4)
        # 追加新函数
        (ws / "a.py").write_text(
            (ws / "a.py").read_text(encoding="utf-8") + "\n\ndef epsilon():\n    return 5\n",
            encoding="utf-8",
        )
        sim.notify_file_changed(ws, "a.py")
        await asyncio.sleep(0.1)
        # plan-248-1273: 绕过启动首轮闸门（测试显式调用 _scan_once，非启动场景）
        sim._first_scan_done = True
        await sim._scan_once()
        # plan-248-1273: _scan_once 提交 worker 后 fire-and-forget，
        # 需等待 worker 任务完成才能检索到新符号
        task = sim._index_tasks.get(str(ws.resolve()))
        if task is not None:
            await asyncio.wait_for(task, timeout=30)
        return sis.search_symbols(ws, "epsilon")

    hits = asyncio.get_event_loop().run_until_complete(_run())
    assert any(h["name"] == "epsilon" for h in hits)


def test_symbol_index_hint_not_enabled():
    from app.orchestration.context_manager import _symbol_index_hint

    got = asyncio.get_event_loop().run_until_complete(_symbol_index_hint("D:/nonexistent/none"))
    assert "NOT ENABLED" in got


def test_symbol_index_hint_ready(tmp_path):
    ws = _mk_ws(tmp_path)
    from app.orchestration.context_manager import _symbol_index_hint

    async def _run():
        await sim.enable(str(ws))
        await asyncio.sleep(0.4)
        return await _symbol_index_hint(str(ws))

    got = asyncio.get_event_loop().run_until_complete(_run())
    assert "READY" in got
    assert "symbol_search" in got
