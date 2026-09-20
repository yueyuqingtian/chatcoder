"""内置 MCP 服务端框架（plan-282-1441 #7/#8）。

为什么自建而不是引入 mcp SDK：本项目对 MCP server 的调用面很窄——只用到
`initialize` / `notifications/initialized` / `tools/list` / `tools/call`
四个 JSON-RPC 方法（见 orchestration/tools/mcp_wrapper.py 与 skill_scanner.fetch_mcp_tools）。
自建一个 ~150 行的 stdio 循环，可以避免给打包产物增加依赖，也便于把风险等级
（annotations）与结构化结果直接按需求输出。

每个内置服务是一个可执行模块，按 `python -m app.mcp_servers.<name>` 启动，
通过 `McpServer.command=<python> / args=["-m","app.mcp_servers.<name>"]` 注册。
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

# 工具处理函数签名：入参 args，返回 (文本输出, 结构化数据)
ToolHandler = Callable[[dict], "tuple[str, dict | None]"]


class ToolSpec:
    """一个工具的元信息 + 处理函数。"""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: ToolHandler,
        *,
        risk_level: str = "medium",
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        # 本服务自报的风险等级：mcp_wrapper 会优先采用它（而不是按名字猜）。
        # low = 只读免审批；medium = 需审批；high = 危险，需审批。
        self.risk_level = risk_level

    def to_mcp(self) -> dict:
        """输出为 MCP tools/list 的条目（annotations 携带本服务自报的风险）。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "riskLevel": self.risk_level,
                "readOnlyHint": self.risk_level == "low",
            },
        }


class McpServerRuntime:
    """极简 MCP stdio 服务端。

    协议要点（与 mcp_wrapper 客户端严格对齐）：
      - 请求/响应均为**单行 JSON**（客户端按行 readline 解析）；
      - `initialize` 必须回一个带 id 的响应，否则客户端握手超时；
      - `notifications/initialized` 是通知（无 id），**不得**回响应；
      - 未知方法返回 JSON-RPC 错误，而不是静默丢弃（便于排障）。
    """

    def __init__(self, name: str, version: str, tools: list[ToolSpec]) -> None:
        self.name = name
        self.version = version
        self.tools = {t.name: t for t in tools}

    # ── 协议处理 ──

    def _write(self, payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _result(self, req_id: Any, result: dict) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def handle(self, req: dict) -> None:
        method = req.get("method")
        req_id = req.get("id")

        if method == "initialize":
            # 回显客户端要求的 protocolVersion（客户端不校验，但这是规范做法）
            params = req.get("params") or {}
            self._result(req_id, {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            })
            return

        if method in ("notifications/initialized", "initialized"):
            # 通知：无 id、不应答
            return

        if method == "tools/list":
            self._result(req_id, {"tools": [t.to_mcp() for t in self.tools.values()]})
            return

        if method == "tools/call":
            params = req.get("params") or {}
            tool_name = str(params.get("name") or "")
            args = params.get("arguments")
            if not isinstance(args, dict):
                args = {}
            spec = self.tools.get(tool_name)
            if spec is None:
                # 工具不存在：按 MCP 约定返回 isError 的 content（而非 JSON-RPC 错误），
                # 这样模型能看到"工具名写错了"的具体信息。
                self._result(req_id, {
                    "content": [{"type": "text", "text": f"未知工具: {tool_name}。"
                                                          f"可用工具: {', '.join(self.tools)}"}],
                    "isError": True,
                })
                return
            try:
                text, data = spec.handler(args)
                content: list[dict] = [{"type": "text", "text": text}]
                result: dict[str, Any] = {"content": content}
                # 结构化数据同时以 text 形式附带（客户端只提取 text；保留 markdown/json
                # 供模型阅读，避免额外协议字段被忽略）
                if data is not None:
                    result["structuredContent"] = data
                self._result(req_id, result)
            except Exception as e:  # noqa: BLE001 —— 单次调用失败不应终止服务
                tb = traceback.format_exc(limit=3)
                self._result(req_id, {
                    "content": [{"type": "text", "text": f"工具执行失败: {e}\n{tb}"}],
                    "isError": True,
                })
            return

        if req_id is None:
            return  # 未知通知，忽略
        self._error(req_id, -32601, f"不支持的方法: {method}")

    def serve_forever(self) -> None:
        """按行读取 stdin 直到 EOF。"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 行（日志等）直接跳过，与客户端口径一致
            if isinstance(req, dict):
                self.handle(req)


def _force_utf8_stdio() -> None:
    """把 stdio 固定为 UTF-8（MCP 协议要求）。

    打包态（PyInstaller 冻结运行时）不读 PYTHONIOENCODING，stdin/stdout 会退回系统
    本地编码（中文 Windows 下为 GBK）：中文输出被客户端按 UTF-8 解码成乱码，输出里
    只要出现 GBK 之外的字符（如调试 MCP 的 ❌）还会直接抛 UnicodeEncodeError，让整次
    工具调用失败。开发态通常已是 UTF-8，统一处理不改变行为。复用 app.core.logging
    的 _utf8_stream，避免两处编码处理逻辑漂移。
    """
    from app.core.logging import _utf8_stream

    sys.stdin = _utf8_stream(sys.stdin)
    sys.stdout = _utf8_stream(sys.stdout)
    sys.stderr = _utf8_stream(sys.stderr)


def run(name: str, version: str, build_tools: Callable[[], list[ToolSpec]]) -> None:
    """入口：构造工具并进入服务循环。"""
    _force_utf8_stdio()
    try:
        tools = build_tools()
    except Exception as e:  # noqa: BLE001
        # 构造失败时也要能响应 initialize（否则客户端只看到"握手无响应"）
        sys.stderr.write(f"[{name}] 工具构造失败: {e}\n")
        tools = []
    McpServerRuntime(name, version, tools).serve_forever()
