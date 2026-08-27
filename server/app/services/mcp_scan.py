"""MCP 本机配置扫描服务（v4 新增）：扫描常见 MCP 客户端配置路径，解析候选 server 列表。

扫描路径（跨平台）：
  - ~/.cursor/mcp.json
  - ~/.claude.json
  - %APPDATA%/Claude/claude_desktop_config.json (Windows) / ~/Library/Application Support/Claude/claude_desktop_config.json (macOS)
  - ~/.codebuddy/mcp.json
"""
import json
import os
from pathlib import Path


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _candidate_paths() -> list[Path]:
    home = _home()
    paths = [
        home / ".cursor" / "mcp.json",
        home / ".claude.json",
        home / ".codebuddy" / "mcp.json",
        home / ".chatcoder" / "mcp.json",
        home / ".qoder" / "mcp.json",
        home / ".trae" / "mcp.json",
        home / ".codex" / "mcp.json",
        home / ".mcp.json",
    ]
    # Claude Desktop / CodeBuddy 桌面端配置
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
            paths.append(Path(appdata) / "CodeBuddy" / "mcp.json")
            paths.append(Path(appdata) / "chatcoder" / "mcp.json")
    elif os.name == "posix":
        paths.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    return paths


def _parse_stdio(spec: dict) -> dict | None:
    """解析单个 stdio 类型 server spec，返回统一候选 dict。"""
    cmd = spec.get("command")
    if not cmd:
        return None
    return {
        "name": "",  # 由调用方填充
        "transport": "stdio",
        "command": str(cmd),
        "args": spec.get("args") or [],
        "env": spec.get("env") or {},
        "url": None,
        "source_path": "",
    }


def _parse_sse(spec: dict) -> dict | None:
    """解析 sse/http 类型 server spec。"""
    url = spec.get("url")
    if not url:
        return None
    return {
        "name": "",
        "transport": "sse",
        "command": None,
        "args": [],
        "env": {},
        "url": str(url),
        "source_path": "",
    }


def _parse_config(raw: str, source_path: str) -> list[dict]:
    """解析单个配置文件内容，返回候选列表。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    # Claude Desktop / CodeBuddy 格式: { "mcpServers": { name: spec } }
    servers = data.get("mcpServers") or data.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        # .claude.json 可能有顶层 mcpServers 嵌套在 projects 下
        if isinstance(data, dict):
            for _proj, proj_data in data.items():
                if isinstance(proj_data, dict) and isinstance(proj_data.get("mcpServers"), dict):
                    servers.update(proj_data["mcpServers"])
        if not isinstance(servers, dict):
            return []

    candidates: list[dict] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        transport = str(spec.get("type", "stdio"))
        if transport in ("sse", "http"):
            parsed = _parse_sse(spec)
        else:
            parsed = _parse_stdio(spec)
        if parsed:
            parsed["name"] = str(name)
            parsed["source_path"] = source_path
            candidates.append(parsed)
    return candidates


async def scan_local_mcp() -> list[dict]:
    """扫描本机常见 MCP 配置路径，返回候选 server 列表（不落库）。"""
    candidates: list[dict] = []
    seen: set[str] = set()
    for p in _candidate_paths():
        try:
            if not p.is_file():
                continue
            raw = p.read_text(encoding="utf-8", errors="replace")
            for item in _parse_config(raw, str(p)):
                name = str(item.get("name") or "")
                if not name or name in seen:
                    continue
                seen.add(name)
                candidates.append(item)
        except OSError:
            continue
    return candidates
