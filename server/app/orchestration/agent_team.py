"""智能体团队的档案解析与派发策略（plan-20261123-agent-team）。"""
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select

from app.core.config import settings
from app.persistence.models.model_reg import Model, Provider
from app.persistence.models.subagent_profile import SubagentProfile

BUILTINS = {
    "explore": ("探索智能体", "阅读、搜索并分析工作区代码", "readonly"),
    "general": ("执行智能体", "实现改动、执行命令并验证结果", "agent"),
}


@dataclass(frozen=True)
class Member:
    id: int | None
    key: str
    name: str
    description: str
    mode: str
    enabled: bool = True
    model_id: int | None = None
    effort: str | None = None
    prompt: str | None = None


def member_of(row: SubagentProfile) -> Member:
    builtin = BUILTINS.get(row.name)
    return Member(
        row.id, row.name, row.display_name or (builtin[0] if builtin else row.name),
        row.description or "", builtin[2] if builtin else (row.execution_mode or "agent"),
        bool(row.is_active), row.model_id, row.reasoning_effort, row.system_prompt,
    )


@dataclass(frozen=True)
class Team:
    members: tuple[Member, ...]
    models: dict[int, tuple[bool, tuple[str, ...]]]
    modes: frozenset[str]

    def builtin(self, mode: str) -> Member:
        return next(member for member in self.members
                    if member.key == ("explore" if mode == "readonly" else "general"))

    def available(self) -> tuple[Member, ...]:
        return tuple(member for member in self.members
                     if member.enabled and member.mode in self.modes)

    def select(self, profile_id: int | None, mode: str | None) -> Member | None:
        if profile_id is None:
            member = self.builtin(mode or ("readonly" if self.modes == {"readonly"} else "agent"))
        else:
            member = next((item for item in self.members if item.id == profile_id), None)
        if member and member in self.available() and (mode is None or member.mode == mode):
            return member
        return None

    def workflow_member(self, role: str | None, mode: str) -> tuple[Member, bool]:
        if role in (None, "", "explorer", "implementer", "reviewer"):
            return self.builtin(mode), False
        member = next((item for item in self.members if item.key == role), None)
        if member and member.mode == mode and (member.enabled or member.key in BUILTINS):
            return member, False
        return self.builtin(mode), True

    def notice(self) -> str:
        modes = {member.mode for member in self.available()}
        if not modes:
            return "本轮由主代理完成调查和执行。"
        parts = [
            ("只读智能体仅可读、搜和分析工作区代码，无法使用浏览器、桌面操控或拓展工具（含 MCP）。"
             "严禁把查看网页、桌面、界面验证或调用拓展的任务交给只读智能体。"
             if "readonly" in modes else "主代理自己调查代码。"),
            ("独立实现任务可派给执行智能体。浏览器、桌面和拓展任务由主代理处理，"
             "或交给已获对应工具权限的执行智能体。写入范围或验证环境冲突时串行推进。"
             if "agent" in modes else "实现、命令、浏览器、桌面和拓展任务由主代理在会话权限内处理。"),
            "派发后继续独立工作，依赖结果时调用 collect_results(wait=true)，避免轮询或重复执行。",
        ]
        return " ".join(parts)

    def model_for(self, member: Member, parent_id: int | None, effort: str | None):
        available = member.model_id is None or self.models.get(member.model_id, (False, ()))[0]
        model_id = (member.model_id if available else None) or parent_id
        _, levels = self.models.get(model_id, (True, ()))
        candidates = (member.effort, effort)
        return model_id, next((choice for choice in candidates if choice in levels), None), not available


async def load_team(db, mode: str = "agent", *, parent_models: Iterable[int | None] = (),
                    allow_spawn: bool = True, all_models: bool = False) -> Team:
    from app.orchestration.subagent_capabilities import effective_mode

    rows = (await db.execute(select(SubagentProfile).order_by(SubagentProfile.id))).scalars().all()
    members = [member_of(row) for row in rows]
    keys = {member.key for member in members}
    members.extend(Member(None, key, name, description, execution)
                   for key, (name, description, execution) in BUILTINS.items() if key not in keys)
    model_ids = {model_id for model_id in (*parent_models, *(m.model_id for m in members)) if model_id}
    models = {}
    if model_ids or all_models:
        query = select(Model.id, Model.is_active, Model.reasoning_efforts,
                       Provider.is_active.label("provider_active")).outerjoin(
            Provider, Provider.id == Model.provider_id,
        )
        if not all_models:
            query = query.where(Model.id.in_(model_ids))
        metadata = (await db.execute(query)).all()
        models = {
            row.id: (bool(row.is_active) and row.provider_active is not False, tuple(row.reasoning_efforts or ()))
            for row in metadata
        }
    kind = effective_mode(mode)
    modes = set()
    if allow_spawn and (settings.subagent_enabled or kind == "workflow"):
        if settings.subagent_explore_enabled or kind == "workflow":
            modes.add("readonly")
        if kind not in ("readonly", "plan") and (settings.subagent_execute_enabled or kind == "workflow"):
            modes.add("agent")
    return Team(tuple(members), models, frozenset(modes))
