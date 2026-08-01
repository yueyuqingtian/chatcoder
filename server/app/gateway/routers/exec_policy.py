"""命令执行策略路由（D4）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import ExecPolicyRuleCreate, ExecPolicyRuleOut
from app.persistence.database import get_db
from app.services import exec_policy_service

router = APIRouter(prefix="/exec-policy", tags=["exec-policy"])


@router.get("", response_model=list[ExecPolicyRuleOut])
async def list_rules(db: AsyncSession = Depends(get_db)):
    return await exec_policy_service.list_rules(db)


@router.post("", response_model=ExecPolicyRuleOut)
async def create_rule(body: ExecPolicyRuleCreate, db: AsyncSession = Depends(get_db)):
    try:
        rule = await exec_policy_service.create_rule(
            db, command_pattern=body.command_pattern, decision=body.decision,
            session_id=body.session_id, justification=body.justification,
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
