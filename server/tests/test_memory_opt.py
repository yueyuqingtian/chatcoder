"""plan-537-2256：记忆提取时机（engine 侧）的回归测试。

覆盖：
- `_has_real_user_prose`：纯工具轮 / 系统提醒轮不应触发提取；
- `_memory_cursor`：cursor 缺失、非法、正常三态；
- `_spawn_memory_extract` 的三道闸门与 pending 合并。

不覆盖服务层（去重、开关、promote 反查等）——那些见 test_memory_service_opt.py。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.orchestration import engine as E


def test_has_real_user_prose_filters_non_prose():
    # 空 / 纯空白 / 系统注入 / 短应答都不算「值得提炼的用户正文」
    assert E._has_real_user_prose("") is False
    assert E._has_real_user_prose("   ") is False
    assert E._has_real_user_prose("[系统] 后台子代理有新进展") is False
    assert E._has_real_user_prose("<system-reminder>继续") is False
    assert E._has_real_user_prose("继续") is False
    assert E._has_real_user_prose("ok") is False


def test_has_real_user_prose_accepts_real_sentences():
    assert E._has_real_user_prose("帮我修复记忆模块的重复写入问题") is True
    assert E._has_real_user_prose("please fix the duplicate memory writes") is True


def test_memory_cursor_reads_shared_context():
    assert E._memory_cursor(SimpleNamespace(shared_context=None)) == 0
    assert E._memory_cursor(SimpleNamespace(shared_context={})) == 0
    assert E._memory_cursor(SimpleNamespace(shared_context={"memory_extracted_turn_id": 42})) == 42
    # 非法值不应抛错，按 0 处理（否则一次脏数据会让提取永久停摆）
    assert E._memory_cursor(SimpleNamespace(shared_context={"memory_extracted_turn_id": "abc"})) == 0


@pytest.fixture(autouse=True)
def _clean_state():
    E._MEM_EXTRACT_STATE.clear()
    E._MEM_EXTRACT_TASKS.clear()
    yield
    E._MEM_EXTRACT_STATE.clear()
    E._MEM_EXTRACT_TASKS.clear()


@pytest.mark.asyncio
async def test_switch_off_skips_without_touching_state():
    await E._spawn_memory_extract(
        None, session_id=901, turn_id=5,
        prompt="请帮我修复这个问题并解释原因", summary="done",
        auto_memory_enabled=False,
    )
    assert 901 not in E._MEM_EXTRACT_STATE


@pytest.mark.asyncio
async def test_no_user_prose_skips_without_touching_state():
    await E._spawn_memory_extract(
        None, session_id=902, turn_id=5,
        prompt="继续", summary="done",
        auto_memory_enabled=True,
    )
    assert 902 not in E._MEM_EXTRACT_STATE


@pytest.mark.asyncio
async def test_no_increment_skips_when_cursor_reached():
    # cursor 已推进到本轮或更晚 → 不重复提炼
    await E._spawn_memory_extract(
        None, session_id=903, turn_id=5,
        prompt="请帮我修复这个问题并解释原因", summary="done",
        auto_memory_enabled=True, last_extracted_turn_id=5,
    )
    assert 903 not in E._MEM_EXTRACT_STATE


@pytest.mark.asyncio
async def test_pending_coalesces_when_already_running():
    """同一会话已有提取在跑时，新调度只覆盖 pending，不再起第二个任务。"""
    E._MEM_EXTRACT_STATE[904] = {"running": True, "pending": None}

    await E._spawn_memory_extract(
        None, session_id=904, turn_id=7,
        prompt="请帮我修复这个问题并解释原因", summary="done",
        auto_memory_enabled=True, last_extracted_turn_id=6, project_id=3,
    )

    st = E._MEM_EXTRACT_STATE[904]
    assert st["running"] is True
    assert st["pending"] is not None
    assert st["pending"]["turn_id"] == 7
    assert st["pending"]["project_id"] == 3
    # 没有多起任务
    assert 904 not in E._MEM_EXTRACT_TASKS
