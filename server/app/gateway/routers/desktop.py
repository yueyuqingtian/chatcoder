"""电脑操控路由（plan-334-1661）——操作路线管理与内核状态。

与内置 MCP 的状态宿主端点（debug.py）思路一致：状态由主服务持有，前端通过本组端点管理。
操作路线是用户可见可编辑的数据，因此提供完整的列表/编辑/批量删除/导入导出接口。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import desktop_env
from app.persistence.database import get_db
from app.services import desktop_recipe_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/desktop", tags=["desktop"])

MAX_BATCH_DELETE = 500


class RecipeListQuery(BaseModel):
    keyword: str = ""
    limit: int = 200


class RecipeIn(BaseModel):
    app_name: str = Field(min_length=1, max_length=120)
    intent: str = Field(min_length=1, max_length=255)
    principle: str = Field(min_length=1)
    pitfalls: str = ""
    steps: list | None = None


class RecipeUpdateIn(BaseModel):
    app_name: str | None = None
    intent: str | None = None
    principle: str | None = None
    pitfalls: str | None = None


class RecipeDeleteIn(BaseModel):
    ids: list[int]


# ── 内核状态 ──────────────────────────────────────────────


@router.get("/status")
async def desktop_status() -> dict:
    """报告内核可用性与运行状态（设置页「检测」按钮用）。"""
    exe = desktop_env.resolve_core_path()
    running = False
    info: dict = {}
    if exe:
        try:
            core = desktop_env.get_desktop_core()
            running = core.running
        except Exception as exc:  # noqa: BLE001
            logger.debug("[desktop] 内核状态探测失败: %s", exc)
    return {
        "core_found": bool(exe),
        "core_path": exe or "",
        "running": running,
        "info": info,
    }


@router.post("/probe")
async def desktop_probe() -> dict:
    """主动启动内核并取回环境信息，用于设置页「立即检测」。

    与 /status 的区别：本端点会真的拉起内核，因此能反映实际可用性，
    而不仅是「文件是否存在」。
    """
    try:
        core = desktop_env.get_desktop_core()
        info = await core.call("info")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"内核不可用：{exc}") from exc
    return {"ok": True, "info": info}


# ── 操作路线 ──────────────────────────────────────────────

# 注意：静态路径（/recipes/export、/recipes/import）必须声明在 /recipes/{id} 之前，
# 否则 "export" 会被当作 recipe_id 解析而导致 422。

@router.get("/recipes/export")
async def export_recipes(db: AsyncSession = Depends(get_db)) -> dict:
    return await desktop_recipe_service.export_recipes(db)


@router.post("/recipes/import")
async def import_recipes(payload: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        result = await desktop_recipe_service.import_recipes(db, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return result


@router.get("/recipes")
async def list_recipes(keyword: str = "", limit: int = 200, db: AsyncSession = Depends(get_db)) -> dict:
    rows = await desktop_recipe_service.list_recipes(db, keyword=keyword, limit=limit)
    return {
        "count": len(rows),
        "items": [desktop_recipe_service.to_dict(r) for r in rows],
    }


@router.post("/recipes")
async def create_recipe(body: RecipeIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        row, is_new = await desktop_recipe_service.save_recipe(
            db,
            app_name=body.app_name,
            intent=body.intent,
            principle=body.principle,
            pitfalls=body.pitfalls,
            steps=body.steps,
            source="user",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return {"created": is_new, "item": desktop_recipe_service.to_dict(row)}


@router.put("/recipes/{recipe_id}")
async def update_recipe(recipe_id: int, body: RecipeUpdateIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        row = await desktop_recipe_service.update_recipe(
            db, recipe_id,
            app_name=body.app_name,
            intent=body.intent,
            principle=body.principle,
            pitfalls=body.pitfalls,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "操作路线不存在")
    await db.commit()
    return {"item": desktop_recipe_service.to_dict(row)}


@router.post("/recipes/delete")
async def delete_recipes(body: RecipeDeleteIn, db: AsyncSession = Depends(get_db)) -> dict:
    """批量删除。使用 POST 而非 DELETE：请求体携带 id 列表在部分客户端下更可靠。"""
    if not body.ids:
        raise HTTPException(400, "未选择要删除的条目")
    if len(body.ids) > MAX_BATCH_DELETE:
        raise HTTPException(400, f"一次最多删除 {MAX_BATCH_DELETE} 条")
    deleted = await desktop_recipe_service.delete_recipes(db, body.ids)
    await db.commit()
    return {"deleted": deleted}


@router.delete("/recipes")
async def delete_all_recipes(db: AsyncSession = Depends(get_db)) -> dict:
    total = await desktop_recipe_service.delete_all(db)
    await db.commit()
    return {"deleted": total}
