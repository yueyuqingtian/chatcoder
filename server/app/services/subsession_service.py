"""子会话服务（plan-156-619 S4）。

【职责】
执行期子会话的全部业务动作：创建、发消息、查进度、中断。路由与工具层只做参数转换，
不把逻辑塞进 router 或 agent_loop 的巨型分支。

【存储口径：复用 sessions.side_parent_id，不新建表】
子会话本身就是完整会话（独立 turn 流水线、独立消息、可被用户接管），
`list_sessions` 已过滤 `side_parent_id IS NULL`，因此子会话不会污染主会话列表。
探索用的只读子代理仍走 `agents(kind=sub)` + `messages.thread_id`，两套机制互不替代。

【一层扁平：同级子会话共享同一个父】
旧实现（`sessions.py`）硬拒「侧边会话再开侧边会话」，导致子会话无法派生同级。
这里改为：**任何一级请求方创建的子会话，`side_parent_id` 都取「根主会话 id」**——
即子会话派生出来的仍是同级，不会出现三级嵌套。这样左栏结构永远只有两层。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.approval_policy import normalize_approval_mode, normalize_execution_mode

logger = logging.getLogger(__name__)

# ── 规则常量（不写裸字面量）──
MAX_SUBSESSIONS_PER_PARENT = 8
DEFAULT_SUBSESSION_MODE = "agent"
TITLE_MAX_LEN = 80
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

# 进度汇报里「最近输出」的截断长度
_OUTPUT_PREVIEW_CHARS = 600


@dataclass
class SubsessionBrief:
    """一个子会话的进度视图（工具返回 / 左栏行共用同一口径）。"""

    subsession_id: int
    title: str
    status: str
    mode: str
    running_turn_id: int | None = None
    last_output: str = ""
    files: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


class SubsessionError(Exception):
    """子会话操作的业务错误（路由转 400 / 工具转错误文本）。"""


# ── 查询辅助 ────────────────────────────────────────────────


async def _get_session_row(db: AsyncSession, session_id: int):
    from app.persistence.models.message import Session

    return await db.get(Session, session_id)


async def root_parent_of(db: AsyncSession, session_id: int) -> int:
    """返回该会话所属的**根主会话 id**（一层扁平的锚点）。

    主会话返回自身；子会话返回它挂在的那个主会话。
    """
    row = await _get_session_row(db, session_id)
    if row is None:
        raise SubsessionError("会话不存在")
    return row.side_parent_id or row.id


async def list_children(db: AsyncSession, parent_session_id: int) -> list:
    """某主会话下的全部子会话（按创建先后；同级子会话共享同一父）。"""
    from app.persistence.models.message import Session

    root = await root_parent_of(db, parent_session_id)
    res = await db.execute(
        select(Session).where(Session.side_parent_id == root).order_by(Session.id)
    )
    return list(res.scalars().all())


async def _running_turn_id(db: AsyncSession, session_id: int) -> int | None:
    from app.persistence.models.turn import Turn

    res = await db.execute(
        select(Turn.id)
        .where(Turn.session_id == session_id, Turn.status == STATUS_RUNNING)
        .order_by(Turn.id.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


# ── 创建 ────────────────────────────────────────────────────


async def assert_can_create(db: AsyncSession, root_session_id: int) -> None:
    """校验该主会话下还能不能再建子会话（路由与服务层共用同一口径）。

    抽出来的原因：`/side-sessions`（建空会话）与 `/subsessions`（建了即开工）
    都必须走同一道容量门禁，否则两条入口的上限会不一致。
    """
    children = await list_children(db, root_session_id)
    active = [c for c in children if c.status != "archived"]
    if len(active) >= MAX_SUBSESSIONS_PER_PARENT:
        raise SubsessionError(
            f"同一主会话下最多 {MAX_SUBSESSIONS_PER_PARENT} 个子会话，"
            "请先完成或归档已有子会话"
        )


async def create_subsession(
    db: AsyncSession,
    *,
    parent_session_id: int,
    title: str,
    task: str,
    context: str = "",
    mode: str | None = None,
    start: bool = True,
) -> int:
    """创建一个子会话并（默认）立刻下发首条任务。

    - `side_parent_id` 一律指向根主会话：子会话派生仍是同级，不产生嵌套；
    - 执行模式由调用方（主代理）指定，权限模式继承主会话——模式是能力边界，
      权限是「要不要问」，后者应跟随用户对主会话的整体设定；
    - 首条消息把 `context` 放在 `task` 之前，保证子会话第一步就看到「已完成什么、
      不要做什么」，而不是干到一半才发现冲突。
    """
    from app.services import session_service

    parent = await _get_session_row(db, parent_session_id)
    if parent is None:
        raise SubsessionError("父会话不存在")
    if parent.project_id is None:
        raise SubsessionError("父会话未关联项目，无法创建子会话")

    root = parent.side_parent_id or parent.id
    await assert_can_create(db, root)

    clean_title = (title or "").strip()[:TITLE_MAX_LEN] or "子会话"
    sid = await session_service.create_session(
        db,
        project_id=parent.project_id,
        title=clean_title,
        # plan-156-619 S8：子会话标题是主代理给的任务名（正式名称），
        # 标 named——否则子会话首条消息的自动归纳会把它改成别的名字。
        title_source="named",
        model_id=parent.model_id,
        permission_mode=normalize_execution_mode(mode or DEFAULT_SUBSESSION_MODE),
        approval_mode=normalize_approval_mode(parent.approval_mode),
        side_parent_id=root,
    )
    # 工作树跟随主会话：子会话的改动应落在同一工作区，否则合并语义会分叉
    worktree_path = getattr(parent, "worktree_path", None)
    if worktree_path:
        await session_service.update_session(db, sid, worktree_path=worktree_path)

    if start:
        await start_turn_in_subsession(db, sid, _compose_first_message(task, context))
    await _broadcast_created(root, sid, clean_title)
    return sid


def _compose_first_message(task: str, context: str) -> str:
    """把「背景上下文」与「任务」合成首条用户消息。"""
    body = (task or "").strip()
    ctx = (context or "").strip()
    if not ctx:
        return body
    return f"{ctx}\n\n---\n\n{body}"


async def _broadcast_created(root_session_id: int, subsession_id: int, title: str) -> None:
    """通知前端：主会话下新增了一个子会话（左栏据此增量插入，无需整表刷新）。"""
    try:
        from app.orchestration.agent_events import broadcast

        await broadcast(root_session_id, {
            "event": "subsession.created",
            "payload": {
                "session_id": root_session_id,
                "subsession_id": subsession_id,
                "title": title,
            },
        })
    except Exception:  # noqa: BLE001 — 广播失败不影响子会话本身
        logger.debug("[subsession] 广播创建事件失败 sid=%s", subsession_id, exc_info=True)


# ── 下发首条 / 后续消息 ──────────────────────────────────────


async def start_turn_in_subsession(db: AsyncSession, session_id: int, text: str) -> int:
    """在子会话里创建用户消息与 turn，并异步启动执行。返回 turn id。"""
    from app.core.enums import MsgType, SenderType
    from app.orchestration import engine
    from app.services import message_service, turn_service

    content = (text or "").strip()
    if not content:
        raise SubsessionError("消息内容不能为空")

    user_msg = await message_service.create_message(
        db, session_id=session_id,
        sender_type=SenderType.USER.value,
        msg_type=MsgType.TEXT.value,
        content={"text": content},
        broadcast=False,
    )
    turn_id = await turn_service.create_turn(
        db, session_id=session_id, user_message_id=user_msg.id,
    )
    await message_service.patch_message(user_msg.id, turn_id=turn_id)
    user_msg.turn_id = turn_id

    async def _run() -> None:
        # 独立会话：用新的 session 连接跑 turn，避免与调用方事务互相阻塞
        from app.persistence.database import async_session_factory

        async with async_session_factory() as s:
            await engine.start_turn(s, turn_id=turn_id)

    asyncio.get_event_loop().create_task(_run())
    return turn_id


async def send_message(db: AsyncSession, subsession_id: int, message: str) -> dict:
    """向子会话发消息：运行中 → 注入当前 turn；空闲 / 已中断 → 新开一个 turn。"""
    from app.orchestration import engine

    row = await _get_session_row(db, subsession_id)
    if row is None or row.side_parent_id is None:
        raise SubsessionError("子会话不存在")

    running = await _running_turn_id(db, subsession_id)
    if running is not None:
        ok = engine.inject_input(running, {"content": message})
        if not ok:
            raise SubsessionError("子会话正在运行，但注入通道不可用，请稍后重试")
        return {"subsession_id": subsession_id, "delivered": "injected", "turn_id": running}

    turn_id = await start_turn_in_subsession(db, subsession_id, message)
    return {"subsession_id": subsession_id, "delivered": "new_turn", "turn_id": turn_id}


# ── 进度 ────────────────────────────────────────────────────


async def describe(db: AsyncSession, subsession_id: int) -> SubsessionBrief:
    """读取一个子会话的进度视图。"""
    from app.persistence.models.task import Task
    from app.persistence.models.turn import Turn

    row = await _get_session_row(db, subsession_id)
    if row is None:
        raise SubsessionError("子会话不存在")

    running_turn_id = await _running_turn_id(db, subsession_id)
    # 状态判定口径与左栏一致：有运行中的 turn 即运行中，否则看最后一个 turn 的终态
    status = STATUS_RUNNING if running_turn_id is not None else STATUS_DONE
    last_output = ""
    files: list[str] = []

    last_turn = (await db.execute(
        select(Turn).where(Turn.session_id == subsession_id).order_by(Turn.id.desc()).limit(1)
    )).scalars().first()
    if last_turn is not None and running_turn_id is None:
        if last_turn.status in ("failed", "cancelled", "interrupted"):
            status = STATUS_FAILED if last_turn.status == "failed" else STATUS_INTERRUPTED
        elif last_turn.status == "awaiting_confirmation":
            status = STATUS_RUNNING

    if last_turn is not None:
        steps = (await db.execute(
            select(Task).where(Task.turn_id == last_turn.id, Task.kind == "step")
        )).scalars().all()
        if steps:
            last_output = "; ".join(
                f"{s.title}[{s.status}]" for s in steps[-6:] if s.title
            )[:_OUTPUT_PREVIEW_CHARS]
        # todo 落库的步骤带 agent_id / note，可用于汇总已改动文件
        for s in steps:
            note = (s.note or "").strip()
            if note and note not in files and ("/" in note or "\\" in note):
                files.append(note)

    return SubsessionBrief(
        subsession_id=subsession_id,
        title=row.title or f"子会话 #{subsession_id}",
        status=status,
        mode=normalize_execution_mode(row.permission_mode),
        running_turn_id=running_turn_id,
        last_output=last_output,
        files=files[:20],
    )


async def collect(
    db: AsyncSession,
    *,
    parent_session_id: int,
    subsession_id: int | None = None,
    wait: bool = False,
    timeout_s: float = 300.0,
) -> list[SubsessionBrief]:
    """汇总子会话进度；`wait=True` 时阻塞到目标全部结束（替代反复轮询）。"""
    if subsession_id is not None:
        targets = [subsession_id]
    else:
        children = await list_children(db, parent_session_id)
        targets = [c.id for c in children if c.status != "archived"]

    if wait and targets:
        await _wait_until_idle(db, targets, timeout_s)
    return [await describe(db, sid) for sid in targets]


async def _wait_until_idle(db: AsyncSession, session_ids: list[int], timeout_s: float) -> None:
    """轮询等待（服务端内部用，短间隔 + 超时上限，不占用模型上下文）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(1.0, timeout_s)
    while loop.time() < deadline:
        pending = [sid for sid in session_ids if await _running_turn_id(db, sid) is not None]
        if not pending:
            return
        await asyncio.sleep(1.5)


# ── 中断 ────────────────────────────────────────────────────


async def interrupt(db: AsyncSession, subsession_id: int) -> dict:
    """中断子会话当前运行中的 turn（无运行中 turn 时幂等返回）。"""
    from app.orchestration import engine

    row = await _get_session_row(db, subsession_id)
    if row is None or row.side_parent_id is None:
        raise SubsessionError("子会话不存在")

    turn_id = await _running_turn_id(db, subsession_id)
    if turn_id is None:
        return {"subsession_id": subsession_id, "interrupted": False, "reason": "没有正在运行的 turn"}

    ok = await engine.cancel_turn(turn_id, source="subsession")
    return {"subsession_id": subsession_id, "interrupted": bool(ok), "turn_id": turn_id}


# ── 子会话侧通信 ─────────────────────────────────────────────

# 主代理空闲时，子会话的汇报先落库为一条用户可见的待送达消息，
# 等主代理下一轮开始时由 injected_inputs 通道取走（不在本函数里阻塞等待）。
_PENDING_PREFIX = "[子会话汇报]"


async def notify_main(
    db: AsyncSession,
    *,
    subsession_id: int,
    message: str,
    kind: str = "progress",
) -> dict:
    """子会话 → 主代理：运行中注入主代理当前 turn；空闲则落库等待下一轮。

    与探索子代理的 `report_to_leader` 同语义，但走会话级通道（子会话不是 agent 线程）。
    """
    from app.orchestration import engine
    from app.persistence.models.message import Session

    row = await _get_session_row(db, subsession_id)
    if row is None or row.side_parent_id is None:
        raise SubsessionError("只有子会话可以向主代理汇报")
    root = row.side_parent_id

    label = "提问" if kind == "question" else "汇报"
    text = f"{_PENDING_PREFIX}「{row.title or f'子会话 #{subsession_id}'}」{label}：{message}"

    main_turn = await _running_turn_id(db, root)
    if main_turn is not None and engine.inject_input(main_turn, {"content": text}):
        return {"delivered": "injected", "main_turn_id": main_turn}

    # 主代理空闲：作为一条系统提示落库，用户与主代理下一轮都能看到
    try:
        from app.core.enums import MsgType, SenderType
        from app.services import message_service

        await message_service.create_message(
            db, session_id=root,
            sender_type=SenderType.SYSTEM.value,
            msg_type=MsgType.TEXT.value,
            content={"text": text},
        )
    except Exception:  # noqa: BLE001 — 落库失败不应让子会话任务中断
        logger.warning("[subsession] 主代理汇报落库失败 sid=%s", subsession_id, exc_info=True)
    return {"delivered": "queued", "main_turn_id": None}


async def message_peer(
    db: AsyncSession,
    *,
    from_subsession: int,
    to_subsession: int,
    message: str,
) -> dict:
    """同级子会话之间通信：必须同属一个主会话，否则视为越权。"""
    if from_subsession == to_subsession:
        raise SubsessionError("不能给自己发消息")
    text = (message or "").strip()
    if not text:
        raise SubsessionError("消息内容不能为空")

    src = await _get_session_row(db, from_subsession)
    dst = await _get_session_row(db, to_subsession)
    if src is None or src.side_parent_id is None:
        raise SubsessionError("只有子会话可以向同级子会话发消息")
    if dst is None or dst.side_parent_id is None:
        raise SubsessionError(f"会话 #{to_subsession} 不是子会话")
    # 一层扁平：同级 = 挂在同一个主会话下
    if src.side_parent_id != dst.side_parent_id:
        raise SubsessionError("只能联系同一主会话下的同级子会话（跨主会话的联系不被允许）")

    label = f"{_PENDING_PREFIX}「{src.title or f'子会话 #{from_subsession}'}」同级消息：{text}"
    target_turn = await _running_turn_id(db, to_subsession)
    if target_turn is not None:
        from app.orchestration import engine

        if engine.inject_input(target_turn, {"content": label}):
            return {"delivered": "injected", "turn_id": target_turn}

    # 对端空闲：直接给它开一轮，把消息作为任务说明（子会话之间无需「点确认」）
    await start_turn_in_subsession(db, to_subsession, label)
    return {"delivered": "new_turn", "turn_id": None}
