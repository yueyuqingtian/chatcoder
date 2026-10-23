"""plan-537-2262：拓展（技能 / 插件 / 连接器）注入链路的回归测试。

覆盖：
- 技能目录渲染：不含路径 / 目录行、按字符预算降级、顺序可复现（S4 / S5 / S11）；
- 引导语的两条硬约束（S6）；
- 插件摘要只列有效贡献项、全停用时保留插件行（S7）；
- stdio 连接器工具清单未就绪时不注册通用 call 包装器，远端仍保留兜底（S8）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.orchestration.context_manager import (
    _SKILLS_CHARS_BUDGET,
    _SKILLS_GUIDANCE,
    _build_skills_block,
    _render_skills,
)


def _skill(name: str, desc: str = "", trigger: str = "", path: str | None = None):
    return SimpleNamespace(name=name, description=desc, trigger=trigger, path=path)


# ── S4：目录只留路由所需字段 ─────────────────────────────


def test_render_skills_drops_path_and_dir():
    """此前每行带 [路径: …] [目录: …]，与 skill_view 返回的信息重复且明显膨胀。"""
    s = _skill("pdf", "处理 PDF 文件", "遇到 pdf 时", path="/tmp/skills/pdf/SKILL.md")
    lines, _chars = _render_skills([s], 200)

    assert lines == ["- pdf: 处理 PDF 文件 [触发条件: 遇到 pdf 时]"]
    assert "路径" not in lines[0] and "目录" not in lines[0]


def test_render_skills_keeps_no_desc_hint():
    """名与描述双空时保留读取指引，不能退化成纯名称噪声。"""
    lines, _ = _render_skills([_skill("bare", "", "", path="/tmp/x/SKILL.md")], 200)
    assert lines == ["- bare [无描述，可用 skill_view 查看正文]"]


# ── S6：引导语两条硬约束 ─────────────────────────────────


def test_guidance_has_anti_hallucination_and_no_reload():
    """对齐 ZCode / deepseek-harness：摘要不等于做法；已内联注入的不重复加载。"""
    assert "不要按技能名的字面推测它的做法" in _SKILLS_GUIDANCE
    assert "不要再调用 skill_view" in _SKILLS_GUIDANCE
    # 目录不再带路径后，读正文与附带资源的入口要说清
    assert "用 skill_view 查看" in _SKILLS_GUIDANCE


# ── S11：字符预算降级 ───────────────────────────────────


def test_build_skills_block_degrades_on_budget():
    """超预算时先截短描述，仍超再减少条数，且只追加一次「非全量」提示。"""
    # 描述取 200 字（不会被首轮 desc_limit=200 截断），×50 条必然超 8000 预算
    skills = [_skill(f"s{i:02d}", "很长的描述" * 40) for i in range(60)]
    block = _build_skills_block(skills)

    assert len(block) <= _SKILLS_CHARS_BUDGET
    assert block.count("本清单非全量") == 1
    # 降级后描述被截到 120
    for line in block.splitlines():
        if line.startswith("- s"):
            body = line.split(": ", 1)[1]
            assert len(body) <= 120


def test_build_skills_block_unchanged_when_under_budget():
    """未超预算时输出与降级前逐字一致，避免误触发降级改变提示词前缀。"""
    skills = [_skill("a", "短描述"), _skill("b", "另一个短描述")]
    block = _build_skills_block(skills)

    assert block == "- a: 短描述\n- b: 另一个短描述"
    assert "本清单非全量" not in block


def test_build_skills_block_caps_count_and_notes_remainder():
    """超过条数上限时截断并提示还剩多少，模型据此知道可无参浏览全部。"""
    skills = [_skill(f"s{i:02d}", "描述") for i in range(55)]
    block = _build_skills_block(skills)

    assert block.count("\n") + 1 == 51  # 50 条 + 1 行提示
    assert "另有 5 个未列出" in block


# ── S5：顺序可复现 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_global_skills_and_mcp_are_ordered():
    """清单顺序必须稳定，否则每轮提示词前缀抖动、KV cache 命中率下降。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.persistence.database import Base
    from app.persistence.models.skill import McpServer, Skill
    from app.services import skill_service

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        # 故意乱序插入：顺序必须由查询决定，而不是插入顺序
        db.add_all([
            Skill(name="zeta", description="z", is_active=True, auto_load=True, source="plugin"),
            Skill(name="alpha", description="a", is_active=True, auto_load=True, source="plugin"),
            Skill(name="beta", description="b", is_active=True, auto_load=True, source="custom"),
        ])
        db.add_all([
            McpServer(name="srv-z", transport="stdio", is_active=True, source="custom"),
            McpServer(name="srv-a", transport="stdio", is_active=True, source="custom"),
        ])
        await db.commit()

        skills = await skill_service.get_global_skills(db)
        servers = await skill_service.get_global_mcp_servers(db)

        assert [s.name for s in skills] == ["beta", "alpha", "zeta"]  # custom 在前，组内按名称
        assert [s.name for s in servers] == ["srv-a", "srv-z"]
    await engine.dispose()


# ── S7：插件摘要只列有效项 ───────────────────────────────


@pytest.mark.asyncio
async def test_plugin_summary_filters_inactive_contributions(monkeypatch):
    """停用的贡献技能 / 连接器不能出现在「已启用插件」摘要里。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.orchestration import context_manager
    from app.persistence.database import Base
    from app.persistence.models.skill import McpServer, Skill
    from app.services import plugin_service

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(
        plugin_service, "list_installed",
        lambda: [{"name": "demo", "displayName": "Demo", "enabled": True, "descriptionZh": ""}],
    )

    async with factory() as db:
        db.add_all([
            Skill(name="demo:on", description="有效技能", is_active=True, auto_load=True),
            Skill(name="demo:off", description="停用技能", is_active=False, auto_load=True),
            McpServer(name="demo:conn-on", transport="stdio", is_active=True, source="plugin"),
            McpServer(name="demo:conn-off", transport="stdio", is_active=False, source="plugin"),
        ])
        await db.commit()

        text = await context_manager._load_plugins(db)

        # 摘要按 `<插件>:` 前缀聚合后只留短名（原注释：名称都带插件名前缀）
        assert "贡献技能: on" in text and "conn-on" in text
        assert "off" not in text
        assert "其贡献项均已停用" not in text
    await engine.dispose()


@pytest.mark.asyncio
async def test_plugin_summary_keeps_row_when_all_contributions_disabled(monkeypatch):
    """插件启用但贡献项全停用：保留插件行并说明，避免用户以为插件坏了。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.orchestration import context_manager
    from app.persistence.database import Base
    from app.persistence.models.skill import Skill
    from app.services import plugin_service

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(
        plugin_service, "list_installed",
        lambda: [{"name": "demo", "displayName": "Demo", "enabled": True}],
    )

    async with factory() as db:
        db.add(Skill(name="demo:off", description="停用技能", is_active=False, auto_load=True))
        await db.commit()

        text = await context_manager._load_plugins(db)

        assert "Demo" in text and "其贡献项均已停用" in text
    await engine.dispose()


# ── S8：stdio 空清单不注册通用工具 ──────────────────────


def _mcp(name: str, transport: str, tools):
    return SimpleNamespace(
        name=name, display_name=name, transport=transport, command="x", args=[],
        env={}, url=None, path=None, tools=tools,
    )


def test_stdio_without_tool_list_registers_nothing():
    """工具清单未就绪的 stdio 连接器不再给「猜 method」的通用包装器。"""
    from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent

    tools = build_mcp_tools_for_agent([_mcp("local", "stdio", [])])
    assert tools == []


def test_remote_without_tool_list_keeps_generic_fallback():
    """远端连接器的工具清单本就需运行时获取，保留 call 兜底。"""
    from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent

    tools = build_mcp_tools_for_agent([_mcp("remote", "sse", [])])
    assert [t.name for t in tools] == ["mcp_remote_call"]


def test_stdio_with_tool_list_registers_each_tool():
    """已就绪的连接器仍逐个注册为原生工具。"""
    from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent

    tools = build_mcp_tools_for_agent([_mcp("local", "stdio", [
        {"name": "do_x", "description": "做 X", "inputSchema": {"type": "object"}},
    ])])
    assert [t.name for t in tools] == ["mcp_local_do_x"]
