"""记忆管线服务（D8：写入/使用/整合）。

plan-230-1144 M4.1 三层化改造：
- **作用域**：global（跨项目规范/偏好）/ project（项目约定）/ session（会话事实），
  读取时按 session → project → global 合并注入，替代原先的扁平 top-N；
- **候选区**：低置信记忆（importance < 0.65）不再直接丢弃，降级为 candidate
  保存——不注入 prompt，但可被 memory_search 检索，避免高价值低置信信息永久丢失；
- **提升**：promote_memory 支持把会话记忆升级为 project/global 记忆；
- **去重**：从逐条 select 全表比对（O(n²)）改为一次性载入内存集合（O(n)）；
- **老化**：expires_at 到期的记忆读取时跳过。
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.memory import MemoryEntry

logger = logging.getLogger(__name__)

# 注入预算（plan-537-2256）：三层各自限量，合计最多 12 条。
# 早前三层都用同一个 top_n，全局规范很容易刷满上限、把更具体的会话事实挤掉。
_SESSION_LIMIT = 5
_PROJECT_LIMIT = 4
_GLOBAL_LIMIT = 3
_KINDS = ("fact", "convention", "pitfall", "decision")
_SCOPES = ("session", "project", "global")
# 记忆来源（plan-548-2276）：manual 模型主动写 / extract turn 结束后抽取 / compaction 压缩归档
_ORIGINS = ("manual", "extract", "compaction")
# 兜底派生关键词的最大个数：再多只会放大「撞词」噪声，反而稀释相关性排序
_MAX_KEYWORDS = 6
# candidate 门槛：低于该 importance 的记忆降级为候选（不丢弃）。
# plan-537-2256：提取提示词要求 importance 取 0.4–1.0，低于 0.65 落候选区。
_CANDIDATE_THRESHOLD = 0.65
# 会话记忆默认存活时间（天）；project/global 不设过期
_SESSION_TTL_DAYS = 30
# 有关键词时单层候选池的放宽倍数与下限（plan-568-2333）。
# 为什么需要：`load_memories` 的排序键 `(命中词数, usage_count, generated_at)` 在 Python
# 侧计算，SQL 层若先按 usage_count 砍到 `limit` 条，高相关但低频/较早的记忆根本进不了
# 候选集；但完全不 limit 又会让单轮上下文重建的载入量随记忆总量无界增长（global 层最明显，
# 每轮重建都可能触发）。
# 取值理由：放宽到 `limit × 10` 且不低于 200 条——默认预算 5/4/3 时三层各 200 条，
# 单轮最多 600 行，与记忆总量无关；同时仍比注入条数大两个数量级，足够让「相关但低频」
# 的记忆进入候选池，`_merge` 的最终截断口径保持不变。
_TASK_CANDIDATE_FACTOR = 10
_TASK_CANDIDATE_MIN = 200


def _candidate_cap(limit: int) -> int:
    """有关键词时某一层的 SQL 候选上限（plan-568-2333）：放宽但不随总量无界。"""
    return max(int(limit) * _TASK_CANDIDATE_FACTOR, _TASK_CANDIDATE_MIN)


def _normalize_text(text: str) -> str:
    """去重键归一化：大小写折叠 + 空白折叠。调用方必须传入已截断的文本。"""
    return " ".join(str(text or "").casefold().split())


def _normalize_kind(raw) -> str:
    kind = str(raw or "fact")
    return kind if kind in _KINDS else "fact"


def _normalize_scope(raw) -> str:
    scope = str(raw or "session")
    return scope if scope in _SCOPES else "session"


def _normalize_origin(raw) -> str:
    origin = str(raw or "manual").strip().lower()
    return origin if origin in _ORIGINS else "manual"


def _clean_keyword(raw) -> str:
    """单个关键词的清洗：去首尾空白并剔除 `"`。

    plan-549-2281 复盘修复：keywords 落库为 JSON 数组字符串，检索侧的元素级匹配
    （`%"term"%` LIKE）以 `"` 为分隔符——关键词里含引号会让该词永远匹配不上，
    写入侧直接剔掉（引号本身也没有检索价值）。
    """
    return str(raw or "").strip().replace('"', "").strip()


def _parse_keywords(raw) -> list[str]:
    """keywords 列（JSON 数组字符串）→ list[str]；任何坏数据都按空处理，绝不抛。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [k for k in (_clean_keyword(x) for x in raw) if k]
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [k for k in (_clean_keyword(x) for x in data) if k]


def _dedupe_keywords(items) -> list[str]:
    """关键词去重保序（按大小写折叠比较，保留首次出现的写法）。

    plan-549-2281 复盘修复：补上限截断——抽取路径的模型输出不设限时，存多少算多少，
    与 memory_write（限 8）/ 压缩归档（限 8）口径不一致；统一截到 _MAX_KEYWORDS*2。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        word = str(item or "").strip()
        if not word:
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(word)
        if len(out) >= _MAX_KEYWORDS * 2:
            break
    return out


def _derive_keywords(text: str, limit: int = _MAX_KEYWORDS) -> list[str]:
    """从正文兜底派生关键词（产出记忆的一方没给 keywords 时使用）。

    口径：按标点/空白切分 → 去中英文停用词与长度 <2 的碎片 → 取前 limit 个。
    先只要「整块词」（中文滑窗碎片是检索用的，当关键词太碎）；整块词全被过滤掉时
    才退回带滑窗的切分，最终仍为空则退化为截断原文——**保证非空**：注入侧的相关性
    打分与检索侧的一级召回都依赖这个字段。
    """
    from app.services.memory_synonyms import tokenize

    words = tokenize(text, with_windows=False)[:limit]
    if not words:
        words = tokenize(text)[:limit]
    if words:
        return words
    fallback = " ".join(str(text or "").split())[:12].strip()
    return [fallback] if fallback else []


def parse_keywords(raw) -> list[str]:
    """公开包装：解析 keywords 列（检索侧渲染用；plan-549-2281 前检索方直接 import
    私有 _parse_keywords，跨模块耦合私有 API，收敛为公开入口）。"""
    return _parse_keywords(raw)


def entry_keywords(entry: MemoryEntry) -> set[str]:
    """条目的关键词集合（小写化）；旧数据 keywords 为空时按正文兜底派生。"""
    words = _parse_keywords(getattr(entry, "keywords", None))
    if not words:
        words = _derive_keywords(entry.text or "")
    return {str(w).casefold() for w in words if str(w).strip()}


def _texts_close(a: str, b: str, tolerance: float = 0.5) -> bool:
    """两条文本长度是否接近（去重粗筛）。

    刻意不用编辑距离：这里只需挡掉「长度差一倍以上」的明显不同条目，真正的语义判定
    交给关键词交集（≥2）承担，避免每次写入都跑 O(n·m) 字符串比对。
    """
    longest = max(len(a or ""), len(b or ""))
    if longest == 0:
        return False
    return abs(len(a or "") - len(b or "")) / longest <= tolerance


def _load_existing(s, conditions) -> list[dict]:
    """载入同作用域内仍有效的既有记忆，供去重匹配。

    排除 `superseded_by` 非空的行：把它们纳入合并会让新内容并进「已失效」的行，
    等于刚写进去的记忆立即消失。
    """
    rows = s.execute(
        select(MemoryEntry).where(*conditions, MemoryEntry.superseded_by.is_(None))
    ).scalars().all()
    existing: list[dict] = []
    for entry in rows:
        words = (_parse_keywords(getattr(entry, "keywords", None))
                 or _derive_keywords(entry.text or ""))
        existing.append({
            "entry": entry,
            "norm": _normalize_text(entry.text or ""),
            "keywords": {str(w).casefold() for w in words},
            "text": entry.text or "",
        })
    return existing


def _find_duplicate(bucket: list[dict], item: dict) -> dict | None:
    """同作用域内判定「同一条记忆」：归一化文本相同，或关键词高度重合且文本长度接近。

    plan-549-2281 复盘修复：旧口径「交集 ≥2 即合并」在关键词大量来自自动派生时
    （同主题、不同结论的两条记忆很容易共享两个派生词），会把语义不同的条目静默
    合并——原文本被更长的一条覆盖，不可逆。收紧为三条同时满足：
    ① 交集 ≥2；② 交集覆盖较小关键词集的一半以上（挡掉「同主题不同事」）；
    ③ 文本长度接近（_texts_close）。
    """
    keywords = {str(w).casefold() for w in item["keywords"]}
    for candidate in bucket:
        if candidate["norm"] == item["key"]:
            return candidate
        inter = keywords & candidate["keywords"]
        if not inter:
            continue
        smaller = min(len(keywords), len(candidate["keywords"]))
        if (len(inter) >= 2 and smaller > 0
                and len(inter) * 2 >= smaller
                and _texts_close(item["text"], candidate["text"])):
            return candidate
    return None


def _merge_into(target: dict, item: dict) -> bool:
    """把新条目并入既有行：文本取更长的一条，关键词取并集，usage_count 保留既有值。

    usage_count 不做任何写入：新条目尚无使用记录，合并时把既有计数清零或重置都会
    让「用过几次」失去意义，故一律保留原值（即两者中的较大者）。
    """
    entry = target["entry"]
    if len(item["text"]) > len(target["text"]):
        entry.text = item["text"]
        target["text"] = item["text"]
        target["norm"] = item["key"]
    merged_words = _dedupe_keywords([*_parse_keywords(entry.keywords), *item["keywords"]])
    if merged_words:
        entry.keywords = json.dumps(merged_words, ensure_ascii=False)
        target["keywords"] = {w.casefold() for w in merged_words}
    # 非候选写入意味着人工/模型已确认这条记忆，更新后不应再留候选标记
    if not item["candidate"]:
        entry.candidate = False
    return True


async def resolve_project_id(db: AsyncSession, session_id: int | None) -> int | None:
    """反查会话所属项目 id；查不到或查询异常一律返回 None（不抛）。

    plan-548-2276 阶段 0：这段反查原先只写在 memory_write 工具里并吞掉异常，
    检索侧判定 project 记忆可见范围也需要同一口径，收敛到服务层一处。
    """
    if session_id is None:
        return None
    try:
        from app.persistence.models.message import Session

        sess = await db.get(Session, int(session_id))
        project_id = getattr(sess, "project_id", None) if sess is not None else None
        return int(project_id) if project_id is not None else None
    except Exception:  # noqa: BLE001
        logger.warning("[memory] 反查会话所属项目失败 session=%s", session_id, exc_info=True)
        return None


async def save_memories(db: AsyncSession, *, session_id: int, turn_id: int | None,
                        memories: list[dict], project_id: int | None = None,
                        origin: str = "manual") -> int:
    """批量写入记忆（按目标作用域去重 + 写引擎单写线程）。

    memories 每项：{text, importance?, kind?, scope?, keywords?, origin?}。
    importance < 0.65 不再丢弃而是以 candidate=True 保存（可检索、不注入）。
    scope=project 的条目挂 `project_id`；项目为空时降级为会话级（与 memory_write 同口径）。

    plan-537-2256：
    - 开关必须挡在**唯一写入口**（本函数），只在调用方检查会绕过用户设置；
    - 去重按目标 scope 分组，且用**截断后**的文本算键，修「超长文本每次插入都被当新记忆」；
    - 查重与插入放在同一个写事务内，避免并发调用各自查重后双双插入。

    plan-548-2276（阶段 1）：
    - 每条落 `keywords`（JSON 数组字符串）与 `origin`（manual / extract / compaction），
      keywords 缺省时用 `_derive_keywords` 从正文兜底派生，保证字段非空；
    - 去重口径扩展为「归一化文本相同**或**关键词交集 ≥2 且文本长度接近」——同一件事
      换个说法不再堆成多条；命中时更新既有行（文本取更长、关键词取并集），
      「不新增重复条目」的既有语义不变，返回的新增条数也不含被合并的条目。
    """
    from app.core.config import settings

    if not getattr(settings, "auto_memory_enabled", True):
        return 0

    default_origin = _normalize_origin(origin)

    # 1) 先规范化：text.strip() → text[:500]，kind/scope 归一，scope=project 但无项目则降级
    pending: list[dict] = []
    for m in memories:
        raw = str(m.get("text", "")).strip()
        if len(raw) < 8:
            continue
        try:
            importance = float(m.get("importance", 1.0))
        except (TypeError, ValueError):
            importance = 0.0
        text = raw[:500]
        scope = _normalize_scope(m.get("scope"))
        if scope == "project" and project_id is None:
            scope = "session"
        keywords = _dedupe_keywords(_parse_keywords(m.get("keywords"))) or _derive_keywords(text)
        pending.append({
            "text": text,
            "kind": _normalize_kind(m.get("kind")),
            "scope": scope,
            "candidate": importance < _CANDIDATE_THRESHOLD,
            "key": _normalize_text(text),  # 用截断后的文本算去重键
            "keywords": keywords,
            "origin": _normalize_origin(m.get("origin") or default_origin),
        })
    if not pending:
        return 0

    from app.persistence.database import run_write_locked

    def _persist(s) -> int:
        # 会话层：本会话的 scope=session（历史空值兼容为 session）
        scopes = {p["scope"] for p in pending}
        existing: dict[str, list[dict]] = {}
        if "session" in scopes:
            existing["session"] = _load_existing(s, (
                MemoryEntry.session_id == session_id,
                or_(MemoryEntry.scope == "session", MemoryEntry.scope.is_(None)),
            ))
        if "project" in scopes and project_id is not None:
            existing["project"] = _load_existing(s, (
                MemoryEntry.scope == "project",
                MemoryEntry.project_id == project_id,
            ))
        if "global" in scopes:
            existing["global"] = _load_existing(s, (MemoryEntry.scope == "global",))

        added = 0
        merged = 0
        for p in pending:
            bucket = existing.setdefault(p["scope"], [])
            # 库内重复 + 同批内重复都在这拦下（bucket 里含本批刚 add、尚未 flush 的条目）
            duplicate = _find_duplicate(bucket, p)
            if duplicate is not None:
                if _merge_into(duplicate, p):
                    merged += 1
                continue
            expires_at = None
            if p["scope"] == "session":
                expires_at = (datetime.now(timezone.utc)
                              + timedelta(days=_SESSION_TTL_DAYS)).isoformat()
            entry = MemoryEntry(
                session_id=session_id, turn_id=turn_id, text=p["text"], kind=p["kind"],
                scope=p["scope"], project_id=(project_id if p["scope"] == "project" else None),
                candidate=p["candidate"], expires_at=expires_at,
                keywords=json.dumps(p["keywords"], ensure_ascii=False),
                origin=p["origin"],
            )
            s.add(entry)
            bucket.append({
                "entry": entry,
                "norm": p["key"],
                "keywords": {str(w).casefold() for w in p["keywords"]},
                "text": p["text"],
            })
            added += 1
        if added or merged:
            s.commit()
        if merged:
            logger.info(
                "[memory] 合并到既有记忆 %d 条（关键词/文本近似）session=%s", merged, session_id
            )
        return added

    return int(await run_write_locked(_persist, label="memory.save"))


def _is_expired(entry: MemoryEntry) -> bool:
    if not entry.expires_at:
        return False
    try:
        return datetime.fromisoformat(str(entry.expires_at).replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return False


async def load_memories(db: AsyncSession, session_id: int, *,
                        session_limit: int = 5, project_limit: int = 4, global_limit: int = 3,
                        project_id: int | None = None, top_n: int | None = None,
                        task_keywords: list[str] | None = None) -> list[MemoryEntry]:
    """三层合并读取：session（本会话）→ project（项目）→ global（全局）。

    plan-537-2256：分层预算——各层独立取值（默认 5/4/3，合计最多 12 条），
    避免「全局规范刷满 top_n」把更具体的会话事实挤掉；同层内也按归一化文本去重。
    排序为 usage_count 降序 + generated_at 降序：**注入不算使用**，本函数不写任何字段，
    否则「每轮注入都刷新排序键/计数」会让头部自我强化、老记忆永久锁位，
    且顺序逐轮抖动会破坏提示词缓存。真实使用计数由 memory_search 命中时累加。

    plan-548-2276（阶段 2）：给出 `task_keywords`（本轮用户消息关键词）时，排序键改为
    `(命中关键词数, usage_count, generated_at)` 全降序——先给「和这次任务相关」的记忆
    留位置，同分才回落到使用频次。为空时排序键退化为原口径，行为与改造前完全一致；
    分层预算、跨层去重、注入不计 usage 三条语义始终不变。

    plan-568-2333：有关键词时 SQL 层改用 `_candidate_cap` 的放宽硬上限（不再全表载入）。
    召回结果只在「某层有效记忆条数超过上限」时才可能不同——那种情况下被截掉的是该层内
    `usage_count / generated_at` 排序尾部的行，属于本轮关键词相关度最低的一段，可接受。

    兼容：`top_n` 非空时覆盖三个分层上限（旧调用点行为不变）。
    """
    if top_n is not None:
        session_limit = project_limit = global_limit = int(top_n)

    task_terms = {str(t).strip().casefold() for t in (task_keywords or []) if str(t).strip()}

    def _score(entry: MemoryEntry) -> tuple[int, int, str]:
        hit = len(task_terms & entry_keywords(entry)) if task_terms else 0
        return (hit, int(entry.usage_count or 0), str(entry.generated_at or ""))

    def _base_stmt():
        return select(MemoryEntry).where(
            MemoryEntry.candidate.is_(False),
            (MemoryEntry.superseded_by.is_(None)),
        )

    def _merge(rows: list[MemoryEntry], limit: int, seen: set[str]) -> None:
        """同层内排序取前 limit 条，再按归一化文本跨层去重后并入结果。"""
        if limit <= 0:
            return
        ranked = sorted(_active(rows), key=_score, reverse=True)[:limit]
        for entry in ranked:
            key = _normalize_text(entry.text)
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)

    entries: list[MemoryEntry] = []
    seen: set[str] = set()

    # 1. 会话层（仅 scope=session 的本会话条目；project/global 由下方各自层负责，
    #    避免"从本会话提升为 global"的条目混入会话层造成重复）
    if session_limit > 0:
        stmt = _base_stmt().where(
            MemoryEntry.session_id == session_id,
            (MemoryEntry.scope == "session") | (MemoryEntry.scope.is_(None)),
        ).order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
        if not task_terms:
            # 有关键词时不预截断：SQL 层先砍到 limit 条会让高相关但低频的记忆进不了候选集
            stmt = stmt.limit(session_limit)
        else:
            # plan-568-2333：仍不预截断到 limit，但改为放宽后的硬上限——候选池足够大，
            # 同时单轮载入量不再随记忆总量无界增长
            stmt = stmt.limit(_candidate_cap(session_limit))
        res = await db.execute(stmt)
        _merge([e for e in res.scalars().all()], session_limit, seen)

    # 2. 项目层
    if project_id is not None and project_limit > 0:
        stmt = _base_stmt().where(
            MemoryEntry.scope == "project", MemoryEntry.project_id == project_id,
        ).order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
        if not task_terms:
            stmt = stmt.limit(project_limit)
        else:
            # plan-568-2333：同上，放宽后的硬上限（见 _candidate_cap）
            stmt = stmt.limit(_candidate_cap(project_limit))
        res = await db.execute(stmt)
        _merge([x for x in res.scalars().all()], project_limit, seen)

    # 3. 全局层
    if global_limit > 0:
        stmt = _base_stmt().where(
            MemoryEntry.scope == "global",
        ).order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
        if not task_terms:
            stmt = stmt.limit(global_limit)
        else:
            # plan-568-2333：global 层此前同样无界（全库全局记忆全量载入），
            # 这里是三层里最需要设限的一层，套用同一放宽上限
            stmt = stmt.limit(_candidate_cap(global_limit))
        res = await db.execute(stmt)
        _merge([x for x in res.scalars().all()], global_limit, seen)

    return entries


def _active(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    return [e for e in entries if not _is_expired(e)]


async def list_memories(db: AsyncSession, session_id: int | None = None,
                        scope: str | None = None, project_id: int | None = None,
                        include_candidate: bool = True, q: str | None = None,
                        limit: int = 500) -> list[MemoryEntry]:
    """列出记忆（管理面板用）。可按 scope/project 过滤；默认含候选区。

    plan-memory-fix：列表有条数上限，关键词搜索放到服务端做，
    否则上限之外的记忆永远搜不到。"""
    stmt = select(MemoryEntry).order_by(MemoryEntry.usage_count.desc(), MemoryEntry.generated_at.desc())
    if q and q.strip():
        stmt = stmt.where(MemoryEntry.text.contains(q.strip()))
    if session_id is not None:
        stmt = stmt.where(MemoryEntry.session_id == session_id)
    if scope:
        stmt = stmt.where(MemoryEntry.scope == scope)
    if project_id is not None:
        stmt = stmt.where(MemoryEntry.project_id == project_id)
    if not include_candidate:
        stmt = stmt.where(MemoryEntry.candidate.is_(False))
    res = await db.execute(stmt.limit(max(1, min(limit, 2000))))
    return list(res.scalars().all())


async def count_memories(db: AsyncSession, include_candidate: bool = True) -> dict[str, int]:
    """按作用域统计记忆总数（plan-memory-fix）。

    作用域页签的计数必须与当前筛选无关；此前前端用"当前已加载列表"现算，
    切换页签后其它作用域的计数会变成 0，且全部视图受列表上限截断。"""
    stmt = select(MemoryEntry.scope, func.count()).group_by(MemoryEntry.scope)
    if not include_candidate:
        stmt = stmt.where(MemoryEntry.candidate.is_(False))
    res = await db.execute(stmt)
    out = {"global": 0, "project": 0, "session": 0}
    for scope, n in res.all():
        key = scope if scope in out else "session"
        out[key] += int(n)
    out["all"] = out["global"] + out["project"] + out["session"]
    return out


async def delete_memory(db: AsyncSession, memory_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        s.delete(entry)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"memory.delete.{memory_id}")


async def update_memory(db: AsyncSession, memory_id: int, *, text: str | None = None,
                       kind: str | None = None) -> bool:
    """编辑记忆文本/类型（S8 / plan-41-197）。

    用户可在设置-记忆的详情弹窗里修正记忆文本；编辑即视为人工确认，
    候选标记随之清除（与 promote 同一语义）。
    """
    if text is not None and not text.strip():
        raise ValueError("记忆内容不能为空")

    from app.persistence.database import run_write_locked

    # plan-537-2256：编辑与写入复用同一归一化口径——文本截断 500、
    # kind 非法时忽略该参数（保持原值），绝不把非法枚举写进库。
    new_text = text.strip()[:500] if text is not None else None
    new_kind = kind if kind in _KINDS else None

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        if new_text is not None:
            entry.text = new_text
            entry.candidate = False
        if new_kind is not None:
            entry.kind = new_kind
        s.commit()
        return True

    return await run_write_locked(patch, label=f"memory.update.{memory_id}")


async def promote_memory(db: AsyncSession, memory_id: int, target_scope: str,
                         project_id: int | None = None) -> bool:
    """把记忆提升/降级到目标作用域（session → project → global）。

    plan-537-2256：
    - 提升为 project 但调用方没给 project_id 时，按记忆的来源会话反查其所属项目，
      反查不到才报错（此前直接要求前端传 project_id，项目内的会话也提升不了）；
    - 反查放在调用 run_write_locked **之前**，写线程里不做额外查询；
    - 转到 session/global 时清空 project_id，避免残留旧项目的脏值让列表筛选串项目。
    """
    if target_scope not in _SCOPES:
        raise ValueError("target_scope 必须为 session/project/global")
    if target_scope == "project" and project_id is None:
        from app.persistence.models.message import Session

        row = (await db.execute(
            select(MemoryEntry.session_id).where(MemoryEntry.id == memory_id)
        )).first()
        source_session_id = int(row[0]) if row and row[0] is not None else None
        if source_session_id is not None:
            project_id = (await db.execute(
                select(Session.project_id).where(Session.id == source_session_id)
            )).scalar()
        if project_id is None:
            raise ValueError(
                "该记忆来源会话未绑定项目，无法提升为项目记忆，请在项目内的会话中提升或先绑定项目"
            )
        project_id = int(project_id)

    from app.persistence.database import run_write_locked

    def patch(s):
        entry = s.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        entry.scope = target_scope
        entry.project_id = project_id if target_scope == "project" else None
        if target_scope == "session":
            entry.expires_at = (datetime.now(timezone.utc) + timedelta(days=_SESSION_TTL_DAYS)).isoformat()
        else:
            entry.expires_at = None  # 项目/全局不过期
        entry.candidate = False  # 人工提升/降级视为已确认
        s.commit()
        return True

    return await run_write_locked(patch, label=f"memory.promote.{memory_id}")


async def consolidate(db: AsyncSession, session_id: int) -> str:
    """整合记忆 → 写 MEMORY.md（汇总三层记忆文本）。"""
    entries = await list_memories(db, session_id, include_candidate=False)
    if not entries:
        return ""
    sections: dict[str, list[str]] = {}
    for e in entries:
        scope_tag = {"global": "全局", "project": "项目"}.get(e.scope or "session", "")
        key = f"## {e.kind}" + (f"（{scope_tag}）" if scope_tag else "")
        sections.setdefault(key, []).append(f"- {e.text}")
    lines = ["# Project Memory", ""]
    kind_label = {"fact": "Facts", "convention": "Conventions", "pitfall": "Pitfalls", "decision": "Decisions"}
    merged: dict[str, list[str]] = {}
    for key, items in sections.items():
        for k in kind_label:
            if key.startswith(f"## {k}"):
                merged[key.replace(f"## {k}", f"## {kind_label[k]}")] = items
                break
        else:
            merged[key] = items
    for key, items in merged.items():
        lines.append(key)
        lines.extend(items)
        lines.append("")
    return "\n".join(lines)


async def consolidate_memories(db: AsyncSession, *, session_id: int,
                               project_id: int | None = None) -> dict:
    """惰性整理记忆库（只做 DB 语义整理，不调用 LLM、不物理删行）。

    plan-537-2256 两条规则：
      1. 同作用域内归一化文本完全相同的多行 → 保留 usage_count 最高（相同取 id 最小）
         者，其余置 `superseded_by = 保留行 id`；
      2. 会话记忆（scope=session）已过期且尚未被取代的行 → `superseded_by = -1`。

    作用域边界：session 只限本会话；project 只限同 project_id；global 全表。
    整个流程在同一个写事务内完成，被取代/过期的行不再参与判断，故重复执行不产生新变更（幂等）。
    返回 {"merged": 被合并行数, "expired": 被判定过期的行数}。
    """
    from app.persistence.database import run_write_locked

    def _persist(s) -> dict:
        merged = 0
        expired = 0
        # 规则一：分作用域收集（一次性载入，避免逐行查询）
        buckets: dict[tuple[str, int | None], dict[str, list[MemoryEntry]]] = {}
        rows = s.execute(select(MemoryEntry).where(MemoryEntry.superseded_by.is_(None))).scalars().all()
        for e in rows:
            scope = e.scope or "session"
            if scope == "session":
                if e.session_id != session_id:
                    continue
                group = (scope, None)
            elif scope == "project":
                if project_id is None or e.project_id != project_id:
                    continue
                group = (scope, e.project_id)
            else:
                group = (scope, None)
            buckets.setdefault(group, {}).setdefault(_normalize_text(e.text), []).append(e)

        for items in buckets.values():
            for group in items.values():
                if len(group) < 2:
                    continue
                keep = min(group, key=lambda x: (-(x.usage_count or 0), x.id))
                for e in group:
                    if e.id == keep.id:
                        continue
                    e.superseded_by = keep.id
                    merged += 1

        # 规则二：会话记忆过期即退出有效集（不删行，只打标）
        # plan-549-2281 复盘修复：此查询原先只按 scope 过滤，缺少 session_id 条件——
        # 而 save_memories 给每条会话记忆都设了 30 天 TTL，于是任一会话触发一次整理，
        # 就会把全库所有已过期会话记忆统一置 superseded_by=-1（永久退出有效集），
        # 越过函数自身「session 只限本会话」的作用域承诺，也把惰性整理变成无界全库写。
        for e in s.execute(select(MemoryEntry).where(
            MemoryEntry.scope == "session",
            MemoryEntry.session_id == session_id,
            MemoryEntry.superseded_by.is_(None),
            MemoryEntry.expires_at.is_not(None),
        )).scalars().all():
            if _is_expired(e):
                e.superseded_by = -1
                expired += 1

        if merged or expired:
            s.commit()
        return {"merged": merged, "expired": expired}

    result = await run_write_locked(_persist, label="memory.consolidate_semantic")
    return result or {"merged": 0, "expired": 0}


def write_memory_file(project_path: str, content: str) -> str:
    """落地 .chatcoder/memory/MEMORY.md，返回路径。"""
    target = Path(project_path) / ".chatcoder" / "memory"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "MEMORY.md"
    path.write_text(content, encoding="utf-8")
    return str(path)