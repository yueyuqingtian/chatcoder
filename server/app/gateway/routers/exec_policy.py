"""命令执行策略路由（D4）。v3.0 (plan-88): 增加工具级规则 UI 的数据源。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import ExecPolicyRuleCreate, ExecPolicyRuleOut
from app.persistence.database import get_db
from app.services import exec_policy_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exec-policy", tags=["exec-policy"])

# 交互类工具不参与策略配置（提问/待办由 UI 直接接管，不是可授权的能力）
_INTERNAL_TOOLS = {"ask_user_question", "todo_write"}


class ExecPolicyToolInfo(BaseModel):
    name: str
    risk_level: str
    description: str = ""


@router.get("/tools", response_model=list[ExecPolicyToolInfo])
async def list_tools(db: AsyncSession = Depends(get_db)):
    """工具级规则候选清单（排除内部交互工具），供 PolicyPanel 工具下拉与
    权限模式白名单勾选矩阵使用。"""
    from app.orchestration.tools.registry import tool_registry
    out: list[ExecPolicyToolInfo] = []
    seen: set[str] = set()
    for t in tool_registry.all():
        if t.name in _INTERNAL_TOOLS:
            continue
        first_line = t.description.strip().splitlines()[0][:80] if t.description else ""
        out.append(ExecPolicyToolInfo(name=t.name, risk_level=t.risk_level, description=first_line))
        seen.add(t.name)

    # v36 (plan-321-1600 R1): 子代理编排工具（agent_loop 特判执行，无 registry 实例）——
    # 此前被 _INTERNAL_TOOLS 整体排除，设置页「工具权限」里既看不到也无法勾选
    # （用户反馈“看不到子代理相关工具”）。现补齐为可配置项，白名单勾选对其生效。
    try:
        from app.orchestration.subagent_tools import (
            SUBAGENT_TOOL_NAMES, SUBAGENT_TOOL_RISK, SUBAGENT_TOOL_SCHEMAS,
        )

        _sub_desc = {
            s["function"]["name"]: str(s["function"].get("description") or "")
            for s in SUBAGENT_TOOL_SCHEMAS
        }
        for _n in sorted(SUBAGENT_TOOL_NAMES):
            if _n in seen:
                continue
            _d = _sub_desc.get(_n, "").strip().splitlines()
            out.append(ExecPolicyToolInfo(
                name=_n, risk_level=SUBAGENT_TOOL_RISK.get(_n, "medium"),
                description=_d[0][:80] if _d else "",
            ))
            seen.add(_n)
    except Exception:
        logger.debug("[exec-policy] 子代理工具候选清单构建失败(非阻塞)", exc_info=True)

    # MCP 工具（mcp_<server>_<tool>）：名字随用户配置动态变化，registry 里只有"某次
    # turn 注入过"才存在——设置页首屏因此看不到、也就无从勾选（plan-230-1144 M1.3
    # 的权限勾选对 MCP 失效的次生原因）。此处直接从 MCP Server 配置构造候选，
    # 配置了 MCP Server 就能在权限面板里勾选/配规则。
    try:
        from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent
        from app.services.skill_service import get_global_mcp_servers

        servers = await get_global_mcp_servers(db)
        for mt in build_mcp_tools_for_agent(servers or []):
            if mt.name in seen:
                continue
            first_line = mt.description.strip().splitlines()[0][:80] if mt.description else ""
            out.append(ExecPolicyToolInfo(
                name=mt.name, risk_level=mt.risk_level, description=first_line,
            ))
            seen.add(mt.name)
    except Exception:
        # 候选清单缺失不应影响设置页其余功能（与 engine 侧"注入失败不阻塞"同口径）
        logger.debug("[exec-policy] MCP 工具候选清单构建失败(非阻塞)", exc_info=True)
    return out


@router.get("", response_model=list[ExecPolicyRuleOut])
async def list_rules(db: AsyncSession = Depends(get_db)):
    return await exec_policy_service.list_rules(db)


@router.post("", response_model=ExecPolicyRuleOut)
async def create_rule(body: ExecPolicyRuleCreate, db: AsyncSession = Depends(get_db)):
    try:
        rid = await exec_policy_service.create_rule(
            db, command_pattern=body.command_pattern, decision=body.decision,
            session_id=body.session_id, justification=body.justification,
            tool_name=body.tool_name,
        )
        from sqlalchemy import select
        from app.persistence.models.exec_policy import ExecPolicyRule
        return (await db.execute(select(ExecPolicyRule).where(ExecPolicyRule.id == rid))).scalars().first()
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{rule_id}", response_model=dict)
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    ok = await exec_policy_service.delete_rule(db, rule_id)
    if not ok:
        raise HTTPException(404, "规则不存在")
    return {"ok": True}
