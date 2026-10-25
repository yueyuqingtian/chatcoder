"""plan-548-2276 阶段 4：feature_lookup 工具（功能 → 模块/文件/符号/坑点/计划文档）。

解决的问题：用户说「压缩那块有问题」，模型只知道业务词、不知道模块，只能先全库 grep。
这里把三源（项目记忆 / ai 计划文档 / 符号索引目录聚类）合成一张本地地图，
按功能描述直接返回该读哪几个文件，模型拿到路径就能 fs_read / outline 精读。

刻意不做语义向量检索：别名与路径是确定性的，可解释、可手改；错了能修。
"""
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

_MAX_LIMIT = 10


class FeatureLookupTool(Tool):
    name = "feature_lookup"
    concurrency_safe = True  # plan-522-2209：只读本地地图，命中缺失时也只写自己的缓存文件
    risk_level = "low"
    description = (
        "把「用户说的功能」映射到代码位置：按功能描述返回相关模块/文件/符号/坑点/相关计划文档。"
        "只记得「哪个功能」但不知道在哪个模块时先用它，别再全库盲搜；"
        "已知具体符号名请用 symbol_search，已知具体字符串请用 fs_grep。"
        "结果不含代码正文，拿到路径后用 fs_read / outline 精读。"
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
                        "query": {
                            "type": "string",
                            "description": (
                                "功能描述（自然语言即可，如「上下文压缩」「记忆检索」「发布更新」）"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"返回几个功能条目（默认 3，最大 {_MAX_LIMIT}）",
                        },
                        "rebuild": {
                            "type": "boolean",
                            "description": (
                                "是否强制重建功能地图（默认 false；代码/计划大改后用一次）"
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="", error="query 不能为空", error_code="ARG_INVALID")
        try:
            limit = int(args.get("limit") or 3)
        except (TypeError, ValueError):
            limit = 3
        limit = max(1, min(limit, _MAX_LIMIT))
        force_rebuild = bool(args.get("rebuild"))

        workspace = str(ctx.workspace_root or "")
        if not workspace:
            return ToolResult(ok=False, output="", error="无工作区上下文", error_code="ARG_INVALID")

        import asyncio

        from app.services import feature_map_service as fms

        entries = await asyncio.to_thread(fms.load_map, workspace)
        if force_rebuild or not entries:
            # 地图缺失：同步建一次再查（读 ai/*.md + 符号库，几十毫秒级），
            # 不让模型先吃一个「请稍后重试」。已有地图但过期：先用旧数据，后台刷新。
            try:
                await fms.rebuild(
                    workspace, db=ctx.db, session_id=ctx.session_id or None,
                )
                entries = await asyncio.to_thread(fms.load_map, workspace)
            except Exception:  # noqa: BLE001
                # 重建失败不影响降级：下面按「地图为空」给出替代工具建议
                entries = entries or []
        elif fms.map_stale(workspace):
            fms.schedule_rebuild(workspace, session_id=ctx.session_id or None)

        if not entries:
            return ToolResult(
                ok=True,
                output=(
                    "功能地图为空：当前工作区没有 ai/ 计划文档、项目级记忆与符号索引数据。"
                    "改用 fs_grep（记得字符串/业务词）或 codebase_search（关键词检索）定位。"
                ),
                data={"count": 0, "total": 0},
            )

        hits = fms.query(entries, query, limit=limit)
        if not hits:
            return ToolResult(
                ok=True,
                output=(
                    f"功能地图里没有匹配「{query}」的条目（共 {len(entries)} 条）。"
                    "可换更通用的说法（如模块名/业务词）再试，或改用 fs_grep / codebase_search。"
                ),
                data={"count": 0, "total": len(entries)},
            )

        blocks: list[str] = [
            f"功能地图匹配「{query}」{len(hits)} 条"
            "（来源：项目记忆 / ai 计划文档 / 符号索引目录聚类）:",
        ]
        for entry in hits:
            lines = [f"\n--- {entry.get('feature')} ---"]
            modules = entry.get("modules") or []
            if modules:
                lines.append("模块: " + ", ".join(str(m) for m in modules))
            files = entry.get("files") or []
            if files:
                lines.append("文件: " + ", ".join(str(f) for f in files))
            symbols = entry.get("symbols") or []
            if symbols:
                lines.append("符号: " + ", ".join(str(s) for s in symbols))
            pitfalls = entry.get("pitfalls") or []
            if pitfalls:
                lines.append("坑点/约定: " + " | ".join(str(p) for p in pitfalls))
            plans = entry.get("plans") or []
            if plans:
                lines.append("计划文档: " + ", ".join(str(p) for p in plans) + "（在 ai/ 目录）")
            paths = [str(p) for p in (entry.get("paths") or [])]
            if paths:
                # 直接给出可执行的下一步：模型复制这行就能精读，不必再猜路径
                lines.append("可执行: " + "；".join(f'fs_read(path="{p}")' for p in paths[:3]))
            blocks.append("\n".join(lines))

        return ToolResult(
            ok=True,
            output="\n".join(blocks),
            data={
                "count": len(hits),
                # plan-549-2281 复盘修复：字段名 entries 与复数语义（列表）不符、实际是
                # 计数，与 features 列表并排更容易误读，改名 total。
                "total": len(entries),
                "features": [
                    {
                        "feature": e.get("feature"),
                        "modules": e.get("modules") or [],
                        "paths": e.get("paths") or [],
                        "source": e.get("source"),
                    }
                    for e in hits
                ],
            },
        )
