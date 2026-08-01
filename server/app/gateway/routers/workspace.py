"""工作目录辅助:扫描规范文档候选。"""
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/utils", tags=["utils"])

_RULE_NAMES = ("AGENTS.md", ".cursorrules", "CLAUDE.md")


@router.get("/scan-rules-docs", response_model=list[str])
async def scan_rules_docs(path: str = Query(..., description="工作目录路径")) -> list[str]:
    """扫描指定目录(根+一级子目录)下的规范文档,返回相对路径列表。"""
    root = Path(path)
    if not root.is_dir():
        return []
    found: list[str] = []
    dirs = [root]
    try:
        dirs += [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")][:8]
    except OSError:
        pass
    for d in dirs:
        for name in _RULE_NAMES:
            p = d / name
            if p.is_file():
                try:
                    found.append(str(p.relative_to(root)))
                except ValueError:
                    found.append(str(p))
    return found
