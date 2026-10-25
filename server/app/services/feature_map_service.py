"""plan-548-2276 阶段 4：功能地图（feature map）服务。

目标：把「用户说的功能」映射到「模块 / 文件 / 符号 / 坑点 / 相关计划文档」，
让模型知道该读哪几个文件，而不是先全库盲搜。

三源合成（全部本地、不调用模型）：
1. project 作用域记忆（kind = architecture/flow/pitfall/convention）：架构、流程与坑点；
2. `{workspace}/ai/*.md` 计划文档：标题即功能名，正文里出现的 `server/…` `client/…` 即涉及文件；
3. 符号索引目录聚类（`{workspace}/.chatcoder/symbols.db`）：目录 → 该目录下的主要符号名。

产物：`{workspace}/.chatcoder/feature_map.json`。刻意**不做向量化**——别名与路径是确定性的，
可解释、可手改，错了能修；查询走内存匹配（中文二元切 + 子串命中），不依赖外部服务。

写入边界：本模块只读 `memory_entries`，绝不写它（记忆的写入由 memory_service 独占）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_MAP_DIR = ".chatcoder"
_MAP_FILE = "feature_map.json"

_MAX_ENTRIES = 400          # 功能条目总数上限（防止超大仓库把 JSON 撑大、拖慢每次查询）
_MAX_PLANS = 300            # 最多收录多少篇计划文档
_MAX_PLAN_BYTES = 400_000   # 单篇计划文档读取上限
_MAX_CLUSTERS = 120         # 最多收录多少个目录聚类
_MAX_MEMORIES = 120         # 最多收录多少条项目记忆
_MAX_PATHS = 12             # 每个条目保留多少个可直接 fs_read 的相对路径
_MAX_SYMBOLS = 12           # 每个条目保留多少个符号名
_MAP_STALE_S = 6 * 3600     # 超过这个时间视为过期（查询仍先用旧数据，同时后台刷新）
_SYMBOL_BATCH = 2000        # plan-568-2333 P3：符号库分批流式读取的批大小（不截断总量）

# plan-549-2281 复盘修复：合并时各字段的上限——此前 pitfalls/plans 也被 _MAX_PATHS(12)
# 一刀切，坑点多的记忆被静默裁剪；坑点是 feature_lookup 的核心价值，给更高上限。
_FIELD_CAPS = {
    "aliases": _MAX_PATHS, "modules": _MAX_PATHS, "files": _MAX_PATHS,
    "paths": _MAX_PATHS, "symbols": _MAX_SYMBOLS,
    "pitfalls": 24, "plans": 12,
}

# 参与功能地图的记忆类型：fact/decision 太碎，architecture/flow/pitfall/convention 才有「功能」含义
_MEMORY_KINDS = ("architecture", "flow", "pitfall", "convention")

# 计划文档里出现的仓库内路径（server/… client/… 等）；末尾标点由 _extract_paths 再剥
_PATH_RE = re.compile(r"(?:server|client|electron|docs|ai)/[A-Za-z0-9_.\-/]*[A-Za-z0-9_\-]")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
_ALIAS_SPLIT_RE = re.compile(r"[（）()【】\[\]、,，·/／:：]+")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？\n；;]")
_CLAUSE_SPLIT_RE = re.compile(r"[，,：:]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[0-9a-z_]+")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "in", "on", "at", "to", "for", "of", "and", "or",
    "how", "what", "where", "which", "with", "this", "that", "it", "be", "do", "does",
}

# 后台重建：脏标记 + 防抖（与 symbol_index_manager 同思路，但这里只重建一个小 JSON，成本低）
# 防抖任务表见 schedule_rebuild 处的 _TASKS（按工作区记账）。
_DEBOUNCE_S = 2.0


# ── 切分与匹配 ────────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """查询分词：英文按词（小写、去停用词）+ 中文按 3 字滑窗切。

    中文没有空格，`split()` 会把整句当成一个词，永远匹配不上；这里按方案定下的口径切：
    `上下文压缩` → 上下文/下文压/文压缩，不足 3 字的中文段（如「压缩」）整体保留。
    与 codebase_search 的口径一致（两处各自实现，避免 services 层反向依赖 tools 层）。
    """
    lowered = text.lower()
    terms = {w for w in _WORD_RE.findall(lowered) if len(w) > 1 and w not in _STOPWORDS}
    for seg in _CJK_RE.findall(lowered):
        if len(seg) <= 3:
            terms.add(seg)
            continue
        terms.update(seg[i:i + 3] for i in range(len(seg) - 2))
    return terms


def _extract_paths(text: str) -> list[str]:
    """抽出文中提到的仓库内相对路径（去重、去尾随标点），保持出现顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _PATH_RE.findall(text):
        p = raw.rstrip("，。、；：）)」』》>,.;:")
        if len(p) < 6 or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _module_of(path: str) -> str:
    """路径 → 模块（目录）名：去掉文件名段后取前三级，如 `server/app/orchestration`。"""
    parts = [s for s in path.replace("\\", "/").split("/") if s]
    if not parts:
        return ""
    if "." in parts[-1]:  # 末段是文件（带扩展名）→ 丢掉
        parts = parts[:-1]
    if not parts:
        return ""
    return "/".join(parts[:3])


def _aliases_from_title(title: str) -> list[str]:
    """从计划文档标题里拆出短别名（括号内用分隔符列出的关键词最有用）。"""
    out: list[str] = []
    for chunk in _ALIAS_SPLIT_RE.split(title):
        chunk = chunk.strip()
        if 2 <= len(chunk) <= 20 and chunk not in out:
            out.append(chunk)
    return out[:8]


def _feature_name(text: str) -> str:
    """记忆文本 → 功能名：取第一个短句（纯分隔符切分，不调用模型）。"""
    head = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)[0]
    head = _CLAUSE_SPLIT_RE.split(head, maxsplit=1)[0].strip()
    return (head or text).strip()[:40]


def _haystack(entry: dict) -> str:
    """把一个功能条目摊平成可子串匹配的低价文本。"""
    parts: list[str] = [str(entry.get("feature") or "")]
    for key in ("aliases", "modules", "files", "paths", "symbols", "pitfalls", "plans"):
        value = entry.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
    return "\n".join(parts).lower()


# ── 三源构建 ──────────────────────────────────────────────────────────

def _memory_features(rows: list[dict]) -> list[dict]:
    """项目记忆 → 功能条目：文本进 pitfalls（坑点），提到的路径进 modules/files。"""
    entries: list[dict] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if len(text) < 6:
            continue
        kind = str(row.get("kind") or "")
        paths = _extract_paths(text)
        entries.append({
            "feature": _feature_name(text),
            "aliases": [],
            "modules": sorted({_module_of(p) for p in paths if _module_of(p)})[:6],
            "files": sorted({Path(p).name for p in paths})[:8],
            "paths": paths[:_MAX_PATHS],
            "symbols": [],
            # 坑点/约定才是「下次别再踩」的信息；architecture/flow 只做定位补充
            "pitfalls": [text[:300]] if kind in ("pitfall", "convention") else [],
            "plans": [],
            "source": "memory",
        })
    return entries


def _load_plans(workspace: Path) -> list[dict]:
    """计划文档 → 功能条目：一级标题即功能名，正文路径即涉及文件。"""
    plans_dir = workspace / "ai"
    if not plans_dir.is_dir():
        return []
    entries: list[dict] = []
    try:
        docs = sorted(plans_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    for doc in docs:
        if len(entries) >= _MAX_PLANS:
            break
        try:
            if doc.stat().st_size > _MAX_PLAN_BYTES:
                continue
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = ""
        for line in text.splitlines()[:30]:
            m = _TITLE_RE.match(line.strip())
            if m:
                title = m.group(1).strip()
                break
        if not title:
            continue
        paths = _extract_paths(text)
        entries.append({
            "feature": title[:80],
            "aliases": _aliases_from_title(title),
            "modules": sorted({_module_of(p) for p in paths if _module_of(p)})[:6],
            "files": sorted({Path(p).name for p in paths})[:8],
            "paths": paths[:_MAX_PATHS],
            "symbols": [],
            "pitfalls": [],
            "plans": [doc.name],
            "source": "plan",
        })
    return entries


def _readonly_sqlite_uri(db: Path) -> str:
    """把 sqlite 文件路径转成只读 URI（不依赖 `Path.as_uri()`，见下方说明）。

    此前的缺陷：`_load_symbol_clusters` 直接 `db.as_uri()` 拼只读 URI。
    `as_uri()` 对**相对路径**抛 `ValueError("relative path can't be expressed as a file URI")`，
    而 `settings.workspace_root` 默认值就是相对路径 `./workspace`——未绑定项目的会话里，
    符号库明明存在却在拼 URI 这一步就炸掉，功能地图恒为空（`feature_lookup` 一直回「地图为空」）。
    这里先 `resolve()` 得到绝对路径，再用 `as_uri()`：Windows 的反斜杠、盘符、中文与空格
    都由标准库正确处理（`quote` 手工拼接容易在 `#`/`?` 等字符上踩坑）。
    """
    return f"{db.resolve().as_uri()}?mode=ro"


def _load_symbol_clusters(workspace: Path) -> list[dict]:
    """符号索引目录聚类：目录 → 该目录下主要符号名（索引缺失时静默跳过）。"""
    db = workspace / _MAP_DIR / "symbols.db"
    if not db.exists():
        return []
    import sqlite3

    try:
        # 只读打开：本模块绝不创建/修改符号库（索引由独立 worker 维护）
        conn = sqlite3.connect(_readonly_sqlite_uri(db), uri=True, timeout=1.0)
    except ValueError:
        # 路径无法表达成 URI（例如空路径、非法字符）：不是 sqlite 层错误，单独留痕，
        # 避免与「库损坏 / 无权限」混成同一条日志而查不出原因
        logger.debug("[feature_map] 符号库路径无法转为 URI，跳过符号源 %s", db, exc_info=True)
        return []
    except OSError:
        # 权限不足 / 路径过长等文件系统错误
        logger.debug("[feature_map] 只读打开符号库失败（文件系统）%s", db, exc_info=True)
        return []
    except sqlite3.Error:
        logger.debug("[feature_map] 只读打开符号库失败 %s", db, exc_info=True)
        return []
    by_module: dict[str, dict[str, Any]] = {}
    try:
        # plan-568-2333 P3：游标分批流式聚合——`fetchall()` 会把整张 symbols 表
        # （本仓库 11 万行）一次拉进内存；改为按批取行边取边聚合，内存只与批大小相关。
        # 不加 LIMIT：截断会让功能地图缺目录，语义必须与原实现完全一致。
        cursor = conn.execute(
            "SELECT file_path, name FROM symbols"
            " WHERE parent_id IS NULL"
            " AND kind IN ('class','function','interface','type','enum','struct')"
        )
        while True:
            batch = cursor.fetchmany(_SYMBOL_BATCH)
            if not batch:
                break
            for file_path, name in batch:
                module = _module_of(str(file_path or ""))
                if not module:
                    continue
                bucket = by_module.setdefault(module, {"files": {}, "symbols": {}})
                bucket["files"][str(file_path)] = bucket["files"].get(str(file_path), 0) + 1
                bucket["symbols"][str(name)] = bucket["symbols"].get(str(name), 0) + 1
    except sqlite3.Error:
        logger.debug("[feature_map] 读取符号库失败", exc_info=True)
        # 中途失败即丢弃已聚合的部分结果：原实现 fetchall 失败同样返回空，
        # 半张地图比空地图更难排查（plan-568-2333）
        return []
    finally:
        conn.close()

    clusters: list[dict] = []
    for module, bucket in by_module.items():
        if len(bucket["symbols"]) < 3:  # 符号太少的目录没有「功能」含义
            continue
        files = [p for p, _ in sorted(bucket["files"].items(), key=lambda kv: (-kv[1], kv[0]))]
        symbols = [n for n, _ in sorted(bucket["symbols"].items(), key=lambda kv: (-kv[1], kv[0]))]
        clusters.append({
            "feature": module.split("/")[-1],
            "aliases": [module],
            "modules": [module],
            "files": [Path(p).name for p in files][:8],
            "paths": files[:_MAX_PATHS],
            "symbols": symbols[:_MAX_SYMBOLS],
            "pitfalls": [],
            "plans": [],
            "source": "symbols",
            "_weight": len(symbols),
        })

    clusters.sort(key=lambda e: (-int(e.get("_weight") or 0), str(e["feature"])))
    for e in clusters:
        e.pop("_weight", None)
    return clusters[:_MAX_CLUSTERS]


def _merge(entries: list[dict]) -> list[dict]:
    """同来源同功能名的条目合并（计划文档可能多篇写同一功能）。"""
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for e in entries:
        key = (str(e.get("source") or ""), str(e.get("feature") or "").lower())
        if key not in merged:
            merged[key] = dict(e)
            order.append(key)
            continue
        base = merged[key]
        for field in ("aliases", "modules", "files", "paths", "symbols", "pitfalls", "plans"):
            cur = base.get(field) or []
            seen = {str(v) for v in cur}
            for v in e.get(field) or []:
                if str(v) not in seen:
                    cur.append(v)
                    seen.add(str(v))
            base[field] = cur[:_FIELD_CAPS.get(field, _MAX_PATHS)]
    return [merged[k] for k in order]


def _abs_workspace(workspace: str | Path) -> Path:
    """把工作区路径统一成绝对路径（相对路径按**当前** cwd 解析一次）。

    为什么统一在入口解析：`settings.workspace_root` 默认是相对路径 `./workspace`，
    同一进程里 cwd 一旦变化，相对路径就会指向不同目录——本模块的地图缓存
    （`{workspace}/.chatcoder/feature_map.json`）与符号库会因此「时而命中时而读不到」。
    在入口处解析一次，后续 `map_path` / `build_map` / `rebuild` 都基于同一绝对路径。
    `resolve()` 默认非严格模式，目录不存在也不会抛错；个别平台（不存在的盘符）抛 OSError
    时退回原值，交由调用方的 `is_dir()` / 读写异常处理。
    """
    path = Path(workspace).expanduser()
    if path.is_absolute():
        return path
    try:
        return path.resolve()
    except OSError:
        logger.debug("[feature_map] 工作区路径无法解析为绝对路径 %s", workspace, exc_info=True)
        return path


def build_map(workspace: str | Path, memories: list[dict] | None = None) -> list[dict]:
    """合成功能地图（纯本地、同步）。`memories` 由调用方异步读取后传入。"""
    ws = _abs_workspace(workspace)
    if not ws.is_dir():
        return []
    # 顺序即优先级：记忆（坑点）少而精 → 计划文档（模块/文件最完整）→ 目录聚类（兜底）
    entries: list[dict] = []
    entries.extend(_memory_features(memories or [])[:_MAX_MEMORIES])
    entries.extend(_load_plans(ws))
    entries.extend(_load_symbol_clusters(ws))
    return _merge(entries)[:_MAX_ENTRIES]


# ── 落盘与读取 ────────────────────────────────────────────────────────

def map_path(workspace: str | Path) -> Path:
    return _abs_workspace(workspace) / _MAP_DIR / _MAP_FILE


def load_map(workspace: str | Path) -> list[dict]:
    """读取已落盘的地图；不存在/损坏返回空列表（调用方按「未构建」处理）。"""
    path = map_path(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("entries")
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("feature")]


def save_map(workspace: str | Path, entries: list[dict]) -> None:
    # plan-549-2281 复盘修复：临时文件 + os.replace 原子落盘——直接 write_text 在
    # 并发重建（防抖只挡同一工作区）同时写同一个文件时可能留下截断的 JSON。
    path = map_path(workspace)
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({"entries": entries, "built_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        logger.debug("[feature_map] 写入地图失败 %s", path, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def map_stale(workspace: str | Path) -> bool:
    """地图缺失或过期。过旧时不阻塞查询：先用旧数据，同时后台重建。"""
    try:
        st = map_path(workspace).stat()
    except OSError:
        return True
    return (time.time() - st.st_mtime) > _MAP_STALE_S


# ── 查询 ──────────────────────────────────────────────────────────────

def query(entries: list[dict], text: str, *, limit: int = 3) -> list[dict]:
    """按自然语言功能描述匹配功能条目（分数降序，同名短名优先）。"""
    terms = _tokenize(text)
    if not terms or not entries:
        return []
    # 长查询里总会有几个噪声词：词项多于 2 个时要求至少命中 2 个，避免一命中就入选
    need = 1 if len(terms) <= 2 else 2
    scored: list[tuple[float, dict]] = []
    for entry in entries:
        haystack = _haystack(entry)
        hits = sum(1 for t in terms if t in haystack)
        if hits < need:
            continue
        score = float(hits)
        feature_lower = str(entry.get("feature") or "").lower()
        if any(t in feature_lower for t in terms):
            score += 2.0  # 命中功能名本身比只命中坑点文本更可信
        if entry.get("source") == "plan":
            score += 0.5  # 计划文档通常带最完整的模块/文件清单
        scored.append((score, entry))
    scored.sort(key=lambda x: (
        -x[0], len(str(x[1].get("feature") or "")), str(x[1].get("feature")),
    ))
    return [e for _, e in scored[:max(1, limit)]]


# ── 记忆读取（只读）与重建 ────────────────────────────────────────────

async def load_project_memories(db: AsyncSession | None, project_id: int | None) -> list[dict]:
    """读取 project 作用域的架构/流程/坑点/约定记忆。

    只使用 text/kind/scope/project_id 四列：该表并行改造中可能新增 keywords/origin，
    这里不依赖它们，也**绝不**写这张表。拿不到项目或不给 db 时返回空（不跨项目串数据）。
    """
    if db is None or project_id is None:
        return []
    from sqlalchemy import select

    from app.persistence.models.memory import MemoryEntry

    try:
        res = await db.execute(
            select(MemoryEntry.text, MemoryEntry.kind)
            .where(
                MemoryEntry.scope == "project",
                MemoryEntry.project_id == project_id,
                MemoryEntry.kind.in_(list(_MEMORY_KINDS)),
                MemoryEntry.candidate.is_(False),
                MemoryEntry.superseded_by.is_(None),
            )
            .order_by(MemoryEntry.usage_count.desc())
            .limit(300)
        )
        rows = res.all()
    except Exception:  # noqa: BLE001
        logger.debug("[feature_map] 读取项目记忆失败", exc_info=True)
        return []
    return [{"text": row[0], "kind": row[1]} for row in rows]


async def resolve_project_id(
    session_id: int | None, db: AsyncSession | None = None,
) -> int | None:
    """会话 → 项目 id。优先复用调用方已有的会话，避免另开连接。"""
    if not session_id:
        return None
    from app.persistence.models.message import Session

    try:
        if db is not None:
            sess = await db.get(Session, session_id)
            return getattr(sess, "project_id", None) if sess else None
        from app.persistence.database import async_session_factory

        async with async_session_factory() as own:
            sess = await own.get(Session, session_id)
            return getattr(sess, "project_id", None) if sess else None
    except Exception:  # noqa: BLE001
        logger.debug("[feature_map] 反查会话所属项目失败", exc_info=True)
        return None


async def rebuild(
    workspace: str | Path,
    *,
    db: AsyncSession | None = None,
    session_id: int | None = None,
    project_id: int | None = None,
) -> dict:
    """重建功能地图并落盘（读盘/解析走线程，不阻塞事件循环）。

    返回 `{entry_count, path, project_id, memory_count}`；任何单源失败都不影响其它源。
    """
    ws = _abs_workspace(workspace)
    if project_id is None and session_id is not None:
        project_id = await resolve_project_id(session_id, db)
    memories = await load_project_memories(db, project_id)
    entries = await asyncio.to_thread(build_map, ws, memories)
    carried = 0
    if project_id is None:
        # plan-549-2281 复盘修复：只给 session_id 时反查项目可能失败（会话已删 / 库异常），
        # 此时 project_id 为 None → 读不到任何项目记忆 → 新地图不含记忆条目 → save_map
        # 覆盖后上一次地图里的记忆条目被静默清空。无法确定项目时宁可沿用旧条目也不丢。
        entries, carried = await asyncio.to_thread(_carry_over_memory_entries, ws, entries)
    await asyncio.to_thread(save_map, ws, entries)
    logger.info(
        "[feature_map] 重建完成 ws=%s 条目=%d 记忆=%d(沿用 %d) 项目=%s",
        ws, len(entries), len(memories), carried, project_id,
    )
    return {
        # plan-549-2281：entries 在别处指条目列表，这里返回的是计数，改名消歧
        "entry_count": len(entries),
        "path": str(map_path(ws)),
        "project_id": project_id,
        # 沿用旧条目时 memories 为空，计入 carried 才能如实反映地图里的记忆条数
        "memory_count": len(memories) + carried,
    }


def _carry_over_memory_entries(ws: Path, entries: list[dict]) -> tuple[list[dict], int]:
    """把上一次地图里的记忆条目接回本次结果（同步，由 rebuild 在线程里跑）。

    仅在「拿不到项目、因而读不到任何项目记忆」时使用：此时记忆源为空不是「项目记忆
    真被清空」，而是「本次无法确认项目」，不能据此把已有条目当成陈旧数据删掉。
    返回 (新条目列表, 沿用条数)；记忆条目优先级最高，置于最前并受总条目上限约束。
    """
    previous = [dict(e) for e in load_map(ws) if str(e.get("source") or "") == "memory"]
    if not previous:
        return entries, 0
    if any(str(e.get("source") or "") == "memory" for e in entries):
        # 本次确实拿到了记忆源：以新读取结果为准，不混入旧条目
        return entries, 0
    return (previous + entries)[:_MAX_ENTRIES], len(previous)


async def _delayed_rebuild(workspace: str, delay: float, session_id: int | None) -> None:
    await asyncio.sleep(delay)
    try:
        await rebuild(workspace, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.debug("[feature_map] 后台重建失败 ws=%s", workspace, exc_info=True)
    finally:
        # 任务结束自摘，避免 _TASKS 里积累已完成句柄（plan-549-2281：改为按工作区记账）
        _TASKS.pop(workspace, None)


# plan-549-2281 复盘修复：防抖任务从单一全局 _TASK 改为按工作区记账——全局单任务时
# A 工作区防抖等待期间，B 工作区的重建请求会被静默丢弃（返回 False），多窗口下漏刷新。
_TASKS: dict[str, "asyncio.Task[None]"] = {}


def schedule_rebuild(
    workspace: str | Path, *, delay: float = _DEBOUNCE_S, session_id: int | None = None,
) -> bool:
    """标记需要后台重建（同一工作区防抖，同时只跑一个）；无事件循环时返回 False。"""
    # 为什么要先绝对化：防抖键此前直接用 `str(workspace)`，`./workspace` 与
    # `D:\...\workspace` 虽然指向同一目录，却被当成两个工作区分别排任务——同一目录被
    # 并发重建，既浪费又可能互相覆盖地图。
    key = str(_abs_workspace(workspace))
    running = _TASKS.get(key)
    if running is not None and not running.done():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _TASKS[key] = loop.create_task(_delayed_rebuild(key, delay, session_id))
    return True


__all__ = [
    "build_map", "load_map", "save_map", "map_path", "map_stale", "query",
    "rebuild", "schedule_rebuild", "load_project_memories", "resolve_project_id",
]
