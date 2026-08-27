"""命令执行策略路由（D4）。v3.0 (plan-88): 增加工具级规则 UI 的数据源。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import ExecPolicyRuleCreate, ExecPolicyRuleOut
from app.persistence.database import get_db
from app.services import exec_policy_service

router = APIRouter(prefix="/exec-policy", tags=["exec-policy"])

# 内部/交互工具不参与策略配置（子代理编排工具也无 registry 实例）
_INTERNAL_TOOLS = {"ask_user_question", "todo_write", "spawn_subagent", "collect_results"}


class ExecPolicyToolInfo(BaseModel):
    name: str
    risk_level: str
    description: str = ""


@router.get("/tools", response_model=list[ExecPolicyToolInfo])
async def list_tools():
    """工具级规则候选清单（排除内部交互工具），供 PolicyPanel 工具下拉使用。"""
    from app.orchestration.tools.registry import tool_registry
    out: list[ExecPolicyToolInfo] = []
    for t in tool_registry.all():
        if t.name in _INTERNAL_TOOLS:
            continue
        first_line = t.description.strip().splitlines()[0][:80] if t.description else ""
        out.append(ExecPolicyToolInfo(name=t.name, risk_level=t.risk_level, description=first_line))
    return out


@router.get("", response_model=list[ExecPolicyRuleOut])
async def list_rules(db: AsyncSession = Depends(get_db)):
    return await exec_policy_service.list_rules(db)


@router.post("", response_model=ExecPolicyRuleOut)
async def create_rule(body: ExecPolicyRuleCreate, db: AsyncSession = Depends(get_db)):
    try:
        rule = await exec_policy_service.create_rule(
            db, command_pattern=body.command_pattern, decision=body.decision,
            session_id=body.session_id, justification=body.justification,
            tool_name=body.tool_name,
        )
        await db.commit()
        return rule
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{rule_id}", response_model=dict)
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    ok = await exec_policy_service.delete_rule(db, rule_id)
    if not ok:
        raise HTTPException(404, "规则不存在")
    await db.commit()
    return {"ok": True}
