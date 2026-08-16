"""v2.2 (对齐 zcode 3.11): 内置模型目录。

扫描导入模型时自动补全元数据（context_window / 多模态 / reasoning 档位），
替代手工填写 context_window。数据源为公开资料 + 本地已知配置，保守取值。

匹配策略（lookup）：
1. 完全匹配；
2. 归一化匹配（大小写/连字符）；
3. 前缀匹配（model 名去掉日期/版本后缀后匹配目录键）。
"""

from __future__ import annotations

import re

# 目录项: {context_window, multimodal, reasoning_efforts}
# context_window=None 表示未知（不覆盖）
MODEL_CATALOG: dict[str, dict] = {
    # ── OpenAI ──
    "gpt-4o": {"context_window": 128000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gpt-4o-mini": {"context_window": 128000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gpt-4.1": {"context_window": 1047576, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gpt-4.1-mini": {"context_window": 1047576, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gpt-4.1-nano": {"context_window": 1047576, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "o3": {"context_window": 200000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "o4-mini": {"context_window": 200000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gpt-5": {"context_window": 400000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gpt-5-mini": {"context_window": 400000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    # ── Anthropic ──
    "claude-3-5-sonnet": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-3-5-haiku": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-3-7-sonnet": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-4-sonnet": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-4-opus": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-4-5-sonnet": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-4-5-haiku": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-opus-4-1": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-sonnet-4-5": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    "claude-haiku-4-5": {"context_window": 200000, "multimodal": True, "reasoning_efforts": []},
    # ── Google ──
    "gemini-1.5-pro": {"context_window": 2097152, "multimodal": True, "reasoning_efforts": []},
    "gemini-1.5-flash": {"context_window": 1048576, "multimodal": True, "reasoning_efforts": []},
    "gemini-2.0-flash": {"context_window": 1048576, "multimodal": True, "reasoning_efforts": []},
    "gemini-2.5-pro": {"context_window": 1048576, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gemini-2.5-flash": {"context_window": 1048576, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "gemini-3-pro": {"context_window": 1048576, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    # ── 智谱 GLM ──
    "glm-4": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "glm-4-flash": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "glm-4-plus": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "glm-4-air": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "glm-4-long": {"context_window": 1000000, "multimodal": False, "reasoning_efforts": []},
    "glm-4.5": {"context_window": 128000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "glm-4.5-air": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "glm-4.6": {"context_window": 200000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "glm-4.7": {"context_window": 200000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "glm-5.1": {"context_window": 200000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "glm-5.2": {"context_window": 200000, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    # ── DeepSeek ──
    "deepseek-chat": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "deepseek-reasoner": {"context_window": 128000, "multimodal": False, "reasoning_efforts": ["low", "medium", "high"]},
    "deepseek-v3": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "deepseek-v3.1": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "deepseek-v3.2": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "deepseek-r1": {"context_window": 128000, "multimodal": False, "reasoning_efforts": ["low", "medium", "high"]},
    # ── Qwen ──
    "qwen-max": {"context_window": 131072, "multimodal": False, "reasoning_efforts": []},
    "qwen-plus": {"context_window": 131072, "multimodal": False, "reasoning_efforts": []},
    "qwen-turbo": {"context_window": 131072, "multimodal": False, "reasoning_efforts": []},
    "qwen-long": {"context_window": 10000000, "multimodal": False, "reasoning_efforts": []},
    "qwen2.5-72b-instruct": {"context_window": 131072, "multimodal": False, "reasoning_efforts": []},
    "qwen3-235b": {"context_window": 131072, "multimodal": False, "reasoning_efforts": ["low", "medium", "high"]},
    "qvq-max": {"context_window": 131072, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    "qwen-vl-max": {"context_window": 131072, "multimodal": True, "reasoning_efforts": []},
    "qwen3-vl-plus": {"context_window": 262144, "multimodal": True, "reasoning_efforts": []},
    "qwen3-vl-max": {"context_window": 262144, "multimodal": True, "reasoning_efforts": []},
    # ── Kimi / Moonshot ──
    "moonshot-v1-8k": {"context_window": 8000, "multimodal": False, "reasoning_efforts": []},
    "moonshot-v1-32k": {"context_window": 32000, "multimodal": False, "reasoning_efforts": []},
    "moonshot-v1-128k": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "kimi-k2": {"context_window": 131072, "multimodal": False, "reasoning_efforts": ["low", "medium", "high"]},
    "kimi-k2.5": {"context_window": 131072, "multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
    # ── MiniMax ──
    "minimax-text-01": {"context_window": 1000000, "multimodal": False, "reasoning_efforts": []},
    "abab6.5s-chat": {"context_window": 245000, "multimodal": False, "reasoning_efforts": []},
    # ── 其他国内 ──
    "ernie-4.0-turbo": {"context_window": 128000, "multimodal": True, "reasoning_efforts": []},
    "hunyuan-turbos": {"context_window": 32000, "multimodal": False, "reasoning_efforts": []},
    "doubao-1.5-pro-32k": {"context_window": 32000, "multimodal": True, "reasoning_efforts": []},
    "doubao-1.5-pro-256k": {"context_window": 256000, "multimodal": True, "reasoning_efforts": []},
    "doubao-seed-1.6": {"context_window": 256000, "multimodal": True, "reasoning_efforts": []},
    # ── 本地/开源（ollama/vllm 常见名）──
    "qwen2.5-coder-32b": {"context_window": 32768, "multimodal": False, "reasoning_efforts": []},
    "deepseek-coder-v2": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "llama3.1-70b": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "llama3.3-70b": {"context_window": 128000, "multimodal": False, "reasoning_efforts": []},
    "llama4": {"context_window": 1000000, "multimodal": True, "reasoning_efforts": []},
    "codestral": {"context_window": 256000, "multimodal": False, "reasoning_efforts": []},
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.\s]+", "", name).lower()


def lookup(model_name: str) -> dict | None:
    """按名称查目录。命中返回元数据副本；未命中返回 None。"""
    if not model_name:
        return None
    name = model_name.strip()
    key = _normalize(name)

    # 1. 完全归一化匹配
    for k, v in MODEL_CATALOG.items():
        if _normalize(k) == key:
            return dict(v)

    # 2. 前缀匹配（目录键是 model 名的前缀：如 glm-4.6-0613 → glm-4.6）
    best: tuple[int, dict] | None = None
    for k, v in MODEL_CATALOG.items():
        nk = _normalize(k)
        if key.startswith(nk) and (best is None or len(nk) > len(_normalize(best[1].get("_k", "") or ""))):
            # 用最长前缀匹配
            best = (len(nk), {**v, "_k": k})
    if best is not None and best[0] >= 6:  # 前缀至少 6 个归一化字符，防止误匹配
        out = dict(best[1])
        out.pop("_k", None)
        return out
    return None


def apply_metadata(model_name: str, *, context_window: int | None,
                   is_multimodal: bool, reasoning_efforts: list | None) -> dict:
    """用目录元数据补全扫描结果缺口（不覆盖已有值）。

    返回 {"context_window", "is_multimodal", "reasoning_efforts"}。
    """
    meta = lookup(model_name)
    if meta is None:
        return {
            "context_window": context_window,
            "is_multimodal": is_multimodal,
            "reasoning_efforts": reasoning_efforts,
        }
    return {
        "context_window": context_window if context_window else meta.get("context_window"),
        "is_multimodal": is_multimodal or bool(meta.get("multimodal")),
        "reasoning_efforts": reasoning_efforts if reasoning_efforts else (meta.get("reasoning_efforts") or []),
    }
