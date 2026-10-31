"""plan-601-2495：ta3 主会话 / 子代理上下文结构对齐官方客户端（Ta+3 牛码 0.7.5）。

覆盖：
1. 主会话 system 段序（persona → 任务列表 → <available_skills> → 记忆 → <project-context>
   → 工作区目录），只有一条 system；功能段以 <system-reminder> 进当轮 user 消息；
   非 ta3 路径不受影响。
2. persona 按模式取（agent / plan / chat），chat 不注入任务列表。
3. <project-context>：`\\n\\n` 拼接、用户级块并入设置中心规则、工作区为空仍输出、32K 截断。
4. 记忆注入：索引行格式、全量加载、先截断后陈旧提醒、无记忆也注入说明段。
5. 子代理 ta3 分支：单条 system、任务说明在 user 消息、inherit_context 回放历史。
6. inherit_context 快照包含 sender_type=agent 的回复，且有总量上限。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.orchestration import context_manager as cm
from app.orchestration.prompts import ta3_fusion as tf
from app.persistence.database import async_session_factory
from app.persistence.models.memory import MemoryEntry
from app.persistence.models.message import Message
from app.persistence.models.message import Session as SessionRow
from app.persistence.models.skill import Skill
from app.persistence.models.turn import Turn

_META = {"systemMessage": "PERSONA-BODY"}


@pytest.fixture
def isolated_rules(tmp_path, monkeypatch):
    """隔离用户级规则来源：假 HOME（无 ~/.claude/CLAUDE.md）+ 假设置中心配置。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cfg = tmp_path / "config.json"
    cfg.write_text('{"global_rules": "全局规则-全局使用中文"}', encoding="utf-8")
    monkeypatch.setenv("CHATCODER_USER_CONFIG", str(cfg))
    return home


# ───────────────────────── 2. persona 按模式 ─────────────────────────


def test_persona_mode_mapping():
    assert tf.ta3_persona_mode("agent") == "agent"
    assert tf.ta3_persona_mode("default") == "agent"
    assert tf.ta3_persona_mode("plan") == "plan"
    assert tf.ta3_persona_mode("readonly") == "plan"
    assert tf.ta3_persona_mode("chat") == "chat"


def test_persona_message_by_mode_with_chat_options():
    meta = {
        "systemMessage": "FALLBACK",
        "chatOptions": {
            "baseAgentSystemMessage": "AGENT",
            "basePlanSystemMessage": "PLAN",
            "baseSystemMessage": "CHAT",
        },
    }
    assert tf.ta3_persona_system_message(meta, "agent") == "AGENT"
    assert tf.ta3_persona_system_message(meta, "plan") == "PLAN"
    assert tf.ta3_persona_system_message(meta, "chat") == "CHAT"
    # 目录只存了 systemMessage 时三种模式都回退到它（现状兼容）
    assert tf.ta3_persona_system_message({"systemMessage": "ONLY"}, "plan") == "ONLY"


def test_chat_mode_has_no_task_section():
    assert "## 任务列表" not in tf.build_ta3_system_prompt(_META, execution_mode="chat")
    p = tf.build_ta3_system_prompt(_META, execution_mode="agent")
    assert p.startswith("PERSONA-BODY\n\n## 任务列表")


# ───────────────────────── 3. <project-context> ─────────────────────────


def test_project_context_join_and_user_block(isolated_rules, workspace, monkeypatch):
    (workspace / "AGENTS.md").write_text("项目约定A", encoding="utf-8")
    (isolated_rules / ".claude").mkdir()
    (isolated_rules / ".claude" / "CLAUDE.md").write_text("用户CLAUDE", encoding="utf-8")
    out = cm._build_ta3_project_context(str(workspace))
    assert out.startswith("<project-context>\n\n以下指令来自 AGENTS.md / CLAUDE.md")
    assert out.endswith("\n\n</project-context>")
    assert "--- 项目级 AGENTS.md (" in out and "项目约定A" in out
    # 用户级块 = CLAUDE.md + 设置中心全局规则，标题按官方用户级块
    user_part = out.split("--- 用户级 CLAUDE.md (", 1)[1]
    assert "用户CLAUDE" in user_part and "全局规则-全局使用中文" in user_part
    # 工作区只读 AGENTS.md：工作区 CLAUDE.md 不注入
    (workspace / "CLAUDE.md").write_text("工作区CLAUDE-不应出现", encoding="utf-8")
    assert "工作区CLAUDE-不应出现" not in cm._build_ta3_project_context(str(workspace))


def test_project_context_empty_workspace_still_user_block(isolated_rules):
    out = cm._build_ta3_project_context("")
    assert "--- 用户级 CLAUDE.md (" in out
    assert "项目级 AGENTS.md" not in out


def test_project_context_truncates_at_32k(isolated_rules, workspace):
    (workspace / "AGENTS.md").write_text("字" * (40 * 1024), encoding="utf-8")
    out = cm._build_ta3_project_context(str(workspace))
    assert "（内容过长，已截断）" in out
    assert out.count("字") == 32 * 1024
    # 额度用尽后用户级块不再输出
    assert "用户级 CLAUDE.md" not in out


# ───────────────────────── 4. 记忆注入 ─────────────────────────


def test_index_line_format_and_freshness_after_truncate():
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    e = SimpleNamespace(id=7, text="项目使用 PowerShell 7。注意编码", generated_at=old)
    line = cm._ta3_index_line(e)
    name = cm._ta3_memory_slug(e)
    assert line == f"- [{name}]({name}.md) — 项目使用 PowerShell 7。"
    assert cm._ta3_memory_age_days(e) == 5
    many = [SimpleNamespace(id=i, text=f"条目{i}", generated_at=old) for i in range(250)]
    index = cm._ta3_truncate_index("\n".join(cm._ta3_index_line(x) for x in many)).strip()
    merged = cm._ta3_append_freshness(index, many)
    head, warn = merged.split("> 陈旧记忆提醒：", 1)
    # 截断 WARNING 在陈旧提醒之前（先截断再追加）
    assert "> WARNING: 索引文件已超出上限被截断" in head
    assert warn.count("天前更新") == 250


async def test_memory_injection_no_entries_still_has_instructions(workspace):
    async with async_session_factory() as db:
        out = await cm._build_ta3_memory_injection(db, 1, None, workspace=str(workspace))
    assert out.startswith("<memory_instructions>")
    assert "<system-reminder>" not in out
    # 目录插值不是不存在的 settings.data_dir，而是真实数据根下的官方形态
    assert "memories`" in out and "memory`" in out and "projects" in out


async def test_memory_injection_loads_all_entries(workspace):
    async with async_session_factory() as db:
        db.add(SessionRow(id=1))
        await db.flush()
        for i in range(20):
            db.add(MemoryEntry(text=f"全局偏好{i}", scope="global", session_id=1))
        for i in range(15):
            db.add(MemoryEntry(text=f"项目约定{i}", scope="project", project_id=3, session_id=1))
        db.add(MemoryEntry(text="候选不注入", scope="global", candidate=True, session_id=1))
        await db.commit()
        out = await cm._build_ta3_memory_injection(db, 1, 3, workspace=str(workspace))
    g = out.split("## 全局记忆索引\n", 1)[1].split("## 项目记忆索引", 1)[0]
    p = out.split("## 项目记忆索引\n", 1)[1]
    assert g.count("\n- [") + 1 == 20  # 不再受 5/4/3 分层限制
    assert p.count("- [") == 15
    assert "候选不注入" not in out
    assert "<system-reminder>\n# 项目记忆索引\n## 全局记忆索引" in out


# ───────────────────────── 1. 主会话段序 ─────────────────────────


def _main_patches(meta):
    return [
        patch.object(cm, "_resolve_ta3_model_meta", new=AsyncMock(return_value=meta)),
        patch.object(cm, "_symbol_index_hint", new=AsyncMock(return_value="")),
        patch.object(cm, "project_structure_brief", new=AsyncMock(return_value="")),
    ]


async def _build_main(meta, workspace, **kw):
    async with async_session_factory() as db:
        db.add(SessionRow(id=1))
        db.add(Skill(name="zeta", description="Z 技能", trigger="需要 Z 时"))
        db.add(Skill(name="alpha", description="A 技能"))
        await db.commit()
        session = SimpleNamespace(id=1, worktree_path=None, model_id=None,
                                  permission_mode=kw.pop("mode", "agent"), shared_context={})
        project = SimpleNamespace(id=None, path=str(workspace), rules_docs=None)
        ps = _main_patches(meta)
        for p in ps:
            p.start()
        try:
            return await cm.build_main_context(
                db, agent=SimpleNamespace(id=1, model_id=None, name="main"),
                session=session, project=project, turn=SimpleNamespace(id=1, user_message_id=None),
                user_message="帮我改代码", **kw,
            )
        finally:
            for p in ps:
                p.stop()


async def test_ta3_main_single_system_in_official_order(isolated_rules, workspace):
    (workspace / "AGENTS.md").write_text("项目约定A", encoding="utf-8")
    bundle = await _build_main(
        _META, workspace,
        goal={"text": "修复登录页", "turns_used": 2}, goal_tool_name="goal_complete",
        attachments=[{"filename": "a.txt", "path": "x/a.txt", "type": "text"}],
        paused_subagents=[{"agent_id": 9, "title": "旧任务", "steps": 3}],
    )
    msgs = bundle.to_messages()
    assert [m.role for m in msgs] == ["system", "user"]
    sys_text = msgs[0].content
    order = [
        sys_text.index("PERSONA-BODY"),
        sys_text.index("## 任务列表"),
        sys_text.index("<available_skills>"),
        sys_text.index("<memory_instructions>"),
        sys_text.index("<project-context>"),
        sys_text.index("当前桌面项目明确绑定了 1 个工作区目录"),
    ]
    assert order == sorted(order)
    # 官方技能格式：按 name 升序 + whenToUse + ReadSkill 引导
    assert sys_text.index("- name: alpha") < sys_text.index("- name: zeta")
    assert "  whenToUse: 需要 Z 时" in sys_text
    assert "Use the ReadSkill tool to read full instructions before applying a skill." in sys_text
    # chatcoder 自有段不在 system
    for banned in ("## Current Goal", "## Reply Language", "## 用户上传的附件",
                   "## Paused Subagents", "Working directory:", "## Available Skills",
                   "## Rule Documents"):
        assert banned not in sys_text
    # 功能段进当轮 user 消息的 <system-reminder>，且在用户原文之前
    user = msgs[1].content
    assert user.startswith("<system-reminder>\n")
    assert user.endswith("帮我改代码")
    assert "当前目标：修复登录页" in user
    # 拓展通道 serverId 由 ta3_mcp 按官方 store.js 形态派生（带稳定 base36 后缀），不写死
    from app.models.providers.ta3_mcp import EXTENSION_SERVER_ID
    _ext = f"mcp__{EXTENSION_SERVER_ID}__"
    assert f"{_ext}goal_complete" in user  # 目标结束方式使用模型可见名
    assert f"{_ext}read_attachment" in user
    assert f"{_ext}resume_subagent" in user
    assert bundle.rules_anchor.startswith("<system-reminder>")


async def test_ta3_chat_mode_omits_task_section(isolated_rules, workspace):
    bundle = await _build_main(_META, workspace, mode="chat")
    assert "## 任务列表" not in bundle.to_messages()[0].content


async def test_non_ta3_main_unchanged_shape(isolated_rules, workspace):
    bundle = await _build_main(None, workspace)
    roles = [m.role for m in bundle.to_messages()]
    assert roles[0] == "system" and "developer" in roles
    assert any(p.startswith("## Current Goal") for p in bundle.developer_parts)
    assert any(p.startswith("## Available Skills") for p in bundle.developer_parts)
    assert "<system-reminder>" not in bundle.instruction


# ───────────────────── 5/6. 子代理与 inherit_context ─────────────────────


async def _seed_history(db, *, n_turns=2, big=False):
    db.add(SessionRow(id=1))
    await db.flush()
    for t in range(1, n_turns + 1):
        db.add(Turn(id=t, session_id=1, status="completed"))
        await db.flush()
        db.add(Message(session_id=1, turn_id=t, sender_type="user", msg_type="text",
                       content={"text": f"问题{t}" + ("x" * 3000 if big else "")}))
        db.add(Message(session_id=1, turn_id=t, sender_type="agent", msg_type="text",
                       content={"text": f"结论{t}" + ("y" * 3000 if big else "")}))
    db.add(Turn(id=n_turns + 1, session_id=1, status="running"))
    await db.flush()
    db.add(Message(session_id=1, turn_id=n_turns + 1, sender_type="user", msg_type="text",
                   content={"text": "进行中的轮次"}))
    await db.commit()


async def test_inherit_snapshot_includes_agent_replies():
    async with async_session_factory() as db:
        await _seed_history(db)
        snap = await cm._load_completed_turns_snapshot(db, 1)
    assert "Assistant: 结论1" in snap and "Assistant: 结论2" in snap
    assert "进行中的轮次" not in snap


async def test_inherit_snapshot_total_cap():
    async with async_session_factory() as db:
        await _seed_history(db, n_turns=30, big=True)
        rows = await cm._load_completed_turns_rows(db, 1)
    total = sum(len(r[2]) for r in rows)
    assert total <= cm._INHERIT_MAX_CHARS
    assert rows and rows[0][1] == "User"
    assert rows[-1][2].startswith("结论30")  # 保留最近的轮次


async def test_ta3_subagent_context_shape():
    task = SimpleNamespace(title="调研X", description="详细说明", acceptance_criteria="AC1")
    async with async_session_factory() as db:
        await _seed_history(db)
        with patch.object(cm, "_resolve_ta3_model_meta", new=AsyncMock(return_value=_META)):
            bundle = await cm.build_subagent_context(
                db, agent=SimpleNamespace(id=5, model_id=11), session=SimpleNamespace(
                    id=1, worktree_path="D:/ws", shared_context={}),
                project=None, task=task, handoff_summary="请调研 X 的实现",
                original_request="原始请求", inherit_context=True,
            )
    msgs = bundle.to_messages()
    assert [m.role for m in msgs] == ["system", "user", "assistant", "user", "assistant", "user"]
    sys_text = msgs[0].content
    assert sys_text.startswith("你是 GeneralPurpose 子代理。")
    assert "你是主会话派生出的独立子代理" in sys_text
    assert "交接要求：" in sys_text and "### Files Touched" in sys_text
    for banned in ("## Reply Language", "## Rule Documents", "## Handoff Summary", "Working directory:"):
        assert banned not in sys_text
    last = msgs[-1].content
    assert last.endswith("请调研 X 的实现")
    assert "<system-reminder>" in last and "验收标准：\nAC1" in last
    assert msgs[2].content == "结论1"


async def test_non_ta3_subagent_unchanged():
    task = SimpleNamespace(title="调研X", description="详细说明", acceptance_criteria="AC1")
    async with async_session_factory() as db:
        with patch.object(cm, "_resolve_ta3_model_meta", new=AsyncMock(return_value=None)), \
             patch.object(cm, "_symbol_index_hint", new=AsyncMock(return_value="")), \
             patch.object(cm, "project_structure_brief", new=AsyncMock(return_value="")):
            bundle = await cm.build_subagent_context(
                db, agent=SimpleNamespace(id=5, model_id=None), session=SimpleNamespace(
                    id=1, worktree_path="D:/ws", shared_context={}),
                project=None, task=task, handoff_summary="请调研",
            )
    assert not bundle.merge_developer_into_system
    assert any(p.startswith("## Handoff Summary") for p in bundle.developer_parts)
