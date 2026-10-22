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
1 = 派发结论：主代理派出去的子会话跑完——不打断运行中的主代理，但它空闲时要叫醒它汇总；
2 = 完成汇报：子会话进度、子代理完成通知——可以等，不必打断。
同优先级 FIFO；控制面消息在打断判定里拥有让出等待的权利。

【去重（plan-20261022-release-ci）】
子会话的提问 / 汇报 / 结论会先落成主会话里的一条系统消息，下一轮上下文装配会把它当作一条
用户输入回放；队列里的同一条消息带着那条落库消息的 id（message_id），轮次开始时已被历史回放的
就不再重复注入，空闲唤醒的提示词里也只写一句「见上方」，不再把原文再贴一遍。
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
    # plan-20261022-release-ci：子会话一轮跑完的结论（此前与进度汇报同为 P2，主代理空闲时只排队，
    # 派出去的子会话全部完成了也没人叫醒主代理汇总，结论一直躺在队列里等用户下一句话）
    SUBSESSION_RESULT = "subsession_result"
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
    MailKind.SUBSESSION_RESULT.value: 1,
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
    # 会话空闲时是否值得为它拉起一轮（P0 一律拉起；P1 / P2 由来源方按场景决定）
    wake: bool = False
    # 已落库、且下一轮上下文会把它作为用户输入回放的那条消息 id；None = 只能靠注入送达
    message_id: int | None = None


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
            existing.wake = existing.wake or item.wake
            # 合并后只有两段都会被历史回放时才算可回放（取较大的 id 作为判定线）
            existing.message_id = (
                max(existing.message_id, item.message_id)
                if existing.message_id is not None and item.message_id is not None else None
            )
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


def _inject_item(item: MailItem) -> dict:
    """注入项：带上来源标记（不再附加给用户消息的并行提醒）与回放判定用的 message_id。"""
    return {
        "content": _render(item),
        "priority": item.priority,
        "source": "mailbox",
        "message_id": item.message_id,
    }


def _render_batch(items: list[MailItem]) -> str:
    """空闲唤醒时把整批消息合成一段提示词（保留每条来源与类型）。

    plan-20261022-release-ci：会被历史回放的消息（message_id 非空）已经在上方，提示词只指个方向，
    不再把原文贴第二遍——否则结论原文（可达上万字）在同一轮上下文里出现两次。
    """
    fresh = [it for it in items if it.message_id is None]
    replayed = [it for it in items if it.message_id is not None]
    parts: list[str] = []
    if replayed:
        questions = sum(1 for it in replayed if it.kind == MailKind.SUBSESSION_QUESTION.value)
        results = sum(1 for it in replayed if it.kind == MailKind.SUBSESSION_RESULT.value)
        hint = f"[系统提醒] 你不在运行时收到 {len(replayed)} 条子会话消息，原文就在上方，请逐条读完再决定下一步。"
        if questions:
            hint += "其中有提问在等你答复：用 send_to_subsession 带上对应的 question_id 回复。"
        if results:
            hint += "已结束的子会话请整合它们的结论继续推进；全部完成就向用户汇报结果。"
        parts.append(hint)
    if len(fresh) == 1 and not replayed:
        return fresh[0].text
    if fresh:
        parts.append(f"[系统提醒] 你不在运行时收到 {len(fresh)} 条消息，请先逐条读完再决定下一步：")
        parts.extend(f"---\n{it.text}" for it in fresh)
    return "\n".join(parts)


async def publish(
    kind: MailKind | str,
    *,
    session_id: int,
    text: str,
    source_session_id: int | None = None,
    question_id: str | None = None,
    persist: bool = True,
    extra: dict | None = None,
    wake: bool = False,
    message_id: int | None = None,
) -> dict:
    """投递一条消息：先落库（可失败），再入队并尝试立即送达。

    `extra`：并入落库消息 content 的结构化字段（如后台命令的 `bg_items`），前端据此渲染，
    不必再从摘要文本里反向解析。返回 `{"delivered": "injected"|"woken"|"queued", ...}`，
    供来源方在工具结果里说明。
    `wake`：会话空闲时也为它拉起一轮（P0 本来就会）；`message_id`：来源方已自行落库、且历史会回放的消息 id。
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
        wake=bool(wake), message_id=message_id,
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
            ok = engine.inject_input(turn_id, _inject_item(it))
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

    # 会话空闲：控制面消息（P0）与来源方要求唤醒的（派出去的子会话全部结束）才拉起一轮——
    # 进度汇报（P2）排队等下一轮带走，否则子会话反复汇报会把空闲会话叫醒，白烧 token。
    # 按优先级排好：唤醒原因（前端分隔线文案）取最紧急的那一类
    drained = sorted(_drain(session_id), key=lambda it: (it.priority, it.seq))
    if not any(it.priority == 0 or it.wake for it in drained):
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


def take_pending(session_id: int, *, replayed_upto: int | None = None) -> list[dict]:
    """取走该会话队列里的全部消息，转成注入项（turn 启动时并入上下文）。

    P2 汇报不会主动唤醒会话，靠这里在下一次 turn（用户发消息 / 其他唤醒）时带走。
    `replayed_upto`：本轮历史装配时已读到的最大消息 id——落库副本不超过它的条目已在历史里回放，丢弃不注入。
    """
    # 与投递同口径：控制面消息排在前面
    items = sorted(_drain(session_id), key=lambda it: (it.priority, it.seq))
    if replayed_upto is not None:
        items = [it for it in items if it.message_id is None or it.message_id > replayed_upto]
    return [_inject_item(it) for it in items]


def snapshot() -> dict[int, int]:
    """全部会话的待投递数量（诊断用）。"""
    return {sid: len(items) for sid, items in _queues.items() if items}


def reset_for_tests() -> None:
    _queues.clear()
    global _seq_counter
    _seq_counter = 0
