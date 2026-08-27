"""web_search 工具 — 联网搜索（多引擎，支持代理）。

v1.0: 改用 Bing 搜索（国内可达），替代 DuckDuckGo（国内无法访问）。
v31.1: 噪音治理——
  1. 请求加 mkt=zh-CN&setlang=zh-hans&cc=CN，避免按出口 IP 返回他国语言结果；
  2. 解析 Bing /ck/a 跳转链接，解出真实目标 URL；
  3. HTML 实体解码 + 按真实 URL 去重；
  4. 轻量相关性过滤（filter_relevant）。
v31.2: 多引擎 + 代理——
  - schema 新增 engine 参数：bing（默认）/ google / duckduckgo；
  - 所有引擎统一走 app.core.http_client.build_http_client()，
    设置面板配置 HTTP 代理后自动走代理（google/duckduckgo 直连通常不可达）；
  - google/duckduckgo 无结果时给出「配置代理」提示。
"""
import base64
import html
import re
from urllib.parse import parse_qs, quote_plus, urlparse

from app.core.http_client import build_http_client
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.duckduckgo import DuckDuckGoSearchAdapter
from app.orchestration.tools.google import GoogleSearchAdapter
from app.orchestration.tools.search_base import filter_relevant

_MAX_RESULTS = 8
_MAX_OUTPUT = 6000

_ENGINES = ("bing", "google", "duckduckgo")


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web for real-time information. Returns titles, URLs, and snippets. "
        "engine: bing (default, no proxy needed) / google / duckduckgo "
        "(google & duckduckgo may require an HTTP proxy configured in settings)."
    )
    risk_level = "low"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "engine": {
                            "type": "string",
                            "enum": list(_ENGINES),
                            "description": "Search engine. bing=默认(直连可用); google/duckduckgo 需在设置中配置 HTTP 代理",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max results (default 8, max 15)",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = args.get("query", "").strip()
        if not query:
            return ToolResult(ok=False, output="", error="query is empty")

        # v31.2: 兼容 ta3 伪装 schema 的参数名（WebSearch: searchEngine/maxResults/timeRange）
        engine_arg = args.get("engine") or args.get("searchEngine")
        if engine_arg in ("search_pro",):
            engine_arg = "google"
        elif engine_arg in ("search_pro_sogou", "search_pro_quark"):
            engine_arg = "duckduckgo"
        elif engine_arg in ("search_std", "noLimit") or engine_arg is None:
            engine_arg = "bing"

        max_results = min(
            15, max(1, args.get("max_results") or args.get("maxResults") or _MAX_RESULTS)
        )
        engine = engine_arg if engine_arg in _ENGINES else "bing"

        if engine == "google":
            results = await GoogleSearchAdapter().search(query, max_results)
            if not results:
                return ToolResult(
                    ok=True,
                    output=(
                        f'No results for "{query}" via google. '
                        "提示: Google 直连通常被网络屏蔽，请在设置面板「HTTP 代理」配置代理后重试。"
                    ),
                    data={"results": [], "engine": "google"},
                )
        elif engine == "duckduckgo":
            results = await DuckDuckGoSearchAdapter().search(query, max_results)
            if not results:
                return ToolResult(
                    ok=True,
                    output=(
                        f'No results for "{query}" via duckduckgo. '
                        "提示: DuckDuckGo 直连通常被网络屏蔽，请在设置面板「HTTP 代理」配置代理后重试。"
                    ),
                    data={"results": [], "engine": "duckduckgo"},
                )
        else:  # bing
            results = await _bing_search(query, max_results)
            if not results:
                return ToolResult(
                    ok=True, output=f'No results for "{query}"', data={"results": []}
                )

        results = filter_relevant(results, query)
        if not results:
            return ToolResult(
                ok=True, output=f'No results for "{query}"', data={"results": [], "engine": engine}
            )

        lines = [f'Web search: "{query}" ({len(results)} results, engine={engine})\n']
        for i, r in enumerate(results):
            lines.append(f"## {i+1}. {r['title']}")
            lines.append(f"URL: {r['url']}")
            if r.get("snippet"):
                lines.append(r["snippet"][:300])
            lines.append("")

        output = "\n".join(lines)
        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + f"\n... [truncated, {len(results)} results total]"

        return ToolResult(ok=True, output=output, data={"results": results, "engine": engine})


async def _bing_search(query: str, max_results: int) -> list[dict]:
    """Bing 搜索（默认引擎，直连可用）。"""
    url = (
        "https://www.bing.com/search"
        f"?q={quote_plus(query)}&count={max_results}"
        "&mkt=zh-CN&setlang=zh-hans&cc=CN&FORM=QBLH"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with build_http_client(
            timeout=15, follow_redirects=True, headers={"Accept-Encoding": "gzip, deflate"}
        ) as client:
            resp = await client.get(url, headers=headers)
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    return _parse_bing(resp.text, max_results)


def _real_url(href: str) -> str:
    """Bing 的 /ck/a 跳转链接 → 真实目标 URL。

    u 参数 = "a1" 前缀 + base64url（无 padding），如 a1aHR0cHM6Ly93d3cueWp3dWppYW4uY24v。
    """
    if "/ck/a" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if not u:
        return href
    raw = u[2:] if u.startswith("a1") else u
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.b64decode(padded).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return href


def _parse_bing(page: str, max_results: int) -> list[dict]:
    """解析 Bing 搜索结果页面 HTML。"""
    results = []

    block_re = re.compile(
        r'<li[^>]+class="b_algo"[^>]*>(.*?)</li>',
        re.DOTALL | re.IGNORECASE,
    )
    title_re = re.compile(
        r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    snippet_re = re.compile(
        r'<p[^>]*>(.*?)</p>',
        re.DOTALL | re.IGNORECASE,
    )

    seen: set[str] = set()
    for block in block_re.findall(page)[: max_results * 2]:
        title_match = title_re.search(block)
        if not title_match:
            continue
        url = _real_url(title_match.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(2))).strip()
        if not title or url in seen:
            continue
        seen.add(url)

        snippet = ""
        snippet_match = snippet_re.search(block)
        if snippet_match:
            snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet_match.group(1))).strip()

        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break

    return results
