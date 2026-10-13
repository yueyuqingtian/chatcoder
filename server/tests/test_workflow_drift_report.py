"""续跑前的工作区报告（设计 §6.6）：HEAD 变化单独提示，改动文件可以查看自暂停以来的差异。"""

import subprocess

import pytest

from app.orchestration.workflow import drift, isolation, store
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _runtime,
    _session,
)


def _git(cwd, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path) -> None:
    _git(path, "init", "-q")
    (path / "docs").mkdir()
    (path / "docs" / "notes.md").write_bytes("旧内容\n".encode())
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "初始提交")


async def _paused_program(rt, repo) -> int:
    pid = await rt.create_program(
        session_id=await _session(),
        plan=_backend_plan(scope="docs/notes.md"),
        workspace=str(repo),
    )
    await rt.start(pid)
    await rt.pause(pid)
    return pid


async def test_the_pause_record_keeps_the_head_and_the_text_of_the_scope(tmp_path):
    _init_repo(tmp_path)
    rt = _runtime(ScriptedExecutor())
    pid = await _paused_program(rt, tmp_path)

    record = await store.load_fingerprint(pid)

    assert record["v"] == drift.RECORD_VERSION
    assert record["head"] == await isolation.head_of(str(tmp_path))
    assert record["contents"]["docs/notes.md"] == "旧内容\n"


async def test_a_commit_made_during_the_pause_is_reported_as_a_head_change(tmp_path):
    _init_repo(tmp_path)
    rt = _runtime(ScriptedExecutor())
    pid = await _paused_program(rt, tmp_path)
    head_then = await isolation.head_of(str(tmp_path))

    (tmp_path / "docs" / "notes.md").write_bytes("新内容\n".encode())
    _git(tmp_path, "commit", "-q", "-am", "用户提交")
    head_now = await isolation.head_of(str(tmp_path))
    report = await rt.drift_report(pid)

    assert head_now != head_then
    assert report["head"] == {"from": head_then, "to": head_now}
    assert report["files"] == ["docs/notes.md"]


async def test_the_diff_shows_what_changed_since_the_pause(tmp_path):
    _init_repo(tmp_path)
    rt = _runtime(ScriptedExecutor())
    pid = await _paused_program(rt, tmp_path)
    (tmp_path / "docs" / "notes.md").write_bytes("新内容\n".encode())

    result = await rt.drift_diff(pid, "docs/notes.md")

    assert result["status"] == "modified"
    assert result["reason"] is None
    assert "-旧内容" in result["diff"]
    assert "+新内容" in result["diff"]


async def test_the_diff_refuses_files_outside_the_change_list(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "docs" / "other.md").write_bytes("别的文件\n".encode())
    rt = _runtime(ScriptedExecutor())
    pid = await _paused_program(rt, tmp_path)
    (tmp_path / "docs" / "other.md").write_bytes("改了别的文件\n".encode())

    with pytest.raises(ValueError, match="不在本次改动清单中"):
        await rt.drift_diff(pid, "docs/other.md")


async def test_a_file_too_large_to_keep_gets_an_explanation_instead_of_a_diff(
    tmp_path, monkeypatch
):
    _init_repo(tmp_path)
    monkeypatch.setattr(drift, "TEXT_LIMIT_BYTES", 4)
    rt = _runtime(ScriptedExecutor())
    pid = await _paused_program(rt, tmp_path)
    (tmp_path / "docs" / "notes.md").write_bytes("新内容\n".encode())

    result = await rt.drift_diff(pid, "docs/notes.md")

    assert result["diff"] is None
    assert "无法展示差异" in result["reason"]


async def test_records_written_before_this_version_still_drive_the_drift_check(tmp_path):
    _init_repo(tmp_path)
    rt = _runtime(ScriptedExecutor())
    pid = await _paused_program(rt, tmp_path)
    # 旧版本只保存“路径 → 指纹”的映射：仍应能比较出外部改动
    old_style = {
        "docs/notes.md": drift.snapshot_fingerprint(str(tmp_path), ["docs/notes.md"])[
            "docs/notes.md"
        ]
    }
    await store.update_run(pid, fingerprint_json=old_style)
    (tmp_path / "docs" / "notes.md").write_bytes("新内容\n".encode())

    assert await rt.drift_of(pid) == ["docs/notes.md"]
    assert (await rt.drift_report(pid))["head"] is None
