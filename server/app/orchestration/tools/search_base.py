"""v31.2: 搜索引擎适配器基类与通用工具。

SearchAdapter 协议：各引擎（google / duckduckgo / bing）实现 search(query, max_results)，
返回 [{title, url, snippet}]；统一经 filter_relevant 清洗后交给 web_search 工具输出。
所有适配器通过 app.core.http_client.build_http_client() 创建 client，
保证「设置面板配置代理 → 搜索引擎自动走代理」。
"""
import re
from abc import ABC, abstractmethod

from app.core.http_client import build_http_client

# 高频无关导航站特征（标题/URL 命中则降权剔除）
_NAV_HINTS = (
    "download chrome",
    "download firefox",
    "google chrome",
    "firefox - download",
    "brave browser",
    "opera browser",
    "download opera",
    "uc browser",
    "best browsers",
)

# 通用浏览器头（各引擎共用）
SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def filter_relevant(results: list[dict], query: str) -> list[dict]:
    """轻量相关性过滤。

    - 剔除命中导航站特征的结果；
    - 保留标题/URL 与 query 词元至少命中 1 个的结果；
    - 全部剔除时返回空（"No results" 让 LLM 换查询词，比喂噪音更可靠）。
    """
    if not results:
        return results
    tokens = [
        t.lower()
        for t in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", query)
        if t.lower() not in ("ai", "的", "了", "是")
    ]
    scored = []
    for r in results:
        hay = f"{r['title']} {r['url']}".lower()
        score = sum(1 for t in tokens if t in hay)
        if any(h in hay for h in _NAV_HINTS):
            score -= 3
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for s, r in scored if s > 0]


class SearchAdapter(ABC):
    """搜索引擎适配器协议。"""

    name: str = "base"

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[dict]:
        """执行搜索，返回 [{title, url, snippet}]；失败/不可用返回空列表。"""
