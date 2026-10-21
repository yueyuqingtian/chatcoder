"""会话邮箱（plan-474-2105）。

【为什么需要一层邮箱】
子会话提问、主代理答复、后台命令完成这三类消息都可能落在「会话正忙」的时刻：
旧实现只有一条 `engine.inject_input` 队列，且只在**每次 LLM 调用前**才被 drain——
主代理正在跑前台命令 / 等 sleep / 轮询 `wait=true` 时完全看不到，子会话就白等。

本模块把「消息」与「投递时机」分开：
  · 先按优先级入队（并先落库，进程重启也不丢）；
  · 再按会话运行态决定投递方式：运行中 → 注入 + 必要时请求打断；空闲 → 拉起一轮；
  · 两类都失败时留在队列里，由下一次投递机会（新 turn 启动 / 用户发消息）带走。

【优先级】
0 = 控制面：子会话提问、主代理答复、后台命令完成——必须尽快让主代理看到；
2 = 完成汇报：子会话结论、子代理完成通知——可以等，不必打断。
同优先级 FIFO；控制面消息在打断判定里拥有让出等待的权利。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class MailKind(str, Enum):
    SUBSESSION_QUESTION = "subsession_question"
    SUBSESSION_REPORT = "subsession_report"
    MAIN_REPLY = "main_reply"
    BG_COMMAND_DONE = "bg_command_done"
    # plan-20261020-bgtask：用户在界面上停掉了后台命令——只需让会话知道，不值得叫醒空闲会话
    BG_COMMAND_STOPPED = "bg_command_stopped"
    SUBAGENT_COMPLETION = "subagent_completion"


# 优先级：数字越小越先投递
PRIORITY: dict[str, int] = {
    MailKind.SUBSESSION_QUESTION.value: 0,
    MailKind.MAIN_REPLY.value: 0,
    MailKind.BG_COMMAND_DONE.value: 0,
    MailKind.SUBSESSION_REPORT.value: 2,
    MailKind.BG_COMMAND_STOPPED.value: 2,
    MailKind.SUBAGENT_COMPLETION.value: 2,
}

# plan-20261020-bgtask：会让运行中主代理「让出等待」的类型。
# 只有延迟敏感的消息才值得打断——子会话正阻塞等主代理答复。后台命令跑完不急：
# 注入后下一次 LLM 调用前就能读到，没必要为它把前台命令转后台、或中断一次 wait 轮询。
INTERRUPTING_KINDS = frozenset({
    MailKind.SUBSESSION_QUESTION.value,
    MailKind.MAIN_REPLY.value,
})

# 同来源同类型的合并窗口（秒）：子会话连续汇报 / 多个后台命令同时结束时不刷屏
MERGE_WINDOW_S = 2.0
# 队列上限（每会话）：防止异常来源无限堆积；超出时丢弃最旧的完成汇报（控制面不丢）
MAX_ITEMS_PER_SESSION = 50


@dataclass
class MailItem:
    kind: str
    session_id: int
    text: str
    priority: int = 2
    source_session_id: int | None = None
    question_id: str | None = None
    created_at: float = field(default_factory=time.time)
    seq: int = 0


# session_id → 按 (priority, seq) 排序的待投递项
_queues: dict[int, list[MailItem]] = {}
_seq_counter = 0


def _next_seq() -> int:
    global _seq_counter
    _seq_counter += 1
    return _seq_counter


def _sorted_queue(session_id: int) -> list[MailItem]:
    return sorted(_queues.get(session_id) or [], key=lambda it: (it.priority, it.seq))


def _push(item: MailItem) -> None:
    """入队；同来源同类型且在合并窗口内 → 合并成一条（只影响投递文本，落库各自成条）。"""
    global _seq_counter
    q = _queues.setdefault(item.session_id, [])
    now = item.created_at
    for existing in reversed(q):
        if (existing.kind == item.kind
                and existing.source_session_id == item.source_session_id
                and existing.question_id is None
                and item.question_id is None
                and now - existing.created_at <= MERGE_WINDOW_S):
            existing.text = f"{existing.text}\n{item.text}"
            existing.created_at = now
            return
    _seq_counter += 1
    item.seq = _seq_counter
    q.append(item)
    # 超限：丢弃最旧的完成汇报（控制面消息不丢）
    if len(q) > MAX_ITEMS_PER_SESSION:
        for i, it in enumerate(q):
            if it.priority > 0:
                q.pop(i)
                logger.warning("[mailbox] session=%s 队列超限，丢弃最旧汇报 kind=%s",
                               item.session_id, it.kind)
                break


def _drain(session_id: int) -> list[MailItem]:
    return _queues.pop(session_id, None) or []


def _render(item: MailItem) -> str:
    return item.text


def _render_batch(items: list[MailItem]) -> str:
    """空闲唤醒时把整批消息合成一段提示词（保留每条来源与类型）。"""
    if len(items) == 1:
        return items[0].text
    lines = [f"[系统提醒] 你不在运行时收到 {len(items)} 条消息，请先逐条读完再决定下一步："]
    lines.extend(f"\n---\n{it.text}" for it in items)
    return "".join(lines)


async def publish(
    kind: MailKind | str,
    *,
    session_id: int,
    text: str,
    source_session_id: int | None = None,
    question_id: str | None = None,
    persist: bool = True,
    extra: dict | None = None,
) -> dict:
    """投递一条消息：先落库（可失败），再入队并尝试立即送达。

    `extra`：并入落库消息 content 的结构化字段（如后台命令的 `bg_items`），前端据此渲染，
    不必再从摘要文本里反向解析。返回 `{"delivered": "injected"|"woken"|"queued", ...}`，
    供来源方在工具结果里说明。
    """
    kind_value = kind.value if isinstance(kind, MailKind) else str(kind)
    body = (text or "").strip()
    if not body or session_id <= 0:
        return {"delivered": "ignored"}

    if persist:
        try:
            from app.core.enums import MsgType, SenderType
            from app.persistence.database import async_session_factory
            from app.services import message_service

            async with async_session_factory() as db:
                await message_service.create_message(
                    db, session_id=int(session_id),
                    sender_type=SenderType.SYSTEM.value,
                    msg_type=MsgType.TEXT.value,
                    content={
                        **(extra or {}),
                        "text": body,
                        "mailbox": {
                            "kind": kind_value,
                            "priority": PRIORITY.get(kind_value, 2),
                            "source_session_id": source_session_id,
                            "question_id": question_id,
                        },
                    },
                )
        except Exception:  # noqa: BLE001 — 落库失败不阻断来源方（内存队列仍可投递）
            logger.warning("[mailbox] 落库失败 session=%s kind=%s", session_id, kind_value, exc_info=True)

    item = MailItem(
        kind=kind_value, session_id=int(session_id), text=body,
        priority=PRIORITY.get(kind_value, 2),
        source_session_id=source_session_id, question_id=question_id,
    )
    _push(item)
    return await deliver(int(session_id))


async def deliver(session_id: int) -> dict:
    """按会话当前状态投递队列：运行中注入（P0 额外请求打断），空闲拉起一轮。"""
    items = _sorted_queue(session_id)
    if not items:
        return {"delivered": "empty"}

    from app.orchestration import engine

    turn_id = engine.running_turn_id(session_id)
    if turn_id is not None:
        drained = _drain(session_id)
        delivered = 0
        interrupt_kind: str | None = None
        for it in drained:
            ok = engine.inject_input(turn_id, {"content": _render(it), "priority": it.priority})
            if not ok:
                _push(it)  # 注入与收尾同刻失败 → 回队，等下个机会，不丢
                continue
            delivered += 1
            if interrupt_kind is None and it.kind in INTERRUPTING_KINDS:
                interrupt_kind = it.kind
        if interrupt_kind is not None:
            # 打断判定交给 engine：没有可打断的等待、写文件 / 等用户回答时不打断，只在动作结束后注入；
            # source 记下是谁的消息，让出等待的文案才不会把别的来源说成「子会话消息」
            engine.request_interrupt(turn_id, reason="mailbox", source=interrupt_kind)
        return {"delivered": "injected", "turn_id": turn_id, "count": delivered}

    # 会话空闲：只有控制面消息（P0）才值得立刻拉起一轮——汇报类（P2）排队等下一轮带走，
    # 否则子会话反复汇报会把空闲会话叫醒，白烧 token。
    drained = _drain(session_id)
    if not any(it.priority == 0 for it in drained):
        for it in drained:
            _push(it)
        return {"delivered": "queued", "count": len(drained)}
    prompt = _render_batch(drained)
    reason = drained[0].kind
    try:
        new_turn = await engine.wakeup_session(session_id, prompt=prompt, reason=reason)
    except Exception:  # noqa: BLE001 — 唤醒失败不能丢消息
        logger.warning("[mailbox] 唤醒失败 session=%s", session_id, exc_info=True)
        new_turn = None
    if new_turn is None:
        for it in drained:
            _push(it)
        return {"delivered": "queued", "count": len(drained)}
    return {"delivered": "woken", "turn_id": new_turn, "count": len(drained)}


def pending_count(session_id: int) -> int:
    """该会话尚未投递的消息数（前端角标 / 诊断用）。"""
    return len(_queues.get(session_id) or [])


def take_pending(session_id: int) -> list[dict]:
    """取走该会话队列里的全部消息，转成注入项（turn 启动时并入上下文）。

    P2 汇报不会主动唤醒会话，靠这里在下一次 turn（用户发消息 / 其他唤醒）时带走。
    """
    items = _drain(session_id)
    return [{"content": _render(it)} for it in items]


def snapshot() -> dict[int, int]:
    """全部会话的待投递数量（诊断用）。"""
    return {sid: len(items) for sid, items in _queues.items() if items}


def reset_for_tests() -> None:
    _queues.clear()
    global _seq_counter
    _seq_counter = 0
