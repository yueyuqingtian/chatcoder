"""v3.6: Skills & MCP Service —— 技能和 MCP Server 的 CRUD + 扫描同步。

功能：
- 创建/查询/更新/删除 Skill
- 创建/查询/更新/删除 McpServer
- 扫描外部工具（Codex/CodeBuddy/Qoder/Trae）的技能和 MCP 配置，同步入库
- Agent 绑定/解绑 Skill 和 MCP Server
- 查询 Agent 可用的 Skill 和 MCP Server 列表
"""
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.skill_scanner import (
    ScannedMcpServer,
    ScannedSkill,
    fetch_mcp_tools,
    scan_all_mcp_servers,
    scan_all_skills,
)
from app.persistence.models.agent import Agent
from app.persistence.models.skill import McpServer, Skill

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────
# Skill CRUD
# ───────────────────────────────────────────────────────────────────

async def create_skill(
    db: AsyncSession, *, name: str, display_name: str | None = None,
    description: str | None = None, content: str | None = None,
    source: str = "custom", path: str | None = None,
    trigger: str | None = None, tools: list[str] | None = None,
    tags: list[str] | None = None, is_active: bool = True,
    auto_load: bool = True, meta: dict | None = None,
) -> int:
    """创建技能（写引擎单写线程），返回 skill id。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        skill = Skill(
            name=name,
            display_name=display_name or name,
            description=description,
            content=content,
            source=source,
            path=path,
            trigger=trigger,
            tools=tools,
            tags=tags,
            is_active=is_active,
            auto_load=auto_load,
            meta=meta,
        )
        s.add(skill)
        s.flush()
        sid = skill.id
        s.commit()
        return sid

    return await run_write_locked(patch, label="skill.create")


async def list_skills(db: AsyncSession, source: str | None = None) -> list[Skill]:
    """列出所有技能，可按来源筛选。"""
    stmt = select(Skill).order_by(Skill.source, Skill.name)
    if source:
        stmt = stmt.where(Skill.source == source)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_skill(db: AsyncSession, skill_id: int) -> Skill | None:
    return await db.get(Skill, skill_id)


async def get_skill_by_name(db: AsyncSession, name: str) -> Skill | None:
    res = await db.execute(select(Skill).where(Skill.name == name))
    return res.scalars().first()


async def update_skill(db: AsyncSession, skill_id: int, **kwargs: Any) -> bool:
    """更新技能字段（写引擎单写线程）。返回可否找到。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        skill = s.get(Skill, skill_id)
        if skill is None:
            return False
        for key, val in kwargs.items():
            if val is not None and hasattr(skill, key):
                setattr(skill, key, val)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"skill.update.{skill_id}")


async def delete_skill(db: AsyncSession, skill_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        skill = s.get(Skill, skill_id)
        if skill is None:
            return False
        s.delete(skill)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"skill.delete.{skill_id}")


# ───────────────────────────────────────────────────────────────────
# MCP Server CRUD
# ───────────────────────────────────────────────────────────────────

async def create_mcp_server(
    db: AsyncSession, *, name: str, display_name: str | None = None,
    description: str | None = None, source: str = "custom",
    transport: str = "stdio", command: str | None = None,
    args: list | None = None, env: dict | None = None,
    url: str | None = None, tools: list | None = None,
    is_active: bool = True, path: str | None = None,
    meta: dict | None = None, fetch_tools: bool = True,
) -> int:
    """创建 MCP Server 配置（握手在 async 侧完成，写入经写引擎单写线程），返回 id。"""
    from app.persistence.database import run_write_locked

    # 智能切分：如果 command 中包含空格且 args 为空，自动通过 shlex.split 分离
    norm_cmd = command.strip() if command else None
    norm_args = list(args) if args else []
    if norm_cmd and not norm_args and " " in norm_cmd:
        import shlex
        try:
            tokens = shlex.split(norm_cmd, posix=False)
            if tokens:
                norm_cmd = tokens[0]
                norm_args = tokens[1:]
        except Exception:
            pass

    fetched_tools: list | None = None
    # v6: 创建/导入时若未提供 tools，自动握手获取真实工具列表，
    # 避免 build_mcp_tools_for_agent 退化为不可用的通用 call 工具（修复 agent 无法使用 MCP）。
    # v6.5: fetch_tools=False 时（导入未启用的 server）跳过握手，避免 codegraph 等
    # 不响应 MCP 的进程把创建请求挂死。
    if fetch_tools and not tools and transport == "stdio" and norm_cmd:
        try:
            from app.orchestration.skill_scanner import fetch_mcp_tools
            # v6: 传入项目路径(rootUri)，codegraph 等 server 依赖它定位项目才能响应握手
            fetched = await fetch_mcp_tools(
                norm_cmd, norm_args or [], env or {}, root_path=path,
            )
            if fetched:
                fetched_tools = fetched
        except Exception:
            pass

    def patch(s):
        srv = McpServer(
            name=name,
            display_name=display_name or name,
            description=description,
            source=source,
            transport=transport,
            command=norm_cmd,
            args=norm_args,
            env=env,
            url=url,
            tools=fetched_tools or tools,
            is_active=is_active,
            path=path,
            meta=meta,
        )
        s.add(srv)
        s.flush()
        sid = srv.id
        s.commit()
        return sid

    return await run_write_locked(patch, label="mcp.create")


async def list_mcp_servers(db: AsyncSession, source: str | None = None) -> list[McpServer]:
    """列出所有 MCP Server，可按来源筛选。"""
    stmt = select(McpServer).order_by(McpServer.source, McpServer.name)
    if source:
        stmt = stmt.where(McpServer.source == source)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_mcp_server(db: AsyncSession, server_id: int) -> McpServer | None:
    return await db.get(McpServer, server_id)


async def update_mcp_server(db: AsyncSession, server_id: int, **kwargs: Any) -> bool:
    """更新 MCP Server 字段（异步握手 + 写引擎单写线程）。返回可否找到。

    plan-234-1171 R1: 修正握手调用签名。此前按 `fetch_mcp_tools(_existing)` 单参调用，
    而定义为 `(command, args, env, root_path=None)`，TypeError 被下方 except 吞进
    logger.debug，表现为「打开启用开关后工具清单永远为空」的静默失败。
    `workspace` 为项目工作区根（非 ORM 字段，仅用于握手 rootUri/占位符替换）。
    """
    from app.persistence.database import run_write_locked

    workspace = kwargs.pop("workspace", None) or None

    _existing = await get_mcp_server(db, server_id)
    if _existing is None:
        return False
    if _existing.is_active and not _existing.tools and _existing.transport == "stdio" and _existing.command:
        try:
            from app.core.config import settings
            from app.orchestration.skill_scanner import fetch_mcp_tools
            fetched = await fetch_mcp_tools(
                _existing.command, _existing.args or [], _existing.env or {},
                root_path=workspace or settings.workspace_root,
            )
            if fetched:
                kwargs["tools"] = fetched
        except Exception:
            # 非阻塞语义保留，但提升到 warning：此前 debug 级别让签名类错误长期不可见
            logger.warning("[mcp] update 时拉取工具列表失败 %s", _existing.name, exc_info=True)

    def patch(s):
        srv = s.get(McpServer, server_id)
        if srv is None:
            return False
        for key, val in kwargs.items():
            if val is not None and hasattr(srv, key):
                setattr(srv, key, val)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"mcp.update.{server_id}")


async def delete_mcp_server(db: AsyncSession, server_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        srv = s.get(McpServer, server_id)
        if srv is None:
            return False
        s.delete(srv)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"mcp.delete.{server_id}")


# ───────────────────────────────────────────────────────────────────
# 扫描同步
# ───────────────────────────────────────────────────────────────────

async def sync_scanned_skills(
    db: AsyncSession, workspace_root: str | None = None,
) -> dict:
    """扫描外部工具的技能文件，同步到数据库（写引擎单写线程内批量 upsert）。

    - 新发现的技能：创建记录
    - 已存在的技能（按 name 匹配）：更新 content/path
    - 数据库中手动创建的技能（source=custom）：不会被覆盖

    Returns:
        {"added": int, "updated": int, "unchanged": int, "total_scanned": int}
    """
    from app.persistence.database import run_write_locked

    scanned = scan_all_skills(workspace_root)

    def patch(s):
        added = 0
        updated = 0
        unchanged = 0
        for item in scanned:
            existing = s.execute(select(Skill).where(Skill.name == item.name)).scalars().first()
            if existing is None:
                s.add(Skill(
                    name=item.name,
                    display_name=item.display_name,
                    description=item.description,
                    content=item.content,
                    source=item.source,
                    path=item.path,
                    trigger=item.trigger,
                    tools=item.tools,
                    tags=item.tags,
                    meta=item.meta,
                    # plan-308-1542 需求2：显式置 True——否则历史 schema 下可能写入 NULL，
                    # 进而被清单查询漏掉（见 get_global_skills 的注释）。
                    is_active=True,
                    auto_load=True,
                ))
                added += 1
            elif existing.source != "custom":
                changed = False
                if existing.content != item.content:
                    existing.content = item.content
                    changed = True
                if existing.path != item.path:
                    existing.path = item.path
                    changed = True
                if existing.display_name != item.display_name:
                    existing.display_name = item.display_name
                    changed = True
                # plan-308-1542 需求2：历史行的 auto_load 为 NULL 时补齐为 True，
                # 否则该技能永远进不了「Available Skills」。
                if existing.auto_load is None:
                    existing.auto_load = True
                    changed = True
                if changed:
                    updated += 1
                else:
                    unchanged += 1
            else:
                unchanged += 1
        s.commit()
        return {
            "added": added, "updated": updated,
            "unchanged": unchanged, "total_scanned": len(scanned),
        }

    result = await run_write_locked(patch, label="skill.sync")
    logger.info(
        "技能扫描同步完成: 扫描=%d 新增=%d 更新=%d 未变=%d",
        result["total_scanned"], result["added"], result["updated"], result["unchanged"],
    )
    return result


async def sync_scanned_mcp_servers(
    db: AsyncSession, workspace_root: str | None = None,
) -> dict:
    """扫描外部工具的 MCP 配置，同步到数据库（写引擎单写线程内批量 upsert）。

    Returns:
        {"added": int, "updated": int, "unchanged": int, "total_scanned": int}
    """
    from app.persistence.database import run_write_locked

    scanned = scan_all_mcp_servers(workspace_root)
    # 扫描时获取 tools/list 填充到数据库（网络在 async 侧完成，写线程内仅 DB）。
    # plan-234-1171 R1: 传入工作区根——codegraph 等 server 依赖 rootUri 定位项目，
    # 且 args 中的 ${workspaceFolder} 需在 spawn 前替换（见 fetch_mcp_tools）。
    fetched_tools: dict[str, list] = {}
    for item in scanned:
        if item.command:
            try:
                fetched = await fetch_mcp_tools(
                    item.command, item.args, item.env, root_path=workspace_root,
                )
                if fetched:
                    fetched_tools[item.name] = fetched
            except Exception:
                pass

    def patch(s):
        added = 0
        updated = 0
        unchanged = 0
        for item in scanned:
            existing = s.execute(select(McpServer).where(McpServer.name == item.name)).scalars().first()
            if existing is None:
                s.add(McpServer(
                    name=item.name,
                    display_name=item.display_name,
                    description=item.description,
                    source=item.source,
                    transport=item.transport,
                    command=item.command,
                    args=item.args,
                    env=item.env,
                    url=item.url,
                    tools=fetched_tools.get(item.name),
                    path=item.path,
                    meta=item.meta,
                ))
                added += 1
            elif existing.source != "custom":
                changed = False
                if existing.command != item.command:
                    existing.command = item.command
                    changed = True
                if existing.args != item.args:
                    existing.args = item.args
                    changed = True
                if existing.env != item.env:
                    existing.env = item.env
                    changed = True
                if existing.url != item.url:
                    existing.url = item.url
                    changed = True
                if changed:
                    updated += 1
                else:
                    unchanged += 1
            else:
                unchanged += 1
        s.commit()
        return {
            "added": added, "updated": updated,
            "unchanged": unchanged, "total_scanned": len(scanned),
        }

    result = await run_write_locked(patch, label="mcp.sync")
    logger.info(
        "MCP 扫描同步完成: 扫描=%d 新增=%d 更新=%d 未变=%d",
        result["total_scanned"], result["added"], result["updated"], result["unchanged"],
    )
    return result


# ───────────────────────────────────────────────────────────────────
# Agent 绑定
# ───────────────────────────────────────────────────────────────────

async def bind_agent_skills(
    db: AsyncSession, agent_id: int, skill_ids: list[int],
) -> bool:
    """设置 Agent 绑定的技能列表（写引擎单写线程）。返回可否找到。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        agent = s.get(Agent, agent_id)
        if agent is None:
            return False
        agent.skill_ids = skill_ids
        s.commit()
        return True

    return await run_write_locked(patch, label=f"agent.bind.skills.{agent_id}")


async def bind_agent_mcp_servers(
    db: AsyncSession, agent_id: int, mcp_server_ids: list[int],
) -> bool:
    """设置 Agent 绑定的 MCP Server 列表（写引擎单写线程）。返回可否找到。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        agent = s.get(Agent, agent_id)
        if agent is None:
            return False
        agent.mcp_server_ids = mcp_server_ids
        s.commit()
        return True

    return await run_write_locked(patch, label=f"agent.bind.mcp.{agent_id}")


async def get_agent_skills(db: AsyncSession, agent: Agent) -> list[Skill]:
    """获取 Agent 绑定的（且激活的）技能列表。"""
    skill_ids = agent.skill_ids or []
    if not skill_ids:
        return []
    res = await db.execute(
        select(Skill).where(Skill.id.in_(skill_ids), Skill.is_active == True)  # noqa: E712
    )
    return list(res.scalars().all())


async def get_agent_mcp_servers(db: AsyncSession, agent: Agent) -> list[McpServer]:
    """获取 Agent 生效的 MCP Server 列表。"""
    bound_ids = set(agent.mcp_server_ids or [])
    res = await db.execute(
        select(McpServer).where(
            (McpServer.is_active == True) | (McpServer.id.in_(bound_ids))  # noqa: E712
        )
    )
    servers = list(res.scalars().all())
    seen: set[int] = set()
    out: list[McpServer] = []
    for s in servers:
        if s.id in seen:
            continue
        seen.add(s.id)
        out.append(s)
    return out


# v2: 全局技能/MCP（context_manager 注入用）
async def get_global_skills(db: AsyncSession) -> list[Skill]:
    """全局激活技能。

    plan-308-1542 需求2：条件从 `auto_load == True` 放宽为 `auto_load IS NOT False`。
    旧行为下 auto_load 为 NULL（历史数据 / 部分安装路径未写该列）的技能会被静默排除出
    「Available Skills」，AI 看不到也调不到——这正是"插件里装的技能 AI 用不了"的一环。
    """
    res = await db.execute(
        select(Skill).where(
            Skill.is_active == True,  # noqa: E712
            Skill.auto_load.is_not(False),
        )
    )
    return list(res.scalars().all())


async def get_global_mcp_servers(db: AsyncSession) -> list[McpServer]:
    """全局激活 MCP Server。"""
    res = await db.execute(
        select(McpServer).where(McpServer.is_active == True)  # noqa: E712
    )
    return list(res.scalars().all())
