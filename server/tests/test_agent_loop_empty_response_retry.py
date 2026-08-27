"""v29 (plan-78): 空响应重试 + 思考降档的配置与判定单测。

v31 (plan-89): 对齐 zcode/AI SDK 语义——本 turn 已有工具产出（has_progress）时，
finish=stop 空响应视为"任务完成、主动结束对话"，不再 fatal/重试。

agent_loop 的重试逻辑内联在 run_agent_loop（依赖 db/engine/广播），
此处覆盖可独立验证的部分：
- 降档序列配置解析（agent_empty_retry_effort_list）
- 空响应/thinking_timeout fatal 判定（重试触发前提，含 has_progress 豁免）
- 非 fatal（截断/部分内容）不进入重试路径的前提判定
"""
from app.core.config import Settings
from app.models.schemas import ChatResponse
from app.orchestration.agent_loop import _response_failure_reason


# ── 降档序列配置解析 ──

def test_empty_retry_effort_list_default():
    s = Settings()
    assert s.agent_empty_response_retries == 2
    assert s.agent_empty_retry_effort_list == ["low", "none"]


def test_empty_retry_effort_list_strips_empty_items():
    s = Settings(agent_empty_retry_efforts="low,,none, ")
    assert s.agent_empty_retry_effort_list == ["low", "none"]


def test_empty_retry_disabled_with_zero_retries():
    s = Settings(agent_empty_response_retries=0)
    assert s.agent_empty_response_retries == 0


# ── 空响应 fatal 判定（重试触发前提）──

def test_empty_response_is_fatal_and_triggers_retry_premise():
    reason, fatal = _response_failure_reason(ChatResponse(content=None, finish_reason="stop"))
    assert fatal is True
    assert "空响应" in reason


def test_thinking_timeout_is_fatal_and_triggers_retry_premise():
    reason, fatal = _response_failure_reason(ChatResponse(content=None, finish_reason="thinking_timeout"))
    assert fatal is True
    assert "思考超时" in reason


# ── v31 (plan-89): 已有工具产出时 stop 空响应豁免 ──

def test_stop_empty_response_with_progress_is_healthy():
    """本 turn 已有工具产出时，stop 空响应 = 任务完成，正常结束不重试不报错。"""
    assert _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop"), has_progress=True,
    ) is None


def test_stop_empty_response_without_progress_is_fatal():
    """零产出（第一步即空响应）时 stop 空响应仍 fatal，保留瞬时故障重试兜底。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop"), has_progress=False,
    )
    assert fatal is True
    assert "空响应" in reason


def test_thinking_timeout_fatal_regardless_of_progress():
    """思考看门狗超时是网关断流真实故障，无论是否有产出都保持 fatal（重试有意义）。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="thinking_timeout"), has_progress=True,
    )
    assert fatal is True


def test_timeout_empty_fatal_regardless_of_progress():
    """网关空闲超时同理：瞬时故障走重试降档，不因已有产出而豁免。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="timeout"), has_progress=True,
    )
    assert fatal is True


# ── 非 fatal 不进入重试路径 ──

def test_truncation_not_fatal():
    reason, fatal = _response_failure_reason(ChatResponse(content="部分", finish_reason="max_tokens"))
    assert fatal is False
    assert "token 上限" in reason


def test_timeout_with_partial_content_not_fatal():
    reason, fatal = _response_failure_reason(ChatResponse(content="部分", finish_reason="timeout"))
    assert fatal is False


def test_healthy_stop_not_fatal():
    assert _response_failure_reason(ChatResponse(content="完成", finish_reason="stop")) is None
