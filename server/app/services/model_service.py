"""模型注册 CRUD。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.model_reg import Model


async def create_model(db: AsyncSession, **kwargs) -> Model:
    model = Model(tenant_id=1, **kwargs)
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
    await db.flush()
    return model


async def delete_model(db: AsyncSession, model_id: int) -> bool:
    model = await db.get(Model, model_id)
    if model is None:
        return False
    await db.delete(model)
    await db.flush()
    return True
