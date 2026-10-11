"""kiro 模型目录同步。

Kiro 服务端没有公开的「列出模型」HTTP 端点：客户端模型列表在会话建立时下发，
CLI 侧为 ``kiro-cli chat --list-models``。因此本模块以**内置全量映射表**为目录
基线（这也正是「放开地区限制」的一环——不依赖分区下发的受限列表），并预留
``discovered`` 参数以合并运行期探测到的服务端模型。

内置表取自 claude-kiro.js 的映射与窗口表，并补充本机账号实际可用的新版本模型。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.model_reg import Model

logger = logging.getLogger(__name__)

# Kiro 模型占位 Key（OAuth 账号型：真实 token 由 kiro_auth 表按凭据动态提供）
KIRO_SESSION_KEY = "__kiro_session__"

# 客户端别名 → 服务端 modelId（点为分隔符，与 claude-kiro.js FULL_MODEL_MAPPING 一致）
ALIAS_TO_SERVER: dict[str, str] = {
    "claude-haiku-4-5": "claude-haiku-4.5",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-opus-4-5-20251101": "claude-opus-4.5",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-opus-4-7": "claude-opus-4.7",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4.5",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-sonnet-4-20250514": "claude-sonnet-4.0",
    "claude-3-7-sonnet-20250219": "claude-3.7-sonnet",
}

_REASONING = ["low", "medium", "high", "max"]

# 内置模型目录：name 为展示名（即服务端 ID），model_id 为下发值
BUILTIN_KIRO_MODELS: list[dict] = [
    {"name": "claude-opus-5.5", "model_id": "claude-opus-5.5", "context_window": 1000000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
    {"name": "claude-sonnet-5.5", "model_id": "claude-sonnet-5.5", "context_window": 1000000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
    {"name": "claude-opus-4.7", "model_id": "claude-opus-4.7", "context_window": 1000000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
    {"name": "claude-opus-4.6", "model_id": "claude-opus-4.6", "context_window": 1000000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
    {"name": "claude-opus-4.5", "model_id": "claude-opus-4.5", "context_window": 1000000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
    {"name": "claude-sonnet-4.6", "model_id": "claude-sonnet-4.6", "context_window": 200000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
    {"name": "claude-sonnet-4.5", "model_id": "claude-sonnet-4.5", "context_window": 200000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
    {"name": "claude-haiku-4.5", "model_id": "claude-haiku-4.5", "context_window": 200000,
     "is_multimodal": True, "vendor": "anthropic", "reasoning_efforts": _REASONING},
]


def normalize_model_id(name: str | None) -> str:
    """把用户/别名写法折算为服务端 modelId；未知名原样保留（允许前向兼容新模型）。"""
    value = (name or "").strip()
    if not value:
        return ""
    if value in ALIAS_TO_SERVER:
        return ALIAS_TO_SERVER[value]
    # 已是服务端风格（带点）或未知：直接返回
    return value


def builtin_entries() -> list[dict]:
    """内置目录副本（调用方不得就地改动模块常量）。"""
    return [dict(entry) for entry in BUILTIN_KIRO_MODELS]


def merge_entries(discovered: list[str] | None = None) -> list[dict]:
    """合并「服务端探测列表 ∪ 内置表」。

    内置表打底；服务端多出来的 id 追加为条目（窗口/多模态给保守默认值），
    保证分区下发的额外模型也可被选用，而内置表确保常用模型不会因分区过滤而丢失。
    """
    entries = builtin_entries()
    known = {e["model_id"] for e in entries}
    for raw in discovered or []:
        model_id = normalize_model_id(raw)
        if not model_id or model_id in known:
            continue
        known.add(model_id)
        entries.append({
            "name": model_id,
            "model_id": model_id,
            "context_window": 200000,
            "is_multimodal": False,
            "vendor": "kiro",
            "reasoning_efforts": None,
        })
    return entries


async def sync_kiro_models(
    db: AsyncSession, provider, *, discovered: list[str] | None = None,
) -> list[dict]:
    """把目录 upsert 到 Model 表（写引擎单写线程）；返回本次同步的条目列表。

    - OAuth 账号型：Model.api_key 存占位符，真实 token 由 kiro_auth 动态提供；
    - 服务端 modelId 记入 kiro_meta.model_id，供 provider 构造请求体时读取。
    """
    from app.persistence.database import run_write_locked

    provider_id = provider.id
    entries = merge_entries(discovered)

    def patch(s):
        existing = list(s.execute(
            select(Model).where(Model.provider_id == provider_id)
        ).scalars().all())
        by_name = {m.name: m for m in existing}
        for e in entries:
            name = e["name"]
            m = by_name.get(name)
            if m is None:
                m = Model(tenant_id=1, name=name, provider_id=provider_id, source_type="byok")
                s.add(m)
                by_name[name] = m
            m.api_format = "kiro"
            m.provider = "kiro"
            m.is_active = True
            if not m.api_key:
                m.api_key = KIRO_SESSION_KEY
            meta = dict(m.kiro_meta or {})
            meta["model_id"] = e["model_id"]
            meta["vendor"] = e.get("vendor") or "kiro"
            m.kiro_meta = meta
            if e.get("context_window"):
                m.context_window = e["context_window"]
            m.is_multimodal = bool(e.get("is_multimodal", False))
            if e.get("reasoning_efforts") is not None:
                m.reasoning_efforts = e["reasoning_efforts"]
        s.flush()
        s.commit()

    await run_write_locked(patch, label=f"kiro.catalog.sync.{provider_id}")
    return entries


def resolve_server_model(model: Model | None, fallback: str = "") -> str:
    """由 Model 行解析出应下发的服务端 modelId（优先 kiro_meta，其次按名折算）。"""
    if model is None:
        return normalize_model_id(fallback)
    meta = getattr(model, "kiro_meta", None) or {}
    model_id = str(meta.get("model_id") or "").strip()
    if model_id:
        return model_id
    return normalize_model_id(model.name or fallback)
