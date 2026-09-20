"""插件路由（plan-282-1441 #6 拓展中心）。

端点：
  GET    /plugins/marketplace      市场视图（内置目录 + 已安装标注）
  GET    /plugins/installed        已安装列表
  POST   /plugins/install-dir      从本地目录安装
  POST   /plugins/install-git      从 Git 仓库安装
  PATCH  /plugins/{name}/enabled   启用 / 停用
  DELETE /plugins/{name}           卸载
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import plugin_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plugins", tags=["plugins"])


class InstallDirBody(BaseModel):
    path: str


class InstallGitBody(BaseModel):
    repo_url: str
    market: str = "local"


class EnabledBody(BaseModel):
    enabled: bool


@router.get("/marketplace", response_model=dict)
async def plugin_marketplace():
    """插件市场视图：内置精选目录 + 已安装/已启用标注。"""
    return plugin_service.list_marketplace()


@router.get("/installed", response_model=list[dict])
async def plugin_installed():
    return plugin_service.list_installed()


@router.post("/install-dir", response_model=dict)
async def plugin_install_dir(body: InstallDirBody, db: AsyncSession = Depends(get_db)):
    """从本地目录安装（要求目录含合法 plugin.json）。"""
    try:
        return await plugin_service.install_from_dir(db, body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/install-git", response_model=dict)
async def plugin_install_git(body: InstallGitBody, db: AsyncSession = Depends(get_db)):
    """从 Git 仓库安装（浅克隆后按 plugin.json 安装）。"""
    try:
        return await plugin_service.install_from_git(db, body.repo_url, market=body.market)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.patch("/{name}/enabled", response_model=dict)
async def plugin_set_enabled(name: str, body: EnabledBody, db: AsyncSession = Depends(get_db)):
    """启用 / 停用插件（其贡献的技能随之一并启停）。"""
    try:
        return await plugin_service.set_enabled(db, name, body.enabled)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/{name}", response_model=dict)
async def plugin_uninstall(name: str, db: AsyncSession = Depends(get_db)):
    """卸载插件：移除目录、注销贡献技能。"""
    try:
        return await plugin_service.uninstall(db, name)
    except ValueError as e:
        raise HTTPException(404, str(e))
