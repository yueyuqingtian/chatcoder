"""工作流工具调用的效果记录与核对：执行前记下能观察到的状态，续跑对账时据此判断“结果未知”的操作是否已经生效。

- 写入类操作（fs_write / editor_apply_diff / multi_file_edit）：记下每个目标文件执行前的哈希；
  整文件写入还记下写入内容的哈希，核对时可以区分“已执行”与“未执行”。
- 命令（terminal_exec）：记下工作区的文件状态（每个有改动的文件的 git 状态码与内容哈希），
  核对时可以区分“工作区已有改动”与“未观察到改动”。
命令的网络请求、外部服务等副作用不在工作区里，本机无法核对，仍保持“结果未知”。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from app.orchestration.approval_policy import target_paths

WRITE_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")
SHELL_TOOLS = ("terminal_exec",)
_MAX_SHELL_PATHS = 300

VERDICT_LABEL = {
    "not_executed": "已确认未执行，可以重做",
    "executed": "已确认已执行，无需重做",
    "unclear": "文件已被改动，无法确定是否执行，需核实",
    "shell_changed": "工作区文件已有改动，很可能已执行，需核实",
    "shell_unchanged": "未观察到工作区文件改动，是否执行需核实",
}


def file_sha(path: str) -> str | None:
    """文件内容的 SHA-256；不存在、不是文件或读取失败时返回 None。"""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def write_effect(tool_name: str, args: dict[str, Any], workspace: str) -> str | None:
    """写入类操作执行前的文件效果（JSON）。

    记录每个目标文件执行前的哈希；整文件写入还记录写入内容的哈希。
    """
    if tool_name not in WRITE_TOOLS or not workspace:
        return None
    paths = target_paths(tool_name, args)
    if not paths:
        return None
    content = args.get("content") if tool_name == "fs_write" else None
    expected = (
        hashlib.sha256(content.encode("utf-8")).hexdigest() if isinstance(content, str) else None
    )
    files: dict[str, dict[str, str | None]] = {}
    for rel in paths:
        absolute = os.path.normpath(os.path.join(workspace, rel))
        files[absolute] = {"pre": file_sha(absolute), "expected": expected}
    return json.dumps({"files": files}, ensure_ascii=False)


async def _workspace_state(workspace: str) -> dict[str, str] | None:
    """工作区文件状态：每个有改动的文件的 git 状态码与内容哈希。

    非 git 仓库或改动过多时返回 None。
    """
    from app.orchestration.workflow.guard import _git_status  # 延迟导入：守卫模块依赖本模块

    status = await _git_status(workspace)
    if status is None or len(status) > _MAX_SHELL_PATHS:
        return None
    return {
        path: f"{code}:{file_sha(os.path.join(workspace, path)) or '-'}"
        for path, code in status.items()
    }


async def shell_effect(tool_name: str, workspace: str) -> str | None:
    """命令执行前的工作区文件状态（JSON）；不是命令工具、不是 git 仓库或改动过多时返回 None。"""
    if tool_name not in SHELL_TOOLS or not workspace:
        return None
    state = await _workspace_state(workspace)
    if state is None:
        return None
    return json.dumps({"shell": {"workspace": workspace, "state": state}}, ensure_ascii=False)


def _file_verdict(files: dict[str, Any]) -> str | None:
    """依据当前的文件状态核对写入：executed / not_executed / unclear；没有文件效果时返回 None。

    所有目标文件都与预期内容一致 → 已执行；都与执行前一致 → 未执行；其余（部分文件已改、
    或文件被别的改动覆盖）无法确定，交给模型核实。
    """
    if not files:
        return None
    verdicts: set[str] = set()
    for path, info in files.items():
        current = file_sha(path)
        expected = info.get("expected")
        if expected is not None and current == expected:
            verdicts.add("executed")
        elif current == info.get("pre"):
            verdicts.add("not_executed")
        else:
            verdicts.add("unclear")
    return verdicts.pop() if len(verdicts) == 1 else "unclear"


async def effect_verdict(effect_json: str | None) -> str | None:
    """依据当前的状态核对一次操作的效果；没有可核对的效果时返回 None。"""
    if not effect_json:
        return None
    try:
        data = json.loads(effect_json) or {}
    except (TypeError, ValueError):
        return None
    shell = data.get("shell")
    if isinstance(shell, dict):
        before = shell.get("state")
        now = await _workspace_state(str(shell.get("workspace") or ""))
        if not isinstance(before, dict) or now is None:
            return None
        return "shell_changed" if now != before else "shell_unchanged"
    return _file_verdict(data.get("files") or {})
