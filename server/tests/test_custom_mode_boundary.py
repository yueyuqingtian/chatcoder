"""自定义模式的边界归一回归用例（plan-560-2311 C1/C2）。

缺陷原貌：模式归一有两份半实现——`approval_policy.normalize_execution_mode` 只做旧名
映射、对自定义模式名原样返回；`subagent_capabilities.effective_mode` 按 kind 归一却没人
调用。而 `_mode_boundary` 拿模式名和内置字面量做**等值比较**，自定义 `kind=readonly` /
`kind=plan` 的模式名两者都不等，于是执行模式硬边界被整体跳过：选了自定义只读模式，
`fs_write` / `terminal_exec` 写操作照样放行。

plan-560-2311 C1 的修法是把归一逻辑下沉到 `permission_profile_service.resolve_mode_kind`
（单一事实源），审批层改用**判定档**做比较；只在判定处归一，不写回会话里存的模式名。

plan-560-2311 C2 是同一批的口径校正：`workflow` 的 kind 是 `full`，实现上等同 agent
（可派可写子代理），此前的注释与提示词却声明「强制只读 explore」，与实现不符。本文件
一并钉住「workflow 拿完整版派发提示与 operator 提示」这一口径。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import permission_profile_service as pps  # noqa: E402


@pytest.fixture
def profiles(tmp_path, monkeypatch) -> Path:
    """把用户配置指向临时文件，按需写入自定义模式。

    `permission_profile_service` 的模式配置在 `~/.chatcoder/config.json` 的
    `permission_profiles` 键下（可经 CHATCODER_USER_CONFIG 覆盖）。测试直接改模块常量
    `_CFG_PATH`——`_read_cfg()` 每次调用都重新读它，因此改动立即生效，也不会污染真实配置。
    """
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pps, "_CFG_PATH", cfg)
    return cfg


def _write_profiles(cfg: Path, *entries: dict) -> None:
    cfg.write_text(
        json.dumps({"permission_profiles": list(entries)}, ensure_ascii=False),
        encoding="utf-8",
    )


def _ctx(tmp_path, mode: str, approval: str = "auto"):
    from app.orchestration.tools.base import ToolContext

    ctx = ToolContext(
        workspace_root=str(tmp_path), session_id=1, task_id=1, agent_id=1, agent_name="t",
    )
    ctx.permission_mode = mode
    ctx.approval_mode = approval
    return ctx


def _decide(tool_cls, tool_name: str, args: dict, ctx):
    from app.orchestration.approval_policy import decide

    return decide(tool_name, args, ctx, risk_level=tool_cls.risk_level)


# ── C1：归一下沉入口 resolve_mode_kind ─────────────────────────────────────────

def test_builtin_modes_resolve_to_their_own_kind(profiles):
    """内置四模式直接命中，不查配置文件（绝大多数调用应是零 IO）。"""
    assert pps.resolve_mode_kind("readonly") == pps.MODE_KIND_READONLY
    assert pps.resolve_mode_kind("plan") == pps.MODE_KIND_PLAN
    assert pps.resolve_mode_kind("agent") == pps.MODE_KIND_AGENT
    assert pps.resolve_mode_kind("workflow") == pps.MODE_KIND_WORKFLOW


def test_empty_and_legacy_modes_resolve_like_agent(profiles):
    """空值按 agent（与 `normalize_execution_mode` 同口径）；旧名经 `_canonical` 归一后同样落 agent。"""
    assert pps.resolve_mode_kind(None) == pps.MODE_KIND_AGENT
    assert pps.resolve_mode_kind("") == pps.MODE_KIND_AGENT
    assert pps.resolve_mode_kind("   ") == pps.MODE_KIND_AGENT
    assert pps.resolve_mode_kind("default") == pps.MODE_KIND_AGENT
    assert pps.resolve_mode_kind("accept_edits") == pps.MODE_KIND_AGENT


def test_custom_modes_resolve_by_kind(profiles):
    """plan-560-2311 C1：自定义模式按 `kind` 归一——这正是硬边界此前被绕过的缺口。"""
    _write_profiles(
        profiles,
        {"name": "review_only", "kind": "readonly", "tools": []},
        {"name": "draft_plan", "kind": "plan", "tools": []},
        {"name": "release_ops", "kind": "full", "tools": []},
    )

    assert pps.resolve_mode_kind("review_only") == pps.MODE_KIND_READONLY
    assert pps.resolve_mode_kind("draft_plan") == pps.MODE_KIND_PLAN
    # kind=full 必须落到 agent（不是 workflow）——只有内置 workflow 名才是 workflow 档
    assert pps.resolve_mode_kind("release_ops") == pps.MODE_KIND_AGENT


def test_unknown_mode_falls_back_to_agent(profiles):
    """未识别模式名按 agent。

    plan-560-2311 C1：本模块的 `resolve_tools` / `is_readonly_like` 与 engine 的工具下发
    对未知模式名都按「全量工具」处理；判定期若按 plan 处理，就会出现「工具面全量可写、
    写操作却一律被拒」的自相矛盾——那是倒退，不是收紧。
    """
    assert pps.resolve_mode_kind("no_such_mode") == pps.MODE_KIND_AGENT


def test_resolve_mode_kind_does_not_rewrite_session_value(profiles):
    """归一只发生在判定期：`normalize_execution_mode` 仍原样保留自定义模式名。

    会话里存的自定义模式名被改写会让用户配置的模式变成另一个模式（见 test_session_modes）。
    """
    from app.orchestration.approval_policy import normalize_execution_mode

    _write_profiles(profiles, {"name": "review_only", "kind": "readonly", "tools": []})

    assert normalize_execution_mode("review_only") == "review_only"
    assert pps.resolve_mode_kind("review_only") == pps.MODE_KIND_READONLY


# ── C1：审批边界（自定义只读 / 计划模式）──────────────────────────────────────

def test_custom_readonly_mode_denies_write(tmp_path, profiles):
    """plan-560-2311 C1：自定义 kind=readonly 模式必须命中执行模式硬边界。"""
    from app.orchestration.approval_policy import VERDICT_DENY
    from app.orchestration.tools.fs_write import FsWriteTool

    _write_profiles(profiles, {"name": "review_only", "kind": "readonly", "tools": []})
    ctx = _ctx(tmp_path, "review_only")

    d = _decide(FsWriteTool, "fs_write", {"path": "src/x.py", "content": "print(1)"}, ctx)

    assert d.verdict == VERDICT_DENY, "自定义只读模式此前完全跳过硬边界，写文件被放行"
    assert "只读模式" in d.reason


def test_custom_readonly_mode_denies_write_command(tmp_path, profiles):
    """同一条边界也要挡住终端写命令——否则换个工具就绕过了。"""
    from app.orchestration.approval_policy import VERDICT_DENY
    from app.orchestration.tools.terminal import TerminalExecTool

    _write_profiles(profiles, {"name": "review_only", "kind": "readonly", "tools": []})
    ctx = _ctx(tmp_path, "review_only")

    d = _decide(
        TerminalExecTool, "terminal_exec",
        {"command": "python -c \"open('x.py','w').write('1')\""}, ctx,
    )

    assert d.verdict == VERDICT_DENY
    assert "只读模式" in d.reason


def test_custom_readonly_mode_still_allows_read(tmp_path, profiles):
    """收紧不能连带把读操作也拒掉——自定义只读模式仍然要能读文件。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW
    from app.orchestration.tools.fs_read import FsReadTool

    _write_profiles(profiles, {"name": "review_only", "kind": "readonly", "tools": []})
    ctx = _ctx(tmp_path, "review_only", approval="ask")

    d = _decide(FsReadTool, "fs_read", {"path": "src/x.py"}, ctx)

    assert d.verdict == VERDICT_ALLOW


def test_custom_plan_mode_allows_only_plan_doc(tmp_path, profiles):
    """plan-560-2311 C1：自定义 kind=plan 模式——计划文档放行，其他写操作拒绝。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW, VERDICT_DENY
    from app.orchestration.tools.fs_write import FsWriteTool

    _write_profiles(profiles, {"name": "draft_plan", "kind": "plan", "tools": []})
    ctx = _ctx(tmp_path, "draft_plan")

    ok = _decide(FsWriteTool, "fs_write",
                 {"path": "ai/plan-x.md", "content": "# 计划"}, ctx)
    assert ok.verdict == VERDICT_ALLOW

    bad = _decide(FsWriteTool, "fs_write",
                  {"path": "src/x.py", "content": "print(1)"}, ctx)
    assert bad.verdict == VERDICT_DENY
    assert "计划模式" in bad.reason


def test_custom_full_mode_is_not_over_denied(tmp_path, profiles):
    """对照组：自定义 kind=full 模式不得被新归一误拒，否则就是「收紧成倒退」。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW, VERDICT_DENY
    from app.orchestration.tools.fs_write import FsWriteTool

    _write_profiles(profiles, {"name": "release_ops", "kind": "full", "tools": []})
    ctx = _ctx(tmp_path, "release_ops", approval="auto")

    d = _decide(FsWriteTool, "fs_write", {"path": "src/x.py", "content": "print(1)"}, ctx)

    assert d.verdict == VERDICT_ALLOW
    assert d.verdict != VERDICT_DENY


def test_builtin_modes_keep_their_behavior(tmp_path, profiles):
    """内置模式的边界行为不因归一下沉而改变（回归基线）。"""
    from app.orchestration.approval_policy import VERDICT_DENY
    from app.orchestration.tools.fs_write import FsWriteTool

    assert _decide(FsWriteTool, "fs_write",
                   {"path": "src/x.py", "content": "1"}, _ctx(tmp_path, "readonly")).verdict == VERDICT_DENY
    assert _decide(FsWriteTool, "fs_write",
                   {"path": "src/x.py", "content": "1"}, _ctx(tmp_path, "plan")).verdict == VERDICT_DENY


# ── C2：workflow 等同 agent ────────────────────────────────────────────────────

def test_workflow_effective_mode_is_workflow(profiles):
    """`workflow` 归一档是 workflow，且仍在能力矩阵的四个键之内。"""
    from app.orchestration import subagent_capabilities

    assert subagent_capabilities.effective_mode("workflow") == "workflow"
    assert "workflow" in subagent_capabilities.CAPABILITIES


def test_custom_modes_share_effective_mode_with_resolve_kind(profiles):
    """能力矩阵不再自带一份映射副本——它与审批层必须得出同一个判定档。"""
    from app.orchestration import subagent_capabilities

    _write_profiles(profiles, {"name": "review_only", "kind": "readonly", "tools": []})

    assert subagent_capabilities.effective_mode("review_only") == pps.resolve_mode_kind("review_only")
    assert subagent_capabilities.effective_mode("review_only") == "readonly"


def test_workflow_gets_full_dispatch_hint(monkeypatch):
    """plan-560-2311 C2：workflow 必须拿到**完整版**派发提示（含「实现」引导）。

    旧的 `!= "agent"` / 白名单判定让 workflow 落到受限版，「实现派 mode=agent」的引导
    在 workflow 下整段消失，与「可派可写子代理」的实现口径矛盾。
    """
    from app.core.config import settings
    from app.orchestration.prompts import subagent_dispatch

    monkeypatch.setattr(settings, "subagent_enabled", True, raising=False)

    hint = subagent_dispatch.subagent_dispatch_hint("workflow")

    assert hint, "workflow 下派发提示不得为空"
    assert hint == subagent_dispatch.SUBAGENT_DISPATCH_HINT, "必须是完整版而非只读受限版"
    assert "实现" in hint and 'mode="agent"' in hint, "完整版应含「实现 → mode=agent」的引导"


def test_readonly_and_plan_still_get_limited_dispatch_hint(monkeypatch):
    """对照组：只读 / 计划模式仍拿受限版——C2 的口径校正不能顺带放开这两类模式。"""
    from app.core.config import settings
    from app.orchestration.prompts import subagent_dispatch

    monkeypatch.setattr(settings, "subagent_enabled", True, raising=False)

    for mode in ("readonly", "plan"):
        assert subagent_dispatch.subagent_dispatch_hint(mode) == \
            subagent_dispatch.SUBAGENT_DISPATCH_HINT_READONLY


def test_custom_readonly_mode_gets_limited_dispatch_hint(monkeypatch, profiles):
    """自定义 kind=readonly 模式同样拿受限版（此前因模式名不在白名单里而漏判成完整版）。"""
    from app.core.config import settings
    from app.orchestration.prompts import subagent_dispatch

    monkeypatch.setattr(settings, "subagent_enabled", True, raising=False)
    _write_profiles(profiles, {"name": "review_only", "kind": "readonly", "tools": []})

    assert subagent_dispatch.subagent_dispatch_hint("review_only") == \
        subagent_dispatch.SUBAGENT_DISPATCH_HINT_READONLY


def test_workflow_gets_operator_hint(monkeypatch):
    """plan-560-2311 C2：`operator_subagent_hint("workflow", gui_tools=True)` 不得为空。"""
    from app.core.config import settings
    from app.orchestration.prompts import subagent_dispatch

    monkeypatch.setattr(settings, "subagent_enabled", True, raising=False)
    monkeypatch.setattr(settings, "operator_subagent_enabled", True, raising=False)

    hint = subagent_dispatch.operator_subagent_hint("workflow", gui_tools=True)

    assert hint
    assert hint == subagent_dispatch.OPERATOR_SUBAGENT_HINT


def test_operator_hint_still_absent_for_readonly_modes(monkeypatch, profiles):
    """对照组：只读 / 计划模式（含自定义）不注入 operator 提示——诱导一次必被拒的派发没有意义。"""
    from app.core.config import settings
    from app.orchestration.prompts import subagent_dispatch

    monkeypatch.setattr(settings, "subagent_enabled", True, raising=False)
    monkeypatch.setattr(settings, "operator_subagent_enabled", True, raising=False)
    _write_profiles(profiles, {"name": "review_only", "kind": "readonly", "tools": []})

    for mode in ("readonly", "plan", "review_only"):
        assert subagent_dispatch.operator_subagent_hint(mode, gui_tools=True) == ""
