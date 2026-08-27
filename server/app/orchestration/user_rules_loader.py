"""用户级规则加载：设置中心保存的 global_rules / workdir_rules。

与 rules_loader（项目规则文档 AGENTS.md/CLAUDE.md 自动扫描）互补：
- rules_loader 扫描工作区内的规则文档；
- 本模块读取 CHATCODER_USER_CONFIG（~/.chatcoder/config.json）中
  设置中心「AI 规则」面板持久化的全局规则与当前工作目录规则。

调用方（context_manager.build_main_context / build_subagent_context）把
返回内容拼进 developer_parts，保证用户设置的规则对模型可见。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

def _load_config() -> dict:
    """每次读取设置文件，确保设置中心修改后对下一轮任务立即生效。"""
    path = Path(os.environ.get("CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json")))
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def load_global_rules() -> str:
    """返回全局规则文本（对所有项目生效），无则空串。"""
    data = _load_config()
    rules = data.get("global_rules")
    return rules if isinstance(rules, str) else ""


def load_workdir_rules(workspace: str) -> str:
    """返回当前工作目录规则文本（rules_workdir_<路径> 键），无则空串。"""
    if not workspace:
        return ""
    data = _load_config()
    try:
        key = str(Path(workspace).resolve())
    except (OSError, ValueError):
        return ""
    stored = data.get(f"rules_workdir_{key}")
    if isinstance(stored, dict):
        rules = stored.get("rules")
        return rules if isinstance(rules, str) else ""
    return stored if isinstance(stored, str) else ""
