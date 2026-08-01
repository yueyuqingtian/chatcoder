"""v3.6: Skills 扫描器 —— 自动发现外部工具的技能文件。

支持的扫描来源：
- Codex: .codex/skills/*.md (项目级) 或 ~/.codex/skills/*.md (全局)
- CodeBuddy: .codebuddy/skills/*.md (项目级) 或 ~/.codebuddy/skills/*.md
- Qoder: .qoder/skills/*.md (项目级) 或 ~/.qoder/skills/*.md
- Trae: .trae/skills/*.md (项目级) 或 ~/.trae/skills/*.md

每个 .md 文件代表一个技能，文件名（去后缀）作为技能名，
文件内容作为技能指令。

MCP 配置扫描来源：
- Codex: .codex/mcp.json 或 ~/.codex/mcp.json
- CodeBuddy: .codebuddy/mcp.json 或 ~/.codebuddy/mcp.json
- Qoder: .qoder/mcp.json 或 ~/.qoder/mcp.json
- Trae: .trae/mcp.json 或 ~/.trae/mcp.json

也支持标准 MCP 配置：
- .mcp.json (项目根)
- ~/.claude/mcp.json (Claude Code 风格)
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 外部工具扫描目录配置
_TOOL_DIRS: dict[str, list[str]] = {
    "codex": [".codex", ".codex"],
    "codebuddy": [".codebuddy", ".codebuddy"],
    "qoder": [".qoder", ".qoder"],
    "trae": [".trae", ".trae"],
}

# 每种工具在项目级和全局级（~）下的相对路径
def _scan_dirs(source: str) -> tuple[Path, Path]:
    """返回 (项目级目录, 全局级目录)。"""
    dir_name = _TOOL_DIRS.get(source, [f".{source}", f".{source}"])[0]
    project_dir = Path(dir_name)
    home_dir = Path.home() / dir_name
    return project_dir, home_dir


@dataclass
class ScannedSkill:
    """扫描到的技能信息。"""
    name: str
    display_name: str
    description: str
    source: str  # codex / codebuddy / qoder / trae
    path: str
    content: str
    trigger: str = ""
    tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class ScannedMcpServer:
    """扫描到的 MCP Server 配置。"""
    name: str
    display_name: str
    description: str
    source: str  # codex / codebuddy / qoder / trae / mcp_standard
    path: str
    transport: str  # stdio / sse / websocket
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    url: str = ""
    meta: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)  # v4.8: MCP tools/list 结果缓存


def scan_all_skills(workspace_root: str | None = None) -> list[ScannedSkill]:
    """扫描所有外部工具的技能文件。

    Args:
        workspace_root: 项目工作目录。None 时仅扫描全局级。

    Returns:
        扫描到的技能列表（去重后）。
    """
    results: list[ScannedSkill] = []
    seen_names: set[str] = set()

    for source in _TOOL_DIRS:
        try:
            skills = _scan_source_skills(source, workspace_root)
            for skill in skills:
                if skill.name not in seen_names:
                    seen_names.add(skill.name)
                    results.append(skill)
        except Exception as e:
            logger.debug("扫描 %s 技能失败(非阻塞): %s", source, e)

    return results


def _scan_source_skills(source: str, workspace_root: str | None) -> list[ScannedSkill]:
    """扫描单个来源的技能文件。"""
    project_dir, home_dir = _scan_dirs(source)
    skills_dir_project = project_dir / "skills"
    skills_dir_home = home_dir / "skills"

    skills: list[ScannedSkill] = []
    search_dirs: list[tuple[Path, bool]] = []  # (dir, is_global)

    if workspace_root:
        ws_skills = Path(workspace_root) / skills_dir_project
        if ws_skills.is_dir():
            search_dirs.append((ws_skills, False))
    if skills_dir_home.is_dir():
        search_dirs.append((skills_dir_home, True))

    for skills_dir, is_global in search_dirs:
        try:
            for md_file in sorted(skills_dir.glob("*.md")):
                skill = _parse_skill_file(md_file, source, is_global)
                if skill:
                    skills.append(skill)
            # 也扫描 YAML 格式
            for yml_file in sorted(skills_dir.glob("*.yaml")) + sorted(skills_dir.glob("*.yml")):
                skill = _parse_skill_file_yml(yml_file, source, is_global)
                if skill:
                    skills.append(skill)
        except OSError as e:
            logger.debug("扫描目录 %s 失败: %s", skills_dir, e)

    return skills


def _parse_skill_file(md_path: Path, source: str, is_global: bool) -> ScannedSkill | None:
    """解析 Markdown 格式的技能文件。"""
    try:
        content = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    name = md_path.stem  # 文件名（去后缀）
    display_name = name.replace("_", " ").replace("-", " ").title()

    # 尝试从 Front Matter 提取元数据
    trigger = ""
    tools: list[str] = []
    tags: list[str] = []
    desc = ""
    body = content

    # YAML front matter: ---\n...\n---
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2).strip()
        for line in fm_text.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'")
                if key in ("description", "desc"):
                    desc = val
                elif key in ("trigger", "when"):
                    trigger = val
                elif key in ("tools", "tool"):
                    tools = [t.strip() for t in val.split(",") if t.strip()]
                elif key in ("tags", "tag"):
                    tags = [t.strip() for t in val.split(",") if t.strip()]
                elif key in ("name", "title", "display_name"):
                    display_name = val
    else:
        # 从正文第一行提取标题
        first_line = content.strip().split("\n")[0] if content.strip() else ""
        if first_line.startswith("#"):
            display_name = first_line.lstrip("#").strip()
        desc = display_name

    return ScannedSkill(
        name=name,
        display_name=display_name,
        description=desc or display_name,
        source=source,
        path=str(md_path),
        content=body or content,
        trigger=trigger,
        tools=tools,
        tags=tags,
        meta={"is_global": is_global},
    )


def _parse_skill_file_yml(yml_path: Path, source: str, is_global: bool) -> ScannedSkill | None:
    """解析 YAML 格式的技能文件。"""
    try:
        content = yml_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    name = yml_path.stem
    display_name = name.replace("_", " ").replace("-", " ").title()

    # 简易 YAML 解析（不依赖 pyyaml）
    trigger = ""
    desc = ""
    tools: list[str] = []
    tags: list[str] = []
    body = content

    for line in content.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip().strip('"').strip("'")
            if key in ("description", "desc"):
                desc = val
            elif key in ("trigger", "when"):
                trigger = val
            elif key in ("tools", "tool"):
                tools = [t.strip() for t in val.split(",") if t.strip()]
            elif key in ("tags", "tag"):
                tags = [t.strip() for t in val.split(",") if t.strip()]
            elif key in ("name", "title"):
                display_name = val
            elif key in ("content", "instructions", "body"):
                body = val

    return ScannedSkill(
        name=name,
        display_name=display_name,
        description=desc or display_name,
        source=source,
        path=str(yml_path),
        content=body,
        trigger=trigger,
        tools=tools,
        tags=tags,
        meta={"is_global": is_global},
    )


# ───────────────────────────────────────────────────────────────────
# MCP Server 扫描
# ───────────────────────────────────────────────────────────────────

def scan_all_mcp_servers(workspace_root: str | None = None) -> list[ScannedMcpServer]:
    """扫描所有外部工具的 MCP 配置文件。

    搜索以下文件：
    - .codex/mcp.json, .codebuddy/mcp.json, .qoder/mcp.json, .trae/mcp.json
    - .mcp.json (标准 MCP 配置)
    - ~/.claude/mcp.json (Claude Code 风格)

    Args:
        workspace_root: 项目工作目录。

    Returns:
        扫描到的 MCP Server 列表（去重后）。
    """
    results: list[ScannedMcpServer] = []
    seen_names: set[str] = set()

    # 搜索路径列表
    search_paths: list[tuple[str, str, bool]] = []  # (source, file_path, is_global)

    if workspace_root:
        ws = Path(workspace_root)
        for source in _TOOL_DIRS:
            dir_name = _TOOL_DIRS[source][0]
            search_paths.append((source, str(ws / dir_name / "mcp.json"), False))
        # 标准 MCP 配置
        search_paths.append(("mcp_standard", str(ws / ".mcp.json"), False))

    # 全局级
    for source in _TOOL_DIRS:
        dir_name = _TOOL_DIRS[source][0]
        search_paths.append((source, str(Path.home() / dir_name / "mcp.json"), True))
    # Claude Code 风格
    search_paths.append(("mcp_standard", str(Path.home() / ".claude" / "mcp.json"), True))

    for source, file_path, is_global in search_paths:
        try:
            p = Path(file_path)
            if not p.is_file():
                continue
            servers = _parse_mcp_config(p, source, is_global)
            for srv in servers:
                if srv.name not in seen_names:
                    seen_names.add(srv.name)
                    results.append(srv)
        except Exception as e:
            logger.debug("解析 MCP 配置 %s 失败(非阻塞): %s", file_path, e)

    return results


async def fetch_mcp_tools(command: str, args: list[str], env: dict) -> list[dict]:
    """通过 stdio 与 MCP server 握手并获取 tools/list。
    v4.8: 扫描时填充 tools 列表，解决 agent 看不到 MCP 工具的问题。
    """
    import asyncio
    import os

    if not command:
        return []

    full_env = {**os.environ, **env}
    try:
        proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
    except Exception:
        return []

    try:
        # initialize
        init_req = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "chatcoder", "version": "4.8"},
        }}
        proc.stdin.write((json.dumps(init_req) + "\n").encode())
        await proc.stdin.drain()
        await asyncio.wait_for(proc.stdout.readline(), timeout=5)

        # initialized notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write((json.dumps(notif) + "\n").encode())
        await proc.stdin.drain()

        # tools/list
        list_req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        proc.stdin.write((json.dumps(list_req) + "\n").encode())
        await proc.stdin.drain()

        import time
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=max(1, deadline - asyncio.get_event_loop().time()),
                )
            except asyncio.TimeoutError:
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                resp = json.loads(text)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == 1:
                tools = resp.get("result", {}).get("tools", [])
                return tools if isinstance(tools, list) else []
        return []
    except Exception:
        return []
    finally:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass


def _parse_mcp_config(
    config_path: Path, source: str, is_global: bool,
) -> list[ScannedMcpServer]:
    """解析 MCP 配置文件。

    支持两种格式：
    1. 标准格式: {"mcpServers": {"name": {"command": "...", "args": [...]}}}
    2. 简单格式: {"name": {"command": "...", "args": [...]}}
    3. Claude Code 格式: {"mcpServers": {...}} 同标准
    """
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("MCP 配置解析失败 %s: %s", config_path, e)
        return []

    if not isinstance(data, dict):
        return []

    # 提取 servers 字典
    servers_dict = data.get("mcpServers") or data.get("mcp_servers") or data
    if not isinstance(servers_dict, dict):
        return []

    results: list[ScannedMcpServer] = []
    for name, config in servers_dict.items():
        if not isinstance(config, dict):
            continue

        command = config.get("command", "")
        args = config.get("args", [])
        env = config.get("env", {})
        url = config.get("url", "")
        transport = config.get("transport", "")

        # 推断 transport
        if not transport:
            if url:
                transport = "sse"
            elif command:
                transport = "stdio"
            else:
                transport = "stdio"

        display_name = config.get("displayName") or config.get("name") or name
        description = config.get("description", "")

        results.append(ScannedMcpServer(
            name=name,
            display_name=display_name,
            description=description,
            source=source,
            path=str(config_path),
            transport=transport,
            command=command,
            args=args if isinstance(args, list) else [],
            env=env if isinstance(env, dict) else {},
            url=url,
            meta={"is_global": is_global},
        ))

    return results
