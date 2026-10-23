"""v29 (plan-78): 空响应重试 + 思考降档的配置与判定单测。

v968: 撤销 v31 (plan-89) 的 has_progress 豁免——完全空响应一律 fatal 走统一
重试计划（实测网关截断输出流曾被静默当"任务完成"，用户看到"无故中断"）。

agent_loop 的重试逻辑内联在 run_agent_loop（依赖 db/engine/广播），
此处覆盖可独立验证的部分：
- 降档序列配置解析（agent_empty_retry_effort_list）
- 空响应/thinking_timeout fatal 判定（重试触发前提）
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


# ── v968: 已有工具产出时 stop 空响应同样 fatal（原 v31 豁免撤销）──

def test_stop_empty_response_with_progress_is_fatal():
    """本 turn 已有工具产出时，完全空响应不再判"任务完成"——疑似网关截断
    输出流，走统一重试计划，穷尽后报错。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop"), has_progress=True,
    )
    assert fatal is True
    assert "空响应" in reason
    assert "截断" in reason


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


def test_frames_received_stop_empty_with_progress_is_fatal():
    """v968: 已收到帧但零输出的 stop 空响应同样 fatal（原 v31 豁免已撤销）。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=2),
        has_progress=True,
    )
    assert fatal is True
    assert "空响应" in reason


def test_frames_received_stop_empty_without_progress_is_fatal():
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=2),
        has_progress=False,
    )
    assert fatal is True
    assert "空响应" in reason


def test_frames_received_none_stop_empty_with_progress_is_fatal():
    """v968: provider 未提供 frames 信号（None）时，完全空响应同样 fatal——
    不再回退"有产出即健康"的旧判定。"""
    reason, fatal = _response_failure_reason(
        ChatResponse(content=None, finish_reason="stop", frames_received=None),
        has_progress=True,
    )
    assert fatal is True
    assert "空响应" in reason


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


# ── v45: 统一异常重试策略（间隔标签序列，个数即重试次数）──

def test_retry_interval_list_default_is_10_20_30():
    """默认策略：3 次重试，间隔依次为 10/20/30 秒。"""
    s = Settings()
    assert s.agent_retry_interval_list == [10.0, 20.0, 30.0]


def test_retry_interval_list_count_follows_tags():
    """间隔个数即重试次数，不再补齐或截断。"""
    s = Settings(agent_retry_intervals="5,5")
    assert s.agent_retry_interval_list == [5.0, 5.0]


def test_retry_interval_list_empty_disables_retry():
    """没有任何间隔标签时不自动重试——报错直接停止。"""
    s = Settings(agent_retry_intervals="")
    assert s.agent_retry_interval_list == []


def test_retry_interval_list_ignores_invalid_tokens():
    """非法项与越界项被跳过，合法项仍生效。"""
    s = Settings(agent_retry_intervals="10, abc ,20,-1,5000")
    assert s.agent_retry_interval_list == [10.0, 20.0]


def test_retry_interval_list_capped_at_20_items():
    """重试次数上限 20，超出的标签丢弃。"""
    s = Settings(agent_retry_intervals=",".join(["1"] * 25))
    assert len(s.agent_retry_interval_list) == 20


def test_migrate_retry_policy_expands_legacy_count(tmp_path, monkeypatch):
    """旧版「次数 + 间隔」迁移为间隔序列：次数多于序列则用末项补齐，次数为 0 则清空，旧键删除。"""
    from app.gateway.routers import settings as settings_router

    monkeypatch.setattr(settings_router, "_USER_CONFIG_PATH", tmp_path / "config.json")
    data = {"agent_retry_count": 4, "agent_retry_intervals": "5,10"}
    settings_router._migrate_retry_policy(data)
    assert data["agent_retry_intervals"] == "5,10,10,10"
    assert "agent_retry_count" not in data

    data = {"agent_retry_count": 0, "agent_retry_intervals": "10,20,30"}
    settings_router._migrate_retry_policy(data)
    assert data["agent_retry_intervals"] == ""
