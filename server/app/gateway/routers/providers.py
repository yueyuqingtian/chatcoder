"""供应商管理 API（v16）。"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (
    ModelOut,
    ProviderCreate,
    ProviderCredentialCreate,
    ProviderCredentialOut,
    ProviderCredentialUpdate,
    ProviderModelsBulkIn,
    ProviderOut,
    ProviderProxyTestOut,
    ProviderScanOut,
    ProviderUpdate,
)
from app.persistence.database import get_db
from app.services import credential_service, provider_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _mask_key(key: str | None) -> str | None:
    """plan-248-1258: 凭据 key 掩码预览（不泄露完整 key）。"""
    if not key:
        return None
    if len(key) <= 8:
        return "••••" + key[-2:]
    return f"{key[:4]}••••{key[-4:]}"


async def _cred_to_out(c) -> ProviderCredentialOut:
    return ProviderCredentialOut(
        id=c.id,
        provider_id=c.provider_id,
        label=c.label,
        has_api_key=bool(c.api_key),
        api_key_preview=_mask_key(c.api_key),
        token_ref=c.token_ref,
        priority=c.priority or 0,
        is_active=c.is_active,
        status=c.status or "ok",
        last_error=c.last_error,
        cooldown_until=c.cooldown_until,
        last_ok_at=c.last_ok_at,
        credits=float(c.credits) if c.credits is not None else None,
        extra=c.extra,
    )


async def _to_out(db: AsyncSession, p) -> ProviderOut:
    # plan-248-1258 M2.2: 附带凭据统计，供前端徽标显示「N 个 Key/账号」
    creds = await credential_service.list_credentials(db, p.id)
    active_creds = credential_service.available_credentials(creds)
    return ProviderOut(
        id=p.id,
        name=p.name,
        base_url=p.base_url,
        api_format=p.api_format or "openai",
        is_active=p.is_active,
        has_api_key=bool(p.api_key) or bool(creds),
        model_count=await provider_service.count_models(db, p.id),
        auth_status=getattr(p, "auth_status", None),
        account_label=getattr(p, "account_label", None),
        created_at=str(p.created_at) if p.created_at else None,
        proxy_mode=getattr(p, "proxy_mode", None) or "inherit",
        proxy_url=getattr(p, "proxy_url", None),
        credential_count=len(creds),
        active_credential_count=len(active_creds),
        # plan-271-1364: 凭据取用策略（sticky | round_robin）
        credential_strategy=getattr(p, "credential_strategy", None) or "sticky",
    )


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(db: AsyncSession = Depends(get_db)):
    providers = await provider_service.list_providers(db)
    return [await _to_out(db, p) for p in providers]


@router.post("/providers", response_model=ProviderOut)
async def create_provider(body: ProviderCreate, db: AsyncSession = Depends(get_db)):
    pid = await provider_service.create_provider(
        db,
        name=body.name.strip(),
        base_url=body.base_url,
        api_key=body.api_key,
        api_format=body.api_format,
        is_active=body.is_active,
        proxy_mode=body.proxy_mode or "inherit",
        proxy_url=body.proxy_url,
        credential_strategy=body.credential_strategy or "sticky",
    )
    # plan-248-1258 M2.1: 新建供应商时 api_key 作为首条凭据落库（统一凭据模型）
    if body.api_key:
        await credential_service.create_credential(
            db, pid, label="默认凭据", api_key=body.api_key, priority=0, is_active=True,
        )
    provider = await provider_service.get_provider(db, pid)  # async 只读
    return await _to_out(db, provider)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: int, body: ProviderUpdate, db: AsyncSession = Depends(get_db)):
    ok = await provider_service.update_provider(
        db, provider_id,
        name=body.name.strip() if body.name else None,
        base_url=body.base_url,
        api_key=body.api_key,
        api_format=body.api_format,
        is_active=body.is_active,
        proxy_mode=body.proxy_mode,
        proxy_url=body.proxy_url,
        credential_strategy=body.credential_strategy,
    )
    if not ok:
        raise HTTPException(404, "provider not found")
    provider = await provider_service.get_provider(db, provider_id)  # async 只读
    return await _to_out(db, provider)


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    ok = await provider_service.delete_provider(db, provider_id)
    if not ok:
        raise HTTPException(404, "provider not found")
    return {"ok": True}


@router.post("/providers/{provider_id}/test", response_model=dict)
async def test_provider_connectivity(provider_id: int, db: AsyncSession = Depends(get_db)):
    """v2.2 (对齐 zcode 3.11): 连通性测试（一条 max_tokens=1 的 ping）。"""
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() in ("ta3", "trae", "workbuddy"):
        raise HTTPException(400, "登录态供应商（ta3/trae/workbuddy）请使用「同步模型」验证连通性")
    try:
        result = await provider_service.test_connectivity(db, provider_id)
    except ValueError as e:
        msg = str(e)
        if msg == "provider not found":
            raise HTTPException(404, msg)
        raise HTTPException(400, msg)
    return result


@router.post("/providers/{provider_id}/scan", response_model=ProviderScanOut)
async def scan_provider_models(provider_id: int, db: AsyncSession = Depends(get_db)):
    """扫描供应商支持的模型列表。"""
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    if (provider.api_format or "openai").lower() in ("ta3", "trae", "workbuddy"):
        raise HTTPException(400, "登录态供应商的模型目录来自账号登录，请使用「同步模型」")
    try:
        models = await provider_service.scan_models(db, provider_id)
    except ValueError as e:
        msg = str(e)
        if msg == "provider not found":
            raise HTTPException(404, msg)
        raise HTTPException(400, msg)
    except httpx.HTTPStatusError as e:
        logger.warning("供应商模型扫描失败: %s", e)
        raise HTTPException(502, f"供应商返回错误: HTTP {e.response.status_code}")
    except httpx.RequestError as e:
        logger.warning("供应商模型扫描连接失败: %s", e)
        raise HTTPException(502, f"无法连接供应商: {e.__class__.__name__}")
    return ProviderScanOut(models=models)


def _model_to_out(m) -> ModelOut:
    tmeta = getattr(m, "trae_meta", None) or {}
    if not isinstance(tmeta, dict):
        tmeta = {}
    return ModelOut(
        id=m.id,
        name=m.name,
        provider=m.provider,
        provider_id=m.provider_id,
        base_url=m.base_url,
        intelligence_level=m.intelligence_level,
        context_window=m.context_window,
        source_type=m.source_type,
        is_active=m.is_active,
        is_multimodal=getattr(m, "is_multimodal", False),
        api_format=getattr(m, "api_format", "openai"),
        has_api_key=bool(getattr(m, "api_key", None)),
        reasoning_efforts=getattr(m, "reasoning_efforts", None) or [],
        trae_max_context=tmeta.get("context_window_max"),
        trae_consumption_rate=tmeta.get("consumption_rate"),
        trae_available=bool(tmeta.get("is_available")),
        trae_thinking=bool(tmeta.get("thinking")),
    )


@router.get("/providers/{provider_id}/models", response_model=list[ModelOut])
async def list_provider_models(provider_id: int, db: AsyncSession = Depends(get_db)):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    from sqlalchemy import select

    from app.persistence.models.model_reg import Model
    res = await db.execute(select(Model).where(Model.provider_id == provider_id).order_by(Model.name.asc()))
    return [_model_to_out(m) for m in res.scalars().all()]


@router.post("/providers/{provider_id}/models", response_model=list[ModelOut])
async def bulk_save_provider_models(provider_id: int, body: ProviderModelsBulkIn, db: AsyncSession = Depends(get_db)):
    """批量保存扫描结果中用户勾选的模型配置（upsert，写引擎单写线程）。"""
    try:
        await provider_service.bulk_upsert_models(
            db, provider_id, [item.model_dump() for item in body.models],
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    from sqlalchemy import select
    from app.persistence.models.model_reg import Model
    res = await db.execute(select(Model).where(Model.provider_id == provider_id).order_by(Model.name.asc()))
    return [_model_to_out(m) for m in res.scalars().all()]


# ── plan-248-1258 M2.2: 供应商凭据（多 API Key / 多登录账号）──


async def _require_provider(db: AsyncSession, provider_id: int):
    provider = await provider_service.get_provider(db, provider_id)
    if provider is None:
        raise HTTPException(404, "provider not found")
    return provider


@router.get("/providers/{provider_id}/credentials", response_model=list[ProviderCredentialOut])
async def list_credentials(provider_id: int, db: AsyncSession = Depends(get_db)):
    """列出该供应商的全部凭据（按 priority）。"""
    await _require_provider(db, provider_id)
    creds = await credential_service.list_credentials(db, provider_id)
    return [await _cred_to_out(c) for c in creds]


@router.post("/providers/{provider_id}/credentials", response_model=ProviderCredentialOut)
async def create_credential(provider_id: int, body: ProviderCredentialCreate,
                            db: AsyncSession = Depends(get_db)):
    """新增一条凭据（API Key 或 OAuth 账号引用）。"""
    await _require_provider(db, provider_id)
    cid = await credential_service.create_credential(
        db, provider_id,
        label=body.label, api_key=body.api_key, token_ref=body.token_ref,
        priority=body.priority, is_active=body.is_active,
        status="ok" if body.api_key else "disabled",
        extra=body.extra,
    )
    cred = await credential_service.get_credential(db, cid)
    return await _cred_to_out(cred)


@router.patch("/credentials/{credential_id}", response_model=ProviderCredentialOut)
async def update_credential(credential_id: int, body: ProviderCredentialUpdate,
                            db: AsyncSession = Depends(get_db)):
    """编辑凭据（label / key / 优先级 / 启停）。"""
    ok = await credential_service.update_credential(
        db, credential_id,
        label=body.label, api_key=body.api_key, priority=body.priority,
        is_active=body.is_active, extra=body.extra,
    )
    if not ok:
        raise HTTPException(404, "credential not found")
    cred = await credential_service.get_credential(db, credential_id)
    return await _cred_to_out(cred)


@router.delete("/credentials/{credential_id}")
async def delete_credential(credential_id: int, db: AsyncSession = Depends(get_db)):
    ok = await credential_service.delete_credential(db, credential_id)
    if not ok:
        raise HTTPException(404, "credential not found")
    return {"ok": True}


@router.post("/providers/{provider_id}/proxy-test", response_model=ProviderProxyTestOut)
async def test_provider_proxy(provider_id: int, db: AsyncSession = Depends(get_db)):
    """plan-248-1258 M2.3: 按该供应商的代理配置做一次连通性探测。

    直连 base_url 根路径（HEAD/GET），验证代理是否可达；仅验证网络层，
    不发送模型请求（避免消耗配额）。
    """
    import time

    provider = await _require_provider(db, provider_id)
    if not provider.base_url:
        raise HTTPException(400, "供应商未配置 Base URL")
    proxy = credential_service.resolve_proxy(provider)
    opts: dict = {"timeout": 10.0, "follow_redirects": True}
    if proxy:
        opts["proxy"] = proxy
    elif credential_service.proxy_disabled(provider):
        opts["trust_env"] = False
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(**opts) as client:
            resp = await client.get(provider.base_url.rstrip("/") + "/models")
        return ProviderProxyTestOut(
            ok=resp.status_code < 500,
            latency_ms=int((time.monotonic() - t0) * 1000),
            proxy=proxy,
            error=None if resp.status_code < 500 else f"HTTP {resp.status_code}",
        )
    except httpx.HTTPError as e:
        return ProviderProxyTestOut(ok=False, latency_ms=0, proxy=proxy, error=str(e)[:300])
