"""模型调用失败恢复引擎（plan-20261020-retry-cred-perf §1）。

为什么单独成模块：agent_loop 里的重试代码原本散落在「响应异常」「函数调用 400」
「上下文溢出」「瞬时故障」四条路径上，各自维护等待、换 Key、记账与广播，行为分叉
（有的不做健康检查、有的只给一次机会、有的重试发送的是未压缩的原始消息）。

现在收敛成**一处**：以「轮」为单位重试。

- 一轮内按凭据排序依次尝试尚未试过的 Key/账号，失败立刻换下一个、**不等待**；
  因此 N 个 Key 不会吃掉重试次数（3 个 Key × 3 轮 = 最多 9 次尝试）。
- 整轮都失败才按 `agent_retry_intervals` 等待并开下一轮。
- 每次拿到响应都要过健康检查：空响应、断流、仅思考无正文与抛异常同等对待。
- 分类为 `client` 的失败（上下文溢出、function-call 400、参数错误）原样重试无意义，
  引擎立即返回，交给调用方修复/压缩请求内容后再次进入本引擎。

依赖全部注入（调用、健康检查、等待、状态广播、结果上报），因此可脱离真实 provider 单测。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

# 失败分类
KIND_CLIENT = "client"      # 请求内容有问题：上下文溢出 / function-call 400 / 参数错误
KIND_AUTH = "auth"          # 鉴权失败：换 Key 有意义，原样重试无意义但换 Key 后可能成功
KIND_QUOTA = "quota"        # 限流 / 额度耗尽：等重置或换 Key
KIND_UPSTREAM = "upstream"  # 网关 5xx / 不可用 / 过载
KIND_NETWORK = "network"    # 超时 / 连接中断 / 断流
KIND_UNKNOWN = "unknown"

_CLIENT_HINTS = (
    "context_length", "context length", "context_window", "context window",
    "maximum context", "max context", "too long for context", "token limit",
    "maximum tokens", "max tokens", "too many tokens", "token count exceeds",
    "prompt is too long", "request too large", "上下文长度", "上下文窗口",
    "超出上下文", "上下文超出",
    "please ensure that function call", "called exactly once", "must be followed by tool",
    "role 'tool' must be a response",
    "invalid request", "invalid_request_error", "unknown field", "unexpected keyword",
)
_AUTH_HINTS = (
    "401", "403", "unauthorized", "forbidden", "invalid api key", "authentication",
    "invalid_api_key", "permission denied", "登录已过期", "重新登录",
)
_QUOTA_HINTS = (
    "429", "rate limit", "rate_limit", "quota", "insufficient", "余额", "积分",
    "额度", "too many requests", "overloaded",
)
_UPSTREAM_HINTS = (
    "500", "502", "503", "504", "bad gateway", "service unavailable", "gateway",
    "at capacity", "不可用", "internal server error", "server error",
)
_NETWORK_HINTS = (
    "timeout", "timed out", "超时", "connection", "connecterror", "connect error",
    "reset by peer", "reset", "eof", "broken pipe", "remote protocol",
    "remote disconnected", "peer closed", "断流", "中断", "丢帧", "空响应",
)

# 「将在 2026-10-19 00:00 (UTC+8) 重置」/「retry after 30」两类重置提示
_RESET_AT_RE = re.compile(
    r"(?:将在|于)?\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
    r"\s*(?:\(?\s*UTC\s*([+-])(\d{1,2})(?::?(\d{2}))?\s*\)?)?",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(r"retry[ -]after[^0-9]{0,6}(\d+)", re.IGNORECASE)
_RESET_HINT_RE = re.compile(r"(重置|reset|恢复|refill)", re.IGNORECASE)


@dataclass(frozen=True)
class Failure:
    """一次失败的分类结果。"""

    kind: str
    text: str
    # 原样重试是否有意义；client 类为 False（必须先修请求内容）
    retryable: bool = True
    # 是否计入凭据健康：client 类不计（换 Key 不会好转，不该让 Key 背锅）
    counted: bool = True
    # 额度类错误带出的重置时刻（用于设置暂缓窗口）
    reset_at: datetime | None = None

    @property
    def is_client(self) -> bool:
        return self.kind == KIND_CLIENT


def _parse_reset_at(text: str) -> datetime | None:
    """解析额度重置时刻。

    两种来源：绝对时间「将在 2026-10-19 00:00 (UTC+8) 重置」（默认按 UTC+8 解释，
    与文案一致；带时区标记则按其偏移），以及相对时间「retry after 30」（秒）。
    """
    if not text or not _RESET_HINT_RE.search(text):
        # retry-after 常见于不带「重置」字样的限流响应，单独处理
        m = _RETRY_AFTER_RE.search(text)
        if m:
            try:
                return datetime.now(timezone.utc) + timedelta(seconds=int(m.group(1)))
            except (TypeError, ValueError):
                return None
        return None
    m = _RESET_AT_RE.search(text)
    if m:
        year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
        second = int(m.group(6) or 0)
        sign, off_h, off_m = m.group(7), m.group(8), m.group(9)
        offset = timedelta(hours=8)  # 无时区标记时按 UTC+8（文案里的默认口径）
        if sign:
            offset = timedelta(hours=int(off_h or 0), minutes=int(off_m or 0))
            if sign == "-":
                offset = -offset
        try:
            local = datetime(year, month, day, hour, minute, second,
                             tzinfo=timezone(offset))
        except ValueError:
            return None
        return local.astimezone(timezone.utc)
    m = _RETRY_AFTER_RE.search(text)
    if m:
        try:
            return datetime.now(timezone.utc) + timedelta(seconds=int(m.group(1)))
        except (TypeError, ValueError):
            return None
    return None


def classify_failure(error: object) -> Failure:
    """把错误文本/异常归入 client | auth | quota | upstream | network | unknown。

    client 类（请求内容问题）标记为不可原样重试且不计入凭据健康——换 Key 不会好转，
    只有修复/压缩请求后才值得再试。
    """
    text = str(error or "")
    low = text.lower()
    if any(h in low for h in _CLIENT_HINTS):
        return Failure(KIND_CLIENT, text, retryable=False, counted=False)
    if any(h in low for h in _AUTH_HINTS):
        return Failure(KIND_AUTH, text)
    if any(h in low for h in _QUOTA_HINTS):
        return Failure(KIND_QUOTA, text, reset_at=_parse_reset_at(text))
    if any(h in low for h in _UPSTREAM_HINTS):
        return Failure(KIND_UPSTREAM, text, reset_at=_parse_reset_at(text))
    if any(h in low for h in _NETWORK_HINTS):
        return Failure(KIND_NETWORK, text)
    return Failure(KIND_UNKNOWN, text)


@dataclass
class Attempt:
    """一次尝试的坐标：第几轮（1-based）、本轮第几次（1-based）、思考档位。"""

    round: int
    index: int
    effort: str | None = None


@dataclass
class RecoveryResult:
    """引擎的最终结果。"""

    response: Any | None = None
    failure: Failure | None = None
    # 实际等待并尝试的轮数（不含首次调用）
    rounds: int = 0
    # 实际尝试次数（轮内换 Key 也算）
    attempts: int = 0
    # 最后一次失败的原始错误（异常路径用于终止文案）
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None and self.response is not None


class RecoveryCancelled(RuntimeError):
    """等待或尝试期间收到用户中断信号。"""


async def recover_model_call(
    *,
    call: Callable[[Attempt], Awaitable[Any]],
    intervals: Sequence[float],
    wait: Callable[[float], Awaitable[bool]],
    cancelled: Callable[[], bool],
    check: Callable[[Any], Failure | None] | None = None,
    efforts: Sequence[str | None] = (),
    per_round: int = 1,
    on_status: Callable[[str], Awaitable[None]] | None = None,
    on_outcome: Callable[[Attempt, Any | None, Failure | None], Awaitable[None]] | None = None,
) -> RecoveryResult:
    """按「轮」重试一次模型调用。

    Args:
        call: 执行一次调用；内部负责切换到本轮对应的凭据并构造请求。
            抛出的异常会被分类后当作该次失败。
        intervals: 每轮等待秒数；长度即重试轮数（空 = 不重试，直接返回首次失败）。
        wait: 等待；返回 False 表示等待期间被中断。
        cancelled: 是否已收到中断信号。
        check: 响应健康检查；返回 None 表示健康。
        efforts: 每轮的思考档位降档序列（不足按默认 None）。
        per_round: 每轮最多尝试几个凭据（= 该供应商可用凭据数，至少 1）。
        on_status: 状态行广播（「N 秒后重试 i/j」）。
        on_outcome: 每次尝试结束后的记账回调（成功/失败都调用）。

    Raises:
        RecoveryCancelled: 等待或尝试期间用户中断。
    """
    rounds = max(0, int(per_round or 1))
    result = RecoveryResult()
    failures: list[Failure] = []
    last_error: BaseException | None = None

    for r_idx, wait_s in enumerate(list(intervals), start=1):
        if cancelled():
            raise RecoveryCancelled("用户中断")
        effort = efforts[r_idx - 1] if r_idx - 1 < len(efforts) else None
        if on_status is not None:
            reason = failures[-1].text if failures else ""
            await on_status(
                f"{reason}，{max(0.0, float(wait_s)):.0f} 秒后重试 {r_idx}/{len(intervals)}…"
                if reason else f"{max(0.0, float(wait_s)):.0f} 秒后重试 {r_idx}/{len(intervals)}…"
            )
        if not await wait(float(wait_s)):
            raise RecoveryCancelled("重试等待期间收到中断信号")
        result.rounds = r_idx

        for i_idx in range(1, rounds + 1):
            if cancelled():
                raise RecoveryCancelled("用户中断")
            attempt = Attempt(round=r_idx, index=i_idx, effort=effort)
            result.attempts += 1
            response = None
            failure: Failure | None = None
            try:
                response = await call(attempt)
            except RecoveryCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 所有调用异常都当作一次失败
                last_error = exc
                failure = classify_failure(exc)
                logger.warning(
                    "[recovery] 第 %d 轮第 %d 次调用失败（%s）：%s",
                    r_idx, i_idx, failure.kind, str(exc)[:200],
                )
            else:
                if check is not None:
                    failure = check(response)
                if failure is None:
                    if on_outcome is not None:
                        await on_outcome(attempt, response, None)
                    result.response = response
                    result.failure = None
                    result.error = None
                    return result
                logger.warning(
                    "[recovery] 第 %d 轮第 %d 次响应不健康（%s）：%s",
                    r_idx, i_idx, failure.kind, failure.text[:200],
                )

            failures.append(failure)
            result.failure = failure
            result.error = last_error if response is None else None
            if on_outcome is not None:
                await on_outcome(attempt, None, failure)
            # 请求内容本身有问题：原样重试不会好转，交调用方修复后重新进入
            if not failure.retryable:
                return result

    return result
