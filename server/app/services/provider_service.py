"""供应商（Provider）CRUD + 模型扫描 + 批量模型配置。"""
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.model_reg import Model, Provider

logger = logging.getLogger(__name__)

SCAN_TIMEOUT = 20.0


async def create_provider(db: AsyncSession, **kwargs) -> Provider:
    provider = Provider(tenant_id=1, **kwargs)
    db.add(provider)
    await db.flush()
    return provider


async def get_provider(db: AsyncSession, provider_id: int) -> Provider | None:
    return await db.get(Provider, provider_id)


async def list_providers(db: AsyncSession) -> list[Provider]:
    res = await db.execute(select(Provider).order_by(Provider.id.asc()))
    return list(res.scalars().all())


async def update_provider(db: AsyncSession, provider_id: int, **kwargs) -> Provider | None:
    """更新供应商字段(只更新非 None 字段)。api_key 空字符串 = 清除。"""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        return None
    for k, v in kwargs.items():
        if v is None:
            continue
        if k == "api_key" and v == "":
            setattr(provider, k, None)
        else:
            setattr(provider, k, v)
    await db.flush()
    return provider


async def delete_provider(db: AsyncSession, provider_id: int) -> bool:
    """删除供应商，并级联删除其下模型（会话 model_id 会悬空，前端选择器自动忽略）。"""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        return False
    models = (await db.execute(select(Model).where(Model.provider_id == provider_id))).scalars().all()
    for m in models:
        await db.delete(m)
    await db.delete(provider)
    await db.flush()
    return True


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
    url = f"{base}/models"
    api_format = (provider.api_format or "openai").lower()

    headers: dict[str, str] = {}
    if api_format == "anthropic":
        if provider.api_key:
            headers["x-api-key"] = provider.api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

    async with httpx.AsyncClient(timeout=SCAN_TIMEOUT, headers={"Accept-Encoding": "gzip, deflate"}) as client:
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


async def bulk_upsert_models(db: AsyncSession, provider_id: int, items: list[dict]) -> list[Model]:
    """按 (provider_id, name) upsert 模型配置；返回该供应商下全部模型。"""
    provider = await db.get(Provider, provider_id)
    if provider is None:
        raise ValueError("provider not found")

    existing = (await db.execute(
        select(Model).where(Model.provider_id == provider_id)
    )).scalars().all()
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
            db.add(m)
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
    await db.flush()

    res = await db.execute(select(Model).where(Model.provider_id == provider_id).order_by(Model.name.asc()))
    return list(res.scalars().all())


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

    headers: dict[str, str] = {}
    if api_format == "anthropic":
        if provider.api_key:
            headers["x-api-key"] = provider.api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

    models = (await db.execute(
        select(Model).where(Model.provider_id == provider_id, Model.is_active == True)  # noqa: E712
    )).scalars().all()
    model_name = models[0].name if models else None

    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=15.0) as client:
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
