"""子代理 ↔ 主代理的提问/答复通道（plan-549-2281）。

【为什么需要独立一层】
子会话（subsession）下线前，它有两套能力是子代理没有的：
  · 提问能**打断**主代理的次要等待（走 `mailbox` P0 + `engine.request_interrupt`）；
  · 提问有 `question_id`，答复能精确唤醒等待方，超时/丢弃也有生命周期。
子代理旧实现只有一条「写进 `_pending_inputs`，等下一次 LLM 调用前注入」的路径——
主代理正在跑前台命令、或正卡在 `wait=true` 轮询里时根本读不到，子代理只能干等到超时。

本模块把「投递」与「等待」拆开：
  · `publish_question` → 走 mailbox P0，主代理运行中会被打断并立即读到；
  · `wait_reply`       → 子代理阻塞在事件上，直到收到答复或被兜底条件打断（**无总时长上限**）；
  · `resolve_question` → 主代理答复时唤醒等待方，答复同时回注子代理上下文；
  · `discard_question` → 清场（取代 / 撤单 / 会话结束），避免 `_questions` 泄漏。

【等待为什么没有超时上限（plan-565-2323 G16）】
子代理问出问题后，主代理也可能不确定而去问用户，用户可能很久才回答。此时的正确行为是
**继续等**，不是「按最佳判断继续」——旧实现等 600s 就放弃，而且停滞看门狗阈值同样是 600s，
看门狗必然先杀掉子代理（落 failed「长时间无活动」），设计上的优雅降级根本走不到。
兜底不靠时间，靠三条确定性信号：子代理自身被取消、显式终止（`task.cancel()`）、
会话/管理器已结束（调用方传入的 `alive` 判定）。详见 `wait_reply` 的文档。

【与 mailbox 的分工】
mailbox 负责「消息怎么送到主代理」（优先级 / 打断 / 空闲唤醒），本模块负责「问题与答复的
配对」。两者都不持有会话状态，进程内内存即可——与旧 `subsession_service` 的
`_questions` 同口径：进程重启后未答复的提问随子代理一起失效，这与旧行为一致。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# plan-565-2323 G16：等待答复期间「会话是否仍然存在」的轮询粒度（秒）。
# 这不是等待上限，只是存活检查的采样间隔——见 wait_reply 的说明。
_ALIVE_POLL_S = 1.0


@dataclass
class _PendingQuestion:
    """一条等待答复的提问。"""

    question_id: str
    session_id: int
    agent_id: int
    task_title: str
    message: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    reply: str | None = None
    # plan-549-2281 修复：本条提问被同一子代理的新提问取代。旧实现只 pop 不唤醒，
    # 旧 wait_reply 会一直阻塞到 600s 超时；这里单独标记而不用 reply=None 表达，
    # 是为了让语义可分辨——取代不等于「主代理没回」，调用方不会因此重发同一条提问。
    superseded: bool = False


# question_id → 待答复提问
_questions: dict[str, _PendingQuestion] = {}
# (session_id, agent_id) → question_id：同一子代理同一时刻只保留一条待答复提问，
# 避免它连发问题后答复错配。
_by_agent: dict[tuple[int, int], str] = {}


def _key(session_id: int, agent_id: int) -> tuple[int, int]:
    return (int(session_id), int(agent_id))


def _rollback_question(qid: str, session_id: int, agent_id: int) -> None:
    """回滚一条投递失败的提问登记（plan-560-2311 B4）。

    只删仍指向自己的登记：同一子代理可能已在本条投递期间发出了新提问，不能把新登记误删。
    回滚后该 qid 的 `wait_reply` 会立即返回 None（按最佳判断继续），而不是空等到超时。
    """
    item = _questions.pop(qid, None)
    if item is not None and not item.event.is_set():
        item.superseded = True
        item.event.set()
    if _by_agent.get(_key(session_id, agent_id)) == qid:
        _by_agent.pop(_key(session_id, agent_id), None)


async def publish_question(
    *, session_id: int, agent_id: int, task_title: str, message: str,
    wake: bool = True, turn_id: int | None = None,
) -> str:
    """投递一条子代理提问给主代理，返回 question_id。

    P0 优先级：主代理运行中会被要求让出等待（前台命令转后台 / wait 轮询中断），
    空闲时直接拉起一轮——提问是需要决策的，不能躺在队列里等下一条用户消息。

    `turn_id`：plan-20261026-notice-turn：提问归属的主代理轮次。mailbox 落库时写进消息的
    `turn_id`，撤回该轮时提问行随 `turn_id >= 目标轮` 一起软删（此前恒为空，会残留）。
    """
    from app.orchestration import mailbox

    qid = uuid.uuid4().hex[:12]
    item = _PendingQuestion(
        question_id=qid, session_id=int(session_id), agent_id=int(agent_id),
        task_title=str(task_title or ""), message=str(message or ""),
    )
    _questions[qid] = item
    prev = _by_agent.get(_key(session_id, agent_id))
    prev_item = _questions.pop(prev, None) if prev else None
    if prev_item is not None:
        # 同一子代理重复提问：旧问题作废（它已经改为等新的这条答复）。
        # plan-549-2281 修复：作废必须 set() 旧事件——旧实现只 pop 登记项，
        # 旧 wait_reply 不在超时/取消路径上，会一直挂到 600s 超时。
        prev_item.superseded = True
        prev_item.event.set()
    _by_agent[_key(session_id, agent_id)] = qid

    title = f"「{task_title}」" if task_title else ""
    text = f"【子代理提问 subagent#{agent_id}{title}】{message}\n（答复：send_to_subagent {agent_id}）"
    # plan-560-2311 B4：投递失败（含 CancelledError）必须把登记项回滚——旧实现只捕 Exception，
    # 取消路径上 `_questions` / `_by_agent` 会永久留住一条主代理从未收到的提问，
    # 而 `send_to_subagent` 的 `resolve_question` 仍会成功并声称「子代理已恢复」（假唤醒）。
    try:
        await mailbox.publish(
            mailbox.MailKind.SUBAGENT_QUESTION,
            session_id=int(session_id), text=text,
            source_session_id=int(session_id), question_id=qid,
            wake=bool(wake), turn_id=turn_id,
        )
    except asyncio.CancelledError:
        _rollback_question(qid, session_id, agent_id)
        raise  # 取消必须向上传递，不能静默吞掉
    except Exception:  # noqa: BLE001 — 投递失败不能让子代理直接崩，它仍会等到超时
        _rollback_question(qid, session_id, agent_id)
        logger.warning("[subagent_comm] 提问投递失败 agent=%s", agent_id, exc_info=True)
    return qid


async def wait_reply(
    qid: str,
    timeout_s: float | None = None,
    *,
    alive: Callable[[], bool] | None = None,
) -> str | None:
    """等待该提问的答复；未收到答复返回 None（调用方按最佳判断继续）。

    plan-565-2323 G16：**提问等待没有总时长上限**。用户口径：子代理问出问题后，主代理也可能
    不确定而去问用户，用户可能很久才回答——此时应当继续等，等到即用。旧实现是
    `wait_for(item.event.wait(), timeout=600)`，与停滞看门狗阈值同为 600s，于是看门狗必然先
    到期把子代理 cancel 掉，落成 failed「长时间无活动」：本该「按最佳判断继续」的分支永远走不到。

    不设上限之后靠什么兜底（任一成立即立刻返回 None，绝不悬挂成僵尸）：
      1. 子代理自身被取消（用户终止 / turn 收尾）→ `CancelledError` 正常向上传播，由驱动循环
         落终态；本函数不吞取消，也不会因此丢登记（见下）；
      2. 显式终止（右面板「终止」→ `SubagentManager.cancel` → `task.cancel()`）→ 同 1；
      3. 会话执行已结束 / 管理器已销毁 → 由调用方传入的 `alive` 判定。这里按 `_ALIVE_POLL_S`
         轮询一次（只是**存活检查的采样间隔**，不是等待上限），判定失效就清场返回 None。
    `alive` 为 None 时不轮询——纯事件等待，靠上面两条兜底（测试与轻量调用方用）。

    `timeout_s`：显式上限。生产路径 `agent_loop._run_report_to_leader` **只在模型自己传了
    `timeout_s` 时才把它传进来**；不传 = 无上限。所以「默认永远等下去」与「模型主动限时」
    两种语义各归各位，不会出现「配置说 N 秒、实际无限」的误导。
    """
    item = _questions.get(qid)
    if item is None:
        return None
    limit = None if not timeout_s or float(timeout_s) <= 0 else max(1.0, float(timeout_s))
    try:
        if limit is None and alive is None:
            await item.event.wait()
        else:
            started = asyncio.get_running_loop().time()
            while True:
                if item.event.is_set():
                    break
                # 轮询粒度取「剩余上限」与存活采样间隔的较小者：有上限时不会超时，
                # 无上限时只是定期确认会话还在，语义上仍是无限等。
                wait_s = _ALIVE_POLL_S
                if limit is not None:
                    remaining = limit - (asyncio.get_running_loop().time() - started)
                    if remaining <= 0:
                        discard_question(qid)
                        return None
                    wait_s = min(wait_s, remaining)
                try:
                    await asyncio.wait_for(item.event.wait(), timeout=wait_s)
                except asyncio.TimeoutError:
                    # 仅表示「这一轮采样没等到」，不是等待失败——除非会话已不在
                    if alive is not None and not alive():
                        logger.info(
                            "[subagent_comm] 会话/管理器已结束，停止等待答复 qid=%s", qid,
                        )
                        discard_question(qid)
                        return None
                    continue
                break
    except asyncio.CancelledError:
        # plan-549-2281 修复：等待被取消（子代理被中断 / 停止）也要清场——
        # 旧 subsession 实现在此 pop 登记项，迁移时丢了这行，取消路径会让
        # _questions / _by_agent 留下永久条目（直到该子代理下次提问被覆盖）。
        discard_question(qid)
        raise
    if item.superseded:
        # 被新提问取代：按「未收到答复」返回 None，调用方走「按最佳判断继续」分支。
        # 返回契约保持不变（str | None），调用方无需区分取代与超时。
        return None
    return item.reply


def resolve_question(qid: str, reply: str) -> bool:
    """主代理答复：写入答复并唤醒等待方。返回是否有等待中的提问被解答。"""
    item = _questions.pop(qid, None)
    if item is None:
        return False
    # plan-560-2311 B4：pop 前校验登记仍指向自己——同一子代理的新提问已覆盖登记时，
    # 答复不能写给早已作废的旧提问（那样两边都收不到）。
    if _by_agent.get(_key(item.session_id, item.agent_id)) != qid:
        _questions[qid] = item  # 归位，不算本函数消费了它
        return False
    item.reply = str(reply or "")
    item.event.set()
    _by_agent.pop(_key(item.session_id, item.agent_id), None)
    return True


def discard_question(qid: str) -> None:
    """丢弃提问（超时 / 撤单）。"""
    item = _questions.pop(qid, None)
    if item is not None and _by_agent.get(_key(item.session_id, item.agent_id)) == qid:
        _by_agent.pop(_key(item.session_id, item.agent_id), None)


def clear_agent(session_id: int, agent_id: int) -> None:
    """清理某子代理的全部待答复提问（子代理进入终态时调用，plan-560-2311 B4）。

    不清的后果：子代理已经结束时主代理再 `send_to_subagent`，`resolve_question` 仍会返回
    成功并回一句「it resumes immediately」——实际没有任何等待方被唤醒，指令静默丢失。
    被清掉的等待方按 superseded 语义立刻返回 None（交给子代理自己按最佳判断处理）。
    """
    qid = _by_agent.pop(_key(session_id, agent_id), None)
    if not qid:
        return
    item = _questions.pop(qid, None)
    if item is not None and not item.event.is_set():
        item.superseded = True
        item.event.set()


def clear_session(session_id: int) -> None:
    """清理该会话的全部待答复提问（会话收尾 / 管理器销毁时调用，plan-560-2311 B4）。"""
    sid = int(session_id)
    for qid, item in list(_questions.items()):
        if int(item.session_id) != sid:
            continue
        _questions.pop(qid, None)
        if _by_agent.get(_key(item.session_id, item.agent_id)) == qid:
            _by_agent.pop(_key(item.session_id, item.agent_id), None)
        if not item.event.is_set():
            item.superseded = True
            item.event.set()


def pending_question_of(session_id: int, agent_id: int) -> str | None:
    """该子代理当前待答复的 question_id（主代理答复时据此带上 id）。"""
    return _by_agent.get(_key(session_id, agent_id))


def reset_for_tests() -> None:
    _questions.clear()
    _by_agent.clear()
