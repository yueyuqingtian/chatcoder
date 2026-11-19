"""v2: 幂等种子数据 — 让服务开箱即用。

覆盖:tenant 1、user 1、默认 system_default 模型(读 env)、
默认主代理 Agent、预设配置 profile(default/ci/paranoid)、
默认安全执行策略规则。
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.database import async_session_factory
from app.persistence.models.agent import Agent
from app.persistence.models.config import ConfigProfile
from app.persistence.models.exec_policy import ExecPolicyRule
from app.persistence.models.model_reg import Model
from app.persistence.models.tenant import Tenant, User

logger = logging.getLogger(__name__)


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

    # plan-20261107-subagent-mode-tools：设置已取消工具白名单，初始化不能重新植入隐藏限制。
    presets = [
        {
            "name": "explore",
            "description": "只读探索代理：查看与调研，不执行写入操作",
            "system_prompt": "你是只读探索代理，请使用提供的工具进行调研并给出结论，不要修改文件或操作界面。",
        },
        {
            "name": "general",
            "description": "通用代理：按智能体模式使用已启用工具",
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
    ]
    # plan-03054563-1：移除内置「开发调试」连接器——老库若存有 source="builtin" 的 debugger 行则同步清理
    legacy_dbg = (await db.execute(
        select(McpServer).where(McpServer.name == "debugger", McpServer.source == "builtin")
    )).scalars().all()
    for row in legacy_dbg:
        await db.delete(row)
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


async def _seed_builtin_skills(db: AsyncSession) -> int:
    """注册应用内置技能（plan-693-2868 P2）。

    正文放在 `app/resources/skills/*.md`：技能文档会随版本改进，所以**每次都刷新正文**，
    但绝不碰 `is_active` / `auto_load`——用户手动关掉的技能不能因为升级又自动打开，
    这是 _seed_builtin_mcp 已经定下的同一条口径。

    `auto_load=False`：微应用创建是一份长文档，只在用户用 `/技能` 或模型主动 skill_view
    时才读，常驻会让每一轮上下文都白占几千字。
    """
    import sys
    from pathlib import Path

    from app.persistence.models.skill import Skill

    base = Path(__file__).resolve().parent.parent / "resources" / "skills"
    # 打包态：PyInstaller 把随包文件放到 _MEIPASS，路径与源码树不同
    if not base.is_dir():
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            base = Path(meipass) / "app" / "resources" / "skills"

    specs = [
        {
            "name": "microapp-create",
            "display_name": "微应用创建",
            "description": "为当前任务造一个带界面的微应用（manifest 协议 + 界面规范 + 示例）",
            "file": "microapp-create.md",
            "trigger": "需要可视化界面、结构图、看板，或要反复调整某个可视化结果时",
            "tools": ["microapp_call", "microapp_list", "microapp_validate"],
            "tags": ["microapp", "ui"],
        },
    ]
    created = 0
    for spec in specs:
        content_path = base / spec["file"]
        try:
            content = content_path.read_text(encoding="utf-8")
        except OSError as e:
            # 缺文件不阻断启动：技能少一个，总比服务起不来好
            logger.warning("内置技能正文缺失 %s: %s", content_path, e)
            continue
        existing = (await db.execute(
            select(Skill).where(Skill.name == spec["name"])
        )).scalars().first()
        if existing is None:
            db.add(Skill(
                name=spec["name"],
                display_name=spec["display_name"],
                description=spec["description"],
                source="builtin",
                content=content,
                trigger=spec["trigger"],
                tools=spec["tools"],
                tags=spec["tags"],
                is_active=True,
                auto_load=False,
            ))
            created += 1
        else:
            # 正文随版本刷新；is_active / auto_load 保留用户选择
            existing.display_name = spec["display_name"]
            existing.description = spec["description"]
            existing.content = content
            existing.trigger = spec["trigger"]
            existing.tools = spec["tools"]
            existing.source = "builtin"
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
        builtin_skills = await _seed_builtin_skills(db)
        await _heal_orphan_turns(db)
        await db.commit()
        return {
            "tenant_id": tenant.id,
            "user_id": user.id,
            "model_id": model.id if model else None,
            "main_agent_id": agent.id,
            "builtin_mcp_created": builtin_mcp,
            "builtin_skill_created": builtin_skills,
        }
