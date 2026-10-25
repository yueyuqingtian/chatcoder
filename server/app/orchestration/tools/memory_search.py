"""v0.8 / plan-230-1144 M4.1: memory.search 工具(low risk,免审批)。

三源联合检索（此前只查会话历史消息，命名与语义错位——与 MemoryEntry 记忆库无关）：
1. **记忆库**（MemoryEntry）：AI 记录/抽取的长期记忆（session/project/global 三层），
   含候选区（低置信未注入 prompt 的记忆）；
2. **会话历史消息**：按关键词检索主群 + 子会话的全部消息（含已被摘要压缩的）；
3. **压缩块**：被落库式压缩遮蔽的历史消息（标注所属压缩块）。

结果按来源分组并注明出处层，AI 据此判断可信度与时效。

plan-548-2276 阶段 0/2 对记忆库这一源做了两处重写：
- **作用域硬约束**：此前查询只有 `text ILIKE` + `superseded_by IS NULL`，没有任何
  session/project 条件，会话记忆会被其它会话检索到（真实越权）。现在 session 层只认
  本会话、project 层只认同项目、global 层无条件，条件写在 SQL 里而非提示词里；
- **三级召回 + RRF**：keywords 命中 → text 子串 → FTS5 trigram（不可用时跳过），
  用 RRF 融合三级排名；排序完全不依赖 usage_count 这类可变计数，支持稳定的 `offset` 分页。
"""
import asyncio
import logging
from typing import Any

from app.orchestration.prompts.memory_rules import MEMORY_VERIFY_REMINDER
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.persistence.database import async_session_factory

logger = logging.getLogger(__name__)

# plan-560-2311 D6：异步补记 usage 的后台任务集。
# 必须持引用：asyncio 只持 task 的弱引用，不保存可能在执行前就被 GC 回收。
_BG_TASKS: set[asyncio.Task] = set()

# 记忆三层与来源的中文标注（输出用）
_SCOPE_TAG = {"global": "全局", "project": "项目", "session": "会话"}
_ORIGIN_TAG = {"manual": "手动", "extract": "抽取", "compaction": "压缩"}
_MEMORY_SCOPES = ("session", "project", "global")
# RRF 常数取业界常用的 60：压平单级排名的头部优势，避免某一级完全主宰顺序
_RRF_K = 60
# 每一级召回取多少候选（融合后再按 limit/offset 分页）
#
# 为什么从 60 提到 200：此前是 60，三级并集上界只有 180，所以 `total` 报的是**池大小**
# 而不是真实命中数，翻页到 offset=180 之后必然返回空——而页尾提示还引导模型继续翻。
# 现在改成 200（单级），并把 total 换成真实 count（见 `_search_memories`），
# 可翻页上限由「本级池实际命中的并集大小」决定（page_cap），超过即明确告知已到上限。
#
# 为什么**固定**而不随 offset 增大：RRF 分取决于各级召回的名次，而名次取决于池子大小；
# 一旦第 1 页用 60、第 3 页用 120，同一查询的两页就会得到不同排名，翻页必然重复或漏项。
# 固定值在同一次查询内是不变量，跨页严格一致——这也是 `test_memory_search_paging` 的断言前提。
_CANDIDATE_POOL = 200
# offset 允许翻到的硬上限：与池并集上界（单级 200 × 三级）对齐，避免无限翻页
_MAX_OFFSET = _CANDIDATE_POOL * 3
# 查询扩展的候选词上限：每个词都要拼进 SQL LIKE / FTS MATCH 串
_MAX_QUERY_TERMS = 48
# 进入 SQL LIKE 条件的词数上限（plan-568-2333）
#
# 为什么需要：每个词都拼一个 `ILIKE '%词%'` 的 OR 分支，前后通配无法走索引，
# 单条查询本身就是一次全表扫描；48 个词等于把同一次扫描的过滤表达式放大 48 倍
# （SQL 解析 + 逐行多次子串匹配都要按词数增长），而 `keywords` 与 `text` 两级各扫一遍、
# `count(*)` 再扫一遍，共三次全表。
#
# 取值理由：12 个词的表达式规模约为原来的 1/4，而候选池上界（200）远小于典型命中数，
# 因此过滤强度基本不损失。选词策略见 `_sql_like_terms`：长词（选择性高）优先，
# 但短词必须优先保留——见下一条常量。
_MAX_SQL_LIKE_TERMS = 12
# FTS trigram 的最小可检索词长（与 memory_index._MIN_TERM_LEN 一致；那边是私有常量，
# 不跨模块引用私有 API，故在本模块重新声明）。
#
# 为什么关键（plan-568-2333）：trigram 切分器对长度 < 3 的查询恒为零命中，
# 所以「压缩」「回滚」「部署」这类 2 字中文词**只能靠 SQL LIKE 召回**。
# 中文查询经 `tokenize` 后短词占比很高，若按长度降序一律裁剪，这些短词会被挤出 LIKE，
# 只含短词的记忆就会三级全漏。故 SQL 词表里短词优先占名额。
_FTS_MIN_TERM_LEN = 3
_MAX_SQL_SHORT_TERMS = 8
# 结果末尾的固定提示（记忆是历史快照，必须提示核对当前代码）
# 唯一事实源在 prompts/memory_rules.MEMORY_VERIFY_REMINDER——此前本文件另写一份
# _STALE_HINT，与 memory_rules 头注释「memory_search 在结果末尾附本提示」的声明不一致，
# 两处口径收敛为一处（plan-549-2281 复盘修复）。
_STALE_HINT = MEMORY_VERIFY_REMINDER


class MemorySearchTool(Tool):
    name = "memory_search"
    concurrency_safe = True  # plan-522-2209：只读检索，写操作在 memory_write（独占）
    risk_level = "low"
    description = (
        "跨来源检索记忆：同时搜索①记忆库（长期记忆条目，跨 session/project/global 三层）、"
        "②当前会话全部历史消息（含被摘要/压缩的早期消息）、③被压缩遮蔽的原始消息。"
        "两个用途：**写记忆前查重**（先搜同主题条目，能更新就不新增）、"
        "回忆之前讨论过的细节/决策/任务安排/项目约定。"
        "注意：记忆库结果是历史快照，可能已过时或被后续决策取代，采信前先与当前代码/对话核对。"
        "**按需检索**：必须给出具体关键词（不要空泛词），结果已限量分页；"
        "如需查看完整段落，再用 compaction_view 按 index + keyword 精确取回，"
        "**不要全量拉取压缩前上下文**。"
        "结果会标注来源（记忆库/历史消息/压缩块）。"
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
                        "keyword": {
                            "type": "string",
                            "description": "搜索关键词(如任务名、人名、技术名词)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "每类来源最多返回条数(默认10)",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "记忆库结果分页偏移(默认0)；结果满 limit 时用它翻页",
                        },
                        "scopes": {
                            "type": "string",
                            "description": (
                                "记忆库作用域过滤，逗号分隔：session,project,global（默认全查）。"
                                "各层始终受权限约束：session 仅本会话、project 仅当前项目"
                            ),
                        },
                        "sources": {
                            "type": "string",
                            "description": "逗号分隔的来源过滤：memory,history,compaction（默认全部）",
                        },
                    },
                    "required": ["keyword"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        keyword = str(args.get("keyword", "")).strip()
        try:
            limit = int(args.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 50))
        try:
            offset = int(args.get("offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        # 上限与候选池并集上界对齐（原先硬编码 500，与池大小无关）
        offset = max(0, min(offset, _MAX_OFFSET))
        raw_sources = str(args.get("sources") or "memory,history,compaction").lower()
        sources = {s.strip() for s in raw_sources.split(",") if s.strip()}
        raw_scopes = str(args.get("scopes") or ",".join(_MEMORY_SCOPES)).lower()
        scopes = {s.strip() for s in raw_scopes.split(",") if s.strip()} & set(_MEMORY_SCOPES)
        if not scopes:
            scopes = set(_MEMORY_SCOPES)

        if not keyword:
            return ToolResult(ok=False, output="", error="keyword 不能为空")

        sections: list[str] = []
        data: dict = {
            "keyword": keyword, "memory": 0, "memory_total": 0,
            "history": 0, "compaction": 0, "offset": offset,
            "fts": False, "fts_available": False,
        }
        kw_lower = keyword.casefold()
        mem_note = ""
        # plan-560-2311 D6：本页命中的记忆 id 先攒着，整次请求算完再统一异步补记使用次数
        _bump_ids: list[int] = []

        async with async_session_factory() as db:
            # ── 源 1: 记忆库（三层 + 候选区，作用域硬约束 + 三级召回）──
            # plan-548-2276：作用域条件在 SQL 层（session 只认本会话、project 只认同项目），
            # 候选区仍可检索——它们只是未达注入阈值，标注「（候选·未注入）」提示置信度。
            if "memory" in sources:
                try:
                    found = await _search_memories(
                        db, ctx.session_id, keyword=keyword,
                        limit=limit, offset=offset, scopes=scopes,
                    )
                    data["memory_total"] = found["total"]
                    data["memory_page_cap"] = found["page_cap"]
                    data["memory_total_exact"] = found["total_exact"]
                    data["fts"] = found["fts"]
                    data["fts_available"] = found["fts_available"]
                    mems = found["entries"]
                    if mems:
                        lines = _render_memory(mems)
                        if data["memory_total"] > offset + len(mems):
                            # total 已是**真实命中数**（count(*)），
                            # 与池大小区分开；池被打满时如实给出「最多能翻到第几条」，
                            # 不再写「共 N+ 条」这种既像精确值又像下界的模糊口径。
                            if found.get("pool_capped"):
                                total_note = (
                                    f"共 {data['memory_total']} 条，候选池每级只召回最近 "
                                    f"{_CANDIDATE_POOL} 条，最多只能翻到第 "
                                    f"{found['page_cap']} 条"
                                )
                            else:
                                total_note = f"共 {data['memory_total']} 条"
                            lines.append(
                                f"（{total_note}，已显示第 {offset + 1}-"
                                f"{offset + len(mems)} 条，翻页用 offset={offset + len(mems)}）"
                            )
                        lines.append(_STALE_HINT)
                        sections.append("\n".join(lines))
                        data["memory"] = len(mems)
                        # plan-537-2256：被主动检索命中才算「使用」——注入只让模型看到，不计次。
                        # plan-560-2311 D6：不在本页渲染之前 await 写库——写库会阻塞响应。
                        # plan-562-2318 H6 后 usage_count 已不参与任何排序键，翻页一致性不再
                        # 依赖「何时 bump」；仍保留异步补记，只为不让检索等一次写事务。
                        _bump_ids.extend(e.id for e in mems)
                    else:
                        # 本页为空但 total>0 时，`cap_note` 会说明「已到
                        # 可检索上限」，不能退化成「没结果」——那会让模型反复调大 offset 空转。
                        mem_note = found.get("cap_note") or _fts_note(
                            data["fts"], data["fts_available"],
                        )
                except Exception:
                    # plan-560-2311 D6：此前是裸 except: pass，任一级静默失败都无从排查
                    # （记忆库检索挂了只表现为「没结果」）。补日志，保持不影响其它源。
                    logger.warning("[memory_search] 记忆库检索失败(已跳过该源)", exc_info=True)
            if "history" in sources:
                try:
                    from app.orchestration.context_memory import search_session_memory
                    msgs = await search_session_memory(db, ctx.session_id, keyword, limit)
                    if msgs:
                        lines = ["【历史消息】"]
                        for m in msgs:
                            speaker = m.sender_type
                            if m.sender_type == "agent":
                                speaker = m.content.get("agent_name") or f"agent#{m.sender_id}"
                            text = m.content.get("text") or m.content.get("note") or "(非文本)"
                            if len(text) > 300:
                                text = text[:300] + "..."
                            thread_tag = f"[子会话#{m.thread_id}]" if m.thread_id else "[主群]"
                            lines.append(f"- {thread_tag}[{speaker}] {text}")
                        sections.append("\n".join(lines))
                        data["history"] = len(msgs)
                except Exception:
                    # plan-560-2311 D6：同上，历史消息源失败必须留痕
                    logger.warning("[memory_search] 历史消息检索失败(已跳过该源)", exc_info=True)

            # ── 源 3: 压缩块（被遮蔽的原始消息）──
            if "compaction" in sources:
                try:
                    from app.services import compression_service
                    entries = await compression_service.list_compaction_index(db, ctx.session_id)
                    hits: list[str] = []
                    for e in entries:
                        cid = e.get("compaction_id") or ""
                        idx = e.get("index") or "-"
                        summary_hit = kw_lower in str(e.get("summary_preview") or "").casefold()
                        # 摘要未命中时深入被遮蔽消息检索（限量防炸）
                        found: list[str] = []
                        if not summary_hit and cid:
                            try:
                                hidden = await compression_service.get_compacted_messages(
                                    db, ctx.session_id, cid,
                                )
                            except Exception:
                                # plan-560-2311 D6：单个压缩块读取失败不该整源失败，但必须留痕
                                logger.warning(
                                    "[memory_search] 压缩块消息读取失败 compaction_id=%s",
                                    cid, exc_info=True,
                                )
                                hidden = []
                            for m in _flatten(hidden)[:400]:
                                text = _msg_text(m)
                                if kw_lower in text.casefold():
                                    found.append(f"    · [#{getattr(m, 'id', '?')}] {text[:200]}")
                                    if len(found) >= limit:
                                        break
                        if summary_hit or found:
                            head = f"- 压缩块 #{idx}（compaction_id={cid}）："
                            if summary_hit:
                                head += f"摘要命中「{str(e.get('summary_preview') or '')[:120]}」"
                            hits.append(head)
                            hits.extend(found)
                    if hits:
                        sections.append("【压缩块】\n" + "\n".join(hits))
                        data["compaction"] = len(hits)
                except Exception:
                    # plan-560-2311 D6：同上，压缩块源失败必须留痕
                    logger.warning("[memory_search] 压缩块检索失败(已跳过该源)", exc_info=True)

        total = data["memory"] + data["history"] + data["compaction"]
        # plan-560-2311 D6：所有分页计算已完成，此时再补记「使用次数」，不再污染本页的
        # 候选池排序。用后台任务异步执行，不阻塞本次响应（原实现是 await 前台写入）。
        _schedule_bump(_bump_ids)
        if total == 0:
            # 本页为空要区分「真的没命中」与「翻过头了」。
            # 后者若也回「未找到」，模型只会继续加大 offset 空转（原实现的真实表现）。
            if mem_note and data["memory_total"] > 0:
                return ToolResult(
                    ok=True,
                    output=(
                        f"关键词 '{keyword}' 的记忆检索已到可检索上限，本页没有更多条目。"
                        f"（共命中 {data['memory_total']} 条）{mem_note}"
                    ),
                    data=data,
                )
            return ToolResult(
                ok=True,
                output=f"未找到与 '{keyword}' 相关的记忆/历史/压缩内容。{mem_note}",
                data=data,
            )

        header = (
            f"关键词 '{keyword}' 检索结果："
            f"记忆库 {data['memory']} 条 · 历史消息 {data['history']} 条 · 压缩块 {data['compaction']} 条"
        )
        if data["memory_total"] > data["memory"]:
            # total 是真实命中数；池打满时明确告知最多能翻到第几条，
            # 不要让模型拿 offset 无限试探（超出上限后每页都空）。
            if data.get("memory_page_cap") and data["memory_total"] > data["memory_page_cap"]:
                header += (
                    f"（记忆库共 {data['memory_total']} 条，候选池只覆盖最近 "
                    f"{data['memory_page_cap']} 条，可翻到该条为止）"
                )
            else:
                header += f"（记忆库共 {data['memory_total']} 条，可用 offset 翻页）"
        return ToolResult(ok=True, output=header + "\n\n" + "\n\n".join(sections), data=data)


def _schedule_bump(ids: list[int]) -> None:
    """把「本次命中的记忆」的使用计数丢到后台补记（plan-560-2311 D6）。

    整次请求算完后异步补记：既不阻塞本次响应，也不占用前台事务。
    plan-562-2318 H6 之后 usage_count 已退出排序键（排序只看 RRF 分 + id），
    因此补记时机不再影响分页一致性，这里只是「不阻塞响应」的实现细节。
    """
    if not ids:
        return
    try:
        task = asyncio.create_task(_bump_usage(ids))
    except RuntimeError:  # 无运行中的事件循环（同步调用 / 已关闭）：直接放弃，不影响检索
        logger.debug("[memory_search] 无事件循环，跳过使用计数更新")
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _escape_like(term: str) -> str:
    """转义 LIKE 元字符（调用处统一带 `escape="\\\\"`）。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sql_like_terms(terms: list[str]) -> list[str]:
    """挑出进入 SQL LIKE 的词（plan-568-2333）：短词优先占位，长词按长度降序补足。

    两种排序口径分开是有意的：
    - 短词（长度 < `_FTS_MIN_TERM_LEN`）只能靠 LIKE 召回，按 `terms` 的**原顺序**取——
      `expand_query` 输出保序（用户原词及其同义词在前），序列即相关性优先级；
    - 长词 FTS 也能覆盖，按长度降序取，让 SQL 过滤选择性更高。
    返回值顺序确定（`terms` 顺序稳定），不会让候选池边界抖动。
    只裁剪「拼进 SQL 的过滤词」，返回条数不超过 `_MAX_SQL_LIKE_TERMS`。
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in terms:
        word = str(raw or "").strip()
        if not word or word in seen:
            continue
        seen.add(word)
        ordered.append(word)
    short = [w for w in ordered if len(w) < _FTS_MIN_TERM_LEN][:_MAX_SQL_SHORT_TERMS]
    long = sorted((w for w in ordered if len(w) >= _FTS_MIN_TERM_LEN), key=lambda w: (-len(w), w))
    return short + long[:max(0, _MAX_SQL_LIKE_TERMS - len(short))]


def expand_query(keyword: str) -> list[str]:
    """查询改写：切分 + 同义词扩展（plan-548-2276 4.3 第 1 步）。

    中文靠 `tokenize` 的滑窗拿到「压缩」这类短片段，再经同义词组扩展到
    「上下文压缩 / compaction」——否则「压缩后失忆」这类自然表述零命中。
    结果去重保序，并限制总量避免拼出过长的 SQL / MATCH 串。
    """
    from app.services import memory_synonyms

    terms: list[str] = []
    seen: set[str] = set()
    for base in memory_synonyms.tokenize(keyword):
        # sorted 保证扩展词顺序确定（集合迭代顺序不稳定，会让排序结果抖动）
        for term in (base, *sorted(memory_synonyms.expand_terms(base))):
            word = str(term or "").strip()
            if not word or word in seen:
                continue
            seen.add(word)
            terms.append(word)
            if len(terms) >= _MAX_QUERY_TERMS:
                return terms
    return terms


def _fts_note(fts_used: bool, fts_available: bool) -> str:
    """无结果时的说明：区分「确实没记过」「FTS 未命中」「索引不可用」三种情况。"""
    if fts_used:
        return ""
    if fts_available:
        return "（已启用 FTS 全文召回但未命中；可换更具体的词重试）"
    return "（本次未启用 FTS 全文召回，只按关键词与文本子串匹配；可换更具体的词重试）"


def _render_memory(entries: list) -> list[str]:
    """渲染记忆库结果：每条带 `[作用域/来源/日期]` 前缀 + 候选标记。"""
    # 延迟导入：memory_service 依赖模型层，模块级导入会绕成循环引用
    # plan-549-2281 复盘修复：改走公开包装 parse_keywords——此前直接 import
    # 私有 _parse_keywords，跨模块耦合私有 API。
    from app.services.memory_service import parse_keywords

    lines = ["【记忆库】"]
    for entry in entries:
        scope = getattr(entry, "scope", None) or "session"
        origin = getattr(entry, "origin", None) or "manual"
        tag = _SCOPE_TAG.get(scope, scope)
        origin_tag = _ORIGIN_TAG.get(origin, origin)
        date = str(getattr(entry, "generated_at", "") or "")[:10]
        cand = "（候选·未注入）" if getattr(entry, "candidate", False) else ""
        words = parse_keywords(getattr(entry, "keywords", None))
        kw_note = f" {{{'/'.join(words[:4])}}}" if words else ""
        lines.append(f"- [{tag}/{origin_tag}/{date}]{cand}{kw_note} {entry.text}")
    return lines


async def _search_memories(db, session_id: int, *, keyword: str, limit: int, offset: int,
                           scopes: set[str]) -> dict:
    """记忆库三级召回 + RRF 融合（plan-548-2276 阶段 0/2）。

    作用域是**硬约束**（写在 SQL 里，不是提示词里）：
    session 只认本会话、project 只认同项目、global 无条件；未绑定项目时 project 层自然为空。

    三级召回各自贡献 RRF 分（1/(60+rank)），因此不依赖任何一级的绝对分数：
      1. `keywords` JSON 元素命中（写入时由模型/兜底派生，最精确）；
      2. `text` 子串命中（保留原 LIKE 兜底，覆盖 keywords 缺失的旧数据）；
      3. FTS5 trigram BM25（运行期不可用时整级跳过，功能不失效）。
    排序口径只由「RRF 分 + id」决定，不使用 usage_count（plan-562-2318 H6）：
    usage_count 会被 `_bump_usage` 在每次命中后异步 +1，是可变计数；一旦参与排序，
    第 1 页命中项 +1 就会改变后续页的候选池边界与 tier 内名次，`ordered[offset:offset+limit]`
    会重复或跳过条目。可变计数只用于展示与治理，不再进排序键。
    """
    from sqlalchemy import and_, or_, select

    from app.persistence.models.memory import MemoryEntry
    from app.services import memory_index
    from app.services.memory_service import entry_keywords, resolve_project_id

    result: dict = {
        "entries": [], "total": 0, "total_exact": True,
        # 可翻页上限（本次候选池的并集大小）：offset 达到它之后不会再有任何条目
        "page_cap": 0, "pool_capped": False, "cap_note": "",
        "fts": False, "fts_available": False,
    }
    fts_available = memory_index.is_available()
    result["fts_available"] = fts_available
    terms = expand_query(keyword)
    if not terms:
        return result

    project_id = await resolve_project_id(db, session_id)
    scope_conds = []
    if "session" in scopes:
        scope_conds.append(and_(
            or_(MemoryEntry.scope == "session", MemoryEntry.scope.is_(None)),
            MemoryEntry.session_id == session_id,
        ))
    if "project" in scopes and project_id is not None:
        scope_conds.append(and_(
            MemoryEntry.scope == "project",
            MemoryEntry.project_id == project_id,
        ))
    if "global" in scopes:
        scope_conds.append(MemoryEntry.scope == "global")
    if not scope_conds:
        return result

    base = [MemoryEntry.superseded_by.is_(None), or_(*scope_conds)]
    scores: dict[int, float] = {}
    objects: dict[int, object] = {}
    term_set = {t.casefold() for t in terms}
    # plan-568-2333：只有前 `_MAX_SQL_LIKE_TERMS` 个词进 SQL LIKE（见该常量注释）；
    # `term_set` 仍用词全集打分，FTS MATCH 串也用词全集。
    like_terms = _sql_like_terms(terms)

    def _contribute(items: list) -> None:
        for rank, entry in enumerate(items):
            memory_id = getattr(entry, "id", None)
            if memory_id is None:
                continue
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            objects.setdefault(memory_id, entry)

    # ── 一级：keywords 命中（JSON 数组元素精确匹配）──
    # plan-549-2281 复盘修复：SQL 侧补稳定排序——无 ORDER BY 时同分条目的返回顺序不保证，
    # RRF 按名次给分会让多次相同查询的排序微抖。
    # plan-562-2318 H6：排序键改为纯 id desc（去掉 usage_count）——usage_count 是可变计数，
    # 参与 ORDER BY 会让「本页命中 → 计数 +1 → 下一页候选池边界变化」造成跨页重复/漏项。
    # Python 侧排序改用（关键词重合数, -id）：同一查询两次调用必然得到同一顺序。
    keyword_conds = [
        MemoryEntry.keywords.ilike(f'%"{_escape_like(t)}"%', escape="\\") for t in like_terms
    ]
    res = await db.execute(
        select(MemoryEntry).where(*base, or_(*keyword_conds))
        .order_by(MemoryEntry.id.desc())
        .limit(_CANDIDATE_POOL)
    )
    tier1 = list(res.scalars().all())
    pool_capped = len(tier1) >= _CANDIDATE_POOL
    tier1.sort(key=lambda e: (-len(term_set & entry_keywords(e)), -e.id))
    _contribute(tier1)

    # ── 二级：text 子串命中（原 LIKE 路径，覆盖没有 keywords 的旧记忆）──
    text_conds = [MemoryEntry.text.ilike(f"%{_escape_like(t)}%", escape="\\") for t in like_terms]
    res = await db.execute(
        select(MemoryEntry).where(*base, or_(*text_conds))
        .order_by(MemoryEntry.id.desc())
        .limit(_CANDIDATE_POOL)
    )
    tier2 = list(res.scalars().all())
    pool_capped = pool_capped or len(tier2) >= _CANDIDATE_POOL
    tier2.sort(key=lambda e: (-len(term_set & entry_keywords(e)), -e.id))
    _contribute(tier2)

    # ── 三级：FTS5 trigram（不可用/失败时返回空，整级跳过）──
    fts_ids = await memory_index.search_ids(terms, _CANDIDATE_POOL)
    if fts_ids:
        pool_capped = pool_capped or len(fts_ids) >= _CANDIDATE_POOL
        res = await db.execute(select(MemoryEntry).where(*base, MemoryEntry.id.in_(fts_ids)))
        found = {e.id: e for e in res.scalars().all()}
        # 按 bm25 顺序还原；不在 found 里的 id 说明不满足作用域，直接丢弃（越权防护）
        tier3 = [found[i] for i in fts_ids if i in found]
        if tier3:
            result["fts"] = True
            _contribute(tier3)

    # plan-549-2281 复盘修复：usage_count 不再作为加性微调参与总分。
    # 原写法 `scores += 0.001 × usage_count` 的前提是「远小于 RRF 名次差」，
    # 但相邻名次差只有 1/60-1/61≈0.000273，1 次命中（+0.001）就已经超过它，
    # 于是「用得多的老记忆」能靠累计计数翻转整个排名。
    # plan-562-2318 H6：连「同分时用 usage_count 当 tiebreak」也一并去掉——
    # `_bump_usage` 会把本页命中的条目 +1，同分条目的相对名次照样会变，
    # `ordered[offset:offset+limit]` 第 2 页仍可能重复第 1 页、或整体跳过若干条。
    # 现在排序键只有（-RRF 分, -id）：两者在同一次查询内都是不变量，跨页严格一致。
    # 代价是「常用的靠前」在检索分页里不再生效；usage_count 仅用于展示与老化治理。
    ordered_ids = sorted(scores, key=lambda i: (-scores[i], -int(i)))
    ordered = [objects[i] for i in ordered_ids]

    # ── total 必须是**真实命中数**，不是候选池大小 ──
    # 原实现 `result["total"] = len(ordered)` 报的是池内并集大小（上界 三级×池），
    # 于是「共 180 条」既不是真实总数，翻到 offset=180 之后又必然为空——提示与行为自相矛盾。
    # 这里用一条 count(*)：过滤条件与二级召回（text 子串）完全一致——keywords 命中与
    # FTS 命中的条目文本里必然也含查询词（keywords 由文本派生、FTS 索引的就是文本），
    # 因此 count(text LIKE ...) 就是三级并集的真实上界，且**只走索引/一次聚合，
    # 不把全表行拉进内存**（记忆条目可能上万，禁止全表扫描式实现）。
    # 计数失败（例如某些库不支持该聚合写法）时退回池大小并标 total_exact=False，
    # 不静默假装精确。
    #
    # plan-568-2333：count 的过滤条件仍是二级召回的 text LIKE（保留，因为它是唯一能给出
    # 「真实命中总数」的口径），但 SQL 过滤词已收敛到前 `_MAX_SQL_LIKE_TERMS` 个
    # （见 text_conds 的构造），因此 query 词超过该数量时，只被「剩余短词」命中的条目
    # 不会计入 count——total 在那种情况下只是并集的近似下界，故用 max(len(ordered), count)
    # 兜底，避免出现「共 N 条」反而小于本页可翻条数（文案会走 `memory_total > memory_page_cap`
    # 之外的分支，给出自相矛盾的提示）。
    total = len(ordered)
    total_exact = False
    try:
        from sqlalchemy import func

        res = await db.execute(
            select(func.count()).select_from(MemoryEntry).where(*base, or_(*text_conds))
        )
        counted = res.scalar()
        if counted is not None:
            total = int(counted)
            total_exact = True
    except Exception:  # noqa: BLE001
        logger.warning("[memory_search] 记忆命中总数统计失败，退回候选池口径", exc_info=True)

    if total < len(ordered):
        # plan-568-2333：count 口径没覆盖到池内已有的条目（词数被裁剪或三级并集越出
        # text LIKE 的假设），如实标为不精确，并以并集大小为下界
        total = len(ordered)
        total_exact = False

    result["total"] = total
    result["total_exact"] = total_exact
    # plan-549-2281 复盘修复：候选池打满时池内并集只是真实命中的子集——如实标注，
    # 并在文案里写清「只覆盖最近 N 条」，避免把池口径当成精确总数。
    result["pool_capped"] = bool(pool_capped)
    result["page_cap"] = len(ordered)
    if limit <= 0:
        result["entries"] = []
        return result

    page = ordered[offset:offset + limit]
    if page:
        result["entries"] = page
        return result

    # 翻页真正可用的另一半——offset 越过可翻页上限时不能静默返回空。
    # 池是固定大小（见 _CANDIDATE_POOL 注释：随 offset 变大破坏跨页一致性），因此超过上限
    # 就是**确实没有更多可翻**；给一条明确说明，让模型停止盲目翻页（而不是以为「正好没命中」）。
    if offset > 0 and offset >= len(ordered):
        result["cap_note"] = (
            f"（offset={offset} 已超出可检索上限：本次候选池最多覆盖到第 "
            f"{len(ordered)} 条{'（候选池已打满，更早的记忆未进池）' if pool_capped else ''}；"
            f"请缩小关键词或换更具体的说法，不要继续增大 offset）"
        )
    return result


async def _bump_usage(ids: list[int]) -> None:
    """命中记忆的「使用」计数（plan-537-2256）：usage_count + 1 且记录真实活跃时间。

    批量更新走写引擎单写线程；计数失败不影响检索结果，只记日志。
    """
    if not ids:
        return
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from app.persistence.database import run_write_locked
    from app.persistence.models.memory import MemoryEntry

    now_iso = datetime.now(timezone.utc).isoformat()

    def _persist(s):
        s.execute(
            update(MemoryEntry)
            .where(MemoryEntry.id.in_(ids))
            .values(usage_count=MemoryEntry.usage_count + 1, last_usage_at=now_iso)
        )
        s.commit()

    try:
        await run_write_locked(_persist, label="memory.search_usage")
    except Exception:  # noqa: BLE001
        logger.warning("[memory_search] 记忆使用计数更新失败(非阻塞)", exc_info=True)


def _flatten(items) -> list:
    """get_compacted_messages 返回值可能是消息列表或含列表的 dict，统一为列表。"""
    if isinstance(items, dict):
        for key in ("messages", "items", "shadowed"):
            v = items.get(key)
            if isinstance(v, list):
                return v
        return []
    if isinstance(items, list):
        return items
    return []


def _msg_text(m) -> str:
    c = m.content if isinstance(getattr(m, "content", None), dict) else {}
    return str(c.get("text") or c.get("output") or c.get("error") or c.get("note") or "")
