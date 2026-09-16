"""权限模式配置服务（plan-230-1144 M2）。

改造前问题：工具白名单硬编码在 `engine._READONLY_TOOLS` / `_PLAN_TOOLS`，
模式提示词硬编码在 `_MODE_HINTS`，前端 `setMode` 只接受 3 个值——
"模式自定义、各种模式的权限分配不够自由"无从谈起。

本模块把"模式 → 工具白名单 + 提示词"外置为可配置数据：

- **内置 4 模式**（default / readonly / plan / accept_edits）：白名单沿用
  engine 原定义（含 M1.2 追加的 skill_view / compaction_*），作为默认集，
  用户可覆盖其白名单与提示词；
- **自定义模式**：用户新建，`builtin=False`，白名单从全量工具中勾选，
  持久化到用户 config.json 的 `permission_profiles` 键；
- **安全兜底（新旧双跑取更严方）**：`resolve_tools` 返回的白名单只允许是
  全量注册工具的子集；未识别的自定义模式名在 engine 侧按 `default` 处理。

模式语义（`kind` 字段，供 engine/executor 复用既有判定分支）：
- `full`：全量工具（default / accept_edits 属此类，审批策略不同）；
- `readonly`：仅只读工具；
- `plan`：只读 + 计划文档写 + 命令行（受审批门约束）。

存储位置与 `skills_mcp.py` 的技能仓库配置同址（~/.chatcoder/config.json，
可经 CHATCODER_USER_CONFIG 覆盖），不新增表、不需要迁移。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CFG_PATH = Path(
    os.environ.get("CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json"))
)

# ── 内置模式定义（白名单与 engine 原硬编码保持一致，外置后仍是唯一事实源）──

BUILTIN_PROFILES: list[dict] = [
    {
        "name": "default",
        "display_name": "完全访问",
        "kind": "full",
        "builtin": True,
        "description": "全量工具，按审批策略执行写操作",
        "tools": [],  # 空列表 = 全量工具（不限制）
        "hint": "",
    },
    {
        "name": "readonly",
        "display_name": "只读模式",
        "kind": "readonly",
        "builtin": True,
        "description": "仅查看、检索、分析，禁止任何修改",
        "tools": [
            "fs_read", "fs_list", "fs_grep", "git_diff",
            "memory_search", "web_fetch", "web_search", "view_image",
            "read_attachment", "codebase_search",
            "ask_user_question", "todo_write",
            "skill_view", "compaction_index", "compaction_view",
            # plan-230-1144 M3: 符号检索/文件骨架属纯读探索能力，只读模式可用
            "symbol_search", "outline",
        ],
        "hint": (
            "【审阅模式】当前处于只读审阅模式，你只能查看、检索、分析代码，"
            "严禁执行任何修改操作（禁止写入文件、运行命令、应用补丁）。"
            "请直接给出审阅意见、发现的问题与改进建议。"
        ),
    },
    {
        "name": "plan",
        "display_name": "计划模式",
        "kind": "plan",
        "builtin": True,
        "description": "先规划后执行：只读工具 + 计划文档写入 + 命令行",
        "tools": [
            "fs_read", "fs_list", "fs_grep", "git_diff",
            "memory_search", "web_fetch", "web_search", "view_image",
            "read_attachment", "codebase_search",
            "ask_user_question", "todo_write",
            "skill_view", "compaction_index", "compaction_view",
            "symbol_search", "outline",
            "fs_write", "terminal_exec",
        ],
        # plan 的完整提示词较长且含 {session_id}/{turn_id} 占位符，
        # 仍由 engine._MODE_HINTS 维护（格式化处理在那边）；这里留空表示沿用。
        "hint": "",
    },
    {
        "name": "accept_edits",
        "display_name": "计划执行",
        "kind": "full",
        "builtin": True,
        "description": "确认计划后的执行模式：写盘免审批",
        "tools": [],  # 全量工具，写盘免审批由 executor 按 permission_mode 判定
        "hint": "",
    },
]

_BY_NAME = {p["name"]: p for p in BUILTIN_PROFILES}


def _read_cfg() -> dict:
    try:
        if _CFG_PATH.exists():
            data = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.debug("[permission_profile] 读取用户配置失败", exc_info=True)
    return {}


def _write_cfg(data: dict) -> None:
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_custom() -> list[dict]:
    raw = _read_cfg().get("permission_profiles", [])
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw:
        if isinstance(p, dict) and p.get("name"):
            out.append({
                "name": str(p["name"]),
                "display_name": str(p.get("display_name") or p["name"]),
                "kind": str(p.get("kind") or "full"),
                "builtin": False,
                "description": str(p.get("description") or ""),
                "tools": [str(t) for t in (p.get("tools") or [])],
                "hint": str(p.get("hint") or ""),
            })
    return out


def list_profiles() -> list[dict]:
    """全部模式 = 内置（可能被用户覆盖）+ 自定义。"""
    overrides = {p["name"]: p for p in _load_custom() if p["name"] in _BY_NAME}
    merged = []
    for p in BUILTIN_PROFILES:
        merged.append(overrides.get(p["name"], p))
    merged.extend(p for p in _load_custom() if p["name"] not in _BY_NAME)
    return merged


def get_profile(name: str) -> dict | None:
    for p in list_profiles():
        if p["name"] == name:
            return p
    return None


def resolve_tools(mode: str, all_tool_names: set[str]) -> list[str] | None:
    """把模式解析为工具白名单。

    - `default` / `accept_edits` / 未识别模式 → None（全量，不限制）；
    - 内置只读/计划/自定义模式 → 白名单列表（过滤掉未注册的幽灵工具名）。

    安全口径：白名单只能从 `all_tool_names` 中取（deny-by-default），
    配置里写了不存在的工具名不会产生越权。
    """
    profile = get_profile(mode)
    if profile is None:
        return None
    tools = profile.get("tools") or []
    if not tools:
        return None  # 空 = 全量
    return [t for t in tools if t in all_tool_names]


def resolve_hint(mode: str) -> str:
    """自定义模式的提示词；内置模式返回空串（由 engine 既有逻辑处理）。"""
    profile = get_profile(mode)
    if profile and not profile.get("builtin"):
        return profile.get("hint") or ""
    return ""


def mcp_allowed_tools(mode: str) -> set[str] | None:
    """MCP 工具的注入白名单（None = 不限制）。

    MCP 工具名（`mcp_<server>_<tool>`）随用户配置动态变化，无法预先写进
    内置模式白名单，此前 `_inject_mcp_tools` 因此对它们"无条件注入"——
    设置页里把某个 MCP 工具排除在白名单外并不生效。现按模式类型分流：

    - 只读/计划类（kind=readonly/plan）：返回 None，低风险过滤由 engine 的
      `readonly_only` 规则承担（保持既有行为：规划/审阅时仍可用只读类 MCP 工具）；
    - 显式勾选了工具的模式（tools 非空）：只放行其中列出的 MCP 工具，
      未勾选即不注入——设置页勾选对 MCP 工具同样生效；
    - default / accept_edits 等空列表（= 全量工具）：返回 None（全量注入）。
    """
    profile = get_profile(mode)
    if profile is None:
        return None
    kind = profile.get("kind") or "full"
    if kind in ("readonly", "plan"):
        return None
    tools = profile.get("tools") or []
    if not tools:
        return None  # 空 = 全量
    return {str(t) for t in tools}


def is_readonly_like(mode: str) -> bool:
    """该模式是否属于只读类（MCP 注入只放行只读工具的依据）。"""
    profile = get_profile(mode)
    if profile is None:
        return False
    kind = profile.get("kind") or "full"
    return kind in ("readonly", "plan")


def upsert_profile(profile: dict) -> dict:
    """新建/更新自定义模式，或覆盖内置模式的白名单/提示词。

    校验：name 只允许小写字母/数字/下划线；tools 过滤到已注册集合外仍保留
    （前端按全量工具勾选，此处不做注册校验——未注册工具名在
    resolve_tools 时会被过滤，不产生越权）。
    """
    import re
    name = str(profile.get("name") or "").strip()
    if not re.fullmatch(r"[a-z0-9_]{1,40}", name):
        raise ValueError("模式名仅允许小写字母、数字、下划线（1-40 字符）")
    if profile.get("kind") not in (None, "full", "readonly", "plan"):
        raise ValueError("kind 必须为 full/readonly/plan 之一")

    data = _read_cfg()
    profiles = data.get("permission_profiles", [])
    if not isinstance(profiles, list):
        profiles = []
    entry = {
        "name": name,
        "display_name": str(profile.get("display_name") or name),
        "kind": str(profile.get("kind") or (
            _BY_NAME[name]["kind"] if name in _BY_NAME else "full"
        )),
        "description": str(profile.get("description") or ""),
        "tools": [str(t) for t in (profile.get("tools") or [])],
        "hint": str(profile.get("hint") or ""),
    }
    profiles = [p for p in profiles if not (isinstance(p, dict) and p.get("name") == name)]
    profiles.append(entry)
    data["permission_profiles"] = profiles
    _write_cfg(data)
    return {**entry, "builtin": name in _BY_NAME}


def delete_profile(name: str) -> bool:
    """删除自定义模式；内置模式只能重置覆盖（删除覆盖条目）。"""
    data = _read_cfg()
    profiles = data.get("permission_profiles", [])
    if not isinstance(profiles, list):
        return False
    kept = [p for p in profiles if not (isinstance(p, dict) and p.get("name") == name)]
    if len(kept) == len(profiles):
        return False
    data["permission_profiles"] = kept
    _write_cfg(data)
    return True
