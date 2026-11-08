"""plan-20261107-subagent-mode-tools：初始化与配置接口不再提供子代理工具白名单。"""
from sqlalchemy import select

from app.gateway.routers.subagents import SubagentProfileIn, SubagentProfilePatch, _to_out
from app.persistence.database import async_session_factory
from app.persistence.models.subagent_profile import SubagentProfile
from app.persistence.seed import _seed_subagent_profiles


async def test_seed_does_not_create_hidden_tool_restrictions():
    async with async_session_factory() as db:
        await _seed_subagent_profiles(db)
        await _seed_subagent_profiles(db)
        profiles = (await db.execute(select(SubagentProfile))).scalars().all()
        assert {p.name for p in profiles} == {"explore", "general"}
        assert all(p.tools_whitelist is None for p in profiles)


async def test_seed_preserves_existing_profile_overrides():
    async with async_session_factory() as db:
        profile = SubagentProfile(name="general", tools_whitelist=["fs_read"],
                                  description="自定义描述", system_prompt="自定义规则",
                                  reasoning_effort="high", is_active=False)
        db.add(profile)
        await db.flush()
        await _seed_subagent_profiles(db)
        assert profile.description == "自定义描述"
        assert profile.system_prompt == "自定义规则"
        assert profile.reasoning_effort == "high"
        assert profile.is_active is False


def test_profile_api_ignores_legacy_whitelist_input_and_hides_output():
    for cls, payload in [(SubagentProfileIn, {"name": "general"}), (SubagentProfilePatch, {})]:
        body = cls(**payload, tools_whitelist=["fs_read"])
        assert "tools_whitelist" not in body.model_dump(exclude_unset=True)
    profile = SubagentProfile(id=1, name="general", tools_whitelist=["fs_read"], is_active=True)
    assert "tools_whitelist" not in _to_out(profile)
