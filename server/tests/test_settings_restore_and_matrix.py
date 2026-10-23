"""启动还原、Anthropic 输出上限兜底与执行模式能力矩阵的单测。"""
import pytest

from app.core.config import settings
from app.gateway.routers import settings as settings_router
from app.models.providers.anthropic import _official_default_max_tokens
from app.orchestration.subagent_capabilities import (
    CAP_EXPLORE_AGENT,
    CAP_SUBSESSION,
    MODE_AGENT,
    MODE_PLAN,
    MODE_READONLY,
    MODE_WORKFLOW,
    capabilities_for,
)


# ───────────────── 启动还原 ─────────────────


def test_saved_false_subsession_survives_restart(monkeypatch):
    """用户保存的子会话 false 必须被还原到运行时，而不是回落为默认 true。"""
    monkeypatch.setattr(settings_router, "_load_config", lambda: {
        "subsession_enabled": False, "subagent_enabled": True,
    })
    monkeypatch.setattr(settings_router, "_save_config", lambda data: None)
    monkeypatch.setattr(settings, "subsession_enabled", True)
    settings_router.load_persisted_workspace()
    assert settings.subsession_enabled is False


def test_missing_switch_keys_are_backfilled(monkeypatch):
    """旧配置缺键时补写（子会话跟随旧的合并开关），且不覆盖已有的 false；不再写入已移除的并行提示开关。"""
    saved: dict = {}
    monkeypatch.setattr(settings_router, "_load_config", lambda: {"subagent_enabled": False})
    monkeypatch.setattr(settings_router, "_save_config", lambda data: saved.update(data))
    settings_router.load_persisted_workspace()
    assert saved.get("subagent_enabled") is False  # 已存在的 false 保留
    assert saved.get("subsession_enabled") is False  # 子会话开关跟随旧的合并开关
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


def test_readonly_allows_subagent_but_not_subsession():
    caps = capabilities_for(MODE_READONLY)
    assert CAP_EXPLORE_AGENT in caps
    assert CAP_SUBSESSION not in caps


def test_plan_allows_subagent_only():
    caps = capabilities_for(MODE_PLAN)
    assert CAP_EXPLORE_AGENT in caps and CAP_SUBSESSION not in caps


def test_agent_allows_subagent_and_subsession():
    caps = capabilities_for(MODE_AGENT)
    assert CAP_EXPLORE_AGENT in caps and CAP_SUBSESSION in caps


def test_workflow_allows_subagent_only():
    caps = capabilities_for(MODE_WORKFLOW)
    assert CAP_EXPLORE_AGENT in caps and CAP_SUBSESSION not in caps
