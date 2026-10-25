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
    # 侧边会话（子会话）所属主会话 id；旧库缺列时启动迁移补齐，NULL = 普通会话
    ("sessions", "side_parent_id", "BIGINT"),
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
    # plan-75-332：语义改为"执行模式"（readonly / plan / agent）。旧值 default /
    # accept_edits 在读取侧归一化为 agent，此处不强制回填，避免改动用户既有数据。
    ("sessions", "permission_mode", "VARCHAR(20) DEFAULT 'default' NOT NULL"),
    # ========== sessions（plan-75-332：权限模式，与执行模式正交）==========
    # ask 询问审批 / auto 自动审批 / full 完全访问
    ("sessions", "approval_mode", "VARCHAR(20) DEFAULT 'ask' NOT NULL"),
    # ========== sessions（v1.1：最后一次 API 真实上下文占用）==========
    ("sessions", "last_prompt_tokens", "INTEGER DEFAULT 0 NOT NULL"),
    ("sessions", "last_usage_at", "VARCHAR(40)"),
    # ========== sessions（v21：主会话上下文摘要持久化）==========
    ("sessions", "shared_context", "JSON"),
    # ========== sessions（v7：置顶时间——"后置顶在上"排序依据）==========
    ("sessions", "pinned_at", "VARCHAR(40)"),
    # plan-156-619 S8：标题来源（auto = AI 归纳 / manual = 用户手改，手改后不再被覆盖）
    ("sessions", "title_source", "VARCHAR(10) DEFAULT 'auto' NOT NULL"),
    # plan-156-620：挂在主会话下的会话种类（side 侧边对话 / sub 执行子会话），回填见 _backfill_side_kind
    ("sessions", "side_kind", "VARCHAR(8)"),
    # plan-20261018-operator-boost：会话级思考深度（操控子会话用最低档；空 = 跟随全局）
    ("sessions", "reasoning_effort", "VARCHAR(20)"),
    # plan-20261018-operator-boost：操作路线类型（desktop 桌面应用 / browser 网页）
    ("desktop_recipes", "scope", "VARCHAR(10) DEFAULT 'desktop' NOT NULL"),
    # ========== projects（plan-282-1441 #5：工作树复用 Project 表）==========
    ("projects", "is_worktree", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("projects", "parent_project_id", "BIGINT"),
    ("projects", "worktree_branch", "VARCHAR(200)"),
    # ========== projects（plan-20261009-ui U1：项目图标 / 颜色）==========
    ("projects", "icon", "VARCHAR(40)"),
    ("projects", "color", "VARCHAR(20)"),
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
    # plan-156-619 S6：清单步骤的执行者子会话（sessions.id）；NULL = 主代理自己执行
    ("tasks", "subsession_id", "BIGINT"),
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
    # ========== models（v26 kiro 模型：服务端 modelId 映射 JSON）==========
    ("models", "kiro_meta", "JSON"),
    # ========== team_agents（v1 遗留表，v2 不再使用，仅保留迁移以防旧表有数据）==========
    ("team_agents", "learned_facts", "JSON"),
    ("team_agents", "skill_ids", "JSON"),
    ("team_agents", "mcp_server_ids", "JSON"),
    # ========== rollback_writes（plan-88：二进制/超限文件只走 checkpoint 恢复）==========
    ("rollback_writes", "binary", "BOOLEAN DEFAULT 0 NOT NULL"),
    # plan-20261019-diff: 超限文本的 diff 展示窗口（全文仍走 checkpoint）
    ("rollback_writes", "diff_before", "TEXT"),
    ("rollback_writes", "diff_after", "TEXT"),
    # plan-89-387: 写盘记录关联工具调用键——同轮同文件多次编辑按 call_key 取本次变更
    ("rollback_writes", "call_key", "VARCHAR(64)"),
    # plan-20261019-toolcard-v2：写盘操作类型（create / modify / delete）与写后内容指纹（sha1）
    ("rollback_writes", "op", "VARCHAR(8)"),
    ("rollback_writes", "after_hash", "VARCHAR(40)"),
    # ========== rollback_writes（工作流写入归属：回滚与冲突基线，nullable）==========
    ("rollback_writes", "workflow_run_id", "BIGINT"),
    ("rollback_writes", "attempt_id", "BIGINT"),
    # ========== workflow_nodes（冲突闸门载荷：L4 三栏决定所需的基线 / 当前 / 待写入）==========
    ("workflow_nodes", "conflict_json", "JSON"),
    # ========== turns（plan-644：计划模式字段持久化，多轮迭代需求全集与卡片恢复的数据源）==========
    ("turns", "plan_doc_path", "VARCHAR(512)"),
    ("turns", "plan_status", "VARCHAR(20)"),
    # ========== sessions（目标模式：持久目标与续跑状态）==========
    ("sessions", "goal_text", "VARCHAR(2000)"),
    ("sessions", "goal_status", "VARCHAR(20) DEFAULT 'none' NOT NULL"),
    ("sessions", "goal_turns_used", "INTEGER DEFAULT 0 NOT NULL"),
    ("sessions", "goal_created_at", "VARCHAR(40)"),
    # ========== agents（子代理断点续跑：中断时保存的检查点 messages + 运行参数）==========
    ("agents", "checkpoint_json", "JSON"),
    # ========== agents（工作流运行时：子代理归属的工作流节点，nullable）==========
    ("agents", "workflow_node_id", "BIGINT"),
    ("workflow_tool_calls", "effect_json", "TEXT"),
    ("workflow_runs", "closed_reason", "VARCHAR(32)"),
    # ========== usage_records（plan-152-704：供应商显示名）==========
    ("usage_records", "provider_name", "VARCHAR(120) DEFAULT ''"),
    # plan-20261021-usage-update：调用性能（首字 / 生成耗时）与账号快照
    ("usage_records", "first_token_ms", "INTEGER"),
    ("usage_records", "gen_ms", "INTEGER"),
    ("usage_records", "credential_id", "INTEGER"),
    ("usage_records", "credential_label", "VARCHAR(120) DEFAULT ''"),
    ("usage_records", "credential_hint", "VARCHAR(40) DEFAULT ''"),
    # ========== scheduled_tasks（plan-230-1144 M1.1：调度器落地所需的执行状态列）==========
    ("scheduled_tasks", "enabled", "BOOLEAN DEFAULT 1 NOT NULL"),
    ("scheduled_tasks", "last_run_at", "VARCHAR(40)"),
    ("scheduled_tasks", "next_run_at", "VARCHAR(40)"),
    ("scheduled_tasks", "last_status", "VARCHAR(16)"),
    ("scheduled_tasks", "last_error", "VARCHAR(300)"),
    ("scheduled_tasks", "missed_policy", "VARCHAR(12) DEFAULT 'skip'"),
    # ========== hook_configs（S12 / plan-41-197：提示词注入型钩子）==========
    ("hook_configs", "hook_type", "VARCHAR(20) DEFAULT 'command' NOT NULL"),
    ("hook_configs", "prompt", "VARCHAR(2000)"),
    # ========== memory_entries（plan-230-1144 M4.1：记忆三层化）==========
    ("memory_entries", "scope", "VARCHAR(12) DEFAULT 'session'"),
    ("memory_entries", "project_id", "BIGINT"),
    ("memory_entries", "candidate", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("memory_entries", "expires_at", "VARCHAR(40)"),
    ("memory_entries", "superseded_by", "BIGINT"),
    # ========== memory_entries（plan-548-2276：检索键与来源）==========
    # keywords 存 JSON 数组字符串（如 ["上下文压缩","compaction"]），旧行为 NULL；
    # origin 区分写入通道 manual / extract / compaction，旧行按 manual 处理。
    # keywords 允许 NULL（旧行没这列）；origin 与模型侧口径一致为 NOT NULL DEFAULT 'manual'——
    # SQLite 的 ADD COLUMN 只要求默认值是常量，带 NOT NULL 也能补列、不重建表。
    # plan-156-621 复盘修复：按旧 DDL（可空）加过列的库，由 _memory_origin_not_null 收紧。
    ("memory_entries", "keywords", "TEXT"),
    ("memory_entries", "origin", "VARCHAR(16) DEFAULT 'manual' NOT NULL"),
    # ========== providers（plan-248-1258 M2.3：供应商级代理）==========
    ("providers", "proxy_mode", "VARCHAR(12) DEFAULT 'inherit'"),
    ("providers", "proxy_url", "VARCHAR(255)"),
    # ========== providers（plan-271-1364：凭据取用策略）==========
    # sticky=粘性优先（上次成功的先用）| round_robin=严格按 priority 轮转
    ("providers", "credential_strategy", "VARCHAR(16) DEFAULT 'sticky'"),
    # ========== providers（plan-41-225：供应商手动排序位）==========
    # 用户在模型页左列拖拽调整顺序；0 = 未排序，由 id 兜底保证顺序稳定。
    ("providers", "sort_order", "INTEGER DEFAULT 0"),
    # ========== workbuddy_auth / ta3_auth / trae_auth（plan-248-1258 M2.2：凭据维度）==========
    # 多账号支持：auth 行归属某条 provider_credentials（旧行迁移时挂到首条凭据）。
    ("workbuddy_auth", "credential_id", "BIGINT"),
    ("ta3_auth", "credential_id", "BIGINT"),
    ("trae_auth", "credential_id", "BIGINT"),
    # ========== provider_credentials（plan-290：连续失败计数，达到阈值才冷却）==========
    ("provider_credentials", "fail_count", "INTEGER DEFAULT 0 NOT NULL"),
    # ========== subagent_profiles（plan-330-1648 M2：子代理思考深度档位覆盖）==========
    ("subagent_profiles", "reasoning_effort", "VARCHAR(20)"),
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


_MEMORY_TABLE = "memory_entries"
_MEMORY_TMP_TABLE = "memory_entries__nullable_tmp"


def _retarget_index_sql(sql: str, source_table: str) -> str:
    """把索引 DDL 里的表名换回主表（从临时表上取到的 DDL 指向旧表名）。"""
    import re

    return re.sub(
        rf'(?is)(\bON\s+)["`\[]?{re.escape(source_table)}["`\]]?',
        '\\1"memory_entries"', sql, count=1,
    )


async def _replay_memory_indexes(
    db: AsyncSession, index_rows, *, source_table: str | None = None,
) -> None:
    """重放 memory_entries 的索引 DDL。

    索引可能已随建表/换名自动重建、与当前结构等价，单条失败一律忽略；
    `source_table` 非空时把 DDL 里的表名换回主表（恢复分支从临时表上取 DDL）。
    """
    for (idx_sql,) in index_rows:
        stmt = str(idx_sql)
        if source_table:
            stmt = _retarget_index_sql(stmt, source_table)
        try:
            await db.execute(text(stmt))
        except Exception:  # noqa: BLE001 — 索引重放不是数据完整性的一环
            pass


async def _restore_memory_entries_from_tmp(db: AsyncSession) -> bool:
    """plan-549-2281：从「重建中断」的半态恢复 memory_entries。

    `_memory_session_nullable` 的 SQLite 重建路径是「建临时表 → 拷贝 → DROP 主表 → RENAME」
    四步。若在 DROP 之后、RENAME 之前崩溃（进程被杀 / 断电），主表已不存在而临时表持有
    全部数据；旧实现在 `_table_exists` 上直接 return False，临时表既不恢复也不清理——
    用户的记忆库等于静默丢掉，而且下一轮启动仍然读不到。

    这里在存在性判定之前先把临时表接回主表并重放它的索引。幂等：主表存在或临时表
    不存在时什么都不做（返回 False）。
    """
    if not settings.database_url.startswith("sqlite"):
        return False
    try:
        if await _table_exists(db, _MEMORY_TABLE):
            return False  # 主表在，无需恢复（也避免盖掉正常数据）
        if not await _table_exists(db, _MEMORY_TMP_TABLE):
            return False
        index_rows = (await db.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:t AND sql IS NOT NULL"
        ), {"t": _MEMORY_TMP_TABLE})).fetchall()
        await db.execute(text(f'ALTER TABLE "{_MEMORY_TMP_TABLE}" RENAME TO "{_MEMORY_TABLE}"'))
        await db.commit()
        await _replay_memory_indexes(db, index_rows, source_table=_MEMORY_TMP_TABLE)
        await db.commit()
    except Exception as e:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning("迁移: memory_entries 半态恢复失败(非阻塞): %s", e)
        return False
    logger.warning(
        "迁移: 检出记忆表重建中断的半态——已将临时表 %s 恢复为 %s 并重放索引（%d 条索引 DDL）",
        _MEMORY_TMP_TABLE, _MEMORY_TABLE, len(index_rows),
    )
    return True


async def _memory_session_nullable(db: AsyncSession) -> bool:
    """plan-537-2256：去掉 memory_entries.session_id 的 NOT NULL（幂等）。

    记忆的 session_id 只是「创建来源」，project/global 记忆是跨会话资产——
    来源会话被永久删除时只应解除关联（外键 ON DELETE SET NULL），行本身必须保留。
    SET NULL 的前提是列可空 + 外键带 ON DELETE 动作，而旧库两者都没有：
    旧 DDL 是 `session_id BIGINT NOT NULL` + 表级 `FOREIGN KEY(...) REFERENCES sessions (id)`
    （RESTRICT），直接删会话会因跨层记忆仍引用而被拒。SQLite 不支持事后改列/改外键，
    故按「建新表 → 复制数据 → 换名 → 重放索引」重建，并在改写 DDL 时同时
    去掉 NOT NULL、补上 ON DELETE SET NULL。非 SQLite 走 ALTER + 重建约束，失败只记 warning。
    """
    import re

    # plan-549-2281：先尝试从「重建中断」的半态恢复（见 _restore_memory_entries_from_tmp）。
    # 必须在 _table_exists 判定之前：半态下主表已不存在，旧实现在这里直接 return False，
    # 临时表里的全部记忆既不恢复也不清理。
    await _restore_memory_entries_from_tmp(db)

    if not await _table_exists(db, "memory_entries"):
        return False
    meta = await _legacy_column_meta(db, "memory_entries")
    target = next((m for m in meta if m[0] == "session_id"), None)
    if target is None or not target[2]:
        return False  # 列不存在或已可空：什么也不做（幂等）

    if not settings.database_url.startswith("sqlite"):
        try:
            await db.execute(text(
                "ALTER TABLE memory_entries ALTER COLUMN session_id DROP NOT NULL"))
            await db.commit()
            logger.info("迁移: memory_entries.session_id 已改为可空")
        except Exception as e:
            logger.warning("迁移: memory_entries.session_id 改可空失败(非阻塞): %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            return False
        # 外键若仍是 RESTRICT，删会话会被跨层记忆挡住——尽力改成 SET NULL
        try:
            names = [r[0] for r in (await db.execute(text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_name='memory_entries' AND constraint_type='FOREIGN KEY'"
            ))).fetchall()]
            for name in names:
                cols = [r[0] for r in (await db.execute(text(
                    "SELECT column_name FROM information_schema.key_column_usage "
                    "WHERE table_name='memory_entries' AND constraint_name=:n"
                ), {"n": name})).fetchall()]
                if cols != ["session_id"]:
                    continue
                await db.execute(text(f'ALTER TABLE memory_entries DROP CONSTRAINT "{name}"'))
                await db.execute(text(
                    f'ALTER TABLE memory_entries ADD CONSTRAINT "{name}" '
                    "FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE SET NULL"))
            await db.commit()
        except Exception as e:
            logger.warning("迁移: memory_entries 外键改 SET NULL 失败(非阻塞): %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
        return True

    rows = (await db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_entries'"
    ))).fetchall()
    create_sql = str(rows[0][0] or "") if rows else ""
    if not create_sql:
        return False

    # 1) session_id 列定义：去 NOT NULL；若外键写在该列内（REFERENCES）则补 ON DELETE SET NULL
    pattern = re.compile(r'(?is)(["`\[]?session_id["`\]]?\s+)([^,)]*)')

    def _fix_column(m: "re.Match") -> str:
        body = re.sub(r"(?is)\s+NOT\s+NULL", "", m.group(2))
        if "references" in body.lower() and "on delete" not in body.lower():
            body = re.sub(
                r"(?is)(references\s+[\"`\[]?sessions[\"`\]]?\s*\([^)]*\))",
                r"\1 ON DELETE SET NULL", body, count=1,
            )
        return m.group(1) + body

    rewritten, n = pattern.subn(_fix_column, create_sql, count=1)
    if n == 0:
        logger.warning("迁移: 未能定位 memory_entries.session_id 列定义，跳过改可空")
        return False

    # 2) 表级外键约束：FOREIGN KEY(session_id) REFERENCES sessions (id) → 补 ON DELETE SET NULL
    fk_pattern = re.compile(
        r'(?is)(FOREIGN\s+KEY\s*\(\s*["`\[]?session_id["`\]]?\s*\)\s*'
        r'REFERENCES\s+["`\[]?sessions["`\]]?\s*\([^)]*\))(\s+ON\s+DELETE\s+\w+)?'
    )

    def _fix_fk(m: "re.Match") -> str:
        return m.group(1) + " ON DELETE SET NULL"

    rewritten = fk_pattern.sub(_fix_fk, rewritten, count=1)

    before = int((await db.execute(text("SELECT count(*) FROM memory_entries"))).scalar() or 0)
    tmp = _MEMORY_TMP_TABLE
    tmp_sql = re.sub(
        r'(?is)^\s*CREATE\s+TABLE\s+["`\[]?memory_entries["`\]]?',
        f'CREATE TABLE "{tmp}"', rewritten, count=1,
    )
    # DROP TABLE 会连带删索引，重建后重放
    index_rows = (await db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='memory_entries' AND sql IS NOT NULL"
    ))).fetchall()
    try:
        await db.execute(text(f'DROP TABLE IF EXISTS "{tmp}"'))
        await db.execute(text(tmp_sql))
        await db.execute(text(f'INSERT INTO "{tmp}" SELECT * FROM "memory_entries"'))
        after = int((await db.execute(text(f'SELECT count(*) FROM "{tmp}"'))).scalar() or 0)
        if after != before:
            # 绝不静默丢行：回滚后抛出，由调用方显式报错
            raise RuntimeError(f"记忆表重建前后行数不一致：{before} → {after}")
        await db.execute(text('DROP TABLE "memory_entries"'))
        await db.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "memory_entries"'))
        await _replay_memory_indexes(db, index_rows)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise
    logger.info("迁移: memory_entries 已重建，session_id 允许为空（%d 行）", before)
    return True


async def _memory_origin_not_null(db: AsyncSession) -> bool:
    """plan-156-621 复盘修复：把 memory_entries.origin 收紧为 NOT NULL DEFAULT 'manual'（幂等）。

    模型侧 `origin: Mapped[str]` 一直是隐式 NOT NULL，而迁移只加过可空的
    `VARCHAR(16) DEFAULT 'manual'`：新建库由 create_all 建成 NOT NULL，升级库却是可空，
    两份 schema 不一致，不经 ORM 的直插会落下 NULL（读取侧靠 `or "manual"` 兜底才没暴露）。

    顺序固定为「先回填、后收紧」，绝不丢行：NULL 行一律回填成 'manual'，再做约束检查。
    SQLite 不支持 ALTER COLUMN SET NOT NULL，按 `_memory_session_nullable` 同一套四步协议
    重建（建临时表 → 拷数据 → 行数校验 → DROP + RENAME → 重放索引）；行数不一致立即回滚
    并抛出，由调用方显式报错。列不存在、或已是 NOT NULL 时什么都不做。
    """
    import re

    # 上一次重建若在 DROP 之后中断，先把临时表接回主表（与 session_id 改可空共用同一自救逻辑）
    await _restore_memory_entries_from_tmp(db)

    if not await _table_exists(db, _MEMORY_TABLE):
        return False
    meta = await _legacy_column_meta(db, _MEMORY_TABLE)
    target = next((m for m in meta if m[0] == "origin"), None)
    if target is None or target[2]:
        # 列不存在（由 _MIGRATIONS 补，补的时候已带 NOT NULL）或已收紧：幂等返回
        return False

    if not settings.database_url.startswith("sqlite"):
        try:
            await db.execute(text(
                "UPDATE memory_entries SET origin = 'manual' WHERE origin IS NULL"))
            await db.execute(text(
                "ALTER TABLE memory_entries ALTER COLUMN origin SET DEFAULT 'manual'"))
            await db.execute(text(
                "ALTER TABLE memory_entries ALTER COLUMN origin SET NOT NULL"))
            await db.commit()
            logger.info("迁移: memory_entries.origin 已收紧为 NOT NULL DEFAULT 'manual'")
        except Exception as e:
            logger.warning("迁移: memory_entries.origin 收紧失败(非阻塞): %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            return False
        return True

    rows = (await db.execute(text(
        f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{_MEMORY_TABLE}'"
    ))).fetchall()
    create_sql = str(rows[0][0] or "") if rows else ""
    if not create_sql:
        return False

    # 1) 定位 origin 列定义并补 NOT NULL（缺 DEFAULT 时一并补上）。
    # 模式里用 (?:\([^)]*\)|[^,()])* 才能整体跳过 VARCHAR(16) 这类带括号的类型，
    # 既有的 session_id 改写只用 [^,)]* 是因为它是无括号的 BIGINT。
    pattern = re.compile(r'(?is)(["`\[]?origin["`\]]?\s+)((?:\([^)]*\)|[^,()])*)')

    def _fix_column(m: "re.Match") -> str:
        body = m.group(2)
        if not re.search(r"(?is)\bdefault\b", body):
            body = f"{body} DEFAULT 'manual'"
        if not re.search(r"(?is)\bnot\s+null\b", body):
            body = f"{body} NOT NULL"
        return m.group(1) + body

    rewritten, n = pattern.subn(_fix_column, create_sql, count=1)
    if n == 0:
        logger.warning("迁移: 未能定位 memory_entries.origin 列定义，跳过收紧约束")
        return False

    before = int((await db.execute(text(f"SELECT count(*) FROM {_MEMORY_TABLE}"))).scalar() or 0)
    tmp = _MEMORY_TMP_TABLE
    tmp_sql = re.sub(
        rf'(?is)^\s*CREATE\s+TABLE\s+["`\[]?{_MEMORY_TABLE}["`\]]?',
        f'CREATE TABLE "{tmp}"', rewritten, count=1,
    )
    # DROP TABLE 会连带删索引，重建后重放
    index_rows = (await db.execute(text(
        f"SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='{_MEMORY_TABLE}' "
        "AND sql IS NOT NULL"
    ))).fetchall()
    try:
        await db.execute(text(f'DROP TABLE IF EXISTS "{tmp}"'))
        await db.execute(text(
            f"UPDATE {_MEMORY_TABLE} SET origin = 'manual' WHERE origin IS NULL"))
        await db.execute(text(tmp_sql))
        await db.execute(text(f'INSERT INTO "{tmp}" SELECT * FROM "{_MEMORY_TABLE}"'))
        after = int((await db.execute(text(f'SELECT count(*) FROM "{tmp}"'))).scalar() or 0)
        if after != before:
            # 绝不静默丢行：回滚后抛出，由调用方显式报错
            raise RuntimeError(f"记忆表重建前后行数不一致：{before} → {after}")
        await db.execute(text(f'DROP TABLE "{_MEMORY_TABLE}"'))
        await db.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "{_MEMORY_TABLE}"'))
        await _replay_memory_indexes(db, index_rows)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise
    logger.info("迁移: memory_entries.origin 已收紧为 NOT NULL DEFAULT 'manual'（%d 行）", before)
    return True


# (索引名, 表名, 列)。create_all 不会给已存在的表补索引，这里幂等补建。
_INDEXES: list[tuple[str, str, str]] = [
    # plan-20261021-usage-update：调用记录表按时间倒序分页，数万行时没有索引会每页全表排序
    ("ix_usage_records_created_at", "usage_records", "created_at"),
]


async def _ensure_indexes(db: AsyncSession) -> None:
    for name, table, columns in _INDEXES:
        try:
            if not await _table_exists(db, table):
                continue
            await db.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))
            await db.commit()
        except Exception as e:
            logger.warning("迁移: 索引 %s 创建失败(非阻塞): %s", name, e)
            try:
                await db.rollback()
            except Exception:
                pass


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
    await _ensure_indexes(db)

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

    # plan-537-2256: memory_entries.session_id 改为可空（外键 SET NULL 的前提）。
    try:
        await _memory_session_nullable(db)
    except Exception as e:
        # 行数校验不通过时 helper 已回滚（不丢行）；这里必须显式报错，不能悄悄放过
        logger.error("记忆表 session_id 改可空失败，记忆写入可能仍受 NOT NULL 限制: %s", e,
                     exc_info=True)
        # plan-562-2318 H5：这条迁移失败的影响不只是「记忆写入」——旧库若仍是
        # 「session_id NOT NULL + RESTRICT」，删除会话 / 删除项目会撞外键约束失败，
        # 用户看到的是「点了永久删除但删不掉」。补一条**用户可读**的启动告警：
        # 没有现成的对外诊断通道承载它，走 warning 级日志（控制台 + server.log 均可见）。
        # 删除侧已改为显式 UPDATE 解绑（见 session_service.purge_session_children），
        # 该告警只提示"可能"受影响，不代表删除一定失败。
        logger.warning(
            "启动警告：记忆表（memory_entries）的会话关联可能仍受 NOT NULL / RESTRICT 限制，"
            "删除会话或删除项目可能失败。可重启应用让迁移重试，或备份数据后重建记忆表。"
        )
        try:
            await db.rollback()
        except Exception:
            pass

    # plan-156-621 复盘修复: memory_entries.origin 收紧为 NOT NULL DEFAULT 'manual'（幂等）。
    # 旧 DDL 只加过可空列，与模型侧隐式 NOT NULL 不一致；按旧 DDL 建过表的库在这里对齐。
    # 放在 _memory_session_nullable 之后：那张表可能刚被重建过，此处再读到的就是最新 DDL。
    try:
        await _memory_origin_not_null(db)
    except Exception as e:
        # 非阻塞：origin 读取侧本就有 `or "manual"` 兜底，收紧失败不影响记忆读写，
        # 但必须留下可诊断的日志（重启会重试）。
        logger.warning("memory_entries.origin 收紧为 NOT NULL 失败(非阻塞，重启重试): %s", e,
                       exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass

    # plan-156-620: 数据迁移 -- 回填子会话种类（幂等，只处理 side_kind 为空的行）
    try:
        await _backfill_side_kind(db)
    except Exception as e:
        # plan-156-621：回填失败不阻塞启动，但必须让人看得见——回填不到位的存量会话
        # 会被 list_children / _require_subsession 直接当成「不存在」，用户无法操作。
        logger.error("子会话种类回填失败：存量执行子会话将无法操作，重启或手动修复后重试: %s", e,
                     exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass

    # plan-156-624: 数据迁移 -- 旧执行模式名回填为 agent（幂等，只改 default/accept_edits）
    try:
        await _backfill_legacy_permission_mode(db)
    except Exception as e:
        logger.warning("执行模式回填失败(非阻塞，读取侧仍会归一化): %s", e)
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


# 侧边对话创建时的固定占位标题（sessions.create_side_session），回填据此识别存量侧边对话
_SIDE_SESSION_PLACEHOLDER_TITLE = "侧边会话"


async def _backfill_side_kind(db: AsyncSession) -> int:
    """plan-156-620：为存量挂靠会话补 side_kind。

    字段上线前两类会话无法区分；侧边对话的标题恒为固定占位（标 named 不参与归纳），
    执行子会话的标题是任务名——按此判定，其余兜底为侧边对话（宁可留在右侧，也不误入左栏）。

    plan-156-621：用户重命名过的侧边对话也会脱离占位标题（rename 端点置 title_source='manual'），
    只按标题判定会把它误判成执行子会话，从右侧面板消失却出现在左栏。因此 manual 一律兜底为 side。
    """
    from app.core.enums import SideKind

    if not await _column_exists(db, "sessions", "side_kind"):
        return 0
    res_sub = await db.execute(text(
        "UPDATE sessions SET side_kind = :sub "
        "WHERE side_parent_id IS NOT NULL AND side_kind IS NULL "
        "AND title IS NOT NULL AND title <> :placeholder "
        "AND COALESCE(title_source, 'auto') <> 'manual'"
    ), {"sub": SideKind.SUB.value, "placeholder": _SIDE_SESSION_PLACEHOLDER_TITLE})
    res_side = await db.execute(text(
        "UPDATE sessions SET side_kind = :side "
        "WHERE side_parent_id IS NOT NULL AND side_kind IS NULL"
    ), {"side": SideKind.SIDE.value})
    await db.commit()
    as_sub = int(res_sub.rowcount or 0)
    as_side = int(res_side.rowcount or 0)
    if as_sub or as_side:
        logger.info("子会话种类回填完成：sub=%s side=%s", as_sub, as_side)
    return as_sub + as_side


async def _backfill_legacy_permission_mode(db: AsyncSession) -> int:
    """plan-156-624：把历史上保存的旧执行模式名回填为 agent。

    旧名 default / accept_edits 与 agent 能力完全相同（读取侧早已归一化），但库里留着
    旧值会让「子会话工具可用的模式」在统计与设置页上口径不一致，升级用户看起来像
    "还需要手动改模式"。只迁移这两个旧名，用户显式选择的 readonly / plan / workflow 不动。
    """
    if not await _column_exists(db, "sessions", "permission_mode"):
        return 0
    res = await db.execute(text(
        "UPDATE sessions SET permission_mode = 'agent' "
        "WHERE permission_mode IN ('default', 'accept_edits')"
    ))
    await db.commit()
    count = int(res.rowcount or 0)
    if count:
        logger.info("执行模式回填完成：%s 个会话由旧名迁到 agent", count)
    return count


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
        for table in ("workbuddy_auth", "ta3_auth", "trae_auth", "kiro_auth"):
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
