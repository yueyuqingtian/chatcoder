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
    ("tasks", "needs_review", "BOOLEAN DEFAULT 0 NOT NULL"),
    # ========== audit_logs（v1 表结构不同，v2 缺 session_id / turn_id）==========
    ("audit_logs", "session_id", "BIGINT"),
    ("audit_logs", "turn_id", "BIGINT"),
    # ========== models（v2 新增多模态 / api_format / api_key / reasoning_efforts）==========
    ("models", "is_multimodal", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("models", "api_format", "VARCHAR(20) DEFAULT 'openai'"),
    ("models", "api_key", "VARCHAR(500)"),
    ("models", "reasoning_efforts", "JSON"),
    # ========== team_agents（v1 遗留表，v2 不再使用，仅保留迁移以防旧表有数据）==========
    ("team_agents", "learned_facts", "JSON"),
    ("team_agents", "skill_ids", "JSON"),
    ("team_agents", "mcp_server_ids", "JSON"),
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

    if migrated:
        logger.info("数据库迁移完成: 新增 %d 列, 跳过 %d, 失败 %d", migrated, skipped, errors)
    return {"migrated": migrated, "skipped": skipped, "errors": errors}


async def _upgrade_template_prompts(db: AsyncSession) -> int:
    """v6.0: 幂等升级核心角色模板的 system_prompt（仅当仍是旧版简略文本时）。

    按 role 匹配 LEGACY_ROLE_PROMPTS 快照，只有完全一致才升级为 CORE_ROLE_PROMPTS，
    避免覆盖用户已自定义的提示词。
    """
    from app.orchestration.prompts import CORE_ROLE_PROMPTS, LEGACY_ROLE_PROMPTS

    # v3：AgentTemplate 模型已移除（团队概念废弃），无模板可升级，直接返回。
    return 0
