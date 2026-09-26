"""市场目录路由（plan-41-198）。

两个只读端点，服务于拓展页的两个视图：

- `GET /api/market/catalog?kind=plugin|skill|connector`：左面板「市场」视图的条目来源
  （内置精选目录 + 本机真实项叠加，标注已安装 / 可直接安装 / 需前往市场）。
- `GET /api/market/installed`：设置页「扩展管理」的已安装聚合计数。

安装动作复用既有通道（插件 install-dir/install-git、技能 import-local/repos、
连接器 JSON 导入/扫描），本路由不做写操作。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import market_service

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/catalog", response_model=dict)
async def get_catalog(kind: str = "plugin", db: AsyncSession = Depends(get_db)):
    """市场目录。kind：plugin（插件）/ skill（技能）/ connector（连接器）。"""
    try:
        return await market_service.list_catalog(db, kind)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/browse", response_model=dict)
async def browse_market(
    kind: str = "plugin",
    page: int = 1,
    page_size: int = 40,
    keyword: str = "",
    category: str = "",
    sort: str = "hot",
    db: AsyncSession = Depends(get_db),
):
    """市场浏览（plan-41-225）：远端分页数据 + 已装状态 + 缓存降级。

    全量可见的实现：远端分页 + 前端无限滚动；搜索/分类透传远端参数以覆盖全库
    （而不是本地过滤已加载的几页）。远端不可达时降级到缓存或本机聚合。
    """
    try:
        return await market_service.browse(
            db, kind, page=page, page_size=page_size,
            keyword=keyword, category=category, sort=sort,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


class MarketInstallBody(BaseModel):
    """市场安装请求（plan-41-225）。

    ident 为**远端条目 id**（如 skill 的 `official03866510`、插件名、连接器名），
    前端从 `MarketItem.id`（形如 `skill:official03866510`）拆出后传入。
    """
    kind: str
    ident: str
    display_name: str = ""
    description: str = ""


@router.post("/install", response_model=dict)
async def install_market_item(body: MarketInstallBody, db: AsyncSession = Depends(get_db)):
    """把市场条目安装到本机（技能 / 插件 / 连接器三类均可，全程不跳浏览器）。

    失败时返回 400 + 可读原因（前端直接展示给用户，不暴露技术栈错误）。
    """
    try:
        return await market_service.install_item(
            db, kind=body.kind, ident=body.ident,
            display_name=body.display_name, description=body.description,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/installed", response_model=dict)
async def get_installed_summary(db: AsyncSession = Depends(get_db)):
    """已安装聚合计数（插件 / 技能 / 连接器 / 智能体）。"""
    return await market_service.installed_summary(db)
