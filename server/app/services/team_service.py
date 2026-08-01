"""团队与 Agent CRUD（v3：已废弃）。

v3 重构改为「项目内任务驱动」，不再需要设置团队（见需求 1）。
原 Team/TeamAgent/AgentTemplate 模型已从 persistence.models.agent 移除。
本模块保留为桩，避免 routers/teams.py 与 smart_team_service 的导入链崩溃；
所有操作均抛出 RuntimeError，调用方（teams 路由）应返回 410 Gone。
"""
from sqlalchemy.ext.asyncio import AsyncSession

_DEPRECATED_MSG = "团队功能已在 v3 移除，改为项目内任务驱动"


async def create_team(db: AsyncSession, *, name: str, description: str | None):
    raise RuntimeError(_DEPRECATED_MSG)


async def get_team(db: AsyncSession, team_id: int):
    raise RuntimeError(_DEPRECATED_MSG)


async def list_teams(db: AsyncSession):
    raise RuntimeError(_DEPRECATED_MSG)


async def create_agent_template(db: AsyncSession, **kwargs):
    raise RuntimeError(_DEPRECATED_MSG)


async def create_team_agent(db: AsyncSession, **kwargs):
    raise RuntimeError(_DEPRECATED_MSG)


async def list_team_agents(db: AsyncSession, team_id: int):
    raise RuntimeError(_DEPRECATED_MSG)


async def set_leader(db: AsyncSession, team_id: int, agent_id: int) -> None:
    raise RuntimeError(_DEPRECATED_MSG)


async def update_team_agent(db: AsyncSession, agent_id: int, **kwargs):
    raise RuntimeError(_DEPRECATED_MSG)


async def delete_team_agent(db: AsyncSession, agent_id: int) -> bool:
    raise RuntimeError(_DEPRECATED_MSG)


async def update_agent_template(db: AsyncSession, template_id: int, **kwargs):
    raise RuntimeError(_DEPRECATED_MSG)
