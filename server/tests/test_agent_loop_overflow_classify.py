"""v33: 上下文溢出错误分类测试——异常恢复分支只对真正溢出走紧急压缩。

覆盖：
- 各网关溢出错误消息（英文/中文/HTTP 400）判定为 True
- 瞬时故障（429 余额/503 渠道/连接失败/限流/function-call 400）判定为 False，
  避免"低占用异常压缩"与"压缩后 AI 中断"
"""
from app.orchestration.agent_loop import _is_context_overflow_error


class TestContextOverflowClassify:
    def test_openai_style_max_context(self):
        assert _is_context_overflow_error(
            "This model's maximum context length is 200000 tokens. "
            "However, you requested 250000 tokens"
        )

    def test_anthropic_style_context_exceeded(self):
        assert _is_context_overflow_error(
            "prompt is too long: 300000 tokens > 200000 maximum"
        )

    def test_gemini_style(self):
        assert _is_context_overflow_error(
            "The total number of tokens in the request exceeds the context window"
        )

    def test_deepseek_style(self):
        assert _is_context_overflow_error(
            "context_length_exceeded: input exceeds model context window"
        )

    def test_chinese_message(self):
        assert _is_context_overflow_error("请求超出上下文窗口大小，请压缩后重试")
        assert _is_context_overflow_error("您的请求超过了模型上下文长度限制")

    def test_request_too_large(self):
        assert _is_context_overflow_error("Request too large for the model context window")

    def test_quota_429_not_overflow(self):
        # 余额不足/配额——瞬时故障，压缩无意义
        assert not _is_context_overflow_error(
            "model gateway error (HTTP 429): 余额不足或无可用的资源包,请充值"
        )

    def test_channel_503_not_overflow(self):
        assert not _is_context_overflow_error(
            "模型请求失败 503：所有渠道均不可用: Key池已满: ZAI"
        )

    def test_connection_error_not_overflow(self):
        assert not _is_context_overflow_error(
            "model gateway error (connection): All connection attempts failed"
        )
        assert not _is_context_overflow_error("模型请求失败：ConnectError: connection refused")

    def test_rate_limit_not_overflow(self):
        assert not _is_context_overflow_error("rate limit exceeded, please slow down")

    def test_function_call_400_not_overflow(self):
        # function-call 配对错误走独立修复分支，不归为溢出
        assert not _is_context_overflow_error(
            "Please ensure that function call appears exactly once"
        )

    def test_stream_timeout_not_overflow(self):
        assert not _is_context_overflow_error(
            "model gateway error: stream chunk timeout (300s)"
        )

    def test_empty_message(self):
        assert not _is_context_overflow_error("")
        assert not _is_context_overflow_error(None)
