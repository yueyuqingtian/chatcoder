"""团队与 Agent 配置 API（v3：已废弃）。

v3 重构改为「项目内任务驱动」，不再需要设置团队（见需求 1）。
本路由不再被 main.py 注册，保留文件仅为历史参考。
团队相关 schema（TeamCreate/TeamAgentOut 等）已从 gateway.schemas 移除。
"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.api_route("/teams", methods=["GET", "POST", "PUT", "DELETE"])
async def teams_deprecated():
    """团队功能已废弃，所有 /teams 请求返回 410 Gone。"""
    raise HTTPException(status_code=410, detail="团队功能已在 v3 移除，改为项目内任务驱动")
