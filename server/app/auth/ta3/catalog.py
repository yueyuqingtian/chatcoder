"""ta3 模型目录同步（对齐参考项目 authService.ts:484-586 fetchYinhaiCatalog）。

端点：POST {apiBase}/ai/continue/ide/list-organizations；
POST {apiBase}/ai/continue/ide/list-assistants?appId=；全部为空时请求默认配置。

鉴权：Authorization: Bearer <ide-session-…>+ X-Client-Type: app
（参考项目 auth/http.ts:91-103 authHeaders 契约）
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import session as ta3_session
from app.auth.ta3 import endpoints
from app.persistence.models.ta3_auth import Ta3Auth

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
    return {"Authorization": f"Bearer {token}", "X-Client-Type": "app"}


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


# 官方 getModelIdentity 的字段顺序（modelClient.js:44-60）
_IDENTITY_KEYS = (
    "provider", "providerName", "modelProvider", "model", "modelName", "modelId",
    "name", "title", "id",
)
# JS 正则 \b 只认 ASCII 单词字符；Python 默认按 Unicode，需加 re.ASCII 才与官方一致
_ANTHROPIC_WORD_RE = re.compile(r"\b(anthropic|claude|kimi)\b", re.ASCII)
_DEEPSEEK_WORD_RE = re.compile(r"\bdeepseek\b", re.ASCII)
# 基址字段优先级（modelClient.js:113-120、150-157）
_OPENAI_BASE_KEYS = ("apiBaseOpenAI", "apiBase", "apiBaseUrl", "baseURL", "baseUrl", "base_url")
_ANTHROPIC_BASE_KEYS = ("apiBaseAnthropic", "apiBase", "apiBaseUrl", "baseURL", "baseUrl", "base_url")
# 显式端点字段（modelClient.js:102-109、137-146 两组的并集，按原样入库）
ENDPOINT_KEYS = (
    "anthropicMessagesEndpoint", "messagesEndpoint", "messageEndpoint",
    "chatCompletionsEndpoint", "chatCompletionEndpoint", "completionEndpoint",
    "apiEndpoint", "apiUrl", "endpoint",
)
# 系统提示词相关字段（官方 systemPromptAssembly.js:22-39 getSystemMessage 读取的键）
_CHAT_OPTION_PROMPT_KEYS = (
    "baseAgentSystemMessage", "basePlanSystemMessage", "baseSystemMessage",
    "baseChatSystemMessage", "systemMessage",
)
_TOP_LEVEL_PROMPT_KEYS = ("baseAgentSystemMessage", "basePlanSystemMessage", "baseChatSystemMessage")


def _string_value(value) -> str:
    """官方 stringValue：只认字符串并 trim（modelClient.js:11-13）。"""
    return value.strip() if isinstance(value, str) else ""


def _first_string_by_keys(model: dict, keys) -> str:
    for k in keys:
        v = _string_value(model.get(k))
        if v:
            return v
    return ""


def model_identity(model: dict) -> str:
    """官方 getModelIdentity：非空字段以空格拼接后小写。"""
    return " ".join(str(model.get(k)) for k in _IDENTITY_KEYS if model.get(k)).lower()


def _explicit_protocol(model: dict) -> str:
    return str(model.get("protocol") or model.get("apiProtocol") or "").lower()


def is_anthropic_protocol_model(model: dict) -> bool:
    """对齐官方 isAnthropicProtocolModel（modelClient.js:61-73）。

    plan-601-2495：此前用子串匹配且忽略显式 protocol 字段——名称里带 "skimix" 之类的
    模型会被误判成 Anthropic；显式声明 protocol=openai 的 claude 代理模型也会被判错。
    """
    explicit = _explicit_protocol(model)
    if explicit:
        return explicit.startswith("anthropic")
    provider = str(model.get("provider") or model.get("providerName")
                   or model.get("modelProvider") or "").lower()
    return (provider in ("anthropic", "kimi")
            or bool(_ANTHROPIC_WORD_RE.search(model_identity(model)))
            or "/anthropic" in str(model.get("apiBase") or "").lower())


def is_deepseek_model(model: dict) -> bool:
    """对齐官方 isDeepSeekModel（modelClient.js:78-87）。"""
    explicit = _explicit_protocol(model)
    if explicit:
        return explicit == "deepseek"
    provider = str(model.get("provider") or model.get("providerName")
                   or model.get("modelProvider") or "").lower()
    return provider == "deepseek" or bool(_DEEPSEEK_WORD_RE.search(model_identity(model)))


def resolve_protocol(model: dict) -> str:
    """流式聊天的协议分派：官方 builtInProviders 顺序是 deepseek → anthropic → openai
    （providers.js:122-131），因此 DeepSeek 先匹配。"""
    if is_deepseek_model(model):
        return "deepseek"
    if is_anthropic_protocol_model(model):
        return "anthropic"
    return "openai"


def _is_anthropic(model: dict) -> bool:
    """是否走 Anthropic 协议（DeepSeek 先匹配，plan-601-2495）。"""
    return resolve_protocol(model) == "anthropic"


def resolve_api_base(model: dict, anthropic: bool) -> str:
    """按官方字段优先级取请求基址（OpenAI 面优先 apiBaseOpenAI，Anthropic 面优先 apiBaseAnthropic）。"""
    return _first_string_by_keys(model, _ANTHROPIC_BASE_KEYS if anthropic else _OPENAI_BASE_KEYS)


# plan-147-674: 视觉系模型 identity 兜底——上游目录缺 isMultimodal 字段时，
# 按模型名/供应商名识别多模态能力（对齐 BYOK MODEL_CATALOG 的兜底思路）。
# plan-156-739: 补齐新式多模态模型家族——glm-5.x（内置目录 glm-5.1/5.2=True）、
# Doubao Seed 家族（doubao-seed-1.6=True）、MiniMax（M3 为官方多模态模型）。
# kimi 系版本能力未确证（kimi-k2.7-code 为代码变体），不在此兜底，由用户手动配置（Step 1 已保证持久化）。
_VISION_IDENTITY_RE = re.compile(
    r"gpt-4o|gpt-4\.1|gpt-5|claude|gemini|qwen[\w.-]*vl|glm-4v|glm-5|doubao[\w.-]*vision"
    r"|doubao(?:[\w.-]+)?seed|minimax[\w.-]*m3|vision|pixtral|llava"
    r"|llama[\w.-]*vision|internvl|grok[\w.-]*vision"
)


def _is_vision_identity(model: dict) -> bool:
    """identity（provider+各名称字段）命中视觉系关键词 → 视为多模态。"""
    identity = " ".join(str(model.get(k) or "") for k in (
        "provider", "providerName", "modelProvider", "model", "modelName", "modelId",
        "name", "title", "id",
    )).lower()
    return bool(_VISION_IDENTITY_RE.search(identity))


def _is_multimodal_model(model: dict) -> bool:
    """多模态判定：显式字段优先（对齐 workbuddy supportsImages / trae display.multimodal
    的字段命名差异），字段缺失时按内置目录兜底，最后按 identity 关键词兜底。

    plan-156-739: 内置目录（models.catalog.MODEL_CATALOG）优先于 identity 正则——ta3 目录
    的新式模型（glm-5.x / doubao-seed / kimi 等）不被身份正则覆盖，而内置目录对同家族
    （glm-5.1/5.2、doubao-seed-1.6、kimi-k2.5）已标 multimodal=True，作为权威兜底。
    """
    display = model.get("displayConfig") or {}
    for v in (
        model.get("isMultimodal"), model.get("multimodal"),
        model.get("supportsImages"), model.get("supports_images"),
        model.get("vision"),
        display.get("multimodal") if isinstance(display, dict) else None,
    ):
        if v is not None:
            return bool(v)
    # 内置目录兜底（对齐 BYOK apply_metadata 语义；函数内 import 避免循环依赖）
    try:
        from app.models.catalog import lookup
        name = _request_name(model)
        if name:
            meta = lookup(name)
            if meta is not None:
                return bool(meta.get("multimodal", False))
    except Exception:
        logger.debug("[ta3] 内置目录多模态判定失败(非阻塞)", exc_info=True)
    return _is_vision_identity(model)


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
        # plan-601-2495：协议判定与基址选择对齐官方 modelClient.js:61-163——显式
        # protocol 优先、DeepSeek 先匹配、名称按词边界匹配；OpenAI 面优先 apiBaseOpenAI。
        protocol = resolve_protocol(m)
        anthropic = protocol == "anthropic"
        entries.append({
            "name": name,
            "api_key": m.get("apiKey") or "",
            "base_url": resolve_api_base(m, anthropic).rstrip("/"),
            "anthropic": anthropic,
            "deepseek": protocol == "deepseek",
            # 官方 buildAnthropicHeaders 取 model.anthropicVersion ?? anthropic_version（modelClient.js:228）
            "anthropic_version": _first_text(m.get("anthropicVersion"), m.get("anthropic_version")),
            "protocol": str(m.get("protocol") or ""),
            "api_protocol": str(m.get("apiProtocol") or ""),
            "endpoints": {k: m[k] for k in ENDPOINT_KEYS if _string_value(m.get(k))},
            "identity": {k: m[k] for k in _IDENTITY_KEYS if m.get(k)},
            "chat_options": dict(chat_options),
            "top_level_prompts": {k: m.get(k) or "" for k in _TOP_LEVEL_PROMPT_KEYS},
            "system_message": system_message,
            "completion_options": completion_opts,
            "request_headers": (m.get("requestOptions") or {}).get("headers") or {},
            "provider": m.get("provider") or m.get("providerName") or "",
            "title": m.get("title") or name,
            "context_window": _context_window(completion_opts, m),
            "is_multimodal": _is_multimodal_model(m),
        })
    return entries


def entry_meta(entry: dict, name: str = "") -> dict:
    """目录条目 → 模型行 ta3_meta 的增量字段（纯函数，便于单测）。

    plan-601-2495：除原有字段外补存官方请求构造要读的字段——anthropicVersion、显式
    protocol、DeepSeek 判定、显式端点、identity 各字段，以及 persona 选择用的
    chatOptions / 顶层 base*SystemMessage（官方 systemPromptAssembly.js:22-39）。
    原有 systemMessage 保持不变，旧代码继续可用。
    """
    chat_options = entry.get("chat_options") or {}
    meta = {
        "systemMessage": entry.get("system_message") or "",
        "anthropic": bool(entry.get("anthropic")),
        "deepseek": bool(entry.get("deepseek")),
        "provider": entry.get("provider") or "",
        "completionOptions": entry.get("completion_options") or {},
        "requestHeaders": entry.get("request_headers") or {},
        "title": entry.get("title") or name or entry.get("name") or "",
        "orgId": entry.get("org_id") or "",
        "orgName": entry.get("org_name") or "",
        "profileId": entry.get("profile_id") or "",
        "anthropicVersion": entry.get("anthropic_version") or "",
        "protocol": entry.get("protocol") or "",
        "apiProtocol": entry.get("api_protocol") or "",
        "endpoints": dict(entry.get("endpoints") or {}),
        "identity": dict(entry.get("identity") or {}),
        # 原样保存 chatOptions，并保证 persona 相关键始终存在（缺省空串）
        "chatOptions": {**{k: "" for k in _CHAT_OPTION_PROMPT_KEYS}, **dict(chat_options)},
    }
    for k, v in (entry.get("top_level_prompts") or {}).items():
        meta[k] = v or ""
    for k in _TOP_LEVEL_PROMPT_KEYS:
        meta.setdefault(k, "")
    return meta


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
                            stage: str, token: str, provider=None) -> dict:
    """按序尝试候选请求，首个业务成功返回（对齐 fetchFirstJson）。"""
    errors = []
    for cand in candidates:
        url = cand["url"]
        method = cand.get("method", "GET")
        try:
            headers = {**_auth_headers(token), **cand.get("headers", {})}
            if provider is not None:
                parsed = urlsplit(url)
                path = parsed.path[parsed.path.index("/ai/continue/ide/"):] + (("?" + parsed.query) if parsed.query else "")
                resp = await endpoints.request(provider, method, path, headers=headers)
            else:
                resp = await client.request(method, url, headers=headers, follow_redirects=False)
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


def _items(body, key):
    if isinstance(body, list):
        return body
    body = _as_dict(body)
    data = _as_dict(body.get("data"))
    result = _as_dict(body.get("result") or data.get("result"))
    return _first_list(body.get(key), data.get(key), result.get(key), body.get("data"),
                       data.get("records"), data.get("items"), data.get("list"),
                       body.get("records"), body.get("items"), body.get("list"))


async def _list_organizations(client, api_base, token, provider=None):
    body = await _fetch_first_json(client, [{"url": api_base + "/ai/continue/ide/list-organizations",
        "method": "POST", "headers": {"Cache-Control": "no-cache", "Pragma": "no-cache"}}], "加载项目列表", token, provider)
    return _items(body, "organizations") or [dict(_DEFAULT_ORG)]


async def _list_assistants(client, api_base, token, org_id, provider=None):
    suffix = ("?" + urlencode({"appId": org_id})) if org_id is not None else ""
    body = await _fetch_first_json(client, [{"url": api_base + "/ai/continue/ide/list-assistants" + suffix,
        "method": "POST", "headers": {"Cache-Control": "no-cache", "Pragma": "no-cache"}}],
        "加载模型配置", token, provider)
    return _items(body, "assistants")


async def fetch_catalog_raw(api_base, token, client_kwargs=None, provider=None):
    async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT, follow_redirects=False,
                                 headers={"Accept-Encoding": "gzip, deflate"},
                                 **(client_kwargs or {})) as client:
        organizations = await _list_organizations(client, api_base, token, provider)
        assistants_by_org = {}
        errors = []
        for org in organizations:
            org_id = str(org.get("id") or org.get("organizationId") or "").strip()
            if not org_id:
                continue
            try:
                assistants_by_org[org_id] = await _list_assistants(client, api_base, token, org_id, provider)
            except Ta3Unauthorized:
                raise
            except RuntimeError as exc:
                errors.append(exc)
                assistants_by_org[org_id] = []
        if not any(assistants_by_org.values()):
            try:
                assistants_by_org[str(organizations[0].get("id") or "personal")] = await _list_assistants(
                    client, api_base, token, None, provider)
            except Ta3Unauthorized:
                raise
            except RuntimeError:
                if errors:
                    raise errors[-1]
                raise
        return {"organizations": organizations, "assistants_by_org": assistants_by_org}


async def fetch_catalog_probe(provider, token):
    response = await endpoints.request(provider, "POST", "/ai/continue/ide/list-organizations",
                                       headers=_auth_headers(token), timeout=8)
    if response.status_code == 401:
        raise Ta3Unauthorized()
    response.raise_for_status()


async def sync_ta3_models(db: AsyncSession, provider, api_base: str) -> list[dict]:
    """同步目录 → upsert Model 表，返回新增/更新条目（含 orgId/profileId 元数据）。

    组织选择对齐参考项目 normalizeYinhaiCatalog：优先 state.selectedOrgId，否则
    取第一个有 assistants 的组织（多组织配置可能不一致，不做跨组织混合合并）。

    401 时自动 refresh 一次后重试（refresh_token 轮转防并发由 session 锁保证）。
    """
    from app.persistence.models.model_reg import Model

    # plan-110-438 M2: 目录同步按该供应商的代理配置出网
    from app.services.credential_service import provider_client_kwargs

    _client_kwargs = provider_client_kwargs(provider)
    token = await ta3_session.ensure_token(db, provider.id, api_base)
    auth = await ta3_session.load_auth(db, provider.id)
    generation = auth.session_generation if auth else None
    try:
        catalog = await fetch_catalog_raw(api_base, token, _client_kwargs, provider)
    except Ta3Unauthorized:
        token = await ta3_session.ensure_token(db, provider.id, api_base, failed_token=token)
        try:
            catalog = await fetch_catalog_raw(api_base, token, _client_kwargs, provider)
        except Ta3Unauthorized as exc:
            raise ta3_session.Ta3AuthError("登录验证失败，请重新网页登录") from exc

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
    # v0.5.13 修复（登录后 key 不保存）：模型必须在**写引擎的同步会话**内查询并按需
    # 新建。此前先用异步会话查 existing、再把它的 ORM 对象塞进 _persist 赋属性后
    # `s.commit()`——两个会话互不相识，属性变更不会落库，于是每次登录同步都只更新
    # 了 auth 行缓存，模型的 api_key（目录下发的 llm- key）永远是 NULL。
    updated = 0
    created = 0
    # 缓存目录原文（脱敏：key 只留前 8 位）
    def _mask(v: str) -> str:
        return (v[:8] + "…") if v else ""

    sanitized = {
        # selectedOrgId：对齐参考项目 sessionInfo.catalog.selectedOrgId（SSO/appId 透传数据源）
        "selectedOrgId": selected_org_id,
        "organizations": catalog["organizations"],
        "models": [
            {k: (_mask(str(v)) if k == "api_key" else v) for k, v in e.items()}
            for e in entries.values()
        ],
    }

    from app.persistence.database import run_write_locked

    def _persist(s):
        # plan-20261106-ta3-080：目录结果必须属于发起时的登录代次。
        auth_now = ta3_session._row(s, provider.id)
        if auth_now is None or not auth_now.access_token or auth_now.session_generation != generation:
            raise ta3_session.Ta3AuthError("登录状态已变化，请重新同步模型")
        # 在同步会话内重新查询（跨会话复用 async ORM 对象不会落库，见上）
        by_name_local = {
            m.name: m for m in s.execute(
                select(Model).where(Model.provider_id == provider.id)
            ).scalars().all()
        }
        created_local = 0
        updated_local = 0
        for name, entry in entries.items():
            m = by_name_local.get(name)
            if m is None:
                m = Model(tenant_id=1, name=name, provider_id=provider.id, source_type="byok")
                s.add(m)
                by_name_local[name] = m
                created_local += 1
            m.api_key = entry.get("api_key") or None
            m.base_url = entry.get("base_url") or None
            m.context_window = entry.get("context_window") or 200000
            # plan-147-674: 用户手动设置过多模态标记（meta.multimodal_override）时
            # 目录同步不覆盖，避免每次登录同步冲掉 UI 上的手动修正
            meta = dict(m.ta3_meta or {})
            if not meta.get("multimodal_override"):
                m.is_multimodal = bool(entry.get("is_multimodal"))
            m.api_format = "ta3"
            meta.update(entry_meta(entry, name))
            m.ta3_meta = meta
            updated_local += 1
        # v0.5.13 修复：Ta3Auth 主键是自增 id，不是 provider_id——此前 s.get(Ta3Auth,
        # provider.id) 永远取不到行，目录缓存从未落库（对齐 workbuddy 的同类修复）。
        auth_row = s.execute(
            select(Ta3Auth).where(Ta3Auth.provider_id == provider.id).order_by(Ta3Auth.id.desc())
        ).scalars().first()
        if auth_row is not None:
            auth_row.catalog = sanitized
        s.commit()
        return created_local, updated_local

    created, updated = await run_write_locked(_persist, label="ta3.catalog.sync")

    return [{"name": k, **v} for k, v in entries.items()]
