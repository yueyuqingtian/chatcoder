"""v31.2: DuckDuckGo 搜索适配器（免费，无需 API key）。

端点: https://html.duckduckgo.com/html/?q=...（经典 HTML 端点，结构稳定）
解析: div.result 块 → a.result__a（标题+链接）→ a.result__snippet（摘要）。
- 链接为 //duckduckgo.com/l/?uddg=<encoded> 跳转包装，需解包 uddg 参数；
- 广告（/y.js?ad_provider=...）直接剔除。
注意: 直连（无代理）时多数网络环境不可达，需在设置面板配置 HTTP 代理。
"""
import html as html_mod
import re
from urllib.parse import parse_qs, quote_plus, urlparse

from app.core.http_client import build_http_client
from app.orchestration.tools.search_base import SEARCH_HEADERS, SearchAdapter

_BLOCK_RE = re.compile(
    r'<div[^>]+class="[^"]*result[^"]*results_links[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


def _unwrap_ddg_url(href: str) -> str:
    """DDG 跳转包装 //duckduckgo.com/l/?uddg=<encoded>&rut=... → 真实 URL。"""
    if "uddg=" in href:
        u = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        if u:
            return u
    return href


class DuckDuckGoSearchAdapter(SearchAdapter):
    name = "duckduckgo"

    async def search(self, query: str, max_results: int) -> list[dict]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            async with build_http_client(
                timeout=15, follow_redirects=True, headers=SEARCH_HEADERS
            ) as client:
                resp = await client.get(url)
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        return _parse_ddg(resp.text, max_results)


def _parse_ddg(page: str, max_results: int) -> list[dict]:
    results = []
    seen: set[str] = set()
    for block in _BLOCK_RE.findall(page):
        tm = _TITLE_RE.search(block)
        if not tm:
            continue
        url = _unwrap_ddg_url(tm.group(1))
        # 剔除广告：DDG 广告跳转到 /y.js?ad_domain=...&ad_provider=...
        if "ad_provider" in url or "y.js" in url or "bing.com/aclick" in url:
            continue
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", tm.group(2))).strip()
        if not title or url in seen:
            continue
        seen.add(url)
        snippet = ""
        sm = _SNIPPET_RE.search(block)
        if sm:
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", sm.group(1))).strip()
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results
