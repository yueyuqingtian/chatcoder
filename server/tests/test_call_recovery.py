"""plan-20261020-retry-cred-perf §1 单测：统一重试引擎（纯逻辑，不连真实 provider）。

覆盖：间隔与调用一一对应、共享预算、健康检查视为失败、client 类立即返回、
等待被中断、状态广播与终止信息、失败分类与额度重置时刻解析。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.orchestration.call_recovery import (
    Attempt, Failure, KIND_AUTH, KIND_CLIENT, KIND_NETWORK, KIND_QUOTA, RecoveryCancelled,
    RetryBudget, classify_failure, recover_model_call,
)


class _Resp:
    def __init__(self, content="", thinking="", tool_calls=None, finish="stop", frames=1):
        self.content = content
        self.thinking = thinking
        self.tool_calls = tool_calls or []
        self.finish_reason = finish
        self.frames_received = frames


def _run(coro):
    return asyncio.run(coro)


async def _never_wait(_seconds: float) -> bool:
    return True


def test_classify_client_errors_are_not_retryable():
    for text in (
        "Error code: 400 - context_length_exceeded",
        "Please ensure that function call turn comes immediately after a user turn",
        "invalid_request_error: unknown field 'foo'",
        "上下文长度超出",
    ):
        f = classify_failure(text)
        assert f.kind == KIND_CLIENT, text
        assert not f.retryable
        assert not f.counted


def test_classify_auth_quota_network():
    assert classify_failure("401 Unauthorized").kind == KIND_AUTH
    assert classify_failure("429 rate limit exceeded").kind == KIND_QUOTA
    assert classify_failure("httpx.ConnectError: connection refused").kind == KIND_NETWORK


def test_quota_reset_time_parsed_from_gateway_text():
    f = classify_failure("额度已用尽，将在 2026-10-19 00:00 (UTC+8) 重置")
    assert f.kind == KIND_QUOTA
    assert f.reset_at is not None
    # UTC+8 的 00:00 即前一日 16:00 UTC
    assert f.reset_at.astimezone(timezone.utc).hour == 16
    assert f.reset_at.astimezone(timezone.utc).day == 18


def test_retry_after_hint_becomes_reset_time():
    f = classify_failure("429 too many requests, retry after 30")
    assert f.reset_at is not None
    assert f.reset_at > datetime.now(timezone.utc)


def test_each_interval_authorizes_exactly_one_attempt():
    """plan-20261112-retry-policy-1：配置三个标签就只能重试三次。"""
    async def scenario():
        calls: list[Attempt] = []
        waits: list[float] = []

        async def call(att):
            calls.append(att)
            raise RuntimeError("502 bad gateway")

        async def wait(s):
            waits.append(s)
            return True

        res = await recover_model_call(
            call=call, intervals=[10, 20, 30], wait=wait, cancelled=lambda: False,
        )
        return res, calls, waits

    res, calls, waits = _run(scenario())
    assert res.attempts == 3
    assert res.rounds == 3
    assert [c.round for c in calls] == [1, 2, 3]
    assert [c.index for c in calls] == [1, 1, 1]
    assert waits == [10, 20, 30]
    assert res.failure is not None and res.failure.kind == "upstream"


def test_success_in_later_round_returns_response():
    async def scenario():
        state = {"n": 0}

        async def call(att):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("connection reset by peer")
            return _Resp(content="ok")

        res = await recover_model_call(
            call=call, intervals=[1, 1, 1], wait=_never_wait, cancelled=lambda: False,
        )
        return res, state["n"]

    res, n = _run(scenario())
    assert res.ok and res.response.content == "ok"
    assert n == 3


def test_empty_response_counts_as_failure_and_retries():
    """空响应与异常同等对待——必须过健康检查（R3）。"""
    async def scenario():
        replies = [_Resp(), _Resp(content="好了")]
        calls = []

        async def call(att):
            calls.append(att)
            return replies.pop(0)

        def check(resp):
            if not resp.content and not resp.tool_calls:
                return Failure(KIND_NETWORK, "模型返回空响应")
            return None

        return await recover_model_call(
            call=call, check=check, intervals=[5, 10], wait=_never_wait,
            cancelled=lambda: False,
        ), calls

    res, calls = _run(scenario())
    assert res.ok and res.response.content == "好了"
    assert len(calls) == 2


def test_client_failure_returns_immediately_without_further_rounds():
    async def scenario():
        calls = []

        async def call(att):
            calls.append(att)
            raise RuntimeError("context_length_exceeded")

        return await recover_model_call(
            call=call, intervals=[10, 20, 30], wait=_never_wait,
            cancelled=lambda: False,
        ), calls

    res, calls = _run(scenario())
    # 第一次就判定为请求内容问题：不换 Key、不等待、不消耗后续轮次
    assert len(calls) == 1
    assert res.failure is not None and res.failure.kind == KIND_CLIENT


def test_wait_interruption_raises_cancelled():
    async def scenario():
        async def call(att):
            raise RuntimeError("503 service unavailable")

        async def wait(_s):
            return False

        return await recover_model_call(
            call=call, intervals=[10], wait=wait, cancelled=lambda: False,
        )

    with pytest.raises(RecoveryCancelled):
        _run(scenario())


def test_cancel_before_attempt_raises():
    async def scenario():
        async def call(att):  # pragma: no cover - 不应被调用
            raise AssertionError("cancelled 时不得调用")

        return await recover_model_call(
            call=call, intervals=[10], wait=_never_wait, cancelled=lambda: True,
        )

    with pytest.raises(RecoveryCancelled):
        _run(scenario())


def test_status_and_outcome_callbacks_receive_details():
    async def scenario():
        statuses: list[str] = []
        outcomes: list[tuple[int, int, str | None]] = []
        state = {"n": 0}

        async def call(att):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("429 rate limit")
            return _Resp(content="ok")

        async def on_status(text):
            statuses.append(text)

        async def on_outcome(att, resp, fail):
            outcomes.append((att.round, att.index, fail.kind if fail else None))

        res = await recover_model_call(
            call=call, intervals=[30, 60], wait=_never_wait, cancelled=lambda: False,
            on_status=on_status, on_outcome=on_outcome,
        )
        return res, statuses, outcomes

    res, statuses, outcomes = _run(scenario())
    assert res.ok
    assert statuses == ["30 秒后重试 1/2…", "正在重试 1/2…",
                        "429 rate limit，60 秒后重试 2/2…", "正在重试 2/2…"]
    assert outcomes == [(1, 1, KIND_QUOTA), (2, 1, None)]


def test_effort_ladder_follows_rounds():
    async def scenario():
        seen: list[str | None] = []

        async def call(att):
            seen.append(att.effort)
            if len(seen) < 3:
                return _Resp()
            return _Resp(content="ok")

        def check(resp):
            return Failure(KIND_NETWORK, "空响应") if not resp.content else None

        await recover_model_call(
            call=call, check=check, intervals=[1, 1, 1], wait=_never_wait,
            cancelled=lambda: False, efforts=["high", "low", "none"],
        )
        return seen

    assert _run(scenario()) == ["high", "low", "none"]


def test_zero_intervals_means_no_retry():
    async def scenario():
        calls = []

        async def call(att):
            calls.append(att)
            raise RuntimeError("503")

        res = await recover_model_call(
            call=call, intervals=[], wait=_never_wait, cancelled=lambda: False,
        )
        return res, calls

    res, calls = _run(scenario())
    assert calls == []
    # 没有重试机会时由调用方按首次失败收尾
    assert res.failure is None and not res.ok


def test_reenter_after_client_repair_uses_only_remaining_intervals():
    async def scenario():
        budget = RetryBudget([10, 20, 30])
        calls, waits = [], []

        async def call(att):
            calls.append(att.round)
            if len(calls) == 1:
                raise RuntimeError("context_length_exceeded")
            raise RuntimeError("503 unavailable")

        async def wait(seconds):
            waits.append(seconds)
            return True

        first = await recover_model_call(call=call, intervals=budget.intervals, budget=budget,
                                         wait=wait, cancelled=lambda: False)
        assert first.failure.is_client
        second = await recover_model_call(call=call, intervals=budget.intervals, budget=budget,
                                          wait=wait, cancelled=lambda: False)
        assert second.attempts == 2
        third = await recover_model_call(call=call, intervals=budget.intervals, budget=budget,
                                         wait=wait, cancelled=lambda: False)
        assert third.attempts == 0 and third.failure.text == "503 unavailable"
        assert calls == [1, 2, 3] and waits == [10, 20, 30]
        assert budget.attempts == 3

    _run(scenario())


def test_cancel_after_wait_does_not_spend_budget_or_call():
    async def scenario():
        event = asyncio.Event()
        budget = RetryBudget([0])

        async def wait(seconds):
            event.set()
            return True

        async def call(att):
            pytest.fail("取消后不应调用模型")

        with pytest.raises(RecoveryCancelled):
            await recover_model_call(call=call, intervals=[0], budget=budget, wait=wait,
                                     cancelled=event.is_set)
        assert budget.attempts == 0

    _run(scenario())
