"""ta3 目录多模态判定单测（plan-147-674）。

覆盖：
- _is_multimodal_model：显式字段候选（isMultimodal/multimodal/supportsImages/vision/
  displayConfig.multimodal）优先，字段缺失时 identity 视觉关键词兜底
- _parse_config_models：assistant 配置解析出的条目携带正确 is_multimodal
"""
from app.auth.ta3.catalog import _is_multimodal_model, _parse_config_models


def test_explicit_field_is_multimodal_true():
    assert _is_multimodal_model({"isMultimodal": True, "model": "custom-x"}) is True


def test_explicit_field_false_beats_identity():
    """显式字段优先：上游明确 multimodal=False 时即使名字像视觉模型也不兜底。"""
    assert _is_multimodal_model({"isMultimodal": False, "model": "gpt-4o"}) is False


def test_supports_images_field():
    assert _is_multimodal_model({"supportsImages": True, "model": "x"}) is True


def test_display_config_multimodal():
    assert _is_multimodal_model({
        "model": "x", "displayConfig": {"multimodal": True},
    }) is True


def test_identity_fallback_vision_models():
    for name in ("gpt-4o", "gpt-4.1-mini", "claude-sonnet-4", "gemini-2.5-pro",
                 "qwen3-vl-max", "glm-4v-flash", "glm-5", "glm-5.3",
                 "doubao-seed-vision", "doubao-seed-2.1-pro", "pixtral-large",
                 "minimax-m3"):
        assert _is_multimodal_model({"model": name}) is True, name


def test_identity_fallback_text_models():
    for name in ("deepseek-v4-flash", "kimi-k3", "qwen3.8-max", "minimax-text-01"):
        assert _is_multimodal_model({"model": name}) is False, name


def test_builtin_catalog_fallback():
    """显式字段缺失、身份正则不覆盖时，内置目录兜底（plan-156-739）。

    内置目录已收录的模型按目录 multimodal 值判定，无需依赖身份正则。
    """
    assert _is_multimodal_model({"model": "glm-5.2"}) is True       # 内置目录：glm-5.2 True
    assert _is_multimodal_model({"model": "deepseek-v4-pro"}) is False  # 内置目录：False


def test_parse_config_models_carries_multimodal():
    """assistant 配置解析：显式字段与 identity 兜底两条路径都落到条目。"""
    assistant = {
        "configResult": {"config": {"models": [
            {"model": "custom-vlm", "isMultimodal": True},
            {"model": "gpt-4o"},
            {"model": "deepseek-v4"},
        ]}},
    }
    entries = _parse_config_models(assistant)
    by_name = {e["name"]: e for e in entries}
    assert by_name["custom-vlm"]["is_multimodal"] is True   # 显式字段
    assert by_name["gpt-4o"]["is_multimodal"] is True       # identity 兜底
    assert by_name["deepseek-v4"]["is_multimodal"] is False


def test_parse_config_models_carries_multimodal():
    """assistant 配置解析：显式字段与 identity 兜底两条路径都落到条目。"""
    assistant = {
        "configResult": {"config": {"models": [
            {"model": "custom-vlm", "isMultimodal": True},
            {"model": "gpt-4o"},
            {"model": "deepseek-v4"},
        ]}},
    }
    entries = _parse_config_models(assistant)
    by_name = {e["name"]: e for e in entries}
    assert by_name["custom-vlm"]["is_multimodal"] is True   # 显式字段
    assert by_name["gpt-4o"]["is_multimodal"] is True       # identity 兜底
    assert by_name["deepseek-v4"]["is_multimodal"] is False
