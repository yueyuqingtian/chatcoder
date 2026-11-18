"""各协议仅在支持的请求面设置缓存参数。"""
from hashlib import sha256
from urllib.parse import urlsplit


def official_host(url: str, host: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == host


def openai_cache_key(session: str | None) -> str | None:
    if not session:
        return None
    return "chatcoder-" + sha256(session.encode("utf-8")).hexdigest()[:32]


def _mark(blocks: list[dict]) -> list[dict]:
    return [*blocks[:-1], {**blocks[-1], "cache_control": {"type": "ephemeral"}}]


def mark_anthropic_cache(payload: dict) -> None:
    """plan-20261118-runtime-question-cache：优先命中系统和已闭合历史，按协议最多使用四个断点。"""
    system = payload.get("system")
    if isinstance(system, str) and system:
        payload["system"] = _mark([{"type": "text", "text": system}])
    if payload.get("tools"):
        payload["tools"] = _mark(payload["tools"])
    messages = payload.get("messages") or []
    eligible = [i for i, row in enumerate(messages) if row["role"] == "user" and row["content"]]
    for i in eligible[-2:]:
        row = messages[i]
        messages[i] = {**row, "content": _mark(row["content"])}
