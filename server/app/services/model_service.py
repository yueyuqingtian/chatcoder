"""模型注册 CRUD。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.model_reg import Model


async def create_model(db: AsyncSession, **kwargs) -> Model:
    model = Model(tenant_id=1, **kwargs)
    # plan-156-739: 新建 ta3 模型若带 is_multimodal，同步打 multimodal_override 标记，
    # 防止目录同步覆盖（对齐 provider_service.bulk_upsert_models 语义）。
    if kwargs.get("is_multimodal") and getattr(model, "api_format", "") == "ta3":
        meta = dict(model.ta3_meta or {})
        meta["multimodal_override"] = True
        model.ta3_meta = meta
    db.add(model)
    await db.flush()
    return model


async def get_model(db: AsyncSession, model_id: int) -> Model | None:
    return await db.get(Model, model_id)


async def list_models(db: AsyncSession) -> list[Model]:
    res = await db.execute(select(Model).order_by(Model.id.desc()))
    return list(res.scalars().all())


async def update_model(db: AsyncSession, model_id: int, **kwargs) -> Model | None:
    """更新模型字段(只更新非 None 的字段)。

    特殊处理: api_key 传空字符串 "" 表示清除密钥。
    """
    model = await db.get(Model, model_id)
    if model is None:
        return None
    for k, v in kwargs.items():
        if v is None:
            continue
        # api_key 空字符串 = 清除
        if k == "api_key" and v == "":
            setattr(model, k, None)
        else:
            setattr(model, k, v)
    # plan-156-739: ta3 模型手动修改 is_multimodal → 写 multimodal_override 标记，
    # 防止目录同步（catalog.py 仅在 override 存在时保留用户设置）把用户手动开启的
    # 多模态覆盖回目录判定值。参照 provider_service.bulk_upsert_models 语义。
    # 注意：必须复制新 dict 再赋回，避免 SQLAlchemy 对 JSON 列同一对象引用不触发变更检测。
    if "is_multimodal" in kwargs and getattr(model, "api_format", "") == "ta3":
        meta = dict(model.ta3_meta or {})
        meta["multimodal_override"] = True
        model.ta3_meta = meta
    await db.flush()
    return model


async def delete_model(db: AsyncSession, model_id: int) -> bool:
    model = await db.get(Model, model_id)
    if model is None:
        return False
    await db.delete(model)
    await db.flush()
    return True
