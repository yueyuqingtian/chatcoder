"""Agent 推理循环（v2：主/子代理统一，当前实际生效实现）。

职责：流式模型调用 + 思考/文本广播 + 工具执行 + token 预算 + 产物抽取 + 回滚写盘埋点。
由 engine.start_turn（主代理）与 subagent.SubagentManager（子代理）统一调用。
旧版 agent_runtime.py（v0.1 团队编排路径）已废弃删除。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import MsgType, SenderType
from app.gateway.ws import manager as ws_manager
from app.models.registry import get_model_registry
from app.models.schemas import ChatMessage, ChatRequest
from app.orchestration.agent_events import broadcast
from app.orchestration.call_recovery import (
    Failure as CallFailure,
    RecoveryCancelled,
    classify_failure,
    recover_model_call,
)
from app.orchestration.approval_policy import (
    can_run_in_parallel,
    normalize_approval_mode,
    normalize_execution_mode,
)
from app._diag import log_tool_error, summarize_args  # v36: 工具错误诊断日志
from app.persistence.database import async_session_factory
from app.orchestration.tools import ToolContext, ToolResult, tool_executor
from app.orchestration.tools.read_cache import read_cache_for
from app.orchestration.tools.registry import tool_registry
from app.orchestration.workflow.tools import WORKFLOW_TOOL_NAMES, run_workflow_tool
from app.services import hook_service, message_service, rollback_service, task_service

logger = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_CHARS = 16000  # v15: 与 fs_read 上限(settings.tool_output_chars_read)对齐；历史上曾为 8000
# 估算校准系数（real / est）已迁移到 token_counter 并按模型落盘（plan-527-2218）：
# 重启后不必从 1.0 重新校准，前置压缩判断从第一步起就是准的。
# subagent_inspect 诊断 JSON 的单次展示上限（超出时显式提示，结论全文走 collect_results 分段）
_INSPECT_PAYLOAD_CHARS = 8000
_STREAM_CHUNK_SIZE = 80
_STREAM_INTERVAL = 0.01

# v3.1 (plan-102): 规划模式纠偏参数——模型未写 ai/chatcoder-plan-*.md 时的重试上限，
# 及注入纠偏消息的思考草稿字符上限（防止思考过长撑爆上下文）。
_PLAN_CORRECTION_MAX = 3
_PLAN_CORRECTION_DRAFT_CHARS = 8000

# v9: 写盘工具集合——执行时记录前后内容（精确回滚依据）
_WRITE_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")

# v34: 只读路径类工具——退化调用过滤范围（误执行无副作用，丢弃安全）
_READ_PATH_TOOLS = ("fs_read", "view_image", "read_attachment")
# v35: 写工具的必需参数——模型误发缺参调用（如 fs_write 无 content）时
# 必然失败或写空文件，过滤器直接丢弃（见 _filter_degenerate_tool_calls）
_WRITE_TOOL_REQUIRED_ARGS = {"fs_write": ("content",)}
_PATH_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


async def _broadcast_turn_status(session_id: int, turn_id: int, thread_id: int | None, text: str) -> None:
    """v35: 广播 turn 级状态提示（重试/恢复等），前端在流式状态行展示。

    与 _emit_agent_msg 的区别：不落库、不进消息流——重试过程是瞬态信息，
    持久化成普通消息会污染时间线（用户反馈：不应作为普通消息展示）。
    text 为空串表示清除当前状态。
    """
    await broadcast(session_id, {
        "event": "turn.status",
        "payload": {"turn_id": turn_id, "thread_id": thread_id, "text": text},
    })


async def _wait_retry_interval(cancel_event: asyncio.Event | None, seconds: float | None = None) -> bool:
    """v35/v45: 重试前等待指定秒数。

    seconds=None 时取配置序列的首个间隔（兼容旧调用）；期间每 0.5s 检查一次中断信号，
    被中断返回 False（调用方应停止重试），正常等待完成返回 True。
    """
    if seconds is None:
        seconds = _retry_wait_seconds(1)
    total = max(0.0, float(seconds))
    waited = 0.0
    while waited < total:
        if cancel_event and cancel_event.is_set():
            return False
        await asyncio.sleep(0.5)
        waited += 0.5
    return not (cancel_event and cancel_event.is_set())


def _retry_wait_seconds(attempt: int) -> float:
    """v45: 第 attempt 次重试（1-based）前的等待秒数，取间隔标签序列的对应项；序列为空时为 0。"""
    intervals = settings.agent_retry_interval_list
    if not intervals:
        return 0.0
    idx = min(max(int(attempt), 1), len(intervals)) - 1
    return max(0.0, float(intervals[idx]))


def _retry_plan() -> list[float]:
    """v45: 统一重试计划——每次重试前的等待秒数序列，个数即重试次数。

    任何报错（模型异常 / 空响应 / 超时中断）都按该序列依次重试，穷尽后才停止并显示报错。
    间隔为空时不自动重试。
    """
    return list(settings.agent_retry_interval_list)


def _credential_slots(db_creds: int) -> int:
    """一轮重试最多尝试几个凭据（N 个 Key 不吃掉重试次数）。"""
    return max(1, int(db_creds or 1))


def _retry_exhausted_text(reason: str, attempts: int, intervals: list[float],
                          creds: int = 1) -> str:
    """重试穷尽后的用户可见文案（R11）：说清重试了几轮、试过几个 Key。

    此前异常路径只写原始错误、响应路径只写重试次数，用户分不清「没重试」还是
    「重试过」；多 Key 时也看不到系统确实换了账号再试。
    """
    rounds = len(intervals)
    tried = max(int(attempts or 0), rounds)
    if creds > 1:
        return f"{reason}（已自动重试 {rounds} 次，尝试 {tried} 个 Key/账号仍失败）"
    return f"{reason}（已自动重试 {rounds} 次仍失败）"


async def _report_credential(db, provider, failure: CallFailure | None = None,
                             ok_reported: set[int] | None = None) -> None:
    """统一凭据记账（plan-20261020-retry-cred-perf R7）。

    - 非 ta3：成功走 report_success（**每次**都刷新「最近一次成功」，内部节流落盘），
      失败走 report_failure（指数退避暂缓；client 类不计入）；
    - ta3：保持旧行为（mark_ok 每个 loop 每凭据只记一次、mark_failed 按阈值冷却）。
    """
    cid = getattr(provider, "credential_row_id", None)
    if cid is None:
        return
    legacy = bool(getattr(provider, "credential_legacy", False))
    try:
        from app.services import credential_service as _cred_svc

        if failure is None:
            if legacy:
                if ok_reported is not None:
                    if cid in ok_reported:
                        return
                    ok_reported.add(cid)
                await _cred_svc.mark_ok(db, cid)
            else:
                _cred_svc.report_success(cid)
        elif legacy:
            if _cred_svc.is_retryable_error(failure.text):
                await _cred_svc.mark_failed(db, cid, failure.text)
        else:
            _cred_svc.report_failure(cid, failure.text, reset_at=failure.reset_at,
                                     counted=failure.counted)
    except Exception:  # noqa: BLE001 - 记账失败不影响调用
        logger.debug("[agent] 凭据记账失败(非阻塞)", exc_info=True)


async def _retry_stream_call(*, db, model, state: dict, api_messages, tool_schemas,
                             session_id, turn_id, step, agent_id, agent_name,
                             thread_id, cancel_event, reasoning_effort, thinking,
                             workspace, effort, index_no, failed_creds: set[int]):
    """重试中的一次尝试：轮内第 2 次起先换凭据，再走**与首次相同的流式管线**。

    - R5：messages 用调用方传入的 api_messages（build_api_copy 后的同一份 payload），
      不再重发未做落盘保护/旧推理剥离的原始 messages；
    - R6：统一走 _stream_chat_and_broadcast，长思考模型不会被网关总超时截断，UI 也有流式反馈。
    """
    provider = state["provider"]
    _cur_cid = getattr(provider, "credential_row_id", None)
    # 「失败即换」：本轮当前凭据刚失败过（已在 failed_creds 里）时，首次尝试就切走，
    # 不再先用同一个 Key 白打一次。
    _must_switch = (
        (index_no > 1 or (_cur_cid is not None and _cur_cid in failed_creds))
        and bool(failed_creds)
    )
    if _must_switch and model is not None:
        try:
            _np, _nreason, _ncid = await get_model_registry().next_provider_for_model(
                db, model, exclude=failed_creds,
            )
            if _np is not None:
                state["provider"] = provider = _np
                logger.warning("[agent] turn=%s 已切换到备用凭据 #%s", turn_id, _ncid)
                await _broadcast_turn_status(
                    session_id, turn_id, thread_id, "当前凭据不可用，已自动切换…",
                )
        except Exception:  # noqa: BLE001 - 切换失败沿用当前凭据
            logger.warning("[agent] turn=%s 凭据切换失败，沿用当前凭据重试", turn_id,
                           exc_info=True)
    # v29: 降档序列里的 "none" 显式关闭思考；有档位时开启；空档位沿用原三态
    _eff_l = (effort or "").strip().lower()
    _retry_thinking = False if _eff_l == "none" else (True if _eff_l else thinking)
    _retry_req = ChatRequest(
        messages=api_messages, model="",
        tools=tool_schemas or None,
        temperature=settings.agent_tool_temperature if tool_schemas else settings.agent_text_temperature,
        reasoning_effort=effort or reasoning_effort or (
            settings.agent_reasoning_effort if tool_schemas else None),
        max_tokens=settings.agent_max_output_tokens or None,
        session_id=str(session_id),
        message_id=f"{session_id}-{turn_id}-{step}-retry",
        thinking=_retry_thinking,
        workspace_dir=workspace or None,  # plan-270-1358: ta3 x-ws-id
    )
    return await _stream_chat_and_broadcast(
        provider, _retry_req,
        session_id=session_id, turn_id=turn_id,
        agent_id=agent_id, agent_name=agent_name,
        cancel_event=cancel_event, thread_id=thread_id,
    )


# plan-91-394: 瞬时故障判定——只有网络/网关/超时/空响应/流丢帧类失败才允许子代理保活等待恢复。
# 确定性失败（输出达到上限、仅生成思考、鉴权、模型不可用、上下文溢出）即使重试也不会好转，不保活。
# plan-20261020-retry-cred-perf R10: 补齐 500 / at capacity / peer closed 等常见文案；
# 「不可用」不再算永久失败——真实案例「503 所有渠道均不可用」被误判永久，子代理因此不保活。
_TRANSIENT_HINTS = (
    "超时", "网关", "空响应", "断流", "丢帧", "截断", "中断", "限流", "过载",
    "timeout", "timed out", "connection", "connect", "reset", "eof", "broken pipe",
    "gateway", "500", "502", "503", "504", "429", "rate limit", "overloaded",
    "temporarily", "at capacity", "peer closed", "remote disconnected", "unavailable",
)
_PERMANENT_HINTS = (
    "401", "403", "unauthorized", "forbidden", "invalid api key", "authentication",
    "token 上限", "仅生成思考", "model not found", "overflow", "context length",
    # plan-565-2323 G5：凭据/鉴权类失败不会因为等待而好转，必须判永久。
    # 事故复盘：provider_credential_unavailable 含 "unavailable"，被上面的瞬时表命中，
    # 子代理进入无上限保活循环、永久显示「执行中」（配合 subagent.py 的保活上限一并收紧）。
    # 这里只写 credential 一个词即可覆盖 provider_credential_unavailable /
    # provider_credentials_all_disabled 两种取值；永久表优先于瞬时表匹配。
    "credential", "api key", "鉴权", "凭据",
)


def _is_transient_failure_text(text: str) -> bool:
    """plan-91-394: 失败文案是否属于瞬时故障（见 _TRANSIENT_HINTS / _PERMANENT_HINTS）。"""
    low = (text or "").lower()
    if any(h in low for h in _PERMANENT_HINTS):
        return False
    return any(h in low for h in _TRANSIENT_HINTS)


def _filter_degenerate_tool_calls(response) -> None:
    """就地丢弃退化工具调用：模型把自身正文片段误发成工具参数的毛刺。

    真实案例（msg 44680）：模型输出总结正文后附带
    fs_read(path="视角(工作区根解析相对路径): 文件不存在 → 即原失败根因")，
    参数是正文一行的逐字复制。该调用必然失败，还会在消息流渲染出误导性
    工具卡片（用户误以为"正常消息被识别成工具调用"）。
    判据全部满足才丢弃（宁可漏放不可错杀）：
    只读路径类工具；path ≥16 字符且逐字出现在同响应正文中；
    不含路径分隔符与文件扩展名；含空白与 CJK 字符（正文特征）。
    真实路径要么带分隔符/扩展名，要么是短文件名，均不会命中。

    v35: 写工具缺必需参数的退化调用同样丢弃（真实案例 msg 46408/46410：
    模型输出总结正文时误发 fs_write(path="工具写文件**")、
    fs_write(path="写入才算完成")——参数为正文片段且无 content 键，
    执行后 0 字节文件被真实创建，并渲染出误导性工具卡片）。
    缺 schema required 参数的调用必然失败或产生空写入，丢弃安全：
    模型下一步会重新正确调用。
    """
    calls = response.tool_calls
    if not calls:
        return
    kept = []
    for tc in calls:
        args = tc.get("arguments") or {}
        # v35: 写工具缺必需参数（如 fs_write 无 content）→ 必然失败/写空文件
        if any(k not in args for k in _WRITE_TOOL_REQUIRED_ARGS.get(tc.get("name"), ())):
            logger.warning(
                "[agent] 丢弃退化工具调用 %s(args=%r) —— 缺少必需参数 %s，"
                "疑似模型把正文片段误发成工具调用",
                tc.get("name"), str(args)[:80],
                _WRITE_TOOL_REQUIRED_ARGS.get(tc.get("name"), ()),
            )
            continue
        path = args.get("path")
        if (tc.get("name") in _READ_PATH_TOOLS
                and isinstance(path, str)
                and len(path) >= 16
                and response.content and path in response.content
                and "/" not in path and "\\" not in path
                and not _PATH_EXT_RE.search(path)
                and re.search(r"\s", path)
                and re.search(r"[\u4e00-\u9fff]", path)):
            logger.warning(
                "[agent] 丢弃退化工具调用 %s(path=%r) —— 参数为同响应正文逐字片段",
                tc.get("name"), path[:60],
            )
            continue
        kept.append(tc)
    response.tool_calls = kept


def _present_tool_output(text: object, *, tool_name: str, session_id: int, call_key: str) -> str:
    """工具结果进入模型上下文与落库的统一形态（plan-156-620）。

    此前：上下文里是「头尾各半 + 中间截断」，落库是「只取开头、无标记」——下一轮从库里重建的
    历史与本轮模型看到的不一致；被截掉的中间部分也没有任何办法取回。
    现在：两处用同一份文本；超出预算时完整输出落盘，结果里写明总字数与 read_attachment 续读方式。

    plan-522-2209：预算解析与呈现规则收口到 output_paging（budget_for_tool / present_for_model），
    历史重建路径调用同一个函数，避免两处口径再次分叉。
    """
    from app.orchestration.output_paging import present_for_model

    return present_for_model(
        str(text or ""), tool_name=tool_name, session_id=session_id,
        tag=f"{tool_name}-{call_key or 'call'}",
    )


# v33: 上下文溢出错误识别（对齐 deepseek-harness CONTEXT_WINDOW_EXCEEDED_CODE）。
# 只有真正超出上下文窗口的错误才值得走紧急压缩（context-overflow）；429/503/
# 连接失败等瞬时故障压缩无济于事，且会造成"低占用异常压缩"与"压缩后 AI 中断"。
_CONTEXT_OVERFLOW_MARKERS = (
    "context_length", "context length", "context_window", "context window",
    "maximum context", "max context", "too long for context",
    "token limit", "maximum tokens", "max tokens", "too many tokens",
    "token count exceeds", "prompt is too long", "request too large",
    "context_length_exceeded", "上下文长度", "上下文窗口",
    "超出上下文", "上下文超出", "上下文长度超出",
)


def _is_context_overflow_error(err_msg: str) -> bool:
    """判断模型调用错误是否为上下文溢出类。

    供异常恢复分支决策：溢出错误 → 紧急压缩后重试；其他错误（瞬时网关故障）
    不压缩直接重试，避免在低占用时产生异常压缩块并拖垮 turn。
    """
    if not err_msg:
        return False
    lowered = err_msg.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


# v19: 部分网关把思考内容以 <thinking>…</thinking> 标签混入正文文本，
# 落库/广播前剥离，思考部分并入 thinking 消息（ThinkingBlock 展示），正文只保留干净文本。
# v27: 兼容未闭合/嵌套标签（gpt-5.6-luna 等模型只输出 <thinking> 前缀或嵌套复用标签），
# 不再要求标签闭合；<thinking> 到文本末尾的截断内容同样视为思考块。
_INLINE_THINKING_TAG_RE = re.compile(r"<(?:/?)(?:thinking|thought)>", re.IGNORECASE)
_INLINE_THINKING_TAGS = ("<thinking>", "<thought>", "</thinking>", "</thought>")


def _is_substantive_thinking(text: str | None) -> bool:
    """思考内容是否含有实质文字（plan-20261023-thinking-blank）。

    网关常用纯空白 reasoning 当占位（实测单个空格）；这类内容一旦被当有效值
    落库或广播，前端就会出现「思考过程 持续了几秒」+ 展开后一片空白的块。
    统一按 strip 后是否为空判定，调用点不再各自写 `if text:`。
    """
    return bool((text or "").strip())


def _split_inline_thinking(text: str) -> tuple[str, str]:
    """剥离正文中的内联思考标签，返回 (干净正文, 思考内容)。

    按标签扫描而非正则整块匹配：未闭合的 `<thinking>…`（截断到文本末尾）、
    嵌套 open 标签（按多个连续思考段拼接）、块外的孤立闭合标签均能正确处理。
    """
    if not text:
        return "", ""
    matches = list(_INLINE_THINKING_TAG_RE.finditer(text))
    if not matches:
        return text, ""
    clean_parts: list[str] = []
    think_parts: list[str] = []
    in_think = False
    pos = 0
    for m in matches:
        tag = m.group(0)
        closing = tag.startswith("</")
        if in_think:
            think_parts.append(text[pos:m.start()])
            if closing:
                in_think = False
        else:
            clean_parts.append(text[pos:m.start()])
            if not closing:
                in_think = True
        pos = m.end()
    if in_think:
        think_parts.append(text[pos:])
    else:
        clean_parts.append(text[pos:])
    return "".join(clean_parts).strip(), "\n".join(p.strip() for p in think_parts if p.strip())


class _InlineThinkingStreamSplitter:
    """流式剥离正文中的内联思考标签，标签可跨 chunk 拆分。

    部分模型把思考内容直接写进 content（带 <thinking> 标签），若原样透传，
    前端流式过程中会实时看到思考文本。feed(delta) 实时剥离：完整标签块转
    thinking 增量、块外文本透传为 content 增量；未闭合的思考块在 flush()
    时兜底转为 thinking。剥离结果仅用于实时广播，最终落库仍由
    _split_inline_thinking 对全量内容兜底（幂等，避免重复计入）。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False
        self._think_parts: list[str] = []

    def _tag_prefix_len(self) -> int:
        """buf 末尾最长的可作为某标签前缀的长度（0=无），避免半截标签被误发。"""
        for ln in range(len(self._buf), 0, -1):
            tail = self._buf[-ln:]
            if any(t.startswith(tail) for t in _INLINE_THINKING_TAGS):
                return ln
        return 0

    def feed(self, delta: str) -> tuple[str, str]:
        """输入一段 content 增量，返回 (content 增量, thinking 增量)。"""
        self._buf += delta
        content_parts: list[str] = []
        think_out: list[str] = []
        while self._buf:
            m = _INLINE_THINKING_TAG_RE.search(self._buf)
            if m is None:
                # 无完整标签：非标签前缀的文本立即输出，可能的半截标签留在缓冲等下一段
                keep = self._tag_prefix_len()
                emit, self._buf = (
                    (self._buf[:-keep], self._buf[-keep:])
                    if keep else (self._buf, "")
                )
                if self._in_think:
                    self._think_parts.append(emit)
                else:
                    content_parts.append(emit)
                break
            tag = m.group(0)
            closing = tag.startswith("</")
            if self._in_think:
                self._think_parts.append(self._buf[: m.start()])
                if closing:
                    self._in_think = False
                    think_out.append("".join(self._think_parts))
                    self._think_parts = []
            else:
                content_parts.append(self._buf[: m.start()])
                if not closing:
                    self._in_think = True
            self._buf = self._buf[m.end():]
        return "".join(content_parts), "".join(think_out)

    def flush(self) -> tuple[str, str]:
        """流结束调用；未闭合思考块转为 thinking，半截标签前缀丢弃。"""
        if self._in_think:
            think = "".join(self._think_parts)
            self._in_think = False
            self._think_parts = []
            return "", think
        self._buf = ""
        return "", ""


def _truncate_args(args: dict, limit: int = 800) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "\n...(已截断)"


def _line_change_stat(before: str | None, after: str | None) -> tuple[int, int]:
    """行级变更统计（v2.2，对齐 ZCode computeLineChangeStat 简化版）。

    plan-20261019-diff: 直接复用 rollback_service.diff_stats——工具卡摘要 +N -M
    与展开区的行级 diff 必须同一套 SequenceMatcher 口径，否则会出现「摘要 +0 -0、
    展开却有一行新增」这类对不上的情况。
    """
    return rollback_service.diff_stats(before, after)


def _fallback_after_text(result, target) -> str | None:
    """plan-1085: 磁盘读取失败时从工具结果 data 取兜底新内容。

    Windows 杀软/EDR 可能对刚写盘文件瞬时加锁，导致 agent_loop 写后读盘
    失败；写工具（editor_apply_diff/fs_write/multi_file_edit）在
    ToolResult.data 带回其写入的新内容（new_content / new_contents），
    此处用作 change_stat 与 rollback 记录的兜底数据源。data 不进模型上下文。
    """
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return None
    nc = data.get("new_content")
    if isinstance(nc, str):
        return nc
    ncs = data.get("new_contents")
    if isinstance(ncs, dict):
        tn = str(target).replace("\\", "/")
        for k, v in ncs.items():
            if isinstance(k, str) and isinstance(v, str) and k.replace("\\", "/") == tn:
                return v
    return None


def _fallback_before_text(result, target) -> str | None:
    """plan-20261019-diff: 写前读盘失败时从工具结果 data 取写前内容。

    写工具在读取原文件时本来就知道旧内容，把它带回 data（不进模型上下文），
    可避免「写前读取失败 → before=None → 整篇被当成新增」的统计失真。
    """
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return None
    oc = data.get("old_content")
    if isinstance(oc, str):
        return oc
    ocs = data.get("old_contents")
    if isinstance(ocs, dict):
        tn = str(target).replace("\\", "/")
        for k, v in ocs.items():
            if isinstance(k, str) and isinstance(v, str) and k.replace("\\", "/") == tn:
                return v
    return None


def _write_kind_to_bin(kind: str) -> bool | None:
    """文件类型 → _resolve_write_record 的三态入参（True=不可存文本全文）。

    oversize 也算 True：全文不落库、回滚走 checkpoint；区别只在于展示层会用
    diff 窗口补出 +N -M，见 build_diff_window。
    """
    if kind == rollback_service.FILE_KIND_UNKNOWN:
        return None
    return kind != rollback_service.FILE_KIND_TEXT


def _resolve_write_record(*, bin_pre: bool | None, bin_post: bool | None,
                          disk_after: str | None, result, target,
                          turn_id: int, tool_name: str) -> tuple[bool, str | None]:
    """plan-1085: 写盘记录 binary 归属与 after 内容的三态归并。

    bin_pre/bin_post 为写前/写后 _is_binary_path 三态结果（True 二进制 /
    False 文本 / None 读取失败）；任一为 True 即按二进制记录（checkpoint 兜底）。
    文本归属时 after 优先磁盘内容；磁盘读取失败（杀软/EDR 瞬时锁）时回退工具
    返回的 new_content；仍不可得且写后读取失败未知时降级二进制记录并 warning。
    """
    is_bin = bin_pre is True or bin_post is True
    after = None if is_bin else disk_after
    if not is_bin and after is None:
        fb = _fallback_after_text(result, target)
        if isinstance(fb, str):
            after = fb
            logger.warning(
                "[write.result] turn=%s tool=%s path=%s 磁盘读取失败，"
                "用工具返回 new_content 兜底统计与记录",
                turn_id, tool_name, target,
            )
        elif bin_post is None:
            logger.warning(
                "[write.result] turn=%s tool=%s path=%s before/after 均不可得，"
                "按二进制记录",
                turn_id, tool_name, target,
            )
            is_bin = True
    return is_bin, after


async def _register_pre_checkpoint(db, *, turn_id: int, target: str, target_rel: str | None,
                                   existed_before: bool, ckpt: str | None,
                                   registered: set[str], consumed: set[str]) -> None:
    """登记一次写盘的备份信息（plan-20261019-toolcard-v2）。

    备份是工具执行**之前**取的（见调用处的 _pre_ckpt）：
      · 文件原本存在 → 把备份登记到本轮快照（同一文件每轮只登记首次写盘前的样子）；
        该文件本轮已有备份而登记被去重时，这份多取的立即删除，不留孤儿文件；
      · 文件原本不存在 → 登记为「本轮新建」，回滚时删除。
    """
    key = target_rel or target
    if existed_before:
        reg = await rollback_service.record_checkpoint_for_turn(db, turn_id, ckpt or "", key)
        if reg:
            registered.add(key)
        elif ckpt:
            rollback_service.discard_checkpoint(ckpt)
        consumed.add(key)
    else:
        await rollback_service.record_checkpoint_for_turn(db, turn_id, "", key, new_file=key)


@dataclass
class AgentOutput:
    kind: str  # message | done | error | cancelled | skipped
    text: str = ""
    error: str = ""
    artifact_ids: list[int] = field(default_factory=list)
    # v36 (plan-321-1600 M1): 本 loop 累计 token 用量——子代理据此累加到 Task.token_usage，
    # 使子代理消耗对用户可见（此前子任务用量恒为 0）。
    tokens: int = 0
    # plan-91-394: 失败是否属于网络/网关类瞬时故障（子代理据此保活等待恢复，而非判死）。
    # 确定性失败（鉴权、模型不可用、输出达到上限）为 False。
    transient: bool = False


def _is_memory_pairing_balanced(messages: list, k: int) -> bool:
    """内存 ChatMessage 版的「切点配对平衡」判定（逻辑对齐
    `context_compressor._is_pairing_balanced`）。

    - 压缩区 [0,k) 内每个 assistant(tool_calls) 都必须在压缩区内配对闭合；
    - 保留区 [k:] 内不允许出现孤立 tool 消息（其 assistant(tool_calls) 落在压缩区）。
    """
    open_ids: set[str] = set()
    for m in messages[:k]:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                tid = tc.get("id")
                if tid:
                    open_ids.add(str(tid))
        elif m.role == "tool" and m.tool_call_id:
            open_ids.discard(str(m.tool_call_id))
    if open_ids:
        return False

    seen_calls: set[str] = set()
    for m in messages[k:]:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                tid = tc.get("id")
                if tid:
                    seen_calls.add(str(tid))
        elif m.role == "tool" and m.tool_call_id:
            if str(m.tool_call_id) not in seen_calls:
                return False
    return True


def _prune_messages_for_compaction(messages: list, retain_tokens: int) -> list:
    """plan-278-1391：压缩成功后按保留预算裁剪「本轮内存消息」。

    根因修复：落库式压缩把被压缩消息记入 shared_context.compacted_ids，只在**下一轮**
    上下文重建时生效；本轮内存 messages 若不裁剪，真实 prompt 占用几乎不下降，
    于是每步都判定「超阈值」而反复触发压缩（可压范围耗尽后收益断崖式衰减）。

    策略（只扩大保留区，绝不拆散工具回合）：
    1. 系统/开发者消息（系统提示、分层上下文）全部保留；
    2. 其余消息从尾部向前累计 token 至 retain_tokens 得初始切点；
    3. 切点向前回退到 tool 配对平衡位置；
    4. 按原顺序重组（保持消息相对次序），返回裁剪后的列表。

    plan-282-0: 累计用「有效口径」——reasoning_content 计入估算后，旧工具回合的
    thinking（出站会被 build_api_copy 剥离）不应再白吃保留预算，否则内存裁剪会把
    真正的对话挤出去、却留下不占真实体积的旧 thinking。

    注意：仅作用于内存，落库消息与 compacted_ids 不变，前端压缩卡片/还原不受影响。
    """
    from app.orchestration.token_counter import (
        estimate_message_tokens, rough_token_estimate,
    )
    from app.orchestration.compaction import _REASONING_KEEP_TOOL_ROUNDS

    if len(messages) <= 1:
        return messages
    body = [m for m in messages if m.role not in ("system", "developer")]
    if not body:
        return messages

    # 旧 reasoning 分界线：索引 < cutoff 的 assistant reasoning 出站时会被剥离，
    # 核算时只计消息结构开销（与 compaction.strip_stale_reasoning 同一语义）。
    cutoff = 0
    seen_rounds = 0
    for i in range(len(body) - 1, -1, -1):
        m = body[i]
        if m.role == "assistant" and m.tool_calls:
            seen_rounds += 1
            if seen_rounds >= _REASONING_KEEP_TOOL_ROUNDS:
                cutoff = i
                break

    def _eff_tokens(idx: int) -> int:
        """单条消息的有效 token（旧工具回合的 reasoning 只计结构开销）。"""
        m = body[idx]
        if idx < cutoff and m.role == "assistant" and m.reasoning_content:
            return estimate_message_tokens(m) - rough_token_estimate(str(m.reasoning_content))
        return estimate_message_tokens(m)

    keep_from = len(body)
    acc = 0
    for i in range(len(body) - 1, -1, -1):
        acc += _eff_tokens(i)
        keep_from = i
        if acc >= retain_tokens:
            break
    if keep_from == 0:
        return messages  # 全部都在保留预算内，无需裁剪

    k = keep_from
    while k > 0 and not _is_memory_pairing_balanced(body, k):
        k -= 1
    if k == 0:
        return messages

    kept_ids = {id(m) for m in body[k:]}
    return [m for m in messages if m.role in ("system", "developer") or id(m) in kept_ids]


async def _compact_persistent_or_fallback(
    db: AsyncSession, *, session_id: int, provider, agent_window: int,
    used_tokens: int | None, agent_id: int, agent_name: str,
    turn_id: int, messages: list, trigger: str = "pressure",
    reply_language: str = "auto",
    thread_id: int | None = None, agent_kind: str = "main",
) -> tuple[dict | None, list]:
    """优先落库式压缩（v30，参照 deepseek-harness compaction）。

    成功时：落库 SUMMARY 消息 + 更新 shared_context.compacted_ids（下轮上下文
    重建自动跳过被压缩消息并注入 checkpoint），并把 checkpoint 摘要注入当前
    内存 messages（本轮剩余 step 立即可见），广播 compact.summary。
    失败时：回退旧内存式 auto_compact / emergency_compact（不落库）。

    plan-560-2311 A2：子代理（thread_id 非空或 agent_kind != "main"）**不做落库式压缩**。
    compact_session / emergency_compact_session 取的是 `_fetch_main_messages`（主线程
    `thread_id IS NULL` 的消息），会把主会话历史遮蔽、把 SUMMARY 卡片写回主线程并
    改写主会话 shared_context——子代理自己的线程却完全没被压缩。因此子代理直接返回
    (None, messages)，由下方内存式回退只裁剪自己的上下文。

    Returns:
        (result, messages)：result 非 None 表示落库式压缩成功。
    """
    # plan-19-82: checkpoint 前言按本轮语言选择（中文会话用中文前言，避免英文污染回复语言）
    from app.orchestration.prompts import get_checkpoint_preamble

    _result: dict | None = None
    # plan-560-2311 A2：非主线程不能落库式压缩（原因见 docstring），交由内存式回退
    _persistent_allowed = thread_id is None and agent_kind == "main"
    # plan-19-82 步骤5: 固定开销（system/developer + 工具规则）实测值——压缩不会动它们，
    # 必须先扣除再判定目标可达性，否则会「压不动却反复压」。
    from app.orchestration.token_counter import estimate_fixed_overhead_tokens as _fixed_of
    _fixed_overhead = _fixed_of(messages)
    try:
        from app.persistence.models.message import Session as _SessRow
        from app.orchestration.context_compressor import compact_session, emergency_compact_session
        _sess = await db.get(_SessRow, session_id) if _persistent_allowed else None
        if _sess is not None:
            if trigger == "context-overflow":
                _result = await emergency_compact_session(
                    db, session=_sess, provider=provider, context_window=agent_window,
                    used_tokens=used_tokens, agent_id=agent_id, agent_name=agent_name,
                    turn_id=turn_id, language=reply_language,
                    # plan-560-2311 A4：与 pressure 路径同源传固定开销，口径一致
                    fixed_overhead_tokens=_fixed_overhead,
                )
            else:
                _result = await compact_session(
                    db, session=_sess, provider=provider, context_window=agent_window,
                    used_tokens=used_tokens, agent_id=agent_id, agent_name=agent_name,
                    turn_id=turn_id, trigger=trigger, language=reply_language,
                    fixed_overhead_tokens=_fixed_overhead,
                )
    except Exception:
        logger.warning("[agent] turn=%s %s 落库式压缩失败，回退内存式", turn_id, trigger, exc_info=True)
        _result = None

    if _result is not None:
        # plan-278-1391: 先裁剪本轮内存消息，再追加 checkpoint。
        # 不裁剪的话，被压缩内容仍留在本轮 prompt 里 → 占用不降 → 每步反复触发压缩。
        from app.orchestration.token_counter import estimate_messages_tokens
        _before_tokens = estimate_messages_tokens(messages)
        # plan-19-82 步骤5: 内存裁剪预算改用「压缩后目标预算」而非固定 16% 保留，
        # 否则落库压缩达标了、内存 prompt 仍占高位（下一 step 又判超阈值 → 反复压缩）。
        from app.orchestration.token_counter import get_compact_target_tokens
        _retain = _result.get("retained_tokens")
        if not isinstance(_retain, int) or _retain <= 0:
            _retain = get_compact_target_tokens(agent_window)
        _pruned = _prune_messages_for_compaction(messages, _retain)
        _after_tokens = estimate_messages_tokens(_pruned)
        _post_ratio = round(_after_tokens / agent_window * 100, 1) if agent_window else None
        logger.info(
            "[agent] turn=%s 压缩后内存上下文 %d -> %d tokens（保留预算 %d，消息 %d -> %d 条，压缩后占用 %s%%）",
            turn_id, _before_tokens, _after_tokens, _retain, len(messages), len(_pruned), _post_ratio,
        )
        messages = [*_pruned, ChatMessage(
            role="developer",
            content=f"{get_checkpoint_preamble(reply_language)}\n\n{_result['summary']}",
        )]
        await broadcast(session_id, {
            "event": "compact.summary",
            "payload": {
                "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                "compaction_id": _result.get("compaction_id"),
                "index": _result.get("index"),
                "shadowed_range": ([min(_result["shadowed_ids"]), max(_result["shadowed_ids"])]
                                   if _result.get("shadowed_ids") else None),
                "shadowed_seqs": _result.get("shadowed_ids"),
                "shadowed_tokens": _result.get("shadowed_tokens"),
                "saved_tokens": _result.get("saved_tokens"),
                "summary_message_id": _result.get("summary_message_id"),
                "summary": (_result.get("summary") or "")[:300],
                "trigger": _result.get("trigger"),
                "used_tokens": used_tokens,
                "context_window": agent_window,
                "ratio": round((used_tokens or 0) / agent_window * 100, 1) if agent_window else None,
                # plan-19-82: 压缩后占用率（验收口径：应落入 10%-15% 目标区间）
                "post_compact_ratio": _post_ratio,
                "rounds": _result.get("rounds"),
            },
        })
    else:
        if trigger == "context-overflow":
            from app.orchestration.compaction import emergency_compact
            messages = emergency_compact(messages, agent_window)
        else:
            from app.orchestration.compaction import auto_compact
            messages = await auto_compact(messages, agent_window, provider,
                                          language=reply_language)
    return _result, messages


def _desktop_obs_scene(tools: list, write_tools: frozenset) -> str:
    """推断电脑操控的观测场景（plan-340-1705 L3.1）。

    仅用于「场景标签 × 思考量」的观测埋点，不参与任何行为判定——
    本版刻意不做自动降档（依据不足时降档会伤准确率，需先拿到分布数据）。
    场景取自「紧邻的上一步对电脑做了什么」：刚看完界面只需挑动作，操作失败才需要诊断。
    """
    name, ok = tools[-1]
    if not ok:
        return "after_error"
    return "after_action" if name in write_tools else "after_perceive"


async def _agent_started_extra(agent) -> dict:
    """agent.started 的附加字段：工作流节点子代理带上所属程序编号与派发时刻。

    节点子代理不经 spawn_subagent 工具调用落库，前端无法按消息序定位其卡片：
    - workflow_program_id：所属程序，卡片挂到同轮该程序的工作流卡附近；
    - spawn_message_id：派发时刻 = 线程首条消息（任务说明）的 id。派发前的主线程消息都已落库（见
      _spawn 的 flush 屏障），其 id 均小于它；卡片据此放在派发前最后一条消息之后。
    两者都由数据库事实推导，实时事件与 REST 回填（子代理列表）结果一致。
    只读查询，失败只记日志，绝不阻塞子代理启动。
    """
    node_id = getattr(agent, "workflow_node_id", None)
    if not node_id:
        return {}
    extra: dict = {}
    try:
        from app.orchestration.workflow import store as workflow_store

        brief = (await workflow_store.node_briefs([int(node_id)])).get(int(node_id))
        program_id = brief.get("program_id") if brief else None
        if program_id is not None:
            extra["workflow_program_id"] = int(program_id)
    except Exception:
        logger.debug("[agent] 工作流节点程序编号解析失败(非阻塞) node=%s", node_id, exc_info=True)
    agent_id = getattr(agent, "id", None)
    if agent_id is not None:
        try:
            from sqlalchemy import func, select

            from app.persistence.database import async_session_factory
            from app.persistence.models.message import Message

            async with async_session_factory() as s:
                spawn_id = (
                    await s.execute(select(func.min(Message.id)).where(Message.thread_id == int(agent_id)))
                ).scalar()
            if spawn_id is not None:
                extra["spawn_message_id"] = int(spawn_id)
        except Exception:
            logger.debug("[agent] 派发时刻解析失败(非阻塞) agent=%s", agent_id, exc_info=True)
    return extra


# ── plan-474-2105：身份二次防御 ──────────────────────────────
# 工具 schema 在组装时已按身份剥离（subagent_tools.strip_for_subagent），
# 但「类型工具白名单」「历史 profile 配置」
# 「自定义权限模式」都可能把工具名重新塞回来。这里按调用方身份再拦一次：
# 剥离解决「模型看不到」，本守卫解决「看到了也执行不了」。
# plan-549-2281：子会话的创建与控制已下线，旧 _SUBSESSION_DENIED 随之删除——
# 不再有任何创建 / 操作子会话的工具可被重新塞回。
_SUBAGENT_DENIED: frozenset[str] = frozenset({
    # 子代理只能通过 report_to_leader 向主代理求助，不得直接打扰用户
    "ask_user_question",
    # 不得派生下级执行体，也不得碰子会话（历史名保留：旧会话回放仍可能出现）
    "create_subsession", "send_to_subsession", "get_subsession_progress", "interrupt_subsession",
    "message_main", "message_peer",
    "spawn_subagent", "collect_results", "subagent_inspect", "cancel_subagent",
    "send_to_subagent", "resume_subagent",
})


def _interruptible_for(tool_name: str, args: dict) -> str:
    """邮箱打断档位（plan-474-2105）。

    返回值与 engine.request_interrupt / 本模块的让出分派一致：
      · never  —— 默认：不打断，消息留在注入队列，动作结束后、下一次 LLM 调用前立刻生效；
      · detach —— 前台同步命令（含 sleep）：进程转后台，控制权让回主代理；
      · abort  —— 明确在「等待」的工具（terminal_bg_status 的 wait_until_done）：直接结束等待。

    plan-20261020-bgtask：此前除写工具外一律 abort，连 todo_write / fs_read / 浏览器与电脑操控这类
    瞬间完成、且带副作用的工具也会被取消——动作已经发生、结果却被丢掉，模型还会据此重复操作。
    现在只有白名单里真正在等的工具才允许让出，其余一律不打断。
    """
    if tool_name == "terminal_exec":
        raw = args.get("waitForCompletion")
        if isinstance(raw, str):
            is_bg = raw.strip().lower() in ("false", "0", "no")
        else:
            is_bg = raw is False
        # 后台启动瞬间返回 shell_id：取消它会让进程已启动、模型却拿不到 shell_id
        return "never" if is_bg else "detach"
    if tool_name == "terminal_bg_status":
        raw = args.get("wait_until_done")
        waiting = raw.strip().lower() not in ("false", "0", "no", "") if isinstance(raw, str) else bool(raw)
        return "abort" if waiting else "never"
    return "never"


# plan-20261020-bgtask：邮箱消息类型 → 让出等待时给模型看的来源说明
# plan-549-2281：子代理成为唯一执行体，它的提问也走 P0 打断（替代原子会话提问）。
_INTERRUPT_SOURCE_LABEL: dict[str, str] = {
    "subagent_question": "收到子代理提问",
    "subagent_completion": "有子代理已结束",
    "main_reply": "收到主会话答复",
    "bg_command_done": "有后台命令已结束",
    "bg_command_stopped": "有后台命令已被停止",
}


def _interrupt_source_label(source: str) -> str:
    return _INTERRUPT_SOURCE_LABEL.get(source, "收到新的系统消息")


def _deny_tool_for_identity(tool_name: str, *, is_subsession: bool, is_subagent: bool) -> str:
    """按调用方身份返回拒绝文案；允许时返回空串。"""
    if is_subagent and tool_name in _SUBAGENT_DENIED:
        return (
            f"Error: 子代理无权调用工具 {tool_name}。"
            "需要主代理决策时用 report_to_leader(kind=\"question\")，"
            "需要用户决策时把问题写进最终结论，由主代理转达。不要重试本工具。"
        )
    # plan-549-2281：子会话的创建与控制已下线，_SUBSESSION_DENIED 随之删除——
    # 历史挂靠会话仍在跑，但它们拿不到任何子会话工具，无需再按身份二次拦截。
    # 仍按「旧口径 + report_to_leader」拦：挂靠会话没有 subagent_comm，调用它只会拿到未知工具。
    if is_subsession and (tool_name in _SUBAGENT_DENIED or tool_name == "report_to_leader"):
        return (
            f"Error: 该会话无权调用工具 {tool_name}。"
            "需要主代理决策时把问题写进最终结论，由主代理决定自己回答还是转问用户。不要重试本工具。"
        )
    return ""


async def run_agent_loop(
    db: AsyncSession,
    *,
    session_id: int,
    turn_id: int,
    agent,
    context_messages: list[ChatMessage],
    tool_schemas: list[dict],
    workspace: str,
    max_steps: int | None = None,
    cancel_event: asyncio.Event | None = None,
    token_budget: int | None = None,
    subagent_context: dict | None = None,
    # plan-330-1648 M4: 子代理侧的双向通信上下文（manager + agent_id），
    # 用于 report_to_leader 上报/提问；主代理为 None。plan-541-2264：子代理一律后台执行，
    # 不再携带「是否同步阻塞」标记。
    subagent_comm: dict | None = None,
    # plan-547: 运行中注入的用户消息提供方（每次 LLM 调用前 drain，返回注入项列表）
    injected_inputs_provider: Callable[[], list[dict]] | None = None,
    reasoning_effort: str | None = None,
    # plan-330-1648 M1: 子代理运行模式（按类型锁定：explore=readonly / general=default）。
    # 显式传入时**不再读取会话 permission_mode**——子代理不继承主代理的计划/只读模式。
    subagent_mode: str | None = None,
    task_id: int | None = None,
    model_id: int | None = None,
    multimodal: bool = False,
    # plan-19-82: 本轮回复语言（zh/en/auto），用于压缩摘要与 checkpoint 文案对齐
    reply_language: str = "auto",
    reply_language_source: str = "user",
    # plan-91-394: 断点续跑检查点回调——每步开始时把当前 messages 列表引用交给调用方。
    # 列表原地追加，调用方持有引用即可在失败/暂停时读到最新进度（压缩重建时会再次回调）。
    checkpoint_sink: Callable[[list], None] | None = None,
) -> AgentOutput:
    """运行单个 agent 推理循环。

    context_messages 已由 context_manager 组装（system + developer + 历史 + 指令）。
    subagent_context: 主代理专用，含子代理管理能力（spawn_subagent/collect_results 工具）。
    multimodal: 当前模型支持图片输入时，read_attachment/view_image 的图片结果
    会以 image_url 内容块追加一条 user 消息，让模型真正"看到"图片（v15）。
    reply_language: 本轮语言，压缩路径据此生成同语言摘要，避免英文 checkpoint 污染回复语言。
    """
    agent_id = agent.id
    agent_name = agent.name
    # v0.3.1: 缓存 agent 标量属性——写失败恢复路径会 rollback 主 db 会话使 agent 对象过期，
    # 之后任何属性访问都会触发同步 reload 抛 MissingGreenlet（SQLAlchemy xd2s）。
    # 提前取为局部变量双保险（agent_id/agent_name 原已有缓存）。
    agent_kind = agent.kind
    agent_model_id = getattr(agent, "model_id", None)
    thread_id = None if agent_kind == "main" else agent_id  # 子代理消息进自己的线程

    # plan-474-2105：调用方身份（工具权限的二次防御依据）。
    # 子代理：由主代理派生，subagent_comm 非空或 agent 行 kind≠main；
    # 子会话：本身就是会话，靠 sessions.side_parent_id 判定——每个 turn 只查一次。
    _caller_is_subagent = bool(subagent_comm) or agent_kind != "main"
    _caller_is_subsession = False
    try:
        from app.persistence.models.message import Session as _IdentSession
        _ident_row = await db.get(_IdentSession, session_id)
        _caller_is_subsession = bool(
            _ident_row is not None and getattr(_ident_row, "side_parent_id", None)
        )
    except Exception:
        logger.debug("[agent] turn=%s 会话身份判定失败(按主会话处理)", turn_id, exc_info=True)
    # plan-549-2281：子代理的任务标题——审批卡据此显示「来自子代理 #N「标题」」。
    # 子代理的 agent.name 是任务标题的截断（40 字符，见 spawn 处），主代理侧为空。
    _caller_task_title = str(getattr(agent, "name", "") or "") if _caller_is_subagent else ""

    # v2.2 (对齐 zcode 3.12): 会话权限模式（executor 审批门前决策）
    # plan-330-1648 M1: 子代理按类型锁定运行模式（subagent_mode），**不继承会话模式**——
    # 否则主代理处于 plan 模式时，子代理会被 executor 的 plan 规则拒绝写业务文件，
    # 并被“未写计划文档”的纠偏逻辑反复催写 ai/chatcoder-plan-*.md（用户反馈 Bug）。
    permission_mode = "agent"
    if subagent_mode:
        permission_mode = normalize_execution_mode(str(subagent_mode))
        logger.info(
            "[agent] 子代理 turn=%s 执行模式=%s（按类型锁定，不继承会话模式）",
            turn_id, permission_mode,
        )
    else:
        try:
            from app.persistence.models.message import Session as _Session
            _sess = await db.get(_Session, session_id)
            permission_mode = normalize_execution_mode(getattr(_sess, "permission_mode", None))
        except Exception:
            logger.warning("[agent] turn=%s 读取会话执行模式失败，用 agent", turn_id, exc_info=True)

    # plan-549-2281：排队消息（运行中「立即发送」）也要带上派发子代理的提醒。
    # 与 turn 开始时的完整提示同一套开关：设置关闭或该模式不下发子代理工具时为空串。
    # 子代理自身与历史挂靠子会话（side_parent_id 非空）都不带——它们没有派发工具。
    from app.orchestration.prompts.subagent_dispatch import subagent_dispatch_hint
    _parallel_reminder = subagent_dispatch_hint(
        permission_mode, full=False,
        is_subagent=_caller_is_subagent, is_legacy_child=_caller_is_subsession,
    )

    # plan-75-332: 权限模式（ask / auto / full）——与执行模式正交，注入本 turn 所有
    # ToolContext（主代理/子代理共用此入口）。子代理同样继承会话权限模式：子代理的
    # 写操作也应按用户所选口径裁决，不因是子代理就绕开询问。
    approval_mode = "ask"
    try:
        from app.persistence.models.message import Session as _SessionPM
        _sess_pm = await db.get(_SessionPM, session_id)
        approval_mode = normalize_approval_mode(getattr(_sess_pm, "approval_mode", None))
    except Exception:
        logger.warning("[agent] turn=%s 读取权限模式失败，用 ask", turn_id, exc_info=True)

    # v3.0 (plan-88): 沙箱模式。
    # plan-75-332：**已废弃、不再参与任何判定**（read-only / danger-full-access 的
    # 硬边界已由「执行模式 × 权限模式」取代）。保留计算仅为兼容子代理快照与
    # ta3 提示词段的旧参数位，避免改动面外溢。
    sandbox_mode = "workspace-write"
    try:
        from app.services import config_service
        _eff = await config_service.effective_config(db, project_path=workspace)
        sandbox_mode = str(_eff.get("sandbox_mode") or "workspace-write")
        if sandbox_mode == "workspace-write" and settings.sandbox_mode != "workspace-write":
            sandbox_mode = settings.sandbox_mode
    except Exception:
        logger.warning("[agent] turn=%s 读取沙箱模式失败，用 workspace-write", turn_id, exc_info=True)

    # 解析 provider（v10: 会话级模型覆盖优先——engine 传入 session.model_id，
    # 优先于 agent.model_id，解决"配置无默认模型 + 页面会话选择模型"不可用问题）
    registry = get_model_registry()
    provider, reason = None, None
    # plan-248-1258 M2.2: 记住解析所用的 Model 行，凭据轮询时据此重建 provider
    _cur_model = None
    if model_id is not None:
        from app.persistence.models.model_reg import Model
        _cur_model = await db.get(Model, model_id)
        provider, reason = await registry.get_provider_for_model(db, _cur_model)
    if provider is None:
        # plan-271-1364 回归修复：回落 agent 绑定模型时**不要**用它的原因覆盖已有的具体原因。
        # 此前无条件 `provider, reason = await get_provider_for_agent(...)`，把 model_id 路径的
        # 真实失败原因（如 workbuddy_login_required / provider_disabled）冲成
        # no_default_configured，用户只看到误导性的"模型不可用(no_default_configured)"。
        _primary_reason = reason
        _fb_provider, _fb_reason = await registry.get_provider_for_agent(db, agent)
        if _fb_provider is not None:
            provider = _fb_provider
            reason = _fb_reason
            if getattr(agent, "model_id", None):
                from app.persistence.models.model_reg import Model as _Model
                _cur_model = await db.get(_Model, agent.model_id)
        else:
            # 回落同样失败：主路径有具体原因时保留（更贴近真实故障）；
            # v36 修复：主路径无原因（如 model_id 未传）时用回落原因兜底，
            # 否则 reason 停留在 None → 对外显示「模型不可用(None)」，错误信息为空。
            reason = _primary_reason or _fb_reason
    # plan-156-620：续读提示里的工具调用写法跟随当前模型（Ta3 会话用伪装名 / 伪装参数键）
    from app.orchestration.output_paging import set_ta3_visible
    set_ta3_visible(getattr(_cur_model, "api_format", "") == "ta3")
    # plan-20261019-desktop-precision：工具按模型族选坐标口径（电脑操控），模型名随 ToolContext 下发。
    # 提前取成标量：写失败恢复会 rollback 主 db，之后再访问 ORM 属性会触发同步 reload。
    _tool_model_name = str(getattr(_cur_model, "name", "") or "")
    # plan-20261019-desktop-precision：本轮上下文里的工具截图消息（按出现顺序），超量时裁掉较早的
    _tool_image_msgs: list[ChatMessage] = []
    if provider is None:
        await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                              agent_id=agent_id, agent_name=agent_name,
                              msg_type=MsgType.ERROR,
                              content={"text": f"模型不可用({reason})", "agent_name": agent_name})
        return AgentOutput(kind="skipped", error=reason)

    # plan-248-1258 M2.2: 凭据轮询状态——已失败的凭据 id 集合 + 已上报成功的集合
    _failed_creds: set[int] = set()
    _ok_reported: set[int] = set()
    # plan-20261020-retry-cred-perf：该供应商的凭据条数——决定一轮重试能换几次 Key。
    # 多凭据时轮内换 Key 不等待，N 个 Key 不会吃掉重试次数。
    _cred_total = 1
    try:
        _cred_pid = getattr(provider, "provider_row_id", None)
        if _cred_pid:
            from app.services import credential_service as _cred_svc0
            _cred_total = max(1, len(await _cred_svc0.list_credentials(db, _cred_pid)))
    except Exception:  # noqa: BLE001 - 数不清就按单凭据处理
        _cred_total = 1

    # v21: thinking 模式开关 —— provider 支持 thinking 参数（DeepSeek/GLM/Kimi 系）
    # 且本轮 effort 非 none 时开启（对齐 deepseek-harness：thinking:{type:"enabled"} + effort）。
    # 修复：此前只发 reasoning_effort、从不发 thinking，DeepSeek 官方网关不识别
    # reasoning_effort，思考模式从未生效 → 同一模型显得"笨"。
    # v29: 三态语义——True 显式开启；False 用户选择档位 none，要求**显式关闭**思考
    # （各 provider 按自身协议翻译成 disabled / enable_thinking=false；协议无关闭
    # 字段的供应商保持不下发）；None 不触碰该字段（未指定档位 / provider 不支持思考）。
    _thinking_flag: bool | None = None
    _eff = reasoning_effort or (settings.agent_reasoning_effort if tool_schemas else None)
    _eff_norm = (_eff or "").strip().lower()
    try:
        if _eff_norm == "none":
            _thinking_flag = False
            logger.info("[agent] turn=%s 思考档位为 none，显式关闭思考", turn_id)
        elif (_eff_norm and settings.agent_thinking_enabled
              and hasattr(provider, "supports_thinking") and bool(provider.supports_thinking())):
            _thinking_flag = True
            logger.info("[agent] turn=%s 启用 thinking 模式 (effort=%s)", turn_id, _eff)
    except Exception:
        logger.debug("[agent] turn=%s thinking 能力判断失败(非阻塞)", turn_id, exc_info=True)

    if max_steps is None:
        max_steps = settings.agent_max_steps
    # 0 或负数表示不限制步数（使用大数值兜底循环安全）
    is_unlimited_steps = (max_steps <= 0)
    loop_step_limit = 10_000_000 if is_unlimited_steps else max_steps

    # v6.4: 启动时打印压缩阈值诊断日志
    try:
        from app.orchestration.token_counter import get_agent_context_window
        _diag_window = await get_agent_context_window(db, agent, model_id=model_id)
        if _diag_window < 100000:
            logger.warning(
                "[agent] turn=%s agent_window=%d 过小(<100k)，用默认 500000 兜底",
                turn_id, _diag_window,
            )
            _diag_window = 500000
        logger.info(
            "[agent] turn=%s agent_context_window=%d, 压缩阈值=%.0f%% (%d tokens)",
            turn_id, _diag_window,
            settings.auto_compact_threshold_ratio * 100,
            int(_diag_window * settings.auto_compact_threshold_ratio),
        )
    except Exception:
        logger.warning("[agent] turn=%s 压缩阈值诊断日志获取失败", turn_id, exc_info=True)

    await broadcast(session_id, {
        "event": "agent.started",
        "payload": {"agent_id": agent_id, "kind": agent_kind, "name": agent_name,
                    "turn_id": turn_id, **(await _agent_started_extra(agent))},
    })
    # plan-522-2209：把本轮使用的提示词版本记进日志。此前提示词改动无从追溯——
    # 线上出现行为差异时只能翻 git 历史猜是哪次改动生效的。
    try:
        from app.orchestration.prompts import PROMPT_VERSION as _PROMPT_VERSION

        logger.info("[agent] turn=%s prompt_version=%s", turn_id, _PROMPT_VERSION)
    except Exception:
        pass
    await broadcast(session_id, {
        "event": "agent.updated",
        "payload": {"agent_id": agent_id, "status": "running"},
    })

    messages = list(context_messages)
    total_tokens = 0
    final_text = ""
    write_paths: list[str] = []
    # plan-20261019-toolcard-v2：本轮已登记过写前备份的文件（相对工作区路径）——每个文件每轮只备份首次写盘前的样子
    _ckpt_registered: set[str] = set()
    # plan-20261018-operator-boost：操作路线沉淀守卫——本轮完成了 GUI 操作却没沉淀时，收尾前提醒一次
    from app.orchestration.op_route_guard import OpRouteGuard
    _route_guard = OpRouteGuard(enabled=bool(getattr(settings, "desktop_recipe_enabled", True)))
    # v2.2 (对齐 zcode 3.9): todo 提醒机制——模型维护的执行清单连续 N 步未更新时注入提醒
    _todo_active = False       # 本 turn 是否已创建过清单
    _todo_updated_at_step = 0  # 清单最后一次更新的步数
    # v3.0 (plan-88): 规划模式纠偏提醒——模型未写入 ai/chatcoder-plan-*.md 时
    # 注入系统提醒。v3.1 (plan-102): 纠偏允许重试最多 _PLAN_CORRECTION_MAX 次
    # （部分 thinking 模型把方案写进思考内容后直接结束，从不调用 fs_write，
    #  一次提示往往不够）。
    _plan_correction_tries = 0
    # v6.5: 估算校准系数 -- 用 API 真实 prompt_tokens 动态校准估算值。
    # 字节/4 对中文偏高，不同模型/网关分词差异大，静态常量无法精准。
    # 每次拿到 API 真实值就更新系数，用于前置压缩估算，实现自适应精准压缩。
    # 校准系数按模型跨轮保留（进程内）：此前每次运行都从 1.0 重来，前几步的前置压缩判断总是偏离真实占用
    _calib_key = str(model_id or "default")
    # plan-527-2218: 系数按模型持久化（token_counter 落盘 store），重启后仍有效
    from app.orchestration.token_counter import get_calibration_factor as _get_calib
    _calib_factor = _get_calib(_calib_key)  # real / est
    # v2.2 (对齐 zcode 3.14): 重复工具调用检测——工具名+参数签名 → 连续次数
    _call_sigs: dict[str, int] = {}
    _last_sig: str | None = None  # 上一次执行的工具签名（连续重复判定用）
    # 待注入提醒（下一步循环构建 api_messages 后追加，避免被重建丢弃）
    _pending_reminders: list[str] = []
    # plan-64-291: 运行中子代理提醒的节流状态——等待期纪律需「持续在场」。
    # 主代理一旦在等待期开始自己干，就没有机制把它拉回来（用户实测：子代理仍在跑，
    # 主代理却把它的活干了，最终重复劳动、token 与时间双倍消耗）。
    _sub_mgr_for_reminder = (subagent_context or {}).get("manager") if subagent_context else None
    _subagent_reminded_sig: tuple[int, ...] = ()
    _subagent_reminded_at_step = 0
    # plan-20261020-bgtask：本轮自己启动、仍在运行的后台命令提醒的节流状态
    _bg_reminded_sig: tuple[str, ...] = ()
    _bg_reminded_at_step = 0
    # S12（plan-41-197）：user_prompt_submit 钩子——每轮任务开始时触发，
    # 提示词型钩子的文本并入本轮首批提醒（fail-open，不影响主流程）。
    try:
        for _hr in await hook_service.run_hooks(
            db, "user_prompt_submit",
            {"session_id": session_id, "turn_id": turn_id, "agent_kind": agent_kind},
        ):
            _inj = (_hr or {}).get("inject")
            if _inj:
                _pending_reminders.append(f"[钩子提示词]\n{_inj}")
    except Exception:
        logger.debug("[agent] user_prompt_submit 钩子执行失败(不阻断)", exc_info=True)
    # v31 (plan-89): 本 turn 是否已执行过工具（有产出）——空响应判定依据：
    # 已有产出时 finish=stop 空响应 = 任务完成正常结束（对齐 zcode/AI SDK），
    # 不重试不报错；零产出时保留重试兜底瞬时故障。
    _tool_executed = False
    # 本轮开始时刻（epoch 秒，惰性读取一次；判断文件是否「本轮才创建」用）
    _turn_t0_box: list[float | None] = []
    # plan-547 C1: 进度提醒计数——连续 N 步无面向用户的文字输出时注入提醒
    _steps_since_last_text = 0
    # plan-547 C3: 最近一次 todo_write 的清单全集（plan 模式每步注入状态标记）
    _last_todos: list[dict] = []
    # plan-19-82 增强（对齐 ZCode runtime-reminders）：周期性重申的步数游标。
    # 语言纪律/规则此前只在上下文构建时静态注入一次，长 turn 内会被工具结果稀释；
    # 这里按步重新注入，频率由 settings.language_reminder_interval /
    # rules_reminder_interval 控制（0 = 禁用）。
    _lang_reminded_at_step = 0
    _rules_reminded_at_step = 0
    # 语言漂移检测：记录已因"输出语言与锚定语言不符"注入过提醒的次数，
    # 避免模型持续英文时每步都注入（徒增上下文，且重复提醒无增量信息）。
    _lang_drift_reminders = 0

    # L3.1（plan-340-1705）：电脑操控的「场景标签 × 思考量」观测缓冲。
    # 只有本步真正调过 desktop_* 工具才非空——普通任务全程为空，零开销、零日志噪音。
    # 写操作集合用于区分「看了界面再动作」与「只是读取」两种场景。
    _desktop_write_tools = frozenset({
        "desktop_click", "desktop_type", "desktop_keys", "desktop_scroll", "desktop_batch",
        "desktop_focus", "desktop_apps",
    })
    _obs_tools: list[tuple[str, bool]] = []

    try:
        for step in range(1, loop_step_limit + 1):
            # plan-91-394: 每步开始登记检查点引用（列表原地追加，失败/暂停时读到的即最新进度）
            if checkpoint_sink is not None:
                try:
                    checkpoint_sink(messages)
                except Exception:
                    logger.debug("[agent] checkpoint 回调失败(非阻塞)", exc_info=True)
            if cancel_event and cancel_event.is_set():
                logger.warning("[agent] task turn=%s 收到中断信号", turn_id)
                return AgentOutput(kind="cancelled", error="任务被用户中断")

            await asyncio.sleep(0)

            # plan-547: drain 运行中注入的用户消息（前端"立即发送"），
            # 作为 user 消息进入本次调用的上下文——下次 AI 调用前传达
            if injected_inputs_provider is not None:
                try:
                    for _inj in injected_inputs_provider():
                        _txt = str(_inj.get("content") or "").strip()
                        _atts = _inj.get("attachments") or []
                        _blocks = None
                        _suffix = ""
                        if _atts and isinstance(_atts, list):
                            # plan-166-767: 注入路径直注入图片——复用 context_manager 的图片块实现，
                            # 否则运行中「立即发送」带图消息图片被静默丢弃（此前只取 content 文本）。
                            if multimodal:
                                from app.orchestration.context_manager import _load_inline_image_blocks
                                _blocks, _notes = _load_inline_image_blocks(_atts)
                                if _blocks:
                                    _suffix = f"\n（注入消息附带的 {len(_blocks)} 张图片已直接附在消息中，请直接查看）"
                            # 无论是否多模态都附上附件路径文本（read_attachment 可读），不丢信息
                            _paths = [
                                f"- {a.get('filename') or '(未命名)'}: path=`{a.get('path') or ''}`"
                                for a in _atts if isinstance(a, dict) and a.get("path")
                            ]
                            if _paths:
                                _suffix += "\n以下附件路径均可通过 read_attachment 读取：\n" + "\n".join(_paths)
                        # plan-166-767: 纯图片（无文字）注入项只要有图片块即保留，不再被 continue 丢弃
                        if not _txt and not _blocks:
                            continue
                        # plan-156-621：用户排队消息补一条并行分派提醒；子会话汇报等系统注入项不加。
                        # plan-20261022-release-ci：邮箱 / 子代理完成通知带 source 标记——此前只认旧的
                        # _persist_on_drop，子会话的提问、汇报、结论每条都被附上「请并行分派子会话」，
                        # 主代理读到结论反被催着再派一轮
                        if _parallel_reminder and not (_inj.get("_persist_on_drop") or _inj.get("source")):
                            _suffix = f"{_suffix}\n\n{_parallel_reminder}"
                        _msg = ChatMessage(role="user", content=(_txt + _suffix).strip() or None)
                        if _blocks:
                            _msg.content_blocks = _blocks
                        messages.append(_msg)
                        logger.info(
                            "[agent] turn=%s step=%s 注入用户消息 (%d chars, images=%d, model_id=%s)",
                            turn_id, step, len(_txt), len(_blocks or []), _inj.get("model_id"),
                        )
                except Exception:
                    logger.warning("[agent] turn=%s 注入消息 drain 失败(非阻塞)", turn_id, exc_info=True)

            # 上下文压缩（v6.4: 移除每步开头的估算式 should_auto_compact 检查）
            # 根因：估算 token 与 API 真实 prompt_tokens 偏差大，且当 model.context_window
            # 配置较小时会过早触发 auto_compact（用户反馈 20k 就摘要）。
            # 改为仅在 API 响应后用精确 prompt_tokens 判断（见下方 step 后段）。
            agent_window = 0
            try:
                from app.orchestration.compaction import build_api_copy, ensure_tool_pairing, normalize_tool_sequence, repair_tool_call_ids, take_last_reclaim as _take_reclaim
                from app.orchestration.token_counter import get_agent_context_window, estimate_messages_tokens as _est_tokens
                # v16: 窗口口径 = 实际调用的模型（effective model_id），与前端占用百分比、
                # 压缩阈值、重建窗口预算完全一致（此前用 agent 兜底模型，口径分裂）
                agent_window = await get_agent_context_window(db, agent, model_id=model_id)
                # v6.4: 最小窗口保护 —— 若 model.context_window 配置过小，用默认值兜底
                # 避免因模型窗口配置错误导致压缩阈值过低、过早摘要
                if agent_window < 100000:
                    logger.warning(
                        "[agent] turn=%s agent_window=%d 过小(<100k)，用默认 500000 兜底",
                        turn_id, agent_window,
                    )
                    agent_window = 500000
                # 修复重复/空 tool_call id（Gemini 等网关 400 根因），再配对/排序
                messages = repair_tool_call_ids(messages)
                messages = ensure_tool_pairing(messages)
                # v8 根治: 强制 assistant(tool_calls) 后紧跟 tool 结果，杜绝 400
                messages = normalize_tool_sequence(messages)
                # v16: build_api_copy 不再按占用占比折叠历史（隐式压缩已移除），
                # 只保留单条超长工具结果的落盘保护；压缩仅由超设置阈值的
                # 落库式压缩（可见压缩卡片）完成。
                # plan-20261020-pts：超长结果落盘到本会话工作区，模型按占位提示 fs_read 才读得回
                api_messages = build_api_copy(messages, workspace)
                # plan-282-1441：把本轮"上下文回收"如实告知用户（此前静默折叠，
                # 用户只看到占用突然降几十 k，误以为丢历史——即"隐藏压缩"的观感来源）。
                try:
                    _reclaim = _take_reclaim()
                    if _reclaim and (_reclaim.get("folded") or _reclaim.get("reasoning_stripped")
                                     or _reclaim.get("est_tokens_saved", 0) > 2000):
                        await broadcast(session_id, {
                            "event": "context.folded",
                            "payload": {
                                "agent_id": agent_id, "agent_name": agent_name,
                                "turn_id": turn_id,
                                "folded_results": _reclaim.get("folded", 0),
                                # plan-282-0: 旧工具回合的 reasoning 被出站剥离的条数
                                "reasoning_stripped": _reclaim.get("reasoning_stripped", 0),
                                "est_tokens_saved": _reclaim.get("est_tokens_saved", 0),
                                "budget_tokens": _reclaim.get("budget"),
                                "context_window": agent_window,
                            },
                        })
                except Exception:  # noqa: BLE001 —— 提示失败不影响请求
                    logger.debug("[agent] 上下文回收提示广播失败", exc_info=True)

                # v6.5: 前置压缩检查 -- 用校准系数调整估算，超阈值则先压缩
                # plan-282-0: **必须把 tools schema 计入**——它随每次请求独立发送，
                # 不属于 messages，此前完全漏算（实测 57 个工具占掉近半 prompt），
                # 是「压缩器自报 14.4% 而真实占用 69%」口径缺口的重要来源。
                from app.orchestration.token_counter import estimate_tools_tokens as _est_tools
                _tools_tokens = _est_tools(tool_schemas)
                _est_raw = _est_tokens(api_messages) + _tools_tokens
                _est_prompt = int(_est_raw * _calib_factor)
                # 阈值为窗口的比例，再扣掉为本次输出预留的余量（否则输出一吃满就会超窗）；下限取窗口的一半
                _out_reserve = min(settings.agent_max_output_tokens or 8192, agent_window // 4)
                _pre_threshold = max(
                    int(agent_window * 0.5),
                    int(agent_window * settings.auto_compact_threshold_ratio) - _out_reserve,
                )
                if _est_prompt >= _pre_threshold:
                    logger.info("[agent] turn=%s step=%s 前置估算 prompt=%d (raw=%d calib=%.3f) >= 阈值 %d，调用前压缩", turn_id, step, _est_prompt, _est_raw, _calib_factor, _pre_threshold)
                    await broadcast(session_id, {"event": "compact.started", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id, "used_tokens": _est_prompt, "context_window": agent_window, "ratio": round(_est_prompt / agent_window * 100, 1)}})
                    # v30: 优先落库式压缩（持久化 checkpoint），失败回退内存式 auto_compact
                    # plan-560-2311 A2：带上线程身份，子代理不做落库式压缩
                    _cc_pre, messages = await _compact_persistent_or_fallback(
                        db, session_id=session_id, provider=provider, agent_window=agent_window,
                        used_tokens=_est_prompt, agent_id=agent_id, agent_name=agent_name,
                        turn_id=turn_id, messages=messages, trigger="pressure",
                        reply_language=reply_language,
                        thread_id=thread_id, agent_kind=agent_kind,
                    )
                    messages = repair_tool_call_ids(messages)
                    messages = ensure_tool_pairing(messages)
                    messages = normalize_tool_sequence(messages)
                    api_messages = build_api_copy(messages, workspace)
                    _est_after = int((_est_tokens(api_messages) + _tools_tokens) * _calib_factor)
                    logger.info("[agent] turn=%s step=%s 前置压缩后 prompt=%d -> %d (persistent=%s)", turn_id, step, _est_prompt, _est_after, bool(_cc_pre))
                    await broadcast(session_id, {"event": "usage.update", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id, "prompt_tokens": _est_after, "completion_tokens": 0, "total_tokens": _est_after, "context_window": agent_window, "usage_source": "est_after_compact", "cached_input_tokens": 0, "reasoning_tokens": 0, "agent_kind": agent_kind}})
                    await broadcast(session_id, {"event": "compact.completed", "payload": {"agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id, **(_cc_pre or {})}})
                else:
                    logger.debug("[agent] turn=%s step=%s 前置估算 prompt=%d (raw=%d calib=%.3f) < 阈值 %d，不压缩", turn_id, step, _est_prompt, _est_raw, _calib_factor, _pre_threshold)
            except Exception:
                logger.warning("[agent] turn=%s step=%s 前置压缩检查失败，降级为原始消息继续", turn_id, step, exc_info=True)
                api_messages = messages

            # plan-19-82 增强（对齐 ZCode runtime-reminders）：周期性重申语言纪律与规则优先级。
            # 只在本步骤首次进入该区间时注入，注入后更新游标（避免同一提醒连续多步重复）。
            # 注意：api_messages 在上方可能已被压缩重建，这里追加到末尾——位置贴近当次
            # 模型调用，正是 ZCode 强调的"贴近动作点"约束。
            if (reply_language != "auto"
                    and settings.language_reminder_interval > 0
                    and step - _lang_reminded_at_step >= settings.language_reminder_interval):
                from app.orchestration.prompts import build_language_reminder
                api_messages = [*api_messages, ChatMessage(
                    role="system", content=build_language_reminder(
                        reply_language, source=reply_language_source,
                    ),
                )]
                _lang_reminded_at_step = step
                logger.info("[agent] turn=%s step=%s 注入语言重申（间隔 %d 步，lang=%s）",
                            turn_id, step, settings.language_reminder_interval, reply_language)
            if (settings.rules_reminder_interval > 0
                    and step - _rules_reminded_at_step >= settings.rules_reminder_interval):
                from app.orchestration.prompts import build_rules_reminder
                api_messages = [*api_messages, ChatMessage(
                    role="system", content=build_rules_reminder(reply_language),
                )]
                _rules_reminded_at_step = step
                logger.info("[agent] turn=%s step=%s 注入规则重申（间隔 %d 步）",
                            turn_id, step, settings.rules_reminder_interval)

            # v2.2: todo 提醒——存在清单且连续 N 步未更新 → 注入 system 提醒
            # （对齐 ZCode buildTodoReminderBody：防模型"开清单后跑偏"）
            if _todo_active and step - _todo_updated_at_step >= settings.todo_reminder_interval:
                api_messages = [*api_messages, ChatMessage(
                    role="system",
                    content=(
                        "[任务清单提醒] 本任务存在执行清单，但最近几步未调用 todo_write 更新。"
                        "请审视当前进度：已完成步骤标记 completed，进行中步骤标记 in_progress，"
                        "必要时调整清单内容后再继续执行。"
                    ),
                )]
                _todo_updated_at_step = step  # 重置计数，避免每步重复注入
                logger.info(
                    "[agent] turn=%s step=%s 注入 todo 提醒（清单 %d 步未更新）",
                    turn_id, step, settings.todo_reminder_interval,
                )
            # plan-64-291: 运行中子代理提醒——等待期纪律「持续在场」。
            # 节流：运行集合变化（新派发/完工）立即注入一次；集合不变时每
            # _SUBAGENT_REMINDER_STEP_GAP 步才重复一次，避免每步都占上下文。
            if _sub_mgr_for_reminder is not None:
                try:
                    _running_now = _sub_mgr_for_reminder.running_snapshot()
                except Exception:
                    _running_now = []
                _running_sig = tuple(sorted(int(_r.get("agent_id", 0)) for _r in _running_now))
                if _should_emit_running_reminder(
                    has_running=bool(_running_now), sig=_running_sig,
                    last_sig=_subagent_reminded_sig, step=step,
                    last_step=_subagent_reminded_at_step,
                ):
                    _pending_reminders.append(
                        _build_subagent_running_reminder(
                            _running_now,
                            # plan-549-2281：把额度一并告知（上限取当前设置，占用取实时数）
                            max_conc=max(1, min(30, int(getattr(settings, "max_concurrent_subagents", 4) or 4))),
                            pending=int(_sub_mgr_for_reminder.pending_count()),
                        )
                    )
                    _subagent_reminded_sig = _running_sig
                    _subagent_reminded_at_step = step
                elif not _running_now:
                    # 全部结束：复位签名，下一批新派发的子代理能立即触发一次提醒
                    _subagent_reminded_sig = ()

            # plan-20261020-bgtask：本轮自己启动、仍在运行的后台命令——长驻服务用完必须主动停。
            # 只在主线程提醒（子代理线程不替主代理管命令）；只提醒**本轮**启动的，
            # 前几轮有意留给用户使用的服务不再纠缠。节流同子代理提醒：集合变化立即提醒一次，
            # 不变时每 _BG_REMINDER_STEP_GAP 步才重复，避免占上下文。
            if thread_id is None:
                try:
                    from app.orchestration.tools.bg_process import bg_process_registry as _bg_registry

                    _bg_mine = [r for r in _bg_registry.list_session(session_id) if r.get("turn_id") == turn_id]
                except Exception:  # noqa: BLE001 — 提醒失败不影响主循环
                    _bg_mine = []
                _bg_sig = tuple(sorted(str(r.get("shell_id")) for r in _bg_mine))
                if _bg_mine and (_bg_sig != _bg_reminded_sig
                                 or step - _bg_reminded_at_step >= _BG_REMINDER_STEP_GAP):
                    _pending_reminders.append(_build_bg_running_reminder(_bg_mine))
                    _bg_reminded_sig = _bg_sig
                    _bg_reminded_at_step = step
                elif not _bg_mine:
                    _bg_reminded_sig = ()

            # v2.2: 注入待发的重复调用提醒（上一步工具执行中检测到）
            if _pending_reminders:
                api_messages = [*api_messages, *[
                    ChatMessage(role="system", content=r) for r in _pending_reminders
                ]]
                # S12（plan-41-197）：就地清空（而非重新绑定）——审批回调等闭包共享
                # 同一列表引用，重新绑定会让闭包持有的引用失效（钩子注入丢失）。
                _pending_reminders.clear()

            # plan-547 C3: plan 模式每次调用注入计划全集与状态标记——
            # 模型在任意一步都看得到完整清单与进度，防止长规划跑偏或遗漏步骤
            # plan-562-2318 M1：判定改走**判定档**（`_mode_kind`，按 kind 归一）——
            # 此前用原始模式名比较，自定义 kind=plan 模式拿不到这份每步清单。
            if _mode_kind(permission_mode) == "plan" and _last_todos:
                _plan_lines = []
                for _t in _last_todos:
                    _c_item = str(_t.get("content") or "").strip()
                    if _c_item:
                        _plan_lines.append(f"- [{_t.get('status', 'pending')}] {_c_item}")
                if _plan_lines:
                    api_messages = [*api_messages, ChatMessage(
                        role="system",
                        content="[计划状态全集] 当前执行清单（plan 模式，每步随请求提供）：\n"
                                + "\n".join(_plan_lines)
                                + "\n请对照清单继续：完成一项立即用 todo_write 标记 completed；"
                                  "全部完成后输出方案总结并结束本轮。",
                    )]

            request = ChatRequest(
                messages=api_messages, model="",
                tools=tool_schemas or None,
                temperature=settings.agent_tool_temperature if tool_schemas else settings.agent_text_temperature,
                reasoning_effort=reasoning_effort or (settings.agent_reasoning_effort if tool_schemas else None),
                max_tokens=settings.agent_max_output_tokens or None,
                # TRAE create_agent_task 要求稳定的云端会话/消息标识；其它 Provider 忽略。
                session_id=str(session_id),
                message_id=f"{session_id}-{turn_id}-{step}",
                # v21: thinking 模式（provider 会据此发 thinking 参数并移除 temperature）
                thinking=_thinking_flag,
                # plan-270-1358: ta3 x-ws-id 需要工作目录指纹（其它 Provider 忽略）
                workspace_dir=workspace or None,
            )
            # v6.4 临时诊断：打印实际发送给 API 的消息数和角色分布
            if step == 1:
                _role_counts = {}
                for _m in api_messages:
                    _role_counts[_m.role] = _role_counts.get(_m.role, 0) + 1
                logger.warning(
                    "[agent] turn=%s step=1 发送给API的消息数=%d 角色分布=%s",
                    turn_id, len(api_messages), _role_counts,
                )

            try:
                response = await _stream_chat_and_broadcast(
                    provider, request,
                    session_id=session_id, turn_id=turn_id,
                    agent_id=agent_id, agent_name=agent_name,
                    cancel_event=cancel_event,
                    thread_id=thread_id,
                    reply_language=reply_language,
                )
                if response.finish_reason == "language_mismatch" and reply_language in ("zh", "en"):
                    from app.orchestration.prompts import build_language_reminder
                    request.messages = [*request.messages, ChatMessage(
                        role="system",
                        content=build_language_reminder(reply_language, source=reply_language_source)
                        + " 上一次正文开头语言不符，尚未展示给用户。请从头用目标语言重写。",
                    )]
                    response = await _stream_chat_and_broadcast(
                        provider, request,
                        session_id=session_id, turn_id=turn_id,
                        agent_id=agent_id, agent_name=agent_name,
                        cancel_event=cancel_event,
                        thread_id=thread_id,
                        reply_language="auto",
                    )
                # plan-248-1258 M2.2: 凭据调用成功 → 清冷却并刷新粘性（后续优先复用该凭据）
                # plan-20261020-retry-cred-perf：走统一记账——非 ta3 每次都刷新「最近一次成功」
                # （内部 60 秒节流落盘），ta3 保持「每个 loop 每凭据只记一次」。
                await _report_credential(db, provider, None, _ok_reported)
                # v19: 兜底剥离内联思考标签（流式路径已剥离则为 no-op）
                if response.content:
                    _c, _t = _split_inline_thinking(response.content)
                    if _t:
                        response.content = _c or None
                        response.thinking = (response.thinking + "\n" + _t).strip() if response.thinking else _t
                # v6.4: 流式调用被用户中断 → 立即退出循环
                if cancel_event and cancel_event.is_set():
                    logger.warning("[agent] turn=%s 流式调用后检测到中断信号，退出循环", turn_id)
                    final_text = response.content or ""
                    if final_text:
                        await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                              agent_id=agent_id, agent_name=agent_name,
                                              msg_type=MsgType.TEXT,
                                              content={"text": final_text, "agent_name": agent_name})
                    return AgentOutput(kind="cancelled", error="任务被用户中断")
                # v28: 模型响应健康检查——空响应/超时中断/输出截断必须对用户可见，
                # 不再静默结束 turn（修复"突然停止且无报错"）。
                _failure = _response_failure_reason(response, _tool_executed)
                if _failure is not None:
                    _reason, _fatal = _failure
                    logger.warning("[agent] turn=%s 模型响应异常: %s", turn_id, _reason)
                    # v29 (plan-78): 空响应/思考超时中断多为瞬时故障——kimi-k3 等长思考
                    # 模型思考阶段被网关提前终结 SSE 流（无任何产出帧）。旧逻辑直接 fatal
                    # 杀死整个 turn，表现为"经常中断报错"。改为按降档序列自动重试：
                    # 每次降低思考档位（默认 high→low→关闭），提高拿到内容/工具调用的概率。
                    # v45: 统一异常重试——任何 fatal 响应（空响应/超时中断/零帧断流）
                    # 都按配置的重试计划（默认 10/20/30 秒）依次重试，穷尽后才停止并显示报错。
                    # 重试时按降档序列降低思考强度（effort），提高拿到有效响应的概率。
                    # R7: 空响应/断流也是凭据失败——记一次并换下一个 Key（旧逻辑只在异常路径记账）
                    if _fatal:
                        _resp_failure = CallFailure(kind=classify_failure(_reason).kind, text=_reason)
                        _cid0 = getattr(provider, "credential_row_id", None)
                        if _cid0 is not None:
                            _failed_creds.add(_cid0)
                        await _report_credential(db, provider, _resp_failure, _ok_reported)
                    _retry_waits = _retry_plan()
                    _retry_attempts = 0
                    if _fatal and _retry_waits:
                        _efforts = settings.agent_empty_retry_effort_list
                        _state = {"provider": provider}

                        def _health(resp):
                            """重试响应同样过健康检查（R3）：非致命（带正文的超时/截断）不算失败。"""
                            _rf = _response_failure_reason(resp, _tool_executed)
                            if _rf is None or not _rf[1]:
                                return None
                            return CallFailure(kind=classify_failure(_rf[0]).kind, text=_rf[0])

                        async def _outcome(_att, _resp, _fail):
                            await _report_credential(db, _state["provider"], _fail, _ok_reported)

                        try:
                            _rec = await recover_model_call(
                                call=lambda _att: _retry_stream_call(
                                    db=db, model=_cur_model, state=_state,
                                    api_messages=api_messages, tool_schemas=tool_schemas,
                                    session_id=session_id, turn_id=turn_id, step=step,
                                    agent_id=agent_id, agent_name=agent_name,
                                    thread_id=thread_id, cancel_event=cancel_event,
                                    reasoning_effort=reasoning_effort, thinking=_thinking_flag,
                                    workspace=workspace, effort=_att.effort,
                                    index_no=_att.index, failed_creds=_failed_creds,
                                ),
                                check=_health,
                                intervals=_retry_waits,
                                wait=lambda _s: _wait_retry_interval(cancel_event, _s),
                                cancelled=lambda: bool(cancel_event and cancel_event.is_set()),
                                efforts=_efforts,
                                per_round=_credential_slots(_cred_total),
                                on_status=lambda _t: _broadcast_turn_status(
                                    session_id, turn_id, thread_id, _t),
                                on_outcome=_outcome,
                            )
                        except RecoveryCancelled:
                            logger.warning(
                                "[agent] turn=%s 响应异常重试期间收到中断信号，停止重试（原始原因：%s）",
                                turn_id, _reason,
                            )
                            return AgentOutput(kind="cancelled", error="任务被用户中断")
                        provider = _state["provider"]
                        _retry_attempts = _rec.attempts
                        if _rec.ok:
                            response = _rec.response
                            _failure = None
                            logger.info("[agent] turn=%s step=%s 响应异常重试成功 (attempts=%d)",
                                        turn_id, step, _rec.attempts)
                            # v35: 重试成功 → 状态行切换为恢复提示（流式 delta 到达后前端自动清除）
                            await _broadcast_turn_status(
                                session_id, turn_id, thread_id, "已恢复正常，继续执行…",
                            )
                        elif _rec.failure is not None and _rec.failure.is_client:
                            # 重试途中变成请求内容问题（上下文溢出等）：交回外层修复后重试（R4）
                            logger.warning("[agent] turn=%s 重试期间转为请求内容问题: %s",
                                           turn_id, _rec.failure.text[:200])
                            raise RuntimeError(_rec.failure.text)
                        else:
                            logger.warning("[agent] turn=%s step=%s 响应异常重试仍失败（attempts=%d）: %s",
                                           turn_id, step, _rec.attempts, _reason)
                    if _failure is not None:
                        # v968: 按配置次数重试穷尽后报错——文案注明已重试次数，
                        # 用户能明确知道系统已尽力（而非再次误以为"无故中断"）。
                        _err_text = _reason
                        if _fatal and _retry_waits:
                            _err_text = _retry_exhausted_text(
                                _reason, _retry_attempts, _retry_waits, _cred_total)
                        # plan-365-1790: 致命响应终止前记录诊断字段——
                        # 以 finish/frames/usage 三者组合区分「网关截断」与「模型真无输出」。
                        logger.warning(
                            "[agent] turn=%s step=%s 响应异常终止 fatal=%s finish=%s frames=%s "
                            "usage=%s tool_calls=%d content_len=%d thinking_len=%d reason=%s",
                            turn_id, step, _fatal, response.finish_reason,
                            response.frames_received, response.usage is not None,
                            len(response.tool_calls or []), len(response.content or ""),
                            len(response.thinking or ""), _reason,
                        )
                        await _emit_agent_msg(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            agent_id=agent_id, agent_name=agent_name,
                            msg_type=MsgType.ERROR,
                            content={"text": _err_text, "agent_name": agent_name},
                        )
                        if _fatal:
                            return AgentOutput(kind="error", error=_err_text,
                                               transient=_is_transient_failure_text(_reason))
            except Exception as api_err:
                logger.warning("[agent] turn=%s 模型调用失败，尝试修复/压缩: %s", turn_id, str(api_err)[:200])
                # 先兜底初始化：下方 except 分支会读它们，若在赋值前就抛异常不能变成 NameError
                _repair_waits: list[float] = []
                _rec = None
                try:
                    _err_msg = str(api_err)
                    # R7: 进入恢复流程前先记一次失败。client 类（溢出/function-call 400）
                    # 不计入凭据健康、也不换 Key——修请求内容才有用。
                    _failure0 = classify_failure(_err_msg)
                    if not _failure0.is_client:
                        _cid0 = getattr(provider, "credential_row_id", None)
                        if _cid0 is not None:
                            _failed_creds.add(_cid0)
                        await _report_credential(db, provider, _failure0, _ok_reported)
                    _repair_waits = _retry_plan()
                    if not _repair_waits:
                        raise api_err  # 未配置重试：交回外层按失败收尾
                    # plan-20261020-retry-cred-perf R4：修复/压缩只负责改请求内容，之后的失败
                    # 进入同一套轮式重试（不再「修完只给一次机会，再失败就终止」）。
                    for _repair_round in range(1, 3):
                        _fc_400 = (
                            "Please ensure that function call" in _err_msg
                            or "called exactly once" in _err_msg
                            or "must be followed by tool" in _err_msg
                            or "role 'tool' must be a response" in _err_msg
                        )
                        if _fc_400:
                            from app.orchestration.compaction import (
                                build_api_copy as _build_api_fc,
                                ensure_tool_pairing as _ensure_pairing,
                                normalize_tool_sequence as _norm_fc,
                                repair_tool_call_ids as _repair_fc,
                            )
                            messages = _repair_fc(messages)
                            messages = _ensure_pairing(messages)
                            messages = _norm_fc(messages)
                            # v37: 重试发送前同样构建 api_messages 副本
                            api_messages = _build_api_fc(messages, workspace)
                            logger.info("[agent] turn=%s function-call 400 已修复请求，进入重试", turn_id)
                        elif _is_context_overflow_error(_err_msg):
                            # v33: 仅真正的上下文溢出才走紧急压缩（对齐 deepseek-harness：
                            # context-overflow 只对 provider 确认的溢出恢复触发）。
                            from app.orchestration.compaction import (
                                build_api_copy as _build_api_dc,
                                emergency_compact,
                            )
                            from app.orchestration.token_counter import get_agent_context_window
                            # v16: 与主循环同口径——实际调用模型的窗口
                            agent_window = await get_agent_context_window(db, agent, model_id=model_id)
                            # v30: 溢出恢复优先落库式（保留最近6回合 + LLM 摘要 + 限次重试），
                            # 失败回退旧硬编码 emergency_compact。
                            # plan-560-2311 A2：带上线程身份，子代理不做落库式压缩
                            _cc_ovf, messages = await _compact_persistent_or_fallback(
                                db, session_id=session_id, provider=provider, agent_window=agent_window,
                                used_tokens=None, agent_id=agent_id, agent_name=agent_name,
                                turn_id=turn_id, messages=messages, trigger="context-overflow",
                                reply_language=reply_language,
                                thread_id=thread_id, agent_kind=agent_kind,
                            )
                            if _cc_ovf is None:
                                messages = emergency_compact(messages, agent_window)
                            # 紧急压缩后同样修复 id + 规范化工具消息序列，防止孤立 tool / 夹层消息
                            messages = repair_tool_call_ids(messages)
                            messages = normalize_tool_sequence(messages)
                            # R5: 与首次同一份 payload（含超长结果落盘保护、旧推理剥离）
                            api_messages = _build_api_dc(messages, workspace)
                            logger.info("[agent] turn=%s 上下文溢出已压缩，进入重试", turn_id)
                        else:
                            # v33: 429/503/连接/超时等瞬时故障——压缩无意义且会拖垮 turn
                            logger.warning(
                                "[agent] turn=%s step=%s 模型调用失败(非溢出，不压缩): %s",
                                turn_id, step, _err_msg[:200],
                            )

                        _state = {"provider": provider}

                        def _health(resp):
                            """R3: 恢复得到的响应同样过健康检查，空响应/断流不能当正常结果。"""
                            _rf = _response_failure_reason(resp, _tool_executed)
                            if _rf is None or not _rf[1]:
                                return None
                            return CallFailure(kind=classify_failure(_rf[0]).kind, text=_rf[0])

                        async def _outcome(_att, _resp, _fail):
                            await _report_credential(db, _state["provider"], _fail, _ok_reported)

                        try:
                            _rec = await recover_model_call(
                                call=lambda _att: _retry_stream_call(
                                    db=db, model=_cur_model, state=_state,
                                    api_messages=api_messages, tool_schemas=tool_schemas,
                                    session_id=session_id, turn_id=turn_id, step=step,
                                    agent_id=agent_id, agent_name=agent_name,
                                    thread_id=thread_id, cancel_event=cancel_event,
                                    reasoning_effort=reasoning_effort, thinking=_thinking_flag,
                                    workspace=workspace, effort=_att.effort,
                                    index_no=_att.index, failed_creds=_failed_creds,
                                ),
                                check=_health,
                                intervals=_repair_waits,
                                wait=lambda _s: _wait_retry_interval(cancel_event, _s),
                                cancelled=lambda: bool(cancel_event and cancel_event.is_set()),
                                per_round=_credential_slots(_cred_total),
                                on_status=lambda _t: _broadcast_turn_status(
                                    session_id, turn_id, thread_id, _t),
                                on_outcome=_outcome,
                            )
                        except RecoveryCancelled:
                            logger.warning(
                                "[agent] turn=%s 调用异常重试等待期间收到中断信号，停止重试（原始错误：%s）",
                                turn_id, str(api_err)[:200],
                            )
                            return AgentOutput(kind="cancelled", error="任务被用户中断")
                        provider = _state["provider"]
                        if _rec.ok:
                            response = _rec.response
                            logger.info("[agent] turn=%s step=%s 模型调用恢复成功（attempts=%d）",
                                        turn_id, step, _rec.attempts)
                            # v35: 重试成功 → 清除重试状态（下一次流式 delta 或 turn 结束也会兜底清除）
                            await _broadcast_turn_status(session_id, turn_id, thread_id, "")
                            break
                        if (_rec.failure is not None and _rec.failure.is_client
                                and _repair_round < 2):
                            # 换 Key/等待都无效，属于请求内容问题：再修一次请求后重进引擎
                            _err_msg = _rec.failure.text
                            logger.warning(
                                "[agent] turn=%s 恢复失败仍属请求内容问题，重新修复后重试: %s",
                                turn_id, _err_msg[:200])
                            continue
                        raise api_err
                    if _rec is None or not _rec.ok:
                        raise api_err
                    # 重试路径同样剥离内联思考标签
                    if response.content:
                        _c, _t = _split_inline_thinking(response.content)
                        if _t:
                            response.content = _c or None
                            response.thinking = (response.thinking + "\n" + _t).strip() if response.thinking else _t
                except RecoveryCancelled:
                    return AgentOutput(kind="cancelled", error="任务被用户中断")
                except Exception as retry_err:
                    logger.error("[agent] turn=%s 紧急压缩重试失败: %s", turn_id, retry_err)
                    _final_err = str(api_err)[:200]
                    if _repair_waits:
                        _final_err = _retry_exhausted_text(
                            _final_err, _rec.attempts if _rec is not None else 0,
                            _repair_waits, _cred_total)
                    await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                          agent_id=agent_id, agent_name=agent_name,
                                          msg_type=MsgType.ERROR,
                                          content={"text": f"模型调用失败: {_final_err}", "agent_name": agent_name})
                    return AgentOutput(kind="error", error=f"模型调用失败: {_final_err}",
                                       transient=_is_transient_failure_text(str(api_err)))

            # 诊断日志：每步模型输出摘要
            logger.info(
                "[agent] turn=%s step=%s finish=%s calls=%s content_len=%s",
                turn_id, step, response.finish_reason,
                [tc.get("name") for tc in (response.tool_calls or [])],
                len(response.content or ""),
            )

            # v34: 过滤退化工具调用（参数=正文逐字片段的只读调用），防止误导性工具卡片
            _filter_degenerate_tool_calls(response)

            # v6.5: token 统计与广播 -- 网关可能不返回 usage（stream 末尾无 usage chunk），
            # 此时用 estimate_messages_tokens 估算 prompt_tokens 兜底，确保：
            # 1) usage.update 总被广播（前端占用显示实时更新）
            # 2) auto_compact 压缩逻辑能基于真实占用触发（避免永不压缩）
            from app.orchestration.token_counter import estimate_messages_tokens as _est_tokens
            # plan-282-0: 兜底估算同样计入 tools schema（与前置判定口径一致）
            from app.orchestration.token_counter import estimate_tools_tokens as _est_tools
            _est_prompt = (_est_tokens(api_messages) + _est_tools(tool_schemas)) if api_messages else 0
            _api_prompt = 0
            _api_completion = 0
            _api_total = 0
            _usage_source = "none"
            if response.usage:
                _api_prompt = response.usage.prompt_tokens or 0
                _api_completion = response.usage.completion_tokens or 0
                _api_total = response.usage.total_tokens or 0
                if _api_prompt > 0:
                    _usage_source = "api"

            if _usage_source == "none":
                # 网关未返回 usage，用估算值兜底
                _final_prompt = _est_prompt
                _final_completion = 0
                logger.warning(
                    "[agent] turn=%s step=%s 网关未返回 usage，用估算 prompt=%d (est) 兜底",
                    turn_id, step, _est_prompt,
                )
            else:
                _final_prompt = _api_prompt
                _final_completion = _api_completion
                # v6.5: 用 API 真实值更新校准系数（real/est），指数平滑避免抖动。
                # 下一次前置估算会更准，实现自适应精准压缩。
                # v33: 钳制到 [0.3, 3.0]——网关偶发把 cached/虚计值混入 prompt_tokens
                # 时，raw 系数会异常膨胀导致前置估算虚高、过早触发压缩（低占用就压缩）。
                if _est_prompt > 0 and _api_prompt > 0:
                    from app.orchestration.token_counter import (
                        update_calibration_factor as _update_calib,
                    )
                    # 本次实测比值（real/est）单独记录：它是判断估算口径是否漂移的直接依据
                    _measured_factor = _api_prompt / _est_prompt
                    _calib_factor = _update_calib(_calib_key, _api_prompt, _est_prompt)
                    logger.debug(
                        "[agent] turn=%s step=%s 校准系数更新: %.3f (real=%d est=%d measured=%.3f)",
                        turn_id, step, _calib_factor, _api_prompt, _est_prompt,
                        _measured_factor,
                    )

            total_tokens += (_api_total or (_final_prompt + _final_completion))
            if token_budget and total_tokens > token_budget:
                logger.warning("[agent] turn=%s token 预算熔断", turn_id)
                final_text = response.content or "(预算耗尽，任务中止)"
                break

            # v6.5: total_tokens 发送 prompt_tokens（真实当前上下文占用），
            # prompt_tokens 包含 system+history+tools+当前输入，是"窗口被占用了多少"的真实值。
            # v2.2 (对齐 zcode 3.10): 用量分类 breakdown（前端用量环 hover 展示分类条）
            _breakdown: dict = {}
            try:
                from app.orchestration.token_counter import estimate_breakdown
                _breakdown = estimate_breakdown(api_messages)
            except Exception:
                logger.debug("[agent] breakdown 估算失败(非阻塞)", exc_info=True)

            await broadcast(session_id, {
                "event": "usage.update",
                "payload": {
                    "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                    "prompt_tokens": _final_prompt,
                    "completion_tokens": _final_completion,
                    "total_tokens": _final_prompt,
                    "context_window": agent_window,
                    "usage_source": _usage_source,
                    "cached_input_tokens": getattr(response.usage, 'cached_input_tokens', 0) or 0 if response.usage else 0,
                    "reasoning_tokens": getattr(response.usage, 'reasoning_tokens', 0) or 0 if response.usage else 0,
                    "breakdown": _breakdown,
                    # v19: 前端圆环仅统计主代理占用，子代理不覆盖
                    "agent_kind": agent_kind,
                },
            })
            logger.info(
                "[agent] turn=%s step=%s 上下文占用 prompt=%d (source=%s) / window=%d = %.1f%%",
                turn_id, step, _final_prompt, _usage_source, agent_window,
                (_final_prompt / agent_window * 100) if agent_window > 0 else 0,
            )

            # L3.1（plan-340-1705）：电脑操控的「场景标签 × 思考量」观测埋点。
            # 只记录、不改变任何行为——本版明确不据此自动调整思考档位（依据不足时降档会伤准确率）。
            # 场景取自「紧邻的上一步对电脑做了什么」：刚看完界面只需挑动作，操作失败才需要诊断。
            if _obs_tools:
                try:
                    _obs_name = _obs_tools[-1][0]
                    _obs_scene = _desktop_obs_scene(_obs_tools, _desktop_write_tools)
                    logger.info(
                        "[desktop-obs] turn=%s step=%s scene=%s last_tool=%s "
                        "reasoning_tokens=%d prompt=%d completion=%d",
                        turn_id, step, _obs_scene, _obs_name,
                        (getattr(response.usage, "reasoning_tokens", 0) or 0)
                        if response.usage else 0,
                        _final_prompt, _final_completion,
                    )
                except Exception:
                    logger.debug("[desktop-obs] 埋点失败(非阻塞)", exc_info=True)
                _obs_tools = []

            # v1.1: 用量流水落库（全软件统计的数据源），失败不阻断主流程
            # v0.3.1: 改用独立会话——此前在主 db 上 commit 失败 → rollback 主 db，
            # 主 db 会话上已加载对象全部过期，后续属性访问触发同步 reload 抛
            # MissingGreenlet。write-behind 缓冲项只带标量，天然规避该问题。
            try:
                from app.persistence.models.usage_record import UsageRecord  # noqa: F401
                _model_name = ""
                _provider_name = ""
                _m = None
                _p = None
                if model_id is not None:
                    from app.persistence.models.model_reg import Model, Provider
                    _m = await db.get(Model, model_id)
                    _model_name = _m.name if _m else ""
                    # plan-152-704: 供应商显示名，区分不同供应商的同名模型
                    if _m is not None and _m.provider_id is not None:
                        _p = await db.get(Provider, _m.provider_id)
                        _provider_name = _p.name if _p else ""
                # plan-20261021-usage-update：这次调用实际用的账号 / Key 快照，调用记录表展示用
                from app.services.usage_service import credential_snapshot as _cred_snapshot
                _cred_id, _cred_label, _cred_hint = await _cred_snapshot(
                    db, getattr(provider, "credential_row_id", None), _p, _m,
                )
                from app.persistence.write_behind import WriteItem, write_behind
                _buf = write_behind.get(session_id, turn_id)
                _buf.enqueue(WriteItem("usage", {
                    "session_id": session_id, "turn_id": turn_id, "agent_id": agent_id,
                    "model_id": model_id, "model_name": _model_name,
                    "provider_name": _provider_name,
                    "prompt_tokens": _final_prompt, "completion_tokens": _final_completion,
                    "reasoning_tokens": (getattr(response.usage, "reasoning_tokens", 0) or 0) if response.usage else 0,
                    "cached_tokens": (getattr(response.usage, "cached_input_tokens", 0) or 0) if response.usage else 0,
                    "usage_source": _usage_source,
                    "first_token_ms": getattr(response, "first_token_ms", None),
                    "gen_ms": getattr(response, "gen_ms", None),
                    "credential_id": _cred_id, "credential_label": _cred_label,
                    "credential_hint": _cred_hint,
                }))
                # v1.1: 主代理同步持久化最后一次真实占用（重启/切会话后圆环口径一致）
                if thread_id is None and _final_prompt > 0:
                    from datetime import datetime
                    _buf.enqueue(WriteItem("session_usage", {
                        "session_id": session_id,
                        "last_prompt_tokens": _final_prompt,
                        "last_usage_at": str(datetime.utcnow()),
                    }))
            except Exception:
                logger.debug("usage 流水落库失败(非阻塞)", exc_info=True)

            # v6.4: 调用后真实占用驱动压缩 -- 用 API 精确 prompt_tokens 判断
            # 替代旧的 should_auto_compact 估算检查，避免过早摘要。
            # 阈值 = agent_window * auto_compact_threshold_ratio (默认 0.90)
            if _final_prompt > 0 and agent_window > 0:
                _real_ratio = _final_prompt / agent_window
                _threshold = settings.auto_compact_threshold_ratio
                if _real_ratio >= _threshold:
                    logger.info(
                        "[agent] turn=%s step=%s 真实上下文占用 %.1f%% (p=%d/w=%d) >= %.0f%%，触发自动压缩",
                        turn_id, step, _real_ratio * 100, _final_prompt,
                        agent_window, _threshold * 100,
                    )
                    # v6.5: 压缩前广播，让前端显示"正在压缩上下文"反馈
                    await broadcast(session_id, {
                        "event": "compact.started",
                        "payload": {
                            "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                            "used_tokens": _final_prompt,
                            "context_window": agent_window,
                            "ratio": round(_real_ratio * 100, 1),
                        },
                    })
                    # v30: 优先落库式压缩（持久化 checkpoint），失败回退内存式 auto_compact
                    # plan-560-2311 A2：带上线程身份，子代理不做落库式压缩
                    _cc_real, messages = await _compact_persistent_or_fallback(
                        db, session_id=session_id, provider=provider, agent_window=agent_window,
                        used_tokens=_final_prompt, agent_id=agent_id, agent_name=agent_name,
                        turn_id=turn_id, messages=messages, trigger="pressure",
                        reply_language=reply_language,
                        thread_id=thread_id, agent_kind=agent_kind,
                    )
                    # v6.5: 压缩后广播，前端关闭反馈提示（携带阴影定价供渲染压缩卡片）
                    await broadcast(session_id, {
                        "event": "compact.completed",
                        "payload": {
                            "agent_id": agent_id, "agent_name": agent_name, "turn_id": turn_id,
                            **(_cc_real or {}),
                        },
                    })
                else:
                    logger.debug(
                        "[agent] turn=%s step=%s 上下文占用 %.1f%% (p=%d/w=%d) < %.0f%%，无需压缩",
                        turn_id, step, _real_ratio * 100, _final_prompt,
                        agent_window, _threshold * 100,
                    )

            # plan-547 C1: 进度提醒——连续多步只有工具调用、没有面向用户的文字时注入提醒
            if settings.agent_progress_reminder_interval > 0:
                if str(response.content or "").strip():
                    _steps_since_last_text = 0
                else:
                    _steps_since_last_text += 1
                    if _steps_since_last_text >= settings.agent_progress_reminder_interval:
                        _pending_reminders.append(
                            "[进度提醒] 已连续多步执行工具而未向用户输出任何文字。"
                            "请在继续执行的同时，用一两句话向用户简报当前进展与下一步计划。"
                        )
                        _steps_since_last_text = 0
                        logger.info("[agent] turn=%s step=%s 注入进度提醒（连续 %d 步无文字输出）",
                                    turn_id, step, settings.agent_progress_reminder_interval)

            # plan-19-82 增强（对齐 ZCode "贴近输出点的分散约束"）：语言漂移即时纠正。
            # 周期性重申是"定时"，这里是"按需"——模型已实际输出与锚定语言不符的文字时，
            # 立即在下一步追问前拉回，而不是等到下一个间隔步。最多纠正 2 次：持续漂移
            # 通常意味着用户消息本身语言歧义（auto/混合），反复注入只会挤占上下文。
            if reply_language != "auto" and _lang_drift_reminders < 2:
                _out_text = str(response.content or "").strip()
                if _out_text:
                    from app.orchestration.prompts.language import detect_reply_language as _detect
                    _out_lang = _detect(_out_text)
                    if _out_lang != "auto" and _out_lang != reply_language:
                        _lang_drift_reminders += 1
                        from app.orchestration.prompts import build_language_reminder
                        _pending_reminders.append(build_language_reminder(
                            reply_language, source=reply_language_source,
                        ))
                        logger.info(
                            "[agent] turn=%s step=%s 检测到语言漂移（期望 %s，实际 %s），注入纠正提醒（第 %d 次）",
                            turn_id, step, reply_language, _out_lang, _lang_drift_reminders,
                        )


            # 思考写入消息
            # plan-20261023-thinking-blank：纯空白思考不落库。旧判据 `if response.thinking:` 对
            # " " 这类占位是 truthy，于是库里留下一条只有一个空格的 thinking 消息——前端照常渲染
            # 「思考过程」行，展开却是空白块。落库按 strip 后判断，正文仍保留原文（含内部换行缩进）。
            _thinking_text = (response.thinking or "").strip()
            if _is_substantive_thinking(response.thinking):
                await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                      agent_id=agent_id, agent_name=agent_name,
                                      msg_type=MsgType.THINKING,
                                      content={"text": _thinking_text, "agent_name": agent_name,
                                               "thinking": True})

            # 工具调用
            if response.tool_calls:
                # v1.3: 有工具调用且有文本内容时，先提交文本消息再提交工具调用
                # 否则前端 streamingBuffers 被 tool_call 的 message.created 清空后，
                # 文本内容永久丢失（后端没提交，前端也读不到）
                if response.content:
                    await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                          agent_id=agent_id, agent_name=agent_name,
                                          msg_type=MsgType.TEXT,
                                          content={"text": response.content, "agent_name": agent_name})
                messages.append(ChatMessage(
                    role="assistant", content=response.content,
                    tool_calls=response.tool_calls,
                    # v1.2: thinking 模式网关要求工具调用回合的历史 assistant 回传
                    # reasoning_content（模型本步产生的思考），否则下一轮调用 400
                    reasoning_content=response.thinking or None,
                ))
                # plan-20261019-suggestion-flow：本步的建议工具是否成功（收束判定用，见 for 循环之后）
                _suggestion_ok_this_step = False
                # plan-522-2209：先把本步切成可并行批次——连续「并发安全且确定免审」的调用为一批。
                # 只读工具之间没有共享可变状态，逐个 await 只会白等磁盘 / 网络往返；一旦遇到
                # 写盘、命令、审批、子会话等调用立即形成屏障，副作用调用之间仍严格按序。
                _par_batch_of: dict[int, list[int]] = {}
                # 并行批内每个调用预先分配 call_key：执行与提交两段必须用同一个键，
                # 否则前端会把一次调用拆成两张卡（调用与结果对不上）。
                _par_callkeys: dict[int, str] = {}
                for _b in _parallel_batches(
                    response.tool_calls,
                    approval_mode=approval_mode,
                    permission_mode=permission_mode,
                    workspace=workspace,
                    is_subagent=_caller_is_subagent,
                    is_subsession=_caller_is_subsession,
                ):
                    for _i in _b:
                        _par_batch_of[_i] = _b
                        _par_callkeys[_i] = "tc_" + uuid.uuid4().hex[:12]
                # 已并行执行的结果（按调用序号）与已启动的批次；提交仍在下面按序进行
                _par_results: dict[int, tuple[ToolResult, int]] = {}
                _par_started: set[int] = set()
                # plan-522-2209：工作流节点的工具调用会读写节点相关状态（台账、
                # 写入基线、读取索引），且 agent 是 ORM 对象——并发访问其关联字段
                # 可能触发懒加载异常。这类场景一律不并行，走原串行路径。
                if getattr(agent, "workflow_node_id", None) is not None:
                    _par_batch_of.clear()
                    _par_callkeys.clear()
                for _idx, tc in enumerate(response.tool_calls):
                    # v6.4: 工具执行前检查中断信号
                    if cancel_event and cancel_event.is_set():
                        logger.warning("[agent] turn=%s 工具执行前检测到中断信号，退出循环", turn_id)
                        # plan-522-2209：本步 assistant 消息已带上全部 tool_calls，直接返回会
                        # 留下「有 tool_call 无 tool_result」的孤儿，下一次请求会被网关拒绝——
                        # 还没提交的调用必须补结果；已并行跑完的用真实结果，未启动的补「中止」。
                        await _append_aborted_tool_calls(
                            db, list(response.tool_calls), start_index=_idx,
                            session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            agent_id=agent_id, agent_name=agent_name,
                            messages=messages, done_results=_par_results,
                            callkeys=_par_callkeys,
                        )
                        return AgentOutput(kind="cancelled", error="任务被用户中断")
                    tool_name = tc.get("name", "")
                    args = tc.get("arguments", {}) or {}
                    call_key = _par_callkeys.get(_idx) or ("tc_" + uuid.uuid4().hex[:12])

                    # plan-522-2209：本调用属于并行批时，第一次遇到它就把整批一次性并发跑完
                    # （上限 4 路），结果按调用序号存表；下面每次提交仍按模型返回顺序串行进行。
                    _par_res: tuple[ToolResult, int] | None = _par_results.get(_idx)
                    _par_batch = _par_batch_of.get(_idx)
                    if _par_batch is not None and _idx not in _par_started:
                        _par_started.update(_par_batch)
                        _par_done, _par_injected = await _run_parallel_batch(
                            [(_j, response.tool_calls[_j], _par_callkeys[_j]) for _j in _par_batch],
                            step=step, workspace=workspace, session_id=session_id,
                            turn_id=turn_id, agent_id=agent_id, agent_name=agent_name,
                            agent=agent, permission_mode=permission_mode,
                            approval_mode=approval_mode, sandbox_mode=sandbox_mode,
                            reasoning_effort=reasoning_effort, model_name=_tool_model_name,
                            cancel_event=cancel_event,
                            read_cache=read_cache_for(session_id, lambda: messages),
                            is_subagent=_caller_is_subagent,
                            is_subsession=_caller_is_subsession,
                            subagent_title=_caller_task_title,
                        )
                        _par_results.update(_par_done)
                        # 钩子注入文本按批内顺序并入：钩子仍在工具执行前生效，语义与串行一致
                        for _inj_text in _par_injected:
                            _pending_reminders.append(f"[钩子提示词]\n{_inj_text}")
                        _par_res = _par_results.get(_idx)
                        logger.info(
                            "[agent] turn=%s step=%s 并行执行 %d 个只读工具调用（上限 %d 路）",
                            turn_id, step, len(_par_batch), _PARALLEL_TOOL_LIMIT,
                        )

                    # plan-474-2105：身份二次防御——命中即拒绝并把错误作为工具结果回给模型，
                    # 不进入任何执行分支（子会话问用户 / 子会话再建子会话 / 子代理再派子代理）。
                    _identity_err = _deny_tool_for_identity(
                        tool_name,
                        is_subsession=_caller_is_subsession,
                        is_subagent=_caller_is_subagent,
                    )
                    if _identity_err:
                        logger.warning(
                            "[agent] 身份拦截 tool=%s session=%s subsession=%s subagent=%s",
                            tool_name, session_id, _caller_is_subsession, _caller_is_subagent,
                        )
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_CALL.value,
                            content={"tool": tool_name, "args": args, "call_key": call_key,
                                     "agent_name": agent_name},
                            buffered=True,
                        )
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_RESULT.value,
                            content={"tool": tool_name, "call_key": call_key,
                                     "ok": False, "output": "", "error": _identity_err,
                                     "agent_name": agent_name},
                            buffered=True,
                        )
                        messages.append(ChatMessage(
                            role="tool", content=_identity_err,
                            name=tool_name, tool_call_id=tc.get("id", ""),
                        ))
                        continue

                    # plan-474-2105：邮箱打断事件——每步一个，terminal_exec 命中后据此转后台
                    _detach_event = asyncio.Event()

                    # v2.2 (对齐 zcode 3.14): 重复调用签名检测——连续 ≥2 步同工具同参数
                    # 注入提醒（对齐 buildRepeatedToolCallReminderBody），防死循环空转。
                    try:
                        _sig = tool_name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
                        # 只统计“紧邻”的重复：中间夹了别的调用即重新计数（此前是累计，误伤间隔较远的正常复用）
                        if _sig == _last_sig:
                            _sig_count = _call_sigs.get(_sig, 0) + 1
                        else:
                            _sig_count = 1
                            _call_sigs = {}
                        _call_sigs[_sig] = _sig_count
                        _last_sig = _sig
                        if _sig_count >= 2:
                            _pending_reminders.append(
                                f"[重复调用提醒] 你已连续 {_sig_count} 次调用 {tool_name} "
                                "且参数完全相同，但结果未推动任务进展。"
                                "请停止重复，改用其他方法：检查文件是否存在、换用不同工具或参数、"
                                "或总结当前发现并询问用户。"
                            )
                            logger.warning(
                                "[agent] turn=%s step=%s 重复调用提醒: %s x%d",
                                turn_id, step, tool_name, _sig_count,
                            )
                    except Exception:
                        pass

                    # ── 子代理工具（主代理专用）──
                    if subagent_context and tool_name in (
                        "spawn_subagent", "collect_results", "subagent_inspect", "cancel_subagent",
                        "send_to_subagent", "resume_subagent",
                    ):
                        # v21: 子代理工具消息持久化 —— 此前子代理交互（spawn/collect）从不落库，
                        # 停止任务后重建上下文时找不到任何子代理记录，主代理会重新 spawn 重复执行。
                        # plan-330-1648 M5-a（用户反馈问题1：卡片位置错乱）：TOOL_CALL 必须在
                        # **工具执行前**落库并排空（write-behind 屏障）——保证 message.created
                        # 广播先于子代理启动的 agent.started 事件。否则前端先拿到启动事件、却还没
                        # 对应调用消息，卡片只能走“未落位兑底槽位”渲染（固定挂在折叠容器末尾），
                        # 表现为“子代理卡片显示在 AI 文本上方”。
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_CALL.value,
                            content={"tool": tool_name, "args": args, "call_key": tc.get("id", ""),
                                     "agent_name": agent_name},
                            buffered=True,
                        )
                        try:
                            from app.persistence.write_behind import write_behind as _wb
                            await _wb.get(session_id, turn_id).flush()
                        except Exception:
                            logger.debug("[agent] 子代理调用消息落库屏障失败(非阻塞)", exc_info=True)
                        tool_output = await _run_subagent_tool(
                            db, tool_name=tool_name, args=args,
                            session_id=session_id, turn_id=turn_id,
                            agent=agent, workspace=workspace,
                            subagent_context=subagent_context,
                            # v0.3.1: 传入缓存值——主 db 会话 rollback 后 agent 对象过期，
                            # 直接读 agent.model_id/agent.id 会抛 MissingGreenlet
                            agent_model_id=agent_model_id,
                            parent_agent_id=agent_id,
                        )
                        tool_output = _present_tool_output(tool_output, tool_name=tool_name, session_id=session_id, call_key=tc.get("id", ""))
                        messages.append(ChatMessage(
                            role="tool", content=tool_output,
                            name=tool_name, tool_call_id=tc.get("id", ""),
                        ))
                        # plan-20261022-subagent-dispatch-fail：ok 记录真实成败——并发闸门拒绝、
                        # 校验不过等错误输出此前一律标成 ok=True，前端卡片因此无法区分
                        # 「子代理真在跑」与「派发已被拒绝」，被拒的调用会永远显示「执行中」。
                        _sub_ok = not str(tool_output or "").lstrip().startswith("Error")
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_RESULT.value,
                            content={"tool": tool_name, "call_key": tc.get("id", ""),
                                     "ok": _sub_ok, "output": tool_output,
                                     "error": "" if _sub_ok else str(tool_output or "")[:300],
                                     "agent_name": agent_name},
                            buffered=True,
                        )
                        _tool_executed = True
                        continue

                    # ── 工作流工具（主代理专用，设计 §5.8）：与子代理工具同一特判通道 ──
                    if subagent_context and tool_name in WORKFLOW_TOOL_NAMES:
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_CALL.value,
                            content={"tool": tool_name, "args": args, "call_key": tc.get("id", ""),
                                     "agent_name": agent_name},
                            buffered=True,
                        )
                        tool_output = await run_workflow_tool(
                            tool_name, args,
                            session_id=session_id, turn_id=turn_id, workspace=workspace,
                            readonly_only=permission_mode in ("readonly", "plan"),
                        )
                        tool_output = _present_tool_output(tool_output, tool_name=tool_name, session_id=session_id, call_key=tc.get("id", ""))
                        messages.append(ChatMessage(
                            role="tool", content=tool_output,
                            name=tool_name, tool_call_id=tc.get("id", ""),
                        ))
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_RESULT.value,
                            content={"tool": tool_name, "call_key": tc.get("id", ""),
                                     "ok": True, "output": tool_output,
                                     "error": "", "agent_name": agent_name},
                            buffered=True,
                        )
                        _tool_executed = True
                        continue

                    # ── plan-330-1648 M4: 子代理 → 主代理上报/提问（不走 executor）──
                    if subagent_comm and tool_name == "report_to_leader":
                        tool_output = await _run_report_to_leader(
                            subagent_comm, args,
                            session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            agent_id=agent_id, agent_name=agent_name, db=db,
                        )
                        tool_output = _present_tool_output(tool_output, tool_name=tool_name, session_id=session_id, call_key=tc.get("id", ""))
                        messages.append(ChatMessage(
                            role="tool", content=tool_output,
                            name=tool_name, tool_call_id=tc.get("id", ""),
                        ))
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_CALL.value,
                            content={"tool": tool_name, "args": args, "call_key": tc.get("id", ""),
                                     "agent_name": agent_name},
                            buffered=True,
                        )
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TOOL_RESULT.value,
                            content={"tool": tool_name, "call_key": tc.get("id", ""),
                                     "ok": True, "output": tool_output,
                                     "error": "", "agent_name": agent_name},
                            buffered=True,
                        )
                        _tool_executed = True
                        continue

                    await broadcast(session_id, {
                        "event": "tool.call",
                        "payload": {"turn_id": turn_id, "agent_id": agent_id,
                                    "tool": tool_name, "args_preview": _truncate_args(args, 200)},
                    })
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                        sender_type=SenderType.AGENT.value, sender_id=agent_id,
                        msg_type=MsgType.TOOL_CALL.value,
                        content={"tool": tool_name, "args": args, "call_key": call_key,
                                 "agent_name": agent_name},
                        buffered=True,
                    )

                    # plan-865: 工具执行使用独立 AsyncSession——不再与主循环共享 db，
                    # 消除同 session 并发 IO 触发 greenlet_spawn（SQLAlchemy xd2s）；
                    # 工具内写库 commit 经 LockedAsyncSession 全局写锁串行化
                    async with async_session_factory() as tdb:
                        # v7(B): 注入运行时输出回调——工具每帧上报增量，广播 tool.output 前端实时展示。
                        # 累积上限钳制在 tool_output_chars_terminal，超量后停止转发（后续仍随最终结果返回）。
                        _out_sent = {"len": 0}
                        _out_limit = int(getattr(settings, "tool_output_bytes_terminal", 30_720))

                        async def _emit_tool_output(chunk: str) -> None:
                            if not chunk:
                                return
                            _out_sent["len"] += len(chunk)
                            if _out_sent["len"] > _out_limit:
                                return
                            try:
                                await broadcast(session_id, {
                                    "event": "tool.output",
                                    "payload": {"turn_id": turn_id, "call_key": call_key,
                                                "tool": tool_name, "chunk": chunk},
                                })
                            except Exception:
                                logger.debug("[agent] tool.output 广播失败(非阻塞)", exc_info=True)

                        ctx = ToolContext(
                            workspace_root=workspace, session_id=session_id,
                            task_id=turn_id, agent_id=agent_id, agent_name=agent_name,
                            cancel_event=cancel_event,
                            db=tdb,
                            permission_mode=permission_mode,
                            approval_mode=approval_mode,
                            sandbox_mode=sandbox_mode,
                            reasoning_effort=reasoning_effort,
                            on_tool_output=_emit_tool_output,
                            model_name=_tool_model_name,
                            # plan-474-2105：邮箱打断通道——终端命令据此把运行中的进程转后台
                            detach_event=_detach_event,
                            turn_id=turn_id,
                            # fs_read 未变化短路：缓存按会话保存，消息列表用 getter 取本轮最新值（压缩会替换列表）
                            read_cache=read_cache_for(session_id, lambda: messages),
                            # plan-522-2209：把身份带进工具上下文，审批等待策略据此豁免静默超时
                            is_subagent=_caller_is_subagent,
                            is_subsession=_caller_is_subsession,
                            # plan-549-2281：审批卡显示「来自子代理 #N「标题」」
                            subagent_title=_caller_task_title,
                        )
                        # S12（plan-41-197）：pre_tool_use 钩子——工具调用前触发（可按工具名匹配）；
                        # 提示词型钩子文本注入下一步上下文，命令型钩子在此执行（fail-open）。
                        # plan-522-2209：并行批内的调用已在执行体内各自跑过这个钩子，这里不重复。
                        if _par_res is None:
                            try:
                                for _hr in await hook_service.run_hooks(
                                    db, "pre_tool_use",
                                    {"tool": tool_name, "args": args,
                                     "session_id": session_id, "turn_id": turn_id},
                                    matcher_key=tool_name,
                                ):
                                    _inj = (_hr or {}).get("inject")
                                    if _inj:
                                        _pending_reminders.append(f"[钩子提示词]\n{_inj}")
                            except Exception:
                                logger.debug("[agent] pre_tool_use 钩子执行失败(不阻断)", exc_info=True)
                        _ts0 = time.monotonic()
                        # v9: 写盘工具执行前读取原文件内容（精确回滚依据：只撤销 AI 改动部分）
                        _pre_paths = rollback_service.resolve_write_paths(tool_name, args) if tool_name in _WRITE_TOOLS else []
                        _pre_before = {p: rollback_service._read_file_text(workspace, p) for p in _pre_paths}
                        # v2.2 (plan-88): 写盘前二进制/超限判定——此类文件回滚走 checkpoint 恢复，
                        # 不存文本前后内容（防乱码损坏文件 / DB 膨胀）
                        # plan-20261019-diff: 区分 text/oversize/binary/unknown，超限文本仍可展示 diff
                        _pre_kind = {p: rollback_service.inspect_write_target(
                            workspace, p, settings.rollback_record_max_bytes)[0] for p in _pre_paths}
                        _pre_bin = {p: _write_kind_to_bin(_pre_kind[p]) for p in _pre_paths}
                        # v25: 工具伪装兜底——非白名单写盘工具（terminal_exec 等）从命令解析候选写盘路径
                        # （重定向 / Set-Content / Out-File / python open 等），执行前后对比内容识别真实变更，
                        # 使"改文件"在工具卡展开/输入框贴条中可见，且回滚记录完整。
                        _guess_paths = (rollback_service.resolve_command_write_paths(tool_name, args, workspace)
                                        if tool_name not in _WRITE_TOOLS else [])
                        # plan-20261019-toolcard-v2：删除目标是目录时，在执行前展开成其中的文件
                        # （执行之后目录已经不存在，没法再列出里面有哪些文件被删了）。
                        if _guess_paths:
                            _guess_paths = rollback_service.expand_directory_targets(workspace, _guess_paths)
                        _guess_before = {p: rollback_service._read_file_text(workspace, p) for p in _guess_paths}
                        _guess_kind = {p: rollback_service.inspect_write_target(
                            workspace, p, settings.rollback_record_max_bytes)[0] for p in _guess_paths}
                        _guess_bin = {p: _write_kind_to_bin(_guess_kind[p]) for p in _guess_paths}
                        # plan-20261019-toolcard-v2：写前是否存在 + 字节指纹。写后据此判定新建 / 修改 / 删除，
                        # 并按指纹（而不是有损的文本解码结果）判断命令有没有真的改动文件。
                        _guess_existed = {p: rollback_service.path_exists(workspace, p) for p in _guess_paths}
                        _guess_digest = {p: rollback_service.file_digest(workspace, p) for p in _guess_paths}
                        # plan-20261020-born-in-turn：本轮才创建的文件（截图 / 构建产物等未登记途径新建）——
                        # 之后被命令删掉时对本轮零净变更，不能当成「删除了本来就有的文件」记录（否则撤销会把它们还原出来）
                        if _guess_paths and not _turn_t0_box:
                            _turn_t0_box.append(await rollback_service.turn_started_epoch(db, turn_id))
                        _guess_born = {
                            p: rollback_service.file_born_after(
                                rollback_service._fs_path(workspace, p), _turn_t0_box[0] if _turn_t0_box else None)
                            for p in _guess_paths if _guess_existed.get(p)
                        }
                        _pre_existed = {p: rollback_service.path_exists(workspace, p) for p in _pre_paths}
                        # plan-20261019-toolcard-v2：写前备份必须在工具执行**之前**取。此前在执行之后才复制，
                        # 备份里装的是写后内容，二进制文件被改写 / 删除后无从恢复。
                        # 同一文件每轮只备份一次（登记的是首次写盘前的样子）；没有被登记的备份在下面清理。
                        _pre_ckpt: dict[str, str | None] = {}
                        _ckpt_consumed: set[str] = set()
                        for _cp in [*_pre_paths, *_guess_paths]:
                            if _cp in _pre_ckpt:
                                continue
                            _pre_ckpt[_cp] = None
                            _cp_key = rollback_service.normalize_workspace_path(workspace, _cp) or _cp
                            if _cp_key in _ckpt_registered:
                                continue
                            if _pre_existed.get(_cp) or _guess_existed.get(_cp):
                                _pre_ckpt[_cp] = rollback_service.checkpoint_file(
                                    workspace, _cp, session_id=session_id, turn_id=turn_id)
                        # v10: 对已存在的目标文件额外建立磁盘 checkpoint（.chatcoder/checkpoints 兜底备份），
                        # 与精确回滚的 before/after 记录双保险，防数据库记录异常时无法恢复。
                        # v1.1: 取消穿透——长工具执行期间轮询 cancel_event，命中即取消底层任务
                        # plan-522-2209：并行批已执行完的调用不再重跑，只把结果包成「已完成任务」
                        # 送回同一条等待 / 异常 / 运行态清理路径。
                        if _par_res is not None:
                            _exec_task = asyncio.ensure_future(_already_done(_par_res[0]))
                        else:
                            _exec_task = asyncio.ensure_future(tool_executor.execute(
                                tool_name=tool_name, args=args, call_key=call_key,
                                agent=agent, ctx=ctx,
                                on_approval_request=_make_approval_emitter(session_id, _pending_reminders),
                                llm_call_id=tc.get("id"),
                            ))
                        # plan-474-2105：登记工具运行态，等待期间邮箱消息据此决定
                        # 「能否打断 / 以什么方式让出」（写文件与等用户回答一律不打断）。
                        from app.orchestration.engine import (
                            claim_pending_interrupt as _claim_pending_interrupt,
                            clear_tool_state as _clear_tool_state,
                            interrupt_source as _interrupt_source,
                            set_tool_state as _set_tool_state,
                            wake_event_for as _wake_event_for,
                        )

                        _wake_event = _wake_event_for(turn_id)
                        _interruptible = _interruptible_for(tool_name, args)
                        # plan-20261020-bgtask：先清掉残留信号，再登记运行态——消息本身已在注入队列里，
                        # 不能让一个过期信号把刚启动的工具当场取消。清理与登记之间没有 await，不会漏掉真正的打断。
                        if _wake_event.is_set():
                            _wake_event.clear()
                        _set_tool_state(turn_id, tool=tool_name, interruptible=_interruptible)
                        # 模型思考期间到达的打断请求（子会话提问 / 主会话答复）在这里兑现：
                        # 会等待的工具（前台命令 / wait 轮询）立刻让出，让模型马上看到那条消息；
                        # 瞬间完成的工具（todo_write 等）直接丢弃，消息在下一次 LLM 调用前注入。
                        _deferred_source = _claim_pending_interrupt(turn_id)
                        if _deferred_source and _interruptible in ("abort", "detach"):
                            from app.orchestration.engine import tool_state as _tool_state

                            _st_now = _tool_state(turn_id)
                            if _st_now is not None:
                                _st_now["interrupt_source"] = _deferred_source
                            _wake_event.set()
                        try:
                            if tool_name == "ask_user_question":
                                # plan-238-1188: 结构化提问彻底不设执行超时——
                                # 等待用户回答的时长不可预估（approval.py 已对 question
                                # 取消审批超时，但本层 tool_exec_timeout_sec 仍会杀掉
                                # 等待中的工具，表现为"提问工具还是会超时"）。
                                # 结束方式：用户回答，或取消 turn（_poll_cancel_or_wake 命中
                                # cancel_event 取消底层任务 → CancelledError 分支）。
                                result, _wake_reason = await asyncio.shield(
                                    _poll_cancel_or_wake(_exec_task, cancel_event, _wake_event)
                                )
                            else:
                                result, _wake_reason = await asyncio.wait_for(
                                    asyncio.shield(
                                        _poll_cancel_or_wake(_exec_task, cancel_event, _wake_event)
                                    ),
                                    timeout=settings.tool_exec_timeout_sec,  # v21: 120s → 可配置(默认600s)，长编译/测试不再被误杀
                                )
                            # plan-474-2105：邮箱打断 → 按档位让出控制权
                            if _wake_reason == "mailbox":
                                from app.orchestration.tools.base import ToolResult as _TR
                                if _interruptible == "detach":
                                    # 前台命令：进程不终止，交后台继续跑，等它返回 shell_id
                                    _detach_event.set()
                                    result = await _exec_task
                                elif _interruptible == "abort":
                                    _exec_task.cancel()
                                    result = _TR(
                                        ok=False, output="",
                                        error=f"[已让出等待] {_interrupt_source_label(_interrupt_source(turn_id))}，"
                                              "先处理消息。本次等待已中断，需要时可重新调用该工具。",
                                    )
                                else:  # never：登记时不会置位，兜底按原样等完
                                    result = await _exec_task
                        except asyncio.TimeoutError:
                            _exec_task.cancel()
                            from app.orchestration.tools.base import ToolResult
                            logger.error(
                                "[tool.error] phase=timeout turn=%s step=%s tool=%s call_key=%s 超时(%ss) args=%s",
                                turn_id, step, tool_name, call_key,
                                settings.tool_exec_timeout_sec, summarize_args(args),
                            )
                            result = ToolResult(ok=False, output="", error=f"[工具执行超时({settings.tool_exec_timeout_sec}s)] {tool_name}")
                        except asyncio.CancelledError:
                            from app.orchestration.tools.base import ToolResult
                            result = ToolResult(ok=False, output="", error="[已被用户中断]")
                        except Exception as exc:
                            from app.orchestration.tools.base import ToolResult
                            # v36: 此前 f"[工具执行异常] {exc}" 丢弃 traceback，
                            # 事后只剩一行消息，无法定位抛错代码行。这里记录完整堆栈、
                            # 异常类型与调用参数，并把类型前缀保留给用户可见的 error。
                            log_tool_error(
                                turn_id=turn_id, step=step, tool_name=tool_name,
                                call_key=call_key, exc=exc, args=args, phase="execute",
                            )
                            result = ToolResult(
                                ok=False, output="",
                                error=f"[工具执行异常] {type(exc).__name__}: {exc}",
                            )
                        finally:
                            # plan-474-2105：无论成功、超时、取消还是让出，都要清掉运行态，
                            # 否则下一轮的邮箱消息会被误判成「上一个工具还不可打断」；
                            # 同时复位打断信号——Event 一旦保持 set，后续每个工具都会在启动
                            # 瞬间被判定为「收到子会话消息」而立刻让出，表现为空转。
                            _clear_tool_state(turn_id)
                            try:
                                if _wake_event.is_set():
                                    _wake_event.clear()
                            except Exception:  # noqa: BLE001 — 复位失败不影响工具结果
                                logger.debug("[agent] 打断信号复位失败 turn=%s", turn_id, exc_info=True)
                    _dur = _par_res[1] if _par_res is not None else int((time.monotonic() - _ts0) * 1000)
                    # v31 (plan-89): 工具已执行（无论成败）即视为有产出——后续空响应
                    # 判定据此豁免 stop 空响应的 fatal，避免"任务完成后误报异常"。
                    _tool_executed = True
                    # S12（plan-41-197）：post_tool_use 钩子——工具执行完成后触发；
                    # 提示词型钩子文本注入下一步上下文（fail-open，不阻断）。
                    try:
                        for _hr in await hook_service.run_hooks(
                            db, "post_tool_use",
                            {"tool": tool_name, "ok": bool(result.ok),
                             "error": (result.error or "")[:500],
                             "session_id": session_id, "turn_id": turn_id},
                            matcher_key=tool_name,
                        ):
                            _inj = (_hr or {}).get("inject")
                            if _inj:
                                _pending_reminders.append(f"[钩子提示词]\n{_inj}")
                    except Exception:
                        logger.debug("[agent] post_tool_use 钩子执行失败(不阻断)", exc_info=True)

                    # 写盘工具：登记路径 + 记录 before/after（v9 精确回滚依据）
                    _change_stat = None
                    # v36: 写盘结果留痕——写盘记录只在 result.ok 时生成，
                    # 失败即无记录，前端展开便报「该 turn 无此文件的写盘记录」。
                    if tool_name in _WRITE_TOOLS:
                        logger.info(
                            "[write.result] turn=%s step=%s tool=%s call_key=%s ok=%s "
                            "paths=%s error=%s dur_ms=%s",
                            turn_id, step, tool_name, call_key, result.ok,
                            _pre_paths, (result.error or "")[:300],
                            int((time.monotonic() - _ts0) * 1000),
                        )
                    if tool_name in _WRITE_TOOLS and result.ok:
                        _total_add = 0
                        _total_del = 0
                        for target in _pre_paths:
                            write_paths.append(str(target))
                            # v10: 文件级 checkpoint 兜底（写盘前快照备份）
                            # v26: 登记 {ckpt, path} 到 turn 快照——非 git 仓库回滚也可恢复
                            _target_rel = rollback_service.normalize_workspace_path(workspace, target)
                            # v2.2 (plan-88): 二进制/超限文件只做 checkpoint 备份，不存文本前后内容
                            # plan-1085 三态：pre/post 任一 True→二进制；post False→文本；
                            # post None(读取失败)时磁盘内容+工具兜底均不可得才降级二进制并 warning
                            # plan-20261019-diff: 用 kind 区分超限文本，后者仍生成展示窗口
                            _kind_post, _ = rollback_service.inspect_write_target(
                                workspace, target, settings.rollback_record_max_bytes)
                            _bin_post = _write_kind_to_bin(_kind_post)
                            _is_bin, _after_text = _resolve_write_record(
                                bin_pre=_pre_bin.get(target), bin_post=_bin_post,
                                disk_after=rollback_service._read_file_text(workspace, target),
                                result=result, target=target,
                                turn_id=turn_id, tool_name=tool_name,
                            )
                            # 写前内容：磁盘读取失败时用工具返回的旧内容兜底（新建文件为 None）
                            _before_text = _pre_before.get(target)
                            if _before_text is None:
                                _before_text = _fallback_before_text(result, target)
                            # 超限文本：存一份变更窗口供 UI 展示（回滚仍按 checkpoint）
                            _diff_before = _diff_after = None
                            _textual = (_pre_kind.get(target) in (
                                            rollback_service.FILE_KIND_TEXT,
                                            rollback_service.FILE_KIND_OVERSIZE)
                                        or _kind_post in (
                                            rollback_service.FILE_KIND_TEXT,
                                            rollback_service.FILE_KIND_OVERSIZE))
                            if _is_bin and _textual:
                                _diff_after_text = rollback_service.read_diff_text(workspace, target)
                                if _diff_after_text is None:
                                    _diff_after_text = _fallback_after_text(result, target)
                                # 写后内容不可得时宁可不展示：否则 before 有值、after 为空
                                # 会被 diff 统计误算成「整篇删除」。
                                if _diff_after_text is not None:
                                    _diff_before, _diff_after = rollback_service.build_diff_window(
                                        _before_text, _diff_after_text)
                            # plan-20261019-toolcard-v2：写前存在性来自执行前的探测；备份是执行前取的
                            _existed_pre = bool(_pre_existed.get(target)) or _pre_before.get(target) is not None
                            await _register_pre_checkpoint(
                                db, turn_id=turn_id, target=target, target_rel=_target_rel,
                                existed_before=_existed_pre, ckpt=_pre_ckpt.get(target),
                                registered=_ckpt_registered, consumed=_ckpt_consumed,
                            )
                            _exists_post = rollback_service.path_exists(workspace, target)
                            await rollback_service.record_turn_write(
                                db, session_id=session_id, turn_id=turn_id, tool=tool_name,
                                path=target,
                                before=None if _is_bin else _before_text,
                                after=None if _is_bin else _after_text,
                                binary=_is_bin,
                                # plan-89-387: 关联工具调用——同轮同文件多次编辑各自独立取 diff
                                call_key=call_key,
                                # plan-20261019-diff: 超限文本的展示窗口
                                diff_before=_diff_before,
                                diff_after=_diff_after,
                                # plan-20261019-toolcard-v2：操作类型 + 写后字节指纹（撤销前校验「本轮之后有没有被改过」）
                                op=rollback_service.write_op(_existed_pre, _exists_post),
                                after_hash=rollback_service.file_digest(workspace, target) if _exists_post else None,
                            )
                            # v2.2 (对齐 zcode 3.7): 行级变更统计（+N -M），工具卡摘要展示
                            if not _is_bin:
                                _add, _del = _line_change_stat(_before_text, _after_text)
                            elif _diff_before is not None or _diff_after is not None:
                                # 超限文本：窗口内统计（不再固定 +0 -0）
                                _add, _del = rollback_service.diff_stats(_diff_before, _diff_after)
                            else:
                                _add = _del = 0
                            _total_add += _add
                            _total_del += _del
                        if _total_add or _total_del:
                            _change_stat = {
                                "path": str(_pre_paths[0]),
                                "additions": _total_add,
                                "deletions": _total_del,
                            }
                        # v24: 写盘实时广播——任务执行期间输入框贴条"文件变更"即时刷新，
                        # 前端收到后拉取该 turn 最新变更清单（原仅在 turn 完成后拉取）。
                        if _pre_paths:
                            await broadcast(session_id, {
                                "event": "file.change",
                                # 携带 session_id（串会话修复）：前端据此丢弃迟到/串通道的旧会话事件
                                "payload": {"turn_id": turn_id, "path": str(_pre_paths[0]),
                                            "session_id": session_id},
                            })
                    elif result.ok and _guess_paths:
                        # v25: 工具伪装写盘检测——命令解析出的候选路径，前后内容有变化才登记
                        # （无变化 = 未真正写盘，跳过），并实时广播 file.change 驱动前端刷新。
                        _detected: list[str] = []
                        _total_add = 0
                        _total_del = 0
                        for target in _guess_paths:
                            _after = rollback_service._read_file_text(workspace, target)
                            # plan-20261019-toolcard-v2：先看存在性与字节指纹——删除（前有后无）与新建都必须登记；
                            # 前后都存在时按指纹判断有没有真改动，指纹不可得（过大 / 读取失败）才退回文本比较。
                            _existed_g = bool(_guess_existed.get(target))
                            _exists_post_g = rollback_service.path_exists(workspace, target)
                            _digest_pre_g = _guess_digest.get(target)
                            _digest_post_g = rollback_service.file_digest(workspace, target) if _exists_post_g else None
                            if (_existed_g and not _exists_post_g and _guess_born.get(target)
                                    and not await rollback_service.has_turn_write(db, turn_id, target)):
                                continue  # 本轮由未登记途径新建又被删除：零净变更，不记录（备份由下方统一清理）
                            if _existed_g == _exists_post_g:
                                if _digest_pre_g is not None and _digest_post_g is not None:
                                    if _digest_pre_g == _digest_post_g:
                                        continue
                                elif _guess_before.get(target) == _after:
                                    continue
                            write_paths.append(target)
                            _detected.append(target)
                            # v26: 伪装写盘同样登记 checkpoint 到 turn 快照
                            _target_rel = rollback_service.normalize_workspace_path(workspace, target)
                            # v2.2 (plan-88): 二进制/超限文件只做 checkpoint 备份
                            # plan-1085 三态：与精确写盘分支同口径
                            # plan-20261019-diff: 超限文本仍生成展示窗口
                            _kind_post_g, _ = rollback_service.inspect_write_target(
                                workspace, target, settings.rollback_record_max_bytes)
                            _bin_post_g = _write_kind_to_bin(_kind_post_g)
                            _is_bin, _after = _resolve_write_record(
                                bin_pre=_guess_bin.get(target), bin_post=_bin_post_g,
                                disk_after=_after, result=result, target=target,
                                turn_id=turn_id, tool_name=tool_name,
                            )
                            _before_text_g = _guess_before.get(target)
                            _diff_before_g = _diff_after_g = None
                            _textual_g = (_guess_kind.get(target) in (
                                              rollback_service.FILE_KIND_TEXT,
                                              rollback_service.FILE_KIND_OVERSIZE)
                                          or _kind_post_g in (
                                              rollback_service.FILE_KIND_TEXT,
                                              rollback_service.FILE_KIND_OVERSIZE))
                            if _is_bin and _textual_g:
                                _diff_after_text_g = rollback_service.read_diff_text(workspace, target)
                                if _diff_after_text_g is None:
                                    _diff_after_text_g = _after
                                # 同精确写盘分支：写后内容不可得不生成窗口，避免误算整篇删除
                                if _diff_after_text_g is not None:
                                    _diff_before_g, _diff_after_g = rollback_service.build_diff_window(
                                        _before_text_g, _diff_after_text_g)
                            await _register_pre_checkpoint(
                                db, turn_id=turn_id, target=target, target_rel=_target_rel,
                                existed_before=_existed_g, ckpt=_pre_ckpt.get(target),
                                registered=_ckpt_registered, consumed=_ckpt_consumed,
                            )
                            await rollback_service.record_turn_write(
                                db, session_id=session_id, turn_id=turn_id, tool=tool_name,
                                path=target, before=None if _is_bin else _before_text_g,
                                after=None if _is_bin else _after, binary=_is_bin,
                                # plan-89-387: 伪装写盘同样关联工具调用（terminal_exec 等）
                                call_key=call_key,
                                # plan-20261019-diff: 超限文本的展示窗口
                                diff_before=_diff_before_g,
                                diff_after=_diff_after_g,
                                # plan-20261019-toolcard-v2：命令可能新建 / 修改 / 删除文件，按前后状态记录
                                op=rollback_service.write_op(_existed_g, _exists_post_g),
                                after_hash=_digest_post_g,
                            )
                            if not _is_bin:
                                _add, _del = rollback_service.diff_stats(_before_text_g, _after)
                            elif _diff_before_g is not None or _diff_after_g is not None:
                                _add, _del = rollback_service.diff_stats(_diff_before_g, _diff_after_g)
                            else:
                                _add = _del = 0
                            _total_add += _add
                            _total_del += _del
                        if _total_add or _total_del:
                            _change_stat = {
                                "path": str(_detected[0]),
                                "additions": _total_add,
                                "deletions": _total_del,
                            }
                        if _detected:
                            await broadcast(session_id, {
                                "event": "file.change",
                                # 携带 session_id（串会话修复）：与精确写盘分支同口径
                                "payload": {"turn_id": turn_id, "path": str(_detected[0]),
                                            "session_id": session_id},
                            })

                    # plan-20261019-toolcard-v2：命令并没有真的改动这些文件（或工具失败）时，
                    # 执行前取的备份没有被登记，清掉——避免备份目录里堆满孤儿文件。
                    for _cp, _ck in _pre_ckpt.items():
                        if _ck and (rollback_service.normalize_workspace_path(workspace, _cp) or _cp) not in _ckpt_consumed:
                            rollback_service.discard_checkpoint(_ck)

                    # plan-20261015-browser-agent：截图类结果落盘，消息流在该调用的时间线位置展示预览
                    from app.orchestration.tool_images import persist_tool_image
                    _tool_image = persist_tool_image(tool_name, result)
                    await broadcast(session_id, {
                        "event": "tool.result",
                        # v7(H): 补 call_key——前端据此精确关联写操作/命令行，运行中即可展示 +N-M/diff
                        "payload": {"turn_id": turn_id, "tool": tool_name, "call_key": call_key,
                                    "ok": result.ok, "duration_ms": _dur,
                                    "output_preview": (result.output or result.error)[:300],
                                    **({"change_stat": _change_stat} if _change_stat else {}),
                                    **({"image": _tool_image} if _tool_image else {})},
                    })

                    # plan-156-620：落库与上下文使用同一份呈现文本（超长时完整输出落盘并给出续读方式）
                    _presented_output = _present_tool_output(
                        result.output, tool_name=tool_name, session_id=session_id, call_key=call_key,
                    )
                    # plan-20261015-browser-agent：原 browser.mirror 镜像广播已移除——浏览器工具现在直接
                    # 驱动右侧面板里的真实网页（桌面端宿主自带可视化），不再需要“镜像一个 URL”。
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                        sender_type=SenderType.AGENT.value, sender_id=agent_id,
                        msg_type=MsgType.TOOL_RESULT.value,
                        content={"tool": tool_name, "call_key": call_key,
                                 "ok": result.ok, "output": _presented_output,
                                 "error": result.error, "error_code": result.error_code, "duration_ms": _dur,
                                 "agent_name": agent_name,
                                 **({"change_stat": _change_stat} if _change_stat else {}),
                                 **({"image": _tool_image} if _tool_image else {})},
                        buffered=True,
                    )
                    # 失败时把错误类别与恢复指引附在文本末尾，模型据此选择修正动作（落库的 error 字段保持原样）
                    _model_content = _presented_output if result.output else (result.error or "")
                    if not result.ok:
                        result.classify_if_missing()
                        _model_content = f"{_model_content}\n{result.failure_hint_line()}".strip()
                    # plan-548-2277：写盘后跑最小检查，把语法/格式错误立刻回灌给模型。
                    # 只覆盖 .py/.json/.cjs/.js（单文件级、秒级、零副作用）；.ts/.tsx 的类型
                    # 检查留给交付前的项目级 typecheck，避免每次编辑都等一次编译。
                    if (result.ok and tool_name in ("fs_write", "editor_apply_diff", "multi_file_edit")
                            and getattr(settings, "post_edit_check_enabled", True)):
                        try:
                            from app.services import post_edit_check

                            _check_note = await post_edit_check.check_after_tool(
                                tool_name, result.data, workspace,
                                fail_counts=post_edit_check.fail_counts_for(session_id),
                            )
                        except Exception:  # noqa: BLE001 — 检查失败不影响主流程
                            _check_note = ""
                            logger.debug("[agent] 写后检查异常 tool=%s", tool_name, exc_info=True)
                        if _check_note:
                            _model_content = f"{_model_content}\n\n{_check_note}".strip()
                    messages.append(ChatMessage(
                        role="tool", content=_model_content,
                        name=tool_name, tool_call_id=tc.get("id", ""),
                    ))
                    # fs_read 成功读取后登记：之后同文件同范围未变化时，可凭此结果回存根（见 tools/read_cache.py）
                    if tool_name == "fs_read" and result.ok:
                        _rc = read_cache_for(session_id, lambda: messages)
                        _rk = result.data.get("read_key")
                        _fp = result.data.get("fingerprint")
                        if _rk and _fp:
                            _rc.record(tuple(_rk), tuple(_fp), tc.get("id", ""))
                        # plan-548-2277：「先读后改」闸门要的是实际行区间——shown_lines 形如
                        # "120-419"，即本次真正返回给模型的行范围。未变化存根不带该字段，
                        # 但它指向的更早那次读取已经登记过，无需重复。
                        _shown = str(result.data.get("shown_lines") or "")
                        if _rk and _shown:
                            try:
                                _a, _, _b = _shown.partition("-")
                                _rc.record_range(_rk[0], int(_a), int(_b))
                            except (TypeError, ValueError):
                                pass
                    # L3.1（plan-340-1705）：登记本步的电脑操控调用，供下一步的观测埋点使用
                    if tool_name.startswith("desktop_"):
                        _obs_tools.append((tool_name, bool(result.ok)))
                    try:
                        _route_guard.observe(tool_name, args, bool(result.ok), result.data)
                    except Exception:  # noqa: BLE001 — 守卫只做登记，失败不影响主流程
                        logger.debug("[agent] 路线守卫登记失败 tool=%s", tool_name, exc_info=True)
                    # plan-537-2251：沉淀提醒提前到「还在执行」的阶段。
                    # 收尾分支里的 reminder() 发出时，本步的最终汇报正文已经落库（见下方「最终文本」前
                    # 的注入顺序），沉淀因此必然晚于汇报。这里在每步工具结果登记后补一次中途提醒，
                    # 让模型在任务没结束时就顺手把路线存下来；收尾那次仍保留作为兜底。
                    try:
                        _mid_note = _route_guard.mid_reminder()
                        if _mid_note:
                            _pending_reminders.append(_mid_note)
                    except Exception:  # noqa: BLE001 — 同为辅助提醒，失败不影响主流程
                        logger.debug("[agent] 路线中途提醒失败 tool=%s", tool_name, exc_info=True)
                    # 步级提交：结束主 db 零星写（checkpoint/任务态）形成短事务边界，
                    # 避免消息缓冲化后主 db 长事务悬挂（违背短事务原则）。
                    try:
                        await db.commit()
                    except Exception:
                        await db.rollback()
                        logger.debug("[agent] 步级提交失败 turn=%s tool=%s", turn_id, tool_name, exc_info=True)
                    # 记录本步建议卡是否生成成功（真实名 / ta3 伪装名都认），供下方「汇报 + 建议卡」收束判定
                    if tool_name in ("suggest_next_task", "SuggestNextTask"):
                        _suggestion_ok_this_step = bool(result.ok)
                    # v2.2: todo_write 成功 → 重置 todo 提醒计数
                    if tool_name == "todo_write" and result.ok:
                        _todo_active = True
                        _todo_updated_at_step = step
                        # plan-547 C3: 保存清单全集，供 plan 模式每步注入状态标记
                        _raw_todos = args.get("todos") if isinstance(args, dict) else None
                        _last_todos = [t for t in _raw_todos if isinstance(t, dict)] if isinstance(_raw_todos, list) else []
                    # v15: 图片类工具结果 → 多模态模型追加 image_url 消息，让模型真正看到图片。
                    # （此前 base64 只放在 ToolResult.data，从未进入对话，模型只能看到
                    #   "Base64 length: N" 文本，被迫转向 OCR/命令行猜图）
                    # plan-365-1757：判据由「工具名白名单」改为「结果里是否带图片」——
                    # 电脑操控的「动作后附带截图」（then_shot）也会带 base64，白名单写法会让
                    # 点击后返回的新画面根本看不到，等于白截一张。
                    if multimodal and result.ok:
                        _b64 = (result.data or {}).get("base64")
                        if _b64:
                            import mimetypes as _mt
                            _fname = str((result.data or {}).get("filename") or (result.data or {}).get("path") or "")
                            _mime = _mt.guess_type(_fname)[0] or "image/png"
                            _img_msg = ChatMessage(
                                role="user",
                                content=f"[系统] 上面工具 {tool_name} 读取的图片内容如下，请直接查看：",
                                content_blocks=[{"type": "image_url", "image_url": {"url": f"data:{_mime};base64,{_b64}"}}],
                            )
                            messages.append(_img_msg)
                            if tool_name.startswith(_SCREEN_TOOL_PREFIXES):
                                _tool_image_msgs.append(_img_msg)
                                _prune_tool_images(_tool_image_msgs)
                # plan-20261019-suggestion-flow：汇报与建议卡连续展示——本步正文（汇报）已随本步落库，
                # 且本步只调用了建议工具并成功时直接收束 turn：不再让模型多走一步
                # （否则会多出「已调用工具 + 思考 + 一段重复收尾语」，还把前面的汇报当成过程折叠）。
                # 后台子代理仍在跑时不收束（需继续等待它们）；正文为空时也不收束，留给下一步补汇报。
                # 只对主代理生效，且不绕过收尾前的两道纠偏：规划模式必须写 ai/ 计划文档、
                # GUI 操作路线沉淀提醒——这两道只在「无工具调用的收工步」才会触发，提前 break 会整个跳过。
                if (_suggestion_ok_this_step and agent_kind == "main" and permission_mode != "plan"
                        and not _route_guard.has_pending_reminder()
                        and (response.content or "").strip()
                        and all(tc.get("name", "") in ("suggest_next_task", "SuggestNextTask")
                                for tc in response.tool_calls)):
                    _mgr_now = (subagent_context or {}).get("manager")
                    try:
                        _bg_pending = int(_mgr_now.pending_count()) if _mgr_now is not None else 0
                    except Exception:
                        _bg_pending = 0
                    if _bg_pending == 0:
                        logger.info("[agent] turn=%s 建议卡已生成，收束本轮（汇报已落库）", turn_id)
                        final_text = response.content or ""
                        break
                continue

            # ── v43: 主代理收工等待后台子代理（会话保持运行，直到子代理结束）──
            # 语义：后台子代理运行时主代理可继续并行工作；主代理做完自己的事（本步无工具
            # 调用、准备结束 turn）而子代理仍在跑时，turn **不结束**——保持会话 running
            # 并阻塞等待（前端状态行显示「等待子代理结束…」，左侧转圈、输入框停止按钮可中断），
            # 子代理结束后把完成报告注入同一 turn 继续（不伪造用户消息、不新开唤醒轮）。
            # pending=0 时同样取一次未送达报告：子代理在模型生成期间完成的通知不丢。
            _sub_mgr = (subagent_context or {}).get("manager")
            if _sub_mgr is not None:
                _wait_notes = await _await_background_subagents(
                    _sub_mgr, session_id=session_id, turn_id=turn_id,
                    thread_id=thread_id, cancel_event=cancel_event,
                )
                if _wait_notes is None:
                    return AgentOutput(kind="cancelled", error="任务被用户中断")
                if _wait_notes:
                    # plan-64-289: 本步正文先固化落库再续跑——收工步（无工具调用）的正文
                    # 原本只在 turn 真正结束时落库（见下方「最终文本」），而此处 turn 要延续，
                    # 这段「等待汇报」便永不落库：它只留在前端流式缓冲里，随后被下一轮的
                    # token.done 覆盖 → 表现为「等待汇报消失、新内容刷到上方思考块」。
                    # 直写（非 buffered）保证 message.created 先于下一轮流式到达。
                    if response.content:
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TEXT.value,
                            content={"text": response.content, "agent_name": agent_name},
                        )
                    logger.info("[agent] turn=%s 注入 %d 条子代理完成报告，继续本 turn",
                                turn_id, len(_wait_notes))
                    messages.append(ChatMessage(
                        role="user",
                        content=(
                            "[系统] 后台子代理有新进展，以下是它们的完成报告或提示。"
                            "请整合结论并继续推进任务（若有子代理在等你答复，用 send_to_subagent 回复）；"
                            "不要复述本条提示。\n\n"
                            + "\n\n".join(_wait_notes)
                        ),
                    ))
                    continue

            # plan-20261018-operator-boost：GUI 操作完成却未沉淀路线 → 注入一次提醒再走一步
            _route_note = _route_guard.reminder()
            if _route_note:
                if response.content:
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                        sender_type=SenderType.AGENT.value, sender_id=agent_id,
                        msg_type=MsgType.TEXT.value,
                        content={"text": response.content, "agent_name": agent_name},
                    )
                    messages.append(ChatMessage(role="assistant", content=response.content))
                logger.info("[agent] turn=%s 注入操作路线沉淀提醒", turn_id)
                messages.append(ChatMessage(role="user", content=_route_note))
                continue

            # 最终文本
            final_text = response.content or ""
            # plan-330-1648 M1: 计划文档纠偏**只对主代理生效**——子代理不参与规划流程，
            # 运行模式已按类型锁定（不再继承会话 plan），此处再显式限定 agent_kind，
            # 彻底消除"子代理被系统反复催写 ai/chatcoder-plan-*.md"的现象。
            if (agent_kind == "main" and _mode_kind(permission_mode) == "plan"
                    and not any(p.replace("\\", "/").startswith("ai/chatcoder-plan") or "/ai/chatcoder-plan" in p.replace("\\", "/") for p in write_paths)):
                if _plan_correction_tries < _PLAN_CORRECTION_MAX:
                    _plan_correction_tries += 1
                    _draft = (response.thinking or "")[:_PLAN_CORRECTION_DRAFT_CHARS]
                    _reminder = (
                        "[系统提醒] 当前处于【规划模式】，你尚未创建/写入方案文档！\n"
                        "注意：你刚才的思考/回复内容【不会】被自动保存为方案文档，"
                        "只有通过 fs_write 工具把内容写入 `ai/chatcoder-plan-"
                        f"{session_id}-{turn_id}.md` 文件才算完成。\n"
                        "请立即调用 fs_write 工具，将完整的方案规划内容写入该文件，"
                        "不要仅在对话中回复文字或思考。"
                    )
                    if _draft.strip():
                        _reminder += (
                            "\n\n【你的思考草稿（可直接整理为文档内容，"
                            "但必须通过 fs_write 写入才算完成）】\n"
                            + _draft
                        )
                    logger.info(
                        "[agent] turn=%s 规划模式纠偏 %d/%d 次（模型未写 ai/ 计划文档，"
                        "注入思考草稿 %d 字符）",
                        turn_id, _plan_correction_tries, _PLAN_CORRECTION_MAX, len(_draft),
                    )
                    messages.append(ChatMessage(
                        role="assistant",
                        content=final_text,
                    ))
                    messages.append(ChatMessage(
                        role="user",
                        content=_reminder,
                    ))
                    # plan-64-289: 与子代理续跑同理——纠偏续跑时本步正文也需先固化落库，
                    # 否则它只留在前端流式缓冲里、被下一轮的 token.done 覆盖（内容丢失 + 错位）。
                    # 直写（非 buffered）保证 message.created 先于下一轮流式到达。
                    if final_text:
                        await message_service.create_message(
                            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                            sender_type=SenderType.AGENT.value, sender_id=agent_id,
                            msg_type=MsgType.TEXT.value,
                            content={"text": final_text, "agent_name": agent_name},
                        )
                    continue

            if final_text:
                await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                      agent_id=agent_id, agent_name=agent_name,
                                      msg_type=MsgType.TEXT,
                                      content={"text": final_text, "agent_name": agent_name})
            break

        else:
            final_text = response.content if "response" in locals() else ""
            if not final_text and not _tool_executed:
                # v31 (plan-89): 零产出 + 耗尽步数 = 真实失败（保持 error）
                _limit_desc = "不限制" if is_unlimited_steps else str(max_steps)
                await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                      agent_id=agent_id, agent_name=agent_name,
                                      msg_type=MsgType.ERROR,
                                      content={"text": f"达到步数上限({_limit_desc})，任务未完成", "agent_name": agent_name})
                return AgentOutput(kind="error", error=f"达到步数上限({_limit_desc})")
            # v31 (plan-89): 对齐 zcode/AI SDK stepCountIs 语义——步数上限是停止
            # 条件而非失败条件：本 turn 已有产出或已有内容时正常结束，落非错误提示。
            if final_text:
                await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                      agent_id=agent_id, agent_name=agent_name,
                                      msg_type=MsgType.TEXT,
                                      content={"text": final_text, "agent_name": agent_name})
            _limit_desc = "不限制" if is_unlimited_steps else str(max_steps)
            await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                  agent_id=agent_id, agent_name=agent_name,
                                  msg_type=MsgType.TEXT,
                                  content={"text": f"已达步数上限({_limit_desc})，以上为已完成的执行进度",
                                           "agent_name": agent_name})

        # 产物抽取（v7: 关联到主 turn 对应的任务；task_id 为空时兜底用 turn_id）
        artifact_ids: list[int] = []
        if final_text and len(final_text.strip()) > 10:
            try:
                from app.orchestration.artifacts import extract_and_persist_artifacts
                artifact_ids = await extract_and_persist_artifacts(
                    db, task_id=task_id or turn_id, text=final_text, write_paths=write_paths,
                )
            except Exception:
                logger.warning("[agent] 产物抽取失败(非阻塞)", exc_info=True)

        # 主代理最终文字与产物也写入主线程（子代理已写 thread）
        if agent_kind == "main" and artifact_ids:
            await message_service.create_message(
                db, session_id=session_id, turn_id=turn_id, thread_id=None,
                sender_type=SenderType.AGENT.value, sender_id=agent_id,
                msg_type=MsgType.ARTIFACT.value,
                content={
                    "artifact_ids": artifact_ids,
                    "agent_name": agent_name,
                    # v7: 携带实际写盘文件列表，前端产物卡片据此展示文件清单
                    "files": list(dict.fromkeys(write_paths)),
                },
            )

        await broadcast(session_id, {
            "event": "agent.completed",
            # plan-20261019-subagent-ghost: 必须带 kind——主代理每轮结束也走这里，
            # 不带类型时前端会把主代理（agent_id=1）记成「子代理 #1」，点开是一个空面板。
            "payload": {"agent_id": agent_id, "kind": agent_kind,
                        "session_id": session_id,
                        "summary": final_text, "artifact_ids": artifact_ids},
        })
        return AgentOutput(kind="message", text=final_text, artifact_ids=artifact_ids,
                           tokens=total_tokens)

    except Exception as e:
        logger.exception("agent loop 异常 turn=%s agent=%s", turn_id, agent_name)
        # 主 db session 可能已因数据库锁冲突处于 rollback-only（见 usage 落库修复），
        # 错误提示改由独立会话补发，确保「AI 异常终止」对用户可见而非静默中断。
        _err_text = f"执行异常: {str(e)[:200]}"
        try:
            await _emit_agent_msg(db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                                  agent_id=agent_id, agent_name=agent_name,
                                  msg_type=MsgType.ERROR,
                                  content={"text": _err_text, "agent_name": agent_name})
        except Exception:
            try:
                async with async_session_factory() as _err_db:
                    await _emit_agent_msg(_err_db, session_id=session_id, turn_id=turn_id,
                                          thread_id=thread_id, agent_id=agent_id,
                                          agent_name=agent_name, msg_type=MsgType.ERROR,
                                          content={"text": _err_text, "agent_name": agent_name})
                    await _err_db.commit()
            except Exception:
                logger.error("[agent] turn=%s 错误提示落库失败: %s", turn_id, str(e)[:200])
        return AgentOutput(kind="error", error=str(e))


# ── 辅助 ──

async def _poll_cancel(task: asyncio.Task, cancel_event: asyncio.Event | None):
    """等待工具任务完成；期间监听取消信号，命中则取消底层任务（停止按钮对长工具即时生效）。

    plan-20261019-desktop-precision：改为同时等待「任务完成 / 取消信号」两个事件。此前每 0.2 s 轮询一次，
    任何工具（包括 1 ms 级的读窗口、读文件）都要等到下一个轮询点才返回，每次调用白白多出最多 0.2 s。
    """
    if cancel_event is None:
        return await task
    if cancel_event.is_set():
        task.cancel()
        raise asyncio.CancelledError("cancelled by user")
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not waiter.done():
            waiter.cancel()
    if task not in done:
        task.cancel()
        raise asyncio.CancelledError("cancelled by user")
    return await task


async def _poll_cancel_or_wake(
    task: asyncio.Task,
    cancel_event: asyncio.Event | None,
    wake_event: asyncio.Event | None = None,
):
    """等待工具任务；同时监听「用户取消」与「邮箱打断」两个信号。

    返回值 `(结果, wake_reason)`：
      · 正常完成 → (result, None)
      · 用户取消 → 取消底层任务并抛 CancelledError（与原 `_poll_cancel` 一致）
      · 邮箱打断 → (None, "mailbox")，底层任务保持运行，由调用方按档位决定
        转后台（detach）还是取消放弃（abort）——见 `_interruptible_for`。
    """
    if cancel_event is None and wake_event is None:
        return await task, None
    if cancel_event is not None and cancel_event.is_set():
        task.cancel()
        raise asyncio.CancelledError("cancelled by user")
    waiters: dict[str, asyncio.Future] = {}
    if cancel_event is not None:
        waiters["cancel"] = asyncio.ensure_future(cancel_event.wait())
    if wake_event is not None:
        waiters["wake"] = asyncio.ensure_future(wake_event.wait())
    try:
        done, _ = await asyncio.wait({task, *waiters.values()}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters.values():
            if not w.done():
                w.cancel()
    if task in done:
        return await task, None
    if "cancel" in waiters and waiters["cancel"] in done:
        task.cancel()
        raise asyncio.CancelledError("cancelled by user")
    return None, "mailbox"


# ── plan-522-2209：工具并行执行 ──────────────────────────────
# 并发上限硬编码 4：只读工具的实际瓶颈是磁盘 / 网络 / SQLite 读，4 路足以把
# 「一条消息里 3 个 fs_read」的串行等待压掉，再多只会让单次工具的输出洪峰挤占上下文，
# 并不会更快。不引入新配置项，避免多一个需要用户理解的旋钮。
_PARALLEL_TOOL_LIMIT = 4

# 取消时给「未启动的调用」补的合成结果。历史里不能出现「有 tool_call 无 tool_result」
# 的孤儿，否则下一次请求会被网关以「消息序列不合法」拒绝。
ABORTED_BEFORE_DISPATCH = "Error: tool call aborted before dispatch"

# 无论是否标记并发安全都不得并行的工具：特判通道（子代理 / 工作流 / 上报）
# 与互斥交互（结构化提问）。这些调用在原循环里走独立分支且带 user-visible 副作用，
# 并行只会打乱它们的时序。
_NEVER_PARALLEL_TOOLS: frozenset[str] = frozenset({
    "ask_user_question",
    "report_to_leader",
    "spawn_subagent", "collect_results", "subagent_inspect", "cancel_subagent",
    "send_to_subagent", "resume_subagent",
}) | frozenset(WORKFLOW_TOOL_NAMES)


def _parallel_batches(
    tool_calls: list[dict],
    *,
    approval_mode: str,
    permission_mode: str,
    workspace: str,
    is_subagent: bool,
    is_subsession: bool,
) -> list[list[int]]:
    """把本步的工具调用切成可并行的批次（返回下标组）。

    规则：连续的「并发安全且确定免审」调用成一批；遇到不满足的调用即断开（形成屏障），
    该调用自己不参与并行。只有长度 ≥ 2 的组才值得并行——单个调用并行了也没有收益，
    反而多套一层任务。

    plan-522-2209：只看静态标记与策略矩阵，不查 exec_policy 规则（要 db 会话）——
    规则只会让判定更宽松，这里保守处理，最多少并行几个调用。
    """
    # 判定上下文：只需裁决用到的字段，不构造真实 ToolContext（它要 db 会话，此处不该开）
    stub = SimpleNamespace(
        approval_mode=approval_mode,
        permission_mode=permission_mode,
        workspace_root=workspace,
        is_subagent=is_subagent,
        is_subsession=is_subsession,
    )
    batches: list[list[int]] = []
    current: list[int] = []
    for idx, tc in enumerate(tool_calls):
        name = str(tc.get("name") or "")
        args = tc.get("arguments", {}) or {}
        safe = name not in _NEVER_PARALLEL_TOOLS and not _deny_tool_for_identity(
            name, is_subsession=is_subsession, is_subagent=is_subagent,
        )
        tool = tool_registry.get(name) if safe else None
        safe = bool(safe and tool is not None and getattr(tool, "concurrency_safe", False))
        if safe and isinstance(args, dict):
            try:
                # 审批是互斥交互（一张卡对应一次决定）：询问档下的只读外部读取
                # 也会弹卡，这类调用必须独占，不能与批内其它调用同时等用户。
                safe = can_run_in_parallel(name, args, stub, risk_level=tool.risk_level)
            except Exception:  # noqa: BLE001 — 判定异常时按独占处理（保守）
                logger.debug("[agent] 并行判定失败 tool=%s", name, exc_info=True)
                safe = False
        if safe:
            current.append(idx)
            continue
        if len(current) >= 2:
            batches.append(current)
        current = []
    if len(current) >= 2:
        batches.append(current)
    return batches


async def _run_parallel_batch(
    batch: list[tuple[int, dict, str]],
    *,
    step: int,
    workspace: str,
    session_id: int,
    turn_id: int,
    agent_id: int,
    agent_name: str,
    agent,
    permission_mode: str,
    approval_mode: str,
    sandbox_mode: str,
    reasoning_effort: str | None,
    model_name: str,
    cancel_event: asyncio.Event | None,
    read_cache,
    is_subagent: bool,
    is_subsession: bool,
    subagent_title: str = "",
) -> tuple[dict[int, tuple["ToolResult", int]], list[str]]:
    """并行执行一批调用，返回 ({下标: (结果, 耗时毫秒)}, 批内按序排列的钩子注入文本)。

    plan-522-2209：这里只做**执行**——落库、messages 追加、post 钩子、回滚记账、呈现
    全部留在原循环的提交段按模型返回顺序串行完成（历史必须可复现）。因此每路
    执行体只读共享状态：文件系统、索引、网络；写库经由独立的 async_session_factory
    会话，不碰主 db。
    """
    from app.orchestration.tools.base import ToolResult as _TR

    results: dict[int, tuple[_TR, int]] = {}
    # pre_tool_use 的注入文本按批内下标回填（并行收集、事后保序），与串行路径同一语义
    injected: list[tuple[int, str]] = []
    sem = asyncio.Semaphore(min(_PARALLEL_TOOL_LIMIT, len(batch)))

    async def _one(idx: int, tc: dict, call_key: str) -> tuple[int, _TR, int]:
        name = str(tc.get("name") or "")
        args = tc.get("arguments", {}) or {}
        aborted = _TR(ok=False, output="", error=ABORTED_BEFORE_DISPATCH, error_code="INTERNAL")
        if cancel_event is not None and cancel_event.is_set():
            return idx, aborted, 0
        async with sem:
            # 排队期间可能被取消：未启动的调用直接补合成结果，不再占资源
            if cancel_event is not None and cancel_event.is_set():
                return idx, aborted, 0
            t0 = time.monotonic()
            task: asyncio.Task | None = None
            try:
                async with async_session_factory() as tdb:
                    # pre_tool_use 钩子仍在执行前跑，每路用自己的会话（不碰主 db）。
                    # 注入文本只收集不追加，由提交段按模型返回顺序并入 _pending_reminders。
                    try:
                        for _hr in await hook_service.run_hooks(
                            tdb, "pre_tool_use",
                            {"tool": name, "args": args,
                             "session_id": session_id, "turn_id": turn_id},
                            matcher_key=name,
                        ):
                            _inj = (_hr or {}).get("inject")
                            if _inj:
                                injected.append((idx, _inj))
                    except Exception:
                        logger.debug("[agent] pre_tool_use 钩子执行失败(不阻断)", exc_info=True)
                    ctx = ToolContext(
                        workspace_root=workspace, session_id=session_id,
                        task_id=turn_id, agent_id=agent_id, agent_name=agent_name,
                        cancel_event=cancel_event,
                        db=tdb,
                        permission_mode=permission_mode,
                        approval_mode=approval_mode,
                        sandbox_mode=sandbox_mode,
                        reasoning_effort=reasoning_effort,
                        # 只读工具不产生增量输出（on_tool_output 只有 terminal_exec 消费）
                        on_tool_output=None,
                        model_name=model_name,
                        turn_id=turn_id,
                        read_cache=read_cache,
                        is_subagent=is_subagent,
                        is_subsession=is_subsession,
                        # plan-549-2281 修复：并行批路径同样要带子代理任务标题——
                        # 此前只给串行路径传了 subagent_title，只读工具走并行批时审批卡
                        # 就退回「子代理 #N」而没有标题（两边渲染不一致）。
                        subagent_title=subagent_title,
                    )
                    task = asyncio.ensure_future(tool_executor.execute(
                        tool_name=name, args=args, call_key=call_key,
                        agent=agent, ctx=ctx,
                        on_approval_request=None,
                        llm_call_id=tc.get("id"),
                    ))
                    result, _ = await asyncio.wait_for(
                        asyncio.shield(_poll_cancel_or_wake(task, cancel_event, None)),
                        timeout=settings.tool_exec_timeout_sec,
                    )
            except asyncio.TimeoutError:
                if task is not None:
                    task.cancel()
                logger.error(
                    "[tool.error] phase=timeout turn=%s step=%s tool=%s call_key=%s 超时(%ss) args=%s",
                    turn_id, step, name, call_key,
                    settings.tool_exec_timeout_sec, summarize_args(args),
                )
                result = _TR(
                    ok=False, output="",
                    error=f"[工具执行超时({settings.tool_exec_timeout_sec}s)] {name}",
                    error_code="TIMEOUT",
                )
            except asyncio.CancelledError:
                if task is not None:
                    task.cancel()
                result = _TR(ok=False, output="", error="[已被用户中断]", error_code="INTERNAL")
            except Exception as exc:  # noqa: BLE001 — 单个调用异常不能炸掉整批
                if task is not None:
                    task.cancel()
                log_tool_error(
                    turn_id=turn_id, step=step, tool_name=name,
                    call_key=call_key, exc=exc, args=args, phase="execute.parallel",
                )
                result = _TR(
                    ok=False, output="",
                    error=f"[工具执行异常] {type(exc).__name__}: {exc}",
                    error_code="INTERNAL",
                )
            return idx, result, int((time.monotonic() - t0) * 1000)

    gathered = await asyncio.gather(*(_one(i, tc, ck) for i, tc, ck in batch), return_exceptions=True)
    for item in gathered:
        if isinstance(item, BaseException):
            continue
        idx, result, dur = item
        results[idx] = (result, dur)
    # 兜底：任何没拿到结果的调用补合成结果——宁可报「已中止」，也不能留下孤儿调用
    for i, _tc, _ck in batch:
        results.setdefault(i, (
            _TR(ok=False, output="", error=ABORTED_BEFORE_DISPATCH, error_code="INTERNAL"), 0,
        ))
    return results, [text for _i, text in sorted(injected, key=lambda x: x[0])]


async def _already_done(value):
    """plan-522-2209：把已算好的结果包成「立刻完成的任务」。

    并行批已执行完的调用走这里回到原来的等待 / 异常 / 运行态清理路径——不复制一套
    提交流程，提交只有一条代码路径可言，避免并行与串行两处行为漂移。
    """
    return value


async def _append_aborted_tool_calls(
    db, tool_calls: list[dict], *, start_index: int,
    session_id: int, turn_id: int, thread_id: int | None,
    agent_id: int, agent_name: str,
    messages: list | None = None,
    done_results: dict[int, "tuple[ToolResult, int]"] | None = None,
    callkeys: dict[int, str] | None = None,
) -> None:
    """plan-522-2209：中断时给「还没提交的调用」补结果，保证历史里没有孤儿。

    为什么必须补：中断时本步的 assistant 消息已经带着全部 tool_calls 追加进上下文，
    若只 return 而不补 tool 消息，历史里就会留下「有 tool_call 无 tool_result」的孤儿，
    下一次请求会被网关以消息序列不合法拒绝，用户看到的是任务莫名中断且无法继续。

    已经并行执行完的调用不能一律报「已中止」——它的真实结果仍有效，只是没轮到提交；
    这类调用按真实结果补记，只有确实未启动的才补 ABORTED_BEFORE_DISPATCH。
    """
    done_results = done_results or {}
    callkeys = callkeys or {}
    for _idx in range(start_index, len(tool_calls)):
        tc = tool_calls[_idx]
        name = str(tc.get("name") or "")
        _done = done_results.get(_idx)
        if _done is not None:
            result, dur = _done
            ok, output, error = bool(result.ok), str(result.output or ""), str(result.error or "")
            # 与正常提交路径同一份呈现文本（plan-156-620）：否则同一次调用在
            # 「已提交」与「中断补记」两条路径下落库的形态不同，重建历史时会不一致。
            output = _present_tool_output(
                output, tool_name=name, session_id=session_id, call_key=callkeys.get(_idx) or "",
            ) if output else output
        else:
            ok, output, error, dur = False, "", ABORTED_BEFORE_DISPATCH, 0
        call_key = callkeys.get(_idx) or ("tc_" + uuid.uuid4().hex[:12])
        # 上下文与落库用同一份文本：只追加到 messages 而不落库会让两侧不一致
        _model_content = output if ok and output else (error or "")
        if messages is not None:
            messages.append(ChatMessage(
                role="tool", content=_model_content,
                name=name, tool_call_id=tc.get("id", ""),
            ))
        try:
            await message_service.create_message(
                db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                sender_type=SenderType.AGENT.value, sender_id=agent_id,
                msg_type=MsgType.TOOL_CALL.value,
                content={"tool": name, "args": tc.get("arguments") or {}, "call_key": call_key,
                         "agent_name": agent_name},
                buffered=True,
            )
            await message_service.create_message(
                db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
                sender_type=SenderType.AGENT.value, sender_id=agent_id,
                msg_type=MsgType.TOOL_RESULT.value,
                content={"tool": name, "call_key": call_key, "ok": ok,
                         "output": output, "error": error, "duration_ms": dur,
                         "agent_name": agent_name},
                buffered=True,
            )
        except Exception:  # noqa: BLE001 — 补记录失败不影响中断本身
            logger.debug("[agent] 中断补记工具结果失败 tool=%s", name, exc_info=True)


# 产出屏幕截图的工具前缀：电脑操控与内置浏览器（用户附件、view_image 读的图不在此列）
_SCREEN_TOOL_PREFIXES = ("desktop_", "browser_")
# 本轮上下文保留最近几张工具截图；超过「保留数 + 块大小」时一次裁到保留数
_KEEP_TOOL_IMAGES = 3
_PRUNE_TOOL_IMAGES_CHUNK = 3


def _prune_tool_images(image_msgs: list[ChatMessage]) -> int:
    """把较早的工具截图替换为文字占位，返回本次裁掉的张数（plan-20261019-desktop-precision）。

    为什么：截图会在本轮每次请求里重复发送。实测一轮二十多张截图后 prompt 从 34K 涨到 68K，
    单步耗时从 2 s 涨到 7–22 s；而旧截图对决策几乎没有价值。按块裁剪（攒满 3 张再一起裁）
    是 Anthropic 参考实现的做法：减少前缀变化次数，让供应商侧的提示缓存尽量多命中。
    """
    live = [m for m in image_msgs if m.content_blocks]
    if len(live) <= _KEEP_TOOL_IMAGES + _PRUNE_TOOL_IMAGES_CHUNK:
        return 0
    drop = live[: len(live) - _KEEP_TOOL_IMAGES]
    for m in drop:
        m.content_blocks = None
        m.content = "[系统] 这里原有一张较早的截图，已省略以控制上下文；请以最新截图为准。"
    return len(drop)


async def _emit_agent_msg(db, *, session_id, turn_id, thread_id, agent_id, agent_name,
                          msg_type, content) -> None:
    await message_service.create_message(
        db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
        sender_type=SenderType.AGENT.value, sender_id=agent_id,
        msg_type=msg_type.value, content=content,
        buffered=True,  # 热路径消息走 write-behind 批量落库
    )


def _is_stream_timeout(exc: Exception) -> bool:
    """判断流式调用异常是否为空闲/分块超时（ta3 ReadTimeout、openai chunk timeout）。

    ta3 把 httpx.ReadTimeout 转成 RuntimeError("模型请求超时：ReadTimeout")，
    openai_compatible 转成 RuntimeError("model gateway error: stream chunk timeout")，
    消息/类型均含超时关键字；asyncio.TimeoutError 直接命中 isinstance。
    """
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "timeout" in name or "timeout" in msg or "超时" in msg


def _response_failure_reason(response, has_progress: bool = False) -> tuple[str, bool] | None:
    """模型响应健康检查：返回 (用户可见原因, 是否致命) 或 None（健康）。

    v28: 空响应（无 content/thinking/tool_calls）与输出截断/超时中断不再静默完成。
    fatal=True：turn 必须终止并标记失败（空响应）；fatal=False：提示后继续
    （超时但有部分内容、max_tokens/length 截断——部分内容仍落库展示）。
    v31 (plan-89): 对齐 zcode/AI SDK 语义——本 turn 已有工具产出（has_progress）
    时，finish_reason=stop 的空响应是模型"任务已完成、主动结束对话"的正常信号，
    视为健康直接结束，不触发重试/报错（任务完成后误报"模型返回空响应"的根因）。
    v966: 上述豁免仅适用于"网关已下发数据帧"的应答。若 provider 报告零帧
    （frames_received == 0，如 ta3 网关 HTTP 200 空流/仅 [DONE]），说明请求期间
    网络卡顿或服务器处理超时——模型根本没有应答，不能判定为"结束"，
    必须 fatal 走多次重试（与访问异常重试一致），否则任务会静默中断。
    frames_received=None（provider 未提供信号）时保持旧逻辑不变。
    v967: "仅思考无正文"（thinking 有实质推理内容、content 为空、无 tool_calls、
    finish=stop）不再视为健康结束。真正"任务完成主动结束"会产出最终正文；只有思考
    说明推理结束后流被截断或输出预算被思考吃光，用户什么都没拿到。
    纳入 fatal → 按统一重试计划重试（重试带 effort 降档，通常可恢复），穷尽后由
    调用方显式提示异常，不做"把思考提升为正文"的兜底。>10 字符的门槛用于与上方
    "残缺思考片段"判据互斥。
    v968: 撤销 v31 的 has_progress 豁免——实测网关截断输出流时（HTTP 200 但无
    content/thinking/tool_calls、无 usage），只要本 turn 执行过工具就会被判
    "任务完成、主动结束"，turn 静默收尾：用户视角是"回了几句话后无故中断"，
    既无报错也无重试。真正"任务完成主动结束"必然产出最终正文，完全空响应几乎
    只来自流截断；一律纳入 fatal 走统一重试计划（次数即间隔序列长度），
    重试穷尽后由调用方显式报错。
    """
    finish = response.finish_reason or "stop"

    # 畸形流检测：网关声明 finish_reason=tool_calls 但工具调用为空，或流截断只返回极短思考
    if finish == "tool_calls" and not response.tool_calls:
        return "网关响应异常 (finish_reason=tool_calls 但无工具调用数据，流丢帧)", True

    if not response.content and not response.tool_calls and response.thinking and len(response.thinking.strip()) <= 10:
        return "网关流式输出异常截断 (仅返回残缺思考片段)", True

    # v967: 仅思考无正文——有实质推理产出，却没有任何正文/工具调用且 finish=stop。
    # 这不是"任务完成主动结束"（那会给出最终正文），多为推理阶段结束后网关截断 SSE
    # 或输出预算被思考吃光。此前该形态漏过上方两条判据（全空分支要求 thinking 为空），
    # 落到函数末尾被判健康，表现为"会话突然中断且无报错"。现纳入 fatal，走统一重试计划。
    if (finish == "stop" and not response.content and not response.tool_calls
            and response.thinking and len(response.thinking.strip()) > 10):
        return "模型仅生成思考未输出正文 (finish_reason=stop)，任务可能未完成", True

    if not response.content and not response.thinking and not response.tool_calls:
        # v966: 零帧断流优先判定（即使 finish=stop/有产出）——网关未应答即异常
        if response.frames_received == 0:
            return (
                "模型长时间未返回任何数据（网关断流或网络异常），任务未完成",
                True,
            )
        if finish in ("timeout", "thinking_timeout"):
            return ("模型思考超时，未生成任何内容" if finish == "thinking_timeout"
                    else "模型响应因网关空闲超时中断，未生成任何内容"), True
        # v968: 撤销 v31 的 has_progress 豁免（原语义：本 turn 已有工具产出时，
        # stop 空响应 = 模型"任务完成、主动结束"→ 健康）。实测该响应无
        # content/thinking/tool_calls 且无 usage，是网关截断输出流的特征——被
        # 静默当"任务完成"后，用户看到"回了几句话后无故中断"（无报错、无重试）。
        if finish == "stop" and has_progress:
            return (
                "模型返回空响应 (finish_reason=stop)，未生成内容或工具调用；"
                "本 turn 已有执行产出，疑似网关截断输出流，任务未完成",
                True,
            )
        return f"模型返回空响应 (finish_reason={finish})，未生成内容或工具调用", True
    # plan-20261020-retry-cred-perf R2: 超时/思考超时且**无正文、无工具调用**一律致命——
    # 即使已经有思考内容。长思考模型的典型断流形态就是“思考完、正文一个字符没出”，
    # 旧逻辑此时 `return …, False` → 不重试、turn 静默收尾，用户什么都拿不到。
    # 带正文的部分输出仍保留并提示（非致命），避免重试后 UI 文字重复。
    if finish in ("timeout", "thinking_timeout") and not response.content and not response.tool_calls:
        return ("模型思考超时，未生成任何内容" if finish == "thinking_timeout"
                else "模型响应因网关空闲超时中断，未生成任何内容"), True
    if finish == "timeout":
        return "响应因网关空闲超时中断，以上为已生成的部分内容", False
    if finish in ("max_tokens", "length"):
        if response.tool_calls:
            return None  # 已有工具调用，截断不影响本轮动作
        if not response.content:
            # v45: 截断且零正文——推理模型的 thinking 先耗尽了输出预算（实测 finish=length、
            # content_len=0）。旧逻辑判非致命→只提示不重试，用户看到"输出达到 token 上限"后任务中断。
            # 改为致命，交给统一重试计划（默认 10/20/30 秒）重试，通常可恢复。
            return "输出达到 token 上限且未生成正文（推理可能已耗尽输出预算）", True
        return "输出达到 token 上限，可能不完整", False
    return None


async def _anext_with_cancel(stream, cancel_event: asyncio.Event | None):
    """逐项拉取流式事件；等待下一个事件期间可被 cancel_event 即时中断（问题5）。

    原实现 `async for event in provider.stream_structured(request)` 只在拿到事件后检查
    cancel_event——等待下一个 chunk（LLM 长思考/网关排队/网络空闲）期间取消不可达，
    停止要等一个 chunk 或 idle 超时。此处用 asyncio.wait 把「取下一事件」与
    cancel_event.wait() 竞速，cancel 命中即中断迭代（循环正常结束，由调用方根据
    cancel_event.is_set() 判定走 cancelled 收尾）。
    取消未完成的取事件任务会让底层 httpx 流在 CancelledError 时关闭连接。
    """
    it = stream.__aiter__()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return
        next_task = asyncio.ensure_future(it.__anext__())
        cancel_task = asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
        try:
            wait_tasks = [t for t in (next_task, cancel_task) if t is not None]
            done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task is not None and cancel_task in done:
                if not next_task.done():
                    next_task.cancel()
                try:
                    await next_task
                except BaseException:
                    pass
                return
            yield next_task.result()
        except StopAsyncIteration:
            return
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()


async def _chat_with_cancel(provider, request, cancel_event: asyncio.Event | None):
    """非流式调用 provider.chat，等待期间可被 cancel_event 即时中断（问题5）。

    中断返回 None（调用方据此走 cancelled 收尾）；正常返回 ChatResponse。
    """
    if cancel_event is None:
        return await provider.chat(request)
    chat_task = asyncio.ensure_future(provider.chat(request))
    cancel_task = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({chat_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done:
            chat_task.cancel()
            try:
                await chat_task
            except BaseException:
                pass
            return None
        return chat_task.result()
    finally:
        if not cancel_task.done():
            cancel_task.cancel()


def _opening_language_mismatch(text: str, expected: str) -> bool:
    """开头已能判定、且与目标语言不同时返回 True。信号不足不算不符。"""
    if expected not in ("zh", "en"):
        return False
    from app.orchestration.prompts.language import detect_reply_language
    detected = detect_reply_language(text[:240])
    return detected not in ("auto", expected)


async def _stream_chat_and_broadcast(provider, request, *, session_id, turn_id, agent_id, agent_name,
                                     cancel_event: asyncio.Event | None = None,
                                     thread_id: int | None = None,
                                     reply_language: str = "auto"):
    """流式调用 provider，实时广播 thinking/content delta。

    v6.4: 支持 cancel_event —— 流式输出中检测到中断信号时立即终止，返回已收到的部分内容。
    v19: 事件 payload 携带 thread_id（子代理线程），前端据此将子代理流式内容分桶到右面板，
    不再混入主消息流；流式结束后统一剥离正文中的内联 <thinking> 标签。
    v28: 空闲/分块超时不再回退非流式，保留已收到的部分内容并以 finish_reason="timeout" 结束
    （长思考模型 30s 硬编码断流问题的修复点）。
    """
    from app.models.schemas import ChatResponse, Usage as UsageModel
    full_content = ""
    full_thinking = ""
    tool_calls = []
    finish_reason = "stop"
    usage = UsageModel()
    _cancelled = False
    # v966: provider 报告的数据帧数（None=未提供信号，如非流式回退/其他 provider）
    frames_received: int | None = None
    # v27: 流式实时剥离正文内联思考标签——否则前端流式过程中会看到 <thinking> 原文。
    # 最终全量内容仍由下方 _split_inline_thinking 兜底（幂等），保证不重复计入。
    _splitter = _InlineThinkingStreamSplitter()
    _held = ""
    _released = False
    # plan-20261021-usage-update：计时。start = 开始等待首个事件（请求即将发出）；
    # Provider 在首个真实输出帧（含纯工具调用）写 request.timing["first"]；
    # 没接入的 Provider 以首个 thinking / content 事件到达时刻兜底；done 事件到达即流结束。
    _t_req = time.monotonic()
    request.timing = {"start": _t_req}
    _t_first_evt: float | None = None
    _t_end: float | None = None
    try:
        async for event in _anext_with_cancel(provider.stream_structured(request), cancel_event):
            # v6.4: 流式输出中检查中断信号，立即停止（竞速后为幂等兜底）
            if cancel_event and cancel_event.is_set():
                logger.warning("[agent] turn=%s 流式输出中收到中断信号，停止接收", turn_id)
                _cancelled = True
                finish_reason = "cancelled"
                break
            if _t_first_evt is None and event["type"] in ("thinking", "content") and event.get("delta"):
                _t_first_evt = time.monotonic()
            if event["type"] == "thinking":
                delta = event.get("delta", "")
                full_thinking += delta
                await broadcast(session_id, {
                    "event": "thinking.delta",
                    "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": delta,
                                "thread_id": thread_id},
                })
            elif event["type"] == "content":
                delta = event.get("delta", "")
                _c, _t = _splitter.feed(delta)
                # 思考块在正文前广播：标签块总是先于其后正文闭合
                if _t:
                    await broadcast(session_id, {
                        "event": "thinking.delta",
                        "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": _t,
                                    "thread_id": thread_id},
                    })
                if _c:
                    if not _released and reply_language in ("zh", "en") and not request.tools:
                        _held += _c
                        if len(_held) < 80 and not _opening_language_mismatch(_held, reply_language):
                            continue
                        if _opening_language_mismatch(_held, reply_language):
                            continue  # 留待 done 事件确认没有工具调用后决定是否重试
                        _c = _held
                        _held = ""
                        _released = True
                    elif _held and not _released:
                        _c = _held + _c
                        _held = ""
                        _released = True
                    full_content += _c
                    await broadcast(session_id, {
                        "event": "token.delta",
                        "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": _c,
                                    "thread_id": thread_id},
                    })
            elif event["type"] == "done":
                _t_end = time.monotonic()
                _c, _t = _splitter.flush()
                if _t:
                    await broadcast(session_id, {
                        "event": "thinking.delta",
                        "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": _t,
                                    "thread_id": thread_id},
                    })
                if _c:
                    _held += _c
                event_content = event.get("content") or ""
                # plan-20261023-thinking-blank：正文与思考同理——结束帧的 content 只有含实质文字时才覆盖
                # 流式累积值。网关截断 / 重试后常回一两个空白字符当占位，`or` 会把它当有效值，
                # 把已经广播给用户的正文覆盖成空白（消息流里正文凭空消失）。
                if event_content.strip():
                    full_content = event_content
                elif not full_content:
                    full_content = _held
                # plan-20261023-thinking-blank：结束帧的 thinking 只有含实质内容时才覆盖流式累积值。
                # 网关在重试 / 降档后常回一个纯空白 reasoning（如单个空格）当占位；旧写法用
                # `or` 判定，空白是非空字符串 → 被当成有效值，把已经广播给用户的完整思考覆盖掉。
                # 用户看到的过程就是「思考中 ticker 有内容，思考结束后展开一片空白」，落库同样只剩空格。
                _done_thinking = event.get("thinking") or ""
                if _is_substantive_thinking(_done_thinking):
                    full_thinking = _done_thinking
                tool_calls = event.get("tool_calls", [])
                finish_reason = event.get("finish_reason", "stop")
                usage = event.get("usage", UsageModel())
                frames_received = event.get("frames")
                if not tool_calls:
                    if event_content.strip():
                        full_content = event_content
                    elif not full_content:
                        full_content = _held
                    if _opening_language_mismatch(full_content, reply_language):
                        return ChatResponse(
                            content=None,
                            thinking=(full_thinking if _is_substantive_thinking(full_thinking) else None),
                            finish_reason="language_mismatch", usage=usage,
                        )
                    # 增量此前均被缓冲；此时发送完整正文一次，不重复追加。
                    if full_content:
                        await broadcast(session_id, {
                            "event": "token.delta",
                            "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": full_content,
                                        "thread_id": thread_id},
                        })
                elif _held:
                    # 工具调用步骤的说明文字保留原有行为，不用最终回复语言门闩。
                    full_content = full_content or _held
                    await broadcast(session_id, {
                        "event": "token.delta",
                        "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": _held,
                                    "thread_id": thread_id},
                    })
                _held = ""
                _released = True
                break
        # 问题5: 若中断发生在「等待下一事件」窗口（_anext_with_cancel 正常结束迭代而非 break），
        # 循环退出时 cancel_event 已置位——此处统一判定，保证走 cancelled 收尾
        if not _cancelled and cancel_event is not None and cancel_event.is_set():
            _cancelled = True
            finish_reason = "cancelled"
    except Exception as e:
        if _cancelled:
            pass  # 中断导致的异常，忽略
        elif _is_stream_timeout(e):
            # v28: 长思考模型（kimi-k3/grok-4.6）SSE 空闲超时——保留已收到的
            # 部分内容直接结束，不再回退非流式（同一超时限制下必然再等一个周期）。
            _c, _t = _splitter.flush()
            if _t:
                full_thinking = (full_thinking + "\n" + _t).strip() if full_thinking else _t
            if _c:
                full_content += _c
            finish_reason = "timeout"
            logger.warning(
                "[agent] turn=%s 流式调用超时(%s:%s)，保留部分内容 thinking=%d content=%d",
                turn_id, type(e).__name__, str(e)[:120], len(full_thinking), len(full_content),
            )
        else:
            # plan-20261020-retry-cred-perf R8: 429 / 鉴权类错误换请求方式也没用——
            # 直接抛给上层换 Key 或重试，不再白白多打一轮非流式请求。
            _stream_kind = classify_failure(e).kind
            if _stream_kind in ("quota", "auth"):
                logger.warning(
                    "[agent] turn=%s 流式调用失败(%s)，跳过非流式回退: %s",
                    turn_id, _stream_kind, str(e)[:160],
                )
                raise
            logger.exception("[agent] turn=%s 流式调用异常，回退非流式", turn_id)
            # 问题5: 非流式回退也可取消——与 cancel_event 竞速，命中即中断该请求
            response = await _chat_with_cancel(provider, request, cancel_event)
            if response is None:
                logger.warning("[agent] turn=%s 非流式回退被用户中断", turn_id)
                _cancelled = True
                finish_reason = "cancelled"
            else:
                full_content = response.content or ""
                full_thinking = response.thinking or ""
                tool_calls = response.tool_calls or []
                finish_reason = response.finish_reason
                usage = response.usage

    # v19: 剥离正文中的内联思考标签（部分网关把思考写进 content）
    # 超时、取消或流异常若发生在 done 前，尚未释放的开头也必须保留；
    # 结束态不再重试，以免丢失已经生成的部分内容。
    if _held and not _released:
        full_content = full_content or _held
        if not _cancelled and finish_reason not in ("timeout", "cancelled"):
            await broadcast(session_id, {
                "event": "token.delta",
                "payload": {"agent_id": agent_id, "turn_id": turn_id, "delta": _held,
                            "thread_id": thread_id},
            })
    if full_content:
        _clean, _inline_think = _split_inline_thinking(full_content)
        if _inline_think:
            full_thinking = (full_thinking + "\n" + _inline_think).strip() if full_thinking else _inline_think
            full_content = _clean

    # plan-20261023-thinking-blank：纯空白思考不广播——前端收到 full_text 后会把它当成落库文本，
    # 覆盖掉流式累积的真实内容，思考块随即变成空白。
    if _is_substantive_thinking(full_thinking):
        await broadcast(session_id, {
            "event": "thinking.done",
            "payload": {"agent_id": agent_id, "turn_id": turn_id, "full_text": full_thinking,
                        "thread_id": thread_id},
        })
    if full_content.strip():
        await broadcast(session_id, {
            "event": "token.done",
            "payload": {"agent_id": agent_id, "turn_id": turn_id, "full_text": full_content,
                        "thread_id": thread_id},
        })
    # plan-20261021-usage-update：只有「流正常走到 done」才产生实测计时；取消 / 超时 / 异常回退
    # 非流式的响应没有可信的首字与结束时刻，交给用量页按时间戳估算。
    _first_ms: int | None = None
    _gen_ms: int | None = None
    _t_first = (request.timing or {}).get("first") or _t_first_evt
    if _t_first is not None and _t_end is not None and not _cancelled:
        _first_ms = max(0, int((_t_first - _t_req) * 1000))
        _gen_ms = max(0, int((_t_end - _t_first) * 1000))
    return ChatResponse(
        # plan-20261023-thinking-blank：正文与思考同一口径——只有含实质文字才作为有效内容返回。
        # 网关的空白占位一旦当正文返回，上游会落库成一条看不见内容的消息。
        content=(full_content if full_content.strip() else None),
        # plan-20261023-thinking-blank：只含空白的思考等同于没有思考——返回 None 后上游不会落库，
        # 也不会再生成一个展开后空白的思考块（历史库里的空格记录由前端时间线统一跳过）。
        thinking=(full_thinking if _is_substantive_thinking(full_thinking) else None),
        tool_calls=tool_calls, finish_reason=finish_reason, usage=usage,
        frames_received=frames_received,
        first_token_ms=_first_ms, gen_ms=_gen_ms,
    )


def _make_approval_emitter(session_id: int, pending_reminders: list[str] | None = None):
    """审批请求回调：广播 WS 事件，并触发 permission_request 钩子。

    S12（plan-41-197）：提示词型钩子的文本追加到 pending_reminders，
    在下一步构建 api_messages 时注入；命令型钩子在此执行。全部 fail-open，
    钩子失败不影响审批广播（用户仍能看到审批卡）。
    """

    async def _emit(approval_id: str, detail: dict) -> None:
        try:
            # 独立短会话：审批回调不在主循环 db 上下文里，避免干扰主连接
            from app.persistence.database import async_session_factory
            async with async_session_factory() as _hdb:
                _matcher = str(detail.get("tool") or "") or None
                for _hr in await hook_service.run_hooks(
                    _hdb, "permission_request",
                    {"approval_id": approval_id, "detail": detail, "session_id": session_id},
                    matcher_key=_matcher,
                ):
                    _inj = (_hr or {}).get("inject")
                    if _inj and pending_reminders is not None:
                        pending_reminders.append(f"[钩子提示词]\n{_inj}")
        except Exception:
            logger.debug("[agent] permission_request 钩子执行失败(不阻断)", exc_info=True)
        await ws_manager.broadcast(
            session_id,
            {"event": "approval.request", "payload": {"approval_id": approval_id, "detail": detail}},
        )

    return _emit


# ── 子代理工具处理 ──

# plan-64-291: 运行中子代理提醒的节流间隔（步）——集合不变时至少间隔这么多步才重复注入
_SUBAGENT_REMINDER_STEP_GAP = 3


def _should_emit_running_reminder(*, has_running: bool, sig: tuple, last_sig: tuple,
                                  step: int, last_step: int) -> bool:
    """plan-64-291: 运行中提醒的节流判定。

    集合变化（新派发/完工）立即发；集合不变时每 _SUBAGENT_REMINDER_STEP_GAP 步才发一次
    ——避免每步都注入挤占上下文（对齐 Anthropic 的多智能体成本警示）；无运行中子代理不发。
    """
    if not has_running:
        return False
    if sig != last_sig:
        return True
    return step - last_step >= _SUBAGENT_REMINDER_STEP_GAP


def _format_elapsed_zh(seconds: float) -> str:
    """把已运行秒数格式化为中文时长（运行中提醒展示用）。"""
    if seconds < 60:
        return f"{int(seconds)} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} 分钟"
    return f"{minutes / 60:.1f} 小时"


def _build_subagent_running_reminder(
    snapshot: list[dict], max_conc: int | None = None, pending: int | None = None,
) -> str:
    """plan-64-291: 构造「子代理运行中」提醒（等待期纪律的持续在场）。

    只陈述事实（谁在跑、跑了多久）+ 纪律三条，不替主代理决定具体做什么；
    子代理全部结束后自然消失（下一步 running_snapshot 为空即复位）。

    plan-549-2281（复盘）：补一句并发额度（上限 / 占用 / 剩余）。
    此前主代理只有「已经派满了被拒」那一刻才知道上限，派发前只能盲派；
    这条提醒每步都在场，能提前看到还剩几个名额，就不会把额度用到报错。
    """
    parts: list[str] = []
    for _r in snapshot:
        _title = str(_r.get("title") or "未命名任务")
        _elapsed = _r.get("elapsed_s")
        _suffix = ""
        if isinstance(_elapsed, (int, float)):
            _suffix = f"（已运行 {_format_elapsed_zh(float(_elapsed))}）"
        parts.append(f"#{_r.get('agent_id')}「{_title}」{_suffix}")
    # 额度行只在调用方给出上限时出现（子代理自身线程不提醒，不传这两个参数）
    quota = ""
    if max_conc is not None and max_conc > 0:
        used = pending if pending is not None else len(snapshot)
        quota = (
            f"并发额度：上限 {max_conc}，当前在跑 {used}，剩余 {max(0, max_conc - used)}。"
            "剩余为 0 时不要再派发，先把已有结果收回来。\n"
        )
    return (
        "[子代理运行中] 仍在运行：" + "、".join(parts) + "。"
        "子代理看起来慢通常只是还在跑，不代表卡死。\n"
        + quota +
        "等待期纪律：① 不要自己执行它们的任务——重复劳动会双倍消耗 token 与时间；"
        "② 只做与它们不重叠的工作；"
        "③ 没有非重叠工作可做时，调用 collect_results(wait=true) 阻塞等待，完成后统一整合其报告。"
    )

# plan-20261020-bgtask：后台命令运行中提醒的节流间隔（步）——集合不变时至少间隔这么多步才重复注入
_BG_REMINDER_STEP_GAP = 8


def _build_bg_running_reminder(rows: list[dict]) -> str:
    """plan-20261020-bgtask：本轮启动、仍在运行的后台命令提醒。

    只陈述事实 + 两条纪律：会自己结束的不必管（跑完会主动回报）；长驻服务用完必须主动停，
    确需保留给用户时要在最终回复里说明。集合清空后自然不再注入。
    """
    parts: list[str] = []
    for _r in rows[:5]:
        _cmd = " ".join(str(_r.get("command") or "").split())[:80]
        parts.append(
            f"`{_cmd}`（{_r.get('shell_id')}，已运行 {_format_elapsed_zh(float(_r.get('elapsed') or 0))}）"
        )
    more = f"（另有 {len(rows) - 5} 个未列出）" if len(rows) > 5 else ""
    return (
        "[后台命令运行中] 你在本轮启动的后台命令仍在运行：" + "；".join(parts) + more + "。\n"
        "· 会自己结束的（构建、测试）：跑完后系统会主动把结果送回来，无需轮询，继续做别的即可；\n"
        "· 长驻服务（dev server / watch / 后端）：不再需要时立刻用 terminal_bg_kill（BashKill）停止，"
        "不要留在后台占着端口；确需保留给用户使用时，在最终回复里说明它在运行以及如何访问。"
    )


def _subagent_context_snapshot(subagent_context: dict, original_request: str = "") -> dict:
    """plan-248-1258 M6: 汇总主代理的当前上下文快照，传给子代理并留档供 inspect。

    让子代理继承任务关键上下文（原始请求、当前清单、沙箱/工作区），
    同时主代理事后可经 subagent_inspect 回看当时交接了什么。
    """
    snap: dict = {}
    try:
        if original_request:
            snap["original_request"] = original_request[:2000]
        for key in ("main_task_id", "model_id", "permission_mode", "approval_mode"):
            v = subagent_context.get(key)
            if v not in (None, ""):
                snap[key] = v
        todos = subagent_context.get("todos")
        if isinstance(todos, list) and todos:
            snap["todos"] = [
                {"content": str(t.get("content"))[:200], "status": t.get("status")}
                for t in todos[:20] if isinstance(t, dict)
            ]
        summary = subagent_context.get("context_summary")
        if summary:
            snap["context_summary"] = str(summary)[:1500]
    except Exception:  # noqa: BLE001
        logger.debug("[agent] 子代理上下文快照构建失败(非阻塞)", exc_info=True)
    return snap


# 等待被新消息打断时交给调用方的提示（消息本身留在注入队列里，下一步由注入通道交给模型）
_WAIT_WOKEN_NOTE = "等待期间收到了新的消息（见紧随其后的消息），请先处理它。"


def _has_injected_inputs(turn_id: int, *, urgent_only: bool = False) -> bool:
    try:
        from app.orchestration.engine import has_injected_inputs

        return has_injected_inputs(turn_id, urgent_only=urgent_only)
    except Exception:  # noqa: BLE001 — 取不到队列状态按「没有」处理，退回原有的等待行为
        return False


async def _await_background_subagents(manager, *, session_id: int, turn_id: int,
                                      thread_id: int | None,
                                      cancel_event: asyncio.Event | None) -> list[str] | None:
    """v43: 主代理收工后等待后台子代理结束（turn 保持运行，前端显示「等待子代理结束…」）。

    语义（对齐用户需求）：后台子代理运行时主代理可继续并行工作；主代理做完自己的事
    （本步无工具调用、准备结束 turn）而子代理仍在跑时，turn **不结束**——阻塞等待，
    期间会话保持 running（左侧转圈、输入框显示停止按钮），子代理结束后由调用方把
    完成报告注入同一 turn 继续，不伪造用户消息、不新开唤醒轮。

    返回：
        - list[str]：等待结束后待注入的完成报告（可能为空，表示无报告可取）；
        - None：等待期间收到用户中断信号（调用方应结束 turn；子代理已由 cancel_turn 统一取消）。

    每个采样周期（0.5s）检查一次中断信号，保证停止按钮响应及时；
    超过 settings.subagent_wait_timeout_s 仍未结束则停止等待（剩余结果走完成唤醒兜底）。
    """
    def _pending() -> int:
        try:
            return int(manager.pending_count())
        except Exception:
            return 0

    async def _show(pending: int) -> None:
        label = (f"等待子代理结束…（{pending} 个运行中）" if pending > 1 else "等待子代理结束…")
        try:
            await _broadcast_turn_status(session_id, turn_id, thread_id, label)
        except Exception:
            logger.debug("[agent] 等待子代理状态广播失败(非阻塞)", exc_info=True)

    async def _clear() -> None:
        try:
            await _broadcast_turn_status(session_id, turn_id, thread_id, "")
        except Exception:
            logger.debug("[agent] 等待子代理状态清除失败(非阻塞)", exc_info=True)

    timeout_s = float(getattr(settings, "subagent_wait_timeout_s", 1800.0) or 0.0)
    started = time.monotonic()
    pending = _pending()
    if pending > 0:
        logger.info("[agent] turn=%s 主代理已收工，等待 %d 个后台子代理结束（上限 %.0fs）",
                    turn_id, pending, timeout_s)
        await _show(pending)
        while pending > 0:
            if cancel_event is not None and cancel_event.is_set():
                logger.warning("[agent] turn=%s 等待子代理期间收到中断信号", turn_id)
                await _clear()
                return None
            if timeout_s > 0 and (time.monotonic() - started) >= timeout_s:
                logger.warning("[agent] turn=%s 等待子代理超时(%.0fs)，剩余 %d 个转由完成唤醒兜底",
                               turn_id, timeout_s, pending)
                break
            # 等待期间到达的消息（用户追加 / 子代理提问 / 子会话汇报）不能被晾到子代理结束：
            # 提问方（后台子代理）正阻塞等答复，主代理却在这里等它——互相等到超时。
            # 让出等待，调用方带着提示继续下一步，由注入通道把消息交给模型。
            if _has_injected_inputs(turn_id, urgent_only=True):
                await _clear()
                return [_WAIT_WOKEN_NOTE]
            try:
                # 单次最多等 0.5s：既避免忙轮询，又保证停止按钮响应时延可接受
                await manager.wait_one(timeout=0.5)
            except Exception:
                await asyncio.sleep(0.5)
            _now_pending = _pending()
            if _now_pending != pending:
                pending = _now_pending
                if pending > 0:
                    await _show(pending)
        await _clear()
    # 收尾瞬间才到的消息同样要处理（否则 turn 一结束，这些注入项就被丢弃）
    if _has_injected_inputs(turn_id):
        return [_WAIT_WOKEN_NOTE]
    try:
        return list(manager.drain_completions())
    except Exception:
        logger.debug("[agent] 子代理完成报告取出失败(非阻塞)", exc_info=True)
        return []


def _running_subagents_note(manager) -> str:
    """并发闸门拒绝时的「现在谁在跑」摘要（plan-20261022-subagent-reject-fast）。

    主模型只有知道占用名额的是哪些任务，才能判断该 collect_results 收谁的结果、
    哪些活可以自己做，而不是盲目重试一次派发。异常一律降级为空串，不影响拒绝本身。
    """
    try:
        snap = manager.running_snapshot()
    except Exception:
        return ""
    parts: list[str] = []
    for s in snap[:5]:
        title = str(s.get("title") or "未命名任务")
        elapsed = s.get("elapsed_s")
        suffix = f"（已运行 {int(elapsed)} 秒）" if isinstance(elapsed, (int, float)) else ""
        parts.append(f"#{s.get('agent_id')} {title}{suffix}")
    if len(snap) > 5:
        parts.append(f"等共 {len(snap)} 个")
    return "、".join(parts)


# plan-549-2281 复盘修复：操控子代理的档位解析抽成纯函数——此前逻辑内联在 spawn 分支里，
# 测试只剩一条近乎永真的断言，真实解析（lowest=最低非关闭档 / follow / 指定档）零覆盖。
_EFFORT_RANK: dict[str, int] = {"minimal": 0, "low": 1, "medium": 2, "high": 3}


# plan-549-2281 复盘修复：spawn 的执行模式解析抽成纯函数——此前逻辑内联在 spawn 分支，
# 守护测试只能手工复刻一份等价实现（实现漂移时测试照样绿）。测试现在直接测本函数。
def _mode_kind(mode: object) -> str:
    """模式名 → 判定档（plan-560-2311 C1）。

    本模块的模式护栏统一走这里，事实源在 `permission_profile_service.resolve_mode_kind`
    （审批层、能力矩阵、提示词层共用同一份口径，避免各写一份而漂移）。
    配置层不可用时退回内置四档匹配——行为与改造前一致，不放大拒绝面。
    """
    raw = str(mode or "").strip()
    try:
        from app.services.permission_profile_service import resolve_mode_kind

        return resolve_mode_kind(raw)
    except Exception:  # noqa: BLE001 — 配置层异常时按原名判定（旧行为）
        logger.warning("[agent] 模式归一失败 mode=%s(按原名判定)", raw, exc_info=True)
        return raw


def resolve_subagent_readonly(mode: object, explore: bool, master_mode: str | None) -> bool:
    """spawn_subagent 的执行模式解析：显式 mode（readonly/agent）压过兼容别名 explore；
    主代理处于只读 / 计划模式时强制只读（模式语义护栏）。

    操控子代理（purpose=operator）不在此函数职责内：只读会话里它被直接拒绝、
    其余会话强制可写，见 spawn 分支。

    plan-560-2311 C1：`master_mode` 按 kind 归一后再比较——此前拿原始模式名与
    ("readonly", "plan") 做等值比较，自定义 kind=readonly/plan 模式两者都不等，
    护栏被整体跳过（自定义只读模式可派可写子代理）。归一事实源在
    `permission_profile_service.resolve_mode_kind`，不改写会话里存的模式名。
    """
    want = str(mode or "").strip().lower()
    if want not in ("readonly", "agent"):
        want = ""
    readonly = (want == "readonly") if want else bool(explore)
    if _mode_kind(master_mode) in ("readonly", "plan"):
        readonly = True
    return readonly


def lowest_open_effort(levels: list[str]) -> str:
    """模型声明的档位里取最低的「非关闭」档；读不到或全关闭时用 low。

    不能直接取列表首元素——reasoning_efforts 的顺序由用户配置决定，未排序时取错档。
    """
    open_levels = [
        str(x or "").lower() for x in (levels or [])
        if str(x or "").strip() and str(x).lower() != "none"
    ]
    if not open_levels:
        return "low"
    open_levels.sort(key=lambda x: (_EFFORT_RANK.get(x, 50), x))
    return open_levels[0]


def _looks_like_effort_typo(word: str) -> bool:
    """判断该值是否像是对已知档位名的拼写错误（lowestt / hihg …）。

    只在「几乎拼错」的窄范围内判定，避免把供应商自定义档位名误伤成错值。
    """
    from difflib import get_close_matches

    return bool(get_close_matches(
        word, ["lowest", "follow", *sorted(_EFFORT_RANK)], n=1, cutoff=0.85,
    ))


def resolve_operator_effort(want: str, levels: list[str]) -> str | None:
    """操控子代理思考档位：follow=跟随主档位（None）；lowest=最低非关闭档；其余按字面。

    plan-549-2281 修复：旧实现把任意字符串原样透传（`return w[:20]`），
    用户把 `operator_subagent_effort` 配成 "none" 或拼错成 "lowestt" 时，这个值会直接进
    模型请求——网关要么拒绝要么静默降级，用户完全看不出是配置错了。现在按四档判定：
      ① 命中模型本轮声明的档位（大小写不敏感）→ 返回声明里的原始字面量（部分模型区分大小写）；
      ② 命中通用档位语义（minimal/low/medium/high）→ 按字面传，各供应商自行解释；
      ③ 关闭语义（none）或明显拼错（lowestt）→ 记为无效配置，回落最低非关闭档；
      ④ 其余未知值：模型已声明档位就按声明校验（无效即回落），读不到档位则保持旧的宽松透传
         （供应商可能只在请求期才确定档位，硬拒会把可用配置变成不可用）。
    """
    w = str(want or "lowest").strip().lower() or "lowest"
    if w == "follow":
        return None
    if w == "lowest":
        return lowest_open_effort(levels)
    declared = [str(x) for x in (levels or []) if str(x or "").strip()]
    for level in declared:
        if level.strip().lower() == w:
            return level[:20]
    if w in _EFFORT_RANK:
        return w[:20]
    if w == "none" or _looks_like_effort_typo(w):
        # none = 关闭思考，不是可用档位；拼错值必然被网关拒绝或静默降级
        logger.warning(
            "[agent] 操控子代理档位配置无效(%r)，回落最低非关闭档", want,
        )
        return lowest_open_effort(declared)
    if declared:
        logger.warning(
            "[agent] 操控子代理档位 %r 不在模型支持档位 %s 中，回落最低非关闭档", want, declared,
        )
        return lowest_open_effort(declared)
    return w[:20]


def _subagent_concurrency_rejection(running: int, max_conc: int, running_note: str = "") -> str:
    """并发已满时回给主模型的说明——**立即拒绝**，不再排队等空位。

    plan-20261022-subagent-reject-fast：原实现先等 60 秒再拒绝。等待期主模型拿不到
    任何进度、用户只看到一张卡住的卡片，最后结果仍是失败——等待不产生收益，
    只把「并发满」这件事延后一分钟才告诉主模型。这里一次性说清原因、占用者与下一步。
    """
    detail = f"\n当前在运行：{running_note}" if running_note else ""
    return (
        f"Error: 会话内子代理并发已满（{running}/{max_conc} 个在运行），"
        "本次未创建子代理，派发已取消。"
        f"{detail}\n"
        "不要重复调用 spawn_subagent 重试。请先 collect_results(wait=true) 收取已有结果，"
        "或直接自己完成剩余工作；确需更多并行时，由用户到 设置 →「子代理」调高"
        f"「单会话同时运行上限」（当前 {max_conc}），等有子代理结束后再派发。"
    )


async def _run_subagent_tool(db, *, tool_name, args, session_id, turn_id, agent, workspace,
                             subagent_context, agent_model_id: int | None = None,
                             parent_agent_id: int | None = None) -> str:
    """spawn_subagent / collect_results / subagent_inspect 工具实现。

    返回给模型的文本结果。
    """
    import json as _json

    if tool_name == "collect_results":
        manager = subagent_context.get("manager")
        if manager is None:
            return "No subagents were spawned in this turn."
        # v36 (plan-321-1600 M3): agent_id 精确查询单个子代理（对齐 ZCode TaskQuery）
        _only: int | None = None
        if args.get("agent_id") is not None:
            try:
                _only = int(args.get("agent_id"))
            except (TypeError, ValueError):
                return "Error: agent_id must be an integer."
        try:
            _report_offset = max(0, int(args.get("report_offset") or 0))
        except (TypeError, ValueError):
            _report_offset = 0
        # plan-248-1258 M6: wait=true 时阻塞等待全部子代理完成（替代反复轮询空转）
        _wait_note = ""
        if bool(args.get("wait", False)) and manager.pending_count() > 0:
            # 分段等待而不是 wait_all 一等到底：期间有新消息（子代理提问 / 用户追加 / 子会话汇报）就让出，
            # 否则正在等答复的后台子代理与这里互相等，直到各自超时。
            #
            # plan-560-2311 B5：这是一次真正的「等待」，必须登记工具运行态——子代理 P0 提问
            # 触发的 `engine.request_interrupt` 在无运行态时会落进 `_pending_interrupts`，
            # 随后同一 step 的下一个 `terminal_exec` 经 `claim_pending_interrupt` 把打断兑现到
            # 自己头上（前台命令被莫名转后台）；同时循环内要能感知用户停止，否则取消后
            # 还要空转到 `subagent_wait` 超时。
            from app.orchestration.engine import (
                claim_pending_interrupt as _claim_pending_interrupt,
                clear_tool_state as _clear_tool_state,
                set_tool_state as _set_tool_state,
                wake_event_for as _wake_event_for,
            )

            _wait_event = _wake_event_for(turn_id)
            _wait_cancel = subagent_context.get("cancel_event")
            # 与主执行分支同口径：先清残留信号再登记运行态，否则上一个工具留下的 set 状态
            # 会让本次等待刚进入就判定为「收到新消息」直接让出（消息本身仍在注入队列里）。
            if _wait_event.is_set():
                _wait_event.clear()
            _set_tool_state(turn_id, tool="collect_results", interruptible="abort",
                            detail="等待后台子代理结束")
            try:
                # plan-565-2323 G16：这里等的是「子代理跑完」，不是「人回答」——必须遵守总时长
                # 上限（`subagent_wait_timeout_s`），与 `_await_background_subagents` 同口径。
                # 旧实现只有「用户停止 / 收到新消息」两个出口，子代理因外部原因长期不结束时
                # 主代理会无限阻塞在同一次工具调用里（turn 永不结束、停止按钮之外无法脱身）。
                # 0/负数 = 不限制，与配置注释保持一致。
                _wait_deadline = float(getattr(settings, "subagent_wait_timeout_s", 0.0) or 0.0)
                _wait_started = time.monotonic()
                while manager.pending_count() > 0:
                    # 用户点了停止：立即结束等待（子代理已由 cancel_turn / cancel_all 取消）
                    if _wait_cancel is not None and _wait_cancel.is_set():
                        _wait_note = (
                            "[已停止等待] 本轮已收到停止信号，不再阻塞等后台子代理。\n\n"
                        )
                        break
                    # 邮箱打断：这条消息已在注入队列里，让出后下一次 LLM 调用前就能读到
                    if _wait_event.is_set():
                        _wait_event.clear()
                        _wait_note = (
                            "[已让出等待] 等待期间收到新的消息，先处理它；仍在运行的子代理不受影响，"
                            "需要时再次调用 collect_results(wait=true)。\n\n"
                        )
                        break
                    if _has_injected_inputs(turn_id, urgent_only=True):
                        _wait_note = (
                            "[已让出等待] 等待期间收到新的消息，先处理它；仍在运行的子代理不受影响，"
                            "需要时再次调用 collect_results(wait=true)。\n\n"
                        )
                        break
                    if _wait_deadline > 0 and (time.monotonic() - _wait_started) >= _wait_deadline:
                        # 到上限不是失败：已在跑的继续跑，完成时走唤醒通道送达
                        _wait_note = (
                            f"[已停止等待] 已等待 {int(_wait_deadline)} 秒仍未结束，"
                            "剩余子代理完成时会自动通知你；需要时可再次调用 collect_results。\n\n"
                        )
                        break
                    await manager.wait_one(timeout=0.5)
            finally:
                _clear_tool_state(turn_id)
                # 本次等待自己吸收掉的延后打断请求要一并清掉：留着会让下一个工具误以为自己该让出
                _claim_pending_interrupt(turn_id)
        if _only is not None:
            h = manager.get(_only)
            if h is None:
                known = ", ".join(f"#{i}" for i in manager.agent_ids()) or "(none)"
                return f"No subagent with id {_only}. Known subagents: {known}."
            if h.status == "running":
                return f"subagent#{_only} is still running — {h.task_title or '(untitled)'}."
            # v36 (plan-321-1600 M3): 主代理已读到该结果 → claim，避免随后又收到重复推送
            manager.claim_notification(_only)
            results = [r for r in manager.results() if r["agent_id"] == _only]
        else:
            results = manager.results()
            # v36: 全量读取同样视为已读（对齐 ZCode claim 语义）
            for _r in results:
                manager.claim_notification(int(_r["agent_id"]))
        if not results:
            pending = manager.pending_count()
            return f"{_wait_note}No subagents have finished yet ({pending} still running)."
        blocks: list[str] = []
        for r in results:
            status = "completed" if r["status"] == "done" else "failed"
            head = f"### subagent#{r['agent_id']} [{status}]"
            if r.get("title"):
                head += f" — {r['title']}"
            body_lines = [head]
            detail = (r.get("findings") or r.get("summary") or r.get("error") or "").strip()
            if detail:
                # plan-156-620：结论不再静默截到 4000 字——单个子代理按 report_offset 分段给全文，
                # 汇总多个时先给开头，并写明总字数与续读方式（与子会话同一口径）
                from app.orchestration.subsession_report import (
                    REPORT_PAGE_CHARS, REPORT_PREVIEW_CHARS, page_of, paging_hint, tool_ref,
                )
                _offset = _report_offset if _only is not None else 0
                _page = page_of(detail, _offset, REPORT_PAGE_CHARS if _only is not None else REPORT_PREVIEW_CHARS)
                body_lines.append(_page.text + paging_hint(
                    _page, tool_ref("collect_results", agent_id=r["agent_id"], report_offset="{offset}"),
                ))
            if r.get("error") and r.get("summary"):
                body_lines.append(f"Error: {r['error'][:300]}")
            if r.get("files_touched"):
                body_lines.append("Files touched: " + ", ".join(r["files_touched"][:30]))
            # v36 (plan-321-1600 M1): 汇报闭环信息（验证/验收）透传给主代理
            if r.get("verification"):
                body_lines.append("Verification:\n" + "\n".join(f"- {x}" for x in r["verification"][:10]))
            if r.get("acceptance"):
                body_lines.append("Acceptance:\n" + "\n".join(f"- {x}" for x in r["acceptance"][:10]))
            if r.get("risks"):
                body_lines.append("Risks/blockers:\n" + "\n".join(f"- {x}" for x in r["risks"][:10]))
            blocks.append("\n".join(body_lines))
        tail = ""
        if manager.pending_count():
            tail = f"\n\n({manager.pending_count()} subagents still running — call collect_results(wait=true) to block until they finish)"
        return _wait_note + "Subagent results:\n\n" + "\n\n".join(blocks) + tail

    if tool_name == "subagent_inspect":
        # plan-248-1258 M6: 主代理按需查看子代理上下文/轨迹/结果
        manager = subagent_context.get("manager")
        if manager is None:
            return "No subagents were spawned in this turn."
        try:
            aid = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return "Error: agent_id is required (integer)."
        section = str(args.get("section") or "all").lower()
        if section not in ("all", "context", "transcript", "result"):
            section = "all"
        data = manager.inspect(aid, section)
        if data is None:
            known = ", ".join(f"#{i}" for i in manager.agent_ids()) or "(none)"
            return f"No subagent with id {aid}. Known subagents: {known}."
        _full_payload = _json.dumps(data, ensure_ascii=False, indent=2)
        _payload = _full_payload[:_INSPECT_PAYLOAD_CHARS]
        if len(_full_payload) > _INSPECT_PAYLOAD_CHARS:
            from app.orchestration.output_paging import tool_ref as _tool_ref
            # plan-156-620：不再静默截断——写明截断与取全文的方式（结论走 collect_results 分段读取）
            _payload += (
                f"\n…（诊断信息共 {len(_full_payload)} 字，以上为前 {_INSPECT_PAYLOAD_CHARS} 字；"
                f"子代理结论全文请用 {_tool_ref('collect_results', agent_id=aid, report_offset=0)} 分段读取）"
            )
        # plan-64-291: running 状态追加等待期纪律尾注——上一轮实测正是在此触点失守
        # （inspect 看到 running 后转而自己动手做子代理的任务，最终重复劳动、双倍消耗）。
        if str(data.get("status")) == "running":
            _payload += (
                "\n\n[等待期纪律] 该子代理仍在运行——不要自己动手做它的任务（重复劳动会双倍"
                "消耗 token 与时间）；需要它的结论请调用 collect_results(wait=true) 阻塞等待；"
                "subagent_inspect 只用于查看上下文/轨迹，不要用它反复轮询。"
            )
        return "Subagent inspection:\n" + _payload

    if tool_name == "cancel_subagent":
        # v36 (plan-321-1600 M3): 取消单个仍在运行的子代理（对齐 ZCode TaskCancel）。
        manager = subagent_context.get("manager")
        if manager is None:
            return "No subagents were spawned in this turn."
        try:
            _cid = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return "Error: agent_id is required (integer)."
        state = await manager.cancel(_cid)
        if state == "not_found":
            known = ", ".join(f"#{i}" for i in manager.agent_ids()) or "(none)"
            return f"No subagent with id {_cid}. Known subagents: {known}."
        if state == "not_running":
            return f"subagent#{_cid} is not running (already finished or cancelled) — nothing to cancel."
        return f"subagent#{_cid} cancelled."

    if tool_name == "send_to_subagent":
        # plan-330-1648 M4: 主代理 → 运行中子代理的指令通道（改写任务，不打断执行）
        manager = subagent_context.get("manager")
        if manager is None:
            return "No subagents were spawned in this turn."
        try:
            _said = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return "Error: agent_id is required (integer)."
        _smsg = str(args.get("message") or "").strip()
        if not _smsg:
            return "Error: message is required."
        # plan-549-2281：若该子代理正在等一条提问答复，先直接唤醒它——旧实现先 push 再
        # 靠子代理侧自取 inbox，答复与「普通指令」语义混在一起，容易错配或多等一轮。
        from app.orchestration import subagent_comm

        # plan-560-2311 B4：答复前先核验子代理**仍在运行**。旧实现只看登记表就 resolve，
        # 子代理已结束（或登记已过期）时会给模型回一句「it resumes immediately」的假成功，
        # 指令与答复双双丢失。终态清理已在 subagent._after_terminal 补上，这里是数据面兜底。
        _target = manager.get(_said)
        _target_alive = (
            _target is not None and _target.status == "running"
            and _target.task is not None and not _target.task.done()
        )
        _qid = subagent_comm.pending_question_of(session_id, _said)
        if _qid and not _target_alive:
            # 残留登记：丢弃它，下面走 push_agent_input 给出明确的「不在运行」失败
            subagent_comm.discard_question(_qid)
            _qid = None
        if _qid and subagent_comm.resolve_question(_qid, _smsg):
            return (
                f"Reply delivered to subagent#{_said} (question_id={_qid}); it resumes immediately "
                "with this answer and continues its work."
            )
        if not manager.push_agent_input(_said, _smsg):
            _known = ", ".join(f"#{i}" for i in manager.agent_ids()) or "(none)"
            return (
                f"subagent#{_said} is not running (finished or cancelled) — cannot deliver the "
                f"instruction. Known subagents: {_known}."
            )
        return (
            f"Instruction delivered to subagent#{_said}; it is injected into its context before "
            "its next model call."
        )

    if tool_name == "resume_subagent":
        # plan-91-394: 续跑中断/暂停的子代理——保留已完成进度，不新建子代理从头来
        try:
            _rid = int(args.get("agent_id"))
        except (TypeError, ValueError):
            return "Error: agent_id is required (integer)."
        from app.orchestration.subagent import resume_subagent as _resume_subagent
        # 只读/计划模式下不得借续跑恢复可写子代理（沿用原运行模式）
        # plan-560-2311 C1：改走判定档——原实现连 normalize_execution_mode 都没调，
        # 自定义 kind=readonly/plan 模式因此可直接续跑恢复出可写子代理。
        _rstate, _rdetail = await _resume_subagent(
            session_id, _rid, turn_id=turn_id,
            cancel_event=subagent_context.get("cancel_event"),
            readonly_only=_mode_kind(subagent_context.get("permission_mode")) in ("readonly", "plan"),
        )
        if _rstate == "resumed":
            return (
                f"subagent#{_rid} resumed from its checkpoint ({_rdetail}). Completed steps are kept — "
                "do NOT re-dispatch it. Its completion report is pushed back to you automatically; "
                "use collect_results(wait=true) if you need to block for it."
            )
        if _rstate == "already_running":
            return f"subagent#{_rid} is already running — nothing to resume."
        if _rstate == "not_found":
            return f"No resumable subagent with id {_rid} in this session."
        return f"subagent#{_rid} cannot be resumed: {_rdetail}"

    if tool_name == "spawn_subagent":
        manager = subagent_context.get("manager")
        project = subagent_context.get("project")
        session = subagent_context.get("session")
        cancel_event = subagent_context.get("cancel_event")
        main_task_id = subagent_context.get("main_task_id")
        # v12: 兜底解析主任务（步骤载体）——context 缺失时按 turn 查最早的任务记录，
        # 保证子代理任务 parent_task_id 始终指向真实的主任务（而非退化用 turn_id）。
        if not main_task_id:
            from app.persistence.models.task import Task
            mt_res = await db.execute(
                select(Task).where(
                    Task.session_id == session_id,
                    Task.turn_id == turn_id,
                ).order_by(Task.id.asc()).limit(1)
            )
            mt = mt_res.scalars().first()
            main_task_id = mt.id if mt else None
        if manager is None or project is None:
            return "Error: subagent manager unavailable"

        task_title = str(args.get("task_title", ""))[:200]
        task_desc = str(args.get("task_description", ""))[:4000]
        acceptance = str(args.get("acceptance_criteria", ""))[:500]
        # plan-549-2281：执行模式显式化。子代理只有两种模式——readonly（只读调研/审阅）
        # 与 agent（可写可执行）。`mode` 是正式入口，`explore` 保留为兼容别名
        # （旧会话历史与 ta3 侧 subagent_type 还原都还产它）。
        # 目标：让「实现」也能被合法委派，消除旧提示词「implementation is never delegated」
        # 与实际工具面（explore=false 可写）的自相矛盾。
        # 解析抽到 resolve_subagent_readonly（守护测试直接测它，不再手工复刻等价实现）。
        # plan-560-2311 C1：`_pm` 取**判定档**（按 kind 归一）而非原始模式名——此前拿原始名
        # 与 ("readonly","plan") 做等值比较，自定义 kind=readonly/plan 模式两者都不等，
        # 护栏被整体跳过。会话里存的模式名不变，只在判定处归一。
        _pm = _mode_kind(subagent_context.get("permission_mode"))
        _is_operator = str(args.get("purpose") or "").strip().lower() == "operator"
        # plan-560-2311 C3：设置里关掉操控子代理后，spawn 必须真的拒绝，而不是照旧派发并
        # 套用最低思考档（此前该开关只在提示词处读取，运行时毫无效果）。
        # 放在只读拦截**之前**：否则只读会话会先报「只读模式不能派 operator」，
        # 掩盖真实原因（功能已关闭），模型会误以为切模式就能解决。
        if _is_operator and getattr(settings, "operator_subagent_enabled", True) is False:
            return (
                "Error: 操控子代理已在设置中关闭（设置 → 子代理 → 操控子代理），无法派发 purpose=operator。"
                "请自己完成该界面操作，或让用户在设置里开启后再派发。"
            )
        if _pm in ("readonly", "plan"):
            # v36 (plan-321-1600 R1) + plan-330-1648 M1: 主代理处于只读/计划模式时，只允许派发
            # 只读子代理——主代理侧的**模式语义护栏**（该模式下不产生业务写入）。
            # 操控子代理必须可写（点按/输入/下载），只读会话里派它只会得到一个做不了事
            # 的只读子代理——显式拒绝并给出出路，好过静默降级。
            if _is_operator:
                return (
                    "Error: 当前会话为只读 / 计划模式，不能派发操控子代理（它需要可写权限操作界面）。"
                    "请自己完成该操作；确需界面自动化时请用户切换到智能体模式。"
                )
        explore = resolve_subagent_readonly(
            args.get("mode"), bool(args.get("explore", False)), _pm,
        )
        if _is_operator:
            # plan-549-2281 修复：operator 一律可写（要调界面/浏览器工具），不能沿用
            # explore 档案与 readonly 模式——此前 explore+operator 组合下档案按 explore
            # 过闸、模式却保持只读，既绕过了 general 类型的停用闸门，也与「可写」语义矛盾。
            explore = False
        # plan-330-1648 M1: 子代理执行模式**按类型锁定**（readonly / agent），显式下发给
        # run_agent_loop——子代理不继承会话的执行模式，也永远拿不到计划模式。
        _sub_mode = "readonly" if explore else "agent"
        if not task_title:
            return "Error: task_title is required"

        # v36 (plan-321-1600 M1): 防无用功——同一会话内同名子任务已存在时提示拦截。
        # 语义为“提醒”而非硬失败：不抛异常、不终结 turn，主代理可据提醒改为
        # collect_results 取结果，或换一个更具体的 task_title（合法并行拆分场景不受影响）。
        _dup = manager.duplicate_of(task_title)
        if _dup is not None:
            return (
                f"Error: a subagent for this exact subtask is already dispatched "
                f"(#{_dup.agent_id} [{_dup.status}] — {task_title}). "
                "Do NOT dispatch duplicates: call collect_results (wait=true) to gather its result, "
                "or use a more specific task_title if this is genuinely a different subtask."
            )

        # v21: 子代理类型开关门禁 —— 设置里停用（is_active=false）的子代理类型不可再被 spawn。
        # 修复：此前 SubagentProfile.is_active 从未被编排层读取，关闭设置对 AI 毫无效果（用户反馈 Bug）。
        # 语义：explore=true → 匹配 explore 类型；否则 → general 类型；类型不存在时按系统默认放行。
        # plan-330-1648 M2: 类型档案在下方被模型/思考深度解析复用，先做默认绑定（异常路径也保证有值）。
        _profile_name = "explore" if explore else "general"
        _profile = None
        try:
            from app.persistence.models.subagent_profile import SubagentProfile
            _pres = await db.execute(
                select(SubagentProfile).where(SubagentProfile.name == _profile_name)
            )
            _profile = _pres.scalars().first()
            if _profile is not None and not _profile.is_active:
                return (
                    f"Error: 子代理类型「{_profile_name}」已在设置中停用，无法创建子代理。"
                    "请在当前主窗口直接完成该任务，不要再次调用 spawn_subagent，"
                    "也不要尝试用其他子代理类型绕过。"
                )
        except Exception:
            logger.warning("[agent] 子代理类型开关检查失败(非阻塞)", exc_info=True)

        # plan-474-2105 / plan-20261022-subagent-reject-fast：只保留「单会话并发」一道闸门——
        # 本轮不限累计总量（每轮派发总量上限已废弃）。并发已满时**立即拒绝**：
        # 原实现先排队等 60 秒再拒绝，主代理在等待期拿不到任何进度，用户只看到一张卡住的
        # 卡片，最终仍然是失败——等待没有收益，只是把「并发满」延后一分钟才告诉主代理。
        # 拒绝路径不创建 Agent / Task，也不占用任何名额。
        _max_conc = max(1, min(30, int(getattr(settings, "max_concurrent_subagents", 4) or 4)))
        _running = manager.pending_count()
        if _running >= _max_conc:
            logger.info(
                "[subagent.gate] session=%s running=%s/%s result=rejected tool=%s",
                session_id, _running, _max_conc, tool_name,
            )
            return _subagent_concurrency_rejection(
                _running, _max_conc, _running_subagents_note(manager),
            )
        logger.info(
            "[subagent.gate] session=%s running=%s/%s result=pass tool=%s",
            session_id, _running, _max_conc, tool_name,
        )

        # 创建子代理 Agent + Task（写引擎单写线程一次事务：Agent 创建 → Task 创建 →
        # 立即提交；返回 agent_id/task_id 供后续 thread_id=sub_agent.id 等 id 链使用）。
        from app.core.enums import AgentKind
        from app.persistence.database import run_write_locked

        # v36 修复：子代理模型 id 只解析一次（会话级模型优先，回落主代理绑定模型），
        # 落库行与内存展示对象共用同一个值——此前只有落库行写了 model_id，
        # 展示对象漏设，导致子代理 run_agent_loop(model_id=None) → provider 解析失败。
        # plan-330-1648 M2: 模型/思考深度改为「设置中心子代理类型优先 → 会话 → 主代理」；
        # 设置里指定的模型若已删除/停用，回落会话模型并留 warning（不硬失败）。
        _spawn_model_id = subagent_context.get("model_id") or agent_model_id
        # plan-156-619 S4e：设置中心改为「只配模型 + 思考深度」，工具范围与执行模式由主代理
        # 在创建时按能力矩阵决定。因此这里的优先级翻转为：**全局设置 → 类型档案 → 会话**。
        # 0 / 空 = 未设置，继续向下回落（旧库里的 profile 配置仍作为兜底，不丢用户历史设置）。
        _global_model_id = int(getattr(settings, "subagent_model_id", 0) or 0)
        _profile_model_id = _global_model_id or (
            getattr(_profile, "model_id", None) if _profile is not None else None
        )
        if _profile_model_id:
            _mrow = None
            try:
                from app.persistence.models.model_reg import Model as _Model
                _mres = await db.execute(select(_Model).where(_Model.id == int(_profile_model_id)))
                _mrow = _mres.scalars().first()
            except Exception:
                logger.warning("[agent] 子代理类型模型校验查询失败(按回落处理)", exc_info=True)
            if _mrow is not None and bool(getattr(_mrow, "is_active", True)):
                _spawn_model_id = int(_profile_model_id)
            else:
                logger.warning(
                    "[agent] 子代理类型 %s 配置的模型 #%s 不存在或已停用，回落模型 #%s",
                    _profile_name, _profile_model_id, _spawn_model_id,
                )
        # None = run_agent_loop 内回落 settings.agent_reasoning_effort（全局默认档位）
        # plan-156-619 S4e：同一优先级翻转——全局设置（思考深度）> 类型档案 > 本 turn 档位。
        _spawn_effort = (
            (getattr(settings, "subagent_reasoning_effort", "") or "")
            or (getattr(_profile, "reasoning_effort", None) if _profile is not None else None)
            or subagent_context.get("reasoning_effort")
            or None
        )
        # plan-549-2281：操控子代理（原 purpose=operator 的子会话能力迁到子代理）。
        # 界面操作每步都是一次模型往返，几乎不需要深度推理，却是延迟大户——统一压到
        # 设置项 operator_subagent_effort 指定的档位（默认当前模型支持的最低非关闭档）。
        # 档位解析抽到 resolve_operator_effort（含「不取列表首元素、按档位序取最低」修复）。
        if _is_operator:
            try:
                _levels: list[str] = []
                if _spawn_model_id:
                    try:
                        from app.persistence.models.model_reg import Model as _ModelRow
                        _mrow = await db.get(_ModelRow, int(_spawn_model_id))
                        _levels = [str(x) for x in (getattr(_mrow, "reasoning_efforts", None) or [])]
                    except Exception:  # noqa: BLE001 — 读不到档位按 low 处理
                        _levels = []
                _want_effort = resolve_operator_effort(
                    str(getattr(settings, "operator_subagent_effort", "lowest") or "lowest"),
                    _levels,
                )
                if _want_effort:
                    _spawn_effort = _want_effort
            except Exception:  # noqa: BLE001 — 档位解析失败不改默认行为
                logger.debug("[agent] 操控子代理档位解析失败(用默认)", exc_info=True)
        logger.info(
            "[agent] 子代理派发解析 type=%s model=#%s effort=%s (profile_model=%s session_model=%s)",
            _profile_name, _spawn_model_id, _spawn_effort, _profile_model_id,
            subagent_context.get("model_id"),
        )
        # plan-330-1648 M7: 子代理 token 预算（0/负数 = 不限）
        _sub_budget = int(getattr(settings, "subagent_token_budget", 0) or 0) or None

        # plan-568-2333：子代理工具面必须在**写库之前**定稿并校验。排在 Agent/Task 创建之后
        # 就只能在事后告警——agent 行、task 行、并发名额都已占用，用户看到的是「跑着但永远没结果」。
        # 两道校验都是 fail-closed：解析不出只读工具集、或类型白名单与可用工具无交集，直接拒绝派发。
        from app.orchestration.tools.registry import tool_registry
        sub_tools = tool_registry.all_schemas()

        # v20: 探索子代理只读工具（探索 = 调研/阅读/分析，不产生写盘副作用）
        if explore:
            from app.orchestration.subagent_tools import EXPLORE_TOOLS
            explore_schemas = tool_registry.all_schemas(EXPLORE_TOOLS)
            if not explore_schemas:
                # 注册表部分模块加载失败 / 工具名漂移时**不能**回落到全量工具：那会把 fs_write、
                # terminal_exec 一并交给只读子代理，写权限只剩 executor 一层兜底（fail-open）。
                logger.warning(
                    "[agent] 只读子代理工具集解析为空（EXPLORE_TOOLS 无可注册项），拒绝派发 type=%s",
                    _profile_name,
                )
                return (
                    "Error: 无法解析只读子代理的工具集（工具注册表异常），已拒绝以只读模式派发。"
                    "请改用 mode=\"agent\" 派发，或由你在主会话内直接完成该调研。"
                )
            sub_tools = explore_schemas

        # v3.0 (plan-88): 子代理类型工具白名单——profile.tools_whitelist 非空时
        # 在既有范围（探索只读 / 全量）上按名单收窄；勾选=允许，留空=全量（旧语义）。
        from app.orchestration.subagent_tools import filter_tool_schemas
        try:
            _whitelist = list(_profile.tools_whitelist) if (_profile is not None and _profile.tools_whitelist) else None
        except Exception:
            _whitelist = None
        if _whitelist:
            sub_tools = filter_tool_schemas(sub_tools, _whitelist)
            if not sub_tools:
                # plan-568-2333：与可用工具集无交集时不再继续派发。此前只写一条 warning，
                # 子代理最终只剩 report_to_leader，读不了文件也执行不了命令，却照样占用并发名额。
                return (
                    f"Error: 子代理类型「{_profile_name}」勾选的工具与子代理可用工具无交集，无法派发。"
                    "请在设置 → 子代理里检查该类型的工具白名单，或取消勾选（留空表示使用全部可用工具）。"
                )
        # plan-330-1648 M4: 子代理的“与主代理通信”通道始终可用（不受类型工具白名单影响）
        # ——否则用户勾选工具时会无意中切断子代理向主代理上报/求助的能力。
        try:
            from app.orchestration.subagent_tools import SUBAGENT_OWN_TOOLS
            _have = {s.get("function", {}).get("name") for s in sub_tools}
            for _own in SUBAGENT_OWN_TOOLS:
                if _own["function"]["name"] not in _have:
                    sub_tools.append(_own)
        except Exception:
            logger.debug("[agent] 子代理通信工具追加失败(非阻塞)", exc_info=True)

        # plan-474-2105：子代理不得直接问用户，也不得再派生下级执行体（套娃 / 绕过并发上限）。
        # 放在白名单与自有工具追加**之后**统一剥离：白名单是老配置，可能仍含这些工具名。
        # plan-549-2281：剥离范围已含工作流工具（子代理不进计划模式、不使用工作流）。
        try:
            from app.orchestration.subagent_tools import strip_for_subagent
            sub_tools = strip_for_subagent(sub_tools)
        except Exception:
            logger.warning("[agent] 子代理工具边界剥离失败(按原样继续)", exc_info=True)

        def _persist_spawn(s):
            from app.persistence.models.agent import Agent as _Agent
            from app.persistence.models.task import Task as _Task
            # plan-565-2323 G9：派发即写 status="running"。此前不赋值、落到模型默认 "idle"，
            # agents 行的状态对「子代理是否还在跑」失去意义，启动 reconcile 也无从按 agent 维度收尾。
            sa = _Agent(kind=AgentKind.SUB.value, name=task_title[:40],
                        model_id=_spawn_model_id,
                        session_id=session_id,
                        turn_id=turn_id, parent_agent_id=parent_agent_id,
                        status="running")
            s.add(sa)
            s.flush()
            aid = sa.id
            task_row = _Task(
                session_id=session_id, turn_id=turn_id,
                title=task_title, description=task_desc,
                acceptance_criteria=acceptance, agent_id=aid,
                parent_task_id=main_task_id or turn_id, priority=1,
                kind="step", status="pending",
            )
            s.add(task_row)
            s.flush()
            tid = task_row.id
            s.commit()
            return aid, tid

        _agent_id, task_id = await run_write_locked(_persist_spawn, label="subagent.spawn")
        from app.persistence.models.agent import Agent
        # v36 修复：展示对象补上 model_id（与落库行一致）——此前漏设，
        # subagent.spawn 里的 getattr(agent, "model_id") 恒为 None，
        # 子代理固定报「模型不可用(None)」无法执行（用户反馈：子代理调用必失败）。
        sub_agent = Agent(kind=AgentKind.SUB.value, id=_agent_id,
                          name=task_title[:40], model_id=_spawn_model_id)  # 展示对象（id 已定，供 thread_id 链）
        task = await task_service.get_task(db, task_id)  # async 只读对象（后续仅读属性）
        # v10: 子任务创建后立即提交（写引擎内已提交）并广播
        from app.orchestration.agent_events import broadcast
        try:
            await broadcast(session_id, {
                "event": "task.updated",
                "payload": {"task_id": task_id, "status": "pending", "note": (task_desc or "")[:200]},
            })
        except Exception:
            logger.warning("[agent] 子任务创建提交/广播失败(非阻塞)", exc_info=True)

        # v23: 主 AI 下发的任务消息落库到子代理线程（thread_id=sub_agent.id）并广播，
        # 右侧子代理面板首条即"任务从哪来"（此前 handoff 仅注入上下文 developer 消息，
        # 不落库，子代理预览看不到主 AI 下发的任务内容）。
        try:
            await message_service.create_message(
                db, session_id=session_id, turn_id=turn_id, thread_id=sub_agent.id,
                sender_type=SenderType.USER.value, msg_type=MsgType.TEXT.value,
                content={"text": (f"{task_title}\n\n{task_desc}").strip()},
            )
        except Exception:
            logger.warning("[agent] 子代理任务消息落库失败(非阻塞)", exc_info=True)

        # 构建子代理上下文（v19: 注入用户原始请求 + 主会话摘要，修复继承断裂）
        from app.orchestration.context_manager import build_subagent_context
        _orig_req = ""
        try:
            from app.persistence.models.message import Message
            _ures = await db.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.sender_type == "user",
                    Message.deleted == False,  # noqa: E712 问题14: 排除已回滚软删
                ).order_by(Message.id.desc()).limit(1)
            )
            _um = _ures.scalars().first()
            if _um and isinstance(_um.content, dict):
                _orig_req = str(_um.content.get("text") or "")
        except Exception:
            _orig_req = ""
        bundle = await build_subagent_context(
            db, agent=sub_agent, session=session, project=project, task=task,
            handoff_summary=task_desc,
            original_request=_orig_req,
        )
        # plan-568-2333：工具面已在派发前（写库之前）定稿并校验，此处不再重复计算。
        # plan-549-2281 双保险：工作流工具不在 tool_registry 里，子代理原本就拿不到；
        # 但「类型工具白名单」「自定义权限模式」都可能把名字塞回来，这里再按名字硬剥一遍。
        _wf_names = set(WORKFLOW_TOOL_NAMES)
        sub_tools = [
            s for s in sub_tools
            if (s.get("function") or {}).get("name") not in _wf_names
        ]

        from app.orchestration.subagent import get_subagent_manager
        mgr = manager or get_subagent_manager(session_id)
        # v20: explore 子代理旧版在这里同步等待拿结论；plan-541-2264 删除该分支后，
        # 只读与可写子代理走**同一条**后台派发路径（派发即返回，结论由完成报告推回），
        # 因此 agent.started 广播、pending 计数等公共动作只在下面这一处执行。
        sub_agent_id = mgr.spawn(
            db, agent=sub_agent, turn_id=turn_id, task=task,
            handoff_summary=task_desc, context_bundle=bundle,
            tool_schemas=sub_tools, workspace=workspace,
            cancel_event=cancel_event,
            token_budget=_sub_budget,
            task_title=task_title, task_description=task_desc,
            context_snapshot=_subagent_context_snapshot(subagent_context, _orig_req),
            reasoning_effort=_spawn_effort,
            subagent_mode=_sub_mode,
        )
        await broadcast(session.id, {
            "event": "agent.started",
            "payload": {"agent_id": sub_agent_id, "kind": "sub",
                        "name": sub_agent.name, "turn_id": turn_id},
        })
        # v43: 派发即同步前端子代理计数——此前 subagent.pending 只在完成时广播，
        # 运行期前端计数为 0（会话卡不转圈、兜底等待行不出现）。
        try:
            await broadcast(session.id, {
                "event": "subagent.pending",
                "payload": {"session_id": session.id, "pending": int(mgr.pending_count())},
            })
        except Exception:
            logger.debug("[agent] 子代理 pending 广播失败(非阻塞)", exc_info=True)
        return (
            f"Subagent #{sub_agent_id} dispatched in the background for task: {task_title}. "
            f"[运行参数] mode={_sub_mode} model=#{_spawn_model_id} "
            f"effort={_spawn_effort or 'default'}. "
            # plan-549-2281（复盘）：并发额度必须**每次派发都回报**——
            # 此前上限与占用只在「已满被拒」时第一次出现，模型派发前毫无信息，
            # 只能按「一次发多条」的建议一路派到撞墙为止。这里的数字是刚派完的实时值。
            f"[并发额度] 上限 {_max_conc}｜当前在跑 {_running + 1}｜剩余 {max(0, _max_conc - _running - 1)}。"
            "剩余为 0 时不要再派发，先 collect_results(wait=true) 收取结果或自己接着做。"
            "It runs in an isolated context; its completion report is pushed back to you automatically. "
            "## Waiting discipline — while it runs: do NOT do its task yourself (duplicating a running "
            "subagent's work costs tokens and time twice over); take only NON-overlapping work; if none "
            "is left, call collect_results(wait=true) to block until it finishes, or end your turn."
        )

    return "Error: unknown subagent tool"


def _question_waiter_alive(manager, agent_id: int):
    """构造「子代理仍在正常等待」的存活判定（plan-565-2323 G16）。

    提问等待不设总时长上限，靠这条判定兜底：会话执行已结束、管理器已销毁、或该子代理已进入
    终态时必须立刻停等，否则子代理会挂成僵尸（进程还在、永远不产出）。

    为什么逐项看这几条：
      · manager 仍在 `_managers` 注册表里 → 会话级管理器没被销毁（`subagent.cleanup` 会摘除并
        同时 `clear_session`，两道防线互不依赖）；
      · `owner_active` → 主代理 turn 仍存活（turn 收尾时 `pause_for_turn_end` 置 False 并取消
        处于等待态的子代理任务）；
      · handle 仍是 running 且任务未 done → 自己没被终止（用户「终止」→ `cancel()` →
        `task.cancel()`，取消会直接穿透 `wait_reply`，这里只是数据面兜底）。

    任何一步读不到状态都返回 True（按存活处理）：读不到状态不能成为误杀子代理的理由。

    【等待靠什么结束】三条确定性信号，任一成立即立刻停等（绝不靠时间）：
      1. 子代理被终止（用户「终止」/ turn 收尾）→ `task.cancel()` → `CancelledError` 从
         `wait_reply` 直接穿透，本判定根本没机会参与；
      2. 会话执行已结束 / 管理器已销毁 → `peek_subagent_manager` 取不到、或 `owner_active`
         为假 → 本判定返回 False，`wait_reply` 清场后返回 None（子代理按最佳判断继续）；
      3. 该子代理已不在 running（被别的路径落了终态）→ 同上返回 False。
    **不要**在此基础上再加时间上限：用户口径是「等用户回答不设上限」，加了就退回到
    「看门狗先杀子代理」的老缺陷（plan-565-2323 G16 的成因）。
    """
    from app.orchestration.subagent import peek_subagent_manager

    try:
        session_id = int(getattr(manager, "session_id", 0) or 0)
    except (TypeError, ValueError):
        return lambda: True
    if session_id <= 0:
        # 拿不到会话 id（manager 缺失 / 测试桩）：不做存活判定，靠取消传播兜底。
        # 不能在这里返回 False——那等于把「读不到状态」当成「会话已死」，会误杀等待。
        return lambda: True
    aid = int(agent_id)

    def _alive() -> bool:
        try:
            mgr = peek_subagent_manager(session_id)
            if mgr is None:
                return False
            if not bool(getattr(mgr, "owner_active", True)):
                return False
            handle = mgr.get(aid)
            if handle is None:
                return False
            if str(getattr(handle, "status", "running")) != "running":
                return False
            task = getattr(handle, "task", None)
            if isinstance(task, asyncio.Task) and task.done():
                return False
            return True
        except Exception:  # noqa: BLE001 — 判定失败按存活处理，不能误杀
            return True

    return _alive


async def _run_report_to_leader(comm: dict, args: dict, *, session_id: int, turn_id: int,
                                thread_id: int | None, agent_id: int, agent_name: str,
                                db: AsyncSession) -> str:
    """report_to_leader 工具实现（plan-330-1648 M4）：子代理 → 主代理上报/提问。

    - 落子代理线程（面板可见）+ 进主代理注入队列（下一次 LLM 调用前可见）；
    - wait=true（提问并等答复）对所有子代理可用：plan-541-2264 起子代理一律后台执行，
      主代理不会阻塞在派发上，因此不存在“同步子代理等待会死锁”的情况；
    - 等待答复**没有总时长上限**（plan-565-2323 G16），未收到答复才返回“按最佳判断继续”。

    plan-549-2281：`kind=question` 改走 mailbox P0（`subagent_comm.publish_question`）——
    子会话下线后，它是唯一的「能打断主代理次要等待」的求助通道。旧实现只写
    `_pending_inputs`，主代理正在跑前台命令或卡在 wait 轮询时读不到，子代理白等到超时。

    plan-565-2323 G16：等待前把 `handle.waiting` 置真，让停滞看门狗把这段时间当作**正常等待**
    （与瞬时故障保活同口径）。旧实现只靠 `wait_reply` 的 600s 超时，而看门狗阈值也是 600s，
    于是看门狗必然先 cancel 掉驱动任务，子代理落 failed「长时间无活动」——「没等到答复就按
    最佳判断继续」的分支永远走不到。现在的兜底不是时间，是 `alive` 判定 + 取消传播：
    子代理被终止、turn 收尾、管理器销毁都会让等待立刻结束（见 `subagent_comm.wait_reply`）。
    """
    manager = comm.get("manager")
    message = str(args.get("message") or "").strip()
    if not message:
        return "Error: message is required."
    kind = str(args.get("kind") or "progress").lower()
    if kind not in ("progress", "question"):
        kind = "progress"
    wait = bool(args.get("wait", False)) and kind == "question"
    label = "子代理提问" if kind == "question" else "子代理进度上报"
    # 1) 落子代理线程（面板可见，与普通文本同渲染路径）
    try:
        await message_service.create_message(
            db, session_id=session_id, turn_id=turn_id, thread_id=thread_id,
            sender_type=SenderType.AGENT.value, sender_id=agent_id,
            msg_type=MsgType.TEXT.value,
            content={"text": f"【{label}】{message}", "agent_name": agent_name,
                     "subagent_report": kind},
        )
    except Exception:
        logger.debug("[agent] 子代理上报落库失败(非阻塞)", exc_info=True)

    if kind == "question":
        # 2a) 提问：P0 投递（能打断主代理等待）+ 阻塞等答复
        from app.orchestration import subagent_comm

        task_title = str(comm.get("task_title") or "")
        qid = await subagent_comm.publish_question(
            session_id=session_id, agent_id=agent_id,
            task_title=task_title, message=message,
            # plan-20261026-notice-turn：提问行归属主代理轮次，撤回该轮时一并软删
            turn_id=turn_id,
        )
        reply = None
        if wait:
            # plan-565-2323 G16：等待前置真 handle.waiting——停滞看门狗据此走「正常等待」
            # 豁免分支（与瞬时故障保活同口径）。`finally` 复位覆盖全部出口：
            # 收到答复、取消（CancelledError 穿透）、会话结束（alive 判定返回 None）、异常。
            # 不复位的后果：子代理此后每次模型调用之间都会被看门狗当成「等待中」无条件
            # touch，真正的停滞再也检测不到（等于把看门狗关掉）。
            #
            # 【为什么缺省没有上限】用户口径：主代理也可能不确定而去问用户，用户可能很久才回答，
            # 此时应当继续等。只有**模型自己显式传了 timeout_s** 才作为上限生效（那是它的主动
            # 选择，也保住既有「限时提问」语义）；不传 = 一直等到收到答复或被兜底条件打断
            # （终止 / turn 收尾 / 管理器销毁 / 被新提问取代），见 `subagent_comm.wait_reply`。
            _limit = float(args.get("timeout_s") or 0) or None
            handle = manager.get(agent_id) if manager is not None else None
            if handle is not None:
                handle.waiting = True
            try:
                reply = await subagent_comm.wait_reply(
                    qid, _limit, alive=_question_waiter_alive(manager, agent_id),
                )
            finally:
                if handle is not None:
                    handle.waiting = False
        if not wait:
            # plan-549-2281 修复：不等答复的提问不能留在登记表里——主代理答复时
            # resolve_question 会 set 一个没有等待者的 event，答复凭空丢失。
            # 旧 subsession_tool_runner 在此显式 discard_question，迁移时丢了这道防线；
            # 立刻作废后，主代理的 send_to_subagent 会走 push_agent_input 注入子代理上下文。
            subagent_comm.discard_question(qid)
            return (
                f"Question delivered to the main agent (question_id={qid}). "
                "Keep working on anything that does not depend on the answer; do NOT resend it. "
                "If the main agent replies, the reply is injected into your context before your next model call."
            )
        if reply is None:
            # 走到这里只有两种可能：模型自己限了时并到点，或等待被兜底条件打断
            # （子代理被终止 / turn 收尾 / 管理器销毁 / 提问被新提问取代）。
            _why = (f"within {int(_limit)}s " if _limit else "(the question was withdrawn or the wait was interrupted) ")
            return (
                f"No reply from the main agent {_why}. Continue with your best "
                "judgment and record the open question in your final report."
            )
        return f"Reply from the main agent:\n{reply}"

    # 2b) 进度上报：异步，不打断主代理
    if manager is not None:
        manager.notify_leader(agent_id, message, kind)
    return "Reported to the main agent. Continue working — do NOT wait for an acknowledgement."
