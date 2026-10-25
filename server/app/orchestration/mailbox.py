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
    # plan-549-2281：子代理提问。子会话下线后「能打断主代理等待的求助」全部改由子代理承担——
    # 旧实现里子代理提问只进 _pending_inputs（要等下一次 LLM 调用），主代理正在跑前台命令或
    # wait 轮询时看不到，子代理白白阻塞到超时。
    SUBAGENT_QUESTION = "subagent_question"
    # plan-549-2281 修复：子代理进度上报。此前只进 _pending_inputs（不落库），前端为它准备的
    # 「子代理进度上报」行永远等不到数据；改走邮箱落库成行后，提问 / 进度上报 / 完成通知三类
    # 子代理消息才真正「在消息流里成行展示」。
    SUBAGENT_REPORT = "subagent_report"
    MAIN_REPLY = "main_reply"
    BG_COMMAND_DONE = "bg_command_done"
    # plan-20261020-bgtask：用户在界面上停掉了后台命令——只需让会话知道，不值得叫醒空闲会话
    BG_COMMAND_STOPPED = "bg_command_stopped"
    SUBAGENT_COMPLETION = "subagent_completion"


# 优先级：数字越小越先投递
PRIORITY: dict[str, int] = {
    MailKind.SUBSESSION_QUESTION.value: 0,
    MailKind.SUBAGENT_QUESTION.value: 0,
    MailKind.MAIN_REPLY.value: 0,
    MailKind.BG_COMMAND_DONE.value: 0,
    MailKind.SUBSESSION_RESULT.value: 1,
    MailKind.SUBSESSION_REPORT.value: 2,
    MailKind.SUBAGENT_REPORT.value: 2,
    MailKind.BG_COMMAND_STOPPED.value: 2,
    MailKind.SUBAGENT_COMPLETION.value: 2,
}

# plan-549-2281 修复：这些 kind 落库时必须带 SUBSESSION_REPORT_FLAG（历史回放按「用户输入」
# 呈现——它们是外部事件，不是主代理说过的话），并把落库消息 id 回填到队列项——
# 否则 message_id=None 使 take_pending 的 replayed_upto 去重永不生效，
# 同一条提问会「注入 + 历史回放」双份出现（旧 subsession_service 靠预落库实现这两点，
# 迁移到 mailbox.publish 时丢了）。
#
# plan-560-2311 B2：`SUBAGENT_COMPLETION` 已移出本集合。子代理完成通知的**模型通道**只有
# `subagent._completion_queue`（`_format_completion_note`，经 drain_completions 注入）；
# 邮箱这一份是给消息流前端渲染「子代理已结束」行的，此前两边都在回放 / 注入，模型对同一个
# 子代理会收到两份（note 没有 message_id，`_history_cutoff` 去重对它不生效）。
_INPUT_REPLAY_KINDS = frozenset({
    MailKind.SUBAGENT_QUESTION.value,
    MailKind.SUBAGENT_REPORT.value,
})

# plan-560-2311 B2：纯展示类消息——落库成行给前端渲染，但**不进模型上下文**：
# 不注入运行中的 turn、不据它唤醒空闲会话、也不随 take_pending 并入下一轮。
# 模型侧的信息由各自的专用通道承担（子代理完成通知见 `subagent._completion_queue`）。
_DISPLAY_ONLY_KINDS = frozenset({
    MailKind.SUBAGENT_COMPLETION.value,
})

# plan-565-2323 G16：**系统注入的运行事件**——外部世界发生的事（后台命令跑完 / 被用户停掉），
# 不是主代理说过的话。它们必须进模型上下文（模型得知道自己起的命令结束了），但绝不能
# 走「暂存为 agent 文本」那条路：`context_manager` 的 `_pending_agent_text` 是**覆盖写**，
# 这类行落进去会把主代理刚说的一段话整段吞掉，模型随后会以为自己说过「后台命令已完成」。
#
# 所以它们落库时带一个等价标记，装配历史时按「user 侧注入」回放（与 SUBSESSION_REPORT_FLAG
# 同一处理分支）。为什么不直接并入 `_INPUT_REPLAY_KINDS`：那个集合的语义是「需要主代理
# 回应的子代理消息」，后台命令完成不需要回应，混在一起会让两处判据互相牵制。
SYSTEM_EVENT_FLAG = "system_event"
_DISPLAY_ONLY_FLAG = "display_only"

_SYSTEM_EVENT_KINDS = frozenset({
    MailKind.BG_COMMAND_DONE.value,
    MailKind.BG_COMMAND_STOPPED.value,
})


def classify_persisted(content: dict | None) -> str:
    """判定一条**已落库**的系统消息在历史装配里该怎么回放（plan-565-2323 G16）。

    返回：
      · `"display_only"`：纯展示行，模型上下文里**不出现**（子代理完成通知——模型侧
        信息由 `subagent._completion_queue` 承担，两份就是重复通知）；
      · `"system_event"`：系统注入的运行事件（后台命令完成 / 被停止），按 **user 侧注入**
        回放——模型必须知道，但它不是主代理说过的话，绝不能进 `_pending_agent_text`；
      · `"input"`：需要主代理回应的消息（子代理提问 / 进度上报），按 user 输入回放；
      · `""`：不适用（普通消息，交给调用方原有分支）。

    为什么在这里判而不是在装配处判：标记与 kind 的对应关系只应该有一份。落库时写的标记
    （`SYSTEM_EVENT_FLAG` / `_DISPLAY_ONLY_FLAG` / `SUBSESSION_REPORT_FLAG`）是本判据的主依据；
    `mailbox.kind` 是**兜底**——本改动之前落库的老行没有新标记，但一直带着 `mailbox.kind`，
    靠它也能正确回放，不必回头修历史数据。
    """
    if not isinstance(content, dict):
        return ""
    mail_kind = ""
    meta = content.get("mailbox")
    if isinstance(meta, dict):
        mail_kind = str(meta.get("kind") or "")

    if content.get(_DISPLAY_ONLY_FLAG) is True or mail_kind in _DISPLAY_ONLY_KINDS:
        return "display_only"
    if content.get(SYSTEM_EVENT_FLAG) is True or mail_kind in _SYSTEM_EVENT_KINDS:
        return "system_event"
    # SUBSESSION_REPORT_FLAG 由调用方（context_manager）自行判定，这里不重复其语义
    if mail_kind in _INPUT_REPLAY_KINDS:
        return "input"
    return ""

# plan-20261020-bgtask：会让运行中主代理「让出等待」的类型。
# 只有延迟敏感的消息才值得打断——子代理 / 子会话正阻塞等主代理答复。后台命令跑完不急：
# 注入后下一次 LLM 调用前就能读到，没必要为它把前台命令转后台、或中断一次 wait 轮询。
INTERRUPTING_KINDS = frozenset({
    MailKind.SUBSESSION_QUESTION.value,
    MailKind.SUBAGENT_QUESTION.value,
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
    # plan-20261025-bgturn / plan-20261026-notice-turn：事件归属的 turn。撤回该轮时既要按它
    # 把仍在队列里、尚未投递的通知丢掉，也要让落库副本随 `turn_id >= 目标轮` 一起软删。
    # None = 与轮次无关（这类消息不参与撤回）。
    turn_id: int | None = None


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
    """入队；同来源同类型且在合并窗口内 → 合并成一条（只影响投递文本，落库各自成条）。

    plan-549-2281 修复：合并**不得污染可回放性**。`take_pending` 的去重条件是
    `it.message_id is None or it.message_id > replayed_upto`，None 会被无条件保留——
    旧实现遇到一侧 message_id 为空（落库失败一次即可）就把合并项的 id 清成 None，
    于是这条既已随历史回放、又被注入一次。现在两条规则：
      ① 只有两侧都带 message_id 才合并（身份不明的项另起一条，各自按自己的 id 判定）；
      ② 合并时取「已知的最大 id」，不因任一侧为空而丢身份。
    """
    global _seq_counter
    q = _queues.setdefault(item.session_id, [])
    now = item.created_at
    for existing in reversed(q):
        if (existing.kind == item.kind
                and existing.source_session_id == item.source_session_id
                and existing.question_id is None
                and item.question_id is None
                and (existing.message_id is not None) == (item.message_id is not None)
                and now - existing.created_at <= MERGE_WINDOW_S):
            existing.text = f"{existing.text}\n{item.text}"
            existing.created_at = now
            existing.wake = existing.wake or item.wake
            # 合并后取已知的最大 id 作为判定线（两侧同为空时仍是 None：都只能靠注入送达）
            existing.message_id = max(
                filter(lambda v: v is not None, (existing.message_id, item.message_id)),
                default=None,
            )
            return
    _seq_counter += 1
    item.seq = _seq_counter
    q.append(item)
    # 超限：丢一条**最不重要的**（控制面消息不丢）。
    # plan-562-2318 M16：原先按「第一条 priority > 0」挑受害者，实际丢掉的可能是 P1 的
    # 派发结论（SUBSESSION_RESULT）而不是最旧的 P2 汇报，与注释语义相反。现在按
    # 「优先级最低者优先被丢；同优先级里丢最旧的」，与 `_sorted_queue` 的排序口径一致。
    if len(q) > MAX_ITEMS_PER_SESSION:
        victim = max(range(len(q)), key=lambda i: (q[i].priority, -i))
        if q[victim].priority > 0:
            dropped = q.pop(victim)
            logger.warning("[mailbox] session=%s 队列超限，丢弃优先级最低的旧消息 kind=%s",
                           item.session_id, dropped.kind)


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
        questions = sum(1 for it in replayed
                        if it.kind in (MailKind.SUBSESSION_QUESTION.value,
                                       MailKind.SUBAGENT_QUESTION.value))
        results = sum(1 for it in replayed
                      if it.kind == MailKind.SUBSESSION_RESULT.value)
        # plan-560-2311 B2：`SUBAGENT_COMPLETION` 已改为纯展示、不进模型上下文，
        # 不会出现在 drained / replayed 里；子代理结论由 _completion_queue 注入。
        hint = f"[系统提醒] 你不在运行时收到 {len(replayed)} 条任务消息，原文就在上方，请逐条读完再决定下一步。"
        if questions:
            # plan-549-2281：子会话的 send_to_subsession 已随能力下线删除，
            # 提问统一由子代理承担。send_to_subagent 没有 question_id 参数——
            # 答复按 agent_id 自动配对到该子代理当前的待答提问（subagent_comm.pending_question_of）。
            hint += "其中有提问在等你答复：用 send_to_subagent(agent_id, message) 回复。"
        if results:
            hint += "已结束的子任务请整合它们的结论继续推进；全部完成就向用户汇报结果。"
        parts.append(hint)
    if len(fresh) == 1 and not replayed:
        return fresh[0].text
    if fresh:
        parts.append(f"[系统提醒] 你不在运行时收到 {len(fresh)} 条消息，请先逐条读完再决定下一步：")
        parts.extend(f"---\n{it.text}" for it in fresh)
    return "\n".join(parts)


async def _turn_rolled_back(db, turn_id: int) -> bool:
    """归属轮次是否已撤回（plan-20261026-notice-turn）。

    撤回后仍可能有迟到事件（子代理取消收尾、后台命令退出）触发通知：此时落库只会留下
    一条无人再清理的孤儿行——撤回的主软删与兜底补删都已经执行过。落库前据此拦一道。
    读失败按「未撤回」处理：不能因为一次状态查询失败就吞掉用户该看到的通知。
    """
    try:
        from app.persistence.models.turn import Turn
        row = await db.get(Turn, int(turn_id))
    except Exception:  # noqa: BLE001 — 查询失败不影响通知本身
        logger.debug("[mailbox] 轮次状态查询失败 turn=%s", turn_id, exc_info=True)
        return False
    return row is not None and str(getattr(row, "status", "") or "") == "rolled_back"


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
    turn_id: int | None = None,
) -> dict:
    """投递一条消息：先落库（可失败），再入队并尝试立即送达。

    `extra`：并入落库消息 content 的结构化字段（如后台命令的 `bg_items`），前端据此渲染，
    不必再从摘要文本里反向解析。返回 `{"delivered": "injected"|"woken"|"queued", ...}`，
    供来源方在工具结果里说明。
    `wake`：会话空闲时也为它拉起一轮（P0 本来就会）；`message_id`：来源方已自行落库、且历史会回放的消息 id。
    `turn_id`：plan-20261025-bgturn / plan-20261026-notice-turn：事件归属的轮次。落库时写进消息的
    `turn_id`（撤回该轮时随之软删，不再残留成永远删不掉的「孤儿提示」），同时记在队列项上供
    `drop_from_turn` 清理；该轮已撤回时直接丢弃，连落库都不做。
    """
    kind_value = kind.value if isinstance(kind, MailKind) else str(kind)
    body = (text or "").strip()
    if not body or session_id <= 0:
        return {"delivered": "ignored"}

    if persist:
        try:
            from app.core.enums import MsgType, SenderType
            from app.orchestration.subsession_report import SUBSESSION_REPORT_FLAG
            from app.persistence.database import async_session_factory
            from app.services import message_service

            _replay_flag = kind_value in _INPUT_REPLAY_KINDS
            # plan-565-2323 G16：三类落库标记互斥，各管一件事——
            #   · 子代理提问/进度上报 → SUBSESSION_REPORT_FLAG（按 user 输入回放，需要主代理回应）；
            #   · 后台命令完成/被停止 → SYSTEM_EVENT_FLAG（系统注入的运行事件，同样按 user 侧回放，
            #     但**不得**复用 `_pending_agent_text`，那条路是覆盖写，会吞掉主代理刚说的话）；
            #   · 子代理完成通知 → 纯展示（不进模型上下文，模型侧由 `_completion_queue` 承担）。
            _system_event = kind_value in _SYSTEM_EVENT_KINDS
            _display_only = kind_value in _DISPLAY_ONLY_KINDS
            async with async_session_factory() as db:
                # plan-20261026-notice-turn：撤回该轮后，子代理取消收尾等迟到事件仍可能再来一次；
                # 此时撤回的软删已经跑完，落库只会留下删不掉的孤儿行，直接丢弃。
                if turn_id is not None and await _turn_rolled_back(db, int(turn_id)):
                    logger.info("[mailbox] 轮次已撤回，丢弃通知 session=%s turn=%s kind=%s",
                                session_id, turn_id, kind_value)
                    return {"delivered": "ignored", "reason": "turn_rolled_back"}
                row = await message_service.create_message(
                    db, session_id=int(session_id),
                    sender_type=SenderType.SYSTEM.value,
                    msg_type=MsgType.TEXT.value,
                    # plan-20261025-bgturn：带上来源轮次，撤回该轮时这条系统行才会一起被软删。
                    # 此前恒为 None，回滚按 `turn_id >= 目标轮` 删消息永远匹配不到它。
                    turn_id=int(turn_id) if turn_id is not None else None,
                    content={
                        **(extra or {}),
                        "text": body,
                        **({SUBSESSION_REPORT_FLAG: True} if _replay_flag else {}),
                        # 纯展示标记同时写进落库内容：`SUBAGENT_COMPLETION` 不带回放标记
                        # （它不参与模型上下文），若装配历史时无标记可依，它就会落进
                        # `_pending_agent_text` 覆盖主代理刚说的话——这正是本标记要堵的缺口。
                        **({SYSTEM_EVENT_FLAG: True} if _system_event else {}),
                        **({_DISPLAY_ONLY_FLAG: True} if _display_only else {}),
                        "mailbox": {
                            "kind": kind_value,
                            "priority": PRIORITY.get(kind_value, 2),
                            "source_session_id": source_session_id,
                            "question_id": question_id,
                        },
                    },
                )
                if message_id is None:
                    message_id = int(getattr(row, "id", 0) or 0) or None
        except Exception:  # noqa: BLE001 — 落库失败不阻断来源方（内存队列仍可投递）
            logger.warning("[mailbox] 落库失败 session=%s kind=%s", session_id, kind_value, exc_info=True)

    item = MailItem(
        kind=kind_value, session_id=int(session_id), text=body,
        priority=PRIORITY.get(kind_value, 2),
        source_session_id=source_session_id, question_id=question_id,
        wake=bool(wake), message_id=message_id,
        turn_id=int(turn_id) if turn_id is not None else None,
    )
    _push(item)
    delivered = await deliver(int(session_id))
    # plan-562-2318 M16b：纯展示类消息只落库给前端渲染，`deliver` 会立刻把它从队列摘掉，
    # 于是返回 `{"delivered": "empty"}`——语义误导（消息其实已落库、会出现在消息流里，
    # 说"空"会让调用方误判没送达）。这里如实标注。
    if (delivered.get("delivered") == "empty"
            and kind_value in _DISPLAY_ONLY_KINDS):
        return {"delivered": "display_only", "count": 1}
    return delivered


def _drop_display_only(session_id: int) -> None:
    """摘除队列里的纯展示类消息（plan-560-2311 B2）。

    它们已由 `publish` 落库成行（前端消息流据此渲染），而模型侧的信息走各自的专用通道。
    这里在投递阶段摘掉，使它既不会被 `inject_input` 注入运行中的 turn，也不会因
    `priority == 0 or wake` 判定拉起空闲会话，更不会被 `take_pending` 带进下一轮。
    """
    q = _queues.get(session_id)
    if not q:
        return
    kept = [it for it in q if it.kind not in _DISPLAY_ONLY_KINDS]
    if len(kept) == len(q):
        return
    if kept:
        _queues[session_id] = kept
    else:
        _queues.pop(session_id, None)


async def deliver(session_id: int) -> dict:
    """按会话当前状态投递队列：运行中注入（P0 额外请求打断），空闲拉起一轮。"""
    _drop_display_only(session_id)
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


def drop_from_turn(session_id: int, turn_id: int) -> int:
    """丢掉队列里归属于该 turn 及之后轮次的待投递消息（plan-20261025-bgturn）。

    撤回某一轮后，这一轮的后台命令通知、子代理提问 / 进度上报 / 完成通知都已经失去意义：
    消息层那一份会随 `turn_id >= turn_id` 一起软删，队列这一份不清理就会被下一轮
    `take_pending` 带走，在消息流里重新弹出一条属于已撤销轮的提示。
    落库副本同样软删（`mark_notices_deleted`），两步合起来才算撤回干净。
    返回丢弃条数。
    """
    q = _queues.get(session_id)
    if not q:
        return 0
    kept = [it for it in q if it.turn_id is None or it.turn_id < int(turn_id)]
    dropped = len(q) - len(kept)
    if not dropped:
        return 0
    if kept:
        _queues[session_id] = kept
    else:
        _queues.pop(session_id, None)
    logger.info("[mailbox] session=%s 撤回 turn=%s，丢弃待投递消息 %d 条",
                session_id, turn_id, dropped)
    return dropped


def take_pending(session_id: int, *, replayed_upto: int | None = None) -> list[dict]:
    """取走该会话队列里的全部消息，转成注入项（turn 启动时并入上下文）。

    P2 汇报不会主动唤醒会话，靠这里在下一次 turn（用户发消息 / 其他唤醒）时带走。
    `replayed_upto`：本轮历史装配时已读到的最大消息 id——落库副本不超过它的条目已在历史里回放，丢弃不注入。
    """
    # 与投递同口径：控制面消息排在前面；纯展示类消息不进模型上下文（plan-560-2311 B2）
    items = sorted(_drain(session_id), key=lambda it: (it.priority, it.seq))
    items = [it for it in items if it.kind not in _DISPLAY_ONLY_KINDS]
    if replayed_upto is not None:
        items = [it for it in items if it.message_id is None or it.message_id > replayed_upto]
    return [_inject_item(it) for it in items]


async def mark_notices_deleted(session_id: int, turn_id: int) -> int:
    """把该会话里归属于 `turn_id` 及之后轮次的系统通知消息软删（plan-20261025-bgturn）。

    `rollback_service.rollback_turn` 的软删条件本来就覆盖 `turn_id >= turn_id`，这一份是
    兜底：通知的落库与回滚可能交错（回滚先取了消息列表，通知随后才落库），补删一次
    才不会留下一条「撤回后又冒出来」的提示。

    plan-20261026-notice-turn：判据从「系统事件类」放宽为**本模块落库的全部通知**
    （content 带 `mailbox` 来源标记）——子代理提问 / 进度上报 / 完成行此前落库不带
    turn_id，撤回时既匹配不到主软删、也不在补删范围内，于是撤回首条消息后
    「【子代理进度上报…】」「【子代理已完成…】」仍留在消息流里。只动本模块写的通知，
    不碰用户 / 代理正文。返回更新条数。
    """
    try:
        from sqlalchemy import select, update as _update

        from app.persistence.database import run_write_locked
        from app.persistence.models.message import Message

        def _is_mailbox_notice(content) -> bool:
            # 落库时 publish 一律写入 content["mailbox"] 来源块；用它识别「本模块写的通知」，
            # 不依赖具体 kind——新增通知类型不会漏清理。
            return isinstance(content, dict) and isinstance(content.get("mailbox"), dict)

        def _persist(s) -> int:
            # 先按 turn 范围取候选，再只留本模块落库的通知：
            # 直接按 turn_id 整批软删会把模型正文 / 用户消息一起误删（那是 rollback 自身的职责）。
            rows = s.execute(
                select(Message.id, Message.content).where(
                    Message.session_id == int(session_id),
                    Message.deleted == False,  # noqa: E712
                    Message.turn_id.is_not(None),
                    Message.turn_id >= int(turn_id),
                )
            ).all()
            ids = [mid for mid, content in rows if _is_mailbox_notice(content)]
            if not ids:
                s.commit()
                return 0
            s.execute(_update(Message).where(Message.id.in_(ids)).values(deleted=True))
            s.commit()
            return len(ids)

        count = await run_write_locked(_persist, label=f"mailbox.notice_deleted.{turn_id}")
        if count:
            logger.info("[mailbox] session=%s 撤回 turn=%s，补删通知消息 %d 条",
                        session_id, turn_id, count)
        return count
    except Exception:  # noqa: BLE001 — 兜底清理失败不影响回滚主流程
        logger.warning("[mailbox] 撤回后补删通知失败 session=%s turn=%s",
                       session_id, turn_id, exc_info=True)
        return 0


def snapshot() -> dict[int, int]:
    """全部会话的待投递数量（诊断用）。"""
    return {sid: len(items) for sid, items in _queues.items() if items}


def reset_for_tests() -> None:
    _queues.clear()
    global _seq_counter
    _seq_counter = 0
