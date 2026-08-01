"""v3.6: MCP Tool Wrapper —— 将 MCP Server 的工具包装为系统 Tool 接口。

MCP Server 通过 Model Context Protocol 暴露工具，
此处将它们包装为统一的 Tool 子类，注册到 tool_registry，
让 agent_runtime 可以像调用内置工具一样调用 MCP 工具。
"""
import asyncio
import json
import logging
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class McpToolWrapper(Tool):
    """将 MCP Server 暴露的单个工具包装为系统 Tool。

    每个实例代表一个 MCP 工具，name 格式为 "mcp.{server_name}.{tool_name}"。
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict,
        server_config: dict | None = None,
    ) -> None:
        self._server_name = server_name
        self._tool_name = tool_name
        # v6.0: OpenAI function name 只允许 a-z A-Z 0-9 _ -，不允许点号
        # 用下划线替代点号: mcp.codegraph.codegraph_explore → mcp_codegraph_codegraph_explore
        safe_server = server_name.replace(".", "_").replace("-", "_")
        safe_tool = tool_name.replace(".", "_").replace("-", "_")
        self.name = f"mcp_{safe_server}_{safe_tool}"
        self._call_name = f"mcp.{server_name}.{tool_name}"  # 保留原始名用于显示
        self.description = (
            description or f"MCP 工具: {tool_name} (来自 {server_name})"
        ) + f"\n[MCP Server: {server_name}, Tool: {tool_name}]"
        self.risk_level = "medium"  # MCP 工具默认 medium 风险
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._server_config = server_config or {}

    def function_schema(self) -> dict:
        """返回 OpenAI function-calling 格式的工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._input_schema,
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行 MCP 工具调用。

        v3.6 MVP：通过 stdio/sse 连接 MCP Server 并调用工具。
        实际 MCP 协议交互较为复杂，此处提供框架实现。
        """
        transport = self._server_config.get("transport", "stdio")

        try:
            if transport == "stdio":
                result = await self._call_stdio(args, ctx)
            elif transport in ("sse", "websocket"):
                result = await self._call_remote(args, ctx)
            else:
                return ToolResult(
                    ok=False, output="",
                    error=f"不支持的 MCP 传输方式: {transport}",
                )
            return result
        except Exception as e:
            logger.exception("MCP 工具执行异常 %s", self.name)
            return ToolResult(ok=False, output="", error=f"MCP 工具异常: {e}")

    async def _call_stdio(self, args: dict, ctx: ToolContext) -> ToolResult:
        """通过 stdio 调用 MCP Server。

        v6.0 修复：正确实现 MCP 协议握手流程。
        1. 启动子进程
        2. 发送 initialize 请求，等待响应
        3. 发送 initialized 通知
        4. 发送 tools/call 请求，等待响应
        """
        command = self._server_config.get("command", "")
        cmd_args = self._server_config.get("args", [])
        env_vars = self._server_config.get("env", {})

        if not command:
            return ToolResult(ok=False, output="", error="MCP Server 未配置 command")

        import os

        full_env = {**os.environ, **env_vars}

        try:
            proc = await asyncio.create_subprocess_exec(
                command, *cmd_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
            )
        except FileNotFoundError:
            return ToolResult(ok=False, output="", error=f"MCP 命令不存在: {command}")
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"MCP 进程启动失败: {e}")

        try:
            # Step 1: 发送 initialize
            init_request = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "chatcoder", "version": "6.0"},
                },
            }
            init_resp = await self._send_and_read(proc, init_request, timeout=10)
            if not init_resp:
                stderr = await proc.stderr.read() if proc.stderr else b""
                return ToolResult(
                    ok=False, output="",
                    error=f"MCP initialize 无响应: {stderr.decode('utf-8', errors='replace')[:300]}",
                )

            # Step 2: 发送 initialized 通知（无 id，不期望响应）
            notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            proc.stdin.write((json.dumps(notif) + "\n").encode())
            await proc.stdin.drain()

            # Step 3: 发送 tools/call
            call_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": self._tool_name,
                    "arguments": args,
                },
            }
            call_resp = await self._send_and_read(proc, call_request, timeout=60, expected_id=1)
            if not call_resp:
                stderr = await proc.stderr.read() if proc.stderr else b""
                return ToolResult(
                    ok=False, output="",
                    error=f"MCP tools/call 无响应: {stderr.decode('utf-8', errors='replace')[:300]}",
                )

            # 解析结果
            result = call_resp.get("result", {})
            if call_resp.get("error"):
                err = call_resp["error"]
                return ToolResult(
                    ok=False, output="",
                    error=f"MCP 错误({err.get('code', '?')}): {err.get('message', '')}",
                )

            content = result.get("content", [])
            if isinstance(content, list):
                texts = [
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                output = "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)
            else:
                output = str(content)

            is_error = result.get("isError", False)
            return ToolResult(
                ok=not is_error,
                output=output[:8000],
                error="" if not is_error else output[:500],
            )

        except asyncio.TimeoutError:
            return ToolResult(ok=False, output="", error="MCP Server 调用超时")
        finally:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

    async def _send_and_read(
        self, proc: asyncio.subprocess.Process,
        request: dict, timeout: float = 10, expected_id: int | None = None,
    ) -> dict | None:
        """发送 JSON-RPC 请求并读取匹配的响应。

        MCP Server 可能输出多行（日志+JSON），需要按行解析找到匹配 id 的响应。
        """
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()

        import time
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=max(1, deadline - asyncio.get_event_loop().time()),
                )
            except asyncio.TimeoutError:
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                resp = json.loads(text)
            except json.JSONDecodeError:
                continue  # 跳过非 JSON 行（日志等）
            # 匹配 id（通知没有 id，跳过）
            if expected_id is not None and resp.get("id") != expected_id:
                continue
            return resp
        return None

    async def _call_remote(self, args: dict, ctx: ToolContext) -> ToolResult:
        """通过 SSE/WebSocket 调用远程 MCP Server。

        v3.6 MVP：使用 HTTP POST 简化实现。
        """
        url = self._server_config.get("url", "")
        if not url:
            return ToolResult(ok=False, output="", error="MCP Server 未配置 url")

        try:
            import aiohttp

            request_data = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": self._tool_name,
                    "arguments": args,
                },
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=request_data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return ToolResult(ok=False, output="", error=f"MCP HTTP {resp.status}")
                    data = await resp.json()
                    result = data.get("result", {})
                    content = result.get("content", [])
                    if isinstance(content, list):
                        texts = [
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        output = "\n".join(texts) if texts else str(result)
                    else:
                        output = str(content)
                    return ToolResult(ok=True, output=output[:5000])

        except Exception as e:
            return ToolResult(ok=False, output="", error=f"MCP 远程调用失败: {e}")


def build_mcp_tools_for_agent(
    mcp_servers: list,
) -> list[McpToolWrapper]:
    """为 Agent 绑定的 MCP Server 构建工具包装器列表。

    Args:
        mcp_servers: McpServer ORM 对象列表

    Returns:
        McpToolWrapper 列表（每个 MCP 工具一个）
    """
    tools: list[McpToolWrapper] = []
    for srv in mcp_servers:
        server_config = {
            "transport": srv.transport,
            "command": srv.command,
            "args": srv.args or [],
            "env": srv.env or {},
            "url": srv.url,
        }

        # 如果 MCP Server 已有缓存的 tools 列表，直接用
        if srv.tools:
            for tool_def in srv.tools:
                if not isinstance(tool_def, dict):
                    continue
                tool_name = tool_def.get("name", "")
                if not tool_name:
                    continue
                wrapper = McpToolWrapper(
                    server_name=srv.name,
                    tool_name=tool_name,
                    description=tool_def.get("description", ""),
                    input_schema=tool_def.get("inputSchema") or tool_def.get("input_schema") or {"type": "object"},
                    server_config=server_config,
                )
                # v6.0: 根据工具名/描述自动推断风险等级
                wrapper.risk_level = _infer_mcp_risk(tool_name, tool_def.get("description", ""))
                tools.append(wrapper)
        else:
            # 没有缓存的 tools 列表，创建一个通用的 MCP 调用工具
            wrapper = McpToolWrapper(
                server_name=srv.name,
                tool_name="call",
                description=f"调用 {srv.display_name or srv.name} 的 MCP 工具",
                input_schema={
                    "type": "object",
                    "properties": {
                        "method": {"type": "string", "description": "要调用的 MCP 工具名"},
                        "args": {"type": "object", "description": "工具参数"},
                    },
                    "required": ["method"],
                },
                server_config=server_config,
            )
            tools.append(wrapper)

    return tools


# 只读类 MCP 工具关键词（描述或名称中包含这些词时判定为 low 风险）
_READONLY_KEYWORDS = {
    "explore", "search", "query", "read", "get", "list", "view", "inspect",
    "graph", "analyze", "check", "status", "info", "describe", "fetch",
    "检索", "查询", "搜索", "查看", "读取", "分析", "检查",
}


def _infer_mcp_risk(tool_name: str, description: str) -> str:
    """根据工具名和描述推断风险等级。

    只读类工具（explore/search/query/read 等）设为 low（免审批），
    其他设为 medium（需审批）。
    """
    combined = f"{tool_name} {description}".lower()
    for kw in _READONLY_KEYWORDS:
        if kw in combined:
            return "low"
    return "medium"
