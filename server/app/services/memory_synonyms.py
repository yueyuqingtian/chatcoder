"""记忆检索的同义词表与文本切分（plan-548-2276 阶段 2）。

为什么需要这两件事：中文没有空格，模型回忆时用的词和写入时用的词往往不同
（用户问「压缩后失忆」，记忆里写的是「上下文压缩 / compaction」）。只靠
`text LIKE '%关键词%'` 会零命中，因此检索前先把查询改写成一组候选词：

1. **同义词扩展**：内置小表，中英文混排，覆盖工程语境里高频的近义表达；
2. **切分**：英文小写化 + 去停用词；中文按 2 字与 3 字滑窗生成片段。

刻意不引第三方分词库：记忆条目的量级很小，一张可手改的表比引入分词依赖更可控，
出错也能直接改这里。表后续可继续扩充。
"""
import re

# 单个查询最多保留多少个候选词（防止 OR 条件与 FTS MATCH 串爆炸）
MAX_TERMS = 40

# 中文「整块词」的长度上限：超过这个长度的是句子，不是关键词，只保留滑窗碎片
_MAX_CHINESE_WORD = 8

# 参与切分的文本长度上限（plan-562-2318 A30）。
# 为什么需要：`_task_keywords(user_message)` 传进来的是整条用户消息，用户粘一份数万字的
# 报错日志 / 代码时，滑窗片段数约为 2N（数万~十万级字符串对象），而且每轮上下文重建
# 都要再执行一次。只取首个 2000 字符：检索价值集中在开头（用户第一句就在说需求/问题），
# 尾部大量重复的日志行对候选词质量没有增益。
_MAX_TOKENIZE_CHARS = 2000

# 单个查询的滑窗候选总预算，与 MAX_TERMS 同量级。滑窗候选本就排在整块词与英文词之后，
# 超出总数上限的部分一定会被 _push 丢掉，因此提前停止生成，而不是先生成再截断。
_MAX_WINDOWS = MAX_TERMS

# 同义/别名组：任一词命中即把整组加入候选（组间可共用词，合并取并集）
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("坑", "坑点", "陷阱", "踩坑", "pitfall", "pitfalls", "gotcha"),
    ("规范", "约定", "规则", "convention", "conventions", "rule", "rules", "准则"),
    ("决策", "决定", "决议", "decision", "decisions", "取舍"),
    ("压缩", "上下文压缩", "压缩块", "compaction", "compact", "checkpoint", "检查点"),
    ("检索", "搜索", "查询", "search", "grep", "recall", "召回"),
    ("记忆", "memory", "memories"),
    ("发布", "发版", "版本", "release", "releases", "version"),
    ("计划", "方案", "规划", "plan", "plans", "planning"),
    ("配置", "设置", "config", "configuration", "settings", "setting"),
    ("报错", "错误", "异常", "error", "errors", "exception", "traceback"),
    ("性能", "卡顿", "performance", "latency", "耗时"),
    ("测试", "单测", "test", "tests", "pytest"),
    ("迁移", "migration", "migrations", "migrate"),
    ("权限", "审批", "permission", "permissions", "approval"),
    ("提示词", "prompt", "prompts", "system prompt"),
    ("工具", "tool", "tools", "function", "函数"),
    ("后端", "服务端", "backend", "server"),
    ("前端", "客户端", "client", "frontend", "ui"),
)

# 英文停用词 + 中文单字虚词：切分时丢弃，避免「the / 的 / 了」把候选词位置吃满
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "by", "for", "from",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "done",
    "it", "its", "this", "that", "these", "those", "there", "here", "with", "without",
    "as", "not", "no", "but", "if", "then", "than", "so", "such", "can", "could",
    "should", "would", "will", "shall", "may", "might", "must", "have", "has", "had",
    "i", "you", "we", "they", "he", "she", "my", "your", "our", "their", "me", "us",
    "的", "了", "是", "在", "和", "与", "就", "也", "都", "而", "及", "或", "把",
    "被", "这", "那", "个", "有", "不", "很", "再", "又", "还", "要", "会", "对",
})

# 切分单位：一段连续英文标识符、一段连续数字、或一段连续汉字
_PIECE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]*|\d+(?:\.\d+)*|[\u4e00-\u9fff]+")

# 词的别名索引：同一词出现在多组时，取各组并集
_TERM_INDEX: dict[str, set[str]] = {}
for _group in _SYNONYM_GROUPS:
    for _term in _group:
        _TERM_INDEX.setdefault(_term.casefold(), set()).update(t.casefold() for t in _group)


def expand_terms(term: str) -> set[str]:
    """返回该词所在同义词组的全部词（含自身）；未命中返回 `{term}`。

    返回值统一小写化（casefold），便于与 `MemoryEntry.keywords` 做集合运算。
    """
    key = str(term or "").strip().casefold()
    if not key:
        return set()
    group = _TERM_INDEX.get(key)
    if not group:
        return {key}
    return set(group) | {key}


def _chinese_windows(chunk: str, limit: int = _MAX_WINDOWS) -> list[str]:
    """中文片段的滑窗切片：3 字在前、2 字在后，最多生成 `limit` 个就停。

    plan 文档原文写「二元切分」，示例却是 3 字（上下文 / 下文压 / 文压缩）。
    两种长度各有用处：2 字片段能被 `text LIKE '%…%'` 命中，3 字片段才能进
    FTS5 trigram（trigram 对长度 <3 的查询恒为零命中），故都生成。

    plan-562-2318 A30：加硬上限（`limit`，默认 `_MAX_WINDOWS`）且**边生成边计数**，
    超预算立即返回——旧实现在调用侧整段 `extend`，绕过了 MAX_TERMS 的保护。
    3 字先于 2 字生成，所以预算紧张时保留的是能进 FTS5 的 3 字片段。
    """
    if limit <= 0:
        return []
    chunk = chunk[:_MAX_TOKENIZE_CHARS]
    out: list[str] = []
    for size in (3, 2):
        if size > len(chunk):
            continue
        for start in range(len(chunk) - size + 1):
            out.append(chunk[start:start + size])
            if len(out) >= limit:
                return out
    return out


def tokenize(text: str, *, with_windows: bool = True) -> list[str]:
    """把一段文本切成检索候选词：去重保序，最多 `MAX_TERMS` 个。

    规则：
    - 英文片段小写化并丢掉停用词与单字符；
    - 中文片段先按「整块词」给出（超过 `_MAX_CHINESE_WORD` 字的长句不算词，只留滑窗），
      再补充 2 字 / 3 字滑窗（`上下文压缩` → 上下文 / 下文压 / 文压缩 / 上下 / 文压 …）；
    - 整块词在前、滑窗碎片在后：派生 `keywords` 只取前几个时才能拿到像样的词
      （`with_windows=False` 时只返回整块词）；
    - 结果去重保序，超出上限直接截断——候选词越多召回噪声越大；
    - plan-562-2318 A30：入参文本只取前 `_MAX_TOKENIZE_CHARS` 字符，滑窗候选总量与
      整块词共用 `MAX_TERMS` 预算，名额用完即停止生成。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _push(word: str) -> bool:
        w = word.strip()
        if not w or w in seen:
            return True
        seen.add(w)
        out.append(w)
        return len(out) < MAX_TERMS

    # 先截断再切分：粘贴超长消息时不能让滑窗在处理阶段就炸成十万级对象
    text = str(text or "")[:_MAX_TOKENIZE_CHARS]
    windows: list[str] = []
    for piece in _PIECE_RE.findall(text):
        if "\u4e00" <= piece[0] <= "\u9fff":
            if len(piece) <= _MAX_CHINESE_WORD and not _push(piece):
                return out
            if with_windows:
                # 滑窗并入 MAX_TERMS 预算：剩余名额用完就不再生成（而不是生成后截断）
                remaining = MAX_TERMS - len(out) - len(windows)
                if remaining > 0:
                    windows.extend(_chinese_windows(piece, remaining))
            continue
        lowered = piece.casefold()
        if len(lowered) < 2 or lowered in _STOPWORDS:
            continue
        if not _push(lowered):
            return out
    for word in windows:
        if not _push(word):
            return out
    return out
