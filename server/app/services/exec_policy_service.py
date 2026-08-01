"""命令执行策略服务（D4）。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.exec_policy import ExecPolicyRule


async def list_rules(db: AsyncSession, session_id: int | None = None) -> list[ExecPolicyRule]:
    stmt = select(ExecPolicyRule).order_by(ExecPolicyRule.id)
    if session_id is not None:
        stmt = stmt.where(
            (ExecPolicyRule.session_id.is_(None)) | (ExecPolicyRule.session_id == session_id)
        )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def create_rule(db: AsyncSession, *, command_pattern: str, decision: str,
                      session_id: int | None = None, justification: str | None = None) -> ExecPolicyRule:
    if decision not in ("allow", "deny", "ask"):
        raise ValueError("decision 必须为 allow/deny/ask")
    rule = ExecPolicyRule(command_pattern=command_pattern, decision=decision,
                          session_id=session_id, justification=justification)
    db.add(rule)
    await db.flush()
    return rule


async def delete_rule(db: AsyncSession, rule_id: int) -> bool:
    rule = await db.get(ExecPolicyRule, rule_id)
    if rule is None:
        return False
    await db.delete(rule)
    await db.flush()
    return True


def match_rule(rules: list[ExecPolicyRule], command: str) -> tuple[str | None, str | None]:
    """前缀匹配命令。返回 (decision, justification)；未命中返回 (None, None)。

    规则按 id 升序，先匹配先生效。
    """
    cmd_tokens = command.strip().split()
    if not cmd_tokens:
        return None, None
    for rule in rules:
        pat_tokens = rule.command_pattern.strip().split()
        if len(pat_tokens) > len(cmd_tokens):
            continue
        if all(a == b for a, b in zip(pat_tokens, cmd_tokens)):
            return rule.decision, rule.justification
    return None, None
