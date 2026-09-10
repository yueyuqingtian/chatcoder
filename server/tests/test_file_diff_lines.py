# -*- coding: utf-8 -*-
"""test_file_diff_lines：验证 _diff_lines / _truncate_window 行级 diff 展示正确性。

覆盖：
- 大文件改 2 行不再全量 -/+（与徽标 +N -M 同源一致）；
- 编辑位于旧 2000 行截断线之后（>2000）仍可在窗口 diff 中可见；
- 新增/删除/相同文本等边界；
- _diff_lines 的 add/del 计数与 _diff_stats 完全一致（保证展开与徽标一致）。
"""
from app.services.rollback_service import _diff_lines, _diff_stats, _truncate_window


def _make(n: int, prefix: str = "line") -> str:
    """每行唯一，避免 SequenceMatcher autojunk 对重复行的漂移，保证 diff 精确。"""
    return "\n".join(f"{prefix}_{i:04d}_content" for i in range(n))


def _counts(lines):
    adds = sum(1 for l in lines if l["type"] == "add")
    dels = sum(1 for l in lines if l["type"] == "del")
    ctxs = sum(1 for l in lines if l["type"] == "ctx")
    return adds, dels, ctxs


def test_edit_mid_lines_not_full_replaced():
    """大文件中段插入 2 行：_diff_lines 只产出 2 add + 上下文，远小于文件行数（非全量 -/+）。"""
    n = 3000
    before = _make(n)
    lb = before.split("\n")
    insert = ["__INSERT_A__", "__INSERT_B__"]
    after = "\n".join(lb[:1500] + insert + lb[1500:])
    lines = _diff_lines(before, after)
    adds, dels, ctxs = _counts(lines)
    # 只增不删、数量精确；总行数远小于 3000 → 未退化为全量 -/+
    assert adds == 2, lines
    assert dels == 0
    assert len(lines) < 30
    assert ctxs > 0
    # 与徽标统计一致
    stat_add, stat_del = _diff_stats(before, after)
    assert adds == stat_add and dels == stat_del


def test_replace_two_lines_matches_stat():
    """替换 2 行：_diff_lines 的 add/del 计数与 _diff_stats 完全一致，且非全量。"""
    n = 3000
    before = _make(n)
    lb = before.split("\n")
    lb[999] = "__NEW_A__"
    lb[1000] = "__NEW_B__"
    after = "\n".join(lb)
    lines = _diff_lines(before, after)
    adds, dels, _ = _counts(lines)
    stat_add, stat_del = _diff_stats(before, after)
    assert adds == stat_add == 2
    assert dels == stat_del == 2
    assert len(lines) < 30


def test_edit_beyond_2000_line_visible_in_window():
    """编辑位于旧 2000 行截断线之后：_truncate_window 窗口覆盖该变更区（行号正确）。"""
    n = 3000
    before = _make(n)
    lb = before.split("\n")
    lb[2499] = "__EDIT_AT_2500__"  # 0-based 2499 = 第 2500 行
    after = "\n".join(lb)
    b_slice, a_slice = _truncate_window(before, after, context=50)
    # 窗口不包含文件头部，但必须包含变更行附近
    assert "line_0000" not in b_slice
    assert "__EDIT_AT_2500__" in a_slice
    assert "line_2499_content" in b_slice
    lines = _diff_lines(before, after)
    assert any(l["type"] == "add" and l["text"] == "__EDIT_AT_2500__" for l in lines)


def test_add_new_file_and_delete_all():
    """新增（before=None）全量 add、删除（after=None）全量 del，均不崩溃。"""
    body = _make(5)
    lines_add = _diff_lines(None, body)
    assert all(l["type"] == "add" for l in lines_add)
    lines_del = _diff_lines(body, None)
    assert all(l["type"] == "del" for l in lines_del)
    # 新增文件窗口返回原文本（无前置上下文边界差异）
    b_s, a_s = _truncate_window(None, body)
    assert b_s is None
    assert a_s is not None


def test_no_change_returns_empty():
    """前后相同 → _diff_lines 为空，_truncate_window 返回原文本。"""
    body = _make(10)
    assert _diff_lines(body, body) == []
    b_s, a_s = _truncate_window(body, body)
    assert b_s == body and a_s == body
