"""回滚原子应用的回归测试：中途出现非 IO 异常（如编码失败）时，已改写的文件必须全部还原。

旧实现只捕获 OSError，UnicodeEncodeError 等异常会直接逃逸，工作区停留在半回滚状态，
而消息与快照已经在后续的写引擎事务里被标记。
"""
from pathlib import Path

from app.services import rollback_service as rs


class _FailingCodec:
    """编码第二个文件时抛出 UnicodeEncodeError（模拟 GBK 文件里无法编码的字符）。"""

    def encode_text(self, tf, text):
        if text == "B-new":
            raise UnicodeEncodeError("gbk", "B-new", 0, 1, "illegal multibyte sequence")
        return text.encode("utf-8")


def test_apply_restores_earlier_files_when_later_step_raises(tmp_path: Path, monkeypatch):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"A-orig")
    b.write_bytes(b"B-orig")

    plan = rs.RollbackPlan(workspace=str(tmp_path))
    # tf 非空才会走 encode_text 分支（由 _FailingCodec 决定成败）
    plan.finals["a.txt"] = rs._FinalOp(kind="write", text="A-new", tf=object())
    plan.finals["b.txt"] = rs._FinalOp(kind="write", text="B-new", tf=object())

    monkeypatch.setattr(rs, "_text_codec", lambda: _FailingCodec())

    applied = rs._apply_plan_sync(plan)

    assert applied["ok"] is False
    assert a.read_bytes() == b"A-orig"
    assert b.read_bytes() == b"B-orig"


def test_apply_success_writes_all_targets(tmp_path: Path, monkeypatch):
    a = tmp_path / "a.txt"
    a.write_bytes(b"A-orig")

    plan = rs.RollbackPlan(workspace=str(tmp_path))
    plan.finals["a.txt"] = rs._FinalOp(kind="write", text="A-new", tf=None)

    applied = rs._apply_plan_sync(plan)

    assert applied["ok"] is True
    assert applied["restored"] == 1
    assert a.read_bytes() == b"A-new"
