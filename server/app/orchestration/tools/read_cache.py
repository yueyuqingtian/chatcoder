"""fs_read 未变化短路（按会话）。

同一文件、同一行范围在未修改时，不必把相同内容再发一遍。但存根只能在“较早的读取结果仍完整在上下文中”时
返回：若那条结果已被微压缩折叠为占位符，或已随压缩摘要移出上下文，存根就会指向一个模型已经看不到的内容，
此时必须返回全文。重启后按库中历史重建上下文，判断口径相同（历史里仍有完整结果即命中）。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def norm_path(path: object) -> str:
    """读取记录与写入校验统一用的路径口径（fs_read 与 fs_write 解析路径的方式不同，需先规范化）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """排序并合并重叠 / 端点相接的区间：读 1-30 再读 31-60 视为覆盖 1-60。

    plan-549-2281 复盘修复：covers 原先要求**单个**已登记区间完整覆盖目标区间——
    相邻两次 fs_read 拼出的整段拼不起来，改 20-40 行会被误判「未读」（strict 误伤）。
    判定前先合并（n≤64，O(n log n) 代价可忽略）。
    """
    merged: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


# 存根文案：告诉模型去看上文那条工具结果，而不是让它以为文件内容丢了
UNCHANGED_STUB = (
    "（文件自上次读取后未变化，同一路径与行范围的内容仍在上文的工具结果中，请直接参考它。"
    "如需重新查看，请换一个行范围，或先修改文件。）"
)

# 微压缩折叠占位符的开头（见 compaction.py 的 _micro_compact）
_FOLDED_PREFIX = "[内容已折叠"

_MAX_ENTRIES = 2000  # 每会话的记录上限，超出时丢弃最早的记录
_MAX_RANGES_PER_FILE = 64  # 单个文件保留的已读区间段数上限（超出先合并）
_MAX_FILES_WITH_RANGES = 400  # 保留区间记录的文件数上限（超出丢最早的）


class ReadWindowCache:
    def __init__(self) -> None:
        self._messages: Callable[[], list[Any]] = lambda: []
        self._seen: dict[tuple, tuple[tuple[int, int], str]] = {}
        # 本会话里由模型自己写入 / 修改过的文件：内容它已经知道，再次整体重写不需要先读
        self._known: set[str] = set()
        # plan-548-2277：本会话实际返回给模型的行区间（1-based，含端点），按规范化路径聚合。
        # 与 _seen 的区别：_seen 记「同一 (path, offset, limit) 还能否回存根」，只关心可复用的整段；
        # 这里关心「哪些行模型真的看到过」，供编辑前的覆盖校验（covers）使用。
        self._ranges: dict[str, list[tuple[int, int]]] = {}

    def bind(self, messages_getter: Callable[[], list[Any]]) -> None:
        """每轮绑定当前的消息列表（列表可能被压缩替换，故用 getter 而非引用）。"""
        self._messages = messages_getter

    def lookup(self, key: tuple, fingerprint: tuple[int, int]) -> bool:
        """同一键、同一指纹，且较早的结果仍完整可见时返回 True。"""
        prev = self._seen.get(key)
        if prev is None or prev[0] != fingerprint:
            return False
        call_id = prev[1]
        for m in self._messages() or []:
            if getattr(m, "role", None) == "tool" and getattr(m, "tool_call_id", None) == call_id:
                content = getattr(m, "content", "") or ""
                return bool(content) and not content.startswith(_FOLDED_PREFIX)
        return False

    def record(self, key: tuple, fingerprint: tuple[int, int], call_id: str | None) -> None:
        if not call_id:
            return
        if len(self._seen) >= _MAX_ENTRIES and key not in self._seen:
            self._seen.pop(next(iter(self._seen)))
        self._seen[key] = (fingerprint, call_id)

    # ── plan-548-2277：「先读后改」闸门的行区间记录 ──
    def record_range(self, path: object, start: int, end: int) -> None:
        """登记一段实际返回给模型的行区间（1-based，含端点）。区间非法时静默跳过。"""
        if start <= 0 or end < start:
            return
        target = norm_path(path)
        if target not in self._ranges and len(self._ranges) >= _MAX_FILES_WITH_RANGES:
            # 长会话里文件数无上限会导致只增不减；超出后丢弃最早登记的文件区间，
            # 代价只是该文件要重新 fs_read 一次（闸门仍能工作，不会误判为已读）
            self._ranges.pop(next(iter(self._ranges)), None)
        spans = self._ranges.setdefault(target, [])
        spans.append((start, end))
        if len(spans) > _MAX_RANGES_PER_FILE:
            self._ranges[target] = _merge_spans(spans)

    def shift_ranges(self, path: object, edit_start: int, edit_end: int, delta: int) -> None:
        """一次局部编辑后折算已登记的行区间（plan-560-2311 D1）。

        为什么必须折算：`_ranges` 记的是「模型看到过哪些行」，编辑器每改一次都可能
        增删行数，之后的行号就整体漂移。不折算的话，后续 covers 会拿旧行号去比对
        新文件——本该拒的放过、本该放的拒掉（strict 误伤的主要来源）。

        规则（按编辑点在当前文件行号口径）：
        - 完全在编辑点之前的区间不受影响；
        - 完全在编辑点之后的区间整体平移 delta；
        - 与编辑区间重叠的区间只保留两侧中未被改动到的部分——重叠段的内容已经被换掉，
          模型看到的旧内容不再代表现在的文件，保守地不再算「读过」。

        delta 为 0（纯行内替换）时只需处理重叠段，不会引入任何偏移。
        """
        target = norm_path(path)
        spans = self._ranges.get(target)
        if not spans:
            return
        kept: list[tuple[int, int]] = []
        for s, e in spans:
            if e < edit_start:
                kept.append((s, e))
                continue
            if s > edit_end:
                kept.append((s + delta, e + delta))
                continue
            # 与编辑区间重叠：去掉重叠段，两侧按是否坍缩决定保留
            if s <= edit_start - 1:
                kept.append((s, edit_start - 1))
            if e >= edit_end + 1:
                kept.append((edit_end + 1 + delta, e + delta))
        if kept:
            self._ranges[target] = _merge_spans(kept)
        else:
            self._ranges.pop(target, None)

    def record_edit(
        self, path: object, spans: list[tuple[int, int]], delta: int = 0,
    ) -> None:
        """登记一次**局部**编辑（可能命中多处）的写后真实区间（plan-560-2311 D1）。

        `editor_apply_diff` / `multi_file_edit` 只改了几行，只能登记它们刚写过的
        那些行，不能用 `mark_known` 把整文件标成已知——那会让 fs_write 的
        「先读后写」门禁和 editor 的区间门禁对所有行永久失守（D1 的根因）。

        spans 是**编辑发生时**的行号口径（1-based，含端点）；delta 是本次编辑给整个
        文件带来的行数增量。每处命中的插入行数按「总增量均摊到各命中」还原——同一
        次编辑的多处命中用的是同一份 new_text，插入行数必然相同。

        plan-562-2318 M6：均摊结果必须能整除，否则**不再猜测**。
        此前一律 `(old_total + delta) // len(valid)` 取整：`spans` 在上游被截断到 50 处
        （见 text_edit 的 `applied_spans[:50]`），而 delta 是**整个文件**的增量——命中 100 处
        每处 +1 行时，delta=100、valid=50，算出的每段新行数是 3（真实 2），逐段落差累积，
        本会话对该文件的已读区间整体错位，strict 模式下怎么读都会被判「未读区域」。
        无法整除说明「spans 被截断」或「各处增量不同」，此时登记出来的区间一定不准：
        直接放弃本次区间登记，并把该文件退回「未读过」状态，让下一次编辑前必须重新 fs_read——
        误拒一次远好于悄悄放行未读区域。
        """
        valid = sorted((s, e) for s, e in spans if s > 0 and e >= s)
        if not valid:
            return
        old_total = sum(e - s + 1 for s, e in valid)
        new_total = old_total + delta
        if delta and new_total % len(valid) != 0:
            logger.warning(
                "[read_cache] 编辑区间无法均摊(命中 %d 处, 原文 %d 行, 增量 %d)，"
                "放弃区间登记并要求重新读取", len(valid), old_total, delta,
            )
            self.forget(path)
            return
        new_len = max(0, new_total // len(valid))
        offset = 0
        shifted: list[tuple[int, int]] = []
        for s, e in valid:
            old_len = e - s + 1
            # 逐处折算：每处编辑只影响它自己及其后面的行号，前面的部分不受推动
            self.shift_ranges(path, s + offset, e + offset, new_len - old_len)
            if new_len > 0:
                shifted.append((s + offset, s + offset + new_len - 1))
            offset += new_len - old_len
        for s, e in shifted:
            self.record_range(path, s, e)

    def covers(self, path: object, start: int, end: int) -> bool:
        """[start, end] 是否已被已读区间（合并后）完整覆盖。

        plan-560-2311 D1：判定顺序改为「先校验入参合法性，再看整文件已知，最后比行区间」。
        此前 `target in self._known` 短路在最前面，连非法入参都返回 True，且因为
        editor / multi_edit 局部编辑也调 mark_known，整文件被永久免检——门禁形同虚设。
        现在 `_known` 只表达「整文件内容已知」（fs_write 整文件重写的语义）。
        """
        if start <= 0 or end < start:
            return False
        target = norm_path(path)
        if target in self._known:
            return True  # 整文件已知（fs_write 整文件重写后才会有这条通路）
        return any(
            s <= start and end <= e
            for s, e in _merge_spans(self._ranges.get(target, ()))
        )

    def has_ranges(self, path: object) -> bool:
        """该文件是否存在任何已读区间记录（区分「完全没读过」与「读过局部」）。"""
        return bool(self._ranges.get(norm_path(path)))

    def forget(self, path: object) -> None:
        """丢弃某个文件的全部读取凭据（区间 / 整文件已知 / 可复用存根）。

        plan-562-2318 M6：局部编辑后的区间折算一旦无法确定（`record_edit` 无法均摊），
        登记出来的行号必然偏移，继续沿用会让 strict 模式反复误拒、也可能放行未读区域。
        此处退回「没读过」，代价是下一次编辑前必须重新 `fs_read`——语义正确、代价可控。
        """
        target = norm_path(path)
        self._known.discard(target)
        self._ranges.pop(target, None)
        for key in [k for k in self._seen if k[0] == target]:
            self._seen.pop(key, None)

    def has_any(self) -> bool:
        """本进程内本会话是否登记过任何读取（重启后为空，此时不做「先读后写」强制）。

        plan-548-2277：行区间也算读取凭据——只看 _seen 会把「按区间读过」误判成
        「什么都没读过」，strict 闸门于是变成永不生效。
        plan-560-2311 D1：`_known`（fs_write 整文件重写）同样是读取凭据——模型刚写过
        整篇内容，此时不该因为「本会话没调过 fs_read」就让两道门禁整体失效。
        """
        return bool(self._seen or self._ranges or self._known)

    def mark_known(self, path: object) -> None:
        """登记整文件内容已知（plan-560-2311 D1：只给「整文件重写」用）。

        唯一合法调用点是 `fs_write`：整篇内容由模型给出，文件里已没有任何它没见过的行。
        **局部编辑（editor_apply_diff / multi_file_edit）必须用 `record_edit`**——
        它们只改了几行，标成整文件已知会把两个门禁一起废掉。
        """
        self._known.add(norm_path(path))

    def has_read(self, path: object) -> bool:
        """本会话内是否读取过（或整文件写过）该文件。

        plan-548-2277：行区间也算读过——否则只按区间读过的文件会被 fs_write 误判为
        「未读覆盖」而拒绝，两个闸门的口径就不一致了。
        plan-560-2311 D1：`_known` 现在只由 fs_write 登记，局部编辑不再走这条通路，
        此门的语义回到「模型确实掌握过这份内容」。
        """
        target = norm_path(path)
        return (
            target in self._known
            or bool(self._ranges.get(target))
            or any(k[0] == target for k in self._seen)
        )


_CACHES: dict[int, ReadWindowCache] = {}


def read_cache_for(session_id: int, messages_getter: Callable[[], list[Any]]) -> ReadWindowCache:
    """取得（或新建）会话的读取缓存，并绑定本轮的消息列表。"""
    cache = _CACHES.get(session_id)
    if cache is None:
        cache = ReadWindowCache()
        _CACHES[session_id] = cache
    cache.bind(messages_getter)
    return cache


def clear_cache_for_session(session_id: int) -> None:
    """丢弃某个会话的读取缓存（会话删除 / id 复用前必须调用）。

    plan-549-2281 复盘修复：此前 `_CACHES` 只增不清，而 SQLite 主键没有 AUTOINCREMENT
    （见 persistence/database.py 的同一说明），删除会话 A 后新建的会话 B 会复用同一个
    session_id。若不清缓存，B 就继承了 A 的 `_ranges` / `_known`：`covers()` 返回 True
    让 editor_apply_diff 的 strict 读校验被放行，`has_read()` 返回 True 让 fs_write 的
    「先读后写」直接绕过——两个闸门同时失效。
    """
    _CACHES.pop(session_id, None)


def clear_all() -> None:
    """清空全部会话缓存（测试用：避免跨用例相互污染）。"""
    _CACHES.clear()
