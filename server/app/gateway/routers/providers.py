"""供应商管理 API（v16）。"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (
    ModelOut,
    ProviderCreate,
    ProviderModelsBulkIn,
    ProviderOut,
    ProviderScanOut,
    ProviderUpdate,
)
from app.persistence.database import get_db
from app.services import provider_service

logger = logging.getLogger(__name__)
router = APIRouter()


async def _to_out(db: AsyncSession, p) -> ProviderOut:
    return ProviderOut(
        id=p.id,
        name=p.name,
        base_url=p.base_url,
        api_format=p.api_format or "openai",
        is_active=p.is_active,
        has_api_key=bool(p.api_key),
        model_count=await provider_service.count_models(db, p.id),
        auth_status=getattr(p, "auth_status", None),
        account_label=getattr(p, "account_label", None),
        created_at=str(p.created_at) if p.created_at else None,
    )


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(db: AsyncSession = Depends(get_db)):
    providers = await provider_service.list_providers(db)
    return [await _to_out(db, p) for p in providers]


@router.post("/providers", response_model=ProviderOut)
async def create_provider(body: ProviderCreate, db: AsyncSession = Depends(get_db)):
    provider = await provider_service.create_provider(
        db,
        name=body.name.strip(),
        base_url=body.base_url,
        api_key=body.api_key,
        api_format=body.api_format,
        is_active=body.is_active,
    )
    await db.commit()
    return await _to_out(db, provider)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: int, body: ProviderUpdate, db: AsyncSession = Depends(get_db)):
    provider = await provider_service.update_provider(
        db, provider_id,
        name=body.name.strip() if body.name else None,
        base_url=body.base_url,
        api_key=body.api_key,
        api_format=body.api_format,
        is_active=body.is_active,
    )
    if provider is None:
        raise HTTPException(404, "provider not found")
    await db.commit()
    return await _to_out(db, provider)


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    ok = await provider_service.delete_provider(db, provider_id)
    if not ok:
        raise HTTPException(404, "provider not found")
    await db.commit()
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
    """批量保存扫描结果中用户勾选的模型配置（upsert）。"""
    try:
        models = await provider_service.bulk_upsert_models(
            db, provider_id, [item.model_dump() for item in body.models],
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    await db.commit()
    return [_model_to_out(m) for m in models]
