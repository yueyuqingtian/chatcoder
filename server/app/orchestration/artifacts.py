"""v0.3: 从 agent 产出中抽取产物并入库 Artifact。

策略:
- 抽取所有 ```code 块(带或不带语言标记),每块建一个 type=code 的 artifact。
- 从最近 fs.write 工具调用结果抽 path,建 type=file 的 artifact。
- summary 取代码块首行 + 长度信息。
"""
import re
from typing import TYPE_CHECKING

from app.services import task_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_CODE_BLOCK_RE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)


async def extract_and_persist_artifacts(
    db: "AsyncSession",
    *,
    task_id: int,
    text: str,
    write_paths: list[str] | None = None,
) -> list[int]:
    """从 text 与 fs.write 路径列表抽取产物,入库并返回 artifact id 列表。"""
    ids: list[int] = []

    # 代码块
    for i, m in enumerate(_CODE_BLOCK_RE.finditer(text), start=1):
        lang = m.group(1) or "text"
        code = m.group(2)
        first_line = code.strip().split("\n", 1)[0][:80]
        art = await task_service.create_artifact(
            db,
            task_id=task_id,
            type="code",
            title=f"代码块{i} [{lang}] {first_line}",
            storage_ref=f"inline://{task_id}/{i}",
            summary=f"{lang} 代码块,{len(code)} 字符",
        )
        ids.append(art.id)

    # 文件写入产物
    for p in write_paths or []:
        art = await task_service.create_artifact(
            db,
            task_id=task_id,
            type="file",
            title=f"文件:{p}",
            storage_ref=f"workspace://{p}",
            summary=f"由 fs.write 产出 {p}",
            files=[p],
        )
        ids.append(art.id)

    if ids:
        await task_service.attach_artifacts(db, task_id, ids)
    return ids
