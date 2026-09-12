"""权限模式（Permission Profile）路由（plan-230-1144 M2）。

模式 = 工具白名单 + 提示词。内置 4 模式（default/readonly/plan/accept_edits）
只读不可删，但白名单与提示词可覆盖；用户可新建自定义模式。
存储于 ~/.chatcoder/config.json（permission_profiles 键），不需要数据库迁移。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import permission_profile_service as svc

router = APIRouter(prefix="/permission-profiles", tags=["permission-profiles"])


class ProfileUpsert(BaseModel):
    name: str
    display_name: str | None = None
    kind: str | None = None  # full / readonly / plan
    description: str | None = None
    tools: list[str] | None = None
    hint: str | None = None


@router.get("", response_model=list[dict])
async def list_profiles():
    return svc.list_profiles()


@router.post("", response_model=dict)
async def upsert_profile(body: ProfileUpsert):
    try:
        return svc.upsert_profile(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{name}", response_model=dict)
async def delete_profile(name: str):
    ok = svc.delete_profile(name)
    if not ok:
        raise HTTPException(404, "该模式不存在或为内置模式（内置模式仅支持覆盖，不支持删除）")
    return {"ok": True}
