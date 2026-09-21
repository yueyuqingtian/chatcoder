"""AI 自动合并进度事件流测试（plan-308-1542 需求3-A）。

断言：
  1) 进度事件按 prepare → detect → file_* → done 顺序产生；
  2) 纯单侧改动（git 干净合入）**不调用模型**（用 monkeypatch 计数守护）；
  3) 真冲突才走模型，且 done 携带完整汇总报告；
  4) 没有会话上下文时不广播（不报错）。
"""
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import Project, Session  # noqa: F401 注册模型
from app.services import worktree_service


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {p.stderr or p.stdout}")
    return p.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "main"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.local")
    _git(r, "config", "user.name", "t")
    (r / "app.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (r / "other.txt").write_text("o1\no2\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/ai.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


class _Capture:
    """捕获进度广播（替代真实 WS）。

    注意：这里替换的是 `_emit_merge_progress`，它的第二个参数是**原始 payload**
    （事件名 "merge.progress" 由该函数自己在内部包装），因此不能按 {"event":..} 取。
    """

    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, session_id: int, payload: dict) -> None:
        self.events.append({"session_id": session_id, "event": "merge.progress",
                            **(payload or {})})

    @property
    def phases(self) -> list[str]:
        return [e.get("phase") for e in self.events]


@pytest.mark.asyncio
async def test_ai_merge_all_clean_does_not_call_model(db, repo, monkeypatch):
    """仅一侧改动 → git 干净合入，**不得调用模型**，但仍要报进度与汇总。"""
    pid = (p := Project(name="main", path=str(repo)))
    db.add(p)
    await db.commit()
    res = await worktree_service.create_worktree_for_project(db, pid.id, name="wt-a")
    wt = Path(res["path"])
    (wt / "app.txt").write_text("line1\nWT-ONLY\nline3\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "wt only")

    calls = {"n": 0}

    async def _fake_suggest(*args, **kwargs):
        calls["n"] += 1
        return {"ok": True, "suggestion": "X", "model": "fake"}

    monkeypatch.setattr(worktree_service, "ai_merge_suggest", _fake_suggest)

    cap = _Capture()
    monkeypatch.setattr(worktree_service, "_emit_merge_progress", cap)

    out = await worktree_service.ai_merge_all(db, res["project_id"], session_id=1)
    assert out["ok"] is True
    assert calls["n"] == 0, "纯单侧改动不得调用模型"
    assert out["report"]["git"] >= 1
    assert out["report"]["ai"] == 0
    assert cap.phases[0] == "prepare"
    assert "detect" in cap.phases and cap.phases[-1] == "done"
    done = [e for e in cap.events if e.get("phase") == "done"][0]
    assert done["summary"]["total"] >= 1
    assert done["summary"]["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_ai_merge_all_conflict_uses_model_and_reports(db, repo, monkeypatch):
    """真冲突 → 经过 git 标记 + 模型建议，done 带完整报告。"""
    p = Project(name="main", path=str(repo))
    db.add(p)
    await db.commit()
    res = await worktree_service.create_worktree_for_project(db, p.id, name="wt-b")
    wt = Path(res["path"])
    (wt / "app.txt").write_text("line1\nWT-SIDE\nline3\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "wt side")
    # 主工作区改同一行 → 真冲突
    (repo / "app.txt").write_text("line1\nMAIN-SIDE\nline3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main side")

    calls = {"n": 0}

    async def _fake_suggest(*args, **kwargs):
        calls["n"] += 1
        return {"ok": True, "suggestion": "line1\nMERGED\nline3\n", "model": "fake-model"}

    monkeypatch.setattr(worktree_service, "ai_merge_suggest", _fake_suggest)
    cap = _Capture()
    monkeypatch.setattr(worktree_service, "_emit_merge_progress", cap)

    out = await worktree_service.ai_merge_all(db, res["project_id"], session_id=2)
    assert calls["n"] == 1, "真冲突文件应调用一次模型"
    assert out["report"]["ai"] == 1
    assert out["resolved"]["app.txt"] == "line1\nMERGED\nline3\n"

    # 工具调用行（git merge-file / model）必须出现
    tools = [e.get("tool") for e in cap.events if e.get("phase") == "tool"]
    assert any(t and "merge-file" in t for t in tools)
    assert "done" in cap.phases


@pytest.mark.asyncio
async def test_ai_merge_all_survives_model_failure(db, repo, monkeypatch):
    """模型失败不整体中断：该文件记 failed，报告如实体现。"""
    p = Project(name="main", path=str(repo))
    db.add(p)
    await db.commit()
    res = await worktree_service.create_worktree_for_project(db, p.id, name="wt-c")
    wt = Path(res["path"])
    (wt / "app.txt").write_text("line1\nWT\nline3\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "wt")
    (repo / "app.txt").write_text("line1\nMAIN\nline3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main")

    async def _fail(*args, **kwargs):
        return {"ok": False, "error": "未配置可用模型"}

    monkeypatch.setattr(worktree_service, "ai_merge_suggest", _fail)
    cap = _Capture()
    monkeypatch.setattr(worktree_service, "_emit_merge_progress", cap)

    out = await worktree_service.ai_merge_all(db, res["project_id"], session_id=3)
    assert out["ok"] is True  # 主流程不中断
    assert out["report"]["failed"] == 1
    assert cap.phases[-1] == "done"
