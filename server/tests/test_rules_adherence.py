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
