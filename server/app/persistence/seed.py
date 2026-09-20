"""v2: 幂等种子数据 — 让服务开箱即用。

覆盖:tenant 1、user 1、默认 system_default 模型(读 env)、
默认主代理 Agent、预设配置 profile(default/ci/paranoid)、
默认安全执行策略规则。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.database import async_session_factory
from app.persistence.models.agent import Agent
from app.persistence.models.config import ConfigProfile
from app.persistence.models.exec_policy import ExecPolicyRule
from app.persistence.models.model_reg import Model
from app.persistence.models.tenant import Tenant, User


async def _get_or_create_tenant(db: AsyncSession) -> Tenant:
    t = await db.get(Tenant, 1)
    if t:
        return t
    t = Tenant(id=1, name="默认租户", plan="free")
    db.add(t)
    await db.flush()
    return t


async def _get_or_create_user(db: AsyncSession) -> User:
    u = await db.get(User, 1)
    if u:
        return u
    u = User(
        id=1, tenant_id=1, email="owner@chatcoder.local",
        display_name="Owner", role="owner",
    )
    db.add(u)
    await db.flush()
    return u


async def _maybe_create_default_model(db: AsyncSession) -> Model | None:
    """仅当服务端默认模型 env 配置齐全时,落地一条 system_default 记录。"""
    if not settings.default_model_ready:
        return None
    res = await db.execute(
        select(Model).where(Model.source_type == "system_default").limit(1)
    )
    existing = res.scalars().first()
    if existing:
        return existing
    m = Model(
        tenant_id=1,
        name=settings.default_llm_model or "default",
        provider=settings.default_llm_provider or "openai_compatible",
        base_url=settings.default_llm_base_url,
        intelligence_level=2,
        source_type="system_default",
        is_active=True,
        is_multimodal=False,
        api_format=settings.default_llm_api_format or "openai",
    )
    db.add(m)
    await db.flush()
    return m


async def _get_or_create_main_agent(db: AsyncSession, model: Model | None) -> Agent:
    """默认主代理（全局唯一 kind=main）。"""
    res = await db.execute(select(Agent).where(Agent.kind == "main").limit(1))
    existing = res.scalars().first()
    if existing:
        return existing
    agent = Agent(kind="main", name="chatcoder", model_id=model.id if model else None)
    db.add(agent)
    await db.flush()
    return agent


async def _seed_profiles(db: AsyncSession) -> None:
    """预设配置 profile（幂等，按 name+scope 判断）。"""
    presets: dict[str, dict] = {
        "default": {
            "approval_policy": "on-request",
            "sandbox_mode": "workspace-write",
            "writable_paths": [],
            "web_search": "cached",
        },
        "ci": {
            "approval_policy": "never",
            "sandbox_mode": "workspace-write",
            "writable_paths": [],
            "web_search": "disabled",
        },
        "paranoid": {
            "approval_policy": "reject",
            "sandbox_mode": "read-only",
            "writable_paths": [],
            "web_search": "disabled",
        },
    }
    for name, data in presets.items():
        res = await db.execute(
            select(ConfigProfile).where(
                ConfigProfile.name == name, ConfigProfile.scope == "global"
            )
        )
        if res.scalars().first() is None:
            db.add(ConfigProfile(name=name, scope="global", data=data, is_active=(name == "default")))
    await db.flush()


async def _seed_exec_policy(db: AsyncSession) -> None:
    """默认安全命令策略（全局，幂等）。"""
    defaults = [
        ("git push", "ask", "推送远程需确认"),
        ("git reset --hard", "ask", "危险重置需确认"),
        ("rm -rf", "ask", "删除操作需确认"),
        ("format", "ask", "磁盘格式化需确认"),
        ("dd", "ask", "底层写入需确认"),
    ]
    for pattern, decision, justification in defaults:
        res = await db.execute(
            select(ExecPolicyRule).where(
                ExecPolicyRule.command_pattern == pattern,
                ExecPolicyRule.session_id.is_(None),
            )
        )
        if res.scalars().first() is None:
            db.add(ExecPolicyRule(
                command_pattern=pattern, decision=decision,
                justification=justification,
            ))
    await db.flush()


async def _seed_subagent_profiles(db: AsyncSession) -> None:
    """v2.2 (对齐 zcode 3.13): 内置子代理类型 Explore / general（幂等）。"""
    from app.persistence.models.subagent_profile import SubagentProfile

    presets = [
        {
            "name": "explore",
            "description": "只读探索代理：搜索/阅读代码，不可写盘",
            "tools_whitelist": [
                "fs_read", "fs_list", "fs_grep", "git_diff",
                "web_fetch", "web_search", "codebase_search", "memory_search",
            ],
            "system_prompt": (
                "你是代码探索代理，只能读取与搜索。请全面定位相关代码并给出结论，"
                "不要修改任何文件。"
            ),
        },
        {
            "name": "general",
            "description": "通用代理：全量工具",
            "tools_whitelist": None,
            "system_prompt": None,
        },
    ]
    for preset in presets:
        res = await db.execute(
            select(SubagentProfile).where(SubagentProfile.name == preset["name"])
        )
        if res.scalars().first() is None:
            db.add(SubagentProfile(**preset))
    await db.flush()


async def _heal_orphan_turns(db: AsyncSession) -> None:
    """启动自愈：把上次进程异常退出遗留的 running turn / running step 统一收尾。

    后端被杀时 turn 可能停留在 running，导致前端左侧会话永远转圈；
    启动时无任何执行中的 turn，running 状态必然是孤儿。

    plan-282-1441：**同时收尾这些 turn 下仍为 running 的步骤**。
    此前只改了 turn.status，而任务步骤（todo_write 落库的 step）仍是 running。
    重启后前端读引擎步骤时看到最后一步"进行中"，于是胶囊显示
    "最后一步还在执行"——即使该任务在退出前其实已经跑完（只是没来得及写终态）。
    收尾后这些步骤如实回到 pending（未完成，可继续），不再伪装成执行中。
    """
    from datetime import datetime, timezone

    from app.persistence.models.task import Task
    from app.persistence.models.turn import Turn

    res = await db.execute(select(Turn).where(Turn.status == "running"))
    orphans = list(res.scalars().all())

    # 步骤收尾不依赖 turn 是否存在：只要步骤还是 running，就说明没有活着的执行者
    step_res = await db.execute(
        select(Task).where(Task.status == "running", Task.kind == "step")
    )
    orphan_steps = list(step_res.scalars().all())
    for st in orphan_steps:
        st.status = "pending"

    if orphans:
        for t in orphans:
            t.status = "failed"
            t.summary = "执行中断(服务重启)"
            t.completed_at = datetime.now(timezone.utc).isoformat()

    # 同时把"任务清单"分组从 running 收回（其下步骤全做完则应显示已完成）
    group_res = await db.execute(
        select(Task).where(Task.status == "running", Task.kind == "group")
    )
    for g in list(group_res.scalars().all()):
        children = list((await db.execute(
            select(Task).where(Task.parent_task_id == g.id, Task.kind == "step")
        )).scalars().all())
        if children and all(c.status == "done" for c in children):
            g.status = "done"
        elif children:
            g.status = "pending"

    if orphans or orphan_steps:
        await db.flush()


async def _seed_builtin_mcp(db: AsyncSession) -> int:
    """注册应用内置的 MCP 服务（plan-282-1441 #7/#8）。

    幂等：按 name 存在即只修正 command/args（版本升级后路径或参数可能变化），
    **不动 is_active**——用户手动启用的状态必须保留（默认不启用是产品要求）。

    command/args 的两种形态：
    - 开发态：sys.executable + ["-m", "app.mcp_servers.<name>"]
    - 打包态：exe 自身 + ["--mcp-server", "<name>"]（exe 不支持 -m，见 run_server 的分流）
    需要工作区上下文的内置 MCP 再追加 ["--workspace", "${workspaceFolder}"]：子进程按该参数匹配
    项目，取不到时回退自身 cwd，而打包态 cwd 是数据目录而非项目根，会匹配不到项目而返回空连接
    列表。占位符由 mcp_wrapper 在 spawn 前替换，无工作区上下文时整项剔除。
    """
    import sys

    from app.persistence.models.skill import McpServer

    frozen = bool(getattr(sys, "frozen", False))
    specs = [
        {
            "name": "database",
            "display_name": "数据库连接",
            "description": "让 AI 通过 / 命令连接项目数据库，执行查询与数据变更"
                           "（读写与 DDL 权限、是否审批均可在设置中严格控制）。",
            # 必须显式传工作区：打包态子进程 cwd 是数据目录，只靠 cwd 会匹配不到项目
            "args": (["--mcp-server", "database"] if frozen
                     else ["-m", "app.mcp_servers.database"]) + ["--workspace", "${workspaceFolder}"],
        },
        {
            "name": "debugger",
            "display_name": "开发调试",
            "description": "让 AI 在项目运行期间调用接口、在代码中设断点或做方法级现场观测"
                           "（Web 前端 CDP、Java JDWP 断点、Arthas 观测——IDEA 调试中也能用）。",
            "args": ["--mcp-server", "debugger"] if frozen else ["-m", "app.mcp_servers.debugger"],
        },
    ]
    created = 0
    for spec in specs:
        existing = (await db.execute(
            select(McpServer).where(McpServer.name == spec["name"])
        )).scalars().first()
        if existing is None:
            db.add(McpServer(
                name=spec["name"],
                display_name=spec["display_name"],
                description=spec["description"],
                source="builtin",
                transport="stdio",
                command=sys.executable,
                args=spec["args"],
                env={},
                is_active=False,   # 默认不启用，由用户手动开启
            ))
            created += 1
        else:
            # 修正入口（换机器/改打包方式后路径会变），保留用户的启停选择
            existing.display_name = spec["display_name"]
            existing.description = spec["description"]
            existing.source = "builtin"
            existing.command = sys.executable
            existing.args = spec["args"]
    await db.flush()
    return created


async def seed() -> dict:
    """执行幂等种子,返回统计信息。"""
    async with async_session_factory() as db:
        tenant = await _get_or_create_tenant(db)
        user = await _get_or_create_user(db)
        model = await _maybe_create_default_model(db)
        agent = await _get_or_create_main_agent(db, model)
        await _seed_profiles(db)
        await _seed_exec_policy(db)
        await _seed_subagent_profiles(db)
        builtin_mcp = await _seed_builtin_mcp(db)
        await _heal_orphan_turns(db)
        await db.commit()
        return {
            "tenant_id": tenant.id,
            "user_id": user.id,
            "model_id": model.id if model else None,
            "main_agent_id": agent.id,
            "builtin_mcp_created": builtin_mcp,
        }
