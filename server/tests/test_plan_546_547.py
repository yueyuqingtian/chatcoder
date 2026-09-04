"""plan-546 & plan-547 新增能力单测。"""
import pytest
from app.gateway.schemas import SessionCreate, TurnCreate, TurnInjectBody
from app.core.config import settings
from app.orchestration import engine


def test_session_create_permission_mode():
    """plan-547: SessionCreate 支持 permission_mode 字段（首页发送一次落准）。"""
    sc = SessionCreate(project_id=1, title="新任务", permission_mode="plan")
    assert sc.permission_mode == "plan"
    assert sc.model_id is None

    sc_default = SessionCreate(project_id=2)
    assert sc_default.permission_mode is None


def test_turn_inject_body_schema():
    """plan-547: TurnInjectBody 包含 request_id / content / attachments。"""
    b = TurnInjectBody(request_id="q-12345", content="补充说明：请注意单元测试")
    assert b.request_id == "q-12345"
    assert b.content == "补充说明：请注意单元测试"
    assert b.attachments is None

    b_with_att = TurnInjectBody(
        content="看看这个",
        attachments=[{"file_id": "f-1", "filename": "a.png"}],
    )
    assert len(b_with_att.attachments) == 1


def test_engine_inject_queue():
    """plan-547: engine 注入队列机制（未运行返回 False，运行中可入队并一次性 drain）。"""
    turn_id = 999991

    # turn 未在 _running_turns 时拒绝注入
    ok = engine.inject_input(turn_id, {"content": "test"})
    assert not ok
    assert engine.drain_injected_inputs(turn_id) == []

    # 模拟 turn 正在运行
    engine._running_turns.add(turn_id)
    try:
        ok1 = engine.inject_input(turn_id, {"request_id": "q1", "content": "msg 1"})
        ok2 = engine.inject_input(turn_id, {"request_id": "q2", "content": "msg 2"})
        assert ok1 is True
        assert ok2 is True

        drained = engine.drain_injected_inputs(turn_id)
        assert len(drained) == 2
        assert drained[0]["request_id"] == "q1"
        assert drained[1]["content"] == "msg 2"

        # drain 后清空
        assert engine.drain_injected_inputs(turn_id) == []
    finally:
        engine._running_turns.discard(turn_id)
        engine.discard_injected_inputs(turn_id)


def test_config_progress_reminder_interval():
    """plan-547 C1: 进度提醒配置项存在且有效。"""
    assert hasattr(settings, "agent_progress_reminder_interval")
    assert settings.agent_progress_reminder_interval >= 0
