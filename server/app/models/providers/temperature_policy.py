"""Claude 系列模型的采样温度策略（统一收敛）。

问题背景：模型名中携带 claude 字样时（如 claude-haiku-5-5），只接受默认
temperature，携带 agent 循环下发的 0.3/0.7 等非默认值会被网关直接拒绝：

    Error code: 400 - {'error': {'message': 'claude-haiku-5-5 does not support
    non-default temperature', 'type': 'invalid_request_error'}}

各 Provider 此前各自把请求温度原样透传，导致同一模型在 OpenAI 兼容 /
Anthropic / workbuddy / ta3 / commandcode 等通道下行为不一致。本模块把
「是否可下发非默认温度」收敛到一处，判定口径为**模型名任一来源包含
claude**（大小写不敏感，兼容 "kr/claude-opus-5.5"、"anthropic/claude-*"、
"Claude-Sonnet-5.5" 等带前缀/变体的目录 id）：

- 命中 claude：不下发非默认温度（字段缺省即默认 1.0）；
- 其它模型：保持原有请求值优先的行为；
- 网关仍以该错误拒绝（模型别名未识别）时，调用方可用
  is_temperature_rejected() 识别后移除字段重试一次。
"""
from __future__ import annotations

DEFAULT_TEMPERATURE = 1.0

# 模型名中出现即视为 Claude 系（子串匹配，不限定前缀/后缀）。
_CLAUDE_MARKER = "claude"
# Anthropic 品牌名同样按系处理（覆盖 "anthropic/*" 这类不带 claude 的 id）。
_ANTHROPIC_MARKERS = ("claude", "anthropic")


def is_claude_model(*names: str | None) -> bool:
    """模型名中是否携带 claude 字样（忽略大小写与供应商前缀）。"""
    return any(_CLAUDE_MARKER in (name or "").strip().lower() for name in names)


def is_anthropic_family(
    model_name: str | None,
    base_url: str | None = None,
    *,
    extra_names: tuple[str | None, ...] = (),
) -> bool:
    """判断模型是否属于 Claude/Anthropic 系（按多个模型名来源 + 基址）。

    extra_names 用于传入目录标题、别名等其它模型名来源——任一来源含
    claude 即视为命中，避免 request.model 与配置模型名不一致时漏判。
    """
    names = (model_name, *extra_names)
    if any(
        marker in (name or "").strip().lower()
        for name in names
        for marker in _ANTHROPIC_MARKERS
    ):
        return True
    return "api.anthropic.com" in (base_url or "").strip().lower()


def pick_temperature(
    requested: float | None,
    *,
    model_name: str | None,
    base_url: str | None = None,
    catalog_default: float | None = None,
    extra_names: tuple[str | None, ...] = (),
    non_claude_default: float | None = None,
) -> float | None:
    """返回实际应下发的温度；None 表示不携带该字段（由网关/模型走默认值）。

    - 命中 claude：只接受默认温度，忽略 requested 的 0.3/0.7；
      目录明确声明的默认值仍可下发（保持官方 CLI 的请求形态）。
    - 其它模型：请求值优先，其次目录值，最后 non_claude_default。
    """
    if is_anthropic_family(model_name, base_url, extra_names=extra_names):
        if catalog_default is not None and _is_default(catalog_default):
            return float(catalog_default)
        return None
    if requested is not None:
        return requested
    if catalog_default is not None:
        return catalog_default
    return non_claude_default


def scrub_temperature(
    payload: dict,
    *,
    model_name: str | None,
    base_url: str | None = None,
    extra_names: tuple[str | None, ...] = (),
) -> None:
    """就地移除 Claude 系请求体里的非默认温度。"""
    value = payload.get("temperature")
    if value is None:
        return
    if (is_anthropic_family(model_name, base_url, extra_names=extra_names)
            and not _is_default(value)):
        payload.pop("temperature", None)


def is_temperature_rejected(status_code: int | None, message: str | None) -> bool:
    """网关是否因「不支持非默认 temperature」拒绝请求（400）。

    用于静态识别不到的模型别名兜底：命中后移除 temperature 重试一次。
    """
    if status_code != 400:
        return False
    msg = (message or "").lower()
    if "temperature" not in msg:
        return False
    return any(
        marker in msg
        for marker in ("non-default", "default temperature", "does not support",
                       "not support", "unsupported")
    )


def _is_default(value: float | int | str) -> bool:
    try:
        return abs(float(value) - DEFAULT_TEMPERATURE) < 1e-6
    except (TypeError, ValueError):
        return False
