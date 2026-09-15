"""plan-248-1273 M2: 独立索引 worker 进程冒烟测试。"""
import sqlite3
import subprocess
import sys


def test_index_worker_runs_outside_server_process(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "sample.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    state_db = workspace / "index_state.db"
    cmd = [
        sys.executable, "-m", "app.index_worker",
        "--workspace", str(workspace),
        "--state-db", str(state_db),
        "--job-id", "test-worker",
    ]
    result = subprocess.run(cmd, cwd=str(__import__("pathlib").Path(__file__).parents[1]), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(state_db) as conn:
        state = dict(conn.execute("SELECT key, value FROM index_state"))
    assert state["status"] == "ready"
    assert state["job_id"] == "test-worker"
    assert int(state["files"]) == 1
