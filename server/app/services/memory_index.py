"""记忆库 FTS5 索引层（plan-548-2276 阶段 2，第三级召回）。

解决什么：`memory_entries.text` 的中文子串检索只有 `LIKE '%词%'`，同义/近义表述
（记忆写「上下文压缩」，查询说「压缩后失忆」）搜不到。FTS5 trigram 提供子串级倒排
召回，作为 keywords / LIKE 之后的第三级。

三条设计取舍（都为了「索引坏掉也不能影响记忆本身」）：

1. **普通的 FTS5 表，不做 external content**：external content 表必须靠触发器同步，
   一旦触发器与实际表结构不一致，`INSERT INTO memory_entries` 会整体失败——记忆写入
   不能为检索层陪葬。这里索引是纯粹的可重建派生数据。
2. **不建触发器，改为查询前按指纹懒同步**：指纹分两级（plan-568-2333）——先用一条
   C 层聚合（行数 / 最大 id / 文本总长 / 关键词总长）判断「值不值得进一步校验」，
   轻量指标变了或距上次内容摘要校验超过 `_FULL_VERIFY_INTERVAL` 秒，才做索引源内容
   （id|text|keywords）的 sha1 摘要；摘要与上次一致则直接复用索引，不一致才全量重建。
   记忆表量级很小（几百到几千行），重建是毫秒级；换来的是写路径零耦合。
3. **探测与失败一律降级，不重试、不抛**：FTS5 只在 SQLite 且编译带 FTS5 时可用；
   PG 部署或旧版 SQLite 直接返回空结果，检索自动回退到 keywords + LIKE（质量降级，
   功能不失效）。

本模块不参与任何事务：连接独立、用完即关，调用方拿到的只是 id 列表。
"""
import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_TABLE = "memory_fts"
_META_TABLE = "memory_fts_meta"
# trigram 切分器对长度 <3 的查询恒为零命中，短词交给 LIKE 兜底
_MIN_TERM_LEN = 3
# 一次最多取回多少条 FTS 命中（融合排序在调用侧做）
_MAX_RESULTS = 200
# 探测结果缓存：build 支持与否在一个进程内不会变
_probe_lock = threading.Lock()
_probe_state: dict[str, bool | None] = {"build_ok": None}
# 索引同步锁：只包住 _sync 内部的「读指纹 → 比对 → DELETE/INSERT → commit」段。
# 为什么需要（plan-562-2318 M11b）：_query 跑在 asyncio.to_thread 里，并发检索时会有
# 两个线程同时重建索引，后者等 busy_timeout(2s) 超时抛 database is locked，被
# _query 的 except sqlite3.Error 吞掉 → 该次检索静默退回 keywords+LIKE。
# 与 _probe_lock 严格分开，且两者从不嵌套获取（_sync 不碰探测逻辑），不会死锁。
_sync_lock = threading.Lock()
# 内容摘要校验的时间窗口（秒，plan-568-2333）
#
# 为什么需要时间窗口：`memory_entries` 没有 `updated_at` 列（见 persistence/models/memory.py），
# 而 `update_memory`（memory_service.py）和写入时的合并都会**原地**改 `text`——等长替换
# （改一个字、同长度换词）在「行数 / 最大 id / 文本总长」这类聚合指标上完全不可见，
# 这正是 plan-562-2318 M11a 修过的漏检。因此轻量指标一致时不能永久信任它，
# 需要一个上限来保证「最迟多久做一次内容摘要校验」。
# 取值理由：陈旧的代价只落在第三级 FTS 召回上——keywords 与 `text ILIKE` 两级始终直读源表，
# 新写入/改动立刻可见，功能不会失效（只是这一路召回质量短暂降低）；而窗口越长，
# 全表摘要的次数越少。30 秒在「连续多轮检索」场景下已能省掉绝大多数全表摘要。
_FULL_VERIFY_INTERVAL = 30.0
# 上次做内容摘要校验的 monotonic 时刻（进程内；重启后为 0 → 首次查询必定校验一次）
_last_full_verify = 0.0


def _sqlite_path() -> str | None:
    """当前数据库对应的 sqlite 文件路径；非 SQLite 或内存库返回 None。"""
    url = str(getattr(settings, "database_url", "") or "")
    if not url.startswith("sqlite"):
        return None
    raw = url.rsplit("///", 1)[-1]
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        return None
    return str(Path(raw).resolve())


def _build_supports_fts5() -> bool:
    """SQLite 编译期是否带 FTS5 + trigram（结果在进程内永久缓存）。"""
    with _probe_lock:
        cached = _probe_state["build_ok"]
        if cached is not None:
            return bool(cached)
        ok = False
        try:
            probe = sqlite3.connect(":memory:")
            try:
                probe.execute("CREATE VIRTUAL TABLE _probe USING fts5(x, tokenize='trigram')")
                ok = True
            finally:
                probe.close()
        except sqlite3.Error as exc:
            logger.info(
                "[memory_index] 当前 SQLite 不支持 FTS5 trigram，记忆检索回退 LIKE: %s", exc
            )
        _probe_state["build_ok"] = ok
        return ok


def is_available() -> bool:
    """FTS5 是否可用于本次查询（构建能力 + 库文件存在）。"""
    path = _sqlite_path()
    if path is None:
        return False
    try:
        if not Path(path).exists():
            return False
    except OSError:
        return False
    return _build_supports_fts5()


def _connect(path: str) -> sqlite3.Connection:
    """短连接：索引读写不与其他 ORM 会话共享，避免持有连接影响写线程。"""
    conn = sqlite3.connect(path, timeout=2.0)
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE} "
        "USING fts5(text, keywords, tokenize='trigram')"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_META_TABLE} (key TEXT PRIMARY KEY, value TEXT)"
    )


def _fingerprint_light(conn: sqlite3.Connection) -> str:
    """轻量指纹：一条 C 层聚合，替代「整表读进 Python 逐行 sha1」（plan-568-2333）。

    此前每次 FTS 查询前都要把有效记忆的 id/text/keywords 全表取出并在 Python 里算摘要，
    记忆表增长后每次检索都在全表读 + 摘要。这里先用一条聚合查询回答「值不值得进一步校验」：
    行数 / 最大 id / 文本总长 / 关键词总长任一变化都说明源内容被写过，只有全都一致才跳过。

    为什么不用时间列：`memory_entries` 只有 `generated_at`（插入时刻，UPDATE 不会刷新），
    以及每次命中都会变的 `last_usage_at`——两者都不能作为「内容版本」。等长替换的漏检
    由 `_FULL_VERIFY_INTERVAL` 的时间窗口兜住（见该常量注释）。
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), "
        "COALESCE(SUM(LENGTH(COALESCE(text, ''))), 0), "
        "COALESCE(SUM(LENGTH(COALESCE(keywords, ''))), 0) "
        "FROM memory_entries WHERE superseded_by IS NULL"
    ).fetchone()
    if row is None:
        return "0|0|0|0"
    return "|".join(str(int(v or 0)) for v in row)


def _read_meta(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """读回上次保存的（轻量指纹, 内容摘要）。

    旧版本只存了一个纯 sha1 字符串，解析失败一律当作「没有指纹」——下一轮会走一次
    完整校验并改写为新格式，不影响正确性。
    """
    saved = conn.execute(
        f"SELECT value FROM {_META_TABLE} WHERE key = 'fingerprint'"
    ).fetchone()
    if not saved or not saved[0]:
        return None, None
    try:
        data = json.loads(str(saved[0]))
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    light = str(data.get("light") or "")
    full = str(data.get("full") or "")
    return (light or None, full or None)


def _save_meta(conn: sqlite3.Connection, light: str, full: str) -> None:
    """写入两级指纹（调用方负责 commit）。"""
    conn.execute(
        f"INSERT INTO {_META_TABLE}(key, value) VALUES('fingerprint', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"light": light, "full": full}, ensure_ascii=False),),
    )


def _fingerprint(conn: sqlite3.Connection) -> str:
    """索引源的指纹：与 `_sync` 的 INSERT ... SELECT 取同一投影，逐行摘要。

    plan-562-2318 M11a：此前的指纹是 SQL 聚合（行数, 最大 id, 文本总长, 关键词总长），
    对「等长文本替换」（长度不变、id 不变）完全无感 → 不重建索引 → FTS 仍按旧文本匹配，
    该命中的搜不到、不该命中的搜到。

    改用内容摘要后能覆盖：
    - 新增 / 删除有效记忆（行集合变化）；
    - 文本或关键词的**任何**字符改动（含等长替换、同长度换词）；
    - 行 id 变化（重建表 / 导入）。
    仍不能覆盖：
    - `memory_fts` 索引表自身被外部写坏（与源内容无关）——那种情况由 `_query` 的
      `except sqlite3.Error` 降级处理，下一轮内容变化时自然重建。

    不选「按 id 取 MAX(updated_at)」的原因：`memory_entries` 没有 updated_at 列（见
    `persistence/models/memory.py`）；`last_usage_at` 会在每次命中 bump 时变化，
    拿它当指纹会把「每次检索都重建索引」变成常态。

    代价：要把有效记忆的 id/text/keywords 读一遍（不再是纯聚合的 SQL），
    但表量级只有几百到几千条短文本，仍远低于一次重建的成本；返回值仅用于相等比较。
    plan-568-2333：本函数不再每次检索都跑——由 `_sync` 的两级判断（轻量指纹 → 本摘要，
    加 `_FULL_VERIFY_INTERVAL` 时间窗口）决定何时调用，见 `_sync` 的 docstring。
    """
    digest = hashlib.sha1()
    rows = conn.execute(
        "SELECT id, text, COALESCE(keywords, '') FROM memory_entries "
        "WHERE superseded_by IS NULL ORDER BY id"
    )
    for row in rows:
        digest.update(str(row[0]).encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(str(row[1] or "").encode("utf-8", "replace"))
        digest.update(b"\x1f")
        digest.update(str(row[2] or "").encode("utf-8", "replace"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _sync(conn: sqlite3.Connection) -> None:
    """按「轻量指纹 → 内容摘要」两级判断是否需要重建（幂等，可随时丢弃重来）。

    plan-562-2318 M11b：整段包在 `_sync_lock` 里，且在锁内重新判断：
    并发线程中先拿到锁的那个重建并提交，后到者拿到锁时指纹已一致，直接返回。
    锁只覆盖索引写段，调用方的 DB 查询不在锁内；不碰 _probe_lock，无嵌套。

    plan-568-2333 两级判断（目的：不再每次检索都全表读 + 逐行 sha1）：
      1. 轻量指纹（C 层聚合：行数 / 最大 id / 文本总长 / 关键词总长）与上次一致，
         且距上次内容摘要校验不足 `_FULL_VERIFY_INTERVAL` 秒 → 直接复用现有索引；
      2. 否则做一次内容摘要（`_fingerprint`）比对：一致则只刷新记录、不重建；
         不一致才 `DELETE` + `INSERT...SELECT` 全量重建。
    可见性：新增 / 删除记忆会改变行数或最大 id，下一轮查询即走第 2 级、FTS 立即能查到；
    原地等长改写在轻量指标上不可见，最迟 `_FULL_VERIFY_INTERVAL` 秒后由第 2 级发现。
    这段时间内 keywords 与 `text ILIKE` 两级（直读源表）仍然生效，不会查不到。
    """
    global _last_full_verify
    with _sync_lock:
        light = _fingerprint_light(conn)
        saved_light, saved_full = _read_meta(conn)
        now = time.monotonic()
        if saved_light is not None and saved_light == light:
            if (now - _last_full_verify) < _FULL_VERIFY_INTERVAL:
                # 轻量指标未变且仍在校验窗口内：内容摘要这一趟完全省掉
                return
        current = _fingerprint(conn)
        _last_full_verify = now
        if saved_full is not None and saved_full == current:
            # 内容确实没变（轻量指标的变化来自等长改写之外的情况，或仅是窗口到期）：
            # 只补记轻量指标，不重建
            if saved_light != light:
                _save_meta(conn, light, current)
                conn.commit()
            return
        conn.execute(f"DELETE FROM {_TABLE}")
        conn.execute(
            f"INSERT INTO {_TABLE}(rowid, text, keywords) "
            "SELECT id, text, COALESCE(keywords, '') FROM memory_entries "
            "WHERE superseded_by IS NULL"
        )
        _save_meta(conn, light, current)
        conn.commit()


def _build_match_query(terms: list[str]) -> str | None:
    """把候选词拼成 FTS5 MATCH 串：每个词加双引号做短语匹配，词间 OR。

    加引号是为了避免词里的 `-`、`.`、`+` 被当成 FTS 语法（`a-b` 会被解析成
    NOT 表达式），并让 trigram 按子串匹配而不是切词匹配。
    """
    parts: list[str] = []
    for term in terms:
        word = str(term or "").strip()
        if len(word) < _MIN_TERM_LEN:
            continue
        parts.append('"' + word.replace('"', '""') + '"')
    if not parts:
        return None
    return " OR ".join(parts)


def _query(path: str, terms: list[str], limit: int) -> list[int]:
    match = _build_match_query(terms)
    if match is None:
        return []
    try:
        conn = _connect(path)
    except sqlite3.Error as exc:
        logger.debug("[memory_index] 打开索引库失败: %s", exc)
        return []
    try:
        _ensure_schema(conn)
        _sync(conn)
        rows = conn.execute(
            f"SELECT rowid FROM {_TABLE} WHERE {_TABLE} MATCH ? "
            f"ORDER BY bm25({_TABLE}) LIMIT ?",
            (match, max(1, min(int(limit), _MAX_RESULTS))),
        ).fetchall()
        return [int(r[0]) for r in rows]
    except sqlite3.Error as exc:
        # 索引陈旧/损坏都只降召回质量：下一轮指纹不一致会自动重建
        logger.info("[memory_index] FTS 查询失败，已跳过该级召回: %s", exc)
        return []
    finally:
        conn.close()


async def search_ids(terms: list[str], limit: int = 20) -> list[int]:
    """返回与候选词相关的记忆 id（按 bm25 相关性降序）；不可用或失败返回空列表。

    调用方必须自己再按作用域过滤一遍 id（索引表不含 session/project 列），
    这里只负责「哪些 id 的文本相关」。
    """
    if not terms or limit <= 0 or not is_available():
        return []
    path = _sqlite_path()
    if path is None:
        return []
    try:
        return await asyncio.to_thread(_query, path, list(terms), int(limit))
    except Exception:  # noqa: BLE001
        logger.warning("[memory_index] FTS 召回异常，已降级为 keywords+LIKE", exc_info=True)
        return []


def reset_probe_cache() -> None:
    """清空探测缓存（测试用：模拟 FTS5 不可用或强制重新探测）。"""
    with _probe_lock:
        _probe_state["build_ok"] = None
