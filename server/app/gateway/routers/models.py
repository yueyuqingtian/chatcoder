"""模型管理 API（v2）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import ModelCreate, ModelOut, ModelUpdate
from app.persistence.database import get_db
from app.persistence.models.agent import Agent
from app.services import model_service

router = APIRouter()


def _to_out(m, provider_name: str | None = None, provider_active: bool = True,
            provider_sort_order: int = 0) -> ModelOut:
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
        # plan-248-1258 M2.4: 供应商启用状态（禁用供应商时前端选择器过滤其模型）
        provider_active=provider_active,
        # plan-89-386: 供应商排序位（全局模型选择器按设置页顺序展示）
        provider_sort_order=provider_sort_order,
    )


@router.post("/models", response_model=ModelOut)
async def create_model(body: ModelCreate, db: AsyncSession = Depends(get_db)):
    model_id = await model_service.create_model(
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
            await _patch_main_agent_model(main_agent.id, model_id)
    model = await model_service.get_model(db, model_id)  # async 只读
    return _to_out(model)


@router.get("/models", response_model=list[ModelOut])
async def list_models(db: AsyncSession = Depends(get_db)):
    models = await model_service.list_models(db)
    # v16: 附带供应商名，前端选择器按供应商分组展示
    # plan-248-1258 M2.4: 同时带供应商启用状态（禁用供应商的模型前端需过滤）
    from app.persistence.models.model_reg import Provider
    provider_ids = {m.provider_id for m in models if getattr(m, "provider_id", None)}
    provider_names: dict[int, str] = {}
    provider_active: dict[int, bool] = {}
    # plan-89-386: 供应商排序位（全局模型选择器按「设置-模型管理」的顺序展示供应商）
    provider_sort: dict[int, int] = {}
    if provider_ids:
        res = await db.execute(select(Provider).where(Provider.id.in_(provider_ids)))
        for p in res.scalars().all():
            provider_names[p.id] = p.name
            provider_active[p.id] = bool(p.is_active)
            provider_sort[p.id] = int(getattr(p, "sort_order", 0) or 0)
    return [
        _to_out(
            m,
            provider_names.get(getattr(m, "provider_id", None)),
            # 独立模型（无供应商）默认视为可用
            provider_active.get(getattr(m, "provider_id", None), True),
            provider_sort.get(getattr(m, "provider_id", None), 0),
        )
        for m in models
    ]


@router.patch("/models/{model_id}", response_model=ModelOut)
async def update_model(model_id: int, body: ModelUpdate, db: AsyncSession = Depends(get_db)):
    """编辑模型配置。api_key 传空字符串则清除。"""
    ok = await model_service.update_model(
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
    if not ok:
        raise HTTPException(404, "model not found")
    model = await model_service.get_model(db, model_id)  # async 只读
    return _to_out(model)


@router.delete("/models/{model_id}")
async def delete_model(model_id: int, db: AsyncSession = Depends(get_db)):
    ok = await model_service.delete_model(db, model_id)
    if not ok:
        raise HTTPException(404, "model not found")
    return {"ok": True}


async def _patch_main_agent_model(agent_id: int, model_id: int) -> None:
    """写引擎单写线程绑定 main agent 的 model_id。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.agent import Agent
        a = s.get(Agent, agent_id)
        if a is not None:
            a.model_id = model_id
            s.commit()

    await run_write_locked(patch, label="agent.bind_model")
