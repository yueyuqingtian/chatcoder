"""plan-114-450：规则来源开关——子目录规则文件归属、CONTRIBUTING 可关闭、全部停用不再回落为全部启用。"""
import asyncio
import json

from app.orchestration import rules_loader as rl


def test_source_of_subdir_and_generic():
    assert rl.source_of("AGENTS.md") == "codex"
    # 一级子目录下的规则文件归属到同一来源（此前返回 None，开关管不到）
    assert rl.source_of("client/AGENTS.md") == "codex"
    assert rl.source_of("server/.cursor/rules/a.md") == "cursor"
    assert rl.source_of("CONTRIBUTING.md") == "generic"
    assert rl.source_of("docs/readme.md") is None
    # 界面来源表覆盖注入侧全部来源
    assert {s for _, s in rl._RULE_SOURCE_MAP} <= set(rl.RULE_SOURCE_LABELS)


def test_disable_all_and_single(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("a", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    sub = tmp_path / "client"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("sub", encoding="utf-8")
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("CHATCODER_USER_CONFIG", str(cfg))

    def names(enabled):
        cfg.write_text(json.dumps({"ai_rules_enabled": enabled}), encoding="utf-8")
        got = asyncio.run(rl.collect_rule_docs(str(tmp_path)))
        return sorted(str(p.relative_to(tmp_path)).replace("\\", "/") for p in got)

    # 只关 codex：根目录与子目录的 AGENTS.md 都不再加载
    assert names(["claude"]) == ["CLAUDE.md"]
    # 全部停用：一个都不加载（此前空数组被当成未配置 → 全部加载）
    assert names([]) == []
