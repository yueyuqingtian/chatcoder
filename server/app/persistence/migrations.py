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
    # ========== sessions（v1.1：最后一次 API 真实上下文占用）==========
    ("sessions", "last_prompt_tokens", "INTEGER DEFAULT 0 NOT NULL"),
    ("sessions", "last_usage_at", "VARCHAR(40)"),
    # ========== sessions（v21：主会话上下文摘要持久化）==========
    ("sessions", "shared_context", "JSON"),
    # ========== sessions（v7：置顶时间——"后置顶在上"排序依据）==========
    ("sessions", "pinned_at", "VARCHAR(40)"),
    # ========== projects（plan-282-1441 #5：工作树复用 Project 表）==========
    ("projects", "is_worktree", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("projects", "parent_project_id", "BIGINT"),
    ("projects", "worktree_branch", "VARCHAR(200)"),
    # ========== db_connections（plan-282-1441 #7：内置 MCP「数据库连接」）==========
    ("db_connections", "project_id", "BIGINT"),
    ("db_connections", "is_active", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("db_connections", "params", "JSON"),
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
    # ========== turns（plan-644：计划模式字段持久化，多轮迭代需求全集与卡片恢复的数据源）==========
    ("turns", "plan_doc_path", "VARCHAR(512)"),
    ("turns", "plan_status", "VARCHAR(20)"),
    # ========== sessions（目标模式：持久目标与续跑状态）==========
    ("sessions", "goal_text", "VARCHAR(2000)"),
    ("sessions", "goal_status", "VARCHAR(20) DEFAULT 'none' NOT NULL"),
    ("sessions", "goal_turns_used", "INTEGER DEFAULT 0 NOT NULL"),
    ("sessions", "goal_created_at", "VARCHAR(40)"),
    # ========== usage_records（plan-152-704：供应商显示名）==========
    ("usage_records", "provider_name", "VARCHAR(120) DEFAULT ''"),
    # ========== scheduled_tasks（plan-230-1144 M1.1：调度器落地所需的执行状态列）==========
    ("scheduled_tasks", "enabled", "BOOLEAN DEFAULT 1 NOT NULL"),
    ("scheduled_tasks", "last_run_at", "VARCHAR(40)"),
    ("scheduled_tasks", "next_run_at", "VARCHAR(40)"),
    ("scheduled_tasks", "last_status", "VARCHAR(16)"),
    ("scheduled_tasks", "last_error", "VARCHAR(300)"),
    ("scheduled_tasks", "missed_policy", "VARCHAR(12) DEFAULT 'skip'"),
    # ========== memory_entries（plan-230-1144 M4.1：记忆三层化）==========
    ("memory_entries", "scope", "VARCHAR(12) DEFAULT 'session'"),
    ("memory_entries", "project_id", "BIGINT"),
    ("memory_entries", "candidate", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("memory_entries", "expires_at", "VARCHAR(40)"),
    ("memory_entries", "superseded_by", "BIGINT"),
    # ========== providers（plan-248-1258 M2.3：供应商级代理）==========
    ("providers", "proxy_mode", "VARCHAR(12) DEFAULT 'inherit'"),
    ("providers", "proxy_url", "VARCHAR(255)"),
    # ========== providers（plan-271-1364：凭据取用策略）==========
    # sticky=粘性优先（上次成功的先用）| round_robin=严格按 priority 轮转
    ("providers", "credential_strategy", "VARCHAR(16) DEFAULT 'sticky'"),
    # ========== workbuddy_auth / ta3_auth / trae_auth（plan-248-1258 M2.2：凭据维度）==========
    # 多账号支持：auth 行归属某条 provider_credentials（旧行迁移时挂到首条凭据）。
    ("workbuddy_auth", "credential_id", "BIGINT"),
    ("ta3_auth", "credential_id", "BIGINT"),
    ("trae_auth", "credential_id", "BIGINT"),
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


# ── plan-278-1391: 遗留 NOT NULL 列清理 ──
# 背景：旧版本模型中被删除的列（如 sessions.plan_restore_after_turn）在旧库里仍然存在；
# 若该列 NOT NULL 且无默认值，新代码 INSERT 不带该列 → 直接抛 NOT NULL 约束错误，
# 表现为「升级后新建会话/发消息必失败」。既有迁移机制只补列、从不清理，故此处补齐。
# 策略：优先 ALTER TABLE DROP COLUMN 彻底清理；SQLite 不支持时重建表补 DEFAULT 兜底；
# 两者都失败才登记到 _LEGACY_NOTNULL_DEFAULTS，由写入侧尽量补齐（最后一道保险）。
_LEGACY_NOTNULL_DEFAULTS: dict[str, dict[str, object]] = {}


def legacy_notnull_defaults(table: str) -> dict[str, object]:
    """返回该表仍需要写入侧补齐默认值的遗留列（DROP / 补 DEFAULT 成功时为空）。"""
    return dict(_LEGACY_NOTNULL_DEFAULTS.get(table) or {})


def _legacy_default_value(col_type: str) -> object:
    """按列类型推断兜底默认值（避免遗留 NOT NULL 列插入失败）。"""
    t = (col_type or "").upper()
    if any(k in t for k in ("INT", "BOOL", "REAL", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE")):
        return 0
    return ""


async def _legacy_column_meta(db: AsyncSession, table: str) -> list[tuple[str, str, int, object]]:
    """读取表列元数据 [(列名, 类型, notnull(0/1), 默认值)]，兼容 SQLite / PostgreSQL。"""
    if settings.database_url.startswith("sqlite"):
        rows = (await db.execute(text(f"PRAGMA table_info({table})"))).fetchall()
        # PRAGMA table_info 列顺序: cid, name, type, notnull, dflt_value, pk
        return [(str(r[1]), str(r[2] or ""), int(r[3] or 0), r[4]) for r in rows]
    rows = (await db.execute(text(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name=:t"
    ), {"t": table})).fetchall()
    return [(str(r[0]), str(r[1] or ""), 0 if str(r[2]).upper() == "YES" else 1, r[3]) for r in rows]


async def _add_legacy_column_default(db: AsyncSession, table: str, column: str,
                                     col_type: str) -> bool:
    """给遗留 NOT NULL 列补 DEFAULT（DROP 不可用时的保守兜底）。

    - PostgreSQL：直接 ALTER COLUMN SET DEFAULT；
    - SQLite：不支持改列默认值，按官方协议重建表（建新表→复制数据→换名→重建索引）。
    返回是否成功。
    """
    value = _legacy_default_value(col_type)
    ddl_value = "'" + str(value) + "'" if isinstance(value, str) else str(value)

    if not settings.database_url.startswith("sqlite"):
        await db.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {ddl_value}"))
        await db.commit()
        return True

    import re

    rows = (await db.execute(text(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name=:n"
    ), {"n": table})).fetchall()
    if not rows or not rows[0][1]:
        return False
    create_sql = str(rows[0][1])

    # 仅在目标列定义末尾（逗号 / 右括号前）插入 DEFAULT，避免破坏其它列定义
    pattern = re.compile(
        r'(?is)(["`\[]?' + re.escape(column) + r'["`\]]?\s+[^,]*?)(\s*,\s*|\s*\))'
    )
    rewritten, n = pattern.subn(r'\1 DEFAULT ' + ddl_value + r'\2', create_sql, count=1)
    if n == 0:
        return False

    tmp = f"{table}__legacy_tmp"
    tmp_sql = re.sub(
        r'(?is)^\s*CREATE\s+TABLE\s+["`\[]?' + re.escape(table) + r'["`\]]?',
        f'CREATE TABLE "{tmp}"', rewritten, count=1,
    )
    # 保留原表索引定义（DROP TABLE 会连带删除，重建后需重放）
    index_rows = (await db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:n AND sql IS NOT NULL"
    ), {"n": table})).fetchall()

    await db.execute(text(tmp_sql))
    await db.execute(text(f'INSERT INTO "{tmp}" SELECT * FROM "{table}"'))
    await db.execute(text(f'DROP TABLE "{table}"'))
    await db.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "{table}"'))
    for (idx_sql,) in index_rows:
        try:
            await db.execute(text(str(idx_sql)))
        except Exception:  # 索引可能已随重建存在或与当前结构等价，忽略
            pass
    await db.commit()
    return True


async def _cleanup_legacy_notnull_columns(db: AsyncSession) -> int:
    """清理「模型已删除、库中仍为 NOT NULL 且无默认值」的遗留列。

    只遍历当前 ORM 已映射的表（`Base.metadata.tables`），已废弃的历史表
    （team_agents 等）不在此列也不会被误判。返回成功清理的列数。
    """
    from app.persistence.database import Base

    cleaned = 0
    for table, model_table in list(Base.metadata.tables.items()):
        try:
            if not await _table_exists(db, table):
                continue
            model_cols = set(model_table.columns.keys())
            for name, col_type, notnull, dflt in await _legacy_column_meta(db, table):
                if name in model_cols or not notnull or dflt not in (None, ""):
                    continue
                dropped = False
                try:
                    await db.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))
                    await db.commit()
                    dropped = True
                    cleaned += 1
                    logger.info("迁移: %s.%s 遗留 NOT NULL 列已删除", table, name)
                except Exception as e:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    logger.warning(
                        "迁移: %s.%s 遗留列删除失败(%s)，改用补默认值兜底", table, name, e,
                    )
                if dropped:
                    continue
                try:
                    if await _add_legacy_column_default(db, table, name, col_type):
                        cleaned += 1
                        logger.info("迁移: %s.%s 遗留列已补默认值", table, name)
                        continue
                except Exception as e:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    logger.warning("迁移: %s.%s 遗留列补值失败: %s", table, name, e)
                # 最后一道保险：登记给写入侧（ORM 可能忽略未映射列，仅作兜底）
                value = _legacy_default_value(col_type)
                _LEGACY_NOTNULL_DEFAULTS.setdefault(table, {})[name] = value
                logger.warning("迁移: %s.%s 遗留 NOT NULL 列未能自动修复，已登记写入侧兜底", table, name)
        except Exception as e:
            logger.warning("迁移: 表 %s 遗留列检查失败(非阻塞): %s", table, e)
            try:
                await db.rollback()
            except Exception:
                pass
    return cleaned


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
    # plan-278-1391: 清理模型已删除、库中仍为 NOT NULL 且无默认值的遗留列。
    # 根因：旧版 sessions.plan_restore_after_turn 为 NOT NULL 无默认值，新代码不带该列
    # 插入 → NOT NULL constraint failed（升级后新建会话/发消息必失败）。
    try:
        await _cleanup_legacy_notnull_columns(db)
    except Exception as e:
        logger.warning("遗留 NOT NULL 列清理失败(非阻塞): %s", e)
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

    # plan-248-1258 M2.1: 数据迁移 -- 供应商单 api_key 拆入首条凭据（幂等）
    try:
        await _migrate_provider_credentials(db)
    except Exception as e:
        logger.warning("供应商凭据迁移失败(非阻塞): %s", e)
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


async def _migrate_provider_credentials(db: AsyncSession) -> int:
    """plan-248-1258 M2.1: 幂等迁移——把 providers 的单 api_key 拆入 provider_credentials。

    规则：
    - 已有凭据的供应商跳过（不重复拆）；
    - 无 api_key 的供应商（如未登录的 workbuddy/ta3/trae）也建一条空凭据占位，
      以便 UI 与轮询逻辑有统一入口（api_key 为空、status=disabled）；
    - OAuth 类 auth 表旧行（credential_id 为空）挂到该供应商的首条凭据上。
    """
    from app.persistence.models.model_reg import Provider, ProviderCredential

    if not await _table_exists(db, "provider_credentials"):
        return 0

    providers = (await db.execute(select(Provider))).scalars().all()
    if not providers:
        return 0

    existing = (await db.execute(select(ProviderCredential.provider_id))).scalars().all()
    has_cred = set(existing)
    created = 0
    first_cred_by_provider: dict[int, int] = {}
    for p in providers:
        if p.id in has_cred:
            continue
        cred = ProviderCredential(
            provider_id=p.id,
            label="默认凭据",
            api_key=p.api_key,
            priority=0,
            is_active=True,
            status="ok" if p.api_key else "disabled",
        )
        db.add(cred)
        await db.flush()
        first_cred_by_provider[p.id] = cred.id
        created += 1
    if first_cred_by_provider:
        # 旧 OAuth auth 行挂到首条凭据（后续多账号登录会写各自的 credential_id）
        for table in ("workbuddy_auth", "ta3_auth", "trae_auth"):
            if not await _table_exists(db, table):
                continue
            for pid, cid in first_cred_by_provider.items():
                await db.execute(
                    text(
                        f"UPDATE {table} SET credential_id = :cid "
                        "WHERE provider_id = :pid AND (credential_id IS NULL)"
                    ),
                    {"cid": cid, "pid": pid},
                )
    await db.commit()
    if created:
        logger.info("迁移: 为 %d 个供应商创建首条凭据", created)
    return created
