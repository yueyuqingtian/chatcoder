"""启动还原、Anthropic 输出上限兜底与执行模式能力矩阵的单测。"""
import pytest

from app.core.config import settings
from app.gateway.routers import settings as settings_router
from app.models.providers.anthropic import _official_default_max_tokens
from app.orchestration.subagent_capabilities import (
    CAP_EXPLORE_AGENT,
    MODE_AGENT,
    MODE_PLAN,
    MODE_READONLY,
    MODE_WORKFLOW,
    capabilities_for,
)


# ───────────────── 启动还原 ─────────────────


def test_saved_operator_effort_survives_restart(monkeypatch):
    """用户保存的操控思考深度必须被还原到运行时（plan-549-2281 改名后仍成立）。"""
    monkeypatch.setattr(settings_router, "_load_config", lambda: {
        "operator_subagent_effort": "low", "subagent_enabled": True,
    })
    monkeypatch.setattr(settings_router, "_save_config", lambda data: None)
    monkeypatch.setattr(settings, "operator_subagent_effort", "lowest")
    settings_router.load_persisted_workspace()
    assert settings.operator_subagent_effort == "low"


def test_legacy_operator_effort_key_migrates_to_new_key(monkeypatch):
    """plan-549-2281：旧配置只有 operator_reasoning_effort 时，一次性映射到新键。"""
    monkeypatch.setattr(settings_router, "_load_config", lambda: {
        "operator_reasoning_effort": "none",
    })
    monkeypatch.setattr(settings_router, "_save_config", lambda data: None)
    monkeypatch.setattr(settings, "operator_subagent_effort", "lowest")
    settings_router.load_persisted_workspace()
    assert settings.operator_subagent_effort == "none"


def test_legacy_approval_timeout_key_migrates_to_new_key(monkeypatch):
    """plan-549-2281：approval_timeout_sec_subsession 旧键映射到 approval_timeout_sec_child。"""
    monkeypatch.setattr(settings_router, "_load_config", lambda: {
        "approval_timeout_sec_subsession": 120,
    })
    monkeypatch.setattr(settings_router, "_save_config", lambda data: None)
    monkeypatch.setattr(settings, "approval_timeout_sec_child", 0)
    settings_router.load_persisted_workspace()
    assert settings.approval_timeout_sec_child == 120


def test_missing_switch_keys_are_backfilled(monkeypatch):
    """旧配置缺键时补写子代理开关，且不覆盖已有的 false；已下线的键不再写入。"""
    saved: dict = {}
    monkeypatch.setattr(settings_router, "_load_config", lambda: {"subagent_enabled": False})
    monkeypatch.setattr(settings_router, "_save_config", lambda data: saved.update(data))
    settings_router.load_persisted_workspace()
    assert saved.get("subagent_enabled") is False  # 已存在的 false 保留
    # plan-549-2281：子会话开关已删除，不再补写
    assert "subsession_enabled" not in saved
    assert "parallel_subsession_hint" not in saved


def test_approval_explain_model_restored_on_startup(monkeypatch):
    monkeypatch.setattr(settings_router, "_load_config", lambda: {
        "approval_explain_model_id": 7, "approval_explain_reasoning_effort": "low",
    })
    monkeypatch.setattr(settings_router, "_save_config", lambda data: None)
    monkeypatch.setattr(settings, "approval_explain_model_id", None)
    monkeypatch.setattr(settings, "approval_explain_reasoning_effort", "")
    settings_router.load_persisted_workspace()
    assert settings.approval_explain_model_id == 7
    assert settings.approval_explain_reasoning_effort == "low"


def test_agent_sampling_settings_restored_and_clamped(monkeypatch):
    monkeypatch.setattr(settings_router, "_load_config", lambda: {
        "agent_tool_temperature": 9.0, "agent_text_temperature": 0.5, "agent_max_output_tokens": 4096,
    })
    monkeypatch.setattr(settings_router, "_save_config", lambda data: None)
    monkeypatch.setattr(settings, "agent_tool_temperature", 0.3)
    monkeypatch.setattr(settings, "agent_text_temperature", 0.7)
    monkeypatch.setattr(settings, "agent_max_output_tokens", 0)
    settings_router.load_persisted_workspace()
    assert settings.agent_tool_temperature == 2.0  # 夹紧到 0–2
    assert settings.agent_text_temperature == 0.5
    assert settings.agent_max_output_tokens == 4096


# ───────────────── 全局设置：memory_enabled 落盘与运行时同步 ─────────────────


async def test_memory_enabled_persists_and_syncs_runtime(monkeypatch):
    """plan-549-2281：memory_enabled 原先在 set_global_settings 里写两遍，
    收成一处理后，必须仍然「落盘」且「同步运行时」两者都在。"""
    saved: dict = {}
    monkeypatch.setattr(settings_router, "_load_config", lambda: {})
    monkeypatch.setattr(settings_router, "_save_config", lambda data: saved.update(data))
    monkeypatch.setattr(settings, "auto_memory_enabled", True)

    body = settings_router.GlobalSettingsIn(memory_enabled=False)
    out = await settings_router.set_global_settings(body)

    assert saved.get("memory_enabled") is False, "memory_enabled 未落盘"
    assert settings.auto_memory_enabled is False, "memory_enabled 未同步运行时"
    assert out.memory_enabled is False, "响应未反映本次修改"


async def test_memory_enabled_absent_leaves_runtime_untouched(monkeypatch):
    """未传 memory_enabled 时不得改写已落盘值与运行时（避免默认值覆盖用户选择）。"""
    saved: dict = {"memory_enabled": False}
    monkeypatch.setattr(settings_router, "_load_config", lambda: dict(saved))
    monkeypatch.setattr(settings_router, "_save_config", lambda data: saved.update(data))
    monkeypatch.setattr(settings, "auto_memory_enabled", False)

    await settings_router.set_global_settings(settings_router.GlobalSettingsIn(language="zh"))

    assert saved["memory_enabled"] is False
    assert settings.auto_memory_enabled is False


# ───────────────── Anthropic 输出上限兜底 ─────────────────


@pytest.mark.parametrize("model", [
    "claude-opus-5-5",
    "claude-haiku-5-5-20260101",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "",
])
def test_official_default_max_tokens_is_128k_for_all_models(model):
    """官方直连未显式配置输出上限时统一兜底为软件输出上限 128K。"""
    assert _official_default_max_tokens(model) == 128_000


# ───────────────── 执行模式能力矩阵 ─────────────────


def test_readonly_allows_subagent():
    """plan-549-2281：子会话能力已下线，四个模式的能力集只剩探索子代理。"""
    caps = capabilities_for(MODE_READONLY)
    assert CAP_EXPLORE_AGENT in caps


def test_plan_allows_subagent_only():
    caps = capabilities_for(MODE_PLAN)
    assert caps == {CAP_EXPLORE_AGENT}


def test_agent_allows_subagent_only():
    """原先 agent 额外拥有「执行子会话」能力，现由子代理的可写 mode 承担。"""
    caps = capabilities_for(MODE_AGENT)
    assert caps == {CAP_EXPLORE_AGENT}


def test_workflow_allows_subagent_only():
    caps = capabilities_for(MODE_WORKFLOW)
    assert caps == {CAP_EXPLORE_AGENT}
