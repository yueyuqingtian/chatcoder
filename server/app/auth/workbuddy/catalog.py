"""workbuddy 模型目录同步（对齐 CodeBuddy CLI CloudProductManager：GET /v3/config）。

端点：GET {endpoint}/v3/config（Bearer + X-User-Id + X-Domain + 网关头）
响应：完整产品配置 JSON，含 models[]（50 个）与 agents[]（cli agent 挂对话模型）。

同步策略：
- 只保留对话模型：cli agent 的 models 列表 ∪ tags 含 "craft" 的模型
- 排除用户自定义模型（id 以 "custom-local:" 前缀，来自 ~/.workbuddy/models.json）
- 401 时自动 refresh 一次后重试（refresh_token 轮转防并发由 session 锁保证）
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.workbuddy import session as wb_session
from app.core.config import settings

logger = logging.getLogger(__name__)

CONFIG_PATH = "/v3/config"
_CATALOG_TIMEOUT = 12.0

# 官方模型支持完整思考档位时写入的档位列表（对齐当前项目 REASONING_OPTS）
_FULL_REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]


class WorkBuddyUnauthorized(Exception):
    """业务接口 401 —— 调用方应 refresh 后重试一次。"""


def _netloc(api_base: str) -> str:
    return urlparse(api_base).netloc or ""


def _config_headers(token: str, api_base: str, account: dict) -> dict:
    ua = getattr(settings, "workbuddy_user_agent", "") or "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"
    headers: dict = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Domain": _netloc(api_base),
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "workbuddy-desktop",
        "X-IDE-Version": "1.0.0",
        "X-Private-Data": "false",
        "User-Agent": ua,
    }
    uid = account.get("uid") or ""
    if uid:
        headers["X-User-Id"] = str(uid).strip()
    enterprise_id = account.get("enterpriseId") or ""
    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id).strip()
        headers["X-Tenant-Id"] = str(enterprise_id).strip()
    return headers


async def fetch_config_raw(api_base: str, token: str, account: dict) -> dict:
    """GET {api_base}/v3/config → 完整产品配置。401 抛 WorkBuddyUnauthorized。"""
    url = f"{api_base.rstrip('/')}{CONFIG_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT,
                                     headers={"Accept-Encoding": "gzip, deflate"}) as client:
            resp = await client.get(url, headers=_config_headers(token, api_base, account))
    except httpx.HTTPError as e:
        raise RuntimeError(f"WorkBuddy 配置拉取失败：{url}") from e
    if resp.status_code in (401, 403):
        raise WorkBuddyUnauthorized()
    if not resp.is_success:
        raise RuntimeError(f"WorkBuddy 配置拉取失败 http_{resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError(f"WorkBuddy 配置返回非 JSON：{resp.text[:200]}") from None
    if isinstance(body, dict) and body.get("data") and isinstance(body["data"], dict):
        return body["data"]
    if isinstance(body, dict) and isinstance(body.get("models"), list):
        return body
    raise RuntimeError("WorkBuddy 配置结构异常（缺 models 字段）")


def _keep_model(model: dict, keep_ids: set[str]) -> bool:
    mid = str(model.get("id") or "")
    if not mid or mid.startswith("custom-local:"):
        return False
    if mid in keep_ids:
        return True
    tags = model.get("tags") or []
    return "craft" in tags


def _parse_entry(model: dict) -> dict:
    """模型 → 可入库条目（对齐方案 §1.4 元数据结构）。"""
    reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else {}
    supports_reasoning = bool(model.get("supportsReasoning"))
    only_reasoning = bool(model.get("onlyReasoning"))
    temperature = model.get("temperature")
    return {
        "name": str(model.get("id") or ""),
        "title": model.get("name") or model.get("id") or "",
        "credits": model.get("credits") or "",
        "vendor": model.get("vendor") or "",
        "tags": model.get("tags") or [],
        "context_window": int(model.get("maxInputTokens") or 0) or None,
        "max_output_tokens": int(model.get("maxOutputTokens") or 0) or None,
        "is_multimodal": bool(model.get("supportsImages")),
        "supports_reasoning": supports_reasoning,
        "only_reasoning": only_reasoning,
        "reasoning": reasoning,
        "temperature": float(temperature) if isinstance(temperature, (int, float)) else None,
        "reasoning_efforts": list(_FULL_REASONING_EFFORTS) if supports_reasoning else [],
    }


def _default_reasoning_efforts(entry: dict) -> list:
    return entry["reasoning_efforts"]


async def sync_workbuddy_models(db: AsyncSession, provider, api_base: str) -> list[dict]:
    """同步 /v3/config → upsert Model 表，返回新增/更新条目。

    401 时自动 refresh 一次后重试。
    """
    from app.persistence.models.model_reg import Model

    auth = await wb_session.load_auth(db, provider.id)
    token = await wb_session.ensure_token(db, provider.id, api_base)
    account = auth.account if auth else {}

    try:
        config = await fetch_config_raw(api_base, token, account)
    except WorkBuddyUnauthorized:
        token = await wb_session.refresh_session(db, provider.id, api_base)
        config = await fetch_config_raw(api_base, token, account)

    models = config.get("models") or []
    agents = config.get("agents") or []
    if not isinstance(models, list):
        logger.warning("[workbuddy] provider=%s /v3/config 缺 models 列表", provider.id)
        return []

    # 对话模型 id 集合：cli agent 的 models ∪ craft tags
    keep_ids: set[str] = set()
    for agent in agents:
        if isinstance(agent, dict) and agent.get("name") == "cli":
            for mid in agent.get("models") or []:
                keep_ids.add(str(mid))
    for m in models:
        tags = m.get("tags") if isinstance(m, dict) else []
        if isinstance(tags, list) and "craft" in tags:
            keep_ids.add(str(m.get("id") or ""))

    entries: dict[str, dict] = {}
    for m in models:
        if not isinstance(m, dict) or not _keep_model(m, keep_ids):
            continue
        entry = _parse_entry(m)
        if entry["name"]:
            entries[entry["name"]] = entry

    if not entries:
        logger.warning("[workbuddy] provider=%s 配置未解析出对话模型（总模型=%d）",
                       provider.id, len(models))
        return []

    # upsert
    existing = (await db.execute(
        select(Model).where(Model.provider_id == provider.id)
    )).scalars().all()
    by_name = {m.name: m for m in existing}
    updated = 0
    created = 0
    for name, entry in entries.items():
        m = by_name.get(name)
        if m is None:
            m = Model(tenant_id=1, name=name, provider_id=provider.id, source_type="byok")
            db.add(m)
            by_name[name] = m
            created += 1
        m.api_key = "__workbuddy_session__"  # 占位：真实 token 由 registry 从 auth 表动态取
        m.base_url = api_base
        m.context_window = entry.get("context_window") or 200000
        m.is_multimodal = bool(entry.get("is_multimodal"))
        m.api_format = "workbuddy"
        efforts = entry.get("reasoning_efforts") or []
        m.reasoning_efforts = efforts if efforts else None
        # 必须复制新 dict 再赋回：SQLAlchemy 对可变 JSON 列赋同一对象引用
        # 不触发 change detection，原地 update 会导致 meta 更新不落库
        meta = dict(m.workbuddy_meta or {})
        meta.update({
            "title": entry.get("title") or name,
            "credits": entry.get("credits") or "",
            "vendor": entry.get("vendor") or "",
            "tags": entry.get("tags") or [],
            "maxOutputTokens": entry.get("max_output_tokens"),
            "supportsReasoning": entry.get("supports_reasoning"),
            "onlyReasoning": entry.get("only_reasoning"),
            "reasoning": entry.get("reasoning") or {},
            "temperature": entry.get("temperature"),
        })
        m.workbuddy_meta = meta
        updated += 1
    await db.flush()

    # 缓存目录原文（不含敏感字段）
    auth = await wb_session.get_auth_row(db, provider.id)
    auth.catalog = {
        "models": [
            {"id": e["name"], "title": e["title"], "credits": e["credits"],
             "contextWindow": e["context_window"], "reasoning": e["reasoning"]}
            for e in entries.values()
        ],
    }
    await db.flush()

    logger.info("[workbuddy] provider=%s 同步 %d 个对话模型（新增 %d，更新 %d）",
                provider.id, len(entries), created, updated)
    return [{"name": k, **v} for k, v in entries.items()]
