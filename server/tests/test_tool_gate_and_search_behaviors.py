"""工具层门禁、宽松匹配与清单折叠的回归用例（plan-560-2311 D1/D2/D8）。

D1（读缓存语义拆分）：`mark_known` 原被 `editor_apply_diff` / `multi_file_edit` 调用，等于
「整文件内容已知」，于是 `covers` / `has_read` 对任意行都返回 True——两个门禁（编辑前的行
区间校验、fs_write 的「先读后写」）一起失守。修法是把「整文件已知」与「行区间已知」分开
记账：`mark_known` 只给 fs_write 整文件重写用，局部编辑改登记写后真实区间（`record_edit`）。

D2（宽松匹配的区间回填）：`text_edit` 第 4/5 级宽松匹配（`trailing_ws` / `indent`）原未传
`spans`、`end_line` 写成 `i + 1`。`replace_all=True` 时闸门回落到单区间、只校验第一处，
其余替换位置未读过也静默放行；`end_line` 漏掉匹配窗口行数 `n`，窗口为多行时区间整体偏移。

D8（清单折叠与 owner 解析）：`todo_write` 原「先按上限折叠、再按 content 去重」，重复项
先占名额把别的项挤进折叠名单；被折叠的已完成项随后在落库时被改成 `cancelled` + 隐藏，
是历史失真（它们真的完成过）。`owner` 解析原未排除 `bool`（`True` → agent_id=1）与负数。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration.tools.base import ToolContext  # noqa: E402
from app.orchestration.tools.read_cache import ReadWindowCache  # noqa: E402
from app.orchestration.tools.text_edit import apply_replacement  # noqa: E402
from app.orchestration.tools.todo import _MAX_TODOS, TodoWriteTool  # noqa: E402


def _ctx(tmp_path: Path, cache: ReadWindowCache | None = None) -> ToolContext:
    return ToolContext(
        workspace_root=str(tmp_path), session_id=1, task_id=1, agent_id=1, agent_name="t",
        read_cache=cache if cache is not None else ReadWindowCache(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# D1：mark_known 只表达整文件已知；局部编辑后未读区间仍被拒
# ══════════════════════════════════════════════════════════════════════════════

def test_mark_known_still_means_whole_file():
    """fs_write 整文件重写的语义不变：登记后任意行区间都算已知。"""
    cache = ReadWindowCache()
    p = Path("/tmp/d1_whole.py")

    assert cache.covers(p, 5, 9) is False
    cache.mark_known(p)
    assert cache.covers(p, 5, 9) is True
    assert cache.covers(p, 1, 10_000) is True


def test_local_edit_does_not_grant_whole_file():
    """plan-560-2311 D1：`record_edit` 后只有写过的那些行算已知，其余行仍被拒。"""
    cache = ReadWindowCache()
    p = Path("/tmp/d1_local.py")

    cache.record_edit(p, [(5, 5)], delta=0)

    assert cache.covers(p, 5, 5) is True, "刚写过的行必须算已知，否则接着改同一行会被误拒"
    assert cache.covers(p, 100, 200) is False, "局部编辑不得让整文件免检（D1 的根因）"
    assert cache.has_ranges(p) is True
    assert cache.has_read(p) is True  # 本次写过 = 内容掌握过（fs_write 门禁用这个口径）


def test_record_edit_shifts_later_ranges_by_line_delta():
    """一次局部编辑增删行数后，其后的已读区间要按增量平移，否则门禁会拿旧行号比对。"""
    cache = ReadWindowCache()
    p = Path("/tmp/d1_shift.py")
    cache.record_range(p, 1, 2)
    cache.record_range(p, 10, 12)

    # 在第 5 行处插入 2 行：写入区间变成 5-7，其后所有行号 +2
    cache.record_edit(p, [(5, 5)], delta=2)

    assert cache.covers(p, 5, 7) is True, "写过的三行应登记为已知"
    assert cache.covers(p, 1, 2) is True, "编辑点之前的区间不受影响"
    assert cache.covers(p, 12, 14) is True, "编辑点之后的区间应整体平移 +2"
    assert cache.covers(p, 10, 12) is False, "旧行号不得再被判为已读"


def test_recording_invalid_edit_spans_is_noop():
    """非法区间静默跳过，不得凭空产生「已知」记录。"""
    cache = ReadWindowCache()
    p = Path("/tmp/d1_invalid.py")

    cache.record_edit(p, [(0, 5), (10, 5)])

    assert cache.has_ranges(p) is False


@pytest.mark.asyncio
async def test_editor_local_edit_keeps_unread_region_blocked(tmp_path, monkeypatch):
    """端到端：`editor_apply_diff` 改过一处后，同文件未读区域仍被 strict 闸门拒绝。

    plan-560-2311 D1 的回归点：旧实现在写盘成功后调 `mark_known`，此后对同文件任意行的
    编辑都被无条件放行，strict 模式形同虚设。
    """
    from app.core.config import settings
    from app.orchestration.tools.editor import EditorApplyDiffTool

    monkeypatch.setattr(settings, "edit_gate_mode", "strict", raising=False)
    p = tmp_path / "a.py"
    p.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

    cache = ReadWindowCache()
    cache.record_range(p, 1, 2)  # 只读过前两行
    ctx = _ctx(tmp_path, cache)

    first = await EditorApplyDiffTool().run(
        {"path": "a.py", "old_text": "line1", "new_text": "line1 = 1"}, ctx,
    )
    assert first.ok, first.error
    assert first.data["read_verified"] is True

    # 改第 1 行后，第 4 行仍是未读区域 —— 必须继续被拒
    second = await EditorApplyDiffTool().run(
        {"path": "a.py", "old_text": "line4", "new_text": "line4 = 4"}, ctx,
    )
    assert not second.ok
    assert second.error_code == "STALE_READ"
    assert "line4\n" in p.read_text(encoding="utf-8"), "被拒时一个字都不能写"


# ══════════════════════════════════════════════════════════════════════════════
# D2：宽松匹配在 replace_all 时的 spans 与 end_line
# ══════════════════════════════════════════════════════════════════════════════

def test_trailing_ws_replace_all_reports_every_span():
    """plan-560-2311 D2：`trailing_ws` 分支 `replace_all=True` 必须带回全部命中区间。

    old_text 带行尾空白（文件里没有），精确匹配必然失败，因此一定走第 4 级宽松匹配；
    两处命中、窗口 2 行，`end_line` 必须是窗口末行（3 / 6），而不是 `i + 1`（2 / 5）。
    """
    content = "head\nalpha = 1\nbeta = 2\nmid\nalpha = 1\nbeta = 2\ntail\n"

    res = apply_replacement(content, "alpha = 1   \nbeta = 2   ", "gamma = 3", replace_all=True)

    assert res.ok, res.error
    assert res.strategy == "trailing_ws"
    assert res.count == 2
    assert [tuple(s) for s in res.spans] == [(2, 3), (5, 6)], "每处真正替换的位置都要在 spans 里"
    assert (res.start_line, res.end_line) == (2, 3), "end_line 是窗口末行，不是首行+1"
    assert res.text.count("gamma = 3") == 2


def test_indent_replace_all_reports_every_span():
    """plan-560-2311 D2：`indent` 分支同样要回填 spans 与正确的 `end_line`。

    文件内容是双空格，old_text 是单空格——精确匹配与 trailing_ws 都不会命中，只能走
    第 5 级（缩进/行内空白归一）。
    """
    content = "def a():\n    x  =  1\n    y  =  2\ndef b():\n    x  =  1\n    y  =  2\n"

    res = apply_replacement(content, "x = 1\ny = 2", "z = 9", replace_all=True)

    assert res.ok, res.error
    assert res.strategy == "indent"
    assert res.count == 2
    assert [tuple(s) for s in res.spans] == [(2, 3), (5, 6)]
    assert (res.start_line, res.end_line) == (2, 3)


def test_loose_match_multiple_hits_without_replace_all_is_rejected():
    """未开 replace_all 时多处命中必须报错并给出各行号（不能静默只改一处）。"""
    res = apply_replacement("a  =  1\nb\nc\na  =  1\n", "a = 1", "a = 2")

    assert not res.ok
    assert "匹配 2 处" in res.error
    assert "replace_all" in res.error


def test_loose_match_single_hit_reports_window_end():
    """单处命中也用同一套区间口径：窗口 2 行时 end_line 必须是 2。"""
    res = apply_replacement("head\nalpha = 1  \nbeta = 2  \ntail\n",
                            "alpha = 1 \nbeta = 2 ", "x = 9")

    assert res.ok, res.error
    assert res.strategy == "trailing_ws"
    assert (res.start_line, res.end_line) == (2, 3)
    assert [tuple(s) for s in res.spans] == [(2, 3)]


# ══════════════════════════════════════════════════════════════════════════════
# D8：owner 解析与折叠顺序
# ══════════════════════════════════════════════════════════════════════════════

def _owner_of(res, index: int):
    return res.data["todos"][index]["owner"]


@pytest.mark.asyncio
async def test_owner_rejects_bool_and_non_positive(tmp_path, monkeypatch):
    """plan-560-2311 D8：`True` / `-1` / `"-1"` 一律忽略。

    `bool` 是 `int` 的子类，旧实现让 `True` 变成 `agent_id=1`（凭空指向另一个子代理）；
    字符串分支的 `.isdigit()` 挡得住 `"-5"`，int 分支却放过 `-5`，两套口径不一致。
    """
    async def _no_db(self, ctx, turn_id, todos, folded_contents=None):
        return False  # 本用例只校验参数解析，不落库

    monkeypatch.setattr(TodoWriteTool, "_sync_to_db", _no_db)

    todos = [
        {"content": "bool", "status": "pending", "owner": True},
        {"content": "negative", "status": "pending", "owner": -1},
        {"content": "negative-str", "status": "pending", "owner": "-1"},
        {"content": "zero", "status": "pending", "owner": 0},
        {"content": "int-ok", "status": "pending", "owner": 3},
        {"content": "str-ok", "status": "pending", "owner": "7"},
        {"content": "absent", "status": "pending"},
    ]

    res = await TodoWriteTool().run({"todos": todos}, _ctx(tmp_path))

    assert res.ok, res.error
    assert _owner_of(res, 0) is None, "True 不能被当成 agent_id=1"
    assert _owner_of(res, 1) is None, "负数 id 非法"
    assert _owner_of(res, 2) is None, "字符串负数同样非法（与 int 分支口径一致）"
    assert _owner_of(res, 3) is None
    assert _owner_of(res, 4) == 3
    assert _owner_of(res, 5) == 7
    assert _owner_of(res, 6) is None


@pytest.mark.asyncio
async def test_duplicates_deduped_before_max_folding(tmp_path, monkeypatch):
    """plan-560-2311 D8：必须先按 content 去重、再判断是否超过 `_MAX_TODOS`。

    构造：18 项已完成 + 3 项重复（content 与已完成项相同）+ 2 项待办 = 23 条原始条目。
    去重后正好 20 项（= 上限），因此**不应发生任何折叠**。旧顺序会先按 23 > 20 折叠 3 项
    已完成，去重后只剩 17 项——真实步骤被重复项挤掉。
    """
    captured: dict = {}

    async def _no_db(self, ctx, turn_id, todos, folded_contents=None):
        captured["todos"] = list(todos)
        captured["folded"] = set(folded_contents or set())
        return False

    monkeypatch.setattr(TodoWriteTool, "_sync_to_db", _no_db)

    done = [{"content": f"done-{i}", "status": "completed"} for i in range(18)]
    dupes = [{"content": f"done-{i}", "status": "completed"} for i in (0, 1, 2)]
    pending = [{"content": f"next-{i}", "status": "pending"} for i in range(2)]

    res = await TodoWriteTool().run({"todos": done + dupes + pending}, _ctx(tmp_path))

    assert res.ok, res.error
    assert len(res.data["todos"]) == _MAX_TODOS, "去重后正好等于上限，一项都不该被折叠"
    assert captured["folded"] == set()
    assert "已折叠" not in res.output
    # 每一项真实步骤都还在（占位项被去重掉的不算）
    contents = {t["content"] for t in res.data["todos"]}
    assert contents == {f"done-{i}" for i in range(18)} | {"next-0", "next-1"}


@pytest.mark.asyncio
async def test_folding_records_dropped_items_separately(tmp_path, monkeypatch):
    """超过上限时被折叠的项要单独记下来（供落库时只隐藏、不改状态）。"""
    captured: dict = {}

    async def _capture(self, ctx, turn_id, todos, folded_contents=None):
        captured["todos"] = list(todos)
        captured["folded"] = set(folded_contents or set())
        return False

    monkeypatch.setattr(TodoWriteTool, "_sync_to_db", _capture)

    todos = [{"content": f"done-{i}", "status": "completed"} for i in range(5)]
    todos += [{"content": f"open-{i}", "status": "pending"} for i in range(16)]  # 共 21 项

    res = await TodoWriteTool().run({"todos": todos}, _ctx(tmp_path))

    assert res.ok, res.error
    assert len(captured["todos"]) == _MAX_TODOS
    assert captured["folded"] == {"done-0"}, "折叠的是最早完成的那一项"
    assert "done-0" not in {t["content"] for t in captured["todos"]}
    assert "已折叠最早完成的 1 项" in res.output


@pytest.mark.asyncio
async def test_too_many_open_items_still_rejected(tmp_path, monkeypatch):
    """对照组：未完成项本身超过上限时仍要报错，不能被折叠逻辑吞掉。"""
    async def _no_db(self, ctx, turn_id, todos, folded_contents=None):
        return False

    monkeypatch.setattr(TodoWriteTool, "_sync_to_db", _no_db)

    todos = [{"content": f"s-{i}", "status": "pending"} for i in range(_MAX_TODOS + 2)]

    res = await TodoWriteTool().run({"todos": todos}, _ctx(tmp_path))

    assert not res.ok
    assert "未完成的步骤就有" in res.error


@pytest.mark.asyncio
async def test_folded_done_items_keep_status_in_db(tmp_path):
    """plan-560-2311 D8：折叠只是隐藏，已在库中的已完成项仍是 `done`，不会变成 `cancelled`。

    真实落库验证，分三步（折叠项必须先在库里，才能观察它被折叠时的状态变化）：

    1. 提交 5 项已完成 + 15 项待办 = 20 项（正好上限），全部落库；
    2. 再提交 5 项已完成 + 16 项待办 = 21 项 → 折叠最早的 done-0。它在库里已经是 `done`，
       折叠后必须仍是 `done`、只置 `is_hidden`；
    3. 去掉 done-1 且不再提交 done-0（模拟模型删掉一个真实步骤且折叠项不重复提交）：
       只有 done-1 该置 `cancelled`。
    """
    from sqlalchemy import select

    from app.persistence.database import async_session_factory
    from app.persistence.models.task import Task
    from app.services import session_service

    async with async_session_factory() as db:
        sid = await session_service.create_session(
            db, project_id=None, title="[测试] 清单折叠",
        )
        try:
            ctx = ToolContext(
                workspace_root=str(tmp_path), session_id=sid, task_id=9001,
                agent_id=1, agent_name="t",
            )
            done = [{"content": f"done-{i}", "status": "completed"} for i in range(5)]

            async def _rows() -> dict[str, Task]:
                # 落库经 WriteEngine 的独立同步连接完成，async 侧的 identity map 会返回旧对象；
                # populate_existing 强制按库中最新值刷新，否则读到的是上一轮的状态。
                rows = (
                    await db.execute(
                        select(Task)
                        .where(Task.session_id == sid, Task.kind == "step")
                        .execution_options(populate_existing=True)
                    )
                ).scalars().all()
                return {r.title: r for r in rows}

            # 1) 填满上限：全部落库，此时还没有任何折叠
            r1 = await TodoWriteTool().run(
                {"todos": done + [{"content": f"open-{i}", "status": "pending"} for i in range(15)]},
                ctx,
            )
            assert r1.ok, r1.error
            assert "已折叠" not in r1.output
            seeded = await _rows()
            assert seeded["done-0"].status == "done"
            assert seeded["done-0"].is_hidden is False

            # 2) 多出一项 → 折叠最早的已完成项 done-0
            r2 = await TodoWriteTool().run(
                {"todos": done + [{"content": f"open-{i}", "status": "pending"} for i in range(16)]},
                ctx,
            )
            assert r2.ok, r2.error
            assert "已折叠最早完成的 1 项" in r2.output

            after_fold = await _rows()
            assert after_fold["done-0"].status == "done", "被折叠的已完成项不得被改成 cancelled"
            assert after_fold["done-0"].is_hidden is True, "折叠必须隐藏，否则界面上会重新多出一行"
            assert after_fold["done-1"].is_hidden is False

            # 3) 真正删掉一个步骤（done-1），折叠项 done-0 也不再提交
            remaining = [t for t in done if t["content"] != "done-1"]
            r3 = await TodoWriteTool().run(
                {"todos": remaining
                 + [{"content": f"open-{i}", "status": "pending"} for i in range(16)]},
                ctx,
            )
            assert r3.ok, r3.error

            after_delete = await _rows()
            assert after_delete["done-1"].status == "cancelled", "模型真正删掉的步骤才置 cancelled"
            assert after_delete["done-0"].status == "done", "折叠项的既有状态仍保持不变"
        finally:
            await session_service.delete_session_permanent(db, sid)
