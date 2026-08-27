"""workbuddy thinking 参数翻译（对齐 CodeBuddy CLI ThinkingEffortTranslatorRule）。"""
from app.models.providers.workbuddy_model_caps import get_model_caps, map_reasoning_effort


def test_deepseek_caps():
    caps = get_model_caps("deepseek-v4-pro")
    assert caps["thinking_format"] == "deepseek"
    assert caps["thinking_level_map"]["xhigh"] == "max"


def test_glm_caps_uses_zai_format():
    caps = get_model_caps("glm-5.3")
    assert caps["thinking_format"] == "zai"


def test_kimi_caps_disables_reasoning_effort():
    caps = get_model_caps("kimi-k3-1")
    assert caps.get("supports_reasoning_effort") is False


def test_unknown_model_empty_caps():
    assert get_model_caps("hy3") == {}
    assert get_model_caps("minimax-m3") == {}
    assert get_model_caps("auto") == {}
    assert get_model_caps("") == {}


def test_effort_mapping_via_level_map():
    caps = get_model_caps("deepseek-v4-pro")
    assert map_reasoning_effort("high", caps) == "high"
    assert map_reasoning_effort("xhigh", caps) == "max"
    # 映射值为 None（该模型不支持此档位）→ 删除字段
    assert map_reasoning_effort("low", caps) is None
    assert map_reasoning_effort("medium", caps) is None


def test_effort_fallback_without_level_map():
    assert map_reasoning_effort("high", {}) == "high"
    # LegacyXhighFallbackRule：无档位映射时 xhigh 降级 high
    assert map_reasoning_effort("xhigh", {}) == "high"
    assert map_reasoning_effort(None, {}) is None
