"""plan-1085: 二进制判定三态化与写盘记录兜底测试。

覆盖：
- _is_binary_path 三态：NUL/非 UTF-8 → True；open 连续 PermissionError（模拟
  Windows 杀软/EDR 瞬时锁）重试耗尽 → None（不再误判二进制）；首次失败重试
  成功 → False；正常文本 → False。
- _fallback_after_text：new_content / new_contents（反斜杠归一）/ 缺失与类型错误。
- _resolve_write_record：读取失败+工具兜底 → 文本归属且 after=兜底内容；
  无兜底 → 降级二进制；正常路径磁盘内容优先；内容证据二进制优先。
- 写工具 ToolResult.data 带 new_content（fs_write/editor_apply_diff/
  multi_file_edit），超 16000 字符上限不带。
- get_file_diff：同 turn 同文件 bin=1 污染记录 + 更早 bin=0 记录 → 降级返回
  文本 diff；全 bin=1 → 原 reason 分支不变。
"""
import builtins
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration.agent_loop import _fallback_after_text, _resolve_write_record
from app.persistence.database import Base
from app.persistence.models import Turn  # noqa: F401 注册全部模型到 Base.metadata
from app.persistence.models.rollback import RollbackWrite
from app.services import rollback_service


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """重试退避置 0，测试不真实睡眠。"""
    monkeypatch.setattr(rollback_service, "_BINARY_SNIFF_RETRY_SEC", 0)


# ── _is_binary_path 三态 ──


def test_binary_content_evidence_still_true(workspace):
    (workspace / "a.bin").write_bytes(b"PK\x03\x04\x00\x00\x00\x00")
    assert rollback_service._is_binary_path(str(workspace), "a.bin") is True
    (workspace / "b.bin").write_bytes(b"\xff\xfe\x00\x41\x00\x42")
    assert rollback_service._is_binary_path(str(workspace), "b.bin") is True
    (workspace / "c.txt").write_text("hello\n", encoding="utf-8")
    assert rollback_service._is_binary_path(str(workspace), "c.txt") is False


def test_binary_read_failure_returns_none_after_retries(workspace, monkeypatch):
    """EDR 锁模拟：open 连续 PermissionError → 重试耗尽返回 None（非二进制）。"""
    (workspace / "lock.txt").write_text("text content\n", encoding="utf-8")
    real_open = builtins.open
    calls = {"n": 0}

    def fake_open(file, mode="r", *a, **kw):
        if "b" in str(mode) and str(file).endswith("lock.txt"):
            calls["n"] += 1
            raise PermissionError(13, "denied")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert rollback_service._is_binary_path(str(workspace), "lock.txt") is None
    assert calls["n"] == rollback_service._BINARY_SNIFF_RETRIES


def test_binary_retry_then_success_returns_false(workspace, monkeypatch):
    (workspace / "flaky.txt").write_text("ok content\n", encoding="utf-8")
    real_open = builtins.open
    state = {"n": 0}

    def fake_open(file, mode="r", *a, **kw):
        if "b" in str(mode) and str(file).endswith("flaky.txt"):
            state["n"] += 1
            if state["n"] == 1:
                raise PermissionError(13, "denied")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert rollback_service._is_binary_path(str(workspace), "flaky.txt") is False
    assert state["n"] == 2


# ── 兜底数据源与三态归并 ──


def test_fallback_after_text_sources():
    assert _fallback_after_text(SimpleNamespace(data={"new_content": "x"}), "p") == "x"
    assert _fallback_after_text(
        SimpleNamespace(data={"new_contents": {"a/b.py": "y"}}), "a\\b.py"
    ) == "y"
    assert _fallback_after_text(SimpleNamespace(data=None), "p") is None
    assert _fallback_after_text(SimpleNamespace(data={"new_content": 1}), "p") is None


def test_resolve_write_record_fallback_and_degrade():
    res = SimpleNamespace(data={"new_content": "new\ncontent\n"})
    # 读取失败（bin_post=None）+ 工具兜底 → 文本归属，after=兜底内容
    is_bin, after = _resolve_write_record(
        bin_pre=False, bin_post=None, disk_after=None, result=res, target="f.py",
        turn_id=1, tool_name="editor_apply_diff",
    )
    assert is_bin is False and after == "new\ncontent\n"
    # 无兜底且写后读取失败 → 降级二进制并留痕
    is_bin2, after2 = _resolve_write_record(
        bin_pre=False, bin_post=None, disk_after=None,
        result=SimpleNamespace(data=None), target="f.py",
        turn_id=1, tool_name="editor_apply_diff",
    )
    assert is_bin2 is True and after2 is None
    # 正常路径：磁盘内容优先于工具兜底
    is_bin3, after3 = _resolve_write_record(
        bin_pre=False, bin_post=False, disk_after="disk\n", result=res, target="f.py",
        turn_id=1, tool_name="editor_apply_diff",
    )
    assert is_bin3 is False and after3 == "disk\n"
    # 内容证据二进制优先（before/after 不存文本）
    is_bin4, after4 = _resolve_write_record(
        bin_pre=False, bin_post=True, disk_after=None, result=res, target="f.py",
        turn_id=1, tool_name="editor_apply_diff",
    )
    assert is_bin4 is True and after4 is None


# ── 写工具 data 带 new_content ──


async def test_write_tools_return_new_content(workspace):
    from app.orchestration.tools.editor import EditorApplyDiffTool
    from app.orchestration.tools.fs_write import FsWriteTool
    from app.orchestration.tools.multi_edit import MultiFileEditTool

    ctx = SimpleNamespace(workspace_root=str(workspace))
    fr = await FsWriteTool().run({"path": "n.txt", "content": "line1\nline2\n"}, ctx)
    assert fr.ok and fr.data["new_content"] == "line1\nline2\n"

    (workspace / "e.txt").write_text("old text\n", encoding="utf-8")
    er = await EditorApplyDiffTool().run(
        {"path": "e.txt", "old_text": "old text", "new_text": "new text"}, ctx)
    assert er.ok and er.data["new_content"] == "new text\n"

    mr = await MultiFileEditTool().run(
        {"edits": [{"path": "e.txt", "old_text": "new text", "new_text": "multi text"}]}, ctx)
    assert mr.ok and mr.data["new_contents"]["e.txt"] == "multi text\n"

    # 超上限不带 new_content（走磁盘读取兜底路径）
    fr2 = await FsWriteTool().run({"path": "big.txt", "content": "x" * 20000}, ctx)
    assert fr2.ok and "new_content" not in fr2.data


# ── get_file_diff 污染轮降级 ──


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/plan1085.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_get_file_diff_degrades_for_polluted_binary(db, workspace):
    (workspace / "f.java").write_text("old\nx\ny\n", encoding="utf-8")
    # 污染轮：首条记录被误判 binary=1（无前后内容），其后存在 bin=0 带内容记录
    # → get_file_diff 命中 writes[0].binary 并降级用 bin=0 的 before 出文本 diff
    db.add(RollbackWrite(session_id=7, turn_id=7, tool="editor_apply_diff",
                         path="f.java", old_content=None, new_content=None, binary=True))
    db.add(RollbackWrite(session_id=7, turn_id=7, tool="editor_apply_diff",
                         path="f.java", old_content="old\n", new_content="old\nx\n",
                         binary=False))
    await db.commit()
    diff = await rollback_service.get_file_diff(
        db, session_id=7, turn_id=7, workspace=str(workspace), path="f.java")
    assert diff is not None
    assert diff["before"] == "old\n" and diff["after"] == "old\nx\ny\n"
    assert "降级" in (diff.get("reason") or "")

    # 全 bin=1（真二进制/无任何可用文本记录）→ 原 reason 分支不变
    db.add(RollbackWrite(session_id=8, turn_id=8, tool="editor_apply_diff",
                         path="f.java", old_content=None, new_content=None, binary=True))
    await db.commit()
    diff2 = await rollback_service.get_file_diff(
        db, session_id=8, turn_id=8, workspace=str(workspace), path="f.java")
    assert diff2 is not None
    assert diff2["before"] is None and diff2["after"] is None
    assert "二进制" in diff2["reason"]
