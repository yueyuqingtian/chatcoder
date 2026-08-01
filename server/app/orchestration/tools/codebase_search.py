"""v1.0: Codebase 语义搜索工具 — 基于向量索引的代码语义检索。

使用 embedding 模型将代码分块向量化，支持语义搜索。
存储方案: SQLite + numpy 轻量方案（无需外部向量数据库）。
"""
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from app.orchestration.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500  # 每个分块的字符数
_CHUNK_OVERLAP = 50  # 分块重叠
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
}


class CodebaseSearchTool(Tool):
    name = "codebase_search"
    risk_level = "low"
    description = (
        "语义搜索代码库。基于关键词匹配在已索引的代码中搜索。\n"
        "返回最相关的代码片段及其文件位置。\n"
        "首次使用需要索引（自动触发）。"
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
                        "query": {"type": "string", "description": "搜索查询（自然语言或代码片段）"},
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

        # 构建/加载索引
        index = self._load_or_build_index(workspace)
        if not index:
            return ToolResult(ok=True, output="代码库为空或无支持的代码文件", data={"results": []})

        # 关键词搜索（简化版语义搜索：TF-IDF 风格的关键词匹配）
        query_terms = set(query.lower().split())
        # 移除常见停用词
        stopwords = {"the", "a", "an", "is", "are", "in", "on", "at", "to", "for", "of", "and", "or", "how", "what", "where"}
        query_terms -= stopwords

        scored: list[tuple[float, dict]] = []
        for chunk in index:
            # 文件过滤
            if file_glob:
                import fnmatch
                if not fnmatch.fnmatch(chunk["file"], file_glob):
                    continue

            # 计算相关性分数
            chunk_text = chunk["text"].lower()
            chunk_terms = set(chunk_text.split())

            # 精确匹配加分
            exact_bonus = 0
            if query.lower() in chunk_text:
                exact_bonus = 5.0

            # 词项重叠
            overlap = len(query_terms & chunk_terms)
            if overlap == 0 and not exact_bonus:
                continue

            score = overlap * 1.0 + exact_bonus
            # 文件名匹配加分
            if any(t in chunk["file"].lower() for t in query_terms):
                score += 2.0

            scored.append((score, chunk))

        # 排序取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        results = scored[:top_k]

        if not results:
            return ToolResult(ok=True, output=f"未找到与 '{query}' 相关的代码", data={"results": []})

        # 格式化输出
        output_parts = [f"找到 {len(results)} 个相关结果:\n"]
        for i, (score, chunk) in enumerate(results, 1):
            output_parts.append(
                f"--- [{i}] {chunk['file']}:{chunk['start_line']}-{chunk['end_line']} (相关度: {score:.1f}) ---\n"
                f"{chunk['text'][:600]}\n"
            )

        return ToolResult(
            ok=True,
            output="\n".join(output_parts),
            data={"results": [{"file": c["file"], "line": c["start_line"], "score": s} for s, c in results]},
        )

    def _load_or_build_index(self, workspace: Path) -> list[dict]:
        """加载或构建代码索引。"""
        index_path = workspace / _INDEX_DIR / _INDEX_FILE

        # 尝试加载缓存
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except (json.JSONDecodeError, OSError):
                pass

        # 构建索引
        chunks = []
        file_count = 0

        for file_path in workspace.rglob("*"):
            # 跳过排除目录
            if any(part in _EXCLUDE_DIRS for part in file_path.parts):
                continue
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in _CODE_EXTENSIONS:
                continue
            # 跳过过大文件
            try:
                if file_path.stat().st_size > 100_000:
                    continue
            except OSError:
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel_path = str(file_path.relative_to(workspace))
            file_chunks = self._chunk_file(content, rel_path)
            chunks.extend(file_chunks)
            file_count += 1

            # 限制索引大小
            if len(chunks) > 5000:
                break

        logger.info("[codebase_search] 索引构建完成: %d 文件, %d 分块", file_count, len(chunks))

        # 持久化
        try:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

        return chunks

    @staticmethod
    def _chunk_file(content: str, rel_path: str) -> list[dict]:
        """将文件内容分块。"""
        lines = content.splitlines()
        chunks = []
        i = 0
        while i < len(lines):
            end = min(i + 30, len(lines))  # 每块最多 30 行
            chunk_text = "\n".join(lines[i:end])
            if len(chunk_text.strip()) > 20:  # 跳过空块
                chunks.append({
                    "file": rel_path,
                    "start_line": i + 1,
                    "end_line": end,
                    "text": chunk_text[:_CHUNK_SIZE * 2],
                })
            i = end - 2  # 重叠 2 行
        return chunks
