"""v2: 幂等种子数据 — 让服务开箱即用。

覆盖:tenant 1、user 1、默认 system_default 模型(读 env)、
默认主代理 Agent、预设配置 profile(default/ci/paranoid)、
默认安全执行策略规则。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.database import async_session_factory
from app.persistence.models.agent import Agent
from app.persistence.models.config import ConfigProfile
from app.persistence.models.exec_policy import ExecPolicyRule
from app.persistence.models.model_reg import Model
from app.persistence.models.tenant import Tenant, User


async def _get_or_create_tenant(db: AsyncSession) -> Tenant:
    t = await db.get(Tenant, 1)
    if t:
        return t
    t = Tenant(id=1, name="默认租户", plan="free")
    db.add(t)
    await db.flush()
    return t


async def _get_or_create_user(db: AsyncSession) -> User:
    u = await db.get(User, 1)
    if u:
        return u
    u = User(
        id=1, tenant_id=1, email="owner@chatcoder.local",
        display_name="Owner", role="owner",
    )
    db.add(u)
    await db.flush()
    return u


async def _maybe_create_default_model(db: AsyncSession) -> Model | None:
    """仅当服务端默认模型 env 配置齐全时,落地一条 system_default 记录。"""
    if not settings.default_model_ready:
        return None
    res = await db.execute(
        select(Model).where(Model.source_type == "system_default").limit(1)
    )
    existing = res.scalars().first()
    if existing:
        return existing
    m = Model(
        tenant_id=1,
        name=settings.default_llm_model or "default",
        provider=settings.default_llm_provider or "openai_compatible",
        base_url=settings.default_llm_base_url,
        intelligence_level=2,
        source_type="system_default",
        is_active=True,
        is_multimodal=False,
        api_format=settings.default_llm_api_format or "openai",
    )
    db.add(m)
    await db.flush()
    return m


async def _get_or_create_main_agent(db: AsyncSession, model: Model | None) -> Agent:
    """默认主代理（全局唯一 kind=main）。"""
    res = await db.execute(select(Agent).where(Agent.kind == "main").limit(1))
    existing = res.scalars().first()
    if existing:
        return existing
    agent = Agent(kind="main", name="chatcoder", model_id=model.id if model else None)
    db.add(agent)
    await db.flush()
    return agent


async def _seed_profiles(db: AsyncSession) -> None:
    """预设配置 profile（幂等，按 name+scope 判断）。"""
    presets: dict[str, dict] = {
        "default": {
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
            "writable_paths": [],
            "web_search": "cached",
        },
        "ci": {
            "approval_policy": "never",
            "sandbox_mode": "workspace-write",
            "writable_paths": [],
            "web_search": "disabled",
        },
        "paranoid": {
            "approval_policy": "reject",
            "sandbox_mode": "read-only",
            "writable_paths": [],
            "web_search": "disabled",
        },
    }
    for name, data in presets.items():
        res = await db.execute(
            select(ConfigProfile).where(
                ConfigProfile.name == name, ConfigProfile.scope == "global"
            )
        )
        if res.scalars().first() is None:
            db.add(ConfigProfile(name=name, scope="global", data=data, is_active=(name == "default")))
    await db.flush()


async def _seed_exec_policy(db: AsyncSession) -> None:
    """默认安全命令策略（全局，幂等）。"""
    defaults = [
        ("git push", "ask", "推送远程需确认"),
        ("git reset --hard", "ask", "危险重置需确认"),
        ("rm -rf", "ask", "删除操作需确认"),
        ("format", "ask", "磁盘格式化需确认"),
        ("dd", "ask", "底层写入需确认"),
    ]
    for pattern, decision, justification in defaults:
        res = await db.execute(
            select(ExecPolicyRule).where(
                ExecPolicyRule.command_pattern == pattern,
                ExecPolicyRule.session_id.is_(None),
            )
        )
        if res.scalars().first() is None:
            db.add(ExecPolicyRule(
                command_pattern=pattern, decision=decision,
                justification=justification,
            ))
    await db.flush()


async def seed() -> dict:
    """执行幂等种子,返回统计信息。"""
    async with async_session_factory() as db:
        tenant = await _get_or_create_tenant(db)
        user = await _get_or_create_user(db)
        model = await _maybe_create_default_model(db)
        agent = await _get_or_create_main_agent(db, model)
        await _seed_profiles(db)
        await _seed_exec_policy(db)
        await db.commit()
        return {
            "tenant_id": tenant.id,
            "user_id": user.id,
            "model_id": model.id if model else None,
            "main_agent_id": agent.id,
        }
