"""子代理 / 工作流节点的权限模式跟随会话当前设置（plan-282-1530 阶段 A）。

裁决前从会话读取 approval_mode：用户在输入框切换「询问审批 / 自动审批 / 完全访问」后，
已在运行的子代理下一次工具裁决即按新模式执行，而不是沿用启动时的快照。
"""

from sqlalchemy import update

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.executor import _refresh_approval_mode
from app.persistence.database import async_session_factory
from app.persistence.models.message import Session as SessionRow
from tests.test_workflow_runtime import _session


async def _set_session_mode(sid: int, mode: str) -> None:
    async with async_session_factory() as db:
        await db.execute(update(SessionRow).where(SessionRow.id == sid).values(approval_mode=mode))
        await db.commit()


async def test_refresh_follows_the_session_mode_on_every_decision():
    sid = await _session()
    ctx = ToolContext(workspace_root="", session_id=sid, task_id=0, agent_id=0, agent_name="sub", approval_mode="ask")

    await _set_session_mode(sid, "auto")
    await _refresh_approval_mode(ctx)
    assert ctx.approval_mode == "auto"

    await _set_session_mode(sid, "full")
    await _refresh_approval_mode(ctx)
    assert ctx.approval_mode == "full"


async def test_refresh_keeps_the_context_value_when_the_session_is_missing():
    ctx = ToolContext(workspace_root="", session_id=987654321, task_id=0, agent_id=0, agent_name="sub", approval_mode="auto")
    await _refresh_approval_mode(ctx)
    assert ctx.approval_mode == "auto"
