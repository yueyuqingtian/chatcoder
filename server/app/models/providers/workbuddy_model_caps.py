"""workbuddy 模型 thinking 参数翻译表（对齐 CodeBuddy CLI 内置模型注册表 caps）。

CLI 用内置注册表按模型 id 匹配 caps（thinkingLevelMap / compat.thinkingFormat），
再由 ThinkingFormatTranslatorRule 把 reasoning_effort 翻译为网关可识别的参数：
- deepseek 系：thinking: {type: "enabled"} + reasoning_effort 档位映射
  （thinkingLevelMap: {minimal: null, low: null, medium: null, high: "high", xhigh: "max"}）
- zai（GLM 系）：enable_thinking: true，不传 reasoning_effort
- kimi 系：supportsReasoningEffort=false，不传 reasoning_effort
- 未知模型：reasoning_effort 原样透传，xhigh 降级为 high（LegacyXhighFallbackRule）

此表仅做前缀匹配；模型更新时在此补充即可。
"""
from __future__ import annotations

# (模型 id 前缀, caps)
_MODEL_CAPS: dict[str, dict] = {
    "deepseek": {
        "thinking_format": "deepseek",
        "thinking_level_map": {
            "minimal": None, "low": None, "medium": None,
            "high": "high", "xhigh": "max",
        },
    },
    "glm": {
        "thinking_format": "zai",
        "zai_tool_stream": True,
    },
    "kimi": {
        "supports_reasoning_effort": False,
    },
    # hy3（混元）/ minimax / auto 等：无特殊 caps → 标准 reasoning_effort 透传
}

_EMPTY_CAPS: dict = {}


def get_model_caps(model_id: str) -> dict:
    """按模型 id 前缀匹配 caps；未知模型返回空 dict（标准 reasoning_effort 透传）。"""
    if not model_id:
        return _EMPTY_CAPS
    for prefix, caps in _MODEL_CAPS.items():
        if model_id.startswith(prefix):
            return caps
    return _EMPTY_CAPS


def map_reasoning_effort(effort: str | None, caps: dict) -> str | None:
    """按 thinkingLevelMap 映射 effort；映射值为 None（不支持该档位）→ 删除字段。"""
    if not effort:
        return None
    level_map = caps.get("thinking_level_map")
    if isinstance(level_map, dict) and effort in level_map:
        return level_map[effort]
    # LegacyXhighFallbackRule：无档位映射时 xhigh 降级 high
    if effort == "xhigh":
        return "high"
    return effort
