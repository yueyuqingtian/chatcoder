"""plan-20261118-runtime-question-cache：缓存未上报与供应商明确返回零分别处理。"""
from collections.abc import Mapping

from app.models.schemas import Usage


def _number(*values) -> int | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def openai_usage(raw) -> Usage:
    if not isinstance(raw, Mapping):
        raw = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else {}
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    cached = _number(
        prompt_details.get("cached_tokens"), raw.get("prompt_cache_hit_tokens"),
        raw.get("cached_tokens"), raw.get("cachedContentTokenCount"),
    )
    prompt = _number(raw.get("prompt_tokens"), raw.get("promptTokenCount")) or 0
    completion = _number(raw.get("completion_tokens"), raw.get("candidatesTokenCount")) or 0
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=_number(raw.get("total_tokens"), raw.get("totalTokenCount")) or prompt + completion,
        cached_input_tokens=cached or 0,
        cache_read_reported=cached is not None,
        reasoning_tokens=_number(completion_details.get("reasoning_tokens"), raw.get("thoughtsTokenCount")) or 0,
    )


def anthropic_usage(raw: Mapping) -> Usage:
    cached = _number(raw.get("cache_read_input_tokens"))
    creation = _number(raw.get("cache_creation_input_tokens")) or 0
    prompt = (_number(raw.get("input_tokens")) or 0) + creation + (cached or 0)
    completion = _number(raw.get("output_tokens")) or 0
    details = raw.get("output_tokens_details") or {}
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cached_input_tokens=cached or 0,
        cache_read_reported=cached is not None,
        reasoning_tokens=_number(details.get("thinking_tokens")) or 0,
    )
