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


# ── v966: 零帧断流（网关未下发任何数据帧）不再被 has_progress 豁免 ──

def test_zero_frame_stop_empty_is_fatal_even_with_progress():
    """网关零帧空流（网络卡顿/服务器处理慢）不能判定为结束——即使本 turn
    已有产出，也必须 fatal 走多次重试，避免任务在模型未应答时静默中断。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=0),
        has_progress=True,
    )
    assert fatal is True
    assert "未返回任何数据" in reason


def test_zero_frame_stop_empty_is_fatal_without_progress():
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=0),
        has_progress=False,
    )
    assert fatal is True


def test_zero_frame_timeout_empty_is_fatal():
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="timeout", frames_received=0),
        has_progress=True,
    )
    assert fatal is True


def test_frames_received_stop_empty_with_progress_is_healthy():
    """模型已应答（收到帧）但主动无输出 + 已有产出 → 保留 v31 豁免语义。"""
    assert _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=2),
        has_progress=True,
    ) is None


def test_frames_received_stop_empty_without_progress_is_fatal():
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=2),
        has_progress=False,
    )
    assert fatal is True
    assert "空响应" in reason


def test_frames_received_none_keeps_legacy_logic():
    """provider 未提供 frames 信号（None）时保持旧判定：有产出+stop 空响应豁免。"""
    assert _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=None),
        has_progress=True,
    ) is None


# ── 非 fatal 不进入重试路径 ──

def test_truncation_not_fatal():
    reason, fatal = _response_failure_reason(ChatResponse(content="部分", finish_reason="max_tokens"))
    assert fatal is False
    assert "token 上限" in reason


def test_zero_content_length_truncation_is_fatal():
    """v45: 截断且零正文（thinking 耗尽预算）必须致命并进入重试，不再静默中断。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="length",
                     thinking="我在思考这个问题的实现路径，先分析现有代码结构再决定改动点。"),
    )
    assert fatal is True
    assert "token 上限" in reason


def test_length_with_tool_calls_is_healthy():
    """已有工具调用时截断不影响本轮动作，判健康。"""
    resp = ChatResponse(content=None, finish_reason="length")
    resp.tool_calls = [{"id": "c1", "name": "fs_read", "arguments": {}}]
    assert _response_failure_reason(resp) is None


def test_timeout_with_partial_content_not_fatal():
    reason, fatal = _response_failure_reason(ChatResponse(content="部分", finish_reason="timeout"))
    assert fatal is False


def test_healthy_stop_not_fatal():
    assert _response_failure_reason(ChatResponse(content="完成", finish_reason="stop")) is None


# ── v45: 统一异常重试策略（次数 + 间隔序列）──

def test_retry_interval_list_default_is_10_20_30():
    """默认策略：3 次重试，间隔依次为 10/20/30 秒。"""
    s = Settings()
    assert s.agent_retry_count == 3
    assert s.agent_retry_interval_list == [10.0, 20.0, 30.0]


def test_retry_interval_list_pads_with_last_value():
    """间隔序列项数不足以覆盖重试次数时，用最后一项补齐。"""
    s = Settings(agent_retry_count=3, agent_retry_intervals="5")
    assert s.agent_retry_interval_list == [5.0, 5.0, 5.0]


def test_retry_interval_list_truncated_to_count():
    """间隔序列比重试次数长时，截断到次数长度。"""
    s = Settings(agent_retry_count=2, agent_retry_intervals="10,20,30")
    assert s.agent_retry_interval_list == [10.0, 20.0]


def test_retry_interval_list_disabled_with_zero_retries():
    """重试次数为 0（不重试）时，计划为空——报错直接停止。"""
    s = Settings(agent_retry_count=0, agent_retry_intervals="10,20,30")
    assert s.agent_retry_interval_list == []


def test_retry_interval_list_falls_back_when_intervals_blank():
    """间隔序列为空/非法时回退到 agent_retry_interval_seconds。"""
    s = Settings(agent_retry_count=2, agent_retry_intervals="", agent_retry_interval_seconds=7.0)
    assert s.agent_retry_interval_list == [7.0, 7.0]


def test_retry_interval_list_ignores_invalid_tokens():
    """非法项被跳过，合法项仍生效并可补齐。"""
    s = Settings(agent_retry_count=3, agent_retry_intervals="10, abc ,20")
    assert s.agent_retry_interval_list == [10.0, 20.0, 20.0]
