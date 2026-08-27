"""v0.9: 轻量数据库迁移 — 启动时幂等补列,不引入 Alembic。

策略:
- create_all 已建新表;此模块只负责给旧库的已存在表补新增列。
- SQLite:用 PRAGMA table_info 检测列名。
- PostgreSQL:用 information_schema.columns 检测。
- 每列独立 try/except,失败仅记录 warning,不阻塞启动。
"""
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

# (表名, 列名, DDL 列定义)
# 注意：v2 重构后模型定义与 v1 差异很大，以下迁移覆盖 v1→v2 所有缺失列。
# 团队相关表 (team_agents/teams/agent_templates/session_members/task_edges/decisions)
# 已被 v2 废弃，不再维护迁移。
_MIGRATIONS: list[tuple[str, str, str]] = [
    # ========== sessions（v1 缺 project_id / model_id / pinned / fork_parent / worktree / updated_at）==========
    ("sessions", "project_id", "BIGINT"),
    ("sessions", "workspace_root", "VARCHAR(512)"),
    ("sessions", "knowledge_base_ids", "JSON"),
    ("sessions", "rules_doc", "VARCHAR(512)"),
    ("sessions", "rules_docs", "JSON"),
    ("sessions", "model_id", "BIGINT"),
    ("sessions", "pinned", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("sessions", "fork_parent_id", "BIGINT"),
    ("sessions", "worktree_path", "VARCHAR(512)"),
    ("sessions", "plan_confirmed", "BOOLEAN DEFAULT 1 NOT NULL"),
    ("sessions", "updated_at", "VARCHAR"),
    # ========== sessions（v2.2 对齐 zcode 3.12：权限模式）==========
    ("sessions", "permission_mode", "VARCHAR(20) DEFAULT 'default' NOT NULL"),
    # ========== sessions（plan-88：确认执行后恢复 plan 模式标记）==========
    ("sessions", "plan_restore_after_turn", "BOOLEAN DEFAULT 0 NOT NULL"),
    # ========== sessions（v1.1：最后一次 API 真实上下文占用）==========
    ("sessions", "last_prompt_tokens", "INTEGER DEFAULT 0 NOT NULL"),
    ("sessions", "last_usage_at", "VARCHAR(40)"),
    # ========== sessions（v21：主会话上下文摘要持久化）==========
    ("sessions", "shared_context", "JSON"),
    # ========== exec_policy_rules（v2.2：工具级规则）==========
    ("exec_policy_rules", "tool_name", "VARCHAR(60)"),
    # ========== messages（v1 缺 turn_id）==========
    ("messages", "turn_id", "BIGINT"),
    ("messages", "deleted", "BOOLEAN DEFAULT 0 NOT NULL"),
    # ========== artifacts（v1 缺 git_baseline / files）==========
    ("artifacts", "git_baseline", "VARCHAR(64)"),
    ("artifacts", "files", "JSON"),
    # ========== tasks（v1 缺 turn_id / agent_id / note，assigned_agent_id→agent_id 重命名）==========
    ("tasks", "turn_id", "BIGINT"),
    ("tasks", "agent_id", "BIGINT"),
    ("tasks", "note", "VARCHAR(500)"),
    ("tasks", "kind", "VARCHAR(16) DEFAULT 'request' NOT NULL"),
    ("tasks", "depends_on", "JSON"),
    ("tasks", "estimate", "INTEGER"),
    ("tasks", "is_hidden", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("tasks", "needs_review", "BOOLEAN DEFAULT 0 NOT NULL"),
    # ========== audit_logs（v1 表结构不同，v2 缺 session_id / turn_id）==========
    ("audit_logs", "session_id", "BIGINT"),
    ("audit_logs", "turn_id", "BIGINT"),
    # ========== models（v2 新增多模态 / api_format / api_key / reasoning_efforts）==========
    ("models", "is_multimodal", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("models", "api_format", "VARCHAR(20) DEFAULT 'openai'"),
    ("models", "api_key", "VARCHAR(500)"),
    ("models", "reasoning_efforts", "JSON"),
    # ========== models（v16 供应商化：模型挂到 provider 下）==========
    ("models", "provider_id", "BIGINT"),
    # ========== providers（v23 ta3 供应商：登录态）==========
    ("providers", "auth_status", "VARCHAR(20)"),
    ("providers", "account_label", "VARCHAR(120)"),
    # ========== models（v23 ta3 模型：远端元数据 JSON）==========
    ("models", "ta3_meta", "JSON"),
    # ========== models（v24 workbuddy 模型：远端元数据 JSON）==========
    ("models", "workbuddy_meta", "JSON"),
    # ========== models（v25 trae 模型：远端元数据 JSON）==========
    ("models", "trae_meta", "JSON"),
    # ========== team_agents（v1 遗留表，v2 不再使用，仅保留迁移以防旧表有数据）==========
    ("team_agents", "learned_facts", "JSON"),
    ("team_agents", "skill_ids", "JSON"),
    ("team_agents", "mcp_server_ids", "JSON"),
    # ========== rollback_writes（plan-88：二进制/超限文件只走 checkpoint 恢复）==========
    ("rollback_writes", "binary", "BOOLEAN DEFAULT 0 NOT NULL"),
]


async def _column_exists(db: AsyncSession, table: str, column: str) -> bool:
    if settings.database_url.startswith("sqlite"):
        result = await db.execute(text(f"PRAGMA table_info({table})"))
        rows = result.fetchall()
        return any(row[1] == column for row in rows)
    else:
        result = await db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ),
            {"t": table, "c": column},
        )
        return result.fetchone() is not None


async def _table_exists(db: AsyncSession, table: str) -> bool:
    if settings.database_url.startswith("sqlite"):
        result = await db.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        )
        return result.fetchone() is not None
    else:
        result = await db.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name=:t"),
            {"t": table},
        )
        return result.fetchone() is not None


async def run_migrations(db: AsyncSession) -> dict:
    """启动时执行,幂等。返回 {"migrated": int, "skipped": int, "errors": int}。"""
    migrated = 0
    skipped = 0
    errors = 0
    for table, column, ddl in _MIGRATIONS:
        try:
            if not await _table_exists(db, table):
                skipped += 1
                continue
            if await _column_exists(db, table, column):
                skipped += 1
                continue
            await db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            await db.commit()
            migrated += 1
            logger.info("迁移: %s.%s 已添加", table, column)
        except Exception as e:
            errors += 1
            logger.warning("迁移失败 %s.%s: %s(非阻塞,该功能可能降级)", table, column, e)
            try:
                await db.rollback()
            except Exception:
                pass
    # v6.0: 数据迁移 -- 升级核心角色模板提示词（幂等，仅旧版才升级）
    try:
        await _upgrade_template_prompts(db)
    except Exception as e:
        logger.warning("模板提示词升级失败(非阻塞): %s", e)
        try:
            await db.rollback()
        except Exception:
            pass

    # v16: 数据迁移 -- 存量模型按 (base_url, api_key, api_format) 归组生成供应商
    try:
        await _group_models_into_providers(db)
    except Exception as e:
        logger.warning("模型归组迁移失败(非阻塞): %s", e)
        try:
            await db.rollback()
        except Exception:
            pass

    if migrated:
        logger.info("数据库迁移完成: 新增 %d 列, 跳过 %d, 失败 %d", migrated, skipped, errors)
    return {"migrated": migrated, "skipped": skipped, "errors": errors}


async def _upgrade_template_prompts(db: AsyncSession) -> int:
    """v6.0: 幂等升级核心角色模板的 system_prompt（仅当仍是旧版简略文本时）。

    按 role 匹配 LEGACY_ROLE_PROMPTS 快照，只有完全一致才升级为 CORE_ROLE_PROMPTS，
    避免覆盖用户已自定义的提示词。
    """
    # v3：AgentTemplate 模型已移除（团队概念废弃），无模板可升级，直接返回。
    return 0


async def _group_models_into_providers(db: AsyncSession) -> int:
    """v16: 幂等归组——把带 base_url/api_key 且未挂供应商的存量模型，
    按 (base_url, api_key, api_format) 分组生成 Provider 记录并回填 provider_id。
    供应商名取 base_url 主机名（去端口），空 base_url 的模型跳过。
    """
    from urllib.parse import urlparse

    from app.persistence.models.model_reg import Model, Provider

    if not await _table_exists(db, "providers"):
        return 0
    if not await _column_exists(db, "models", "provider_id"):
        return 0

    rows = (await db.execute(
        select(Model).where(Model.provider_id.is_(None))
    )).scalars().all()
    if not rows:
        return 0

    # 已有供应商按 (base_url, api_key, api_format) 建索引，避免重复创建
    existing = (await db.execute(select(Provider))).scalars().all()
    index: dict[tuple, Provider] = {}
    for p in existing:
        index[(p.base_url or "", p.api_key or "", p.api_format or "openai")] = p

    grouped = 0
    for m in rows:
        if not (m.base_url or m.api_key):
            continue  # 无连接信息的模型保持独立
        key = (m.base_url or "", m.api_key or "", getattr(m, "api_format", None) or "openai")
        provider = index.get(key)
        if provider is None:
            host = urlparse(m.base_url).hostname if m.base_url else None
            name = host or "自定义供应商"
            # 同名供应商（不同 key）加序号区分
            existing_names = {p.name for p in index.values()}
            base_name, i = name, 2
            while name in existing_names:
                name = f"{base_name}-{i}"
                i += 1
            provider = Provider(
                tenant_id=1, name=name, base_url=m.base_url,
                api_key=m.api_key, api_format=key[2], is_active=True,
            )
            db.add(provider)
            await db.flush()
            index[key] = provider
            logger.info("迁移: 创建供应商 %s (%s)", provider.name, provider.base_url)
        m.provider_id = provider.id
        grouped += 1
    await db.commit()
    if grouped:
        logger.info("迁移: %d 个存量模型已归组到供应商", grouped)
    return grouped
