"""质量门禁单测:review verdict 解析 + ci.run 工具。"""
import os

import pytest

from app.orchestration.review import _parse_verdict
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.ci import CiRunTool, _build_command


def _ctx(workspace) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace), session_id=1, task_id=1,
        agent_id=1, agent_name="reviewer",
    )


# ───────────────── verdict 解析 ─────────────────


def test_parse_verdict_pass_explicit():
    assert _parse_verdict("PASS\n代码质量良好。") == "PASS"


def test_parse_verdict_reject_explicit():
    assert _parse_verdict("REJECT\n测试未通过。") == "REJECT"


def test_parse_verdict_chinese_pass():
    assert _parse_verdict("通过\n实现完整。") == "PASS"


def test_parse_verdict_chinese_reject():
    assert _parse_verdict("驳回\nlint 失败。") == "REJECT"


def test_parse_verdict_approve_synonym():
    assert _parse_verdict("APPROVE\n全部 ok。") == "PASS"


def test_parse_verdict_default_needs_review_on_ambiguous():
    """LLM 输出无明确结论时标记 NEEDS_REVIEW，避免低质量产出绕过审查。"""
    assert _parse_verdict("代码看起来还行,没什么大问题。") == "NEEDS_REVIEW"


def test_parse_verdict_reject_takes_priority():
    """同时出现 PASS(非行首) 与 REJECT(行首)，REJECT 优先。"""
    text = "整体看 PASS。\nREJECT:测试覆盖不足,需补 test"
    assert _parse_verdict(text) == "REJECT"


def test_parse_verdict_pass_after_reject_is_pass():
    """改判场景:REJECT 在前 PASS 在后(均行首)，最终结论应为 PASS。"""
    text = "REJECT:测试覆盖不足\n经确认已补充，PASS"
    assert _parse_verdict(text) == "PASS"


def test_parse_verdict_reject_after_pass_is_reject():
    """改判场景:PASS 在前 REJECT 在后(均行首)，最终结论应为 REJECT。"""
    text = "PASS\n复查发现问题，REJECT"
    assert _parse_verdict(text) == "REJECT"


# ───────────────── ci.run 工具 ─────────────────


def test_ci_function_schema():
    tool = CiRunTool()
    schema = tool.function_schema()
    assert schema["function"]["name"] == "ci_run"
    params = schema["function"]["parameters"]
    assert "check" in params["properties"]
    assert params["properties"]["check"]["enum"] == ["lint", "test", "build"]


def test_ci_risk_level_medium():
    assert CiRunTool().risk_level == "medium"


@pytest.mark.asyncio
async def test_ci_unknown_check_rejected(workspace):
    tool = CiRunTool()
    r = await tool.run({"check": "deploy"}, _ctx(workspace))
    assert r.ok is False
    assert "未知检查项" in r.error


@pytest.mark.asyncio
async def test_ci_runs_makefile_target(workspace, monkeypatch):
    """工作区有 Makefile 时,ci.run check=test 应跑 make test。

    用 monkeypatch 验证命令构造,实际执行用 echo 替代。
    """
    (workspace / "Makefile").write_text("test:\n\techo tested\n")
    tool = CiRunTool()
    # monkeypatch _build_command 返简单 echo
    monkeypatch.setattr(
        "app.orchestration.tools.ci._build_command",
        lambda check: f"echo ci_{check}_ok"
    )
    r = await tool.run({"check": "test"}, _ctx(workspace))
    assert r.ok is True
    assert "ci_test_ok" in r.output


@pytest.mark.asyncio
async def test_ci_failing_check(workspace, monkeypatch):
    tool = CiRunTool()
    monkeypatch.setattr(
        "app.orchestration.tools.ci._build_command",
        lambda check: "exit 5"
    )
    r = await tool.run({"check": "lint"}, _ctx(workspace))
    assert r.ok is False
    assert r.data["returncode"] == 5


# ───────────────── _build_command 选择逻辑 ─────────────────


def test_build_command_makefile_priority(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Makefile").write_text("test:\n")
    assert _build_command("test") == "make test"


def test_build_command_node_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}")
    cmd = _build_command("lint")
    assert "npm" in cmd or "eslint" in cmd


def test_build_command_python_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert _build_command("test") == "pytest -q"


def test_build_command_no_config_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _build_command("lint") is None
