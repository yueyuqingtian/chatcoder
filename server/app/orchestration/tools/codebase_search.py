"""v1.0: Codebase 搜索工具（plan-230-1144 M3 改造）。

原实现的两个确定性问题：
1. **描述与实现矛盾**：docstring 宣称"使用 embedding 模型向量化/语义搜索"，
   description 字段却自承"基于关键词匹配"——实际是 500 字符定长切块 + 关键词
   词项重叠打分，从未调用任何 embedding 模型；
2. **切块无视语法边界**：固定 30 行/500 字符切块会把函数劈成两半，命中结果是
   半截代码，AI 拿到后仍需人工拼接。

本次改造：
- 描述纠偏：如实说明"关键词检索 + 符号边界切块"，不再虚假宣传语义向量；
- 符号边界切块：优先按 symbol_index_service 的符号区间切块（命中即完整函数/类），
  符号索引不可用时回退定长切块；
- 结果附符号名（kind + name），便于 AI 直接转 fs_read 精读或 symbol_search 深挖。
"""
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500  # 回退切块的目标字符数
_CHUNK_OVERLAP = 50  # 回退切块重叠
_CHUNK_LINES = 30  # 回退切块每块最多行数
_INDEX_DIR = ".chatcoder"
_INDEX_FILE = "codebase_index.json"

# 支持的文件扩展名
_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".vue", ".svelte", ".html", ".css", ".scss",
    ".sql", ".sh", ".bash", ".yaml", ".yml", ".toml", ".json",
}

# 排除的目录
_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode",
    ".chatcoder",
}


class CodebaseSearchTool(Tool):
    name = "codebase_search"
    risk_level = "low"
    description = (
        "关键词检索代码库（非语义向量检索）。按关键词在已索引的代码块中打分，"
        "切块优先对齐函数/类符号边界，返回完整代码片段及文件位置与符号名。\n"
        "适合「记不清确切符号名、但记得关键词/注释/字符串」的场景；"
        "若已知符号名（函数名/类名）请优先用 symbol_search，更快更准。"
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
                        "query": {"type": "string", "description": "搜索关键词（自然语言或代码片段）"},
                        "top_k": {"type": "integer", "description": "返回结果数量(默认 5)"},
                        "file_glob": {"type": "string", "description": "文件过滤(如 *.py, *.ts)"},
                    },
                    "required": ["query"],
                },
            },
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = args.get("query", "")
        top_k = args.get("top_k", 5)
        file_glob = args.get("file_glob", "")

        if not query:
            return ToolResult(ok=False, output="", error="query 为空")

        workspace = Path(ctx.workspace_root)
        if not workspace.is_dir():
            return ToolResult(ok=False, output="", error=f"工作区不存在: {ctx.workspace_root}")

        # 构建/加载索引（含符号边界信息）
        index = self._load_or_build_index(workspace)
        if not index:
            return ToolResult(ok=True, output="代码库为空或无支持的代码文件", data={"results": []})

        # 关键词搜索（TF 风格词项重叠打分 + 精确匹配加分）
        query_terms = set(query.lower().split())
        stopwords = {"the", "a", "an", "is", "are", "in", "on", "at", "to", "for", "of", "and", "or", "how", "what", "where"}
        query_terms -= stopwords

        scored: list[tuple[float, dict]] = []
        for chunk in index:
            if file_glob:
                import fnmatch
                if not fnmatch.fnmatch(chunk["file"], file_glob):
                    continue

            chunk_text = chunk["text"].lower()
            chunk_terms = set(chunk_text.split())

            exact_bonus = 5.0 if query.lower() in chunk_text else 0.0
            overlap = len(query_terms & chunk_terms)
            if overlap == 0 and not exact_bonus:
                continue

            score = overlap * 1.0 + exact_bonus
            if any(t in chunk["file"].lower() for t in query_terms):
                score += 2.0
            # 符号名命中额外加分（函数名/类名直接匹配关键词时更可能是目标）
            sym = str(chunk.get("symbol") or "").lower()
            if sym and any(t in sym for t in query_terms):
                score += 3.0

            scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = scored[:top_k]

        if not results:
            return ToolResult(ok=True, output=f"未找到与 '{query}' 相关的代码", data={"results": []})

        output_parts = [f"找到 {len(results)} 个相关结果（关键词检索，非语义向量）:\n"]
        for i, (score, chunk) in enumerate(results, 1):
            sym_tag = ""
            if chunk.get("symbol"):
                sym_tag = f" [{chunk.get('symbol_kind') or 'symbol'} {chunk['symbol']}]"
            output_parts.append(
                f"--- [{i}] {chunk['file']}:{chunk['start_line']}-{chunk['end_line']}{sym_tag} "
                f"(相关度: {score:.1f}) ---\n{chunk['text'][:700]}\n"
            )

        return ToolResult(
            ok=True,
            output="\n".join(output_parts),
            data={"results": [
                {"file": c["file"], "line": c["start_line"], "score": s,
                 "symbol": c.get("symbol")}
                for s, c in results
            ]},
        )

    def _load_or_build_index(self, workspace: Path) -> list[dict]:
        """加载或构建代码索引（含符号边界信息与源文件 sha1 校验）。

        v2 (M3): 索引文件带 meta.sha1 校验——源文件变化时 JSON 索引整体重建
        （JSON 无增量能力，重建成本 ~1s 级，可接受；符号库另有 sqlite 增量索引）。
        """
        index_path = workspace / _INDEX_DIR / _INDEX_FILE
        source_files = self._collect_source_files(workspace)
        digest = hashlib.sha1("|".join(sorted(
            f"{p}:{p.stat().st_mtime_ns}" for p in source_files
        )).encode()).hexdigest()

        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                if (isinstance(data, dict) and data.get("meta", {}).get("sha1") == digest
                        and isinstance(data.get("chunks"), list) and data["chunks"]):
                    return data["chunks"]
            except (json.JSONDecodeError, OSError):
                pass

        chunks = self._build_index(workspace, source_files)

        try:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(
                json.dumps({"meta": {"sha1": digest}, "chunks": chunks}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

        return chunks

    @staticmethod
    def _collect_source_files(workspace: Path) -> list[Path]:
        files: list[Path] = []
        for file_path in workspace.rglob("*"):
            if any(part in _EXCLUDE_DIRS for part in file_path.parts):
                continue
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in _CODE_EXTENSIONS:
                continue
            try:
                if file_path.stat().st_size > 100_000:
                    continue
            except OSError:
                continue
            files.append(file_path)
        return files[:3000]

    def _build_index(self, workspace: Path, files: list[Path]) -> list[dict]:
        # 符号索引优先：把每个文件的符号区间读出，切块按其对齐
        symbols_by_file: dict[str, list[dict]] = {}
        try:
            from app.services import symbol_index_service as sis
            sis.index_workspace(workspace)  # 增量，未变化文件零成本
            for p in files:
                rel = p.relative_to(workspace).as_posix()
                syms = sis.outline_file(workspace, rel)
                if syms:
                    symbols_by_file[rel] = syms
        except Exception:
            logger.debug("[codebase_search] 符号索引不可用，回退定长切块", exc_info=True)

        chunks: list[dict] = []
        file_count = 0
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel_path = file_path.relative_to(workspace).as_posix()
            syms = symbols_by_file.get(rel_path)
            chunks.extend(self._chunk_file(content, rel_path, syms))
            file_count += 1
            if len(chunks) > 8000:
                break

        logger.info("[codebase_search] 索引构建完成: %d 文件, %d 分块（符号对齐: %s）",
                    file_count, len(chunks), bool(symbols_by_file))
        return chunks

    @staticmethod
    def _chunk_file(content: str, rel_path: str, symbols: list[dict] | None = None) -> list[dict]:
        """按符号边界切块；无符号时回退定长 30 行切块。"""
        lines = content.splitlines()
        chunks: list[dict] = []
        covered: set[int] = set()

        if symbols:
            # 只取顶层与次级符号（parent_id 为 None 或指向类），避免方法粒度太碎
            for s in symbols:
                if s.get("parent_id") is not None and (s.get("kind") or "") not in ("method",):
                    continue
                start = max(1, int(s.get("line_start") or 1))
                end = min(len(lines), int(s.get("line_end") or start))
                if end < start:
                    continue
                text = "\n".join(lines[start - 1:end])
                if len(text.strip()) <= 20:
                    continue
                # 超长符号（>400 行）拆成若干块，前 30 行带符号名
                segs = [(start, end, text)] if (end - start) < 400 else [
                    (start + i, min(end, start + i + 199), "\n".join(lines[start + i - 1:min(end, start + i + 199)]))
                    for i in range(0, end - start, 200)
                ]
                for seg_start, seg_end, seg_text in segs:
                    chunks.append({
                        "file": rel_path, "start_line": seg_start, "end_line": seg_end,
                        "text": seg_text[:_CHUNK_SIZE * 6],
                        "symbol": s.get("name"), "symbol_kind": s.get("kind"),
                    })
                    covered.update(range(seg_start, seg_end + 1))

        # 未被符号覆盖的行（模块级代码/配置/非解析语言）：定长回退切块
        i = 0
        while i < len(lines):
            if (i + 1) in covered:
                i += 1
                continue
            end = min(i + _CHUNK_LINES, len(lines))
            chunk_text = "\n".join(lines[i:end])
            if len(chunk_text.strip()) > 20:
                chunks.append({
                    "file": rel_path,
                    "start_line": i + 1,
                    "end_line": end,
                    "text": chunk_text[:_CHUNK_SIZE * 2],
                })
            i = end - 2 if end - 2 > i else end  # 重叠 2 行，防死循环

        return chunks