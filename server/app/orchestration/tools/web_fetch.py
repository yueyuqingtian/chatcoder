"""v0.3: web.fetch 工具(medium risk,走审批)。

v1.0: 增加 SSRF 防护——scheme 白名单 + IP 黑名单 + 禁止内网地址。
"""
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from app.core.http_client import build_http_client
from app.orchestration.output_paging import cap_raw
from app.orchestration.tools.base import Tool, ToolContext, ToolResult

_TIMEOUT_SEC = 15


# v1.0: SSRF 防护配置
_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 含云元数据 169.254.169.254
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
_BLOCKED_HOSTNAMES = {"localhost", "0.0.0.0", "[::1]"}


def _validate_url(url: str) -> str | None:
    """v1.0: URL 安全校验。返回错误消息或 None(合法)。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return "URL 解析失败"

    # scheme 白名单
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"不允许的协议: {parsed.scheme}，仅支持 http/https"

    hostname = parsed.hostname or ""
    if not hostname:
        return "URL 缺少主机名"

    # 禁止常见内网主机名
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        return f"禁止访问内网地址: {hostname}"

    # DNS 解析后检查 IP 黑名单
    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
        for info in addr_infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
                for net in _BLOCKED_NETWORKS:
                    if ip in net:
                        return f"禁止访问内网/保留 IP: {ip_str}"
            except ValueError:
                continue
    except socket.gaierror:
        # DNS 解析失败不阻断，让 httpx 处理
        pass

    return None


class WebFetchTool(Tool):
    name = "web_fetch"
    risk_level = "medium"
    description = "HTTP GET 抓取 URL 的文本响应(超时 15s；内容较长时完整保存并分段读取)。需用户审批。"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "完整 URL"},
                    },
                    "required": ["url"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args.get("url", "")
        if not url:
            return ToolResult(ok=False, output="", error="url 为空")

        # v1.0: SSRF 防护校验
        err = _validate_url(url)
        if err:
            return ToolResult(ok=False, output="", error=f"[SSRF 防护] {err}")

        try:
            async with build_http_client(
                timeout=_TIMEOUT_SEC, follow_redirects=True,
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as client:
                resp = await client.get(url, headers={"User-Agent": "chatcoder/1.0"})
                text = resp.text
        except Exception as e:
            # plan-20261020-pts：超时 / 连接类异常的文本常为空，旧提示只剩「抓取失败: 」——带上异常类型与改走的路
            msg = str(e).strip()
            return ToolResult(
                ok=False, output="",
                error=(
                    f"抓取失败：{type(e).__name__}{('：' + msg) if msg else ''}。"
                    "可稍后重试；页面需要执行脚本或登录时改用 browser_navigate 打开。"
                ),
            )
        # plan-156-620：不再硬截断；超出上下文预算由执行层落盘并给出续读方式
        text = cap_raw(text)
        if resp.status_code >= 400:
            text = f"[HTTP {resp.status_code}] 服务器返回错误状态，以下是响应内容：\n{text}"
        return ToolResult(
            ok=True,
            output=text,
            data={"status_code": resp.status_code, "url": url},
        )
