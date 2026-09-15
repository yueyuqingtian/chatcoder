"""plan-248-1258 M3.2: 代码符号索引管理 API（索引库页面数据源）。

端点：
- GET  /symbol-index/workspaces           列出全部工作区的索引状态
- POST /symbol-index/enable   {workspace} 开启索引（后台建库，进度经 WS 广播）
- POST /symbol-index/disable  {workspace} 关闭索引
- POST /symbol-index/rebuild  {workspace} 全量重建
- GET  /symbol-index/search?workspace&query&kind&limit  符号检索（供 UI 预览）
- GET  /symbol-index/outline?workspace&path             文件符号骨架
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import symbol_index_manager as sim

router = APIRouter(prefix="/symbol-index", tags=["symbol-index"])


class WorkspaceBody(BaseModel):
    workspace: str


@router.get("/workspaces", response_model=dict)
async def list_workspaces():
    """列出所有已知工作区及其索引状态（enabled/status/files/symbols/last_updated）。"""
    return {"workspaces": await sim.list_workspaces()}


@router.post("/enable", response_model=dict)
async def enable_index(body: WorkspaceBody):
    """开启指定工作区的符号索引（后台执行，进度经 WS symbol_index.progress 广播）。"""
    if not body.workspace.strip():
        raise HTTPException(400, "workspace 不能为空")
    return await sim.enable(body.workspace.strip())


@router.post("/disable", response_model=dict)
async def disable_index(body: WorkspaceBody):
    """关闭指定工作区的符号索引（保留已建数据）。"""
    if not body.workspace.strip():
        raise HTTPException(400, "workspace 不能为空")
    return await sim.disable(body.workspace.strip())


@router.post("/rebuild", response_model=dict)
async def rebuild_index(body: WorkspaceBody):
    """全量重建（忽略增量缓存）。"""
    if not body.workspace.strip():
        raise HTTPException(400, "workspace 不能为空")
    return await sim.rebuild(body.workspace.strip())


@router.get("/search", response_model=dict)
async def search(workspace: str, query: str, kind: str | None = None,
                 file_glob: str | None = None, limit: int = 30):
    """符号检索（供索引库页面预览检索能力）。"""
    from app.services import symbol_index_service as sis

    if not workspace.strip() or not query.strip():
        raise HTTPException(400, "workspace 与 query 不能为空")
    hits = await asyncio.to_thread(
        sis.search_symbols, workspace.strip(), query.strip(),
        kind=kind, file_glob=file_glob, limit=max(1, min(limit, 100)),
    )
    return {"hits": hits}


@router.get("/outline", response_model=dict)
async def outline(workspace: str, path: str):
    """单文件符号骨架。"""
    from app.services import symbol_index_service as sis

    if not workspace.strip() or not path.strip():
        raise HTTPException(400, "workspace 与 path 不能为空")
    syms = await asyncio.to_thread(sis.outline_file, workspace.strip(), path.strip())
    return {"symbols": syms}
