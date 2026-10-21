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
import secrets
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background import spawn_background as _spawn
from app.core.enums import SideKind
from app.orchestration.subsession_report import SUBSESSION_REPORT_FLAG
from app.orchestration.approval_policy import normalize_approval_mode, normalize_execution_mode

logger = logging.getLogger(__name__)

# ── 规则常量（不写裸字面量）──
# plan-474-2105：子会话并发上限改为设置项（settings.max_subsessions_per_parent，默认 4，范围 1~30），
# 这里只保留「配置属性缺失时」的兜底值——不再作为唯一事实源。
MAX_SUBSESSIONS_PER_PARENT_DEFAULT = 4
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


async def list_running_counts_by_parent() -> dict[int, int]:
    """各主会话当前运行中的子会话数（只读快照，供 /runtime/limits 使用）。

    plan-474-2105：只统计未归档子会话里「有运行中 turn」的那些，与建会话门禁同口径；
    返回里保留运行数为 0 的父，方便前端一次拿全量占用。
    """
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session

    counts: dict[int, int] = {}
    async with async_session_factory() as db:
        res = await db.execute(
            select(Session.id, Session.side_parent_id)
            .where(
                Session.side_kind == SideKind.SUB.value,
                Session.status != "archived",
            )
            .order_by(Session.id)
        )
        for sid, parent_id in res.all():
            if parent_id is None:
                continue
            root = int(parent_id)
            counts.setdefault(root, 0)
            if await _running_turn_id(db, sid) is not None:
                counts[root] += 1
    return counts


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


async def assert_can_create(
    db: AsyncSession,
    root_session_id: int,
    *,
    caller_session_id: int | None = None,
) -> None:
    """校验该主会话下还能不能再建子会话（路由与服务层共用同一口径）。

    plan-474-2105：
      · 上限改读设置 `max_subsessions_per_parent`（单会话并发，默认 4，1~30）；
      · 增加调用方身份校验——子会话不能再创建子会话（工具层已剥离，这里是第二道防线）；
      · 只计「运行中」：已完成的子会话不占名额（本轮不限累计总量）。
    """
    from app.core.config import settings

    if caller_session_id is not None:
        caller = await _get_session_row(db, caller_session_id)
        if caller is not None and caller.side_parent_id is not None:
            raise SubsessionError(
                "子会话不能再创建子会话：需要更多执行力时，把需求写进汇报交给主代理，"
                "由主代理决定是自己做还是再派一个同级子会话。"
            )

    limit = max(1, min(30, int(
        getattr(settings, "max_subsessions_per_parent", MAX_SUBSESSIONS_PER_PARENT_DEFAULT)
        or MAX_SUBSESSIONS_PER_PARENT_DEFAULT
    )))
    # plan-156-620：门禁只计「运行中」的子会话——旧提示让用户「先完成或归档」，
    # 但已完成的仍计入上限，用户照做了也建不出来。
    children = await list_children(db, root_session_id)
    running = 0
    for c in children:
        if c.status != "archived" and await _running_turn_id(db, c.id) is not None:
            running += 1
    if running >= limit:
        raise SubsessionError(
            f"本会话已有 {running} 个子会话在运行，达到上限 {limit}（单会话并发）。"
            "请等其中一个结束，或在设置 →「子代理与子会话」调高上限。"
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
        # plan-474-2105：同时把调用方身份交给门禁——子会话再建子会话在服务层直接拒绝
        await assert_can_create(db, root, caller_session_id=parent_session_id)
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
    "电脑操控的动作结果自带稳定后的新截图和「页面元素」清单：定位优先用清单里的编号或元素文字"
    "（desktop_click element=\"登录\"，免坐标），清单里没有的目标才取最近一次截图坐标、按其「坐标口径」给值；"
    "按动作结果里报告的画面变化、点击处元素、提示与报错决定下一步，不要每一步都重新截图或快照。\n"
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

    async def _fail_turn(reason: str) -> None:
        """turn 没能正常跑起来：必须落成 failed 并回报主代理。

        否则库里的 turn 永远是 running——子会话在左栏一直转圈、占着并发名额（连续失败几次后
        再也建不出新的子会话），主代理的等待也只能熬到超时。
        """
        from app.orchestration.agent_events import broadcast
        from app.persistence.database import async_session_factory

        try:
            async with async_session_factory() as s:
                await turn_service.update_turn_status(s, turn_id, "failed", summary=reason[:200], completed=True)
                await s.commit()
            await broadcast(session_id, {
                "event": "turn.updated",
                "payload": {"turn_id": turn_id, "status": "failed", "session_id": session_id},
            })
            schedule_finish_report(session_id, turn_id, "failed", reason)
        except Exception:  # noqa: BLE001 — 兜底收尾失败只记日志
            logger.warning("[subsession] 失败收尾落库失败 sid=%s turn=%s", session_id, turn_id, exc_info=True)

    async def _run() -> None:
        # 独立会话：用新的 session 连接跑 turn，避免与调用方事务互相阻塞
        from app.persistence.database import async_session_factory

        async with async_session_factory() as s:
            try:
                res = await engine.start_turn(s, turn_id=turn_id)
                await s.commit()
            except Exception as exc:  # noqa: BLE001
                await s.rollback()
                logger.warning("[subsession] turn=%s 执行异常 sid=%s", turn_id, session_id, exc_info=True)
                await _fail_turn(f"执行异常：{type(exc).__name__}")
                return
        # start_turn 在入口校验失败（项目不存在 / 会话不存在）时只返回 ok=False、不改 turn 状态
        if isinstance(res, dict) and res.get("ok") is False and res.get("error") not in (None, "turn already running"):
            await _fail_turn(str(res.get("error") or "启动失败"))

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
    interrupt_check=None,
) -> list[SubsessionBrief]:
    """汇总子会话进度；`wait=True` 时阻塞到目标全部结束（替代反复轮询）。

    interrupt_check：等待期间轮询的判定，为真则提前返回当前进度（调用方有新消息要处理）。
    """
    if subsession_id is not None:
        await require_owned(db, parent_session_id, subsession_id)
        targets = [subsession_id]
    else:
        children = await list_children(db, parent_session_id)
        targets = [c.id for c in children if c.status != "archived"]

    if wait and targets:
        await _wait_until_idle(targets, timeout_s, interrupt_check)
        # 调用方会话仍持有等待前的读快照，看不到终态；也不能 rollback 它（会丢掉调用方
        # 尚未提交的改动）——改用新连接读取结果
        from app.persistence.database import async_session_factory

        async with async_session_factory() as fresh:
            return [await describe(fresh, sid) for sid in targets]
    return [await describe(db, sid) for sid in targets]


async def _wait_until_idle(session_ids: list[int], timeout_s: float, interrupt_check=None) -> None:
    """轮询等待（服务端内部用，短间隔 + 超时上限，不占用模型上下文）。

    plan-156-620：每次轮询都用**新的数据库会话**。此前复用调用方的会话，
    SQLite WAL 下同一读事务只看得到开始时的快照，子会话早已结束也读不到，
    `wait=true` 必然空等到超时（主代理被卡 5 分钟）。
    """
    from app.persistence.database import async_session_factory

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, timeout_s)
    while loop.time() < deadline:
        if interrupt_check is not None and interrupt_check():
            return
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

# ── 提问注册表（plan-474-2105）────────────────────────────────
# 子会话用 message_main(kind="question", wait=true) 提问后在这里挂起，主代理用
# send_to_subsession(question_id=…) 答复时唤醒它——答复直接成为子会话那次工具调用的结果。
# 进程内唯一：提问的生命周期不会跨重启（重启后子会话 turn 已中断，靠检查点续跑）。
_PENDING_QUESTIONS: dict[str, dict] = {}
# 默认等待上限（秒）：与设置项解耦，超时后按最佳判断继续，未决问题写进结论
QUESTION_TIMEOUT_DEFAULT_S = 600.0


def new_question_id(subsession_id: int) -> str:
    return f"q_{int(subsession_id)}_{secrets.token_hex(4)}"


def register_question(question_id: str) -> None:
    """登记一个待答复的提问（同 id 重复登记时保留最早的等待者）。"""
    if question_id in _PENDING_QUESTIONS:
        return
    _PENDING_QUESTIONS[question_id] = {
        "event": asyncio.Event(),
        "reply": None,
        "created_at": time.time(),
    }


async def wait_for_question_reply(
    question_id: str, timeout_s: float, interrupt_check=None,
) -> str | None:
    """子会话侧等待答复；超时返回 None（调用方提示按最佳判断继续）。

    interrupt_check：等待期间轮询的「有新消息」判定（用户直接给子会话发了消息、同级消息等）。
    为真则放弃等待（撤销登记，主代理之后的答复按普通消息送达）并返回 None——
    否则这些消息要等到提问超时（默认 10 分钟）才会被子会话读到。
    """
    entry = _PENDING_QUESTIONS.get(question_id)
    if entry is None:
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, float(timeout_s))
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            slice_s = min(remaining, 0.5) if interrupt_check is not None else remaining
            try:
                await asyncio.wait_for(entry["event"].wait(), timeout=slice_s)
                break
            except asyncio.TimeoutError:
                if interrupt_check is not None and interrupt_check():
                    _PENDING_QUESTIONS.pop(question_id, None)
                    logger.info("[subsession] 提问等待被新消息打断 question=%s", question_id)
                    return None
                if loop.time() >= deadline:
                    raise
    except asyncio.TimeoutError:
        _PENDING_QUESTIONS.pop(question_id, None)
        logger.info("[subsession] 提问等待超时 question=%s", question_id)
        return None
    except asyncio.CancelledError:
        _PENDING_QUESTIONS.pop(question_id, None)
        raise
    _PENDING_QUESTIONS.pop(question_id, None)
    return entry.get("reply") or None


def resolve_question(question_id: str, reply: str) -> bool:
    """主代理答复：唤醒等待方。返回 False 表示该提问已超时/不存在（调用方退回普通发消息）。"""
    entry = _PENDING_QUESTIONS.get(question_id)
    if entry is None:
        return False
    entry["reply"] = reply
    entry["event"].set()
    return True


def discard_question(question_id: str | None) -> None:
    """撤销一个没人等待的提问登记（wait=false 的提问）。

    否则主代理带 question_id 答复时会命中这条登记、被告知「已唤醒子会话」，
    而实际上没有任何等待方——答复凭空丢失，子会话永远收不到。撤销后答复走普通消息送达。
    """
    if question_id:
        _PENDING_QUESTIONS.pop(question_id, None)


def pending_question_count() -> int:
    """当前挂起的提问数（诊断用）。"""
    return len(_PENDING_QUESTIONS)

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
    """子会话 → 主代理：提问 / 汇报统一经会话邮箱投递。

    plan-474-2105：
      · 主代理运行中 → 注入当前轮；提问属控制面（P0），同时请求打断次要等待
        （写文件 / 等用户回答不打断，动作结束后立刻生效）；
      · 主代理空闲 → 提问会拉起一轮（否则子会话白等），汇报只排队等下一轮带走；
      · 提问生成 `question_id` 一并送达，主代理答复时必须带回该 id（见 send_to_subsession）。
    """
    from app.orchestration import mailbox
    from app.persistence.models.message import Session

    row = await _get_session_row(db, subsession_id)
    if row is None or row.side_parent_id is None or row.side_kind != SideKind.SUB.value:
        raise SubsessionError("只有子会话可以向主代理汇报")
    root = row.side_parent_id
    title = row.title or f"子会话 #{subsession_id}"

    question_id: str | None = None
    if kind == "question":
        question_id = new_question_id(subsession_id)
        register_question(question_id)
        text = (
            f"{_PENDING_PREFIX}「{title}」提问（question_id={question_id}）：{message}\n"
            f"答复方式：send_to_subsession({subsession_id}, message, question_id=\"{question_id}\")。"
            "自己答得了就直接答；答不了先用 ask_user_question 问用户，再把用户回答转达给它。"
            "在收到答复前它会一直等待，不要重复提问。"
        )
    else:
        label = _NOTIFY_KIND_LABEL.get(kind, _NOTIFY_KIND_LABEL["progress"])
        text = f"{_PENDING_PREFIX}「{title}」{label}：{message}"

    # 1) 先落库：上下文装配据此把它作为一条独立输入交给主代理（保留既有标记，不改装配口径）；
    #    同时保证投递因任何原因失败时消息仍在库里，下一轮仍能读到。
    try:
        from app.core.enums import MsgType, SenderType
        from app.services import message_service

        await message_service.create_message(
            db, session_id=root,
            sender_type=SenderType.SYSTEM.value,
            msg_type=MsgType.TEXT.value,
            content={
                "text": text,
                SUBSESSION_REPORT_FLAG: True,
                "subsession_id": subsession_id,
                **({"question_id": question_id} if question_id else {}),
                # plan-474-2105：与 mailbox.publish 的落库口径保持一致——前端据 mailbox.kind
                # 直接识别「子会话提问卡 / 汇报」，不必再解析文本前缀。
                "mailbox": {
                    "kind": "subsession_question" if kind == "question" else "subsession_report",
                    "priority": 0 if kind == "question" else 2,
                    "source_session_id": subsession_id,
                    "question_id": question_id,
                },
            },
        )
    except Exception:  # noqa: BLE001 — 落库失败不应让子会话任务中断
        logger.warning("[subsession] 主代理汇报落库失败 sid=%s", subsession_id, exc_info=True)

    # 2) 投递（不再重复落库）：运行中注入并请求打断；空闲时提问拉起一轮、汇报排队
    mail_kind = (
        mailbox.MailKind.SUBSESSION_QUESTION if kind == "question"
        else mailbox.MailKind.SUBSESSION_REPORT
    )
    try:
        res = await mailbox.publish(
            mail_kind, session_id=root, text=text,
            source_session_id=subsession_id, question_id=question_id, persist=False,
        )
    except Exception:  # noqa: BLE001 — 投递异常按已排队处理（消息已落库）
        logger.warning("[subsession] 邮箱投递失败 sid=%s", subsession_id, exc_info=True)
        res = {"delivered": "queued"}
    res["question_id"] = question_id
    return res


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
