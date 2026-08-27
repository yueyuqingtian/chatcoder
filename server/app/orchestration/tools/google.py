"""v31.2: Google 搜索适配器（免费抓取，无需 API key）。

端点: https://www.google.com/search?q=...&num=...&hl=zh-CN
解析: 结果结构 <a href="..."><h3>Title</h3></a>，链接为 /url?q= 包装需解包；
     摘要取标题后最近的 VwiC3b/IsZvec 摘要块。
注意:
1. 直连（无代理）时多数网络环境不可达，需在设置面板配置 HTTP 代理；
2. Google 对数据中心 IP 反爬严格，无头请求可能命中验证码页——此时返回空，
   由 web_search 提示 LLM 换引擎。
"""
import html as html_mod
import re
from urllib.parse import parse_qs, quote_plus, urlparse

from app.core.http_client import build_http_client
from app.orchestration.tools.search_base import SEARCH_HEADERS, SearchAdapter

# 结果项: <a href="URL"><h3...>Title</h3></a>（含 <br> 等中间节点）
_RESULT_RE = re.compile(
    r'<a[^>]+href="(/url\?q=[^"]+|https?://[^"]+)"[^>]*>'
    r'(?:(?!</a>).)*?<h3[^>]*>(.*?)</h3>',
    re.DOTALL | re.IGNORECASE,
)
_SNIPPET_RE = re.compile(
    r'<div[^>]+class="[^"]*(?:VwiC3b|IsZvec)[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)


def _unwrap_google_url(href: str) -> str:
    """Google 结果链接：/url?q=<encoded>&sa=U... → 真实 URL。"""
    if href.startswith("/url?q="):
        q = parse_qs(urlparse(href).query).get("q", [""])[0]
        return q or href
    return href


class GoogleSearchAdapter(SearchAdapter):
    name = "google"

    async def search(self, query: str, max_results: int) -> list[dict]:
        url = (
            "https://www.google.com/search"
            f"?q={quote_plus(query)}&num={max_results}&hl=zh-CN&gl=CN"
        )
        try:
            async with build_http_client(
                timeout=15, follow_redirects=True, headers=SEARCH_HEADERS
            ) as client:
                resp = await client.get(url)
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        return _parse_google(resp.text, max_results)


def _parse_google(page: str, max_results: int) -> list[dict]:
    results = []
    seen: set[str] = set()
    for m in _RESULT_RE.finditer(page):
        url = _unwrap_google_url(m.group(1))
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if not title or url in seen or url.startswith("http://www.google.com/sorry"):
            continue
        seen.add(url)
        # 摘要：标题匹配位置之后 2500 字符内最近的摘要块
        snippet = ""
        sm = _SNIPPET_RE.search(page, m.end())
        if sm and sm.start() - m.end() < 2500:
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", sm.group(1))).strip()
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results
