"""plan-20261109-compact-structure：签名思考必须原样保存，文本推理不能冒充签名块。"""
from copy import deepcopy


def replay_thinking(blocks):
    return [deepcopy(b) for b in (blocks or []) if
            (b.get("type") == "thinking" and b.get("signature")) or
            (b.get("type") == "redacted_thinking" and b.get("data"))]


def collect_thinking(slots, event, payload):
    if not isinstance(payload, dict):
        return
    index = int(payload.get("index") or 0)
    if event == "content_block_start":
        block = payload.get("content_block") or {}
        if block.get("type") in ("thinking", "redacted_thinking"):
            slots[index] = deepcopy(block)
    elif event == "content_block_delta" and index in slots:
        delta = payload.get("delta") or {}
        key = {"thinking_delta": "thinking", "signature_delta": "signature"}.get(delta.get("type"))
        if key:
            slots[index][key] = slots[index].get(key, "") + delta.get(key, "")
