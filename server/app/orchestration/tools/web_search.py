"""web_search 工具 — 联网搜索。

v1.0: 改用 Bing 搜索（国内可达），替代 DuckDuckGo（国内无法访问）。
"""
import re
from urllib.parse import quote_plus

import httpx

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

_MAX_RESULTS = 8
_MAX_OUTPUT = 6000


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web for real-time information. Returns titles, URLs, and snippets."
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

        max_results = min(15, max(1, args.get("max_results", _MAX_RESULTS)))

        # v1.0: 使用 Bing 搜索（国内可达）
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={max_results}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"Search failed: {e}")

        if resp.status_code != 200:
            return ToolResult(ok=False, output="", error=f"Search failed: HTTP {resp.status_code}")

        results = _parse_bing(resp.text, max_results)
        if not results:
            return ToolResult(ok=True, output=f'No results for "{query}"', data={"results": []})

        lines = [f'Web search: "{query}" ({len(results)} results)\n']
        for i, r in enumerate(results):
            lines.append(f"## {i+1}. {r['title']}")
            lines.append(f"URL: {r['url']}")
            if r.get("snippet"):
                lines.append(r["snippet"][:300])
            lines.append("")

        output = "\n".join(lines)
        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + f"\n... [truncated, {len(results)} results total]"

        return ToolResult(ok=True, output=output, data={"results": results})


def _parse_bing(html: str, max_results: int) -> list[dict]:
    """解析 Bing 搜索结果页面 HTML。"""
    results = []

    # Bing 结果块: <li class="b_algo"> 内含 <h2><a href="...">Title</a></h2> 和 <p>snippet</p>
    # 匹配每个搜索结果块
    block_re = re.compile(
        r'<li[^>]+class="b_algo"[^>]*>(.*?)</li>',
        re.DOTALL | re.IGNORECASE,
    )
    # 匹配标题和链接
    title_re = re.compile(
        r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    # 匹配摘要
    snippet_re = re.compile(
        r'<p[^>]*>(.*?)</p>',
        re.DOTALL | re.IGNORECASE,
    )

    blocks = block_re.findall(html)
    for block in blocks[:max_results]:
        title_match = title_re.search(block)
        if not title_match:
            continue
        url = title_match.group(1)
        title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
        if not title:
            continue

        snippet = ""
        snippet_match = snippet_re.search(block)
        if snippet_match:
            snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()

        results.append({"title": title, "url": url, "snippet": snippet})

    return results
