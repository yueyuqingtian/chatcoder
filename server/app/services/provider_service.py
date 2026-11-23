"""供应商（Provider）CRUD + 模型扫描 + 批量模型配置。"""
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.model_reg import Model, Provider

logger = logging.getLogger(__name__)

SCAN_TIMEOUT = 20.0


def normalize_ta3_config(kwargs, current=None):
    """plan-20261106-ta3-080：自定义地址不隐式附加默认平台。"""
    from app.auth.ta3 import endpoints
    fmt = kwargs.get("api_format") or getattr(current, "api_format", None)
    if fmt != "ta3":
        return kwargs
    kwargs = dict(kwargs)
    base = endpoints.normalize_base(kwargs.get("base_url") or getattr(current, "base_url", None) or endpoints.DEFAULT_TA3_API_BASE)
    kwargs["base_url"] = base
    alts = kwargs.get("ta3_alt_base_urls")
    if alts is None:
        alts = getattr(current, "ta3_alt_base_urls", None)
    if alts is None:
        alts = endpoints.DEFAULT_ALT_BASES if base == endpoints.DEFAULT_TA3_API_BASE else []
    kwargs["ta3_alt_base_urls"] = endpoints.normalize_alternates(alts, base)
    return kwargs


async def create_provider(db: AsyncSession, **kwargs) -> int:
    kwargs = normalize_ta3_config(kwargs)
    from sqlalchemy import func

    from app.persistence.database import run_write_locked
    from app.services import credential_service

    def patch(s):
        # plan-81-345: 先清理历史孤儿凭据（旧版本删除供应商时漏删的行）——
        # SQLite 复用被删供应商的 id，孤儿凭据会被误挂到本次新建的供应商名下
        # （表现为「新增后凭空多出一个旧 Key」）。
        credential_service.purge_orphan_credentials(s)
        # plan-41-225: 新供应商排到列表末尾——"新增的加在最后"是所有列表的通用心智，
        # 若插入首位会打断用户已调整好的顺序。
        if kwargs.get("sort_order") is None:
            kwargs["sort_order"] = int(
                s.execute(select(func.max(Provider.sort_order))).scalar() or 0
            ) + 1
        provider = Provider(tenant_id=1, **kwargs)
        s.add(provider)
        s.flush()
        pid = provider.id
        s.commit()
        return pid

    return await run_write_locked(patch, label="provider.create")


async def get_provider(db: AsyncSession, provider_id: int) -> Provider | None:
    # plan-20261123-ux-fixes：写操作走独立同步写引擎提交，populate_existing=True 确保同 AsyncSession
    # 读到刚提交的最新值，不命中过期 identity map（同时不触发 expire_all 导致异步 MissingGreenlet）。
    return await db.get(Provider, provider_id, populate_existing=True)


async def list_providers(db: AsyncSession) -> list[Provider]:
    # plan-41-225: 按用户自定义顺序（sort_order）排序，id 兜底保证顺序稳定不抖动
    # （老库迁移后 sort_order 全为 0 → 等价于原 id 升序，升级零感知）。
    res = await db.execute(
        select(Provider)
        .order_by(Provider.sort_order.asc(), Provider.id.asc())
        .execution_options(populate_existing=True)
    )
    return list(res.scalars().all())


async def update_provider(db: AsyncSession, provider_id: int, **kwargs) -> bool:
    """更新供应商字段(只更新非 None 字段；写引擎单写线程)。api_key 空字符串 = 清除。"""
    from app.persistence.database import run_write_locked

    current = await get_provider(db, provider_id)
    kwargs = normalize_ta3_config(kwargs, current)

    def patch(s):
        provider = s.get(Provider, provider_id)
        if provider is None:
            return False
        # plan-20261106-ta3-080：修改入口后，旧目录/续期结果不得回写新配置。
        if provider.api_format == "ta3" and any(k in kwargs and kwargs[k] != getattr(provider, k, None)
                                               for k in ("base_url", "ta3_alt_base_urls")):
            from app.auth.ta3.session import _row
            auth = _row(s, provider_id)
            if auth is not None:
                auth.session_generation = (auth.session_generation or 0) + 1
        for k, v in kwargs.items():
            if v is None:
                continue
            if k in ("api_key", "proxy_url") and v == "":
                setattr(provider, k, None)
            else:
                setattr(provider, k, v)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"provider.update.{provider_id}")


async def reorder_providers(db: AsyncSession, ids: list[int]) -> int:
    """plan-41-225: 按给定顺序重写供应商排序位（数组下标即 sort_order）。

    为什么用批量而非逐条 PATCH：一次拖拽 = 一次请求，避免逐条写入的中间态闪烁
    （列表可能被中间态渲染两次）；且"顺序"天然是全量有序列表语义。
    未知 id 忽略（防御脏数据）；未出现在 ids 中的供应商保留在末尾，不丢数据。
    """
    from app.persistence.database import run_write_locked

    def patch(s):
        rows = list(s.execute(select(Provider)).scalars().all())
        known = {p.id: p for p in rows}
        order = [pid for pid in ids if pid in known]
        ordered = set(order)
        rest = [p.id for p in rows if p.id not in ordered]
        updated = 0
        for index, pid in enumerate(order + rest):
            p = known[pid]
            if p.sort_order != index:
                p.sort_order = index
                updated += 1
        s.commit()
        return updated

    return await run_write_locked(patch, label="provider.reorder")


async def delete_provider(db: AsyncSession, provider_id: int) -> bool:
    """删除供应商，并级联删除其下模型（写引擎单写线程）。
    删除前置空会话/代理/子代理配置对这些模型的引用（FK 约束），
    引用方 model_id 变 NULL 后前端选择器自动忽略。"""
    from app.persistence.database import run_write_locked
    from app.services import credential_service
    from app.services.model_service import detach_model_refs

    def patch(s):
        provider = s.get(Provider, provider_id)
        if provider is None:
            return False
        models = list(s.execute(select(Model).where(Model.provider_id == provider_id)).scalars().all())
        detach_model_refs(s, [m.id for m in models])
        for m in models:
            s.delete(m)
        # plan-81-345: 凭据必须一并删除——provider_credentials.provider_id 无外键约束，
        # 漏删会残留孤儿行；SQLite 复用被删供应商的 id，孤儿凭据会被误挂到新供应商上
        # （表现为「删除再新增后多出一个旧 Key」）。
        credential_service.purge_provider_credentials(s, provider_id)
        s.delete(provider)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"provider.delete.{provider_id}")


async def _resolve_scan_key(db: AsyncSession, provider: Provider) -> str | None:
    """plan-248-1258 M2.2: 扫描/测试用 key——优先取首个可用凭据，回落 provider.api_key。

    OAuth 类供应商（workbuddy/ta3/trae/kiro/antigravity）的凭据 key 来自各自 auth 表的 access_token。
    """
    from app.services import credential_service

    creds = await credential_service.list_credentials(db, provider.id)
    fmt = (provider.api_format or "openai").lower()
    # plan-20261020-retry-cred-perf：扫描/测试用 key 与真实取用同序（非 ta3 走健康排序），
    # 否则「测试该配置」用的 key 可能与实际调用不是同一条。
    if fmt == "ta3":
        ranked = credential_service.available_credentials(creds)
    else:
        ranked = credential_service.rank_credentials(creds)
    for c in ranked:
        if c.api_key:
            return c.api_key
        if fmt in ("workbuddy", "ta3", "trae", "kiro", "antigravity"):
            from app.models.registry import _load_auth_for_credential

            auth = await _load_auth_for_credential(db, fmt, provider.id, c.id)
            if auth is not None and getattr(auth, "access_token", None):
                return auth.access_token
    return provider.api_key


async def scan_models(db: AsyncSession, provider_id: int) -> list[dict]:
    """请求供应商的模型列表接口。

    OpenAI 兼容: GET {base_url}/models (Authorization: Bearer key)
    Anthropic:   GET {base_url}/models (x-api-key, anthropic-version)
    返回 [{"id": ..., "context_window": ...|None, "owned_by": ...|None}]
    """
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise ValueError("provider not found")

    api_format = (provider.api_format or "openai").lower()

    # plan-116-456: kiro 无 /models 接口，目录来自内置映射表——不请求网络、
    # 也不依赖 Base URL（其端点由区域推导，可留空）
    if api_format == "kiro":
        from app.auth.kiro.catalog import merge_entries

        return [
            {
                "id": str(e["name"]),
                "context_window": e.get("context_window"),
                "owned_by": e.get("vendor") or "kiro",
                "is_multimodal": bool(e.get("is_multimodal")),
                "reasoning_efforts": e.get("reasoning_efforts"),
            }
            for e in merge_entries()
        ]

    # antigravity 无 /models 接口，目录来自账号登录后的 fetchAvailableModels
    # （用「同步模型」拉取）；此处用内置兜底清单保证界面不是空白
    if api_format == "antigravity":
        from app.auth.antigravity.catalog import BUILTIN_MODELS, display_name

        return [
            {
                "id": mid,
                "context_window": None,
                "owned_by": "antigravity",
                "is_multimodal": False,
                "reasoning_efforts": None,
                "display_name": display_name(mid),
            }
            for mid in BUILTIN_MODELS
        ]

    if not provider.base_url:
        raise ValueError("供应商未配置 Base URL")

    base = provider.base_url.rstrip("/")
    # plan-248-1258: key 取自凭据（多 Key 时用首个可用）
    _scan_key = await _resolve_scan_key(db, provider)

    # CommandCode 无 /models 接口，返回官方预设支持模型
    if api_format == "commandcode":
        cmc_models = [
            {"id": "deepseek/deepseek-v4-pro", "context_window": 1000000, "is_multimodal": False, "reasoning_efforts": ["low", "medium", "high", "max"]},
            {"id": "deepseek/deepseek-v4-flash", "context_window": 1000000, "is_multimodal": False, "reasoning_efforts": ["low", "medium", "high", "max"]},
            # plan-234-1171 R3: 补齐 v4.1-flash——多模态模型，此前预设目录缺失导致
            # 扫描看不到该模型、is_multimodal 标记缺失，图片注入双路径都被跳过。
            {"id": "deepseek/deepseek-v4.1-flash", "context_window": 1000000, "is_multimodal": True, "reasoning_efforts": ["low", "medium", "high", "max"]},
            {"id": "zai-org/GLM-5.1", "context_window": 200000, "is_multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
            {"id": "zai-org/GLM-5", "context_window": 200000, "is_multimodal": True, "reasoning_efforts": ["low", "medium", "high"]},
            {"id": "moonshotai/Kimi-K2.6", "context_window": 200000, "is_multimodal": True, "reasoning_efforts": ["low", "high", "max"]},
            {"id": "moonshotai/Kimi-K2.5", "context_window": 200000, "is_multimodal": True, "reasoning_efforts": ["low", "high", "max"]},
            {"id": "MiniMaxAI/MiniMax-M2.7", "context_window": 200000, "is_multimodal": False, "reasoning_efforts": []},
            {"id": "MiniMaxAI/MiniMax-M2.5", "context_window": 200000, "is_multimodal": False, "reasoning_efforts": []},
            {"id": "Qwen/Qwen3.6-Max-Preview", "context_window": 200000, "is_multimodal": True, "reasoning_efforts": []},
            {"id": "Qwen/Qwen3.6-Plus", "context_window": 200000, "is_multimodal": True, "reasoning_efforts": []},
            {"id": "stepfun/Step-3.5-Flash", "context_window": 200000, "is_multimodal": False, "reasoning_efforts": []},
        ]
        return cmc_models

    url = f"{base}/models"

    headers: dict[str, str] = {}
    if api_format == "anthropic":
        if _scan_key:
            headers["x-api-key"] = _scan_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        if _scan_key:
            headers["Authorization"] = f"Bearer {_scan_key}"

    # plan-248-1258 M2.3 / plan-110-438 M1: 扫描请求同样走该供应商的代理配置
    from app.services.credential_service import provider_client_kwargs

    _client_opts: dict = {"timeout": SCAN_TIMEOUT, "headers": {"Accept-Encoding": "gzip, deflate"}}
    _client_opts.update(provider_client_kwargs(provider))
    async with httpx.AsyncClient(**_client_opts) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("供应商返回格式异常：缺少 data 列表")

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = it.get("id") or it.get("name")
        if not mid:
            continue
        # 部分网关在列表里带上下文窗口字段（命名不一，尽量兼容）
        ctx = None
        for k in ("context_window", "context_length", "max_context_tokens", "max_input_tokens"):
            v = it.get(k)
            if isinstance(v, int) and v > 0:
                ctx = v
                break
        # v2.2 (对齐 zcode 3.11): 目录补全——网关未返回时用内置模型目录补元数据
        from app.models.catalog import apply_metadata
        meta = apply_metadata(str(mid), context_window=ctx,
                              is_multimodal=bool(it.get("is_multimodal") or it.get("multimodal")),
                              reasoning_efforts=it.get("reasoning_efforts"))
        out.append({
            "id": str(mid),
            "context_window": meta["context_window"],
            "owned_by": it.get("owned_by"),
            "is_multimodal": meta["is_multimodal"],
            "reasoning_efforts": meta["reasoning_efforts"],
        })
    out.sort(key=lambda x: x["id"])
    return out


async def bulk_upsert_models(
    db: AsyncSession,
    provider_id: int,
    items: list[dict],
    *,
    preserve_existing_config: bool = False,
    default_multimodal: bool = False,
) -> list[int]:
    """按 (provider_id, name) upsert 模型配置（写引擎单写线程）；返回该供应商模型 id 列表。

    plan-20261123-ux-fixes：
    - preserve_existing_config=True（目录同步路径，如 antigravity_sync）：
      已存在模型保留用户配置好的上下文窗口、思考深度、多模态与启用状态；
      仅在上下文窗口缺失时补默认值。
    - default_multimodal=True：新同步模型默认开启多模态。
    - 扫描保存路径（preserve_existing_config=False）若未显式传入 reasoning_efforts，
      也不覆盖已存在模型原有的思考深度配置。
    """
    from app.persistence.database import run_write_locked

    def patch(s):
        provider = s.get(Provider, provider_id)
        if provider is None:
            raise ValueError("provider not found")

        existing = list(s.execute(
            select(Model).where(Model.provider_id == provider_id)
        ).scalars().all())
        by_name = {m.name: m for m in existing}

        for item in items:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            # v2.2 (对齐 zcode 3.11): 目录补全——用户未填元数据时自动补全
            from app.models.catalog import apply_metadata
            has_explicit_multimodal = "is_multimodal" in item and item.get("is_multimodal") is not None
            has_explicit_efforts = "reasoning_efforts" in item and item.get("reasoning_efforts") is not None
            _meta = apply_metadata(
                name,
                context_window=item.get("context_window"),
                is_multimodal=bool(item.get("is_multimodal", False)),
                reasoning_efforts=item.get("reasoning_efforts"),
            )
            m = by_name.get(name)
            is_new = m is None
            if is_new:
                m = Model(tenant_id=1, name=name, provider_id=provider_id, source_type="byok")
                s.add(m)
                by_name[name] = m

            if preserve_existing_config and not is_new:
                if not m.context_window:
                    m.context_window = _meta.get("context_window") or 200000
                continue

            m.is_active = bool(item.get("is_active", True))
            if is_new:
                m.context_window = _meta.get("context_window") or 200000
                if default_multimodal and not has_explicit_multimodal:
                    m.is_multimodal = True
                elif has_explicit_multimodal:
                    m.is_multimodal = bool(item["is_multimodal"])
                else:
                    m.is_multimodal = bool(_meta.get("is_multimodal", False))
                if _meta.get("reasoning_efforts") is not None:
                    m.reasoning_efforts = _meta["reasoning_efforts"]
            else:
                if item.get("context_window"):
                    m.context_window = item["context_window"]
                elif not m.context_window:
                    m.context_window = _meta.get("context_window") or 200000
                if has_explicit_multimodal:
                    m.is_multimodal = bool(item["is_multimodal"])
                if has_explicit_efforts:
                    m.reasoning_efforts = item["reasoning_efforts"]
            # plan-147-674: ta3 模型的手动多模态设置打 override 标记，
            # 目录重新同步时保留用户修正（catalog.py 同步逻辑读取该标记）
            if provider.api_format == "ta3":
                meta = dict(m.ta3_meta or {})
                meta["multimodal_override"] = True
                m.ta3_meta = meta
        s.flush()
        ids = [m.id for m in s.execute(
            select(Model).where(Model.provider_id == provider_id).order_by(Model.name.asc())
        ).scalars().all()]
        s.commit()
        return ids

    return await run_write_locked(patch, label="provider.bulk_upsert")


async def batch_update_models(
    db: AsyncSession,
    provider_id: int,
    model_ids: list[int],
    patch_fields: dict,
) -> int:
    """plan-20261123-ux-fixes：单事务批量修改某供应商下指定模型的配置（未传字段不改动）。"""
    from app.persistence.database import run_write_locked

    ids = [int(i) for i in dict.fromkeys(model_ids) if int(i) > 0]
    if not ids:
        return 0
    if "context_window" in patch_fields:
        ctx = patch_fields["context_window"]
        if not isinstance(ctx, int) or isinstance(ctx, bool) or ctx <= 0:
            raise ValueError("上下文窗口需为大于 0 的整数")

    def patch(s):
        provider = s.get(Provider, provider_id)
        if provider is None:
            raise ValueError("provider not found")
        rows = list(s.execute(
            select(Model).where(Model.provider_id == provider_id, Model.id.in_(ids))
        ).scalars().all())
        for m in rows:
            if "is_active" in patch_fields and patch_fields["is_active"] is not None:
                m.is_active = bool(patch_fields["is_active"])
            if "is_multimodal" in patch_fields and patch_fields["is_multimodal"] is not None:
                m.is_multimodal = bool(patch_fields["is_multimodal"])
                if provider.api_format == "ta3":
                    meta = dict(m.ta3_meta or {})
                    meta["multimodal_override"] = True
                    m.ta3_meta = meta
            if "context_window" in patch_fields and patch_fields["context_window"] is not None:
                m.context_window = int(patch_fields["context_window"])
            if "reasoning_efforts" in patch_fields:
                effs = patch_fields["reasoning_efforts"]
                m.reasoning_efforts = list(effs) if effs else []
        s.commit()
        return len(rows)

    return await run_write_locked(patch, label=f"provider.models.batch_update.{provider_id}")


async def batch_delete_models(
    db: AsyncSession,
    provider_id: int,
    model_ids: list[int],
) -> int:
    """plan-20261123-ux-fixes：单事务批量删除某供应商下指定模型（删除前置空外键引用）。"""
    from app.persistence.database import run_write_locked
    from app.services.model_service import detach_model_refs

    ids = [int(i) for i in dict.fromkeys(model_ids) if int(i) > 0]
    if not ids:
        return 0

    def patch(s):
        provider = s.get(Provider, provider_id)
        if provider is None:
            raise ValueError("provider not found")
        rows = list(s.execute(
            select(Model).where(Model.provider_id == provider_id, Model.id.in_(ids))
        ).scalars().all())
        found_ids = [m.id for m in rows]
        if not found_ids:
            return 0
        detach_model_refs(s, found_ids)
        for m in rows:
            s.delete(m)
        s.commit()
        return len(found_ids)

    return await run_write_locked(patch, label=f"provider.models.batch_delete.{provider_id}")


async def count_models(db: AsyncSession, provider_id: int) -> int:
    res = await db.execute(select(Model).where(Model.provider_id == provider_id))
    return len(res.scalars().all())


async def _test_kiro_connectivity(db: AsyncSession, provider_id: int, provider: Provider) -> dict:
    """kiro 连通性探针：GetUsageLimits（验证登录态 + 端点 + 网络，不消耗对话额度）。"""
    import time

    from app.auth.kiro import quota as kiro_quota
    from app.auth.kiro import session as kiro_session
    from app.services.credential_service import provider_client_kwargs

    t0 = time.monotonic()
    try:
        value = await kiro_quota.get_usage_limits(
            db, provider_id, provider, force=True,
            client_kwargs=provider_client_kwargs(provider))
    except kiro_session.KiroAuthError as e:
        return {"ok": False, "latency_ms": 0, "error": str(e), "model": None, "via": "quota"}
    except kiro_quota.KiroQuotaError as e:
        return {"ok": False, "latency_ms": 0, "error": str(e), "model": None, "via": "quota"}
    latency_ms = int((time.monotonic() - t0) * 1000)
    remaining = value.get("remaining")
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "error": None,
        "model": None,
        "via": "quota",
        "detail": f"剩余积分 {remaining}" if remaining is not None else None,
    }


async def test_connectivity(db: AsyncSession, provider_id: int) -> dict:
    """v2.2 (对齐 zcode 3.11): 连通性测试——发一条 max_tokens=1 的 ping。

    返回 {"ok": bool, "latency_ms": int, "error": str|None}。
    优先用该供应商下第一个 active 模型；无模型时用 /models 接口探测。

    plan-116-456：kiro 走专用探针（积分接口），同时验证登录态与端点可达，
    且不消耗对话额度（对话 ping 会产生 credit 费用）。
    """
    import time

    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise ValueError("provider not found")

    api_format = (provider.api_format or "openai").lower()
    if api_format == "kiro":
        return await _test_kiro_connectivity(db, provider_id, provider)

    if not provider.base_url:
        raise ValueError("供应商未配置 Base URL")

    base = provider.base_url.rstrip("/")
    # plan-248-1258: key 取自凭据（多 Key 时用首个可用）
    _test_key = await _resolve_scan_key(db, provider)

    headers: dict[str, str] = {}
    if api_format == "anthropic":
        if _test_key:
            headers["x-api-key"] = _test_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        if _test_key:
            headers["Authorization"] = f"Bearer {_test_key}"

    models = (await db.execute(
        select(Model).where(Model.provider_id == provider_id, Model.is_active == True)  # noqa: E712
    )).scalars().all()
    model_name = models[0].name if models else None

    # plan-248-1258 M2.3 / plan-110-438 M1: 连通性测试走该供应商的代理配置
    from app.services.credential_service import provider_client_kwargs

    _client_opts: dict = {"timeout": 15.0}
    _client_opts.update(provider_client_kwargs(provider))

    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(**_client_opts) as client:
            if model_name and api_format != "anthropic":
                url = f"{base}/chat/completions"
                body = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                }
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
            else:
                url = f"{base}/models"
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency_ms, "error": None,
                "model": model_name, "via": "chat" if model_name else "models"}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "latency_ms": 0,
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}", "model": model_name}
    except httpx.RequestError as e:
        return {"ok": False, "latency_ms": 0, "error": f"连接失败: {e.__class__.__name__}", "model": model_name}
    except Exception as e:
        return {"ok": False, "latency_ms": 0, "error": str(e)[:200], "model": model_name}
