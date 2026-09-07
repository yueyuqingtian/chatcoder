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
                      session_id: int | None = None, justification: str | None = None,
                      tool_name: str | None = None) -> int:
    from app.persistence.database import run_write_locked

    if decision not in ("allow", "deny", "ask"):
        raise ValueError("decision 必须为 allow/deny/ask")

    def patch(s):
        rule = ExecPolicyRule(command_pattern=command_pattern, decision=decision,
                              session_id=session_id, justification=justification,
                              tool_name=tool_name)
        s.add(rule)
        s.flush()
        rid = rule.id
        s.commit()
        return rid

    return await run_write_locked(patch, label="rule.create")


async def delete_rule(db: AsyncSession, rule_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        rule = s.get(ExecPolicyRule, rule_id)
        if rule is None:
            return False
        s.delete(rule)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"rule.delete.{rule_id}")


def match_rule(rules: list[ExecPolicyRule], command: str) -> tuple[str | None, str | None]:
    """前缀匹配命令。返回 (decision, justification)；未命中返回 (None, None)。

    规则按 id 升序，先匹配先生效。
    """
    cmd_tokens = command.strip().split()
    if not cmd_tokens:
        return None, None
    for rule in rules:
        if getattr(rule, "tool_name", None):
            continue  # 工具级规则不参与命令匹配
        pat_tokens = rule.command_pattern.strip().split()
        if len(pat_tokens) > len(cmd_tokens):
            continue
        if all(a == b for a, b in zip(pat_tokens, cmd_tokens)):
            return rule.decision, rule.justification
    return None, None


def match_tool_rule(rules: list[ExecPolicyRule], tool_name: str) -> tuple[str | None, str | None]:
    """v2.2 (对齐 zcode 3.12): 工具级规则匹配（审批卡"始终允许"生成）。

    返回 (decision, justification)；未命中返回 (None, None)。
    """
    for rule in rules:
        if getattr(rule, "tool_name", None) == tool_name:
            return rule.decision, rule.justification
    return None, None
