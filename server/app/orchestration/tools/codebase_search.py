"""v1.0: Codebase 搜索工具（plan-230-1144 M3 改造）。

原实现的两个确定性问题：
1. **描述与实现矛盾**：docstring 宣称"使用 embedding 模型向量化/语义搜索"，
   description 字段却自承"基于关键词匹配"——实际是 500 字符定长切块 + 关键词
   词项重叠打分，从未调用任何 embedding 模型；
2. **切块无视语法边界**：固定 30 行/500 字符切块会把函数劈成两半，命中结果是
   半截代码，AI 拿到后仍需人工拼接。

本次改造：
- 描述纠偏：如实说明"关键词检索 + 符号边界切块"，不再虚假宣传语义向量；
- 符号边界切块：优先按 symbol_index_service 的符号区间切块（命中即完整函数/类），
  符号索引不可用时回退定长切块；
- 结果附符号名（kind + name），便于 AI 直接转 fs_read 精读或 symbol_search 深挖。

plan-548-2276（阶段 4.2）改造：
- 分页：新增 offset，top_k 上限提到 20——此前只能看前 5 条，后面的永远取不到；
- 增量：索引按**文件**的 (mtime_ns, size) 指纹比对，只重建指纹变化的文件块
  （此前任一文件变动即整体重建）；
- 中文：查询分词改为「英文小写去停用词 + 中文二元切」，此前 `split()` 让中文查询完全失效。
"""
import fnmatch
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500  # 回退切块的目标字符数
_CHUNK_OVERLAP = 50  # 回退切块重叠
_CHUNK_LINES = 30  # 回退切块每块最多行数
_MAX_CHUNKS = 8000  # 索引分块总数上限（超出后不再收录，防止超大仓库撑爆 JSON）
_MAX_SOURCE_FILES = 3000  # 单次索引收录的源文件数上限（超出按路径排序后截断，保证可复现）
_INDEX_DIR = ".chatcoder"
_INDEX_FILE = "codebase_index.json"
_MAX_TOP_K = 20  # 单页最多返回条数

# plan-549-2281 复盘修复：进程内缓存（工作区 → (files 指纹, skipped 指纹, chunks, 符号代次)）。
# 指纹扫描（rglob + stat）**每次都做**——变更检测是正确性问题，「改完立刻再查」必须
# 立即命中增量重建；缓存省掉的是每次检索都全量解析数 MB 索引 JSON 的重活
# （解析结果在内存里直接复用）。无外部失效方（指纹自检兜底），跨进程不共享。
# plan-560-2311 D7：本工具声明 `concurrency_safe=True`，而 _load_or_build_index 会在
# 多个并发调用里同时读写这个缓存与同一个索引文件（同步 IO 跑在 to_thread 里）——
# 无锁时可能读到半写入的缓存项、或两个线程同时重建同一份索引。
# 代价：同一工作区的首次全量建索引会串行（本来也是重活），之后命中缓存只付一次锁。
_INDEX_MEM_CACHE: dict[str, tuple[dict, dict, list, str]] = {}
_INDEX_LOCK = threading.Lock()

# 英文停用词：查询里的虚词不参与打分（它们几乎总能在代码注释里命中，只会稀释相关性）
_STOPWORDS = {
    "the", "a", "an", "is", "are", "in", "on", "at", "to", "for", "of", "and", "or",
    "how", "what", "where", "which", "with", "this", "that", "it", "be", "do", "does",
}
_WORD_RE = re.compile(r"[0-9a-z_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokenize(text: str) -> set[str]:
    """查询分词：英文按词（小写、去停用词）+ 中文按 3 字滑窗切。

    中文没有空格，`text.lower().split()` 会把整句当成一个“词”，永远不可能与代码块
    的词集合相交。这里按方案定下的口径切：`上下文压缩` → 上下文/下文压/文压缩，
    不足 3 字的中文段（如「压缩」「索引」）整体保留。
    刻意不引入 jieba 等分词依赖：滑窗对短查询足够，且完全可解释。
    """
    lowered = text.lower()
    terms = {w for w in _WORD_RE.findall(lowered) if len(w) > 1 and w not in _STOPWORDS}
    for seg in _CJK_RE.findall(lowered):
        if len(seg) <= 3:
            terms.add(seg)
            continue
        terms.update(seg[i:i + 3] for i in range(len(seg) - 2))
    return terms


def _as_int(value: Any, default: int) -> int:
    """模型偶尔把整数参数写成字符串；解析不了就用默认值，不让整次检索失败。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _symbol_epoch(workspace: Path) -> str:
    """符号索引的「代次」标识（plan-560-2311 D7，plan-562-2318 H3 修正）。

    为什么需要：增量指纹只有 (mtime_ns, size)，文件内容没变但符号索引事后建好时，
    指纹完全一致，已缓存的块仍是当初的定长回退切块——永远不会升级成符号对齐切块。
    代次一变就让全部文件重切一次。

    代次**只由符号计数构成**，不拼「上次更新时间」（H3）：`last_updated` 取自
    `SELECT MAX(updated_at) FROM files`，而该列在每次重新解析文件时都会刷新，于是
    任何一次写盘都让代次变化 → 下一次检索把所有文件判成「指纹变了」→ 重读全仓源文件、
    逐文件查一次符号库、重新切块、重写整份索引，比整体重建还慢，也与「只重建改动过的
    文件」这一承诺矛盾。只有符号计数才真正对应「定长回退块需要升级成符号对齐块」的
    那一次状态跃迁。

    取舍（明确记下）：符号索引重建、但符号总数恰好不变时不会触发升级，这些文件继续
    沿用既有的定长回退块，直到自身 mtime/size 变化才重切。相比「每次都全量重切」，
    这个代价可接受：符号数不变意味着符号边界分布基本未变，定长块仍能检索到内容。

    符号索引不可用时返回空串（与「从未建立」等价）。
    """
    try:
        from app.services import symbol_index_service as sis

        stats = sis.index_stats(workspace)
        if not stats.get("available"):
            return ""
        return str(stats.get("symbols", 0))
    except Exception:  # noqa: BLE001
        logger.debug("[codebase_search] 符号索引代次获取失败，按未建索引处理", exc_info=True)
        return ""

# 支持的文件扩展名
_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".vue", ".svelte", ".html", ".css", ".scss",
    ".sql", ".sh", ".bash", ".yaml", ".yml", ".toml", ".json",
}

# 排除的目录
_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode",
    ".chatcoder",
}


class CodebaseSearchTool(Tool):
    name = "codebase_search"
    concurrency_safe = True  # plan-522-2209：关键词检索只读，索引由后台任务独占更新
    risk_level = "low"
    description = (
        "关键词检索代码库（非语义向量检索）。按关键词在已索引的代码块中打分，"
        "切块优先对齐函数/类符号边界，返回完整代码片段及文件位置与符号名。\n"
        "适合「记不清确切符号名、但记得关键词/注释/字符串」的场景（中文查询同样有支持）；"
        "若已知符号名（函数名/类名）请优先用 symbol_search，更快更准。"
        "结果可翻页：看下一页传 offset。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词（自然语言或代码片段）"},
                        "top_k": {
                            "type": "integer",
                            "description": f"本页返回结果数量（默认 5，最大 {_MAX_TOP_K}）",
                        },
                        "offset": {
                            "type": "integer",
                            "description": (
                                "跳过前 N 条结果（分页用，默认 0）。结果末尾会告诉你下一页的 offset"
                            ),
                        },
                        "file_glob": {"type": "string", "description": "文件过滤(如 *.py, *.ts)"},
                    },
                    "required": ["query"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "")
        file_glob = str(args.get("file_glob") or "")
        top_k = max(1, min(_as_int(args.get("top_k"), 5), _MAX_TOP_K))
        offset = max(0, _as_int(args.get("offset"), 0))

        if not query.strip():
            return ToolResult(ok=False, output="", error="query 为空")

        workspace = Path(ctx.workspace_root)
        if not workspace.is_dir():
            return ToolResult(ok=False, output="", error=f"工作区不存在: {ctx.workspace_root}", error_code="NOT_FOUND")

        # 构建/加载索引（含符号边界信息）。这是同步磁盘/解析 IO，
        # 必须移出事件循环；大项目索引期间仍要能响应 providers/models 等请求。
        import asyncio
        index = await asyncio.to_thread(self._load_or_build_index, workspace)
        if not index:
            return ToolResult(ok=True, output="代码库为空或无支持的代码文件", data={"results": []})

        # plan-568-2333 P2：打分要对每个块（最多 8000 块、每块数千字符）逐词做子串扫描，
        # 同步跑在事件循环线程里会把 WS 广播与其它请求一起挡住；移入线程池执行——
        # 返回的 (分数, chunk) 全是 float/dict/str/int 等纯 Python 对象，跨线程安全。
        scored = await asyncio.to_thread(self._score, index, query=query, file_glob=file_glob)
        total = len(scored)
        if not total:
            return ToolResult(
                ok=True, output=f"未找到与 '{query}' 相关的代码",
                data={"results": [], "total": 0, "offset": offset},
            )

        # plan-548-2276：分页——排序后再切片，跨页顺序由 _score 的稳定排序保证
        page = scored[offset:offset + top_k]
        if not page:
            return ToolResult(
                ok=True,
                output=(
                    f"本页 0 条（共 {total} 条，offset={offset} 已超出末页，已到底）。"
                    "想看得更多可以调大 top_k（上限 20），或换更具体的关键词。"
                ),
                data={"results": [], "total": total, "offset": offset, "has_more": False},
            )

        page_end = offset + len(page)
        has_more = page_end < total
        head = (
            f"第 {offset + 1}-{page_end} 条，共 {total} 条"
            + ("，已到底" if not has_more else f"，继续请传 offset={page_end}")
            + "（关键词检索，非语义向量；切块已按函数/类符号边界对齐）:\n"
        )
        output_parts = [head]
        # 编号用全局序号（带上 offset），便于模型说「第 7 条」时两页能对上
        for i, (score, chunk) in enumerate(page, offset + 1):
            sym_tag = ""
            if chunk.get("symbol"):
                sym_tag = f" [{chunk.get('symbol_kind') or 'symbol'} {chunk['symbol']}]"
            output_parts.append(
                f"--- [{i}] {chunk['file']}:{chunk['start_line']}-{chunk['end_line']}{sym_tag} "
                f"(相关度: {score:.1f}) ---\n{chunk['text'][:700]}\n"
            )

        return ToolResult(
            ok=True,
            output="\n".join(output_parts),
            data={
                "results": [
                    {"file": c["file"], "line": c["start_line"], "score": s,
                     "symbol": c.get("symbol")}
                    for s, c in page
                ],
                "total": total,
                "offset": offset,
                "top_k": top_k,
                "has_more": has_more,
                "next_offset": page_end if has_more else None,
            },
        )

    @staticmethod
    def _score(index: list[dict], *, query: str, file_glob: str = "") -> list[tuple[float, dict]]:
        """关键词打分：查询词项命中数 + 精确匹配 + 路径/符号命中加分。

        词项用**子串**命中而不是「按空格切词后求交集」：中文二元切出的词（如「上下文」）
        与英文前缀（context 之于 context_manager）都要求子串语义；子串扫描走 C 层
        str.find，比给每个块重建词集合快得多。
        排序带次级键（文件 + 行号），保证跨页 offset 不重不漏。
        """
        query_lower = query.lower()
        query_terms = _tokenize(query)
        scored: list[tuple[float, dict]] = []
        for chunk in index:
            if file_glob and not fnmatch.fnmatch(chunk["file"], file_glob):
                continue

            chunk_text = chunk["text"].lower()

            exact_bonus = 5.0 if query_lower in chunk_text else 0.0
            overlap = sum(1 for t in query_terms if t in chunk_text)
            if overlap == 0 and not exact_bonus:
                continue

            score = overlap * 1.0 + exact_bonus
            file_lower = chunk["file"].lower()
            if any(t in file_lower for t in query_terms):
                score += 2.0
            # 符号名命中额外加分（函数名/类名直接匹配关键词时更可能是目标）
            sym = str(chunk.get("symbol") or "").lower()
            if sym and any(t in sym for t in query_terms):
                score += 3.0

            scored.append((score, chunk))

        scored.sort(key=lambda x: (
            -x[0], str(x[1].get("file") or ""), int(x[1].get("start_line") or 0),
        ))
        return scored

    def _load_or_build_index(self, workspace: Path) -> list[dict]:
        """加载或增量重建代码索引（按文件指纹 (mtime_ns, size) 比对）。

        plan-548-2276：此前把所有源文件的 mtime 拼成一个 sha1，任一文件变动即**整体重建**
        （每改一行都要重读全仓、重切全量块）。现在按文件比对指纹，只重建指纹变化/新增的
        文件块，其余原样复用；文件被删则丢弃其块。
        旧格式（只有 meta.sha1、没有 meta.files）读到时视为全部失效，重建一次并写新格式。

        plan-549-2281 复盘修复（两处）：
        ① 进程内缓存——增量只省了「重切块」，没省「每次检索的全量索引 JSON 解析」
           （数千块 × 数 MB），高频调用在指纹全一致时也付这笔 IO；指纹扫描每次照做
           （变更检测是正确性），缓存的是解析结果：命中时直接用内存态，不读盘不解析。
        ② 截断记账——chunks 截到 _MAX_CHUNKS 后，落榜文件的指纹移入 meta.skipped：
           之前指纹仍留在 meta.files 里，「有指纹、无块」的文件既不重建也永不出现（静默丢失）；
           现在落榜文件一旦变化就重新参与切块竞争。

        plan-560-2311 D7（三处）：
        ① 整段包锁（`_INDEX_LOCK`）——本工具声明 concurrency_safe=True，同步 IO 跑在
           to_thread 里，无锁时多个并发调用会同时读写 _INDEX_MEM_CACHE 与同一个索引文件，
           可能读到半写入的缓存项或重复重建。代价是同一工作区首次全量建索引串行（本
           就是重活），之后命中缓存只付一次锁；
        ② 落盘改临时文件 + os.replace（对齐 feature_map_service.save_map）：原先
           write_text 先截断后写，并发读或进程被杀时会把索引留成截断的无效 JSON；
        ③ meta 增 `symbol_epoch`，在 _fp_changed 里参与比对——指纹只有 (mtime_ns, size)，
           文件未改但符号索引事后建好时指纹完全一致，已缓存的定长回退块永远不会升级为
           符号对齐切块。代次只由符号计数构成（不含更新时间），故只有「符号索引从无到有 /
           符号数变化」这一次跃迁会触发全量重切，日常写盘不再抖动（plan-562-2318 H3）。
        """
        with _INDEX_LOCK:
            return self._load_or_build_index_locked(workspace)

    def _load_or_build_index_locked(self, workspace: Path) -> list[dict]:
        """`_load_or_build_index` 的加锁实现体（不可重入，仅供上方持锁调用）。"""
        key = str(workspace)
        epoch = _symbol_epoch(workspace)
        mem = _INDEX_MEM_CACHE.get(key)
        if mem is not None:
            cached_files, cached_skipped, cached_chunks = mem[0], mem[1], mem[2]
            cached_epoch = mem[3] if len(mem) > 3 else ""
        else:
            cached_chunks = []
            cached_files = {}
            cached_skipped = {}
            cached_epoch = epoch
            index_path = workspace / _INDEX_DIR / _INDEX_FILE
            if index_path.exists():
                try:
                    data = json.loads(index_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    data = None
                if isinstance(data, dict) and isinstance(data.get("chunks"), list):
                    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
                    files_meta = meta.get("files")
                    cached_files = files_meta if isinstance(files_meta, dict) else {}
                    skipped_meta = meta.get("skipped")
                    cached_skipped = skipped_meta if isinstance(skipped_meta, dict) else {}
                    cached_epoch = str(meta.get("symbol_epoch") or "")
                    cached_chunks = [
                        c for c in data["chunks"]
                        if isinstance(c, dict) and c.get("file") and isinstance(c.get("text"), str)
                    ]

        source_files = self._collect_source_files(workspace)

        # 每文件指纹：索引增量比对的唯一依据（不存整体 sha1，否则无法定位变化文件）
        fingerprints: dict[str, list[int]] = {}
        for p in source_files:
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(workspace).as_posix()
            fingerprints[rel] = [int(st.st_mtime_ns), int(st.st_size)]

        def _fp_changed(rel: str) -> bool:
            fp = fingerprints[rel]
            # plan-560-2311 D7：符号索引代次变了 → 全部重切（定长回退块升级为符号对齐块）。
            # 代次只含符号计数（plan-562-2318 H3），所以这里只会在符号索引「从无到有 /
            # 符号数变化」时命中，不会因为一次普通写盘就把全部文件重切一次。
            if cached_epoch != epoch:
                return True
            if rel in cached_files:
                return cached_files[rel] != fp
            # 新文件，或曾因截断落榜、现在指纹已变化的文件（重新参与竞争）
            return cached_skipped.get(rel) != fp

        changed = {rel for rel in fingerprints if _fp_changed(rel)}
        removed = {rel for rel in cached_files if rel not in fingerprints}
        if not changed and not removed and cached_chunks:
            _INDEX_MEM_CACHE[key] = (cached_files, cached_skipped, cached_chunks, epoch)
            return cached_chunks  # 全部文件指纹一致，直接用缓存

        reused = [
            c for c in cached_chunks
            if c["file"] in fingerprints and c["file"] not in changed
        ]
        rebuild_paths = [p for p in source_files if p.relative_to(workspace).as_posix() in changed]
        rebuilt = self._build_index(workspace, rebuild_paths)

        chunks = reused + rebuilt
        # 稳定顺序：跨页 offset 只有在顺序确定时才有意义
        chunks.sort(key=lambda c: (str(c.get("file") or ""), int(c.get("start_line") or 0)))
        if len(chunks) > _MAX_CHUNKS:
            chunks = chunks[:_MAX_CHUNKS]

        indexed_files = {str(c.get("file") or "") for c in chunks}
        meta_files = {rel: fp for rel, fp in fingerprints.items() if rel in indexed_files}
        # 落榜（截断）或切块为空的文件指纹记入 skipped：指纹未变时不重切，
        # 一旦变化就重新参与竞争（此前这类文件要么永久消失、要么每轮重切）
        meta_skipped = {rel: fp for rel, fp in fingerprints.items() if rel not in indexed_files}

        try:
            index_path = workspace / _INDEX_DIR / _INDEX_FILE
            index_path.parent.mkdir(parents=True, exist_ok=True)
            # plan-560-2311 D7：临时文件 + os.replace 原子落盘——write_text 先截断后写，
            # 并发读或进程中途被杀时会把索引留成截断的无效 JSON（整个索引白白重建一次）
            tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(
                    {
                        "meta": {
                            "files": meta_files,
                            "skipped": meta_skipped,
                            # 切块来源代次：符号索引事后建好时据此触发全量重切
                            "symbol_epoch": epoch,
                        },
                        "chunks": chunks,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp_path, index_path)
        except OSError:
            logger.debug("[codebase_search] 索引落盘失败（不影响本次检索）", exc_info=True)
            try:
                tmp_path.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass

        logger.info(
            "[codebase_search] 索引增量更新: 复用 %d 块, 重建 %d 文件 / %d 块,"
            " 失效 %d 文件, 落榜 %d 文件, 合计 %d 块",
            len(reused), len(rebuild_paths), len(rebuilt), len(removed),
            len(meta_skipped), len(chunks),
        )
        _INDEX_MEM_CACHE[key] = (meta_files, meta_skipped, chunks, epoch)
        return chunks

    @staticmethod
    def _collect_source_files(workspace: Path) -> list[Path]:
        """收集待索引源文件：**先按相对路径排序，再截断**。

        plan-549-2281 复盘修复：此前直接 `files[:3000]` 依赖 `rglob` 的返回顺序
        （实际是文件系统的 os.scandir 顺序，跨平台/跨文件系统/目录重建都会变）。
        而 `_load_or_build_index` 以「在 fingerprints 里 = 仍存在」推断 removed：
        文件数超过上限时，被截掉的既有索引文件会被算成已删除并丢掉其块；rglob 顺序一变，
        同一批文件就在「收录 / 删除」之间反复抖动，索引与检索结果随之不稳。
        排序让「收录哪些」确定下来，抖动消失。

        plan-568-2333 P1：改 `os.scandir` 递归并在**进入目录前剪枝**。此前 `rglob("*")`
        先把整棵树（含 node_modules）全部枚举出来，再在结果层按路径分量过滤，每次检索
        空转数十万次 scandir/stat；现在命中 `_EXCLUDE_DIRS` 的目录整棵子树不进入。
        `_EXCLUDE_DIRS` 是一组**目录名**（不含路径分隔符），原来按任意层级的分量匹配，
        与按目录名匹配对目录等价；差异只在「文件名恰好等于集合里的名字」时，而这类
        无扩展名文件本来就被后缀过滤丢掉。
        """
        files: list[Path] = []
        # 显式栈代替递归：目录再深也不会撞 Python 递归上限
        stack: list[str] = [str(workspace)]
        # 已展开目录的物理标识去重：Windows 的目录联接（junction）`is_symlink()` 为 False、
        # `is_dir(follow_symlinks=False)` 为 True，无法按链接类型识别；一旦成环会无限展开到
        # 路径超长。这里必须用**跟随链接**的 stat 取目标目录标识——lstat 拿到的是联接自身
        # 的 file id，与目标不同，去重会失效（实测会重复收录 loop/a.py 这类别名路径）。
        # pathlib 3.13 的 rglob 用同一套 (st_dev, st_ino) 去重，故语义一致。
        visited: set[tuple[int, int]] = set()
        while stack:
            current = stack.pop()
            try:
                st = os.stat(current)
                ident = (st.st_dev, st.st_ino)
            except OSError:
                continue
            if ident in visited:
                continue
            visited.add(ident)
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                # 普通子目录：命中排除名整棵剪掉，否则入栈待展开
                                if entry.name not in _EXCLUDE_DIRS:
                                    stack.append(entry.path)
                                continue
                            if entry.is_symlink() and entry.is_dir():
                                # 指向目录的符号链接不进入：可能成环，也会把仓库外内容拉进索引
                                continue
                            if not entry.is_file():
                                continue
                            # 与 `.suffix.lower()` 等价，省掉逐条目构造 Path 的开销
                            if os.path.splitext(entry.name)[1].lower() not in _CODE_EXTENSIONS:
                                continue
                            if entry.stat().st_size > 100_000:
                                continue
                        except OSError:
                            # 权限不足 / 条目已被删除：静默跳过
                            continue
                        files.append(Path(entry.path))
            except OSError:
                # 目录不可读（权限 / 扫描期间被删）：跳过该子树，不中断其余收集
                continue
        # 统一按相对路径排序（与 chunk["file"] 口径一致）：无论是否截断，
        # 收集与切块的顺序都只由路径决定，不随文件系统的返回顺序变化
        files.sort(key=lambda p: p.relative_to(workspace).as_posix())
        if len(files) > _MAX_SOURCE_FILES:
            logger.info(
                "[codebase_search] 源文件数 %d 超过上限 %d，按路径排序后截断（其余文件不参与索引）",
                len(files), _MAX_SOURCE_FILES,
            )
            return files[:_MAX_SOURCE_FILES]
        return files

    def _build_index(self, workspace: Path, files: list[Path]) -> list[dict]:
        # 符号索引优先：把每个文件的符号区间读出，切块按其对齐
        symbols_by_file: dict[str, list[dict]] = {}
        try:
            from app.services import symbol_index_service as sis
            # plan-248-1273: 不在主服务请求内触发符号索引；worker 负责维护 symbols.db。
            # 这里仅读取当前快照，索引未就绪时回退轻量定长切块。
            for p in files:
                rel = p.relative_to(workspace).as_posix()
                syms = sis.outline_file(workspace, rel)
                if syms:
                    symbols_by_file[rel] = syms
        except Exception:
            logger.debug("[codebase_search] 符号索引不可用，回退定长切块", exc_info=True)

        chunks: list[dict] = []
        file_count = 0
        # plan-549-2281 复盘修复：files 已由 _collect_source_files 按路径排序，
        # 因此这里的「达到 _MAX_CHUNKS 就停」也是确定的——此前停在哪完全看 rglob 顺序，
        # 同一仓库两次构建可能收录完全不同的文件块。
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel_path = file_path.relative_to(workspace).as_posix()
            syms = symbols_by_file.get(rel_path)
            chunks.extend(self._chunk_file(content, rel_path, syms))
            file_count += 1
            if len(chunks) > _MAX_CHUNKS:
                break

        logger.info("[codebase_search] 索引构建完成: %d 文件, %d 分块（符号对齐: %s）",
                    file_count, len(chunks), bool(symbols_by_file))
        return chunks

    @staticmethod
    def _chunk_file(content: str, rel_path: str, symbols: list[dict] | None = None) -> list[dict]:
        """按符号边界切块；无符号时回退定长 30 行切块。"""
        lines = content.splitlines()
        chunks: list[dict] = []
        covered: set[int] = set()

        if symbols:
            # 只取顶层与次级符号（parent_id 为 None 或指向类），避免方法粒度太碎
            for s in symbols:
                if s.get("parent_id") is not None and (s.get("kind") or "") not in ("method",):
                    continue
                start = max(1, int(s.get("line_start") or 1))
                end = min(len(lines), int(s.get("line_end") or start))
                if end < start:
                    continue
                text = "\n".join(lines[start - 1:end])
                if len(text.strip()) <= 20:
                    continue
                # 超长符号（>400 行）拆成若干块，前 30 行带符号名
                segs = [(start, end, text)] if (end - start) < 400 else [
                    (start + i, min(end, start + i + 199), "\n".join(lines[start + i - 1:min(end, start + i + 199)]))
                    for i in range(0, end - start, 200)
                ]
                for seg_start, seg_end, seg_text in segs:
                    chunks.append({
                        "file": rel_path, "start_line": seg_start, "end_line": seg_end,
                        "text": seg_text[:_CHUNK_SIZE * 6],
                        "symbol": s.get("name"), "symbol_kind": s.get("kind"),
                    })
                    covered.update(range(seg_start, seg_end + 1))

        # 未被符号覆盖的行（模块级代码/配置/非解析语言）：定长回退切块
        i = 0
        while i < len(lines):
            if (i + 1) in covered:
                i += 1
                continue
            end = min(i + _CHUNK_LINES, len(lines))
            chunk_text = "\n".join(lines[i:end])
            if len(chunk_text.strip()) > 20:
                chunks.append({
                    "file": rel_path,
                    "start_line": i + 1,
                    "end_line": end,
                    "text": chunk_text[:_CHUNK_SIZE * 2],
                })
            i = end - 2 if end - 2 > i else end  # 重叠 2 行，防死循环

        return chunks
