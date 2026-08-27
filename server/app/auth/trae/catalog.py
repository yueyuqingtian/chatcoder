"""TRAE 模型目录同步（batch_get_detail_param → Model 表）。

端点：POST {agent_host}/api/ide/v1/batch_get_detail_param。
请求体关键约束（实测验证）：
- `mode_type` / `access_type` 是**整数枚举**（传字符串 "Manual"/"SoloLite" 会 400，传 0 通过）
- `functions` 为请求的 function 白名单

响应结构（实测 2026-08-25，1.1MB）：
```
{ "allow_tenant_user_add_model": bool,
  "client_config": str, "client_config_ab_versions": [],
  "function_configs": [
    { "function": "solo_agent_lite",
      "config_info_list": [
        { "config_name": "Doubao-Seed-Evolving",       // 模型名，无 provider// 前缀
          "config_switch": true,
          "display_config": { "display_name", "multimodal", "model_capability", ... },
          "extra_config": "json-str",
          "is_invisible_to_user": bool,
          "model_detail_list": [ { "model_name", "prompt_max_tokens",
                                   "max_tokens", "model_extra_config": "json-str" } ] },
        ...
      ] } ]
```
同步策略（对齐 workbuddy catalog）：
- 只保留对话型 function（solo_agent_lite / builder / assistant / solo_coder）
- 同名模型跨 function 去重（保留第一个）
- upsert Model 表，api_format="trae"，api_key 占位 "__trae_session__"
- 401 时自动 refresh 一次后重试
方案: docs/plan-trae-solo-provider-integration.md §5.2。
"""
from __future__ import annotations

import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.trae import session as trae_session
from app.auth.trae.business import build_business_headers
from app.core.config import settings

logger = logging.getLogger(__name__)

DETAIL_PARAM_PATH = "/api/ide/v1/batch_get_detail_param"
_CATALOG_TIMEOUT = 60.0

# 对话型 function 白名单（SoloAgentLite 为主）
_CHAT_FUNCTIONS = ("solo_agent_lite", "builder", "assistant", "solo_coder")

# TRAE Work CN 客户端实际可用的模型（用户 2026-08-25 提供；目录接口返回的
# config 全集含大量不可用/工具型模型，前端只展示白名单内模型）。
_AVAILABLE_MODELS = {
    "Doubao-Seed-Evolving", "Doubao-Seed-2.1-Pro", "Doubao-Seed-Code",
    "Doubao-Seed-2.1-Turbo", "glm-5.3", "glm-5.2", "DeepSeek-V4-Flash",
    "DeepSeek-V4-Flash-Official", "DeepSeek-V4-Pro", "DeepSeek-V4-Pro-Official",
    "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "minimax-m3", "qwen3.8-max",
    "qwen-3.7-plus",
}

# 思考档位别名：服务端 options 原值（light/high/extra_high）→ 客户端 UI 档位
_OPTION_ALIASES = {"light": "low", "extra_high": "xhigh"}

# 默认请求体 functions（对齐 ai_agent 日志 BatchDetailParamRequest 全量）
_DEFAULT_FUNCTIONS = [
    "assistant", "solo_agent_lite", "solo_coder", "solo_agent_remote",
    "solo_work_lite", "solo_work_remote", "solo_design_lite",
    "solo_design_remote", "builder",
]


def _detail_param_body(functions: list[str] | None = None) -> dict:
    return {
        "functions": functions or list(_DEFAULT_FUNCTIONS),
        "agent_type": "",
        "current_config_info": {"config_name": "", "is_custom_model": False},
        # mode_type / access_type 为整数枚举：字符串（"Manual"/"SoloLite"）会 400，
        # 0 通过（服务端不校验具体值，实测 access_type 0~3 均 200）
        "mode_type": 0,
        "access_type": 0,
        "ab_force_vids": "",
        "ab_autotest_advanced_mode": 0,
        "show_custom_model": True,
    }


def _extract_function_groups(payload: dict) -> list[tuple[str, list[dict]]]:
    """从响应提取 (function 名, 模型条目列表) 分组（实测结构 function_configs[]）。"""
    if not isinstance(payload, dict):
        return []
    for key in ("data", "Result", "result"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            return _extract_function_groups(inner)
    groups: list[tuple[str, list[dict]]] = []
    for fc in payload.get("function_configs") or []:
        if not isinstance(fc, dict):
            continue
        func = (fc.get("function") or fc.get("function_name")
                or fc.get("name") or "").strip()
        infos = fc.get("config_info_list") or []
        items = [it for it in infos if isinstance(it, dict)]
        if items:
            groups.append((func, items))
    if groups:
        return groups
    logger.warning("[trae] batch_get_detail_param 响应未识别出 function_configs: keys=%s",
                   list(payload.keys())[:10])
    return []


def _is_chat_function(func: str) -> bool:
    """对话型 function 白名单。"""
    return func.lower() in _CHAT_FUNCTIONS


def _parse_config_json(value) -> dict | None:
    if not isinstance(value, str):
        return value if isinstance(value, dict) else None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def _parse_contact_rate(value) -> float | None:
    """display_contact_config（JSON 串或 dict）→ 积分消耗倍率（consumption_rate.data.rate）。

    实测结构：{"access": {...}, "consumption_rate": {"enable": true, "data": {"rate": 0.72}}}。
    max 模式下消耗更快即由该倍率体现（客户端"积分消耗更快"）。
    """
    parsed = _parse_config_json(value)
    if not isinstance(parsed, dict):
        return None
    rate = ((parsed.get("consumption_rate") or {}).get("data") or {}).get("rate")
    try:
        return float(rate) if rate is not None else None
    except (TypeError, ValueError):
        return None


def _parse_reasoning_options(item: dict) -> tuple[list[str], bool]:
    """reasoning_effort_config → (归一化思考档位, 是否开启)。

    服务端 options 原值（light/high/extra_high）按 _OPTION_ALIASES 归一化为
    客户端档位（low/high/xhigh）；max_mode 开启时追加 "max"（1M 上下文、消耗更快）。
    """
    rec = item.get("reasoning_effort_config")
    if not isinstance(rec, dict):
        rec = {}
    options = rec.get("options") or []
    normalized = [
        _OPTION_ALIASES.get(str(o), str(o)) for o in options if isinstance(o, str)
    ]
    support = bool(rec.get("support_thinking"))
    return normalized, support


def _parse_entry(item: dict, idx: int) -> dict | None:
    """模型条目 → 可入库字段（实测 config_info_list 结构）。"""
    name = str(item.get("config_name") or item.get("model_name")
               or item.get("name") or "").strip()
    if not name:
        logger.warning("[trae] 模型条目缺名称（index=%s）: keys=%s", idx, list(item.keys())[:8])
        return None
    display = item.get("display_config")
    if not isinstance(display, dict):
        display = {}
    # 能力档位取第一个（model_detail_list[0]；多档位时 prompt_max_tokens 各异）
    details = item.get("model_detail_list")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        first_detail: dict = details[0]
    else:
        first_detail = {}
    underlying_model_name = str(first_detail.get("model_name") or "").strip() or None
    prompt_max = (first_detail.get("prompt_max_tokens") or item.get("prompt_max_tokens"))
    max_tokens = (first_detail.get("max_tokens") or item.get("max_tokens"))
    try:
        context_window = int(prompt_max or 0) or None
    except (TypeError, ValueError):
        context_window = None
    try:
        max_output = int(max_tokens or 0) or None
    except (TypeError, ValueError):
        max_output = None
    # max 档位上下文（context_window_tokens.max，如 1000000 = 1M）
    ctx_tokens = item.get("context_window_tokens")
    if not isinstance(ctx_tokens, dict):
        ctx_tokens = {}
    ctx_max_raw = ctx_tokens.get("max")
    try:
        context_window_max = int(ctx_max_raw or 0) or None
    except (TypeError, ValueError):
        context_window_max = None
    reasoning_opts, thinking = _parse_reasoning_options(item)
    return {
        "name": name,
        "config_name": name,
        # 请求端 model_name 用目录下发的档位名（provider_model_name，可能带
        # __dev 后缀）；纯配置名在 create_agent_task 全新会话下会报
        # "model config is empty"（实测 2026-08-26，服务端 0.1.56）。
        "model_name": name,
        # 底层 provider 档位名原样保留，供 TraeProvider._resolve_model_name
        # 构造请求 model_name（如 DeepSeek-V4-Flash-Official__dev）。
        "provider_model_name": underlying_model_name,
        "title": display.get("display_name") or name,
        "functions": [],
        "context_window": context_window,
        "context_window_max": context_window_max,
        "max_output_tokens": max_output,
        "is_multimodal": bool(display.get("multimodal")),
        "extra_config": _parse_config_json(item.get("extra_config")
                                           or first_detail.get("model_extra_config")),
        "model_capability": display.get("model_capability") or "",
        "max_mode": bool(display.get("max_mode")),
        "is_custom_model": bool(display.get("is_custom_model")),
        "thinking": thinking,
        "reasoning_options": reasoning_opts,
        "consumption_rate": _parse_contact_rate(item.get("display_contact_config")),
        "is_available": name in _AVAILABLE_MODELS,
    }


def _is_builtin_model(entry: dict) -> bool:
    """内置模型才入库：用户在 TRAE 里配置的自定义模型（is_custom_model）指向
    其自有端点（如 localhost 中转），chatcoder 无法复用，需排除。"""
    return not entry.get("is_custom_model")


async def fetch_catalog_raw(agent_host: str, token: str, meta: dict,
                            functions: list[str] | None = None) -> dict:
    """POST {agent_host}/api/ide/v1/batch_get_detail_param → 响应 JSON。"""
    url = f"{agent_host.rstrip('/')}{DETAIL_PARAM_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT,
                                     headers={"Accept-Encoding": "gzip, deflate"}) as client:
            resp = await client.post(url, json=_detail_param_body(functions),
                                     headers=build_business_headers(token, meta))
    except httpx.HTTPError as e:
        raise RuntimeError(f"TRAE 模型目录拉取失败：{url}") from e
    if resp.status_code in (401, 403):
        raise TraeCatalogUnauthorized()
    if not resp.is_success:
        raise RuntimeError(f"TRAE 模型目录拉取失败 http_{resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError(f"TRAE 模型目录返回非 JSON：{resp.text[:200]}") from None
    return body if isinstance(body, dict) else {}


class TraeCatalogUnauthorized(Exception):
    """目录接口 401 —— 调用方应 refresh 后重试一次。"""


async def sync_trae_models(db: AsyncSession, provider, agent_host: str) -> list[dict]:
    """同步 batch_get_detail_param → upsert Model 表，返回新增/更新条目。

    401 时自动 refresh 一次后重试。
    """
    from app.persistence.models.model_reg import Model

    auth = await trae_session.load_auth(db, provider.id)
    meta = _auth_meta(auth, provider)
    token = await trae_session.ensure_token(
        db, provider.id, api_host=settings.trae_account_endpoint,
        client_id=settings.trae_client_id, ide_version=settings.trae_ide_version)

    try:
        payload = await fetch_catalog_raw(agent_host, token, meta)
    except TraeCatalogUnauthorized:
        token = await trae_session.refresh_session(
            db, provider.id, api_host=settings.trae_account_endpoint,
            client_id=settings.trae_client_id, ide_version=settings.trae_ide_version)
        payload = await fetch_catalog_raw(agent_host, token, meta)

    # 按 function 分组 → 只保留对话型 function + 内置模型 → 同名去重
    entries: dict[str, dict] = {}
    total = 0
    for func, items in _extract_function_groups(payload):
        if not _is_chat_function(func):
            continue
        for idx, item in enumerate(items):
            total += 1
            entry = _parse_entry(item, idx)
            if entry and _is_builtin_model(entry) and entry["name"] not in entries:
                entry["functions"] = [func]
                entries[entry["name"]] = entry

    if not entries:
        logger.warning("[trae] provider=%s 目录未解析出对话模型（原始条目=%d）",
                       provider.id, total)
        return []

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
        m.api_key = "__trae_session__"  # 占位：真实 token 由 registry 从 trae_auth 动态取
        m.base_url = agent_host
        m.context_window = entry.get("context_window") or 200000
        m.is_multimodal = bool(entry.get("is_multimodal"))
        m.api_format = "trae"
        # 思考档位：归一化 options；仅当模型本身支持思考（options 非空）时追加 max 档。
        # 目录里部分模型 max_mode=True 但无 reasoning_effort_config（如 DeepSeek-V4-Flash /
        # Doubao-Seed-Evolving），客户端不显示思考档位，不能把 max 混入。
        reasoning = list(entry.get("reasoning_options") or [])
        if entry.get("max_mode") and reasoning and "max" not in reasoning:
            reasoning.append("max")
        m.reasoning_efforts = reasoning or None
        meta2 = dict(m.trae_meta or {})
        meta2.update({
            "config_name": entry.get("config_name") or name,
            # model_name 存纯配置名（展示/选择用）；档位名（__dev）存
            # provider_model_name，供 TraeProvider 构造请求 model_name。
            "model_name": entry.get("model_name") or name,
            "provider_model_name": entry.get("provider_model_name"),
            "title": entry.get("title") or name,
            "functions": entry.get("functions") or [],
            "max_output_tokens": entry.get("max_output_tokens"),
            "model_extra_config": entry.get("extra_config"),
            "model_capability": entry.get("model_capability") or "",
            "max_mode": entry.get("max_mode"),
            # max 档位上下文（如 1000000 = 1M，模型 meta 供 max 档请求使用）
            "context_window_max": entry.get("context_window_max"),
            "thinking": entry.get("thinking"),
            # 积分消耗倍率（consumption_rate.data.rate）：max 档消耗更快
            "consumption_rate": entry.get("consumption_rate"),
            # 客户端实际可用模型标记（前端模型选择器按此过滤）
            "is_available": bool(entry.get("is_available")),
        })
        # 必须复制新 dict 再赋回：SQLAlchemy 对可变 JSON 列赋同一对象引用不触发 change detection
        m.trae_meta = meta2
        updated += 1
    await db.flush()

    # 缓存目录原文（不含敏感字段）
    auth = await trae_session.get_auth_row(db, provider.id)
    auth.catalog = {
        "models": [
            {"name": e["name"], "config_name": e["config_name"], "title": e["title"],
             "functions": e["functions"], "context_window": e["context_window"]}
            for e in entries.values()
        ],
    }
    await db.flush()

    logger.info("[trae] provider=%s 同步 %d 个对话模型（新增 %d，更新 %d）",
                provider.id, len(entries), created, updated)
    return [{"name": k, **v} for k, v in entries.items()]


def _auth_meta(auth, provider) -> dict:
    """业务请求头元数据：设备指纹 + 账号信息 + 供应商覆盖配置。"""
    meta = {}
    if auth is not None:
        meta.update({
            "device_id": auth.device_id or "",
            "machine_id": auth.machine_id or "",
        })
        account = auth.account or {}
        meta["region"] = account.get("region") or "cn"
    # provider 级覆盖（base_url 等由调用方解析，此处只补头元数据）
    return meta
