"""present_ui 工具、只读/计划模式放行、内置技能与 ui-selection 上下文注入测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.providers.ta3_tool_aliases import TO_TA3_EXTENSION
from app.orchestration import approval_policy, context_manager
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.present_ui import PresentUiTool
from app.orchestration.tools.registry import tool_registry
from app.persistence.database import Base
from app.persistence.models.skill import Skill
from app.persistence.seed import _seed_builtin_skills
from app.services import permission_profile_service


def _ctx(workspace: str = "", mode: str = "agent") -> ToolContext:
    return ToolContext(
        workspace_root=workspace,
        session_id=1,
        task_id=1,
        agent_id=1,
        agent_name="主代理",
        permission_mode=mode,
        approval_mode="ask",
    )


def test_present_ui_registered_and_ta3_exposed():
    assert tool_registry.get("present_ui") is not None
    assert "present_ui" in TO_TA3_EXTENSION
    for mode in ("readonly", "plan"):
        tools = permission_profile_service.resolve_tools(mode, {t.name for t in tool_registry.all()})
        assert tools is not None
        assert "present_ui" in tools

    verdict = approval_policy.decide(
        "present_ui",
        {"title": "测试", "html": "<div>ok</div>"},
        _ctx(mode="readonly"),
    )
    assert verdict.verdict == approval_policy.VERDICT_ALLOW

    verdict_plan = approval_policy.decide(
        "present_ui",
        {"title": "测试", "html": "<div>ok</div>"},
        _ctx(mode="plan"),
    )
    assert verdict_plan.verdict == approval_policy.VERDICT_ALLOW


def test_present_ui_single_and_multi_variants():
    tool = PresentUiTool()

    res_single = asyncio.run(tool.run({"title": "曲线图", "kind": "chart", "html": "<svg></svg>"}, _ctx()))
    assert res_single.ok is True
    payload = res_single.data["ui_payload"]
    assert payload["kind"] == "chart"
    assert len(payload["variants"]) == 1
    assert payload["selection"]["enabled"] is False

    res_multi = asyncio.run(tool.run({
        "title": "登录页比选",
        "kind": "comparison",
        "layout": "grid",
        "variants": [
            {"id": "a", "label": "极简版", "html": "<div>A</div>", "summary": "留白较多"},
            {"id": "b", "label": "分栏版", "html": "<div>B</div>"},
        ],
    }, _ctx()))
    assert res_multi.ok is True
    mp = res_multi.data["ui_payload"]
    assert mp["layout"] == "grid"
    assert mp["selection"]["enabled"] is True
    assert mp["selection"]["mode"] == "single"
    assert [v["id"] for v in mp["variants"]] == ["a", "b"]


def test_present_ui_workspace_file_path_safety(tmp_path: Path):
    tool = PresentUiTool()
    demo = tmp_path / "proto.html"
    demo.write_text("<div>来自文件</div>", encoding="utf-8")

    ok_res = asyncio.run(tool.run({"title": "文件原型", "file_path": "proto.html"}, _ctx(str(tmp_path))))
    assert ok_res.ok is True
    assert ok_res.data["ui_payload"]["variants"][0]["html"] == "<div>来自文件</div>"

    outside = tmp_path.parent / "secret.html"
    outside.write_text("secret", encoding="utf-8")
    bad_res = asyncio.run(tool.run({"title": "越界", "file_path": "../secret.html"}, _ctx(str(tmp_path))))
    assert bad_res.ok is False
    assert bad_res.error_code == "PERMISSION"

    missing_res = asyncio.run(tool.run({"title": "缺失", "file_path": "none.html"}, _ctx(str(tmp_path))))
    assert missing_res.ok is False
    assert missing_res.error_code == "NOT_FOUND"


def test_seed_builtin_interactive_ui_skill_and_refs_context(monkeypatch):
    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as db:
            await _seed_builtin_skills(db)
            row = (await db.execute(select(Skill).where(Skill.name == "interactive-ui"))).scalars().first()
            assert row is not None
            assert row.is_active is True
            assert row.auto_load is False
            assert "present_ui" in (row.tools or [])
            assert "只读" in row.content

            async def _fake_load_refs(_db, _turn):
                return [
                    {
                        "kind": "ui-selection",
                        "value": "登录页 · v2",
                        "label": "B · 分栏版",
                        "detail": "版式标识：v2\n```html\n<div>B</div>\n```",
                    }
                ]

            monkeypatch.setattr(context_manager, "_load_turn_refs", _fake_load_refs)
            refs_ctx = await context_manager._load_refs_context(db, SimpleNamespace(user_message_id=1))
            assert "已选版式：B · 分栏版" in refs_ctx
            assert "<div>B</div>" in refs_ctx
        await engine.dispose()

    asyncio.run(_run())
