"""ta3 模型目录同步（对齐参考项目 authService.ts:484-586 fetchYinhaiCatalog）。

端点（按序尝试候选，首个成功即用）：
- 组织列表：GET {apiBase}/ide/list-organizations
            GET {apiBase}/ai/continue/ide/list-organizations (GET→POST)
- 各组织配置：GET {apiBase}/ide/list-assistants?organizationId={encoded}
             GET {apiBase}/ai/continue/ide/list-assistants?organizationId=
             POST {apiBase}/ai/continue/ide/list-assistants?appId=

鉴权：Authorization: <ide-session-…> 裸值（不带 Bearer）+ X-Client-Type: app
（参考项目 auth/http.ts:91-103 authHeaders 契约）
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import session as ta3_session

logger = logging.getLogger(__name__)

_CATALOG_TIMEOUT = 12.0
# 业务接口 401 时自动 refresh 后重试一次
_RETRY_ON_401 = True

_DEFAULT_ORG = {"id": "personal", "name": "Personal"}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _first_list(*values) -> list:
    for v in values:
        if isinstance(v, list):
            return v
    return []


def _first_text(*values) -> str:
    for v in values:
        if v:
            return str(v).strip()
    return ""


def _auth_headers(token: str) -> dict:
    return {"Authorization": token, "X-Client-Type": "app"}


def _request_name(model: dict) -> str:
    """模型请求名（对齐 modelClient.ts:26-34 getModelRequestName 优先级）。"""
    return _first_text(
        model.get("model"), model.get("modelName"), model.get("modelId"),
        model.get("deployment"), model.get("id"), model.get("name"), model.get("title"),
    ) or ""


def _context_window(completion_opts: dict, model: dict) -> int:
    """上下文窗口：defaultCompletionOptions/completionOptions 的 contextLength，缺省 200000。"""
    for key in ("contextLength", "contextWindow", "context_window", "maxContextTokens", "max_context_tokens"):
        v = completion_opts.get(key) or model.get(key)
        if isinstance(v, int) and v > 0:
            return v
    return 200000


def _is_anthropic(model: dict) -> bool:
    """协议判定（对齐 modelClient.ts:56-68 isAnthropicProtocolModel 的 identity 启发式）。"""
    identity = " ".join(str(model.get(k) or "") for k in (
        "provider", "providerName", "modelProvider", "model", "modelName", "modelId",
        "name", "title", "id",
    )).lower()
    api_base = str(model.get("apiBase") or "").lower()
    return (
        model.get("provider") in ("anthropic", "kimi")
        or any(kw in identity for kw in ("anthropic", "claude", "kimi"))
        or "/anthropic" in api_base
    )


def _extract_models(assistant: dict) -> list[dict]:
    """从 assistant 提取模型列表（对齐 auth/catalog.ts normalizeAssistant + modelsByRoleFromConfigModels）。"""
    config = assistant.get("configResult") or {}
    config = config.get("config") if isinstance(config, dict) else {}
    if not isinstance(config, dict):
        config = assistant.get("config") or {}
    models = _first_list(config.get("models"), assistant.get("models"))
    if models:
        return models
    models_by_role = config.get("modelsByRole") or assistant.get("modelsByRole") or {}
    out: list[dict] = []
    seen = set()
    if isinstance(models_by_role, dict):
        for role, items in models_by_role.items():
            for m in _as_list(items):
                mid = m.get("id") or m.get("model") or m.get("title") or m.get("name")
                if mid and mid not in seen:
                    seen.add(mid)
                    out.append(m)
    return out


def _parse_config_models(assistant: dict) -> list[dict]:
    """把 assistant 模型解析为可入库条目（对齐参考项目模型结构：

    - 系统提示词：模型级 chatOptions.baseAgentSystemMessage → baseChatSystemMessage
      → baseSystemMessage（chatService.ts:91-103 getSystemMessage 优先级）
    - 完成参数：defaultCompletionOptions 与 completionOptions 合并（modelClient.ts
      getCompletionOptions 语义，default 在前被 completion 覆盖）
    - 协议：存在 apiBaseAnthropic 或 identity 含 anthropic/claude/kimi → Anthropic，
      base_url 优先取 apiBaseAnthropic（kimi 等双端点模型）
    """
    config = assistant.get("configResult") or {}
    config = config.get("config") if isinstance(config, dict) else {}
    if not isinstance(config, dict):
        config = assistant.get("config") or {}
    entries = []
    for m in _extract_models(assistant):
        name = _request_name(m)
        if not name:
            continue
        chat_options = m.get("chatOptions") or {}
        if not isinstance(chat_options, dict):
            chat_options = {}
        system_message = _first_text(
            chat_options.get("baseAgentSystemMessage"),
            chat_options.get("baseChatSystemMessage"),
            chat_options.get("baseSystemMessage"),
            m.get("systemMessage"),
        )
        default_opts = m.get("defaultCompletionOptions") or {}
        completion_opts = {**(default_opts if isinstance(default_opts, dict) else {}),
                           **(m.get("completionOptions") or {} if isinstance(m.get("completionOptions"), dict) else {})}
        api_base = m.get("apiBase") or ""
        api_base_anthropic = m.get("apiBaseAnthropic") or ""
        # 协议判定对齐参考项目 isAnthropicProtocolModel：只看 provider/identity/apiBase
        # （apiBaseAnthropic 字段只是双端点模型的备选，不参与协议选择）
        anthropic = _is_anthropic(m)
        entries.append({
            "name": name,
            "api_key": m.get("apiKey") or "",
            # Anthropic 协议优先 apiBaseAnthropic（kimi 等双端点模型）
            "base_url": ((api_base_anthropic or api_base) if anthropic else api_base),
            "anthropic": anthropic,
            "system_message": system_message,
            "completion_options": completion_opts,
            "request_headers": (m.get("requestOptions") or {}).get("headers") or {},
            "provider": m.get("provider") or m.get("providerName") or "",
            "title": m.get("title") or name,
            "context_window": _context_window(completion_opts, m),
            "is_multimodal": bool(m.get("isMultimodal") or m.get("multimodal")),
        })
    return entries


def _is_success(body: dict) -> bool:
    """业务信封成功判定（对齐参考项目 auth/errors.ts isSuccessfulServiceResponse）。"""
    if not isinstance(body, dict):
        return False
    if body.get("errors"):
        return False
    if body.get("errorCode"):
        return False
    code = body.get("code")
    if code not in (None, ""):
        try:
            return int(code) == 200
        except (TypeError, ValueError):
            return False
    if body.get("serviceSuccess") is not None:
        return body.get("serviceSuccess") is not False
    if body.get("success") is not None:
        return bool(body.get("success"))
    return True


async def _fetch_first_json(client: httpx.AsyncClient, candidates: list[dict],
                            stage: str, token: str) -> dict:
    """按序尝试候选请求，首个业务成功返回（对齐 fetchFirstJson）。"""
    errors = []
    for cand in candidates:
        url = cand["url"]
        method = cand.get("method", "GET")
        try:
            resp = await client.request(method, url, headers={**_auth_headers(token), **cand.get("headers", {})})
            text = resp.text
            if not resp.is_success:
                if resp.status_code == 401:
                    raise Ta3Unauthorized()
                errors.append(f"HTTP {resp.status_code}: {url}")
                continue
            try:
                body = resp.json()
            except ValueError:
                errors.append(f"非 JSON: {url}")
                continue
            # 部分端点直接返回裸 JSON 数组（如 POST list-assistants?appId=），视为成功
            if isinstance(body, list):
                return body
            if not _is_success(body):
                code = body.get("code") if isinstance(body, dict) else "?"
                msg = (body.get("errorMessage") or body.get("message") or "") if isinstance(body, dict) else ""
                errors.append(f"{stage}失败: {code} {msg}")
                continue
            return body
        except Ta3Unauthorized:
            raise
        except httpx.HTTPError as e:
            errors.append(f"{e.__class__.__name__}: {url}")
    raise RuntimeError(errors[-1] if errors else f"{stage}失败")


class Ta3Unauthorized(Exception):
    """业务接口 401 —— 调用方应 refresh 后重试一次。"""


async def _list_organizations(client: httpx.AsyncClient, api_base: str, token: str) -> list[dict]:
    body = await _fetch_first_json(client, [
        {"url": f"{api_base}/ide/list-organizations"},
        {"url": f"{api_base}/ai/continue/ide/list-organizations"},
        {"url": f"{api_base}/ai/continue/ide/list-organizations", "method": "POST"},
    ], "加载项目列表", token)
    data = _as_dict(body.get("data"))
    result = _as_dict(data.get("result"))
    orgs = _first_list(
        body.get("organizations"), data.get("organizations"), result.get("organizations"),
        body.get("data"), data.get("records"), data.get("items"), data.get("list"),
        body.get("records"), body.get("items"), body.get("list"),
    )
    if not orgs:
        return [dict(_DEFAULT_ORG)]
    return orgs


async def _list_assistants(client: httpx.AsyncClient, api_base: str, token: str, org_id: str) -> list[dict]:
    """对齐参考项目 fetchYinhaiCatalog 的 3 候选：organizationId GET×2 + appId POST。"""
    candidates = [
        {"url": f"{api_base}/ide/list-assistants?{urlencode({'organizationId': org_id})}"},
        {"url": f"{api_base}/ai/continue/ide/list-assistants?{urlencode({'organizationId': org_id})}"},
        {"url": f"{api_base}/ai/continue/ide/list-assistants?{urlencode({'appId': org_id})}", "method": "POST"},
    ]
    try:
        body = await _fetch_first_json(client, candidates, f"加载 {org_id} 配置", token)
    except RuntimeError:
        return []
    # 裸数组（POST appId 端点）或信封结构（data/result/records/items/list 多层兼容）
    if isinstance(body, list):
        return body
    data = _as_dict(body.get("data"))
    result = _as_dict(data.get("result"))
    return _first_list(
        body.get("assistants"), data.get("assistants"), result.get("assistants"),
        body.get("data"), data.get("records"), data.get("items"), data.get("list"),
        body.get("records"), body.get("items"), body.get("list"),
    )


async def fetch_catalog_raw(api_base: str, token: str) -> dict:
    """拉取完整目录（组织 + 各组织 assistants 原始结构），供同步与调试。"""
    async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT,
                                 headers={"Accept-Encoding": "gzip, deflate"}) as client:
        organizations = await _list_organizations(client, api_base, token)
        assistants_by_org: dict[str, list[dict]] = {}
        for org in organizations:
            org_id = str(org.get("id") or org.get("organizationId") or "").strip()
            if not org_id:
                continue
            assistants_by_org[org_id] = await _list_assistants(client, api_base, token, org_id)
        if not assistants_by_org:
            assistants_by_org["personal"] = await _list_assistants(client, api_base, token, "personal")
        return {"organizations": organizations, "assistants_by_org": assistants_by_org}


async def sync_ta3_models(db: AsyncSession, provider, api_base: str) -> list[dict]:
    """同步目录 → upsert Model 表，返回新增/更新条目（含 orgId/profileId 元数据）。

    组织选择对齐参考项目 normalizeYinhaiCatalog：优先 state.selectedOrgId，否则
    取第一个有 assistants 的组织（多组织配置可能不一致，不做跨组织混合合并）。

    401 时自动 refresh 一次后重试（refresh_token 轮转防并发由 session 锁保证）。
    """
    from app.persistence.models.model_reg import Model

    token = await ta3_session.ensure_token(db, provider.id, api_base)
    try:
        catalog = await fetch_catalog_raw(api_base, token)
    except Ta3Unauthorized:
        token = await ta3_session.ensure_token(db, provider.id, api_base)
        catalog = await fetch_catalog_raw(api_base, token)

    # 组织选择：第一个有 assistants 的组织（参考项目默认行为）
    orgs = catalog["organizations"]
    assistants_by_org = catalog["assistants_by_org"]
    selected_org_id = ""
    selected_assistants: list[dict] = []
    for org in orgs:
        org_id = str(org.get("id") or org.get("organizationId") or "").strip()
        assistants = assistants_by_org.get(org_id) or []
        if assistants:
            selected_org_id = org_id
            selected_assistants = assistants
            break
    if not selected_assistants:
        # 全部组织都空：无可用配置
        logger.warning("[ta3] provider=%s 目录未解析出任何模型（组织=%d，assistants=0）",
                       provider.id, len(orgs))
        return []

    org_name = str(next((o.get("name") or "") for o in orgs
                        if str(o.get("id") or o.get("organizationId") or "").strip() == selected_org_id))
    entries: dict[str, dict] = {}
    for assistant in selected_assistants:
        profile_id = (
            assistant.get("id")
            or _first_text(
                (assistant.get("ownerSlug") or "") and (assistant.get("packageSlug") or ""),
            )
            or str(assistant.get("name") or "")
        )
        for entry in _parse_config_models(assistant):
            key = entry["name"]
            if key not in entries:
                entry["org_id"] = selected_org_id
                entry["org_name"] = org_name
                entry["profile_id"] = profile_id
                entries[key] = entry

    if not entries:
        logger.warning("[ta3] provider=%s 组织 %s 的配置未解析出模型", provider.id, org_name)
        return []
    logger.info("[ta3] provider=%s 组织 %s 解析出 %d 个模型", provider.id, org_name, len(entries))

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
        m.api_key = entry.get("api_key") or None
        m.base_url = entry.get("base_url") or None
        m.context_window = entry.get("context_window") or 200000
        m.is_multimodal = bool(entry.get("is_multimodal"))
        m.api_format = "ta3"
        # 必须复制新 dict 再赋回：SQLAlchemy 对可变 JSON 列赋同一对象引用
        # 不触发 change detection，原地 update 会导致 meta 更新不落库
        meta = dict(m.ta3_meta or {})
        meta.update({
            "systemMessage": entry.get("system_message") or "",
            "anthropic": bool(entry.get("anthropic")),
            "provider": entry.get("provider") or "",
            "completionOptions": entry.get("completion_options") or {},
            "requestHeaders": entry.get("request_headers") or {},
            "title": entry.get("title") or name,
            "orgId": entry.get("org_id") or "",
            "orgName": entry.get("org_name") or "",
            "profileId": entry.get("profile_id") or "",
        })
        m.ta3_meta = meta
        updated += 1
    await db.flush()

    # 缓存目录原文（脱敏：key 只留前 8 位）
    def _mask(v: str) -> str:
        return (v[:8] + "…") if v else ""

    sanitized = {
        "organizations": catalog["organizations"],
        "models": [
            {k: (_mask(str(v)) if k == "api_key" else v) for k, v in e.items()}
            for e in entries.values()
        ],
    }
    auth = await ta3_session.get_auth_row(db, provider.id)
    auth.catalog = sanitized
    await db.flush()

    return [{"name": k, **v} for k, v in entries.items()]
