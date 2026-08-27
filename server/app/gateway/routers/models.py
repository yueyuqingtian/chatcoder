"""模型管理 API（v2）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import ModelCreate, ModelOut, ModelUpdate
from app.persistence.database import get_db
from app.persistence.models.agent import Agent
from app.services import model_service

router = APIRouter()


def _to_out(m, provider_name: str | None = None) -> ModelOut:
    tmeta = getattr(m, "trae_meta", None) or {}
    if not isinstance(tmeta, dict):
        tmeta = {}
    return ModelOut(
        id=m.id,
        name=m.name,
        provider=m.provider,
        provider_id=getattr(m, "provider_id", None),
        provider_name=provider_name,
        base_url=m.base_url,
        intelligence_level=m.intelligence_level,
        context_window=m.context_window,
        source_type=m.source_type,
        is_active=m.is_active,
        is_multimodal=getattr(m, "is_multimodal", False),
        api_format=getattr(m, "api_format", "openai"),
        has_api_key=bool(getattr(m, "api_key", None)),
        reasoning_efforts=getattr(m, "reasoning_efforts", None) or [],
        # trae 供应商扩展字段（与 providers.py _model_to_out 对齐）：
        # 缺这些字段时前端会把 trae 组整个过滤掉（trae_available=undefined → 过滤）
        trae_max_context=tmeta.get("context_window_max"),
        trae_consumption_rate=tmeta.get("consumption_rate"),
        trae_available=bool(tmeta.get("is_available")),
        trae_thinking=bool(tmeta.get("thinking")),
    )


@router.post("/models", response_model=ModelOut)
async def create_model(body: ModelCreate, db: AsyncSession = Depends(get_db)):
    model = await model_service.create_model(
        db,
        name=body.name,
        provider=body.provider,
        provider_id=body.provider_id,
        base_url=body.base_url,
        intelligence_level=body.intelligence_level,
        context_window=body.context_window,
        source_type=body.source_type,
        is_active=body.is_active,
        is_multimodal=body.is_multimodal,
        api_format=body.api_format,
        api_key=body.api_key,
        reasoning_efforts=body.reasoning_efforts,
    )
    # v1.3: 自动绑定到未绑定模型的 main agent，让用户配置后直接可用
    if body.is_active:
        res = await db.execute(select(Agent).where(Agent.kind == "main").limit(1))
        main_agent = res.scalars().first()
        if main_agent and not main_agent.model_id:
            main_agent.model_id = model.id
            await db.flush()
    await db.commit()
    return _to_out(model)


@router.get("/models", response_model=list[ModelOut])
async def list_models(db: AsyncSession = Depends(get_db)):
    models = await model_service.list_models(db)
    # v16: 附带供应商名，前端选择器按供应商分组展示
    from app.persistence.models.model_reg import Provider
    provider_ids = {m.provider_id for m in models if getattr(m, "provider_id", None)}
    provider_names: dict[int, str] = {}
    if provider_ids:
        res = await db.execute(select(Provider).where(Provider.id.in_(provider_ids)))
        provider_names = {p.id: p.name for p in res.scalars().all()}
    return [_to_out(m, provider_names.get(getattr(m, "provider_id", None))) for m in models]


@router.patch("/models/{model_id}", response_model=ModelOut)
async def update_model(model_id: int, body: ModelUpdate, db: AsyncSession = Depends(get_db)):
    """编辑模型配置。api_key 传空字符串则清除。"""
    model = await model_service.update_model(
        db, model_id,
        name=body.name,
        provider=body.provider,
        provider_id=body.provider_id,
        base_url=body.base_url,
        intelligence_level=body.intelligence_level,
        context_window=body.context_window,
        is_active=body.is_active,
        is_multimodal=body.is_multimodal,
        api_format=body.api_format,
        api_key=body.api_key,
        reasoning_efforts=body.reasoning_efforts,
    )
    if model is None:
        raise HTTPException(404, "model not found")
    await db.commit()
    return _to_out(model)


@router.delete("/models/{model_id}")
async def delete_model(model_id: int, db: AsyncSession = Depends(get_db)):
    ok = await model_service.delete_model(db, model_id)
    if not ok:
        raise HTTPException(404, "model not found")
    await db.commit()
    return {"ok": True}
