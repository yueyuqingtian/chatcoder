"""Turn 级回滚与快照服务（§4.10）。

复用 v0.1 rollback.py 已验证的 git 恢复 + 文件 checkpoint 思路，按 turn 粒度重构。
"""
from __future__ import annotations

import asyncio
import codecs
import difflib
import glob as _glob
import hashlib
import logging
import os
import re as _re
import shutil
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.models.message import Message
from app.persistence.models.review import FileReview
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot

logger = logging.getLogger(__name__)

_MAX_GIT_TIMEOUT = 30
_CHECKPOINT_DIR = ".chatcoder/checkpoints"
_BINARY_SNIFF_BYTES = 8192  # 二进制判定采样字节数
# plan-20261019-diff: 超限文本生成展示窗口时最多读取的字节数（超过则只保留回滚备份，
# 不再为展示耗内存读全文）。默认回滚记录上限 1MB，8MB 足够覆盖常见的超大 md/日志。
_DIFF_TEXT_MAX_BYTES = 8 * 1024 * 1024
# 展示窗口本身的字符上限：窗口只服务 UI diff 渲染，超出部分对用户也不可读。
_MAX_DIFF_WINDOW_CHARS = 200_000

# 文件文本类型（plan-20261019-diff）：超限文本不再与真二进制混为一谈——
# 前者仍可生成 diff 与 +N -M，只是全文不落库（回滚走 checkpoint）。
FILE_KIND_TEXT = "text"
FILE_KIND_BINARY = "binary"
FILE_KIND_OVERSIZE = "oversize"
FILE_KIND_UNKNOWN = "unknown"

# 写盘工具集合：这些工具会改写工作区文件，回滚时只恢复这些文件（绝不全局 git 回滚）
_WRITE_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")


# plan-1085: 读取瞬时失败的重试参数（吸收 Windows 杀软/EDR 对刚写盘文件的瞬时锁）
_BINARY_SNIFF_RETRIES = 3
_BINARY_SNIFF_RETRY_SEC = 0.08


# 常见文本扩展名：中文项目里 GBK/GB18030 编码的 md、txt、源码文件很常见，
# UTF-8 解码失败不代表二进制——按扩展名兜底，保证这类文件仍能显示 diff。
_TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".log", ".csv", ".tsv", ".json", ".jsonc",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".css", ".scss", ".less", ".html", ".htm", ".xml", ".svg", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".sql", ".java", ".kt", ".go",
    ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh",
    ".ps1", ".bat", ".cmd", ".gradle", ".properties",
}


def _sample_is_text(sample: bytes, rel: str = "") -> bool:
    """采样字节是否为文本。

    不能用 sample.decode("utf-8") 直接判定：采样点可能刚好落在多字节字符中间
    （中文 UTF-8 每个字 3 字节，8192 字节处极易被截断），那会把正常中文文档误判为
    二进制，进而丢弃前后内容——表现为工具卡无 +N -M、展开提示「二进制/大文件」。
    增量解码器 final=False 会保留不完整尾字节，正好避开采样边界。

    UTF-8 解码失败时按文本扩展名兜底（GBK/GB18030 文档），否则才判二进制。
    """
    if b"\x00" in sample:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return Path(rel).suffix.lower() in _TEXT_SUFFIXES
    return True


def inspect_write_target(workspace: str, rel: str, max_bytes: int = 0) -> tuple[str, int]:
    """判定写盘目标类型，返回 (kind, size)。

    - text：可解码文本且未超限 → 前后内容可完整落库并生成精确 diff
    - oversize：文本但超过 max_bytes → 回滚仍走 checkpoint，但可为展示生成窗口 diff
    - binary：含 NUL 或非 UTF-8 → 只做 checkpoint，不展示文本 diff
    - unknown：读取失败（杀软/EDR 瞬时锁）→ 交由调用方用内存/工具返回内容兜底

    plan-20261019-diff: 旧实现把「超限」直接归入 binary，导致大 md 文档连 +N -M
    都不显示、展开只剩「二进制/大文件」提示。
    """
    p = _fs_path(workspace, rel)
    try:
        if not p.exists() or not p.is_file():
            return FILE_KIND_TEXT, 0
        size = p.stat().st_size
    except OSError as e:
        logger.warning("[rollback] 文件 stat 失败(判未知) %s: %s", rel, e)
        return FILE_KIND_UNKNOWN, 0
    last_err: OSError | None = None
    for attempt in range(_BINARY_SNIFF_RETRIES):
        try:
            with open(p, "rb") as f:
                sample = f.read(_BINARY_SNIFF_BYTES)
            if not _sample_is_text(sample, rel):
                return FILE_KIND_BINARY, size
            if max_bytes > 0 and size > max_bytes:
                return FILE_KIND_OVERSIZE, size
            return FILE_KIND_TEXT, size
        except OSError as e:
            last_err = e
            if attempt + 1 < _BINARY_SNIFF_RETRIES:
                _time.sleep(_BINARY_SNIFF_RETRY_SEC)
    logger.warning(
        "[rollback] 文件读取失败(判未知而非二进制) %s: %s", rel, last_err,
    )
    return FILE_KIND_UNKNOWN, size


def _is_binary_path(workspace: str, rel: str, max_bytes: int = 0) -> bool | None:
    """写盘文件是否不可按文本记录：二进制或超限返回 True，文本 False，读失败 None。

    保留旧签名供回滚/兼容调用；需要区分「超限文本」时用 inspect_write_target。
    """
    kind, _size = inspect_write_target(workspace, rel, max_bytes)
    if kind == FILE_KIND_UNKNOWN:
        return None
    return kind != FILE_KIND_TEXT


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

def checkpoint_file(workspace_root: str, file_path: str,
                    session_id: int | None = None, turn_id: int | None = None) -> str | None:
    """写盘 / 命令**执行前**复制文件到 .chatcoder/checkpoints/。返回快照路径。

    plan-20261019-toolcard-v2：调用方必须在工具执行之前调用——此前在执行之后才调用，
    复制到的是写后内容，二进制文件被改写 / 删除后无从恢复。

    v26: 文件名保留原相对路径（超长时截断并追加路径哈希），
    配合 TurnSnapshot.file_list 的 {ckpt, path} 登记可反查原文件，
    使非 git 仓库也能通过 checkpoint 兜底恢复。
    v2.2 (plan-88): 按会话/轮次分子目录（session-{id}/{turn_id}/），
    便于按会话归档/GC 整体清理；反查仍走 file_list 登记。
    """
    try:
        src = Path(file_path)
        if not src.is_absolute():
            src = Path(workspace_root) / file_path
        if not src.exists():
            return None  # 新文件无需快照
        if not src.is_file() or src.stat().st_size > _CKPT_MAX_BYTES:
            return None  # 目录不备份；过大的文件不复制（plan-20261019-toolcard-v2：以免拖慢每次命令）
        root = Path(workspace_root).resolve()
        ckpt_dir = root / _CHECKPOINT_DIR
        if session_id is not None:
            ckpt_dir = ckpt_dir / f"session-{session_id}"
        if turn_id is not None:
            ckpt_dir = ckpt_dir / str(turn_id)
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        # 毫秒级时间戳，避免同秒多次写盘重名（Windows strftime 无 %f）
        ts = f"{_time.strftime('%Y%m%d_%H%M%S')}_{int(_time.time() * 1000) % 1000:03d}"
        rel = src.resolve().relative_to(root) if src.resolve().is_relative_to(root) else None
        if rel is not None:
            safe = _re.sub(r"[^\w.\-]", "_", rel.as_posix())
            if len(safe) > 80:
                safe = safe[:80] + "_" + hashlib.md5(rel.as_posix().encode("utf-8")).hexdigest()[:8]
            name = f"{ts}_{safe}"
        else:
            name = f"{ts}_{src.name}"
        ckpt_path = ckpt_dir / name
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


async def _restore_checkpoints_for_turn(workspace: str, snap: TurnSnapshot) -> dict:
    """从快照的 checkpoint 清单恢复文件（不依赖 git，非 git 仓库可用）。

    仅处理 v26 后的 {ckpt, path} 字典条目；旧字符串条目（无路径映射）跳过。
    同时删除快照登记的 new_files（该 turn 新建、回滚时移除）。
    """
    restored = deleted = failed = 0
    for item in snap.file_list or []:
        if not isinstance(item, dict):
            continue
        ckpt = item.get("ckpt") or ""
        rel = item.get("path") or ""
        if not ckpt or not rel or not _is_safe_rel_path(rel):
            continue
        src = Path(ckpt)
        if not src.is_absolute():
            src = _fs_path(workspace, ckpt)
        if not src.exists():
            failed += 1
            continue
        try:
            dst = _fs_path(workspace, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            restored += 1
        except OSError:
            failed += 1
    for rel in snap.new_files or []:
        if not _is_safe_rel_path(rel):
            continue
        if _delete_file(workspace, rel):
            deleted += 1
        else:
            failed += 1
    return {
        "ok": failed == 0,
        "skipped": restored == 0 and deleted == 0,
        "restored": restored,
        "deleted": deleted,
        "failed": failed,
        "reason": f"checkpoint 恢复 {restored} 个，删除新建 {deleted} 个，失败 {failed} 个",
    }


# ── 快照创建 ──

async def create_turn_snapshot(db: AsyncSession, *, session_id: int, turn_id: int,
                               workspace: str, user_message_id: int | None = None) -> int | None:
    """turn 开始前创建快照（幂等 get-or-create，写引擎单写线程内完成）。

    plan-95: 路由层在 turn 创建时提前调用一次，引擎内再调用时直接返回已有快照——
    保证"发送后立即停止"等任何路径下 turn 都有快照可回滚。
    返回快照 id（已存在/新建；None 表示未创建成功）。
    """
    from app.persistence.database import run_write_locked

    try:
        git_head = await _get_git_head(workspace)
    except Exception:
        git_head = None

    def patch(s):
        from sqlalchemy import select as _select
        from app.persistence.models.rollback import TurnSnapshot
        existing = s.execute(
            _select(TurnSnapshot).where(TurnSnapshot.turn_id == turn_id)
        ).scalars().first()
        if existing is not None:
            return existing.id
        snap = TurnSnapshot(
            session_id=session_id, turn_id=turn_id,
            user_message_id=user_message_id, git_head=git_head,
            file_list=[], new_files=[],
        )
        s.add(snap)
        s.flush()
        sid = snap.id
        s.commit()
        return sid

    return await run_write_locked(patch, label=f"turn.snapshot.{turn_id}")


def record_checkpoint(snap: TurnSnapshot, checkpoint_path: str, new_file: str | None = None) -> None:
    """工具写盘后登记 checkpoint 或新建文件（旧同步入口，仅登记新建文件）。

    checkpoint 登记请用 record_checkpoint_for_turn（含 {ckpt, path} 反查映射）。"""
    if new_file:
        snap.new_files = list(snap.new_files or []) + [new_file]
    elif checkpoint_path:
        snap.file_list = list(snap.file_list or []) + [checkpoint_path]


async def record_checkpoint_for_turn(db: AsyncSession, turn_id: int, checkpoint_path: str,
                                     rel_path: str, new_file: str | None = None) -> bool:
    """按 turn 登记 checkpoint（写线程内查快照并追加 file_list/new_files）。失败非阻塞。

    rel_path：原文件相对工作区的路径（恢复时反查目标）。
    v2.2 (plan-88): 同 turn 同文件去重——首次写盘前备份一次即可恢复整个 turn，
    后续重复写盘跳过备份，避免 checkpoint 目录膨胀。
    v975：经 WriteEngine 单写线程（读-改-写在写线程内一条事务，去重可靠；
    async 侧不再持有 checkpoint 写事务，消除"写后悬挂"长持锁）。
    返回是否**新登记**了一条（plan-20261019-toolcard-v2：被去重 / 失败时为 False，
    调用方据此清理多取的备份文件）。
    """
    if not checkpoint_path and not new_file:
        return False
    try:
        from app.persistence.database import run_write_locked

        def _persist(s) -> bool:
            from sqlalchemy import select as _select
            from app.persistence.models.rollback import TurnSnapshot
            snap = s.execute(
                _select(TurnSnapshot).where(TurnSnapshot.turn_id == turn_id)
            ).scalars().first()
            if snap is None:
                return False
            if new_file:
                files = list(snap.new_files or [])
                if new_file in files:
                    return False
                files.append(new_file)
                snap.new_files = files
            else:
                entry = {"ckpt": checkpoint_path, "path": rel_path}
                items = list(snap.file_list or [])
                if any(isinstance(it, dict) and it.get("path") == rel_path for it in items):
                    return False  # 该文件本 turn 已备份，无需重复
                items.append(entry)
                snap.file_list = items
            s.commit()
            return True

        return bool(await run_write_locked(_persist, label=f"rollback.ckpt.{turn_id}"))
    except Exception:
        logger.debug("[checkpoint] 登记失败(非阻塞): turn=%s %s", turn_id, rel_path, exc_info=True)
        return False


# ── 回滚执行 ──

async def rollback_turn(db: AsyncSession, *, turn_id: int,
                        restore_to_composer: bool = True,
                        allow_partial: bool = False) -> dict:
    """回滚指定 turn 及其之后的全部更改：文件恢复 + 消息软删 + 任务取消。

    安全顺序（plan-20261017-goal12 项 3）：
      1. 预检：按回滚视图模拟整段写盘序列（只读）。存在冲突且未允许部分回滚 → 直接返回
         code=conflict，不停止任务、不改文件、不改消息；
      2. 停止运行中的任务，并重新规划一次（停止期间 agent 可能又写了文件）；
      3. 原子应用：备份原始字节，任一步失败则还原已改写的文件，且不进入消息层；
      4. 消息软删、快照与 turn 状态在一次写引擎事务内提交（本 turn 及之后的全部快照 / turn）。
    allow_partial=True：跳过冲突文件、回滚其余，冲突清单随结果返回供用户知情。

    restore_to_composer=True 时返回该 turn 用户消息原文（前端回填输入框）。
    """
    snap = await _snapshot_for_turn(db, turn_id)
    if snap is None:
        return {"ok": False, "reason": "无可回滚的快照"}
    if snap.rolled_back:
        return {"ok": False, "reason": "该 turn 已回滚，不可重复操作"}

    workspace = await _workspace_for_session(db, snap.session_id)
    has_writes = bool(await list_turn_writes(db, snap.session_id, turn_id))

    # 1. 预检：有冲突且不允许部分回滚时，什么都不做
    if workspace and has_writes:
        plan = await _plan_for_turn(db, snap.session_id, turn_id, workspace)
        if plan.conflicts and not allow_partial:
            return _conflict_result(turn_id, plan)

    # 2. 先真正停止当前任务：设置 cancel 事件中断运行中的 agent loop（主/子代理共享）。
    #    并等待其退出（最长 3s），避免回滚期间 agent 仍在写文件导致并发冲突。
    #    （延迟 import 避免与 engine 模块循环依赖）
    from app.orchestration import engine
    logger.warning("[cancel] 回滚流程先停止运行中任务 turn=%s", turn_id)
    await engine.cancel_turn(turn_id, source="rollback")
    if turn_id in engine._running_turns:
        for _ in range(30):
            if turn_id not in engine._running_turns:
                break
            await asyncio.sleep(0.1)

    # 2.1 DB 层兜底：将该 turn 之后 pending/running 的任务置 cancelled
    from app.services import task_service
    await task_service.cancel_turn_tasks(db, snap.session_id, turn_id)

    # 2.2 产物清扫（v12）：收集该 turn 及其之后任务/产物的清理项（写统一在下方
    #     一次写引擎事务内提交，保持原子性；此处只读不写）。
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
            rollback_task_ids.append(t.id)
    review_res = await db.execute(
        select(FileReview).where(FileReview.turn_id >= turn_id)
    )
    review_ids = [rv.id for rv in review_res.scalars().all()]

    # 3. 文件回滚：停止之后按最终盘面重新规划，再原子应用（不再依赖 git，非 git 仓库可用）
    later_snaps = await _snapshots_from(db, snap.session_id, turn_id)
    if workspace and has_writes:
        plan = await _plan_for_turn(db, snap.session_id, turn_id, workspace)
        if plan.conflicts and not allow_partial:
            return _conflict_result(turn_id, plan)
        applied = _apply_plan_sync(plan)
        if not applied.get("ok"):
            return {"ok": False, "reason": applied.get("error", "回滚失败，工作区已还原")}
        conflicts = _conflict_list(plan)
        file_result = {
            "ok": True,
            "skipped": False,
            "restored": applied.get("restored", 0),
            "deleted": applied.get("deleted", 0),
            "failed": len(conflicts),
            "conflicts": [c["path"] for c in conflicts],
            "conflict_details": conflicts,
            "detail": (
                f"恢复 {applied.get('restored', 0)} 个，删除 {applied.get('deleted', 0)} 个，"
                f"冲突跳过 {len(conflicts)} 个"
            ),
        }
    else:
        # 无写盘记录：优先从快照显式登记的 checkpoint 恢复（不依赖 git，非 git 仓库可用）。
        ckpt_result = await _restore_checkpoints_for_turn(workspace, snap)
        if ckpt_result.get("restored") or ckpt_result.get("deleted"):
            file_result = ckpt_result
        else:
            # 安全准则（v38）：无写盘记录且无 checkpoint 时严格跳过文件操作，
            # 绝对严禁隐式执行 `git checkout HEAD`（Git HEAD 往往落后多轮，会导致近期修改意外被抹杀）。
            file_result = {
                "ok": True,
                "skipped": True,
                "restored": 0,
                "deleted": 0,
                "failed": 0,
                "reason": "该 turn 无文件写入记录，无需恢复",
            }

    # v2.2 (plan-88): 回滚完成后清理本次回滚涉及快照的 checkpoint 文件（配置开启时）。
    # 备份已完成使命（精确回滚用写盘记录，checkpoint 仅兜底），删除磁盘文件并
    # 同步清空快照 file_list 登记，避免 .chatcoder/checkpoints 无限膨胀。
    _clear_ckpt_snap_ids: set[int] = set()
    if settings.checkpoint_cleanup_on_rollback:
        _deleted_ckpts = 0
        for s_row in later_snaps.values():
            for item in (s_row.file_list or []):
                if not isinstance(item, dict):
                    continue
                ckpt = item.get("ckpt") or ""
                if not ckpt:
                    continue
                cp = Path(ckpt)
                if not cp.is_absolute():
                    cp = _fs_path(workspace, ckpt)
                try:
                    if cp.exists():
                        cp.unlink()
                        _deleted_ckpts += 1
                except OSError:
                    pass
            if s_row.file_list:
                _clear_ckpt_snap_ids.add(s_row.id)
        if _deleted_ckpts:
            logger.info("[rollback] turn=%s 回滚后清理 checkpoint %d 个", turn_id, _deleted_ckpts)

    # 4. 消息软删：本 turn 及其之后的主线程消息，以及这些 turn 内的子代理线程消息。
    #    turn_id IS NULL 的历史消息不删除，避免误删非当前 turn 内容。
    res = await db.execute(
        select(Message).where(
            Message.session_id == snap.session_id,
            Message.deleted == False,  # noqa: E712
        )
    )
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
    user_attachments = None
    for m in msgs:
        if m.id == snap.user_message_id and restore_to_composer:
            # v2.2: 回滚撤销时同时回填文字与附件（图片等），前端可修改后直接重发
            user_text = str((m.content or {}).get("text", ""))
            user_attachments = (m.content or {}).get("attachments")
    soft_msg_ids = [m.id for m in msgs]
    sub_agent_ids = [a.id for a in sub_agents]

    # 4.1 子代理 Agent 记录置为 terminated（保留历史，明确已随 turn 回滚终止）。
    from app.orchestration.agent_events import broadcast
    for a in sub_agents:
        if a.status in ("running", "done", "failed"):
            await broadcast(snap.session_id, {
                "event": "agent.updated",
                "payload": {"agent_id": a.id, "status": "terminated"},
            })

    # 5. 状态标记 + 全部 DB 写入统一在一个写引擎事务内提交（原子性保持）
    from sqlalchemy import delete, update as _update
    from app.persistence.database import run_write_locked

    def _persist(s):
        from app.persistence.models.turn import Turn as _Turn
        if rollback_task_ids:
            s.execute(_update(Task).where(Task.id.in_(rollback_task_ids)).values(artifact_ids=[]))
            s.execute(_update(Artifact).where(Artifact.task_id.in_(rollback_task_ids)).values(task_id=None))
        if review_ids:
            s.execute(delete(FileReview).where(FileReview.id.in_(review_ids)))
        # 本 turn 及其之后的快照一并标记：之后的 turn 已随之撤销，不能再被单独回滚
        later_ids = [s_row.id for s_row in later_snaps.values()]
        if later_ids:
            s.execute(_update(TurnSnapshot).where(TurnSnapshot.id.in_(later_ids)).values(rolled_back=True))
        if _clear_ckpt_snap_ids:
            s.execute(_update(TurnSnapshot).where(TurnSnapshot.id.in_(list(_clear_ckpt_snap_ids))).values(file_list=[]))
        s.execute(delete(RollbackWrite).where(
            RollbackWrite.session_id == snap.session_id,
            RollbackWrite.turn_id >= turn_id,
        ))
        if soft_msg_ids:
            s.execute(_update(Message).where(Message.id.in_(soft_msg_ids)).values(deleted=True))
        if sub_agent_ids:
            s.execute(_update(Agent).where(Agent.id.in_(sub_agent_ids)).values(status="terminated"))
        s.execute(_update(_Turn).where(
            _Turn.session_id == snap.session_id, _Turn.id >= turn_id,
        ).values(status="rolled_back"))
        s.commit()

    await run_write_locked(_persist, label=f"rollback.persist.{turn_id}")

    # plan-1094: 广播回滚完成——前端据此清除该 turn 及其之后的待审缓存。
    await broadcast(snap.session_id, {
        "event": "turn.rolled_back",
        "payload": {"turn_id": turn_id, "session_id": snap.session_id},
    })

    # 6. 审计
    from app.services import audit_service
    await audit_service.log(db, action="rollback", session_id=snap.session_id,
                            turn_id=turn_id, detail={"msgs": len(msgs), "file": file_result})

    return {
        "ok": True,
        "turn_id": turn_id,
        "rolled_back_msgs": len(msgs),
        "file_recovery": file_result,
        "user_message": user_text if restore_to_composer else None,
        "user_attachments": user_attachments if restore_to_composer else None,
    }


def _conflict_result(turn_id: int, plan: RollbackPlan) -> dict:
    conflicts = _conflict_list(plan)
    return {
        "ok": False,
        "code": "conflict",
        "turn_id": turn_id,
        "conflicts": conflicts,
        "reason": f"回滚已取消：{len(conflicts)} 个文件在这些轮次之后被改动过，为避免覆盖，未修改任何文件",
    }


async def _snapshots_from(db: AsyncSession, session_id: int, turn_id: int) -> dict[int, TurnSnapshot]:
    """本 turn 及其之后的快照（turn_id -> 快照），用于按记录所属 turn 查找二进制备份。"""
    res = await db.execute(
        select(TurnSnapshot).where(TurnSnapshot.session_id == session_id, TurnSnapshot.turn_id >= turn_id)
    )
    return {s.turn_id: s for s in res.scalars().all()}


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
            Task.status.in_(["proposed", "pending", "running"]),
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
    """从工具参数解析目标文件路径（agent_loop 与回滚共用）。

    plan-89-387: 补齐路径键名别名（file_path/filepath）——此前只认 args.path，
    模型用别名传参时不落写盘记录、不生成 change_stat，表现为「工具卡没有 +N -M、
    展开看不到变更」（executor / approval_policy 早已识别这些别名）。
    """
    args = args or {}
    if tool == "multi_file_edit":
        paths: list[str] = []
        edits = args.get("edits")
        if isinstance(edits, list):
            for e in edits:
                if not isinstance(e, dict):
                    continue
                p = e.get("path") or e.get("file_path") or e.get("filepath")
                if isinstance(p, str) and p.strip():
                    paths.append(p.strip())
        return paths
    for key in ("path", "file_path", "filepath"):
        p = args.get(key)
        if isinstance(p, str) and p.strip():
            return [p.strip()]
    return []


# ── 工具伪装兜底（v25）：从 shell 命令解析候选写盘路径 ──
# 模型常把文件写入伪装成 terminal_exec 命令（重定向 / Set-Content / Out-File /
# python open(...,'w') / node writeFileSync 等），这些调用不在 _WRITE_TOOLS 白名单里，
# 原逻辑完全不留痕，前端展开看不到文件变更。这里从命令文本解析候选路径，
# agent_loop 在执行前后对比文件内容，检测真实变更并登记回滚记录。

_PATH_TOKEN = r'(?:"(?:\\.|[^"])*"|\'(?:\\.|[^\'])*\'|[^\s"\'<>&|;=,]+)'


def _unquote_token(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


def _redirect_targets(command: str) -> list[str]:
    """重定向写盘：> file / >> file / 1> file / 2> file（排除 >&1 等 fd 引用与空设备）。"""
    out: list[str] = []
    for m in _re.finditer(r'(?<![<>=-])>+[\s]*(?P<p>' + _PATH_TOKEN + r')', command, _re.I):
        p = _unquote_token(m.group("p"))
        if p and p.lower() not in ("nul", "con", "/dev/null"):
            out.append(p)
    return out


_CMDLET_PATH_PARAM_RE = _re.compile(r'(?<!\w)-(?:Path|FilePath|Destination|LiteralPath)\s+(?P<p>' + _PATH_TOKEN + r')', _re.I)
_CMDLET_VALUE_RE = _re.compile(r'\s-(?:Value|Body|InputObject|Content|ItemType|Force|Encoding)\b', _re.I)
_CMDLET_RE = _re.compile(
    r'\b(?P<cmd>Set-Content|Add-Content|Out-File|Clear-Content|Set-Item|New-Item|Copy-Item|Move-Item)\b(?P<seg>[^\r\n;|]*)',
    _re.I,
)


def _cmdlet_targets(command: str) -> list[str]:
    """PowerShell 写盘 cmdlet：优先 -Path/-FilePath 参数，缺省取首个非 -flag 位置参数。"""
    out: list[str] = []
    for m in _CMDLET_RE.finditer(command):
        seg = m.group("seg")
        pm = _CMDLET_PATH_PARAM_RE.search(seg)
        if pm:
            out.append(_unquote_token(pm.group("p")))
            continue
        # 无 -Path 参数：跳过 -Value/-Body 等值参数，取第一个非 flag token（Set-Content f.txt -Value x）
        seg = _CMDLET_VALUE_RE.split(seg, maxsplit=1)[0]
        for t in seg.split():
            if t.startswith("-") or t.startswith("=") or t in ("|", "&&", ";"):
                continue
            p = _unquote_token(t)
            if p:
                out.append(p)
                break
    return out


def _posix_targets(command: str) -> list[str]:
    """POSIX 写命令：tee/touch 目标、curl -o/--output、wget -O、sed -i（跳过 -flag 参数）。"""
    out: list[str] = []
    # tee / touch：首参数可能带 -a/-t 等 flag，跳过
    for m in _re.finditer(r'\b(?P<cmd>tee|touch)\b(?P<seg>[^\r\n;|&]*)', command, _re.I):
        for t in m.group("seg").split():
            if t.startswith("-"):
                continue
            p = _unquote_token(t)
            if p:
                out.append(p)
                break
    # curl -o file / --output file
    for m in _re.finditer(r'\bcurl\b[^\r\n]*?(?<!\w)(?:-o|--output)\s+(?P<p>' + _PATH_TOKEN + r')', command, _re.I):
        out.append(_unquote_token(m.group("p")))
    # wget -O file
    for m in _re.finditer(r'\bwget\b[^\r\n]*?(?<!\w)-O\s+(?P<p>' + _PATH_TOKEN + r')', command, _re.I):
        out.append(_unquote_token(m.group("p")))
    # sed -i ... file（最后的位置参数为被改写文件）
    for m in _re.finditer(r'\bsed\s+-i\b(?P<seg>[^\r\n;|&]*)', command, _re.I):
        toks = [t for t in m.group("seg").split() if not t.startswith("-")]
        if toks:
            out.append(_unquote_token(toks[-1]))
    return out


def _inline_targets(command: str) -> list[str]:
    """内联脚本写文件：python open(..., 'w') / node writeFileSync(...)。"""
    out: list[str] = []
    for m in _re.finditer(r'\bopen\((?P<q>["\'])(?P<p>.*?)(?P=q)\s*,\s*["\']w', command, _re.I):
        out.append(m.group("p"))
    for m in _re.finditer(r'\bwriteFileSync\s*\(\s*["\'](?P<p>.*?)["\']', command, _re.I):
        out.append(m.group("p"))
    return out


def extract_shell_write_paths(command: str) -> list[str]:
    """从 shell 命令解析候选写盘路径（去引号、去重保序，不含工作区归一化）。

    覆盖常见"工具伪装"写法：重定向、PowerShell 写 cmdlet、POSIX 写命令、内联脚本。
    解析是启发式的：漏报只影响展示/回滚记录的完整性，不影响工具执行本身。
    """
    raw = _redirect_targets(command) + _cmdlet_targets(command) + _posix_targets(command) + _inline_targets(command)
    seen: set[str] = set()
    out: list[str] = []
    for p in raw:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ── 删除 / 移动类命令（plan-20261019-toolcard-v2）──
# 此前只识别「写」：模型用 rm / del / Remove-Item 清理临时文件时没有任何记录，
# 于是本轮新建的文件仍留在变更清单里（磁盘上却已经没有），之前就存在、被本轮删除的文件
# 则根本不会出现在清单里。这里补齐删除与「移走源文件」两类目标；识别是启发式的，
# 真正是否发生了变更仍由 agent_loop 对比执行前后的文件内容决定，识别多了不会误记。

_DELETE_WORDS = frozenset({"rm", "del", "erase", "rd", "rmdir", "unlink", "ri", "remove-item"})
_MOVE_WORDS = frozenset({"mv", "move", "move-item", "mi"})
_CD_WORDS = frozenset({"cd", "chdir", "pushd", "sl", "set-location"})
# 这些命令带 Windows 风格的 /s /q /f 开关（单字母），不能当成路径
_WIN_FLAG_WORDS = frozenset({"del", "erase", "rd", "rmdir", "move"})
_PS_VALUE_PARAMS = frozenset({"-include", "-exclude", "-filter", "-stream", "-credential"})
_PS_PATH_PARAMS = frozenset({"-path", "-literalpath"})
_TOKEN_RE = _re.compile(r'"((?:\\.|[^"\\])*)"|\'([^\']*)\'|(\S+)')
_REDIRECT_TOKEN_RE = _re.compile(r'^\d*[<>]')
_INLINE_DELETE_RES = (
    _re.compile(r'\bos\.(?:remove|unlink|rmdir)\(\s*[rRuUbBfF]*(?P<q>["\'])(?P<p>.*?)(?P=q)'),
    _re.compile(r'\bshutil\.rmtree\(\s*[rRuUbBfF]*(?P<q>["\'])(?P<p>.*?)(?P=q)'),
    _re.compile(r'\bPath\(\s*[rRuUbBfF]*(?P<q>["\'])(?P<p>.*?)(?P=q)\s*\)\.unlink'),
    _re.compile(r'\bfs\.(?:unlinkSync|rmSync|rmdirSync|unlink|rm|rmdir)\(\s*(?P<q>["\'`])(?P<p>.*?)(?P=q)'),
    # require('fs').unlinkSync('x') 这种不经变量的链式写法
    _re.compile(r'require\(\s*["\'](?:node:)?fs["\']\s*\)\s*\.\s*(?:unlinkSync|rmSync|rmdirSync|unlink|rm|rmdir)'
                r'\(\s*(?P<q>["\'`])(?P<p>.*?)(?P=q)'),
)
# 删除目录时最多展开多少个文件、哪些目录永不展开（巨型目录无法逐文件记录，也恢复不了）
_DIR_EXPAND_MAX_FILES = 300
_DIR_EXPAND_SKIP = frozenset({".git", "node_modules", ".chatcoder", "__pycache__", ".venv", "venv"})


def _split_statements(command: str) -> list[str]:
    """按 ; | & 与换行切分语句（引号内的分隔符不切）。"""
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in command:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in (";", "|", "&", "\n", "\r"):
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def _tokenize(stmt: str) -> list[str]:
    """按空白切词（引号内保持整体并去掉外层引号，不做反斜杠转义，Windows 路径原样保留）。"""
    out: list[str] = []
    for m in _TOKEN_RE.finditer(stmt):
        tok = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
        if tok:
            out.append(tok)
    return out


def _split_commas(token: str) -> list[str]:
    """PowerShell 的 a.txt,b.txt 列表写法。"""
    return [t.strip() for t in token.split(",") if t.strip()]


def _positional_args(args: list[str], *, win_flags: bool) -> list[str]:
    """从参数里取出路径：跳过 -flag / -Recurse / 值参数，认 -Path / -LiteralPath；`--` 之后全是路径。"""
    out: list[str] = []
    only_pos = False
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if only_pos:
            out.extend(_split_commas(a))
        elif a == "--":
            only_pos = True
        elif _REDIRECT_TOKEN_RE.match(a):
            pass  # 2>&1 之类的重定向残片
        elif low in _PS_PATH_PARAMS and i + 1 < len(args):
            out.extend(_split_commas(args[i + 1]))
            i += 1
        elif low in _PS_VALUE_PARAMS:
            i += 1  # 连同它的值一起跳过
        elif a.startswith("-") and len(a) > 1:
            pass
        elif win_flags and _re.fullmatch(r"/[A-Za-z]", a):
            pass
        else:
            out.extend(_split_commas(a))
        i += 1
    return out


def _command_head(token: str) -> str:
    """命令词归一：小写、去目录前缀（/bin/rm、C:\\Windows\\System32\\cmd.exe 之类）与 .exe 后缀。"""
    head = token.lower().replace("\\", "/").rsplit("/", 1)[-1]
    return head[:-4] if head.endswith(".exe") else head


def _delete_and_move_targets(command: str) -> tuple[list[str], list[str]]:
    """(被删除 / 被移走的源路径, 移动的目标路径)。含 rm / del / Remove-Item / git rm / mv / Move-Item。"""
    dels: list[str] = []
    dests: list[str] = []
    for stmt in _split_statements(command):
        toks = _tokenize(stmt)
        if not toks:
            continue
        head = _command_head(toks[0])
        args = toks[1:]
        if head == "git" and args and args[0].lower() == "rm":
            if "--cached" in args:
                continue  # 只移出索引，工作区文件还在
            dels.extend(_positional_args(args[1:], win_flags=False))
        elif head in _DELETE_WORDS:
            dels.extend(_positional_args(args, win_flags=head in _WIN_FLAG_WORDS or head in ("remove-item", "ri")))
        elif head in _MOVE_WORDS:
            pos = _positional_args(args, win_flags=True)
            if len(pos) >= 2:
                dels.extend(pos[:-1])
                dests.append(pos[-1])
                # 目标是已存在的目录时，文件落在「目录/原文件名」下
                for src in pos[:-1]:
                    name = src.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                    if name:
                        dests.append(pos[-1].rstrip("/\\") + "/" + name)
    return dels, dests


def _inline_delete_targets(command: str) -> list[str]:
    """内联脚本删文件：python os.remove / shutil.rmtree / Path.unlink、node fs.unlinkSync / rmSync。"""
    out: list[str] = []
    for rx in _INLINE_DELETE_RES:
        for m in rx.finditer(command):
            p = m.group("p")
            if p:
                out.append(p)
    return out


def extract_shell_delete_paths(command: str) -> list[str]:
    """从 shell 命令解析候选「被删除 / 被移走」的路径（去重保序，可能含通配符与目录，未做工作区归一化）。"""
    dels, _ = _delete_and_move_targets(command)
    seen: set[str] = set()
    out: list[str] = []
    for p in dels + _inline_delete_targets(command):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def extract_shell_move_dests(command: str) -> list[str]:
    """从 mv / Move-Item 解析移动的目标路径（新文件在这里出现）。"""
    _, dests = _delete_and_move_targets(command)
    return list(dict.fromkeys(p for p in dests if p))


def _cd_dirs(command: str) -> list[str]:
    """命令里出现过的 `cd 目录`——后续语句的相对路径可能相对于它们，候选里一并带上。"""
    out: list[str] = []
    for stmt in _split_statements(command):
        toks = _tokenize(stmt)
        if toks and _command_head(toks[0]) in _CD_WORDS:
            pos = [t for t in _positional_args(toks[1:], win_flags=True) if t not in ("-", "~")]
            if pos:
                out.append(pos[0])
    return list(dict.fromkeys(out))


def resolve_command_write_paths(tool_name: str, args: dict, workspace: str) -> list[str]:
    """非白名单写盘工具（terminal_exec 等）的候选变更路径（相对工作区，越界剔除）。

    plan-20261019-toolcard-v2：除「写」之外，还包含被删除 / 被移走的源路径、移动目标，
    通配符会就地展开（最多 300 个）；目录原样返回，由 expand_directory_targets 在执行前展开成文件。
    """
    if tool_name != "terminal_exec":
        return []
    command = str((args or {}).get("command") or "")
    if not command.strip():
        return []
    if not workspace:
        return []
    root = Path(workspace).resolve()
    cwd = str((args or {}).get("cwd") or "").strip()
    base = (root / cwd) if cwd else root
    # 候选基准目录：命令自身的 cwd，加上命令里 `cd x` 指向的目录（相对 cwd，越界的在下面被剔除）
    bases = [base]
    for d in _cd_dirs(command):
        try:
            dp = Path(d)
            bases.append(dp if dp.is_absolute() else (base / dp))
        except (OSError, ValueError):
            continue
    raws = (extract_shell_write_paths(command) + extract_shell_delete_paths(command)
            + extract_shell_move_dests(command))
    seen: set[str] = set()
    out: list[str] = []

    def _add(full: Path) -> None:
        try:
            rel = full.resolve().relative_to(root)
        except (OSError, ValueError):
            return
        rel_s = rel.as_posix()
        if rel_s and rel_s != "." and rel_s not in seen:
            seen.add(rel_s)
            out.append(rel_s)

    for raw in raws:
        if not raw:
            continue
        has_glob = any(c in raw for c in "*?[")
        for b in bases:
            try:
                p = Path(raw)
                if has_glob:
                    pattern = str(p if p.is_absolute() else (b / p))
                    for hit in _glob.glob(pattern, recursive=True)[:_DIR_EXPAND_MAX_FILES]:
                        _add(Path(hit))
                else:
                    _add(p if p.is_absolute() else (b / p))
            except (OSError, ValueError):
                continue
    return out


def expand_directory_targets(workspace: str, rels: list[str], max_files: int = _DIR_EXPAND_MAX_FILES) -> list[str]:
    """把候选路径里的目录展开成其中的文件（相对工作区）。必须在命令执行**之前**调用——
    执行后被删除的目录已经不存在，没法再列出文件。

    超过 max_files 个文件的目录整体不记录（逐文件记录与恢复都不现实）；.git / node_modules 等永不展开。
    """
    out: list[str] = []
    seen: set[str] = set()
    root = Path(workspace)
    for rel in rels:
        p = root / rel
        try:
            is_dir = p.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
            continue
        if any(part in _DIR_EXPAND_SKIP for part in Path(rel).parts):
            continue
        found: list[str] = []
        too_many = False
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in _DIR_EXPAND_SKIP]
            for fn in filenames:
                found.append((Path(dirpath) / fn).relative_to(root).as_posix())
                if len(found) > max_files:
                    too_many = True
                    break
            if too_many:
                break
        if too_many:
            logger.info("[rollback] 目录 %s 文件过多（>%d），删除不逐个记录", rel, max_files)
            continue
        for f in found:
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


# ── 写盘操作类型与内容指纹（plan-20261019-toolcard-v2）──
#
# 变更清单与撤销都要回答两个问题：「这个文件在本轮开始前存在吗」「本轮结束时还在吗」。
# 文本记录靠 old_content / new_content 是否为 None 勉强能答，但：
#  · 二进制 / 超限文本不存全文，新建与删除无从区分；
#  · 「新建 → 删除」序列里，删除那条的 old_content 有值，旧逻辑据此把「本轮新建的文件」误判成「之前就存在」。
# 因此每条新记录显式带上 op（create / modify / delete），并附写后字节指纹 after_hash，
# 供撤销 / 回滚前精确判断「本轮之后文件有没有被改过」。旧记录没有这两个字段，按内容推断（见 rec_*）。

OP_CREATE = "create"
OP_MODIFY = "modify"
OP_DELETE = "delete"
# 超过此大小不计算指纹（读全文太贵）：严格校验对这类文件退回文本比较 / 跳过
_DIGEST_MAX_BYTES = 64 * 1024 * 1024
# 写前备份的单文件上限：更大的文件不复制（删除后无法恢复，宁可不记录也不拖慢命令）
_CKPT_MAX_BYTES = 128 * 1024 * 1024


def path_exists(workspace: str, rel: str) -> bool:
    """工作区内该路径是否是一个存在的文件（目录不算）。"""
    try:
        return _fs_path(workspace, rel).is_file()
    except OSError:
        return False


def file_digest(workspace: str, rel: str) -> str | None:
    """文件字节的 sha1；文件不存在 / 过大 / 读取失败返回 None。"""
    try:
        p = _fs_path(workspace, rel)
        if not p.is_file() or p.stat().st_size > _DIGEST_MAX_BYTES:
            return None
        h = hashlib.sha1()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def write_op(existed_before: bool, exists_after: bool) -> str | None:
    """由写前 / 写后是否存在推出操作类型；前后都不存在（没有发生任何事）返回 None。"""
    if exists_after:
        return OP_MODIFY if existed_before else OP_CREATE
    return OP_DELETE if existed_before else None


def rec_existed_before(rec: Any) -> bool:
    """这条写盘记录写之前，文件是否存在。旧记录（无 op）按内容推断。"""
    op = getattr(rec, "op", None)
    if op == OP_CREATE:
        return False
    if op in (OP_MODIFY, OP_DELETE):
        return True
    if rec.old_content is not None or getattr(rec, "diff_before", None):
        return True
    # 旧的二进制记录不存任何内容，无从判断：按「已存在」处理，沿用此前的「修改」口径
    return bool(getattr(rec, "binary", False))


def rec_exists_after(rec: Any) -> bool | None:
    """这条写盘记录写完之后，文件是否还在。None = 旧记录没存写后内容，调用方需读磁盘兜底。"""
    op = getattr(rec, "op", None)
    if op in (OP_CREATE, OP_MODIFY):
        return True
    if op == OP_DELETE:
        return False
    if rec.new_content is not None or getattr(rec, "diff_after", None):
        return True
    if getattr(rec, "binary", False):
        return True
    # 写前有内容、写后内容为空：这一次写盘把文件删掉了（plan-20261018-turn-changes 的口径）
    return False if rec.old_content is not None else None


def discard_checkpoint(ckpt_path: str | None) -> None:
    """删除一份没有被登记的写前备份（命令并未真正改动该文件时，避免备份目录里堆满孤儿文件）。"""
    if not ckpt_path:
        return
    try:
        Path(ckpt_path).unlink(missing_ok=True)
    except OSError:
        pass


async def record_turn_write(db: AsyncSession, *, session_id: int, turn_id: int,
                            tool: str, path: str, before: str | None, after: str | None,
                            binary: bool = False, call_key: str | None = None,
                            diff_before: str | None = None,
                            diff_after: str | None = None,
                            op: str | None = None,
                            after_hash: str | None = None) -> None:
    """记录一次写盘操作的前后内容（精确回滚依据）。失败不阻塞（非关键路径）。

    binary=True 表示二进制/超限文件：不存文本前后内容，回滚走 checkpoint 备份恢复。
    plan-20261019-diff: diff_before/diff_after 存「展示窗口」——超过记录上限的文本
    仍能生成 UI diff 与 +N -M，避免大 md 文档只显示「二进制/大文件」。
    plan-89-387: call_key 关联发起该次写盘的工具调用——同一 turn 内同文件多次编辑时，
    前端据此精确取「这一次编辑」的前后内容（见 get_file_diff）。
    plan-20261019-toolcard-v2: op 记录这次写盘是新建 / 修改 / 删除，after_hash 为写后字节指纹，
    撤销与回滚据此严格校验「本轮之后文件有没有被改过」。
    v975：经 WriteEngine 单写线程（写事务不阻塞事件循环、无锁单写者）。
    """
    try:
        from app.persistence.database import run_write_locked

        def _persist(s):
            from app.persistence.models.rollback import RollbackWrite
            s.add(RollbackWrite(session_id=session_id, turn_id=turn_id, tool=tool,
                                path=path, old_content=before, new_content=after,
                                binary=binary, call_key=call_key,
                                diff_before=diff_before, diff_after=diff_after,
                                op=op, after_hash=after_hash))
            s.commit()

        await run_write_locked(_persist, label=f"rollback.write.{turn_id}")
    except Exception:
        # plan-20261017: 写盘记录是变更面板的数据源；失败会让前端展开时 404。
        # run_write_locked 自带重试（默认 2 次），这里至少留下 warning 便于定位，
        # 不再用 debug 静默吞掉。
        logger.warning("[rollback] 写盘记录失败（变更面板将无该文件 diff）: %s %s",
                       tool, path, exc_info=True)


async def reconcile_turn_writes(db: AsyncSession, *, session_id: int, turn_id: int, workspace: str) -> int:
    """轮次收尾对账：核对「本轮已记录过的文件」在磁盘上的现状，是否与最后一条记录写完的样子一致。

    plan-20261019-toolcard-v2：命令里的删除 / 改写不一定都能从命令文本里解析出来
    （脚本里 os.remove、构建脚本清理产物、git checkout 还原……）。这类变更发生在已被记录过的文件上时，
    清单会停在旧状态——例如本轮新建的临时文件被脚本删掉，清单里却还显示「+41 新增」。
    收尾时补一条 turn_sync 记录，让清单与撤销看到本轮的真实结果：
      · 记录说文件在、磁盘上没有了 → 补「删除」；
      · 记录说文件已删、磁盘上又有了 → 补「新建」；
      · 内容与记录不同（按写后字节指纹，旧记录退回文本比较）→ 补「修改」。
    只处理本轮已记录过的路径，不做全盘扫描。返回补记条数；非关键路径，任何异常只记日志。
    """
    if not workspace:
        return 0
    try:
        res = await db.execute(
            select(RollbackWrite).where(
                RollbackWrite.session_id == session_id,
                RollbackWrite.turn_id == turn_id,
            ).order_by(RollbackWrite.id.asc())
        )
        by_path: dict[str, list[RollbackWrite]] = {}
        for w in res.scalars().all():
            by_path.setdefault(w.path, []).append(w)
        added = 0
        for path, recs in by_path.items():
            if not _is_safe_rel_path(path):
                continue
            last = recs[-1]
            exists_now = path_exists(workspace, path)
            expect_after = rec_exists_after(last)
            if expect_after is False:
                if not exists_now:
                    continue
                op = OP_CREATE
            elif not exists_now:
                op = OP_DELETE
            else:
                expected = getattr(last, "after_hash", None)
                digest = file_digest(workspace, path)
                if expected:
                    if digest is None or digest == expected:
                        continue
                else:
                    # 旧记录没有指纹：文本记录退回文本比较，二进制无从对账
                    if last.binary or last.new_content is None:
                        continue
                    cur_text = _read_file_text(workspace, path)
                    if cur_text is None or cur_text == last.new_content:
                        continue
                op = OP_MODIFY
            as_binary = bool(last.binary)
            after_text: str | None = None
            if exists_now:
                kind, _size = inspect_write_target(workspace, path, settings.rollback_record_max_bytes)
                as_binary = as_binary or kind != FILE_KIND_TEXT
                if not as_binary:
                    after_text = _read_file_text(workspace, path)
                    if after_text is None:
                        as_binary = True  # 读不出文本：按二进制记录，撤销走备份
            await record_turn_write(
                db, session_id=session_id, turn_id=turn_id, tool="turn_sync", path=path,
                before=None if as_binary else last.new_content,
                after=None if as_binary else after_text,
                binary=as_binary, op=op,
                after_hash=file_digest(workspace, path) if exists_now else None,
            )
            added += 1
        if added:
            logger.info("[rollback] 轮次收尾对账：turn=%s 补记 %d 条磁盘现状与记录不一致的变更", turn_id, added)
        return added
    except Exception:  # noqa: BLE001 — 对账只是兜底，绝不能影响轮次收尾
        logger.warning("[rollback] 轮次收尾对账失败（非阻塞）: turn=%s", turn_id, exc_info=True)
        return 0


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
# 单次响应渲染的行数上限（含上下文）：超出只返回前 N 行并标记 truncated，
# 统计仍是全量——避免超大 diff 把前端渲染拖垮。
_MAX_DIFF_LINES_RENDER = 4000
# 大文本启用 autojunk：避免重复空行过多时 SequenceMatcher 退化卡顿。
# 统计、取行、窗口三处共用同一匹配器，保证「摘要 +N -M」与展开的行完全一致。
_JUNK_THRESHOLD_LINES = 6000


def _matcher(a: list[str], b: list[str]) -> difflib.SequenceMatcher:
    return difflib.SequenceMatcher(None, a, b, autojunk=len(a) + len(b) > _JUNK_THRESHOLD_LINES)


def diff_stats(before: str | None, after: str | None) -> tuple[int, int]:
    """行级增删统计（SequenceMatcher opcodes，与 _diff_lines 同源）。

    旧实现走 unified_diff，和展开行用的 opcodes 是两套口径；遇到""空行/重复行时
    摘要与展开行数可能对不上。统一为同一匹配器后，摘要 +N -M 恒等于展开的 add/del 行数。
    """
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    if b == a:
        return 0, 0
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in _matcher(b, a).get_opcodes():
        if tag in ("insert", "replace"):
            additions += j2 - j1
        if tag in ("delete", "replace"):
            deletions += i2 - i1
    return additions, deletions


# 兼容旧调用名（agent_loop / 测试此前使用私有名）
_diff_stats = diff_stats


def _truncate_lines(text: str | None, limit: int = _MAX_DIFF_CHANGE_LINES) -> str | None:
    """按行截断长文本（保留足够上下文用于 diff 展示），返回 None 表示原本无内容。"""
    if text is None:
        return None
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + "\n…(内容过长，已截断)"


def _diff_lines(before: str | None, after: str | None, context: int = 3) -> list[dict]:
    """行级 diff（SequenceMatcher opcodes，与 _diff_stats / _line_change_stat 同源算法）。

    仅保留变更块与其前后各 context 行，返回 [{type, text, old_no, new_no}]：
    - type: "add"（新增）/ "del"（删除）/ "ctx"（上下文）
    - old_no / new_no 为 1-based 行号（add 无 old_no，del 无 new_no，ctx 两值相同）
    全量替换/新增/删除文件（a 或 b 为空）时同样适用。
    """
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    sm = _matcher(b, a)
    opcodes = sm.get_opcodes()
    n = len(opcodes)
    out: list[dict] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            continue
        # 前置上下文：取前一个 equal 块尾部 context 行
        if k > 0 and opcodes[k - 1][0] == "equal":
            _pi1, _pi2 = opcodes[k - 1][1], opcodes[k - 1][2]
            start = max(_pi1, _pi2 - context)
            for t in range(start, _pi2):
                out.append({"type": "ctx", "text": b[t], "old_no": t + 1, "new_no": t + 1})
        # 变更行
        if tag == "replace":
            for t in range(i1, i2):
                out.append({"type": "del", "text": b[t], "old_no": t + 1, "new_no": None})
            for t in range(j1, j2):
                out.append({"type": "add", "text": a[t], "old_no": None, "new_no": t + 1})
        elif tag == "delete":
            for t in range(i1, i2):
                out.append({"type": "del", "text": b[t], "old_no": t + 1, "new_no": None})
        elif tag == "insert":
            for t in range(j1, j2):
                out.append({"type": "add", "text": a[t], "old_no": None, "new_no": t + 1})
        # 后置上下文：取后一个 equal 块头部 context 行
        if k + 1 < n and opcodes[k + 1][0] == "equal":
            _ni1, _ni2 = opcodes[k + 1][1], opcodes[k + 1][2]
            end = min(_ni2, _ni1 + context)
            for t in range(_ni1, end):
                out.append({"type": "ctx", "text": b[t], "old_no": t + 1, "new_no": t + 1})
    return out


def _truncate_window(before: str | None, after: str | None, context: int = 50) -> tuple[str | None, str | None]:
    """按变更窗口截断 before/after（覆盖全部变更区，前后各 context 行）。

    用于大文本 diff 展示（Monaco DiffEditor 直接吃 before/after），避免整文件下发
    导致前端前端 simpleLineDiff 命中 1600 行阈值退化为全量 -/+。
    窗口覆盖全文件或前后相同（无变更）时返回原文本。
    """
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    if b == a:
        return before, after
    sm = _matcher(b, a)
    changes = [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    if not changes:
        return before, after
    b_lo = max(0, min(c[0] for c in changes) - context)
    b_hi = min(len(b), max(c[1] for c in changes) + context)
    a_lo = max(0, min(c[2] for c in changes) - context)
    a_hi = min(len(a), max(c[3] for c in changes) + context)
    # 完整覆盖 → 原样返回
    if b_lo <= 0 and b_hi >= len(b) and a_lo <= 0 and a_hi >= len(a):
        return before, after
    b_slice = "\n".join(b[b_lo:b_hi])
    a_slice = "\n".join(a[a_lo:a_hi])
    return b_slice, a_slice


def _cap_window_text(text: str | None) -> str | None:
    """窗口文本按字符上限截断（UI 只渲染有限内容，避免超大 diff 拖垮前端）。"""
    if text is None:
        return None
    if len(text) <= _MAX_DIFF_WINDOW_CHARS:
        return text
    return text[:_MAX_DIFF_WINDOW_CHARS] + "\n…(内容过长，已截断)"


def _head_tail_excerpt(text: str, max_chars: int = _MAX_DIFF_WINDOW_CHARS // 2) -> str:
    """超巨文本的降级摘录：保留头尾各一半，避免对超大内容做全量 diff。"""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n…(内容过长，已截断)\n" + text[-half:]


def build_diff_window(before: str | None, after: str | None) -> tuple[str | None, str | None]:
    """为超限文本生成 UI diff 展示窗口（写盘前后各自裁到变更区周边）。

    仅用于展示：全文不落库，回滚仍按写盘前 checkpoint 恢复。
    真二进制（前后都无文本）返回 (None, None)，调用方保留「二进制」提示。
    超巨文本（> _DIFF_TEXT_MAX_BYTES 字符）不做精确窗口定位，退化为头尾摘录，
    避免为展示对几十 MB 内容跑 SequenceMatcher 卡住事件循环。
    """
    if before is None and after is None:
        return None, None
    b_raw, a_raw = before or "", after or ""
    if b_raw == a_raw:
        return _cap_window_text(b_raw), _cap_window_text(a_raw)
    if len(b_raw) > _DIFF_TEXT_MAX_BYTES or len(a_raw) > _DIFF_TEXT_MAX_BYTES:
        return _cap_window_text(_head_tail_excerpt(b_raw)), _cap_window_text(_head_tail_excerpt(a_raw))
    b_disp, a_disp = _truncate_window(b_raw, a_raw, context=50)
    return _cap_window_text(b_disp), _cap_window_text(a_disp)


def read_diff_text(workspace: str, rel: str, limit: int = _DIFF_TEXT_MAX_BYTES) -> str | None:
    """读取用于展示的文本内容（超过 limit 字节不读，避免为展示耗内存）。"""
    return _read_file_text(workspace, rel, max_bytes=limit)


# ── 「本轮由未登记途径新建、又被删除」的文件（plan-20261020-born-in-turn）──
# 浏览器截图、构建脚本等产出的文件不经写盘工具，也解析不出写路径，所以本轮没有「新建」记录；
# 随后用命令把它们删掉时，删除记录会把它们当成「本轮之前就存在」——变更卡片出现「已删除」行、
# 撤销时又从删除前的备份把它们还原出来（本轮开始前根本没有这些文件）。
# 判据：文件（或其删除前备份）的创建 / 修改时间不早于本轮开始，且本轮没有更早的写盘记录。

_BORN_SLACK_S = 0.5


async def turn_started_epoch(db: AsyncSession, turn_id: int) -> float | None:
    """本轮开始时刻（epoch 秒）；取不到返回 None。"""
    from datetime import datetime, timezone
    from app.persistence.models.turn import Turn

    try:
        turn = await db.get(Turn, turn_id)
        raw = str(getattr(turn, "started_at", "") or "").strip()
        if not raw:
            return None
        at = datetime.fromisoformat(raw.replace(" ", "T"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at.timestamp()
    except (ValueError, TypeError, OSError):
        return None


def file_born_after(path: Path, since: float | None) -> bool:
    """文件是否创建于 since 之后（优先用创建时间；Windows 的 st_ctime 即创建时间）。

    取不到创建时间（非 Windows 且无 st_birthtime）时返回 False——宁可保留记录，也不误判。
    """
    if since is None:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    birth = getattr(st, "st_birthtime", None)
    if birth is None and os.name == "nt":
        birth = st.st_ctime
    return birth is not None and birth >= since - _BORN_SLACK_S


def file_mtime_after(path: Path, since: float | None) -> bool:
    """文件修改时间是否不早于 since（用于旧记录：删除前备份保留了源文件的修改时间）。"""
    if since is None:
        return False
    try:
        return path.stat().st_mtime >= since - _BORN_SLACK_S
    except OSError:
        return False


async def has_turn_write(db: AsyncSession, turn_id: int, path: str) -> bool:
    """本轮是否已有该路径的写盘记录。"""
    res = await db.execute(
        select(RollbackWrite.id).where(RollbackWrite.turn_id == turn_id, RollbackWrite.path == path).limit(1)
    )
    return res.first() is not None


async def unrecorded_birth_paths(db: AsyncSession, turn_id: int,
                                 by_path: dict[str, list[RollbackWrite]]) -> set[str]:
    """旧记录兜底：首条记录就是「删除」、且删除前备份的修改时间不早于本轮开始的路径。

    这类文件是本轮新建（未登记）又被删掉的，对本轮零净变更：不进变更清单，撤销时也不能还原。
    """
    cands = [p for p, recs in by_path.items()
             if recs and getattr(recs[0], "op", None) == OP_DELETE]
    if not cands:
        return set()
    snap = await _snapshot_for_turn(db, turn_id)
    t0 = await turn_started_epoch(db, turn_id)
    if snap is None or t0 is None:
        return set()
    out: set[str] = set()
    for p in cands:
        ckpt = _checkpoint_for_path(snap, p)
        if ckpt and file_mtime_after(Path(ckpt), t0):
            out.add(p)
    return out


async def list_turn_changes(db: AsyncSession, *, session_id: int, turn_id: int,
                            workspace: str) -> list[dict]:
    """聚合该 turn 的全部写盘记录为变更清单（不含文件全文）。

    主/子代理写盘同 turn_id，一并聚合；按 path 分组：
    - before = 首条写盘的 old_content，after = 本轮**末条**写盘记录的 new_content
      （plan-89-387：原先直接读当前磁盘，会把该轮之后的编辑并入本轮统计，
      表现为审核清单行数偏大；plan-20261018-turn-changes：末条写盘是删除时
      after 保持 None，只有老记录没有存写盘后内容才回退磁盘）
    - action：写前不存在 → added；本轮把文件删掉 → deleted；否则 modified；
      净效果为零（新建后又删除、改了又改回）的文件不进清单（见 _net_state）
    - difflib.unified_diff 统计增删行数（与 get_file_diff 同源）
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
    bogus = await unrecorded_birth_paths(db, turn_id, by_path)
    for path, recs in by_path.items():
        if path in bogus:
            continue  # 本轮新建（未登记）又被删除：零净变更
        net = _net_state(workspace, path, recs)
        if net is None:
            continue  # 净效果为零（本轮新建又被删除、改了又改回）：不展示
        additions, deletions = diff_stats(net.before, net.after)
        changes.append({
            "path": path,
            "action": net.action,
            "additions": additions,
            "deletions": deletions,
            "reviewed": reviewed.get(path, False),
        })
    changes.sort(key=lambda c: c["path"])
    return changes


def _action_of(existed_before: bool, exists_after: bool) -> str:
    return "added" if not existed_before else ("modified" if exists_after else "deleted")


@dataclass
class _NetState:
    """同一路径在一个 turn 内的「净变更」：写前是否存在、写后是否还在，以及用于统计 / 展示的前后内容。"""
    existed_before: bool
    exists_after: bool
    before: str | None
    after: str | None
    binary: bool
    has_window: bool

    @property
    def action(self) -> str:
        return _action_of(self.existed_before, self.exists_after)


def _net_state(workspace: str, path: str, recs: list[RollbackWrite]) -> _NetState | None:
    """把同一路径在本轮的全部写盘记录折成净变更；None = 净效果为零，不应出现在清单里。

    plan-20261019-toolcard-v2：此前用「任一记录的 old_content 非空」判定「写前是否存在」，
    「新建 → 删除」序列里删除那条的 old_content 有值，会把本轮新建的文件误判成「之前就存在」。
    现在只看**首条**记录的写前状态与**末条**记录的写后状态：
      · 写前不存在、写后也不在 → 零净变更（新建后又删除），不展示；
      · 写前存在、写后不在 → deleted；写前不存在、写后在 → added。
    """
    first, last = recs[0], recs[-1]
    binary = all(getattr(r, "binary", False) for r in recs)
    # plan-20261019-diff: 超限文本带展示窗口时按窗口统计，不再固定 +0 -0
    window_before = getattr(first, "diff_before", None)
    window_after = getattr(last, "diff_after", None)
    has_window = window_before is not None or window_after is not None
    existed_before = rec_existed_before(first)
    exists_after = rec_exists_after(last)
    if has_window:
        before, after = window_before, window_after
    else:
        before = first.old_content
        if last.new_content is not None:
            after = last.new_content
        elif exists_after is False:
            # 本轮最后一次写盘把文件删掉了。不能回退磁盘（后续轮次可能重建了同名文件），
            # 否则会被误判成 modified，且与 get_file_diff 的删除口径不一致。
            after = None
        else:
            # 旧记录没存写后内容，只能回退磁盘现状（唯一无法规避的兼容分支）
            after = _read_file_text(workspace, path)
    if exists_after is None:
        exists_after = after is not None
    if not existed_before and not exists_after:
        return None
    return _NetState(existed_before, exists_after, before, after, binary, has_window)


async def session_product_paths(db: AsyncSession, session_id: int, workspace: str) -> list[str]:
    """会话「产物」：本会话写过、且**现在仍然存在**的文件（按首次写入先后）。

    摘要面板的产物列以此为准，与变更卡片同一口径——被删除的文件（含本轮新建又删除的临时文件）
    不是产物。按路径取全会话最后一条记录判断写后是否还在；旧记录没存写后状态时读磁盘兜底。
    本轮新建（未登记）又被删除的文件（旧记录）同样排除。
    """
    res = await db.execute(
        select(RollbackWrite).where(RollbackWrite.session_id == session_id).order_by(RollbackWrite.id.asc())
    )
    by_turn_path: dict[int, dict[str, list[RollbackWrite]]] = {}
    last_of: dict[str, RollbackWrite] = {}
    order: list[str] = []
    for w in res.scalars().all():
        if w.path not in last_of:
            order.append(w.path)
        last_of[w.path] = w
        if w.turn_id is not None:
            by_turn_path.setdefault(int(w.turn_id), {}).setdefault(w.path, []).append(w)
    bogus: set[str] = set()
    for tid, grouped in by_turn_path.items():
        bogus |= await unrecorded_birth_paths(db, tid, grouped)
    out: list[str] = []
    for p in order:
        if p in bogus:
            continue
        alive = rec_exists_after(last_of[p])
        if alive is None:
            alive = path_exists(workspace, p)
        if alive:
            out.append(p)
    return out


async def visible_change_turns(db: AsyncSession, session_id: int) -> list[int]:
    """有「净变更」的轮次 id（升序）。撤销顺序锁据此判定——只留下零净变更记录的轮次
    （例如只新建又删除了临时文件）既不会出现变更卡片，也不应该拦住更早一轮的撤销。

    判定口径与 _net_state 一致（写前是否存在 / 写后是否还在），但只取「是否有内容」的轻量列，
    不把每条记录的前后全文载入内存——撤销锁在每轮结束、每次切会话时都会被查询。
    旧记录没存写后内容时 _net_state 会读磁盘兜底，这里保守地视为有变更。
    """
    res = await db.execute(
        select(
            RollbackWrite.turn_id, RollbackWrite.path, RollbackWrite.op, RollbackWrite.binary,
            RollbackWrite.old_content.is_not(None).label("has_old"),
            RollbackWrite.new_content.is_not(None).label("has_new"),
            RollbackWrite.diff_before.is_not(None).label("has_dbefore"),
            RollbackWrite.diff_after.is_not(None).label("has_dafter"),
        ).where(RollbackWrite.session_id == session_id).order_by(RollbackWrite.id.asc())
    )
    by_turn: dict[int, dict[str, list[Any]]] = {}
    for row in res.all():
        if row.turn_id is None:
            continue
        # 用占位字符串顶替内容（只关心「有没有」），复用 rec_existed_before / rec_exists_after 的判定
        light = SimpleNamespace(
            op=row.op, binary=bool(row.binary),
            old_content="x" if row.has_old else None, new_content="x" if row.has_new else None,
            diff_before="x" if row.has_dbefore else None, diff_after="x" if row.has_dafter else None,
        )
        by_turn.setdefault(int(row.turn_id), {}).setdefault(row.path, []).append(light)
    visible: list[int] = []
    for tid in sorted(by_turn):
        for recs in by_turn[tid].values():
            existed_before = rec_existed_before(recs[0])
            exists_after = rec_exists_after(recs[-1])
            if existed_before or exists_after is not False:
                visible.append(tid)
                break
    return visible


async def get_file_diff(db: AsyncSession, *, session_id: int, turn_id: int,
                        workspace: str, path: str, call_key: str | None = None) -> dict | None:
    """单文件 diff。

    plan-89-387: 传 call_key 时按「这一次写盘」返回——before/after 取该条记录自身的
    old_content / new_content（写盘后内容缺失时回退当前磁盘），同一轮内同一文件
    被多次编辑时，每次展开只展示本次变更，行数与工具卡 +N -M 同源一致。
    plan-20261018-turn-changes: 未传 call_key 时按「整轮口径」返回——
    before=该轮首条写盘前内容，after=该轮末条写盘后的内容（删除则为 None）。
    旧实现 after 读当前磁盘，会把该轮之后的编辑并入本轮，导致上一轮审查里
    出现更新轮次的改动；现按写盘记录边界取值，任意一轮都只展示自己的改动。

    返回 None 表示该 turn 无此文件写盘记录。
    兼容正反斜杠与工作区相对/绝对路径归一化匹配。
    """
    res = await db.execute(
        select(RollbackWrite).where(
            RollbackWrite.session_id == session_id,
            RollbackWrite.turn_id == turn_id,
        ).order_by(RollbackWrite.id.asc())
    )
    all_writes = list(res.scalars().all())
    if not all_writes:
        return None

    def _norm(p: str) -> str:
        s = (p or "").replace("\\", "/").strip()
        while s.startswith("./"):
            s = s[2:]
        return s.rstrip("/").casefold()

    target_norm = _norm(path)
    writes = [w for w in all_writes if _norm(w.path) == target_norm]
    if not writes and target_norm:
        # 兼容尾部子路径匹配（如相对路径匹配绝对路径记录）；带 / 边界，避免
        # 「IconButton.tsx」误配到「MyIconButton.tsx」这类同后缀不同文件。
        writes = [w for w in all_writes
                  if _norm(w.path).endswith("/" + target_norm)
                  or target_norm.endswith("/" + _norm(w.path))]
    if not writes and target_norm:
        # 最后兜底：文件名唯一时按 basename 匹配（工作区前缀/绝对相对写法不一致）。
        # 同名文件出现多处时不做猜测，避免展示错文件的 diff。
        base = target_norm.rsplit("/", 1)[-1]
        cands = [w for w in all_writes if _norm(w.path).rsplit("/", 1)[-1] == base]
        if len({_norm(w.path) for w in cands}) == 1:
            writes = cands
    if not writes:
        return None

    # plan-89-387: 单次编辑口径（命中该 call_key 的写盘记录）
    # plan-20261019-diff: 一次调用可能对同一文件产生多条记录（multi_file_edit 多处编辑），
    # 必须按整次调用合并：before=首条写盘前内容，after=末条写盘后内容。
    if call_key:
        hits = [w for w in writes if (getattr(w, "call_key", None) or "") == call_key]
        if hits:
            return _single_write_diff(workspace, path, hits)
        # 该文件记录都带 call_key 却都不匹配：说明消息上的 key 与记录不一致。
        # 回退整轮会把同文件其它编辑并进来（展开第一次看到第二次的改动），
        # 返回 None 让前端用调用参数兜底，保证每次展开只显示自己的改动。
        if all((getattr(w, "call_key", None) or "") for w in writes):
            return None

    # plan-20261019-toolcard-v2：整轮口径先折成净变更——新建后又删除、改了又改回的文件没有可展示的变更
    if await unrecorded_birth_paths(db, turn_id, {writes[0].path: writes}):
        return None
    net = _net_state(workspace, path, writes)
    if net is None:
        return None

    # plan-20261019-diff: 超限文本的展示窗口优先（全文不落库，回滚仍按 checkpoint）
    if net.has_window:
        return _build_diff_payload(
            path, net.before, net.after, single_edit=False, action=net.action,
            reason="文件较大，仅展示变更窗口；完整内容按写盘前备份恢复",
        )

    if writes[0].binary:
        # plan-1085: 历史污染轮降级——同 turn 同文件存在带内容的 bin=0 记录时，
        # 用其 old_content 作 before、当前磁盘作 after 返回文本 diff，避免瞬时
        # 读取失败误判导致整轮展示丢失；真二进制文件仍走原 reason 分支。
        _fb = next((w for w in writes if not w.binary and w.old_content is not None), None)
        _after_now = _read_file_text(workspace, path)
        if _fb is not None and _after_now is not None:
            return _build_diff_payload(
                path, _fb.old_content, _after_now, single_edit=False, action=net.action,
                reason="该轮部分编辑文件读取失败，已降级展示最近可用版本 diff",
            )
        # 二进制/大文件：不展示文本 diff，提示按写盘前备份恢复
        return _binary_diff_payload(path, single_edit=False, action=net.action)

    # plan-20261018-turn-changes: 无 call_key 时按「该轮最后一次写盘」的边界取 after
    # （由 _net_state 折算）。旧实现 after 直接读当前磁盘，会把该轮之后其它轮次对同一文件的编辑
    # 并入本轮 diff（表现为点上一轮的审查，内容里出现更新轮次的改动，且与卡片 +N -M 不一致）。
    return _build_diff_payload(path, net.before, net.after, single_edit=False, action=net.action)


def _binary_diff_payload(path: str, *, single_edit: bool, action: str | None = None) -> dict:
    """真二进制/无任何文本证据：不展示文本 diff。"""
    reason = "二进制/大文件，不展示文本 diff，回滚按写盘前备份恢复"
    if action == "deleted":
        reason = "二进制/大文件，本轮已删除，不展示文本 diff，撤销时按写盘前备份恢复"
    return {
        "path": path,
        "before": None,
        "after": None,
        "truncated": False,
        "reason": reason,
        "lines": None,
        "additions": 0,
        "deletions": 0,
        "single_edit": single_edit,
        "action": action,
    }


def _build_diff_payload(path: str, before: str | None, after: str | None, *,
                        single_edit: bool, reason: str | None = None,
                        action: str | None = None) -> dict:
    """统一生成 diff 响应：统计 / 行级 diff / 展示窗口同源，保证摘要与展开一致。

    action（added / modified / deleted）供前端区分「文件已被删除」——删除后没有「当前内容」可看。
    未显式给出时按前后内容是否为空推断。"""
    add, dele = diff_stats(before, after)
    lines = _diff_lines(before, after)
    truncated = (add + dele) > _MAX_DIFF_CHANGE_LINES
    if len(lines) > _MAX_DIFF_LINES_RENDER:
        lines = lines[:_MAX_DIFF_LINES_RENDER]
        truncated = True
    b_disp, a_disp = _truncate_window(before, after)
    if action is None:
        action = _action_of(before is not None, after is not None)
    return {
        "path": path,
        "before": b_disp,
        "after": a_disp,
        "truncated": truncated,
        "reason": reason,
        "lines": lines,
        "additions": add,
        "deletions": dele,
        "single_edit": single_edit,
        "action": action,
    }


def _single_write_diff(workspace: str, path: str, recs: list[RollbackWrite]) -> dict:
    """plan-89-387: 单次工具调用的「本次变更」diff。

    plan-20261019-diff: 同一次调用对同一文件的多次编辑（multi_file_edit）合并为
    一次变更：before=首条 old_content，after=末条 new_content，避免只取第一条丢失
    后续编辑；超限文本用 diff 窗口展示（全文按写盘前备份回滚）。
    """
    window_before = getattr(recs[0], "diff_before", None)
    window_after = getattr(recs[-1], "diff_after", None)
    existed_before = rec_existed_before(recs[0])
    exists_after = rec_exists_after(recs[-1])
    if window_before is not None or window_after is not None:
        return _build_diff_payload(
            path, window_before, window_after, single_edit=True,
            action=_action_of(existed_before, exists_after is not False),
            reason="文件较大，仅展示变更窗口；完整内容按写盘前备份恢复",
        )
    if all(getattr(r, "binary", False) for r in recs):
        return _binary_diff_payload(
            path, single_edit=True, action=_action_of(existed_before, exists_after is not False))
    before = recs[0].old_content
    after = recs[-1].new_content
    if after is None and exists_after is not False:
        # 写后内容缺失且并非「这次把文件删了」：旧记录兜底读磁盘
        after = _read_file_text(workspace, path)
    return _build_diff_payload(
        path, before, after, single_edit=True,
        action=_action_of(existed_before, after is not None))


async def reviewed_paths(db: AsyncSession, turn_id: int) -> dict[str, bool]:
    """查询该 turn 的全部审核记录（path -> reviewed）。"""
    res = await db.execute(select(FileReview).where(FileReview.turn_id == turn_id))
    return {r.path: bool(r.reviewed) for r in res.scalars().all()}


async def upsert_file_reviews(db: AsyncSession, *, turn_id: int,
                              paths: list[str], reviewed: bool) -> int:
    """幂等写入审核记录（turn_id+path 唯一键原子 upsert），返回变更条数。

    reviewed=False 时仅更新已存在记录为未审，不删除记录（保证状态可追溯）。
    必须用 ON CONFLICT 原子 upsert 而非先查后插：前端对同一 turn 的重复/并发
    请求会在"查不到→INSERT"窗口内双双判不存在，触发 uq_file_review_turn_path
    唯一约束冲突（server.log 2026-08-31 turn=631/632 实例）。
    """
    # 保序去重：批内重复路径会在同一条多行 INSERT 内自撞唯一键
    unique_paths = list(dict.fromkeys(
        p for p in (paths or []) if isinstance(p, str) and p.strip()
    ))
    if not unique_paths:
        return 0

    from app.persistence.database import run_write_locked

    def _persist(s):
        stmt = sqlite_insert(FileReview).values(
            [{"turn_id": turn_id, "path": p, "reviewed": reviewed} for p in unique_paths]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[FileReview.turn_id, FileReview.path],
            set_={"reviewed": stmt.excluded.reviewed},
            # reviewed 未变化的行不更新也不返回，保持"变更条数"语义与幂等性
            where=FileReview.reviewed != stmt.excluded.reviewed,
        ).returning(FileReview.id)
        rows = s.execute(stmt).all()  # 先消费结果再 commit（SQLite 不允许有未读结果时提交）
        s.commit()
        return len(rows)

    return await run_write_locked(_persist, label="review.upsert")


async def resolve_turn_workspace(db: AsyncSession, turn_id: int) -> tuple[int | None, str]:
    """解析 turn 所属会话与工作区路径（worktree 优先，回退项目路径）。

    返回 (session_id, workspace)。无快照时回退写盘记录反查（v26: 快照缺失/旧数据
    场景，只要该 turn 有写盘记录仍可展示变更清单）。
    """
    snap = await _snapshot_for_turn(db, turn_id)
    if snap is not None:
        from app.persistence.models.message import Session
        session = await db.get(Session, snap.session_id)
        workspace = session.worktree_path if session and session.worktree_path else None
        if workspace is None:
            from app.services.project_service import get_project
            if session and session.project_id:
                project = await get_project(db, session.project_id)
                workspace = project.path if project else None
        return snap.session_id, workspace or ""

    # v26: 无快照兜底——从写盘记录反查会话与工作区
    try:
        rec_res = await db.execute(
            select(RollbackWrite).where(RollbackWrite.turn_id == turn_id).limit(1)
        )
        rec = rec_res.scalars().first()
        if rec is None:
            return None, ""
        from app.persistence.models.message import Session
        session = await db.get(Session, rec.session_id)
        workspace = session.worktree_path if session and session.worktree_path else None
        if workspace is None:
            from app.services.project_service import get_project
            if session and session.project_id:
                project = await get_project(db, session.project_id)
                workspace = project.path if project else None
        return rec.session_id, workspace or ""
    except Exception:
        logger.debug("[rollback] 写盘记录反查工作区失败(非阻塞)", exc_info=True)
        return None, ""


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


def normalize_workspace_path(workspace: str, raw: str) -> str | None:
    """把写盘路径参数规范为工作区相对路径（绝对路径转相对；越界返回 None）。"""
    if not raw:
        return None
    try:
        root = Path(workspace).resolve()
        p = Path(raw)
        full = p.resolve() if p.is_absolute() else (root / p).resolve()
        rel = full.relative_to(root)
    except (OSError, ValueError):
        return None
    return rel.as_posix()


def _read_file_text(workspace: str, rel: str, max_bytes: int = 0) -> str | None:
    """读取文本文件内容。

    max_bytes>0 且文件超过该大小时返回 None（调用方视为超限走 checkpoint 兜底）。
    plan-1085: 读取 OSError（杀软/EDR 瞬时锁）短重试退避后再放弃，
    减少瞬时读取失败导致的 diff/统计丢失。
    """
    try:
        p = _fs_path(workspace, rel)
        if p.exists() and p.is_file():
            if max_bytes > 0 and p.stat().st_size > max_bytes:
                return None
        else:
            return None
    except OSError:
        return None
    for attempt in range(_BINARY_SNIFF_RETRIES):
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            if attempt + 1 < _BINARY_SNIFF_RETRIES:
                _time.sleep(_BINARY_SNIFF_RETRY_SEC)
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

    # AI 映射：theirs 行 -> base 行（为 AI 保留的行）；AI 新增行的 base 锚点
    t2b: dict[int, int] = {}
    ai_new_anchor: dict[int, int] = {}  # AI 新增行: theirs idx -> base 锚点。
    ai_del_base: set[int] = set()       # 为 AI 纯删除的 base 行（回滚恢复用）
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


# ── 回滚规划与原子应用（plan-20261017-goal12 项 3）─────────────────────────────
#
# 旧实现逐条记录「读盘 → 三路合并 → 写盘」，问题有三：
#   1. 同一文件被多个 turn 连续改写时，预览按记录各自读取当前盘面，前后步骤互相看不见，
#      会把可安全回滚的文件误判为冲突；
#   2. 写盘逐个进行，中途失败会留下半回滚的工作区，且消息已软删、快照已标记；
#   3. 文本读写用 errors=replace + 平台换行，GBK 文件、LF 文件经回滚会被改坏。
# 现在：先在内存里按「倒序撤销」模拟整段写盘序列（同一文件的中间状态逐步推进），得到每个文件的
# 最终内容与冲突清单；预览与真正回滚共用这一份规划；应用时备份原始字节、任一步失败则整体还原。

@dataclass
class _Step:
    """一条写盘记录在回滚序列中的结果（未落盘）。"""
    path: str
    turn_id: int
    action: str  # "restore" | "delete" | "noop"
    conflict: bool = False
    reason: str | None = None


@dataclass
class _FinalOp:
    """某文件的最终改写动作（只记录终态，同一文件多步只落一次）。"""
    kind: str  # "write" | "delete" | "ckpt"
    text: str | None = None          # write：LF 归一后的最终文本
    tf: Any = None                   # write：原文件的编码 / BOM / 换行（text_edit.TextFile），写回时沿用
    ckpt: str | None = None          # ckpt：二进制文件的写盘前备份路径


@dataclass
class RollbackPlan:
    workspace: str
    steps: list[_Step] = field(default_factory=list)
    finals: dict[str, _FinalOp] = field(default_factory=dict)
    # path -> 规划时磁盘上的当前文本（预览的 before；不存在为 None）
    disk: dict[str, str | None] = field(default_factory=dict)

    @property
    def conflicts(self) -> list[_Step]:
        return [s for s in self.steps if s.conflict]


_BIN_STATE = object()  # 虚拟盘面：该文件已被二进制备份恢复，后续文本步骤无法安全合并


def _text_codec():
    """延迟导入编解码工具：tools 包的 __init__ 会引入执行器，顶层导入有循环依赖风险。"""
    from app.orchestration.tools import text_edit
    return text_edit


def _read_disk_text(workspace: str, rel: str) -> tuple[str | None, Any]:
    """读取工作区文件为 LF 归一文本 + 原编码元信息。不存在 / 非文件 / 读取失败返回 (None, None)。"""
    p = _fs_path(workspace, rel)
    try:
        if not p.is_file():
            return None, None
        raw = p.read_bytes()
    except OSError:
        return None, None
    tf = _text_codec().decode_bytes(raw, strict=False)
    return tf.text, tf


# plan-20261019-toolcard-v2：撤销 / 回滚被拒绝时对用户说明的原因。
_REASON_MODIFIED = "文件在本轮之后被修改过（手动改动或其他程序所致），撤销会覆盖这些改动"
_REASON_DELETED = "文件在本轮之后已被删除，无法恢复到本轮之前的状态"
_REASON_RECREATED = "本轮删除了该文件，之后它又被重新创建（或恢复）了，撤销会覆盖现有文件"
_REASON_CHAIN = "文件在相邻两轮之间被改动过，无法安全衔接撤销"
_REASON_NO_BACKUP = "缺少写盘前备份，无法恢复该文件"


def _state_mismatch_reason(workspace: str, rec: RollbackWrite) -> str | None:
    """磁盘上的文件是否仍是这条记录写完之后的样子；不是就返回原因（有人在这之后动过它）。

    优先比对写后字节指纹（不受 GBK / BOM / 换行归一化影响）；旧记录没有指纹时，
    文本记录退回文本比较（与记录时同一读取口径），二进制旧记录无从校验，放行。
    """
    rel = rec.path
    exists = path_exists(workspace, rel)
    if rec_exists_after(rec) is False:
        return _REASON_RECREATED if exists else None
    if not exists:
        return _REASON_DELETED
    digest = getattr(rec, "after_hash", None)
    if digest:
        cur = file_digest(workspace, rel)
        return _REASON_MODIFIED if (cur is not None and cur != digest) else None
    if not getattr(rec, "binary", False) and rec.new_content is not None:
        cur_text = _read_file_text(workspace, rel)
        if cur_text is not None and cur_text != rec.new_content:
            return _REASON_MODIFIED
    return None


def _chain_gap_reason(workspace: str, prev_last: RollbackWrite, next_first: RollbackWrite,
                      next_ckpt: str | None) -> str | None:
    """相邻两轮的衔接校验：后一轮写之前的状态，必须等于前一轮写完之后的状态。
    只在回滚多轮（同一文件被连续几轮改写）时才会用到。"""
    prev_absent = rec_exists_after(prev_last) is False
    next_absent = getattr(next_first, "op", None) == OP_CREATE
    if prev_absent != next_absent:
        return _REASON_CHAIN
    if prev_absent:
        return None
    digest = getattr(prev_last, "after_hash", None)
    if digest and next_ckpt:
        ck_digest = file_digest(workspace, next_ckpt)
        if ck_digest is not None and ck_digest != digest:
            return _REASON_CHAIN
    return None


def _plan_path_grouped(plan: RollbackPlan, workspace: str, rel: str, recs: list[RollbackWrite],
                       ckpt_for: Callable[[RollbackWrite], str | None], *, strict: bool) -> None:
    """新格式记录（带 op）的单文件规划：校验「最新状态未被动过」后，直接还原到最早一轮写之前的状态。

    与逐条三路合并不同，这里不依赖对文本的反向推演，因此 GBK / BOM / 换行 / 二进制都按字节还原：
      · 最早一轮写之前文件不存在 → 删除它；
      · 否则从该轮首次写盘前的备份（checkpoint）按字节恢复，没有备份时文本记录退回写前文本。
    """
    groups: list[list[RollbackWrite]] = []
    for rec in recs:
        if groups and groups[-1][0].turn_id == rec.turn_id:
            groups[-1].append(rec)
        else:
            groups.append([rec])
    # 二进制 / 超限文件不读全文（预览也用不到），避免为一次规划把大文件整个读进内存
    if any(getattr(r, "binary", False) for r in recs):
        disk_text, tf = None, None
    else:
        disk_text, tf = _read_disk_text(workspace, rel)
    plan.disk.setdefault(rel, disk_text)
    latest = groups[-1][-1]
    first = groups[0][0]
    if strict:
        reason = _state_mismatch_reason(workspace, latest)
        if reason:
            plan.steps.append(_Step(rel, latest.turn_id, "noop", True, reason))
            return
    ckpts = [None if g[0].op == OP_CREATE else ckpt_for(g[0]) for g in groups]
    if strict:
        for i in range(len(groups) - 1, 0, -1):
            reason = _chain_gap_reason(workspace, groups[i - 1][-1], groups[i][0], ckpts[i])
            if reason:
                plan.steps.append(_Step(rel, groups[i][0].turn_id, "noop", True, reason))
                return
    if first.op == OP_CREATE:
        if path_exists(workspace, rel):
            plan.finals[rel] = _FinalOp(kind="delete")
            plan.steps.append(_Step(rel, first.turn_id, "delete"))
        else:
            plan.steps.append(_Step(rel, first.turn_id, "noop"))
        return
    ck = ckpts[0]
    if ck and _fs_path(workspace, ck).is_file():
        plan.finals[rel] = _FinalOp(kind="ckpt", ckpt=ck, text=None if first.binary else first.old_content)
        plan.steps.append(_Step(rel, first.turn_id, "restore"))
        return
    if not first.binary and first.old_content is not None:
        plan.finals[rel] = _FinalOp(kind="write", text=first.old_content, tf=tf)
        plan.steps.append(_Step(rel, first.turn_id, "restore"))
        return
    plan.steps.append(_Step(rel, first.turn_id, "noop", True, _REASON_NO_BACKUP))


def _plan_path_legacy(plan: RollbackPlan, workspace: str, rel: str, recs: list[RollbackWrite],
                      ckpt_for: Callable[[RollbackWrite], str | None], *, strict: bool) -> None:
    """旧格式记录（无 op）的单文件规划：按倒序（最后写入先撤销）逐条模拟，沿用三路合并。

    虚拟盘面 `virt` 记录文件在前序步骤之后的内容，使同一文件的多次改写能逐步推进；
    冲突步骤不推进盘面（保持用户手动改动），后续步骤基于真实盘面继续判断。
    strict 时先校验最新一条记录：磁盘不再是它写完的样子即整文件冲突，不进入三路合并。
    """
    virt: dict[str, object] = {}
    tfs: dict[str, Any] = {}

    def current(r: str) -> object:
        if r not in virt:
            text, tf = _read_disk_text(workspace, r)
            tfs[r] = tf
            plan.disk.setdefault(r, text)
            virt[r] = text
        return virt[r]

    verified = False
    blocked = False
    for rec in reversed(recs):
        if blocked:
            plan.steps.append(_Step(rel, rec.turn_id, "noop"))
            continue
        if strict and not verified:
            reason = _state_mismatch_reason(workspace, rec)
            if reason:
                current(rel)
                plan.steps.append(_Step(rel, rec.turn_id, "noop", True, reason))
                blocked = True
                continue
            # 校验通过：把盘面视为这条记录写完的样子（指纹一致时，文本解码差异不再造成误冲突）
            if not rec.binary and rec.new_content is not None:
                current(rel)
                virt[rel] = rec.new_content
        verified = True
        if rec.binary:
            ckpt = ckpt_for(rec)
            if not ckpt:
                plan.steps.append(_Step(rel, rec.turn_id, "noop", True, "二进制文件缺少写盘前备份，已跳过"))
                continue
            current(rel)  # 记录规划时的磁盘文本，供预览展示
            virt[rel] = _BIN_STATE
            plan.finals[rel] = _FinalOp(kind="ckpt", ckpt=ckpt)
            plan.steps.append(_Step(rel, rec.turn_id, "restore"))
            continue

        cur = current(rel)
        if cur is _BIN_STATE:
            plan.steps.append(_Step(rel, rec.turn_id, "noop", True, "同一文件存在二进制改动，无法安全合并，已跳过"))
            continue
        before, after = rec.old_content, rec.new_content
        if before is None:
            # 该次写盘新建了文件：回滚 = 删除；盘面已被手动修改则保留
            if cur is not None and cur == after:
                virt[rel] = None
                plan.finals[rel] = _FinalOp(kind="delete")
                plan.steps.append(_Step(rel, rec.turn_id, "delete"))
            elif cur is None:
                plan.steps.append(_Step(rel, rec.turn_id, "noop"))
            else:
                plan.steps.append(_Step(rel, rec.turn_id, "noop", True, "文件已被手动修改，回滚已跳过"))
            continue
        if cur is None:
            # plan-20261018-turn-changes：磁盘上已没有该文件。
            if after is None:
                # 本轮就是删除它（before 有内容、after 为空）：撤销 = 恢复删除前的内容。
                # 旧实现一律按「文件当前不存在」判冲突，导致含删除的轮次整单撤销被拒（只看到 409）；
                # 也不能当 noop——那样会「撤销成功」但文件没被找回。
                virt[rel] = before
                plan.finals[rel] = _FinalOp(kind="write", text=before, tf=tfs.get(rel))
                plan.steps.append(_Step(rel, rec.turn_id, "restore"))
            else:
                # 本轮写了内容、文件却已不存在（被手动删掉等）：无法安全还原，跳过并说明
                plan.steps.append(_Step(rel, rec.turn_id, "noop", True, "文件当前不存在，已跳过"))
            continue
        if before == after:
            plan.steps.append(_Step(rel, rec.turn_id, "noop"))
            continue
        merged, conflict = _merge3(before, after, str(cur))
        if conflict:
            plan.steps.append(_Step(rel, rec.turn_id, "noop", True, "与手动改动存在重叠，已跳过（可手动处理）"))
            continue
        virt[rel] = merged
        plan.finals[rel] = _FinalOp(kind="write", text=merged, tf=tfs.get(rel))
        plan.steps.append(_Step(rel, rec.turn_id, "restore"))


def build_plan(workspace: str, writes: list[RollbackWrite],
               ckpt_for: Callable[[RollbackWrite], str | None], *,
               strict: bool = True) -> RollbackPlan:
    """规划「把写盘序列倒着撤销」的结果，只读不写。

    plan-20261019-toolcard-v2 严格模式（默认）：撤销前先校验磁盘上的文件**是否仍是最新一条记录写完的样子**。
    本轮之后有人手动改过它、删过它、或被其他程序改写，就整文件判为冲突并给出原因——
    撤销与回滚默认整体拒绝，不再把用户的手动改动「三路合并」进撤销结果（此前非重叠的手动改动会被静默合并）。
    """
    plan = RollbackPlan(workspace=workspace)
    by_path: dict[str, list[RollbackWrite]] = {}
    for rec in reversed(writes):  # 路径按「最近写入」倒序出现，与此前的步骤顺序一致
        by_path.setdefault(rec.path, [])
    for rec in writes:
        by_path[rec.path].append(rec)
    for rel, recs in by_path.items():
        if not _is_safe_rel_path(rel):
            plan.steps.append(_Step(rel, recs[-1].turn_id, "noop", True, "路径不在工作区内，已跳过"))
        elif all(getattr(r, "op", None) is not None for r in recs):
            _plan_path_grouped(plan, workspace, rel, recs, ckpt_for, strict=strict)
        else:
            _plan_path_legacy(plan, workspace, rel, recs, ckpt_for, strict=strict)

    # 只保留真正改变磁盘的终态：内容与盘面一致的写回、盘面已不存在的删除、备份与盘面字节相同的恢复都不做
    for rel in list(plan.finals):
        op = plan.finals[rel]
        disk_text = plan.disk.get(rel)
        if op.kind == "write" and op.text == disk_text:
            del plan.finals[rel]
        elif op.kind == "delete" and disk_text is None and not path_exists(workspace, rel):
            del plan.finals[rel]
        elif op.kind == "ckpt":
            ck_digest = file_digest(workspace, op.ckpt or "")
            if ck_digest is not None and ck_digest == file_digest(workspace, rel):
                del plan.finals[rel]
    return plan


def plan_preview_rows(plan: RollbackPlan) -> list[dict]:
    """按文件聚合的预览行（与前端 RollbackPreviewFile 对齐）。只展示有改动或有冲突的文件。"""
    by_path: dict[str, list[_Step]] = {}
    for s in plan.steps:
        by_path.setdefault(s.path, []).append(s)
    rows: list[dict] = []
    for rel, steps in by_path.items():
        conflict_steps = [s for s in steps if s.conflict]
        op = plan.finals.get(rel)
        if op is None and not conflict_steps:
            continue
        current = plan.disk.get(rel)
        if op is None:
            action, after = "restore", current
        elif op.kind == "delete":
            action, after = "delete", ""
        elif op.kind == "ckpt":
            # 文本文件按备份字节恢复时带着写前文本，预览能展示恢复后的内容；二进制没有文本可展示
            action, after = "restore", op.text
        else:
            action, after = "restore", op.text
        if conflict_steps and op is not None:
            # 冲突与改动并存（同一文件多步，部分跳过）：预览保持磁盘现状，明确标注
            after = current
        binary_restore = op is not None and op.kind == "ckpt" and op.text is None
        reason = conflict_steps[0].reason if conflict_steps else (
            "二进制文件，回滚按写盘前备份恢复" if binary_restore else None)
        rows.append({
            "path": rel,
            "action": action,
            "conflict": bool(conflict_steps),
            "reason": reason,
            "before": "" if binary_restore else (current or ""),
            "after": after or "",
        })
    return rows


def _apply_plan_sync(plan: RollbackPlan) -> dict:
    """把规划落盘：先备份每个目标文件的原始字节，任一步失败则逐个还原后报错（全部或全不）。"""
    backups: list[tuple[str, bytes | None]] = []
    restored = deleted = 0
    try:
        for rel, op in plan.finals.items():
            p = _fs_path(plan.workspace, rel)
            orig = p.read_bytes() if p.is_file() else None
            backups.append((rel, orig))
            if op.kind == "delete":
                if p.is_file():
                    p.unlink()
                deleted += 1
            elif op.kind == "ckpt":
                if not restore_checkpoint(plan.workspace, op.ckpt or "", rel):
                    raise OSError(f"二进制备份恢复失败: {rel}")
                restored += 1
            else:
                data = _text_codec().encode_text(op.tf, op.text or "") if op.tf is not None else (op.text or "").encode("utf-8")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                restored += 1
    except Exception as e:  # noqa: BLE001 — 任何中途异常（含编码失败 UnicodeEncodeError）都必须还原，不能留下半回滚的工作区
        logger.warning("[rollback] 应用回滚失败，开始还原已改写的文件: %s", e)
        for rel, orig in reversed(backups):
            try:
                p = _fs_path(plan.workspace, rel)
                if orig is None:
                    if p.is_file():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(orig)
            except OSError:
                logger.exception("[rollback] 还原文件失败（需人工检查）: %s", rel)
        return {"ok": False, "error": f"回滚中断，已还原 {len(backups)} 个文件：{e}"}
    return {"ok": True, "restored": restored, "deleted": deleted}


def _conflict_list(plan: RollbackPlan) -> list[dict]:
    seen: dict[str, str] = {}
    for s in plan.conflicts:
        seen.setdefault(s.path, s.reason or "与手动改动冲突")
    return [{"path": p, "reason": r} for p, r in seen.items()]


async def _rollback_turn_files_precise(workspace: str, writes: list[RollbackWrite],
                                       snap: TurnSnapshot | None = None) -> dict:
    """兼容入口：对给定写盘记录规划并原子应用，返回 restored / deleted / failed / conflicts。

    注意：本函数不做冲突门控（冲突会被跳过、其余照常落盘）。整单回滚请走 rollback_turn，
    它会先检查冲突、停止运行中的任务，再应用同一份规划。
    """
    snaps = {snap.turn_id: snap} if snap is not None else {}
    plan = build_plan(workspace, writes, lambda rec: _checkpoint_for_path(snaps.get(rec.turn_id), rec.path))
    applied = _apply_plan_sync(plan)
    conflicts = _conflict_list(plan)
    return {
        "ok": bool(applied.get("ok")) and not conflicts,
        "restored": applied.get("restored", 0),
        "deleted": applied.get("deleted", 0),
        "failed": len(conflicts),
        "conflicts": [c["path"] for c in conflicts],
        "conflict_details": conflicts,
    }


async def _workspace_for_session(db: AsyncSession, session_id: int) -> str:
    from app.persistence.models.message import Session
    session = await db.get(Session, session_id)
    workspace = session.worktree_path if session and session.worktree_path else None
    if workspace is None and session and session.project_id:
        from app.services.project_service import get_project
        project = await get_project(db, session.project_id)
        workspace = project.path if project else None
    return workspace or ""


async def _plan_for_turn(db: AsyncSession, session_id: int, turn_id: int, workspace: str) -> RollbackPlan:
    writes = await list_turn_writes(db, session_id, turn_id)
    res = await db.execute(
        select(TurnSnapshot).where(TurnSnapshot.session_id == session_id, TurnSnapshot.turn_id >= turn_id)
    )
    snaps = {s.turn_id: s for s in res.scalars().all()}
    # 各轮「新建（未登记）又被删除」的文件本来就不存在于该轮之前：不参与还原
    by_turn: dict[int, dict[str, list[RollbackWrite]]] = {}
    for w in writes:
        by_turn.setdefault(int(w.turn_id), {}).setdefault(w.path, []).append(w)
    drop: set[tuple[int, str]] = set()
    for tid, grouped in by_turn.items():
        for p in await unrecorded_birth_paths(db, tid, grouped):
            drop.add((tid, p))
    if drop:
        writes = [w for w in writes if (int(w.turn_id), w.path) not in drop]
    return build_plan(workspace, writes, lambda rec: _checkpoint_for_path(snaps.get(rec.turn_id), rec.path))


async def build_turn_preview(db: AsyncSession, turn_id: int) -> dict:
    """回滚预览（只读）：按文件聚合的变更清单、冲突清单与连带影响。"""
    snap = await _snapshot_for_turn(db, turn_id)
    if snap is None:
        raise LookupError("该 turn 无快照")
    if snap.rolled_back:
        raise ValueError("该 turn 已回滚，不可重复操作")
    workspace = await _workspace_for_session(db, snap.session_id)
    files: list[dict] = []
    conflicts: list[dict] = []
    if workspace and await list_turn_writes(db, snap.session_id, turn_id):
        plan = await _plan_for_turn(db, snap.session_id, turn_id, workspace)
        files = plan_preview_rows(plan)
        conflicts = _conflict_list(plan)
    affected = await count_rollback_affected(db, snap.session_id, turn_id)
    return {"files": files, "conflicts": conflicts, "affected": affected}


def _checkpoint_for_path(snap: TurnSnapshot | None, rel: str) -> str | None:
    """从快照 file_list 反查该 path 的 checkpoint 路径（v26 {ckpt, path} 条目）。"""
    if snap is None:
        return None
    for item in snap.file_list or []:
        if isinstance(item, dict) and item.get("path") == rel:
            return item.get("ckpt") or None
    return None


async def list_snapshots(db: AsyncSession, session_id: int) -> list[TurnSnapshot]:
    res = await db.execute(
        select(TurnSnapshot).where(TurnSnapshot.session_id == session_id)
        .order_by(TurnSnapshot.id.desc())
    )
    return list(res.scalars().all())


# ── 仅撤销单轮文件改动（plan-20261018-turn-changes）──

async def revert_turn_files(db: AsyncSession, *, turn_id: int, allow_partial: bool = False) -> dict:
    """只反向应用该 turn 的写盘改动，不删消息、不改 turn.status、不标记 rolled_back。

    与 rollback_turn 的区别：rollback_turn 是「回滚到该 turn」，会连带软删消息并把
    本 turn 及之后的 turn 置 rolled_back；本函数供「变更卡片 → 撤销本批次」使用，
    只关心文件，轮次与消息保持原样。

    顺序门控（plan-20261018-turn-changes）：写盘记录在撤销/回滚时会被物理删除，
    因此「同会话仍有 turn_id > 本 turn 的写盘记录」即代表更新的改动尚未撤销。
    此时直接拒绝且不改任何文件，避免先撤旧轮导致新轮改动失去基线。
    """
    snap = await _snapshot_for_turn(db, turn_id)
    if snap is None:
        return {"ok": False, "code": "no_snapshot", "reason": "该轮次无可撤销的文件改动"}
    if snap.rolled_back:
        return {"ok": False, "code": "rolled_back", "reason": "该轮次已回滚，不可重复操作"}

    # 1. 顺序门控：只能从最新一轮往前撤，且必须在动文件之前判断。
    #    plan-20261019-toolcard-v2：只看「有净变更」的轮次——只留下零净变更记录的轮次
    #    （如只新建又删除了临时文件）没有变更卡片，不能拦住更早一轮的撤销。
    visible_turns = await visible_change_turns(db, snap.session_id)
    later_turn_ids = [t for t in visible_turns if t > turn_id]
    if later_turn_ids:
        return {
            "ok": False,
            "code": "order_violation",
            "reason": f"请先撤销更晚轮次（#{later_turn_ids[0]}）的改动",
            "later_turn_ids": later_turn_ids,
        }

    workspace = await _workspace_for_session(db, snap.session_id)
    if not workspace:
        return {"ok": False, "code": "no_workspace", "reason": "该会话没有可用的工作区"}

    # 2. 只取本 turn 的写盘记录；list_turn_writes 是 >= 语义，不能用于精确撤销
    write_res = await db.execute(
        select(RollbackWrite).where(
            RollbackWrite.session_id == snap.session_id,
            RollbackWrite.turn_id == turn_id,
        ).order_by(RollbackWrite.id.asc())
    )
    writes = list(write_res.scalars().all())
    if not writes:
        return {"ok": False, "code": "no_writes", "reason": "该轮次没有文件改动"}
    # 本轮新建（未登记）又被删除的文件：本轮开始前并不存在，撤销时不能从删除前备份还原
    _by_path: dict[str, list[RollbackWrite]] = {}
    for w in writes:
        _by_path.setdefault(w.path, []).append(w)
    _bogus = await unrecorded_birth_paths(db, turn_id, _by_path)
    if _bogus:
        writes_for_plan = [w for w in writes if w.path not in _bogus]
    else:
        writes_for_plan = writes

    # 3. 复用三路合并规划：冲突时默认整体拒绝，未改动任何文件
    plan = build_plan(workspace, writes_for_plan, lambda rec: _checkpoint_for_path(snap, rec.path))
    conflicts = _conflict_list(plan)
    if conflicts and not allow_partial:
        # plan-20261019-toolcard-v2：本轮之后被改动过的文件一律拒绝撤销，并逐个说明原因（conflicts[].reason）
        return {
            "ok": False,
            "code": "conflict",
            "reason": f"撤销已取消：{len(conflicts)} 个文件在本轮之后被改动过，为避免覆盖，未修改任何文件",
            "conflicts": conflicts,
        }

    applied = _apply_plan_sync(plan)
    if not applied.get("ok"):
        return {"ok": False, "code": "apply_failed", "reason": applied.get("error", "撤销失败，工作区已还原")}

    # 4. 清理记录与快照登记：文件已撤销，写盘记录与 checkpoint 登记不能留，
    #    否则后续 rollback 会从旧 checkpoint 把已撤销内容重新写回磁盘。
    from sqlalchemy import delete as _delete, update as _update
    from app.persistence.database import run_write_locked

    def _persist(s):
        s.execute(_delete(RollbackWrite).where(
            RollbackWrite.session_id == snap.session_id,
            RollbackWrite.turn_id == turn_id,
        ))
        s.execute(_delete(FileReview).where(FileReview.turn_id == turn_id))
        s.execute(_update(TurnSnapshot).where(TurnSnapshot.id == snap.id).values(
            file_list=[], new_files=[],
        ))
        s.commit()

    await run_write_locked(_persist, label=f"rollback.revert.{turn_id}")

    # 5. 广播 + 审计：前端据此清除该 turn 的变更卡片缓存并重拉撤销锁
    from app.orchestration.agent_events import broadcast
    await broadcast(snap.session_id, {
        "event": "turn.files_reverted",
        "payload": {"turn_id": turn_id, "session_id": snap.session_id},
    })

    from app.services import audit_service
    await audit_service.log(db, action="revert_files", session_id=snap.session_id,
                            turn_id=turn_id,
                            detail={"restored": applied.get("restored", 0),
                                    "deleted": applied.get("deleted", 0),
                                    "conflicts": conflicts})

    return {
        "ok": True,
        "turn_id": turn_id,
        "restored": applied.get("restored", 0),
        "deleted": applied.get("deleted", 0),
        "skipped": len(conflicts),
        "conflicts": conflicts,
        "files": list(dict.fromkeys(w.path for w in writes)),
    }


async def revert_lock(db: AsyncSession, session_id: int) -> dict:
    """撤销顺序锁：返回该会话「仍有写盘记录」的 turn id（升序）与其中最新一个。

    前端据此只允许撤销最新一轮；后端 revert_turn_files 仍做二次强校验，
    避免前端缓存过期时发生乱序撤销。
    """
    # plan-20261019-toolcard-v2：只统计「有净变更」的轮次，与变更卡片可见性一致
    pending = await visible_change_turns(db, session_id)
    return {"latest_turn_id": pending[-1] if pending else None, "pending_turn_ids": pending}
