"""消息流内嵌可交互界面工具（plan-interactive-ui）。

设计约束：
- 纯展示、零工作区写盘副作用（risk_level="low", concurrency_safe=True），
  在 readonly / plan / agent 模式与 ta3 通道下均可直接使用；
- 支持单界面（图表 / 组件 / 页面）与多版式对比（variants[] + 单选/多选）；
- 不阻塞当前 turn：执行即落库并返回简洁摘要，用户在消息流从容交互、勾选、批注后
  再通过「选定并继续」或「填入输入框」推进下一轮。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

_ALLOWED_KINDS = frozenset({"comparison", "chart", "diagram", "component", "page"})
_ALLOWED_LAYOUTS = frozenset({"tabs", "grid"})
_MAX_VARIANTS = 8
_MAX_HTML_CHARS = 200_000


def _read_workspace_html(workspace_root: str, rel_path: str) -> tuple[str | None, str | None, str | None]:
    """只读读取工作区内 HTML/SVG 文件，越界或缺失时返回 (None, error_code, error_msg)。"""
    raw = str(rel_path or "").strip()
    if not raw:
        return None, "ARG_INVALID", "file_path 不能为空"
    if not workspace_root:
        return None, "NOT_FOUND", "当前会话未绑定工作区目录，无法读取 file_path"
    try:
        root = Path(workspace_root).resolve()
        candidate = Path(raw)
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        return None, "PERMISSION", f"file_path 超出当前工作区范围：{raw}"
    if not target.is_file():
        return None, "NOT_FOUND", f"未找到指定的界面文件：{raw}"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, "EXEC_FAILED", f"读取界面文件失败（{raw}）：{exc}"
    if len(text) > _MAX_HTML_CHARS:
        return None, "ARG_INVALID", f"界面文件过大（{len(text)} 字符），上限 {_MAX_HTML_CHARS} 字符"
    return text, None, None


class PresentUiTool(Tool):
    """在消息流原位渲染可交互 HTML 组件、图表或多版式比选卡片。"""

    name = "present_ui"
    risk_level = "low"
    concurrency_safe = True
    description = (
        "在消息流中直接向用户展示可交互的 HTML 组件、数据图表、原型页面或多版式对比选择卡。\n"
        "本工具不会向磁盘写入任何文件，在只读模式、计划模式和智能体模式下均可安全调用。\n"
        "适用场景：用户需要对比多个设计版式并勾选其一继续、体验可交互组件/调参模拟器/数据图表。\n"
        "约束：HTML 必须自包含（不可引用外网 CDN/脚本/样式，可用内联 <style>/<script> 与宿主注入的 CSS 主题变量 "
        "--bg-card/--bg-panel/--text-1/--text-2/--text-3/--border/--brand/--primary/--primary-fg）；"
        "不要使用 100vh / h-screen 撑高根容器。"
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
                        "title": {
                            "type": "string",
                            "description": "卡片标题（不超过 40 字），如「定价卡片 · 3 套版式比选」",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["comparison", "chart", "diagram", "component", "page"],
                            "description": "界面类别：comparison(多版式比选)、chart(交互图表)、diagram(架构/流程图)、component(交互组件)、page(原型页面)",
                        },
                        "description": {
                            "type": "string",
                            "description": "可选：简要说明交互操作方式或对比要点",
                        },
                        "layout": {
                            "type": "string",
                            "enum": ["tabs", "grid"],
                            "description": "多版式展示布局：tabs(标签页精看，默认) 或 grid(并排网格对比)",
                        },
                        "selection": {
                            "type": "object",
                            "description": "可选：用户勾选配置。多版式未传时默认启用单选。",
                            "properties": {
                                "enabled": {"type": "boolean"},
                                "mode": {"type": "string", "enum": ["single", "multiple"]},
                                "confirm_label": {"type": "string"},
                                "allow_note": {"type": "boolean"},
                            },
                        },
                        "variants": {
                            "type": "array",
                            "maxItems": _MAX_VARIANTS,
                            "description": "1~8 个版式/界面项。单界面也可直接在顶层传 html。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "版式唯一标识，如 v1 / minimal"},
                                    "label": {"type": "string", "description": "版式名称，如「A · 极简标准」"},
                                    "summary": {"type": "string", "description": "可选：该版式的设计亮点与适用场景"},
                                    "html": {"type": "string", "description": "自包含 HTML/CSS/JS 源码（无需外链）"},
                                    "file_path": {"type": "string", "description": "可选：工作区内已有 HTML 文件相对路径（与 html 二选一）"},
                                    "height": {"type": "integer", "description": "可选：建议初始高度(px)"},
                                },
                            },
                        },
                        "html": {
                            "type": "string",
                            "description": "单界面快捷参数：等价于传入 1 个 variant（与 variants 二选一）",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "单界面快捷参数：工作区内已有 HTML 文件路径（与 html / variants 互斥）",
                        },
                    },
                    "required": ["title"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = str(args.get("title") or "").strip()[:80] or "交互界面"
        kind = str(args.get("kind") or "component").strip().lower()
        if kind not in _ALLOWED_KINDS:
            kind = "component"
        description = str(args.get("description") or "").strip()[:500]
        layout = str(args.get("layout") or "tabs").strip().lower()
        if layout not in _ALLOWED_LAYOUTS:
            layout = "tabs"

        raw_variants = args.get("variants")
        if not isinstance(raw_variants, list) or not raw_variants:
            single_html = args.get("html")
            single_path = args.get("file_path")
            if isinstance(single_html, str) and single_html.strip():
                raw_variants = [{"id": "v1", "label": title, "html": single_html}]
            elif isinstance(single_path, str) and single_path.strip():
                raw_variants = [{"id": "v1", "label": title, "file_path": single_path}]
            else:
                return ToolResult(
                    ok=False,
                    output="",
                    error_code="ARG_INVALID",
                    error="必须提供 variants（至少 1 项）或在顶层提供 html / file_path。",
                )

        if len(raw_variants) > _MAX_VARIANTS:
            raw_variants = raw_variants[:_MAX_VARIANTS]

        normalized_variants: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, item in enumerate(raw_variants):
            if not isinstance(item, dict):
                return ToolResult(
                    ok=False,
                    output="",
                    error_code="ARG_INVALID",
                    error=f"variants[{idx}] 必须是对象。",
                )
            vid = str(item.get("id") or f"v{idx + 1}").strip()[:40] or f"v{idx + 1}"
            if vid in seen_ids:
                vid = f"{vid}_{idx + 1}"
            seen_ids.add(vid)

            label = str(item.get("label") or f"方案 {idx + 1}").strip()[:60]
            summary = str(item.get("summary") or "").strip()[:400]

            html = item.get("html")
            file_path = item.get("file_path")
            if isinstance(html, str) and html.strip():
                html_text = html
                if len(html_text) > _MAX_HTML_CHARS:
                    return ToolResult(
                        ok=False,
                        output="",
                        error_code="ARG_INVALID",
                        error=f"版式「{label}」HTML 超过 {_MAX_HTML_CHARS} 字符上限。",
                    )
            elif isinstance(file_path, str) and file_path.strip():
                loaded, err_code, err_msg = _read_workspace_html(ctx.workspace_root, file_path)
                if loaded is None:
                    return ToolResult(
                        ok=False,
                        output="",
                        error_code=err_code or "EXEC_FAILED",
                        error=err_msg or f"读取 {file_path} 失败",
                    )
                html_text = loaded
            else:
                return ToolResult(
                    ok=False,
                    output="",
                    error_code="ARG_INVALID",
                    error=f"版式「{label}」缺少 html 或 file_path。",
                )

            v_out: dict[str, Any] = {
                "id": vid,
                "label": label,
                "html": html_text,
            }
            if summary:
                v_out["summary"] = summary
            if isinstance(file_path, str) and file_path.strip():
                v_out["file_path"] = file_path.strip()
            raw_h = item.get("height")
            if isinstance(raw_h, (int, float)) and 80 <= int(raw_h) <= 1200:
                v_out["height"] = int(raw_h)
            normalized_variants.append(v_out)

        raw_sel = args.get("selection") if isinstance(args.get("selection"), dict) else {}
        multi = len(normalized_variants) > 1
        sel_enabled = bool(raw_sel.get("enabled")) if "enabled" in raw_sel else multi
        sel_mode = str(raw_sel.get("mode") or "single").strip().lower()
        if sel_mode not in ("single", "multiple"):
            sel_mode = "single"
        confirm_label = str(raw_sel.get("confirm_label") or "选定并继续").strip()[:30] or "选定并继续"
        allow_note = bool(raw_sel.get("allow_note")) if "allow_note" in raw_sel else True

        ui_payload = {
            "version": 1,
            "title": title,
            "kind": kind,
            "description": description,
            "layout": layout,
            "selection": {
                "enabled": sel_enabled,
                "mode": sel_mode,
                "confirm_label": confirm_label,
                "allow_note": allow_note,
            },
            "variants": normalized_variants,
        }

        labels_text = " / ".join(v["label"] for v in normalized_variants)
        summary_out = (
            f"已在消息流展示交互界面「{title}」（{len(normalized_variants)} 项：{labels_text}）。"
            + ("用户可直接交互体验、勾选心仪版式并附带意见发送下一轮指令。" if sel_enabled else "用户可直接体验并与界面交互。")
        )
        return ToolResult(ok=True, output=summary_out, data={"ui_payload": ui_payload})
