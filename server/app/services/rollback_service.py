"""Turn 级回滚与快照服务（§4.10）。

复用 v0.1 rollback.py 已验证的 git 恢复 + 文件 checkpoint 思路，按 turn 粒度重构。
"""
import asyncio
import difflib
import logging
import re as _re
import shutil
import time as _time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.message import Message
from app.persistence.models.review import FileReview
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot

logger = logging.getLogger(__name__)

_MAX_GIT_TIMEOUT = 30
_CHECKPOINT_DIR = ".chatcoder/checkpoints"

# 写盘工具集合：这些工具会改写工作区文件，回滚时只恢复这些文件（绝不全局 git 回滚）
_WRITE_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")


# ── git 基础操作 ──

async def _run_git(workspace: str, *args: str) -> tuple[bool, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_MAX_GIT_TIMEOUT)
        return proc.returncode == 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return False, "", "git 命令超时(>30s)"
    except FileNotFoundError:
        return False, "", "系统未安装 git"
    except OSError as e:
        return False, "", f"git 执行失败: {e}"


async def _get_git_head(workspace: str) -> str | None:
    ok, out, _ = await _run_git(workspace, "rev-parse", "HEAD")
    return out.strip() if ok and out.strip() else None


# ── 文件级 checkpoint（非 git 兜底）──

def checkpoint_file(workspace_root: str, file_path: str) -> str | None:
    """fs_write 前复制文件到 .chatcoder/checkpoints/。返回快照路径。"""
    try:
        src = Path(file_path)
        if not src.is_absolute():
            src = Path(workspace_root) / file_path
        if not src.exists():
            return None  # 新文件无需快照
        ckpt_dir = Path(workspace_root) / _CHECKPOINT_DIR
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        ts = _time.strftime("%Y%m%d_%H%M%S")
        ckpt_path = ckpt_dir / f"{ts}_{src.name}"
        shutil.copy2(str(src), str(ckpt_path))
        return str(ckpt_path)
    except OSError as e:
        logger.debug("[checkpoint] 快照失败: %s", e)
        return None


def restore_checkpoint(workspace_root: str, checkpoint_path: str, target_path: str) -> bool:
    try:
        src = Path(checkpoint_path)
        if not src.is_absolute():
            src = Path(workspace_root) / checkpoint_path
        if not src.exists():
            return False
        dst = Path(target_path)
        if not dst.is_absolute():
            dst = Path(workspace_root) / target_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        return True
    except OSError as e:
        logger.warning("[checkpoint] 恢复失败: %s", e)
        return False


# ── 快照创建 ──

async def create_turn_snapshot(db: AsyncSession, *, session_id: int, turn_id: int,
                               workspace: str, user_message_id: int | None = None) -> TurnSnapshot | None:
    """turn 开始前创建快照。"""
    try:
        git_head = await _get_git_head(workspace)
    except Exception:
        git_head = None
    snap = TurnSnapshot(
        session_id=session_id, turn_id=turn_id,
        user_message_id=user_message_id, git_head=git_head,
        file_list=[], new_files=[],
    )
    db.add(snap)
    await db.flush()
    return snap


def record_checkpoint(snap: TurnSnapshot, checkpoint_path: str, new_file: str | None = None) -> None:
    """工具写盘后登记 checkpoint 或新建文件。"""
    if new_file:
        snap.new_files = list(snap.new_files or []) + [new_file]
    elif checkpoint_path:
        snap.file_list = list(snap.file_list or []) + [checkpoint_path]


# ── 回滚执行 ──

async def rollback_turn(db: AsyncSession, *, turn_id: int,
                        restore_to_composer: bool = True) -> dict:
    """回滚指定 turn：文件恢复 + 消息软删 + 任务取消。

    restore_to_composer=True 时返回该 turn 用户消息原文（前端回填输入框）。
    """
    snap = await _snapshot_for_turn(db, turn_id)
    if snap is None:
        return {"ok": False, "reason": "无可回滚的快照"}
    if snap.rolled_back:
        return {"ok": False, "reason": "该 turn 已回滚，不可重复操作"}

    from app.persistence.models.message import Session
    session = await db.get(Session, snap.session_id)
    workspace = session.worktree_path if session and session.worktree_path else None
    if workspace is None:
        from app.services.project_service import get_project
        if session and session.project_id:
            project = await get_project(db, session.project_id)
            workspace = project.path if project else None
    workspace = workspace or ""

    # 1. 先真正停止当前任务：设置 cancel 事件中断运行中的 agent loop（主/子代理共享），
    #    并等待其退出（最长 3s），避免回滚期间 agent 仍在写文件导致并发冲突。
    #    （延迟 import 避免与 engine 模块循环依赖）
    from app.orchestration import engine
    await engine.cancel_turn(turn_id)
    if turn_id in engine._running_turns:
        for _ in range(30):
            if turn_id not in engine._running_turns:
                break
            await asyncio.sleep(0.1)

    # 1.1 DB 层兜底：将该 turn 之后 pending/running 的任务置 cancelled
    from app.services import task_service, turn_service
    await task_service.cancel_turn_tasks(db, snap.session_id, turn_id)

    # 1.2 产物清扫（v12）：该 turn 及其之后任务的 artifact_ids 置空，
    #     Artifact 行 task_id 置 NULL（保留行，前端不再按任务展示），
    #     该 turn 的 FileReview 审核记录作废删除——回滚后产物区不残留已撤销文件。
    from app.persistence.models.task import Artifact, Task
    task_res = await db.execute(
        select(Task).where(
            Task.session_id == snap.session_id,
            Task.turn_id >= turn_id,
        )
    )
    rollback_task_ids: list[int] = []
    for t in task_res.scalars().all():
        if t.artifact_ids:
            t.artifact_ids = []
            rollback_task_ids.append(t.id)
    if rollback_task_ids:
        art_res = await db.execute(
            select(Artifact).where(Artifact.task_id.in_(rollback_task_ids))
        )
        for a in art_res.scalars().all():
            a.task_id = None
    review_res = await db.execute(
        select(FileReview).where(FileReview.turn_id == turn_id)
    )
    for rv in review_res.scalars().all():
        await db.delete(rv)
    await db.flush()

    # 2. 文件回滚（v9 精确回滚：只撤销 AI 改动的那部分，保留用户手动改动；
    #    绝不全局 git 恢复，避免误伤非当前 turn 的改动）
    writes = await list_turn_writes(db, snap.session_id, turn_id)
    if writes and workspace:
        file_result = await _rollback_turn_files_precise(workspace, writes)
    else:
        # 旧数据无写盘记录：降级为按路径 git 单文件恢复（仍不全局回滚）
        written_paths = await _turn_written_paths(db, snap.session_id, turn_id)
        file_result = {"ok": True, "skipped": not written_paths, "restored": 0, "deleted": 0, "failed": 0,
                       "reason": "该 turn 无精确写盘记录，降级恢复" if written_paths else "该 turn 无文件写入记录，无需恢复"}
        if written_paths and workspace:
            file_result = await _rollback_turn_files(workspace, written_paths)

    # 3. 消息软删：软删主线程「本 turn 及其之后」的消息，同时软删该 turn 及其之后
    #    子代理线程（thread_id 属于 kind=sub 的 agent）的消息——子代理消息也是本 turn 的
    #    执行内容，回滚后不应残留。turn_id IS NULL 的历史消息不删除，避免误删非当前 turn 内容。
    res = await db.execute(
        select(Message).where(
            Message.session_id == snap.session_id,
            Message.deleted == False,  # noqa: E712
        )
    )
    # 该 turn 及其之后的子代理（v10：子代理线程消息随 turn 一并回滚）
    from app.persistence.models.agent import Agent
    sub_res = await db.execute(
        select(Agent).where(
            Agent.session_id == snap.session_id,
            Agent.kind == "sub",
            Agent.turn_id >= turn_id,
        )
    )
    sub_agents = list(sub_res.scalars().all())
    sub_ids_set = {a.id for a in sub_agents}
    msgs = [
        m for m in res.scalars().all()
        if m.turn_id is not None and m.turn_id >= turn_id
        and (m.thread_id is None or m.thread_id in sub_ids_set)
    ]
    user_text = None
    for m in msgs:
        if m.id == snap.user_message_id and restore_to_composer:
            user_text = str((m.content or {}).get("text", ""))
        m.deleted = True

    # 3.1 子代理 Agent 记录置为 terminated（保留历史，明确已随 turn 回滚终止）
    from app.orchestration.agent_events import broadcast
    for a in sub_agents:
        if a.status in ("running", "done", "failed"):
            a.status = "terminated"
            await broadcast(snap.session_id, {
                "event": "agent.updated",
                "payload": {"agent_id": a.id, "status": "terminated"},
            })

    # 4. 状态标记
    snap.rolled_back = True
    await turn_service.update_turn_status(db, turn_id, "rolled_back")
    await db.flush()

    # 5. 审计
    from app.services import audit_service
    await audit_service.log(db, action="rollback", session_id=snap.session_id,
                            turn_id=turn_id, detail={"msgs": len(msgs), "file": file_result})

    return {
        "ok": True,
        "turn_id": turn_id,
        "rolled_back_msgs": len(msgs),
        "file_recovery": file_result,
        "user_message": user_text if restore_to_composer else None,
    }


async def _snapshot_for_turn(db: AsyncSession, turn_id: int) -> TurnSnapshot | None:
    res = await db.execute(select(TurnSnapshot).where(TurnSnapshot.turn_id == turn_id))
    return res.scalars().first()


async def count_rollback_affected(db: AsyncSession, session_id: int, turn_id: int) -> dict:
    """统计本次回滚将连带撤销的任务与消息数（与 rollback_turn 执行口径一致）。

    任务：该 turn 及其之后 pending/running 的任务（将被置 cancelled）。
    消息：主线程该 turn 及其之后未删消息 + 该 turn 及其之后子代理线程消息（将被软删）。
    """
    from app.persistence.models.agent import Agent
    from app.persistence.models.task import Task

    res = await db.execute(
        select(Task).where(
            Task.session_id == session_id,
            Task.status.in_(["pending", "running"]),
        )
    )
    tasks = 0
    for t in res.scalars().all():
        if (t.turn_id or 0) >= turn_id:
            tasks += 1

    sub_res = await db.execute(
        select(Agent).where(
            Agent.session_id == session_id,
            Agent.kind == "sub",
            Agent.turn_id >= turn_id,
        )
    )
    sub_ids = {a.id for a in sub_res.scalars().all()}
    msg_res = await db.execute(
        select(Message).where(
            Message.session_id == session_id,
            Message.deleted == False,  # noqa: E712
            Message.turn_id.is_not(None),
            Message.turn_id >= turn_id,
        )
    )
    messages = sum(
        1 for m in msg_res.scalars().all()
        if m.thread_id is None or m.thread_id in sub_ids
    )
    return {"tasks": tasks, "messages": messages}


# ── 写盘记录（v9 精确回滚依据）──

def resolve_write_paths(tool: str, args: dict) -> list[str]:
    """从工具参数解析目标文件路径（agent_loop 与回滚共用）。"""
    args = args or {}
    if tool == "multi_file_edit":
        paths: list[str] = []
        for e in args.get("edits") or []:
            p = e.get("path") if isinstance(e, dict) else None
            if isinstance(p, str) and p.strip():
                paths.append(p.strip())
        return paths
    p = args.get("path")
    return [p.strip()] if isinstance(p, str) and p.strip() else []


async def record_turn_write(db: AsyncSession, *, session_id: int, turn_id: int,
                            tool: str, path: str, before: str | None, after: str | None) -> None:
    """记录一次写盘操作的前后内容（精确回滚依据）。失败不阻塞（非关键路径）。"""
    try:
        db.add(RollbackWrite(session_id=session_id, turn_id=turn_id, tool=tool,
                             path=path, old_content=before, new_content=after))
        await db.flush()
    except Exception:
        logger.debug("[rollback] 写盘记录失败(非阻塞): %s %s", tool, path, exc_info=True)


async def list_turn_writes(db: AsyncSession, session_id: int, turn_id: int) -> list[RollbackWrite]:
    """查询「本 turn 及其之后」的写盘记录（按时间升序，回滚时倒序撤销）。"""
    res = await db.execute(
        select(RollbackWrite).where(
            RollbackWrite.session_id == session_id,
            RollbackWrite.turn_id >= turn_id,
        ).order_by(RollbackWrite.id.asc())
    )
    return list(res.scalars().all())


# ── 变更审核（v11）：以 RollbackWrite 为数据源的聚合 + FileReview 审核状态 ──

_MAX_DIFF_CHANGE_LINES = 2000  # 单文件 diff 变更行数上限，超出截断提示


def _diff_stats(before: str | None, after: str | None) -> tuple[int, int]:
    """用 difflib.unified_diff 统计增删行数（仅含变更行，不含上下文/文件头）。"""
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    if b == a:
        return 0, 0
    additions = deletions = 0
    for line in difflib.unified_diff(b, a, n=0, lineterm=""):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _truncate_lines(text: str | None, limit: int = _MAX_DIFF_CHANGE_LINES) -> str | None:
    """按行截断长文本（保留足够上下文用于 diff 展示），返回 None 表示原本无内容。"""
    if text is None:
        return None
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + "\n…(内容过长，已截断)"


async def list_turn_changes(db: AsyncSession, *, session_id: int, turn_id: int,
                            workspace: str) -> list[dict]:
    """聚合该 turn 的全部写盘记录为变更清单（不含文件全文）。

    主/子代理写盘同 turn_id，一并聚合；按 path 分组：
    - before = 首条写盘的 old_content，after = 当前磁盘内容
    - action：全部 old_content 为 None → added；文件当前不存在 → deleted；否则 modified
    - difflib.unified_diff 统计增删行数
    - reviewed 来自 FileReview（turn_id+path 唯一键），与实际磁盘内容无关
    """
    res = await db.execute(
        select(RollbackWrite).where(
            RollbackWrite.session_id == session_id,
            RollbackWrite.turn_id == turn_id,
        ).order_by(RollbackWrite.id.asc())
    )
    writes = list(res.scalars().all())
    if not writes:
        return []

    by_path: dict[str, list[RollbackWrite]] = {}
    for w in writes:
        by_path.setdefault(w.path, []).append(w)

    reviewed = await reviewed_paths(db, turn_id)
    changes: list[dict] = []
    for path, recs in by_path.items():
        before = recs[0].old_content
        after = _read_file_text(workspace, path)
        if all(r.old_content is None for r in recs):
            action = "added"
        elif after is None:
            action = "deleted"
        else:
            action = "modified"
        additions, deletions = _diff_stats(before, after)
        changes.append({
            "path": path,
            "action": action,
            "additions": additions,
            "deletions": deletions,
            "reviewed": reviewed.get(path, False),
        })
    changes.sort(key=lambda c: c["path"])
    return changes


async def get_file_diff(db: AsyncSession, *, session_id: int, turn_id: int,
                        workspace: str, path: str) -> dict | None:
    """单文件 diff：before=首条写盘前内容，after=当前磁盘内容（大文件截断）。

    返回 None 表示该 turn 无此文件写盘记录。
    """
    res = await db.execute(
        select(RollbackWrite).where(
            RollbackWrite.session_id == session_id,
            RollbackWrite.turn_id == turn_id,
            RollbackWrite.path == path,
        ).order_by(RollbackWrite.id.asc())
    )
    writes = list(res.scalars().all())
    if not writes:
        return None

    before = writes[0].old_content
    after = _read_file_text(workspace, path)
    add, dele = _diff_stats(before, after)
    truncated = (add + dele) > _MAX_DIFF_CHANGE_LINES
    return {
        "path": path,
        "before": _truncate_lines(before),
        "after": _truncate_lines(after),
        "truncated": truncated,
    }


async def reviewed_paths(db: AsyncSession, turn_id: int) -> dict[str, bool]:
    """查询该 turn 的全部审核记录（path -> reviewed）。"""
    res = await db.execute(select(FileReview).where(FileReview.turn_id == turn_id))
    return {r.path: bool(r.reviewed) for r in res.scalars().all()}


async def upsert_file_reviews(db: AsyncSession, *, turn_id: int,
                              paths: list[str], reviewed: bool) -> int:
    """幂等写入审核记录（turn_id+path 唯一键 upsert），返回变更条数。

    reviewed=False 时仅更新已存在记录为未审，不删除记录（保证状态可追溯）。
    """
    updated = 0
    paths = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
    if not paths:
        return 0
    existing: dict[str, FileReview] = {}
    res = await db.execute(
        select(FileReview).where(
            FileReview.turn_id == turn_id,
            FileReview.path.in_(paths),
        )
    )
    for r in res.scalars().all():
        existing[r.path] = r
    for p in paths:
        rec = existing.get(p)
        if rec is None:
            db.add(FileReview(turn_id=turn_id, path=p, reviewed=reviewed))
            updated += 1
        elif rec.reviewed != reviewed:
            rec.reviewed = reviewed
            updated += 1
    if updated:
        await db.flush()
    return updated


async def resolve_turn_workspace(db: AsyncSession, turn_id: int) -> tuple[int | None, str]:
    """解析 turn 所属会话与工作区路径（worktree 优先，回退项目路径）。

    返回 (session_id, workspace)。无快照时 session_id 为 None。
    """
    snap = await _snapshot_for_turn(db, turn_id)
    if snap is None:
        return None, ""
    from app.persistence.models.message import Session
    session = await db.get(Session, snap.session_id)
    workspace = session.worktree_path if session and session.worktree_path else None
    if workspace is None:
        from app.services.project_service import get_project
        if session and session.project_id:
            project = await get_project(db, session.project_id)
            workspace = project.path if project else None
    return snap.session_id, workspace or ""


# ── 文件读写（工作区相对路径）──

def _is_safe_rel_path(p: str) -> bool:
    """路径安全校验：仅接受工作区内相对路径，拒绝绝对路径与 .. 逃逸。"""
    norm = p.replace("\\", "/")
    if not norm or norm.startswith("/") or _re.match(r"^[A-Za-z]:", norm):
        return False
    parts = norm.split("/")
    return ".." not in parts and "." not in parts and parts[0] != ""


def _fs_path(workspace: str, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else Path(workspace) / rel


def _read_file_text(workspace: str, rel: str) -> str | None:
    try:
        p = _fs_path(workspace, rel)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def _write_file_text(workspace: str, rel: str, content: str) -> bool:
    try:
        p = _fs_path(workspace, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return True
    except OSError as e:
        logger.warning("[rollback] 写入文件失败 %s: %s", rel, e)
        return False


def _delete_file(workspace: str, rel: str) -> bool:
    try:
        p = _fs_path(workspace, rel)
        if not p.exists():
            return True
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
        return True
    except OSError as e:
        logger.warning("[rollback] 删除文件失败 %s: %s", rel, e)
        return False


# ── 三路合并（精确回滚核心，v9）──

def _merge3(base: str, theirs: str, ours: str) -> tuple[str, bool]:
    """三路合并：在 base（AI 写盘前）上保留用户改动（ours 相对 theirs 的差异）。

    即"只撤销 AI 引入的改动、保留用户手动改动"：
    - base  = AI 写盘前文件内容
    - theirs = AI 写盘后文件内容
    - ours  = 当前文件内容（= AI 写盘后 + 用户手动改动）

    返回 (merged, conflict)。conflict=True 表示用户改动与 AI 改动重叠
    （用户直接修改了 AI 新增/替换的行），无法安全合并——调用方应跳过该文件
    并提示用户，绝不覆盖用户改动。
    """
    b = base.splitlines(keepends=True)
    t = theirs.splitlines(keepends=True)
    o = ours.splitlines(keepends=True)
    if t == o:
        return base, False  # 无用户改动，直接恢复 base
    if b == t:
        return ours, False  # AI 未实际改动，无需回滚

    # AI 映射：theirs 行 -> base 行（仅 AI 保留的行）；AI 新增行的 base 锚点
    t2b: dict[int, int] = {}
    ai_new_anchor: dict[int, int] = {}  # AI 新增行 theirs idx -> base 锚点行
    ai_del_base: set[int] = set()       # 被 AI 纯删除的 base 行（回滚恢复）
    ai_del_contents: set[str] = set()   # AI 删除的 base 行内容（用于用户重复恢复去重）
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, t, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                t2b[j1 + k] = i1 + k
        elif tag == "replace":
            for j in range(j1, j2):
                ai_new_anchor[j] = i1
        elif tag == "delete":
            ai_del_base.update(range(i1, i2))
            ai_del_contents.update(b[i1:i2])
        elif tag == "insert":
            for j in range(j1, j2):
                ai_new_anchor[j] = i1

    # 用户改动：theirs -> ours。插入锚点映射到 base 行索引（在该行前插入）。
    def _user_anchor(t_idx: int) -> int | None:
        if t_idx in t2b:
            return t2b[t_idx]
        if t_idx in ai_new_anchor:
            return ai_new_anchor[t_idx]
        if t_idx == 0:
            return 0
        if t_idx >= len(t):
            return len(b)
        return None

    user_del: set[int] = set()          # 用户删除的 theirs 行
    insert_at: dict[int, list[str]] = {}  # base 锚点 -> 用户插入的行
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, t, o, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            user_del.update(range(i1, i2))
        elif tag == "insert":
            anchor = _user_anchor(i1)
            if anchor is None:
                return ours, True
            ins = o[j1:j2]
            # 用户"重复恢复" AI 删除的行：回滚后会恢复该行，跳过冗余插入
            if len(ins) == 1 and ins[0] in ai_del_contents:
                continue
            insert_at.setdefault(anchor, []).extend(ins)
        elif tag == "replace":
            # 用户直接修改了 AI 新增的行 -> 无法安全合并，标记冲突
            for k in range(i1, i2):
                if k in ai_new_anchor:
                    return ours, True
            user_del.update(range(i1, i2))
            anchor = _user_anchor(i1)
            if anchor is None:
                return ours, True
            ins = o[j1:j2]
            if len(ins) == 1 and ins[0] in ai_del_contents:
                continue
            insert_at.setdefault(anchor, []).extend(ins)

    user_del_base = {t2b[j] for j in user_del if j in t2b}
    result: list[str] = []
    for i in range(len(b) + 1):
        if i in insert_at:
            result.extend(insert_at[i])
        if i < len(b):
            if i in user_del_base:
                continue
            result.append(b[i])
    return "".join(result), False


def _rollback_one_file(workspace: str, rec: RollbackWrite) -> dict:
    """对单条写盘记录做精确回滚（内存三路合并 + 写盘）。"""
    rel = rec.path
    before, after = rec.old_content, rec.new_content
    current = _read_file_text(workspace, rel)
    if before is None:
        # 新建文件：回滚 = 删除；若用户改过（当前 != AI 写后）则跳过并标记冲突
        if current == after:
            _delete_file(workspace, rel)
            return {"path": rel, "action": "delete", "ok": True, "conflict": False}
        return {"path": rel, "action": "delete", "ok": False, "conflict": True,
                "reason": "文件已被手动修改，回滚已跳过"}
    if current is None:
        return {"path": rel, "action": "restore", "ok": False, "conflict": True,
                "reason": "文件当前不存在，跳过"}
    if before == after:
        return {"path": rel, "action": "restore", "ok": True, "conflict": False, "reason": "AI 未实际改动"}
    merged, conflict = _merge3(before, after, current)
    if conflict:
        return {"path": rel, "action": "restore", "ok": False, "conflict": True,
                "reason": "与手动改动存在重叠，已跳过（可手动处理）"}
    if not _write_file_text(workspace, rel, merged):
        return {"path": rel, "action": "restore", "ok": False, "conflict": True, "reason": "写入失败"}
    return {"path": rel, "action": "restore", "ok": True, "conflict": False}


def _preview_one_file(workspace: str, rec: RollbackWrite) -> dict:
    """对单条写盘记录计算回滚预览（不修改文件）。"""
    rel = rec.path
    before, after = rec.old_content, rec.new_content
    current = _read_file_text(workspace, rel)
    if before is None:
        if current == after:
            return {"path": rel, "action": "delete", "conflict": False, "before": current, "after": ""}
        return {"path": rel, "action": "delete", "conflict": True,
                "reason": "文件已被手动修改，回滚将跳过", "before": current, "after": current}
    if current is None:
        return {"path": rel, "action": "restore", "conflict": True,
                "reason": "文件当前不存在，跳过", "before": "", "after": ""}
    if before == after:
        return {"path": rel, "action": "restore", "conflict": False,
                "reason": "AI 未实际改动", "before": current, "after": current}
    merged, conflict = _merge3(before, after, current)
    if conflict:
        return {"path": rel, "action": "restore", "conflict": True,
                "reason": "与手动改动存在重叠，回滚将跳过", "before": current, "after": current}
    return {"path": rel, "action": "restore", "conflict": False, "before": current, "after": merged}


async def _rollback_turn_files_precise(workspace: str, writes: list[RollbackWrite]) -> dict:
    """精确回滚：按写盘记录倒序（最后写入先撤销），只撤销 AI 改动部分。

    用户在同一文件上的手动改动（非 AI 写盘引入的部分）被保留；
    与 AI 改动重叠的冲突文件跳过并标记，不覆盖用户改动。
    """
    restored = deleted = skipped = conflicts = 0
    details: list[dict] = []
    for rec in reversed(writes):
        r = _rollback_one_file(workspace, rec)
        details.append(r)
        if r.get("ok"):
            if r["action"] == "delete":
                deleted += 1
            else:
                restored += 1
        elif r.get("conflict"):
            conflicts += 1
        else:
            skipped += 1
    return {
        "ok": conflicts == 0,
        "skipped": len(details) == 0,
        "restored": restored,
        "deleted": deleted,
        "failed": conflicts,
        "conflicts": [d["path"] for d in details if d.get("conflict")],
        "detail": f"恢复 {restored} 个，删除 {deleted} 个，冲突跳过 {conflicts} 个",
    }


async def preview_turn_files(workspace: str, writes: list[RollbackWrite]) -> list[dict]:
    """计算本次回滚的文件级预览（不修改工作区文件）。"""
    files: list[dict] = []
    for rec in reversed(writes):
        r = _preview_one_file(workspace, rec)
        files.append(r)
    return files


async def _turn_written_paths(db: AsyncSession, session_id: int, turn_id: int) -> list[str]:
    """提取「本 turn 及其之后」通过写盘工具改写的文件路径（语义：回滚该 turn 及其后的更改）。

    包含主代理与子代理的写操作（子代理消息 thread_id 非空但 turn_id 相同）。
    turn_id 为 NULL 的消息不会命中（>= 比较排除 NULL），因此不会把无 turn_id 的
    历史消息当作回滚依据；依赖已落库的 TOOL_CALL 消息，不额外引入存储。
    """
    from app.core.enums import MsgType
    res = await db.execute(
        select(Message).where(
            Message.session_id == session_id,
            Message.turn_id >= turn_id,
            Message.msg_type == MsgType.TOOL_CALL.value,
        )
    )
    paths: list[str] = []
    for m in res.scalars().all():
        c = m.content or {}
        tool = c.get("tool")
        if tool not in _WRITE_TOOLS:
            continue
        args = c.get("args") or {}
        if tool == "multi_file_edit":
            # 多文件编辑：路径在 edits[].path
            for e in args.get("edits") or []:
                p = e.get("path") if isinstance(e, dict) else None
                if isinstance(p, str) and p.strip():
                    paths.append(p.strip())
        else:
            p = args.get("path")
            if isinstance(p, str) and p.strip():
                paths.append(p.strip())
    return paths


async def _rollback_turn_files(workspace: str, paths: list[str]) -> dict:
    """只回滚指定文件：HEAD 中存在 -> git checkout 恢复；否则（该 turn 新建）-> 删除。

    相比旧版全局 `git checkout -- .` + `clean -fd` + `reset --hard`，
    此实现只影响该 turn 真正改写的文件，用户手动改动与其他 turn 的文件不受影响。
    """
    restored = deleted = failed = 0
    seen: set[str] = set()
    for p in paths:
        if not _is_safe_rel_path(p) or p in seen:
            continue
        seen.add(p)
        ok, _, _ = await _run_git(workspace, "cat-file", "-e", f"HEAD:{p}")
        if ok:
            # 已跟踪文件：恢复到 HEAD 版本（撤销该 turn 的修改）
            rok, _, err = await _run_git(workspace, "checkout", "HEAD", "--", p)
            if rok:
                restored += 1
            else:
                failed += 1
                logger.warning("[rollback] 恢复文件失败 %s: %s", p, err)
        else:
            # HEAD 中不存在 -> 该 turn 新建的文件/目录，直接删除
            fpath = Path(workspace) / p
            try:
                if fpath.exists():
                    if fpath.is_dir():
                        shutil.rmtree(fpath, ignore_errors=True)
                    else:
                        fpath.unlink()
                    deleted += 1
            except OSError as e:
                failed += 1
                logger.warning("[rollback] 删除新建文件失败 %s: %s", p, e)
    return {
        "ok": failed == 0,
        "skipped": len(seen) == 0,
        "restored": restored,
        "deleted": deleted,
        "failed": failed,
        "detail": f"恢复 {restored} 个已跟踪文件，删除 {deleted} 个新建文件，失败 {failed} 个",
    }


async def list_snapshots(db: AsyncSession, session_id: int) -> list[TurnSnapshot]:
    res = await db.execute(
        select(TurnSnapshot).where(TurnSnapshot.session_id == session_id)
        .order_by(TurnSnapshot.id.desc())
    )
    return list(res.scalars().all())
