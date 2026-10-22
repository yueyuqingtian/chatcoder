"""plan-527-2218: 出站等效估算与校准系数持久化测试。

覆盖两件事：
- `estimate_outbound_tokens_db` 是否镜像了真实出站内容的两次变换（旧 thinking 剥离、
  工具结果按呈现预算截断）——用错口径会系统性高估，压缩判断随之失真；
- 校准系数（real / est）的内存状态与落盘恢复：初始 1.0、指数平滑、钳制区间、
  变化不足 1% 不落盘。系数是进程内 + 落盘的全局状态，每个用例都要隔离。
"""

import pytest

from app.core.enums import MsgType
from app.orchestration import token_counter as tc


class _Msg:
    """最小消息替身：出站估算只依赖这四个属性。"""

    def __init__(self, mid: int, msg_type: str, content, sender_type: str = "agent"):
        self.id = mid
        self.msg_type = msg_type
        self.content = content
        self.sender_type = sender_type


def _text(mid: int, text: str, sender_type: str = "user") -> _Msg:
    return _Msg(mid, MsgType.TEXT.value, {"text": text}, sender_type)


def _thinking(mid: int, text: str) -> _Msg:
    return _Msg(mid, MsgType.THINKING.value, {"text": text})


def _tool_call(mid: int) -> _Msg:
    """空 tool_call 的估算恰为结构开销：_MESSAGE_OVERHEAD + _TOOL_CALL_OVERHEAD。"""
    return _Msg(mid, MsgType.TOOL_CALL.value, {})


def _tool_result(mid: int, output: str, tool: str = "terminal_exec", error: str = "") -> _Msg:
    content = {"tool": tool, "output": output}
    if error:
        content["error"] = error
    return _Msg(mid, MsgType.TOOL_RESULT.value, content)


# ───────────────────────────────────────────────────────────────────
# 隔离夹具：内存态三件套 + 落盘路径都指向 tmp_path
# ───────────────────────────────────────────────────────────────────

@pytest.fixture
def calib_path(tmp_path, monkeypatch):
    """把校准系数状态完全隔离到临时目录，避免污染真实用户数据目录。"""
    path = tmp_path / "est_calibration.json"
    monkeypatch.setattr(tc, "_CALIB_BY_MODEL", {})
    monkeypatch.setattr(tc, "_CALIB_LAST_SAVED", {})
    monkeypatch.setattr(tc, "_CALIB_LOADED", False)
    monkeypatch.setattr(tc, "_calibration_path", lambda: path)
    return path


# ───────────────────────────────────────────────────────────────────
# 1. 校准系数：初始值 / 平滑 / 钳制 / 非法输入
# ───────────────────────────────────────────────────────────────────

def test_initial_factor_is_one(calib_path):
    """无记录时返回 1.0，且读取本身不落盘。"""
    assert tc.get_calibration_factor("test-model-fresh") == 1.0
    assert not calib_path.exists()


def test_two_updates_smooth_toward_real_ratio(calib_path):
    """平滑系数 0.5：1.0 → 1.1 → 1.15（real/est = 1.2）。"""
    key = "test-model-smooth"
    assert tc.update_calibration_factor(key, 1200, 1000) == pytest.approx(1.1)
    assert tc.update_calibration_factor(key, 1200, 1000) == pytest.approx(1.15)
    assert tc.get_calibration_factor(key) == pytest.approx(1.15)


def test_factor_clamped_to_upper_bound(calib_path):
    """极端高估的比值先被钳到 3.0，再过平滑，多轮后收敛到上限且不越界。"""
    key = "test-model-clamp-high"
    # 单次更新：cur = 0.5*1.0 + 0.5*min(3.0, 100) = 2.0（钳制作用在比值上）
    assert tc.update_calibration_factor(key, 100000, 1000) == pytest.approx(2.0)
    for _ in range(60):
        got = tc.update_calibration_factor(key, 100000, 1000)
    assert got == pytest.approx(tc._CALIB_MAX, abs=1e-6)
    assert got <= tc._CALIB_MAX


def test_factor_clamped_to_lower_bound(calib_path):
    """连续低估时收敛到下限 0.3，不会继续往 0 掉。"""
    key = "test-model-clamp-low"
    for _ in range(60):
        got = tc.update_calibration_factor(key, 1, 10000)
    assert got == pytest.approx(tc._CALIB_MIN, rel=1e-6)
    assert got >= tc._CALIB_MIN


@pytest.mark.parametrize("real_tokens,est_tokens", [(0, 1000), (1000, 0), (-5, 1000), (1000, -5)])
def test_invalid_usage_keeps_factor(calib_path, real_tokens, est_tokens):
    """任一计数非正都视为无效上报：系数不变、也不写盘。"""
    key = "test-model-invalid"
    assert tc.update_calibration_factor(key, real_tokens, est_tokens) == pytest.approx(1.0)
    assert tc.get_calibration_factor(key) == pytest.approx(1.0)
    assert not calib_path.exists()


# ───────────────────────────────────────────────────────────────────
# 2. 校准系数：落盘策略与重启恢复
# ───────────────────────────────────────────────────────────────────

def test_tiny_change_is_not_persisted(calib_path, monkeypatch):
    """变化不足 _CALIB_SAVE_EPS 时只更新内存，不写盘（避免频繁 IO）。"""
    key = "test-model-eps"
    saves: list[str] = []
    monkeypatch.setattr(tc, "_save_calibration", lambda: saves.append("saved"))

    # 先建立已落盘基线（系数 1.0），清掉这次计数
    tc.update_calibration_factor(key, 1000, 1000)
    saves.clear()

    # 1.005 → 新系数 1.0025，与上次落盘的 1.0 相差 0.25%，不足 1%
    cur = tc.update_calibration_factor(key, 1005, 1000)
    assert cur == pytest.approx(1.0025)
    assert tc.get_calibration_factor(key) == pytest.approx(1.0025)
    assert saves == []

    # 再放大到明显变化，才应该落盘
    tc.update_calibration_factor(key, 2000, 1000)
    assert saves == ["saved"]


def test_factor_survives_reload(calib_path, monkeypatch):
    """落盘后清空内存并重置 loaded 标记，系数应能从文件恢复。"""
    key = "test-model-reload"
    tc.update_calibration_factor(key, 1200, 1000)  # 1.1

    assert calib_path.exists()
    assert key in calib_path.read_text(encoding="utf-8")

    monkeypatch.setattr(tc, "_CALIB_BY_MODEL", {})
    monkeypatch.setattr(tc, "_CALIB_LAST_SAVED", {})
    monkeypatch.setattr(tc, "_CALIB_LOADED", False)

    assert tc.get_calibration_factor(key) == pytest.approx(1.1)


def test_out_of_range_values_in_file_are_ignored(calib_path, monkeypatch):
    """文件里的越界/非数值条目直接忽略，回落到 1.0。"""
    calib_path.write_text(
        '{"test-model-bad-high": 9.9, "test-model-bad-low": 0.01, "test-model-bad-str": "x"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(tc, "_CALIB_LOADED", False)
    assert tc.get_calibration_factor("test-model-bad-high") == 1.0
    assert tc.get_calibration_factor("test-model-bad-low") == 1.0
    assert tc.get_calibration_factor("test-model-bad-str") == 1.0


# ───────────────────────────────────────────────────────────────────
# 3. 带校准系数的出站估算
# ───────────────────────────────────────────────────────────────────

def test_calibrated_estimate_multiplies_raw(calib_path):
    """estimate_outbound_with_calibration = 出站等效估算 × 该模型系数。"""
    key = "test-model-multiply"
    msgs = [_text(1, "hello world " * 200)]
    raw = tc.estimate_outbound_tokens_db(msgs)
    assert raw > 0

    # 系数仍为 1.0 时与 raw 一致
    assert tc.estimate_outbound_with_calibration(msgs, key) == raw

    # 多轮极端上报后系数收敛到上限附近
    for _ in range(60):
        tc.update_calibration_factor(key, 100000, 1000)
    factor = tc.get_calibration_factor(key)
    assert factor == pytest.approx(tc._CALIB_MAX, abs=1e-6)
    assert tc.estimate_outbound_with_calibration(msgs, key) == int(raw * factor)


def test_calibrated_estimate_empty_is_zero(calib_path):
    assert tc.estimate_outbound_with_calibration([], "test-model-empty") == 0


# ───────────────────────────────────────────────────────────────────
# 4. 出站等效估算：基础形状与容错
# ───────────────────────────────────────────────────────────────────

def test_outbound_empty_list_is_zero():
    assert tc.estimate_outbound_tokens_db([]) == 0


def test_outbound_tolerates_content_shapes():
    """content 为 dict / str / None 都不能崩，且结构开销照计。"""
    msgs = [
        _Msg(1, MsgType.TEXT.value, {"text": "abc"}),
        _Msg(2, MsgType.TEXT.value, "plain string"),
        _Msg(3, MsgType.TEXT.value, None),
        _Msg(4, MsgType.TOOL_RESULT.value, "not a dict"),
        _Msg(5, MsgType.TOOL_RESULT.value, None),
    ]
    expected = (
        tc._MESSAGE_OVERHEAD
        + tc.rough_token_estimate("abc")
        + tc._MESSAGE_OVERHEAD
        + tc.rough_token_estimate("plain string")
        + tc._MESSAGE_OVERHEAD  # None 只算结构开销
        + (tc._MESSAGE_OVERHEAD + tc._TOOL_RESULT_OVERHEAD)  # 非 dict 按空工具结果算
        + (tc._MESSAGE_OVERHEAD + tc._TOOL_RESULT_OVERHEAD)
    )
    assert tc.estimate_outbound_tokens_db(msgs) == expected


def test_text_estimate_positive_and_grows_with_length():
    short = tc.estimate_outbound_tokens_db([_text(1, "hello")])
    long = tc.estimate_outbound_tokens_db([_text(1, "hello world " * 200)])
    assert short > 0
    assert long > short


# ───────────────────────────────────────────────────────────────────
# 5. 出站等效估算：旧 thinking 剥离
# ───────────────────────────────────────────────────────────────────

def test_old_thinking_costs_structural_overhead_only():
    """3 个工具回合之后，更早的 thinking 只留 4 token 结构开销。"""
    msgs = [_thinking(1, "很长的旧思考" * 2000), _tool_call(2), _tool_call(3), _tool_call(4)]
    got = tc.estimate_outbound_tokens_db(msgs)

    tool_call_cost = tc._MESSAGE_OVERHEAD + tc._TOOL_CALL_OVERHEAD
    assert got == tc._MESSAGE_OVERHEAD + 3 * tool_call_cost


def test_old_thinking_length_does_not_change_estimate():
    """同一位置换成长短不同的旧 thinking，估算必须完全一致（证明确实被剥离）。"""
    short = [_thinking(1, "短"), _tool_call(2), _tool_call(3), _tool_call(4)]
    long = [_thinking(1, "很长的思考" * 3000), _tool_call(2), _tool_call(3), _tool_call(4)]
    assert tc.estimate_outbound_tokens_db(short) == tc.estimate_outbound_tokens_db(long)


def test_recent_thinking_is_fully_counted():
    """保留窗口内的 thinking 不剥离，按全文计入。"""
    msgs = [_tool_call(1), _tool_call(2), _tool_call(3), _thinking(4, "最近思考" * 500)]
    recent_cost = tc.estimate_message_tokens_from_model(msgs[-1])
    assert recent_cost > tc._MESSAGE_OVERHEAD

    got = tc.estimate_outbound_tokens_db(msgs)
    assert got == 3 * (tc._MESSAGE_OVERHEAD + tc._TOOL_CALL_OVERHEAD) + recent_cost


def test_thinking_within_keep_rounds_survives():
    """只有 2 个工具回合时不构成「旧回合」，thinking 全额保留。"""
    msgs = [_thinking(1, "思考" * 300), _tool_call(2), _tool_call(3)]
    thinking_cost = tc.estimate_message_tokens_from_model(msgs[0])
    got = tc.estimate_outbound_tokens_db(msgs)
    assert got == thinking_cost + 2 * (tc._MESSAGE_OVERHEAD + tc._TOOL_CALL_OVERHEAD)


# ───────────────────────────────────────────────────────────────────
# 6. 出站等效估算：工具结果按呈现预算截断
# ───────────────────────────────────────────────────────────────────

def test_tool_result_truncated_by_presentation_budget(monkeypatch):
    """超长工具输出按预算折算，不再按原文全量计。"""
    from app.orchestration import output_paging

    monkeypatch.setattr(output_paging, "budget_for_tool", lambda _name: (200, 0))

    big = "x" * 20000
    got = tc.estimate_outbound_tokens_db([_tool_result(1, big)])
    full = (
        tc._MESSAGE_OVERHEAD
        + tc._TOOL_RESULT_OVERHEAD
        + tc.rough_token_estimate("terminal_exec" + big)
    )
    assert got < full
    assert got < full // 10


def test_fs_read_is_not_truncated(monkeypatch):
    """fs_read 的输出窗口已由工具自身封顶，估算时原样计入。"""
    from app.orchestration import output_paging

    monkeypatch.setattr(output_paging, "budget_for_tool", lambda _name: (10, 0))

    text = "y" * 4000
    got = tc.estimate_outbound_tokens_db([_tool_result(1, text, tool="fs_read")])
    full = (
        tc._MESSAGE_OVERHEAD
        + tc._TOOL_RESULT_OVERHEAD
        + tc.rough_token_estimate("fs_read" + text)
    )
    assert got == full


def test_tool_result_error_field_fallback(monkeypatch):
    """output 为空时退到 error 字段，两者都没有也不会崩。"""
    from app.orchestration import output_paging

    monkeypatch.setattr(output_paging, "budget_for_tool", lambda _name: (200, 0))

    with_error = tc.estimate_outbound_tokens_db([_tool_result(1, "", error="boom")])
    empty = tc.estimate_outbound_tokens_db([_tool_result(1, "")])
    assert with_error > empty == tc._MESSAGE_OVERHEAD + tc._TOOL_RESULT_OVERHEAD + tc.rough_token_estimate("terminal_exec")
