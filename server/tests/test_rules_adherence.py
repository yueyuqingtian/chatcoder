"""规则文档遵循度（plan-19-82 步骤4）测试。

覆盖：
1. 系统提示词含「Rule Documents — MANDATORY」与冲突优先级声明
2. 子代理提示词同样受规则文档约束
3. 规则来源标注（AGENTS.md / 全局规则 / 工作目录规则）
4. 主/子代理上下文中 Global/Project Rules 位于 developer 段前部且带 MANDATORY
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration.prompts import build_main_system_prompt, build_subagent_system_prompt  # noqa: E402


def test_main_prompt_rule_priority():
    p = build_main_system_prompt(language="zh")
    assert "Rule Documents — MANDATORY" in p
    # 冲突优先级：用户全局规则 > 项目规则文档 > 内置方法论
    assert "user Global Rules > project rule documents" in p
    assert "this built-in methodology" in p
    # 覆盖各工具约定名（可核查）
    for name in ("AGENTS.md", "CLAUDE.md", ".cursorrules", "CODEBUDDY.md", "QODER.md"):
        assert name in p
    # 强制语义要点
    assert "Read before acting" in p
    assert "Do not silently deviate" in p
    assert "Self-check before delivery" in p


def test_subagent_prompt_rule_adherence():
    p = build_subagent_system_prompt("t", "ac", language="zh")
    assert "Rule documents are MANDATORY" in p
    assert "Project Rules (MANDATORY)" in p
    # 上下文回看按需约束也在子代理提示词中
    assert "compaction_index" in p


def test_rules_loader_source_label(tmp_path, monkeypatch):
    """规则文档注入应带来源标注（文件相对路径 + 工具来源）。"""
    import asyncio

    from app.orchestration import rules_loader

    (tmp_path / "AGENTS.md").write_text("# 项目规则\n- 必须用 4 空格缩进", encoding="utf-8")
    text = asyncio.run(rules_loader.load_session_rules(str(tmp_path), ["AGENTS.md"]))
    assert "AGENTS.md" in text
    # 来源标注：codex（AGENTS.md 的约定来源）
    assert "codex" in text
    assert "必须用 4 空格缩进" in text


def test_user_rules_loader_labeled(tmp_path, monkeypatch):
    """设置中心规则注入应带来源标注。"""
    import json

    from app.orchestration import user_rules_loader

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "global_rules": "全局：统一用中文注释",
        "rules_workdir_" + str(tmp_path): {"rules": "工作目录：禁止 print 调试"},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CHATCODER_USER_CONFIG", str(cfg))

    g = user_rules_loader.load_global_rules_labeled()
    assert "用户全局规则" in g and "统一用中文注释" in g

    w = user_rules_loader.load_workdir_rules_labeled(str(tmp_path))
    assert "工作目录规则" in w and "禁止 print 调试" in w


# ── plan-19-82 增强：规则独立通道（对齐 ZCode 的 meta_user 通道隔离）────────

def test_rules_go_into_dedicated_channel():
    """规则段必须落在独立通道，不与工具说明/结构摘要平铺在同一条 developer 消息里。"""
    from app.models.schemas import ChatMessage
    from app.orchestration.context_manager import ContextBundle

    bundle = ContextBundle(system="SYS")
    bundle.rules_parts.append("## Global Rules (MANDATORY — highest priority)\n全局规则内容")
    bundle.developer_parts.append("## Tool Usage Rules\n工具说明")
    bundle.developer_parts.append("## Project Structure\n目录结构")

    msgs = bundle.to_messages()
    assert isinstance(msgs[0], ChatMessage) and msgs[0].role == "system"
    # 规则独立成第二条消息，且紧随 system（注意力锚点）
    assert msgs[1].role == "developer"
    assert "Global Rules" in msgs[1].content
    assert "工具说明" not in msgs[1].content
    assert "目录结构" not in msgs[1].content
    # 其余上下文在第三条 developer 消息中
    assert msgs[2].role == "developer"
    assert "工具说明" in msgs[2].content
    assert "Global Rules" not in msgs[2].content


def test_bundle_without_rules_keeps_single_developer():
    """无规则时不应多插空消息（保持既有消息序列形态）。"""
    from app.orchestration.context_manager import ContextBundle

    bundle = ContextBundle(system="SYS")
    bundle.developer_parts.append("## Tool Usage Rules\n工具说明")
    msgs = bundle.to_messages()
    assert [m.role for m in msgs] == ["system", "developer"]
    assert "工具说明" in msgs[1].content


def test_rule_documents_priority_declared():
    """规则通道须携带冲突优先级裁决（对齐 ZCode OVERRIDE 语义）。"""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.orchestration.context_manager import build_main_context

    session = MagicMock()
    session.id = 1
    session.worktree_path = "D:/nonexistent-ws"
    session.model_id = None
    session.permission_mode = "default"
    session.shared_context = {}
    project = MagicMock()
    project.id = 1
    project.path = "D:/nonexistent-ws"
    project.rules_docs = None

    with patch("app.orchestration.context_manager._resolve_ta3_model_meta",
               new=AsyncMock(return_value=None)), \
         patch("app.orchestration.context_manager._symbol_index_hint",
               new=AsyncMock(return_value="")), \
         patch("app.orchestration.context_manager.project_structure_brief",
               new=AsyncMock(return_value="")), \
         patch("app.orchestration.context_manager._session_memory_summary",
               new=AsyncMock(return_value="")), \
         patch("app.orchestration.context_manager._load_memories",
               new=AsyncMock(return_value="")), \
         patch("app.orchestration.context_manager._load_skills_and_mcp",
               new=AsyncMock(return_value=("", ""))):
        bundle = asyncio.run(build_main_context(
            MagicMock(), agent=MagicMock(name="main"), session=session,
            project=project, turn=None, user_message="帮我改代码",
        ))

    rules_text = "\n\n".join(bundle.rules_parts)
    assert "Rule Documents — MANDATORY" in rules_text
    assert "OVERRIDE" in rules_text
    assert "user Global Rules > project rule documents" in rules_text
    # 语言锚点仍紧随 Current Goal 之前/之后的 developer 段中
    assert bundle.reply_language == "zh"
