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


def test_progress_fields_exposed_only_while_indexing(tmp_path):
    """进度字段（files_scanned/files_total）只在索引进行中暴露，结束后归零。

    前端据此渲染「已扫描 x / 共 y 个文件」；若 ready 后仍残留上轮计数，
    界面会显示与实际不符的陈旧数字。
    """
    ws = _mk_ws(tmp_path)
    sim._write_state(ws, enabled="1", status="parsing", files_scanned=42,
                     files_total=100, progress=35)
    st = sim.get_state(ws)
    assert st["files_scanned"] == 42
    assert st["files_total"] == 100

    for done in ("ready", "off", "cancelled", "error"):
        sim._write_state(ws, status=done, files_scanned=42, files_total=100)
        st = sim.get_state(ws)
        assert st["files_scanned"] == 0, done
        assert st["files_total"] == 0, done


def test_worker_reports_files_total_during_parsing(tmp_path):
    """worker 进入解析阶段时须写入 files_total（前端靠它显示总量）。"""
    ws = _mk_ws(tmp_path)  # a.py + b.ts
    state_db = sim._state_db_path(str(ws))  # .chatcoder/index_state.db
    sim._write_state(ws, enabled="1", status="indexing")  # 模拟已开启（enable 的前置）

    async def _run():
        await sim._run_index_worker(str(ws))
        return sim.get_state(ws)

    st = asyncio.get_event_loop().run_until_complete(_run())
    assert st["status"] == "ready"
    assert st["files"] == 2
    # 结束后计数归零（进行中才暴露）
    assert st["files_total"] == 0
    # 原始 state_db 里留有总数，证明解析阶段确实写过
    import sqlite3
    with sqlite3.connect(state_db) as conn:
        raw = dict(conn.execute("SELECT key, value FROM index_state"))
    assert int(raw.get("files_total") or 0) == 2


def test_stalled_worker_is_terminated_by_watchdog(tmp_path, monkeypatch):
    """停滞看门狗：worker 长时间无进度推进时必须被终止并报错。

    针对性防护：正则灾难性回溯会让 worker 空转 CPU（py-spy 抓栈确认）
    却永不更新状态，此前 UI 会永远停在某个百分比。
    """
    import sys

    ws = _mk_ws(tmp_path)
    sim._write_state(ws, enabled="1", status="indexing")  # 模拟已开启
    # 假 worker：只 sleep、从不写状态 → 模拟卡死
    monkeypatch.setattr(
        sim, "_worker_command",
        lambda *_a, **_k: [sys.executable, "-c", "import time; time.sleep(600)"],
    )
    monkeypatch.setattr(sim, "WORKER_STALL_TIMEOUT_S", 1.0)  # 缩短阈值

    async def _run():
        return await sim._run_index_worker(str(ws))

    result = asyncio.get_event_loop().run_until_complete(_run())
    assert result["status"] == "error"
    assert "无响应" in (result.get("error") or "")
    # 不得残留 worker 进程登记
    assert str(ws.resolve()) not in sim._workers


def test_disable_during_index_does_not_become_error(tmp_path):
    """扫描中点「关闭索引」：worker 退出后状态必须是 off，不能误报 error。

    旧逻辑：worker 卡在长遍历中未及时响应取消 → 被 terminate（Windows
    退出码 1）→ manager 收尾把 off 覆盖成 error "worker exited with code 1"。
    """
    ws = _mk_ws(tmp_path)

    async def _run():
        await sim.enable(str(ws))
        await asyncio.sleep(0.05)
        await sim.disable(str(ws))
        task = sim._index_tasks.get(str(ws.resolve()))
        if task is not None:
            await asyncio.wait_for(task, timeout=60)
        return sim.get_state(ws)

    st = asyncio.get_event_loop().run_until_complete(_run())
    assert st["enabled"] is False
    assert st["status"] in ("off", "cancelled", "ready")
    assert not st["error"]


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
