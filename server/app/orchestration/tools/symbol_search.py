"""symbol_search 工具（plan-230-1144 M3）。

解决"项目探索只能靠 fs_list + fs_grep + fs_read 反复扫描"：按符号名（函数/类/
方法/接口等）直接定位 file:line 三元组，返回的 line_start/line_end 可直接喂给
fs_read(offset, limit) 精读，无需大范围扫描。

实现：symbol_index_service 的 SQLite 符号索引（Python ast + 多语言正则提取），
首次调用自动建索引，之后增量更新（仅重解析变化文件）。
low risk 免审批，四种权限模式全部可用。

plan-548-2276（阶段 4.3）：索引开关默认关闭是产品决定（用户能关的能力不能被悄悄打开），
但此前未开启时只能让模型转告用户「请到设置页开启」——模型自己没法继续，只能中断任务。
本模块新增 `symbol_index_enable`（low risk）作为可执行出口：模型可直接开启并触发后台建索引。
"""
import logging
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_KIND_LABEL = {
    "function": "函数", "method": "方法", "class": "类",
    "interface": "接口", "type": "类型", "enum": "枚举", "struct": "结构体",
}


class SymbolSearchTool(Tool):
    name = "symbol_search"
    concurrency_safe = True  # plan-522-2209：只查符号索引，读到的是同一份只读视图
    risk_level = "low"
    description = (
        "在代码库的项目符号索引中搜索函数/方法/类/接口等符号，返回"
        "文件路径:符号名:起止行号 + 签名，可直接用 fs_read 按行精读。"
        "当需要定位某个函数/类定义、查找某功能的实现位置时优先使用本工具，"
        "比 fs_grep 全库文本扫描更快更准。首次调用会自动建立索引（稍慢），之后增量更新。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "符号名（支持部分匹配，如 build_main / Session）"},
                        "kind": {
                            "type": "string",
                            "description": "可选过滤：function / method / class / interface / type / enum / struct",
                        },
                        "file_glob": {"type": "string", "description": "可选文件过滤，如 *.py、src/**/*.ts"},
                        "limit": {"type": "integer", "description": "返回条数(默认 20，最大 100)"},
                    },
                    "required": ["query"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="", error="query 不能为空")
        kind = str(args.get("kind") or "").strip() or None
        file_glob = str(args.get("file_glob") or "").strip() or None
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20

        workspace = str(ctx.workspace_root or "")
        if not workspace:
            return ToolResult(ok=False, output="", error="无工作区上下文")

        # plan-248-1258 M3.3: 尊重「每工作区索引开关」——
        # 已启用：增量维护后检索；未启用：不自动建库（避免"默认关闭"失效），
        # 返回可执行的开启指引，让 AI 在会话中告知用户（需求：开启后 AI 能自动识别并使用）。
        state = await self._state_async(workspace)
        if not state.get("enabled"):
            return ToolResult(
                ok=False, output="",
                error=(
                    "该项目尚未开启代码符号索引。"
                    "现在可以直接调用 symbol_index_enable 为当前工作区开启"
                    "（会在后台建立索引，无需让用户去设置页手动开）；"
                    "开启后即可用 symbol_search/outline 快速定位函数与文件结构。"
                ),
                data={"index_enabled": False, "workspace": workspace},
            )

        # 索引只由独立 worker 维护；主服务只读已有 symbols.db，绝不在请求内启动扫描。
        state = await self._state_async(workspace)
        if state.get("status") == "indexing":
            return ToolResult(ok=True, output="符号索引正在后台建立，请稍后重试；当前请求不会阻塞主服务。", data={"indexing": True})
        hits = await self._search_async(workspace, query, kind=kind, file_glob=file_glob, limit=limit)

        if not hits:
            return ToolResult(
                ok=True,
                output=(
                    f"未找到匹配 '{query}' 的符号。"
                    "（提示：可放宽为部分名称；若代码是新写文件，索引会在下次调用时自动增量更新）"
                ),
                data={"query": query, "count": 0},
            )

        lines = [f"找到 {len(hits)} 个匹配 '{query}' 的符号（file:line 可直接用 fs_read 精读）:"]
        for h in hits:
            label = _KIND_LABEL.get(h["kind"], h["kind"])
            qn = f" ({h['qualified_name']})" if h.get("qualified_name") and h["qualified_name"] != h["name"] else ""
            doc = f" — {h['doc']}" if h.get("doc") else ""
            lines.append(
                f"- {h['file_path']}:{h['line_start']}-{h['line_end']} [{label}] {h['name']}{qn}"
                f"\n    {h.get('signature') or ''}{doc}"
            )
        return ToolResult(ok=True, output="\n".join(lines), data={"query": query, "count": len(hits)})

    # sqlite 为同步 IO：放线程池执行，避免阻塞事件循环
    async def _state_async(self, workspace: str) -> dict:
        import asyncio

        from app.services import symbol_index_manager as sim
        return await asyncio.to_thread(sim.get_state, workspace)

    async def _search_async(self, workspace: str, query: str, **kw):
        import asyncio

        from app.services import symbol_index_service as sis
        return await asyncio.to_thread(sis.search_symbols, workspace, query, **kw)


class SymbolIndexEnableTool(Tool):
    """为当前工作区开启符号索引并触发后台建立（plan-548-2276 阶段 4.3）。

    只开启当前工作区，不改变全局默认；重复调用幂等（symbol_index_manager.enable 本身幂等）。
    """

    name = "symbol_index_enable"
    risk_level = "low"
    # 会写索引开关状态并派发后台 worker，不是只读操作 → 不参与并发
    concurrency_safe = False
    description = (
        "为当前工作区开启代码符号索引并触发后台建立；"
        "开启后可用 symbol_search / outline 快速定位。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        workspace = str(ctx.workspace_root or "")
        if not workspace:
            return ToolResult(ok=False, output="", error="无工作区上下文", error_code="ARG_INVALID")

        from app.services import symbol_index_manager as sim

        try:
            # enable 是 async 且内部已负责建库/派发 worker，这里直接 await；
            # 不阻塞等待索引完成——大仓库全量解析可能几分钟，同步等等于卡死会话。
            state = await sim.enable(workspace)
        except Exception as e:  # noqa: BLE001
            logger.warning("[symbol_index_enable] 开启失败 ws=%s", workspace, exc_info=True)
            return ToolResult(
                ok=False, output="",
                error=f"开启代码符号索引失败：{e}", error_code="EXEC_FAILED",
            )

        status = str(state.get("status") or "")
        if status == "ready":
            output = "该项目已开启代码符号索引且索引已就绪，可直接使用 symbol_search / outline。"
        else:
            output = (
                "已为当前工作区开启代码符号索引，后台正在建立"
                f"（状态：{status or 'indexing'}）。可稍后重试 symbol_search；"
                "首次索引大仓库需要数秒到数分钟，期间可继续用 fs_grep / codebase_search。"
            )
        return ToolResult(ok=True, output=output, data={"index_enabled": True, **state})


class OutlineTool(Tool):
    name = "outline"
    concurrency_safe = True  # plan-522-2209：只查符号索引，读到的是同一份只读视图
    risk_level = "low"
    description = (
        "查看单个文件的符号骨架（类/函数/方法及其行号区间，按定义顺序列出）。"
        "当需要了解一个大文件的结构、决定读哪一段时使用；先 outline 再 fs_read "
        "按行精读，避免整文件通读浪费上下文。"
    )

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径（工作区内相对路径或绝对路径）"},
                    },
                    "required": ["path"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = str(args.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, output="", error="path 不能为空")

        workspace = str(ctx.workspace_root or "")
        if not workspace:
            return ToolResult(ok=False, output="", error="无工作区上下文")

        import asyncio
        from pathlib import Path

        from app.services import symbol_index_manager as sim
        from app.services import symbol_index_service as sis

        # plan-248-1258 M3.3: 未开启索引时不自动建库，返回开启指引
        state = await asyncio.to_thread(sim.get_state, workspace)
        if not state.get("enabled"):
            return ToolResult(
                ok=False, output="",
                error=(
                    "该项目尚未开启代码符号索引。"
                    "可直接调用 symbol_index_enable 为当前工作区开启后，"
                    "再用 outline 查看文件结构（无需让用户去设置页）。"
                ),
                data={"index_enabled": False},
            )

        # 统一为工作区内相对路径（索引按相对路径存储）
        rel = path
        try:
            p = Path(path)
            rel = p.relative_to(Path(workspace)).as_posix() if p.is_absolute() else p.as_posix()
        except ValueError:
            rel = path.replace("\\", "/")

        # 文件变更由独立 worker 的自动增量任务处理；outline 只读取当前快照。
        syms = await asyncio.to_thread(sis.outline_file, workspace, rel)
        if not syms:
            return ToolResult(
                ok=True,
                output=f"文件 {rel} 无符号记录（可能是空文件、非代码文件或尚未索引）。",
                data={"path": rel, "count": 0},
            )

        # 树状缩进：按 parent_id 链计算层级
        by_id = {s["id"]: s for s in syms}
        lines = [f"{rel} 共 {len(syms)} 个符号:"]
        for s in syms:
            depth = 0
            pid = s.get("parent_id")
            guard = 0
            while pid is not None and guard < 10:
                depth += 1
                pid = by_id.get(pid, {}).get("parent_id")
                guard += 1
            label = _KIND_LABEL.get(s["kind"], s["kind"])
            lines.append(
                f"{'  ' * depth}- [{label}] {s['name']}  L{s['line_start']}-{s['line_end']}"
                f"  {s.get('signature') or ''}"
            )
        return ToolResult(ok=True, output="\n".join(lines), data={"path": rel, "count": len(syms)})
