"""归档内容永久删除（plan-20261014-fix4）。

真实 SQLite + 外键开启：构造覆盖全部关联表（含工作流、附件、回滚备份、工作树子项目、项目级配置）的数据，
断言批量删除一次成功、无残留、重复删除幂等；另有元数据守卫，防止以后新增的关联表再次让删除失败。
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.orchestration.workflow import store
from app.persistence.database import Base, async_session_factory, run_write_locked
from app.persistence.models.agent import Agent
from app.persistence.models.audit import AuditLog
from app.persistence.models.config import ConfigProfile
from app.persistence.models.db_connection import DbConnection, DbPolicy
from app.persistence.models.exec_policy import ExecPolicyRule
from app.persistence.models.memory import MemoryEntry
from app.persistence.models.message import Message, Session
from app.persistence.models.project import Project
from app.persistence.models.review import FileReview
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.persistence.models.scheduled import ScheduledTask
from app.persistence.models.task import Artifact, Task
from app.persistence.models.tool_call import ToolCall
from app.persistence.models.turn import Turn
from app.persistence.models.usage_record import UsageRecord
from app.persistence.models.workflow import (
    WorkflowAttempt,
    WorkflowEvent,
    WorkflowMessage,
    WorkflowNode,
    WorkflowPlanRevision,
    WorkflowRun,
    WorkflowToolCall,
)
from app.services import archive_service

PLAN = {
    "title": "冒烟",
    "goal": "检查",
    "flows": [
        {
            "key": "smoke",
            "title": "冒烟",
            "nodes": [
                {"key": "explore", "kind": "explore", "title": "探索", "task": "探索", "role": "explorer"},
                {"key": "verify", "kind": "check", "title": "检查", "inputs": ["explore"],
                 "commands": ["pytest -q"]},
            ],
        }
    ],
}


@pytest.fixture
def uploads(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(settings, "uploads_dir", str(root))
    return root


async def _add(*rows) -> list[int]:
    def _op(s):
        for row in rows:
            s.add(row)
        s.flush()
        ids = [int(r.id) for r in rows]
        s.commit()
        return ids

    return await run_write_locked(_op, label="test.archive.add")


async def _project(path: Path, **kw) -> int:
    path.mkdir(parents=True, exist_ok=True)
    (pid,) = await _add(Project(name=path.name, path=str(path), **kw))
    return pid


async def _session(project_id: int, **kw) -> int:
    (sid,) = await _add(Session(project_id=project_id, title="会话", **kw))
    return sid


async def _populate(sid: int, workspace: Path, uploads: Path, file_id: str) -> None:
    """为一个会话写满全部关联表。"""
    (turn_id,) = await _add(Turn(session_id=sid, status="completed"))
    (agent_id,) = await _add(Agent(name="explorer", kind="sub", session_id=sid, turn_id=turn_id))
    (task_id,) = await _add(Task(session_id=sid, turn_id=turn_id, title="任务"))
    (upload_dir := uploads / file_id).mkdir()
    (upload_dir / "a.png").write_bytes(b"png")
    ckpt = workspace / ".chatcoder" / "checkpoints" / f"{sid}" / "a.py.bak"
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    ckpt.write_text("old", encoding="utf-8")
    await _add(
        Artifact(task_id=task_id, title="产物"),
        Message(session_id=sid, turn_id=turn_id, sender_type="user", msg_type="text",
                content={"text": "看图", "attachments": [{"file_id": file_id, "path": f"{file_id}/a.png"}]}),
        Message(session_id=sid, turn_id=turn_id, sender_type="agent", msg_type="text", content={"text": "好"}),
        ToolCall(session_id=sid, task_id=task_id, agent_id=agent_id, call_key=f"k-{sid}", tool_name="fs_read"),
        RollbackWrite(session_id=sid, turn_id=turn_id, tool="fs_write", path="a.py", old_content="x"),
        TurnSnapshot(session_id=sid, turn_id=turn_id, file_list=[{"ckpt": str(ckpt), "path": "a.py"}]),
        AuditLog(session_id=sid, turn_id=turn_id, action="tool_call"),
        ExecPolicyRule(session_id=sid, command_pattern="(tool)terminal_exec", decision="allow"),
        ScheduledTask(session_id=sid, name="每日", cron="0 9 * * *", prompt="汇总"),
        FileReview(turn_id=turn_id, path="a.py"),
        MemoryEntry(session_id=sid, turn_id=turn_id, text="会话事实"),
        UsageRecord(session_id=sid, turn_id=turn_id, agent_id=agent_id, prompt_tokens=10),
    )
    program_id = await store.create_program(
        session_id=sid, plan=PLAN, workspace=str(workspace), origin_turn_id=turn_id
    )

    def _wf(s):
        node = s.execute(select(WorkflowNode).where(WorkflowNode.node_key == "explore")
                         .join(WorkflowRun, WorkflowRun.id == WorkflowNode.run_id)
                         .where(WorkflowRun.session_id == sid)).scalars().first()
        node.agent_id = agent_id
        attempt = WorkflowAttempt(node_id=node.id, attempt_no=1, agent_id=agent_id, status="done")
        s.add(attempt)
        s.flush()
        s.add(WorkflowMessage(attempt_id=attempt.id, seq=1, kind="delta", payload_json=[]))
        s.add(WorkflowToolCall(attempt_id=attempt.id, node_id=node.id, tool="fs_read",
                               status="done", started_at="2026-10-14T00:00:00Z"))
        s.add(WorkflowEvent(run_id=program_id, seq=999, type="workflow.log.appended"))
        s.commit()

    await run_write_locked(_wf, label="test.archive.wf")


async def _count(model, *criteria) -> int:
    async with async_session_factory() as db:
        return int((await db.execute(select(func.count()).select_from(model).where(*criteria))).scalar() or 0)


async def test_purge_archived_project_cascades_everything(tmp_path, uploads):
    ws = tmp_path / "proj"
    pid = await _project(ws, archived=True)
    wt = await _project(tmp_path / "proj-wt", is_worktree=True, parent_project_id=pid)
    keep_pid = await _project(tmp_path / "other")

    active = await _session(pid)  # 归档项目下的活跃会话：不在归档列表里，但必须一并删除
    archived = await _session(pid, status="archived")
    in_worktree = await _session(wt)
    lone = await _session(keep_pid, status="archived")
    keep = await _session(keep_pid)

    for i, sid in enumerate([active, archived, in_worktree, lone]):
        await _populate(sid, ws, uploads, f"{i + 1:016x}")
    # 分支会话复制过来的消息仍引用 lone 的附件：该附件必须保留
    shared = f"{4:016x}"
    await _add(Message(session_id=keep, sender_type="user", msg_type="text",
                       content={"text": "副本", "attachments": [{"file_id": shared}]}))
    # 项目级数据
    await _add(
        ConfigProfile(name="p", scope="project", project_id=pid, data={}),
        MemoryEntry(session_id=keep, text="项目约定", scope="project", project_id=pid),
        MemoryEntry(session_id=keep, text="全局偏好", scope="global"),
        DbConnection(project_id=pid, name="db", kind="mysql", host="h"),
        DbPolicy(project_id=pid),
    )

    result = await archive_service.purge(project_ids=[pid], session_ids=[lone, 987654])

    assert result["ok"] is True
    assert result["deleted_projects"] == 2  # 主项目 + 工作树子项目
    assert result["deleted_sessions"] == 4
    assert result["missing"] == {"projects": [], "sessions": [987654]}

    gone = [active, archived, in_worktree, lone]
    for model in (Message, ToolCall, RollbackWrite, TurnSnapshot, AuditLog, ExecPolicyRule,
                  ScheduledTask, MemoryEntry, Task, Agent, Turn, WorkflowRun):
        assert await _count(model, model.session_id.in_(gone)) == 0, model.__tablename__
    for model in (WorkflowNode, WorkflowAttempt, WorkflowMessage, WorkflowToolCall, WorkflowEvent,
                  WorkflowPlanRevision, Artifact, FileReview):
        assert await _count(model) == 0, model.__tablename__
    assert await _count(Session, Session.id.in_(gone)) == 0
    assert await _count(Project, Project.id.in_([pid, wt])) == 0
    for model in (ConfigProfile, DbConnection, DbPolicy):
        assert await _count(model, model.project_id == pid) == 0, model.__tablename__
    assert await _count(MemoryEntry, MemoryEntry.project_id == pid) == 0
    # 用量流水保留（用量统计 / 热力图的数据源），只是不再指向已删除的会话
    assert await _count(UsageRecord) == 4
    assert await _count(UsageRecord, UsageRecord.session_id.is_not(None)) == 0
    # 不相关的数据保持原样
    assert await _count(Session, Session.id == keep) == 1
    assert await _count(MemoryEntry, MemoryEntry.scope == "global") == 1
    # 附件：独占的删除，仍被引用的保留；回滚备份文件删除
    assert sorted(p.name for p in uploads.iterdir()) == [shared]
    assert list((ws / ".chatcoder" / "checkpoints").rglob("*.bak")) == []
    assert result["files_removed"] == 3 + 4


async def test_purge_is_idempotent(tmp_path, uploads):
    pid = await _project(tmp_path / "p", archived=True)
    sid = await _session(pid, status="archived")
    first = await archive_service.purge(project_ids=[pid], session_ids=[sid])
    assert first["deleted_sessions"] == 1

    again = await archive_service.purge(project_ids=[pid], session_ids=[sid])
    assert again["ok"] is True
    assert again["deleted_projects"] == 0 and again["deleted_sessions"] == 0
    assert again["missing"] == {"projects": [pid], "sessions": [sid]}


async def test_reclaim_space_vacuums_when_pages_are_free(tmp_path, uploads, monkeypatch):
    pid = await _project(tmp_path / "big", archived=True)
    sid = await _session(pid)
    blob = "x" * 4000
    await _add(*[Message(session_id=sid, sender_type="agent", msg_type="text", content={"text": blob})
                 for _ in range(400)])
    monkeypatch.setattr(archive_service, "MIN_RECLAIM_BYTES", 64 * 1024)

    result = await archive_service.purge(project_ids=[pid], session_ids=[])

    assert result["compacted"] is True
    assert result["freed_bytes"] > 0


def test_every_table_referencing_deleted_rows_is_covered():
    """元数据守卫：外键指向 sessions / projects / turns / tasks / agents / workflow_* 的表必须被清理逻辑覆盖。

    新增这类表时请同步更新 session_service.purge_session_children 或 archive_service._purge_rows，再把表名加入此处。
    """
    covered = {
        "sessions", "messages", "turns", "tasks", "artifacts", "agents", "tool_calls", "rollback_writes",
        "turn_snapshots", "audit_logs", "exec_policy_rules", "scheduled_tasks", "file_reviews",
        "memory_entries", "workflow_runs", "workflow_nodes", "workflow_attempts", "workflow_messages",
        "workflow_events", "workflow_plan_revisions", "workflow_tool_calls", "config_profiles",
    }
    parents = {"sessions", "projects", "turns", "tasks", "agents"}
    offenders = []
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            target = fk.column.table.name
            if (target in parents or target.startswith("workflow_")) and table.name not in covered:
                offenders.append(f"{table.name} → {target}")
    assert offenders == [], f"这些表引用了会被永久删除的行，但删除逻辑没有清理它们：{offenders}"
