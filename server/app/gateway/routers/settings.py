"""系统设置：工作目录、全局规则、记忆开关等桌面版可配置项。"""
import json
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings, update_workspace_root

router = APIRouter()

_USER_CONFIG_PATH = Path(
    os.environ.get("CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json"))
)


# ── 通用配置读写 ──

def _load_config() -> dict:
    """读取持久化用户配置。"""
    try:
        if _USER_CONFIG_PATH.exists():
            return json.loads(_USER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_config(data: dict) -> None:
    """写入持久化用户配置。"""
    try:
        _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


# ── 工作目录 ──

class WorkspaceOut(BaseModel):
    workspace_root: str
    exists: bool


class WorkspaceIn(BaseModel):
    workspace_root: str


def _persist_workspace(path: str) -> None:
    """持久化工作目录到用户配置文件,下次启动自动恢复。"""
    data = _load_config()
    data["workspace_root"] = path
    _save_config(data)


def load_persisted_workspace() -> None:
    """启动时从用户配置恢复工作目录及全局设置。"""
    data = _load_config()
    path = data.get("workspace_root")
    if path and Path(path).exists():
        update_workspace_root(path)
    # v4.2: 启动时恢复自动审批等持久化设置到运行时 settings
    if "auto_approve_tools" in data:
        settings.auto_approve_tools = bool(data["auto_approve_tools"])
    if "force_approval_tools" in data:
        settings.force_approval_tools = str(data["force_approval_tools"])
    if "session_token_budget" in data:
        settings.session_token_budget = int(data["session_token_budget"])


@router.get("/settings/workspace", response_model=WorkspaceOut)
async def get_workspace() -> WorkspaceOut:
    path = settings.workspace_root
    return WorkspaceOut(workspace_root=os.path.abspath(path), exists=Path(path).exists())


@router.put("/settings/workspace", response_model=WorkspaceOut)
async def set_workspace(body: WorkspaceIn) -> WorkspaceOut:
    target = Path(body.workspace_root).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    update_workspace_root(str(target))
    _persist_workspace(str(target))
    return WorkspaceOut(workspace_root=str(target.resolve()), exists=True)


# ── v1.0: 全局设置(记忆开关 + 全局规则) ──

class GlobalSettingsOut(BaseModel):
    memory_enabled: bool = True
    global_rules: str = ""
    auto_compact_enabled: bool = True
    language: str = "zh"
    # v1.0: Agent/安全配置
    auto_approve_tools: bool = False
    force_approval_tools: str = "terminal_exec,ci_run,browser_navigate,browser_click,browser_type"
    session_token_budget: int = 200_000


class GlobalSettingsIn(BaseModel):
    memory_enabled: bool | None = None
    global_rules: str | None = None
    auto_compact_enabled: bool | None = None
    language: str | None = None
    # v1.0: Agent/安全配置
    auto_approve_tools: bool | None = None
    force_approval_tools: str | None = None
    session_token_budget: int | None = None


@router.get("/settings/global", response_model=GlobalSettingsOut)
async def get_global_settings() -> GlobalSettingsOut:
    """读取全局设置(记忆开关、全局规则、Agent/安全配置等)。"""
    data = _load_config()
    return GlobalSettingsOut(
        memory_enabled=data.get("memory_enabled", True),
        global_rules=data.get("global_rules", ""),
        auto_compact_enabled=data.get("auto_compact_enabled", True),
        language=data.get("language", "zh"),
        auto_approve_tools=data.get("auto_approve_tools", settings.auto_approve_tools),
        force_approval_tools=data.get("force_approval_tools", settings.force_approval_tools),
        session_token_budget=data.get("session_token_budget", 200_000),
    )


@router.put("/settings/global", response_model=GlobalSettingsOut)
async def set_global_settings(body: GlobalSettingsIn) -> GlobalSettingsOut:
    """更新全局设置。"""
    data = _load_config()
    if body.memory_enabled is not None:
        data["memory_enabled"] = body.memory_enabled
    if body.global_rules is not None:
        data["global_rules"] = body.global_rules
    if body.auto_compact_enabled is not None:
        data["auto_compact_enabled"] = body.auto_compact_enabled
    if body.language is not None:
        data["language"] = body.language
    # v1.0: Agent/安全配置
    if body.auto_approve_tools is not None:
        data["auto_approve_tools"] = body.auto_approve_tools
        settings.auto_approve_tools = body.auto_approve_tools
    if body.force_approval_tools is not None:
        data["force_approval_tools"] = body.force_approval_tools
        settings.force_approval_tools = body.force_approval_tools
    if body.session_token_budget is not None:
        data["session_token_budget"] = body.session_token_budget
        settings.session_token_budget = body.session_token_budget
    _save_config(data)
    # 运行时更新 context compaction 设置
    if body.auto_compact_enabled is not None:
        settings.context_compaction_enabled = body.auto_compact_enabled
    return GlobalSettingsOut(
        memory_enabled=data.get("memory_enabled", True),
        global_rules=data.get("global_rules", ""),
        auto_compact_enabled=data.get("auto_compact_enabled", True),
        language=data.get("language", "zh"),
        auto_approve_tools=data.get("auto_approve_tools", settings.auto_approve_tools),
        force_approval_tools=data.get("force_approval_tools", settings.force_approval_tools),
        session_token_budget=data.get("session_token_budget", 200_000),
    )


# ── 作用域规则(工作目录 / 群聊) ──

class ScopeRulesOut(BaseModel):
    scope: str
    key: str
    rules: str


class ScopeRulesIn(BaseModel):
    rules: str = ""


def _get_scope_rules_key(scope: str, key: str) -> str:
    """生成作用域规则的存储键。"""
    return f"rules_{scope}_{key}"


@router.get("/settings/rules/{scope}/{key:path}", response_model=ScopeRulesOut)
async def get_scope_rules(scope: str, key: str) -> ScopeRulesOut:
    """读取指定作用域的规则(scope: workdir / group, key: 路径或会话ID)。"""
    data = _load_config()
    storage_key = _get_scope_rules_key(scope, key)
    rules_data = data.get(storage_key, {})
    return ScopeRulesOut(
        scope=scope,
        key=key,
        rules=rules_data.get("rules", "") if isinstance(rules_data, dict) else str(rules_data),
    )


@router.put("/settings/rules/{scope}/{key:path}", response_model=ScopeRulesOut)
async def set_scope_rules(scope: str, key: str, body: ScopeRulesIn) -> ScopeRulesOut:
    """更新指定作用域的规则。"""
    data = _load_config()
    storage_key = _get_scope_rules_key(scope, key)
    data[storage_key] = {"rules": body.rules, "scope": scope, "key": key}
    _save_config(data)
    return ScopeRulesOut(scope=scope, key=key, rules=body.rules)


@router.get("/settings/rules/workdir", response_model=ScopeRulesOut)
async def get_workdir_rules() -> ScopeRulesOut:
    """读取当前工作目录的规则。"""
    ws_key = str(Path(settings.workspace_root).resolve())
    data = _load_config()
    storage_key = _get_scope_rules_key("workdir", ws_key)
    rules_data = data.get(storage_key, {})
    return ScopeRulesOut(
        scope="workdir",
        key=ws_key,
        rules=rules_data.get("rules", "") if isinstance(rules_data, dict) else str(rules_data),
    )


@router.put("/settings/rules/workdir", response_model=ScopeRulesOut)
async def set_workdir_rules(body: ScopeRulesIn) -> ScopeRulesOut:
    """更新当前工作目录的规则。"""
    ws_key = str(Path(settings.workspace_root).resolve())
    data = _load_config()
    storage_key = _get_scope_rules_key("workdir", ws_key)
    data[storage_key] = {"rules": body.rules, "scope": "workdir", "key": ws_key}
    _save_config(data)
    return ScopeRulesOut(scope="workdir", key=ws_key, rules=body.rules)
