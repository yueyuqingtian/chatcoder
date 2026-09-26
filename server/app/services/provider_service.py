"""供应商（Provider）CRUD + 模型扫描 + 批量模型配置。"""
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.model_reg import Model, Provider

logger = logging.getLogger(__name__)

SCAN_TIMEOUT = 20.0


async def create_provider(db: AsyncSession, **kwargs) -> int:
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
    return await db.get(Provider, provider_id)


async def list_providers(db: AsyncSession) -> list[Provider]:
    # plan-41-225: 按用户自定义顺序（sort_order）排序，id 兜底保证顺序稳定不抖动
    # （老库迁移后 sort_order 全为 0 → 等价于原 id 升序，升级零感知）。
    res = await db.execute(
        select(Provider).order_by(Provider.sort_order.asc(), Provider.id.asc())
    )
    return list(res.scalars().all())


async def update_provider(db: AsyncSession, provider_id: int, **kwargs) -> bool:
    """更新供应商字段(只更新非 None 字段；写引擎单写线程)。api_key 空字符串 = 清除。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        provider = s.get(Provider, provider_id)
        if provider is None:
            return False
        for k, v in kwargs.items():
            if v is None:
                continue
            if k == "api_key" and v == "":
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

    OAuth 类供应商（workbuddy/ta3/trae）的凭据 key 来自各自 auth 表的 access_token。
    """
    from app.services import credential_service

    creds = await credential_service.list_credentials(db, provider.id)
    fmt = (provider.api_format or "openai").lower()
    for c in credential_service.available_credentials(creds):
        if c.api_key:
            return c.api_key
        if fmt in ("workbuddy", "ta3", "trae"):
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
    if not provider.base_url:
        raise ValueError("供应商未配置 Base URL")

    base = provider.base_url.rstrip("/")
    api_format = (provider.api_format or "openai").lower()
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

    # plan-248-1258 M2.3: 扫描请求同样走该供应商的代理配置
    from app.services.credential_service import proxy_disabled, resolve_proxy

    _proxy = resolve_proxy(provider)
    _client_opts: dict = {"timeout": SCAN_TIMEOUT, "headers": {"Accept-Encoding": "gzip, deflate"}}
    if _proxy:
        _client_opts["proxy"] = _proxy
    elif proxy_disabled(provider):
        _client_opts["trust_env"] = False
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


async def bulk_upsert_models(db: AsyncSession, provider_id: int, items: list[dict]) -> list[int]:
    """按 (provider_id, name) upsert 模型配置（写引擎单写线程）；返回该供应商模型 id 列表。"""
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
            _meta = apply_metadata(
                name,
                context_window=item.get("context_window"),
                is_multimodal=bool(item.get("is_multimodal", False)),
                reasoning_efforts=item.get("reasoning_efforts"),
            )
            m = by_name.get(name)
            if m is None:
                m = Model(tenant_id=1, name=name, provider_id=provider_id, source_type="byok")
                s.add(m)
                by_name[name] = m
            m.is_active = bool(item.get("is_active", True))
            if _meta.get("context_window"):
                m.context_window = _meta["context_window"]
            elif m.context_window is None:
                m.context_window = 200000
            m.is_multimodal = bool(_meta.get("is_multimodal", False))
            if _meta.get("reasoning_efforts") is not None:
                m.reasoning_efforts = _meta["reasoning_efforts"]
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


async def count_models(db: AsyncSession, provider_id: int) -> int:
    res = await db.execute(select(Model).where(Model.provider_id == provider_id))
    return len(res.scalars().all())


async def test_connectivity(db: AsyncSession, provider_id: int) -> dict:
    """v2.2 (对齐 zcode 3.11): 连通性测试——发一条 max_tokens=1 的 ping。

    返回 {"ok": bool, "latency_ms": int, "error": str|None}。
    优先用该供应商下第一个 active 模型；无模型时用 /models 接口探测。
    """
    import time

    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise ValueError("provider not found")
    if not provider.base_url:
        raise ValueError("供应商未配置 Base URL")

    base = provider.base_url.rstrip("/")
    api_format = (provider.api_format or "openai").lower()
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

    # plan-248-1258 M2.3: 连通性测试走该供应商的代理配置
    from app.services.credential_service import proxy_disabled, resolve_proxy

    _proxy = resolve_proxy(provider)
    _client_opts: dict = {"timeout": 15.0}
    if _proxy:
        _client_opts["proxy"] = _proxy
    elif proxy_disabled(provider):
        _client_opts["trust_env"] = False

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
