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

from app.core.background import spawn_background as _spawn
from app.core.enums import SideKind
from app.orchestration.subsession_report import SUBSESSION_REPORT_FLAG
from app.orchestration.approval_policy import normalize_approval_mode, normalize_execution_mode

logger = logging.getLogger(__name__)

# ── 规则常量（不写裸字面量）──
MAX_SUBSESSIONS_PER_PARENT = 8
DEFAULT_SUBSESSION_MODE = "agent"
# plan-156-621：创建串行化——容量门禁「先查运行中数量、再建子会话」在并发下会同时通过校验。
# 单写线程只保证写库不交错，不保证「校验 → 创建」这段临界区；创建是低频动作，全局串行无感。
_CREATE_GUARD = asyncio.Lock()
TITLE_MAX_LEN = 80
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

# 进度汇报里「步骤进度」一行的长度上限（只是步骤标题 + 状态；结论原文另走 report，不截断）
_OUTPUT_PREVIEW_CHARS = 600
# 取结论原文时向前扫描的正文条数（跳过末尾的空文本 / 思考残留）
_FINAL_ANSWER_SCAN = 5
# 等待子会话结束时的轮询间隔（秒）
_WAIT_POLL_INTERVAL_S = 1.5
# 子会话最近一个 turn 的终态 → 进度视图状态
_TERMINAL_TURN_STATUS: dict[str, str] = {
    "failed": STATUS_FAILED,
    "cancelled": STATUS_INTERRUPTED,
    "interrupted": STATUS_INTERRUPTED,
}
# turn 收尾状态 → 回报给主代理的文案
_FINISH_STATUS_TEXT: dict[str, str] = {
    "completed": "已完成",
    "failed": "执行失败",
    "interrupted": "被中断",
}
# plan-156-621：同一 (子会话, turn) 只回报一次。正常收尾与中断 finalizer 都可能触发，
# 两次回报会让主代理上下文出现两条相同结论、主会话多一条系统消息。
_REPORTED_TURNS: set[tuple[int, int]] = set()
_REPORTED_TURNS_MAX = 4096


@dataclass
class SubsessionBrief:
    """一个子会话的进度视图（工具返回 / 左栏行共用同一口径）。"""

    subsession_id: int
    title: str
    status: str
    mode: str
    running_turn_id: int | None = None
    last_output: str = ""
    # plan-156-620：最近一轮的结论原文（完整，不截断；分段由 subsession_report 负责）
    report: str = ""
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
        select(Session)
        .where(Session.side_parent_id == root, Session.side_kind == SideKind.SUB.value)
        .order_by(Session.id)
    )
    return list(res.scalars().all())


async def list_all_children(db: AsyncSession, project_id: int | None = None) -> list:
    """全部未归档的执行子会话（左栏一次性重建用）。

    plan-156-620：此前左栏对每个主会话各发一次 `/subsessions`（上百个请求），
    启动与切会话时把后端请求队列塞满，表现为「卡片加载很慢、点什么都慢」。
    """
    from sqlalchemy.orm import aliased

    from app.persistence.models.message import Session

    # plan-156-621：父会话已归档 / 已被永久删除时不再返回其子会话——
    # 左栏要把子会话挂在父行下方，父行不在了就无处可挂，用户看到的是无法归位的孤儿行。
    parent = aliased(Session)
    stmt = (
        select(Session)
        .join(parent, parent.id == Session.side_parent_id)
        .where(
            Session.side_kind == SideKind.SUB.value,
            Session.status != "archived",
            parent.status != "archived",
        )
    )
    if project_id is not None:
        stmt = stmt.where(Session.project_id == project_id)
    res = await db.execute(stmt.order_by(Session.id))
    return list(res.scalars().all())


async def _require_subsession(db: AsyncSession, subsession_id: int):
    """取一个执行子会话（不存在或不是子会话时抛业务错误）。"""
    row = await _get_session_row(db, subsession_id)
    if row is None or row.side_parent_id is None or row.side_kind != SideKind.SUB.value:
        raise SubsessionError(f"子会话 #{subsession_id} 不存在")
    return row


async def require_owned(db: AsyncSession, caller_session_id: int, subsession_id: int):
    """校验目标子会话与调用方同属一个主会话（主会话本身或其任一子会话都算同组）。

    plan-156-620：此前 send / progress / interrupt 只按 id 取会话，
    模型填错 id 就能操作别的主会话、甚至别的项目下的子会话。
    """
    row = await _require_subsession(db, subsession_id)
    root = await root_parent_of(db, caller_session_id)
    if row.side_parent_id != root:
        raise SubsessionError(f"子会话 #{subsession_id} 不属于当前主会话，无法操作")
    return row


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
    # plan-156-620：门禁只计「运行中」的子会话——旧提示让用户「先完成或归档」，
    # 但已完成的仍计入上限，用户照做了也建不出来。
    children = await list_children(db, root_session_id)
    running = 0
    for c in children:
        if c.status != "archived" and await _running_turn_id(db, c.id) is not None:
            running += 1
    if running >= MAX_SUBSESSIONS_PER_PARENT:
        raise SubsessionError(
            f"同一主会话下最多同时运行 {MAX_SUBSESSIONS_PER_PARENT} 个子会话，"
            "请等已有子会话完成或中断后再创建"
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
    purpose: str = "",
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
    clean_title = (title or "").strip()[:TITLE_MAX_LEN] or "子会话"

    # plan-156-621：容量门禁是「先查运行中数量、再建子会话」，并发请求会各自通过校验。
    # 把校验、建会话、建 turn 放进同一段串行区——第一次建出的 turn 是 running 状态，
    # 第二次校验才能把它算进上限。创建本身只做写库（执行是异步 spawn），串行代价很小。
    async with _CREATE_GUARD:
        await assert_can_create(db, root)
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
            side_kind=SideKind.SUB.value,
        )
        # 工作树跟随主会话：子会话的改动应落在同一工作区，否则合并语义会分叉
        worktree_path = getattr(parent, "worktree_path", None)
        if worktree_path:
            await session_service.update_session(db, sid, worktree_path=worktree_path)

        # plan-20261018-operator-boost：操控子会话——会话级思考深度落库为最低档（之后每一轮都沿用），
        # 首条消息前置操控规范。设置里关闭「GUI 操作交给子会话」时按普通子会话处理。
        operator = is_operator_purpose(purpose)
        if operator:
            effort = await resolve_operator_effort(db, parent.model_id)
            if effort:
                await session_service.update_session(db, sid, reasoning_effort=effort)

        if start:
            first = _compose_first_message(task, context)
            if operator:
                first = f"{OPERATOR_BRIEF}\n\n---\n\n{first}"
            await start_turn_in_subsession(db, sid, first)
    await _broadcast_created(root, sid, clean_title)
    return sid


# plan-20261018-operator-boost：操控子会话的首条规范。它以最低思考深度运行——规范要短、可执行，
# 把「先查路线、批量动作、失败两次即停、完成即沉淀、汇报带证据」这几条最影响速度与结果的纪律讲清。
OPERATOR_BRIEF = (
    "【操控子会话】你负责执行界面操作（内置浏览器 / 电脑操控），以最低思考深度快速执行：\n"
    "1. 开始前：打开站点或应用时，工具结果会附上已沉淀的操作路线，命中就照做；"
    "不熟悉的站点 / 应用先用 browser_recipe / desktop_recipe 的 recall 查一次。\n"
    "2. 执行：少想多做。流程已知就合并成一次调用（browser_batch / desktop_batch，或 desktop_click 带 text / keys）；"
    "电脑操控的动作结果自带稳定后的新截图，坐标一律取自最近一次截图、按其「坐标口径」给值；"
    "按动作结果里报告的画面变化、提示与报错决定下一步，不要每一步都重新截图或快照。\n"
    "3. 出错：先按错误信息里的修正建议调整一次；同一步失败两次就停下，汇报现状与原因，不要反复重试。\n"
    "4. 完成：多步操作成功后用 recipe 的 save 沉淀做法（不记坐标 / ref）；有下载就给出完整文件路径，"
    "需要核对内容时用 fs_read 或 terminal_exec 检查。\n"
    "5. 汇报：最终回复只写结论——做了什么、结果如何（看到的关键现象、报错原文、文件路径），不复述过程。"
)

_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def is_operator_purpose(purpose: str | None) -> bool:
    """create_subsession 的 purpose 是否为操控子会话（设置关闭时一律按普通子会话）。"""
    from app.core.config import settings

    if (purpose or "").strip().lower() != "operator":
        return False
    return getattr(settings, "operator_subsession_enabled", True) is not False


async def resolve_operator_effort(db: AsyncSession, model_id: int | None) -> str | None:
    """操控子会话的思考深度。

    设置 operator_reasoning_effort：
      · lowest（默认）→ 当前模型声明的最低「非关闭」档；模型未声明档位时用 low；
      · follow → 跟随子会话默认设置（返回 None，不落会话级档位）；
      · 其它 → 原样使用（none / minimal / low / medium / high …）。
    """
    from app.core.config import settings

    want = (getattr(settings, "operator_reasoning_effort", "lowest") or "lowest").strip().lower()
    if want == "follow":
        return None
    if want != "lowest":
        return want[:20]
    levels: list[str] = []
    if model_id:
        try:
            from app.persistence.models.model_reg import Model

            row = await db.get(Model, int(model_id))
            levels = [str(x).lower() for x in (getattr(row, "reasoning_efforts", None) or [])]
        except Exception:  # noqa: BLE001 — 读不到模型档位时按 low 处理
            levels = []
    for lv in _EFFORT_ORDER:
        if lv != "none" and lv in levels:
            return lv
    return "low"


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

    _spawn(_run())
    return turn_id


async def send_message(db: AsyncSession, subsession_id: int, message: str) -> dict:
    """向子会话发消息：运行中 → 注入当前 turn；空闲 / 已中断 → 新开一个 turn。"""
    from app.orchestration import engine

    await _require_subsession(db, subsession_id)
    if not (message or "").strip():
        raise SubsessionError("消息内容不能为空")

    running = await _running_turn_id(db, subsession_id)
    if running is not None:
        ok = engine.inject_input(running, {"content": message})
        if not ok:
            raise SubsessionError("子会话正在运行，但注入通道不可用，请稍后重试")
        return {"subsession_id": subsession_id, "delivered": "injected", "turn_id": running}

    turn_id = await start_turn_in_subsession(db, subsession_id, message)
    return {"subsession_id": subsession_id, "delivered": "new_turn", "turn_id": turn_id}


# ── 进度 ────────────────────────────────────────────────────


async def final_answer(db: AsyncSession, session_id: int, turn_id: int) -> str:
    """某一轮子会话的结论原文：该轮最后一条主线程 AI 正文（不含思考 / 子代理线程）。"""
    from app.core.enums import MsgType, SenderType
    from app.persistence.models.message import Message

    rows = (await db.execute(
        select(Message.content)
        .where(
            Message.session_id == session_id,
            Message.turn_id == turn_id,
            Message.msg_type == MsgType.TEXT.value,
            Message.sender_type == SenderType.AGENT.value,
            Message.thread_id.is_(None),
            Message.deleted.is_(False),
        )
        .order_by(Message.id.desc())
        .limit(_FINAL_ANSWER_SCAN)
    )).scalars().all()
    for content in rows:
        c = content or {}
        if c.get("thinking") is True:
            continue
        text = str(c.get("text") or "").strip()
        if text:
            return text
    return ""


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
    report = ""
    files: list[str] = []

    last_turn = (await db.execute(
        select(Turn).where(Turn.session_id == subsession_id).order_by(Turn.id.desc()).limit(1)
    )).scalars().first()
    if last_turn is not None and running_turn_id is None:
        if last_turn.status in _TERMINAL_TURN_STATUS:
            status = _TERMINAL_TURN_STATUS[last_turn.status]
        elif last_turn.status == "awaiting_confirmation":
            status = STATUS_RUNNING

    if last_turn is not None:
        report = await final_answer(db, subsession_id, last_turn.id)
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
        report=report,
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
        await require_owned(db, parent_session_id, subsession_id)
        targets = [subsession_id]
    else:
        children = await list_children(db, parent_session_id)
        targets = [c.id for c in children if c.status != "archived"]

    if wait and targets:
        await _wait_until_idle(targets, timeout_s)
        # 调用方会话仍持有等待前的读快照，看不到终态；也不能 rollback 它（会丢掉调用方
        # 尚未提交的改动）——改用新连接读取结果
        from app.persistence.database import async_session_factory

        async with async_session_factory() as fresh:
            return [await describe(fresh, sid) for sid in targets]
    return [await describe(db, sid) for sid in targets]


async def _wait_until_idle(session_ids: list[int], timeout_s: float) -> None:
    """轮询等待（服务端内部用，短间隔 + 超时上限，不占用模型上下文）。

    plan-156-620：每次轮询都用**新的数据库会话**。此前复用调用方的会话，
    SQLite WAL 下同一读事务只看得到开始时的快照，子会话早已结束也读不到，
    `wait=true` 必然空等到超时（主代理被卡 5 分钟）。
    """
    from app.persistence.database import async_session_factory

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, timeout_s)
    while loop.time() < deadline:
        async with async_session_factory() as fresh:
            pending = [sid for sid in session_ids if await _running_turn_id(fresh, sid) is not None]
        if not pending:
            return
        await asyncio.sleep(_WAIT_POLL_INTERVAL_S)


# ── 中断 ────────────────────────────────────────────────────


async def interrupt(db: AsyncSession, subsession_id: int) -> dict:
    """中断子会话当前运行中的 turn（无运行中 turn 时幂等返回）。"""
    from app.orchestration import engine

    await _require_subsession(db, subsession_id)

    turn_id = await _running_turn_id(db, subsession_id)
    if turn_id is None:
        return {"subsession_id": subsession_id, "interrupted": False, "reason": "没有正在运行的 turn"}

    ok = await engine.cancel_turn(turn_id, source="subsession")
    return {"subsession_id": subsession_id, "interrupted": bool(ok), "turn_id": turn_id}


# ── 子会话侧通信 ─────────────────────────────────────────────

# 主代理空闲时，子会话的汇报先落库为一条用户可见的待送达消息，
# 等主代理下一轮开始时由 injected_inputs 通道取走（不在本函数里阻塞等待）。
_PENDING_PREFIX = "[子会话汇报]"

# 汇报类型 → 文案（result 由收尾钩子自动发出，见 schedule_finish_report）
_NOTIFY_KIND_LABEL: dict[str, str] = {
    "progress": "汇报",
    "question": "提问",
    "result": "结束",
}


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
    if row is None or row.side_parent_id is None or row.side_kind != SideKind.SUB.value:
        raise SubsessionError("只有子会话可以向主代理汇报")
    root = row.side_parent_id

    label = _NOTIFY_KIND_LABEL.get(kind, _NOTIFY_KIND_LABEL["progress"])
    text = f"{_PENDING_PREFIX}「{row.title or f'子会话 #{subsession_id}'}」{label}：{message}"

    main_turn = await _running_turn_id(db, root)
    if main_turn is not None and engine.inject_input(main_turn, {
        "content": text,
        # plan-156-621：注入是内存队列，turn 收尾会清空。带此标记的项若没被 drain，
        # 由 engine 收尾调用 persist_dropped_inputs 补落库，避免「注入与收尾同刻」时结论消失。
        "_persist_on_drop": {
            "session_id": root,
            "subsession_id": subsession_id,
            "text": text,
        },
    }):
        return {"delivered": "injected", "main_turn_id": main_turn}

    # 主代理空闲：作为一条系统提示落库，用户与主代理下一轮都能看到
    try:
        from app.core.enums import MsgType, SenderType
        from app.services import message_service

        await message_service.create_message(
            db, session_id=root,
            sender_type=SenderType.SYSTEM.value,
            msg_type=MsgType.TEXT.value,
            # plan-156-620：打上来源标记——上下文装配据此把它作为一条独立的输入交给主代理，
            # 而不是当成主代理自己的暂存正文（此前会被下一段正文覆盖而丢失）
            content={"text": text, SUBSESSION_REPORT_FLAG: True, "subsession_id": subsession_id},
        )
    except Exception:  # noqa: BLE001 — 落库失败不应让子会话任务中断
        logger.warning("[subsession] 主代理汇报落库失败 sid=%s", subsession_id, exc_info=True)
    return {"delivered": "queued", "main_turn_id": None}


async def persist_dropped_inputs(items: list[dict]) -> None:
    """plan-156-621：turn 收尾时把没被消费的子会话汇报补落库。

    注入项存在 engine 的内存队列里，turn 收尾会整体清空。子会话回报恰好落在
    「注入成功、但本轮上下文已装配完」的窗口时，汇报既没进上下文也没落库，
    主代理永远拿不到结论。带 `_persist_on_drop` 的项在这里补一条系统消息，
    与「主代理空闲」路径完全同构，下一轮上下文装配照常识别。
    """
    pending = [
        it.get("_persist_on_drop") for it in items
        if isinstance(it, dict) and it.get("_persist_on_drop")
    ]
    if not pending:
        return
    from app.core.enums import MsgType, SenderType
    from app.persistence.database import async_session_factory
    from app.services import message_service

    for p in pending:
        try:
            async with async_session_factory() as db:
                await message_service.create_message(
                    db, session_id=int(p["session_id"]),
                    sender_type=SenderType.SYSTEM.value,
                    msg_type=MsgType.TEXT.value,
                    content={
                        "text": str(p.get("text") or ""),
                        SUBSESSION_REPORT_FLAG: True,
                        "subsession_id": p.get("subsession_id"),
                    },
                )
        except Exception:  # noqa: BLE001 — 兜底落库失败只记日志，不影响 turn 收尾
            logger.warning("[subsession] 丢弃汇报补落库失败 sid=%s",
                           p.get("subsession_id"), exc_info=True)


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
    if src is None or src.side_parent_id is None or src.side_kind != SideKind.SUB.value:
        raise SubsessionError("只有子会话可以向同级子会话发消息")
    if dst is None or dst.side_parent_id is None or dst.side_kind != SideKind.SUB.value:
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
        # plan-156-620：对端在跑但注入通道不可用时不能再开新 turn——
        # 同一会话并发两个 turn，消息流与进度会互相覆盖。
        raise SubsessionError(f"子会话 #{to_subsession} 正在运行，消息暂时无法送达，请稍后重试")

    # 对端空闲：直接给它开一轮，把消息作为任务说明（子会话之间无需「点确认」）
    turn_id = await start_turn_in_subsession(db, to_subsession, label)
    return {"delivered": "new_turn", "turn_id": turn_id}


# ── 子会话结束 → 回报主代理 ───────────────────────────────────


def schedule_finish_report(session_id: int, turn_id: int, final_status: str, summary: str) -> None:
    """turn 收尾时调用：若该会话是执行子会话，把结果回报给主代理（后台执行，不阻塞收尾）。

    plan-156-620：create_subsession 的工具结果承诺「完成后结果会自动回到你的上下文」，
    但此前没有任何收尾钩子——主代理只能反复轮询才知道子会话结束。
    """
    try:
        _spawn(_report_finish(session_id, turn_id, final_status, summary))
    except RuntimeError:
        logger.debug("[subsession] 无事件循环，跳过结束回报 sid=%s", session_id)


async def _report_finish(session_id: int, turn_id: int, final_status: str, summary: str) -> None:
    from app.orchestration.subsession_report import REPORT_PAGE_CHARS, continuation_hint, page_of
    from app.persistence.database import async_session_factory

    # plan-156-621：正常收尾路径与中断 finalizer 可能对同一 turn 各排一次回报，
    # 这里按 (子会话, turn) 去重——检查与占位之间没有 await，单事件循环内天然原子
    key = (int(session_id), int(turn_id))
    if key in _REPORTED_TURNS:
        return
    _REPORTED_TURNS.add(key)
    if len(_REPORTED_TURNS) > _REPORTED_TURNS_MAX:
        _REPORTED_TURNS.clear()
        _REPORTED_TURNS.add(key)

    try:
        async with async_session_factory() as db:
            row = await _get_session_row(db, session_id)
            if row is None or row.side_kind != SideKind.SUB.value:
                return  # 主会话 / 侧边对话的收尾不回报
            status_text = _FINISH_STATUS_TEXT.get(final_status, final_status)
            # plan-156-620：以落库的结论原文为准（引擎传入的 summary 只作兜底），
            # 不再截到 1200 字；过长时给出第一段 + 明确的续读方式
            report = await final_answer(db, session_id, turn_id) or (summary or "").strip()
            page = page_of(report, 0, REPORT_PAGE_CHARS)
            body = page.text or "（子会话没有输出文字结论）"
            await notify_main(
                db, subsession_id=session_id, kind="result",
                message=f"turn {turn_id} {status_text}。结论原文：\n{body}{continuation_hint(page, session_id)}",
            )
    except Exception:  # noqa: BLE001 — 回报失败只记日志，不影响子会话自身收尾
        logger.warning("[subsession] 结束回报失败 sid=%s turn=%s", session_id, turn_id, exc_info=True)
