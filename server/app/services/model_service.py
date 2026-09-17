"""模型注册 CRUD。"""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.model_reg import Model


def detach_model_refs(s, model_ids: list[int]) -> None:
    """删除模型前把引用方（会话/代理/子代理配置）的 model_id 置空。

    三张表以 FK 引用 models.id（sqlite 开启了外键约束），直接删除模型会抛
    sqlite3.IntegrityError: FOREIGN KEY constraint failed。置空后引用方走
    「跟随默认/前端选择器自动忽略」语义。
    """
    if not model_ids:
        return
    from app.persistence.models.agent import Agent
    from app.persistence.models.message import Session
    from app.persistence.models.subagent_profile import SubagentProfile

    s.execute(update(Session).where(Session.model_id.in_(model_ids)).values(model_id=None))
    s.execute(update(Agent).where(Agent.model_id.in_(model_ids)).values(model_id=None))
    s.execute(update(SubagentProfile).where(SubagentProfile.model_id.in_(model_ids)).values(model_id=None))


async def create_model(db: AsyncSession, **kwargs) -> int:
    from app.persistence.database import run_write_locked

    def patch(s):
        model = Model(tenant_id=1, **kwargs)
        # plan-156-739: 新建 ta3 模型若带 is_multimodal，同步打 multimodal_override 标记，
        # 防止目录同步覆盖（对齐 provider_service.bulk_upsert_models 语义）。
        if kwargs.get("is_multimodal") and getattr(model, "api_format", "") == "ta3":
            meta = dict(model.ta3_meta or {})
            meta["multimodal_override"] = True
            model.ta3_meta = meta
        s.add(model)
        s.flush()
        mid = model.id
        s.commit()
        return mid

    return await run_write_locked(patch, label="model.create")


async def get_model(db: AsyncSession, model_id: int) -> Model | None:
    return await db.get(Model, model_id)


async def list_models(db: AsyncSession) -> list[Model]:
    res = await db.execute(select(Model).order_by(Model.id.desc()))
    return list(res.scalars().all())


async def update_model(db: AsyncSession, model_id: int, **kwargs) -> bool:
    """更新模型字段(只更新非 None 的字段；写引擎单写线程)。返回可否找到。

    特殊处理: api_key 传空字符串 "" 表示清除密钥。
    """
    from app.persistence.database import run_write_locked

    def patch(s):
        model = s.get(Model, model_id)
        if model is None:
            return False
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
        s.commit()
        return True

    return await run_write_locked(patch, label=f"model.update.{model_id}")


async def delete_model(db: AsyncSession, model_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        model = s.get(Model, model_id)
        if model is None:
            return False
        detach_model_refs(s, [model_id])
        s.delete(model)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"model.delete.{model_id}")
