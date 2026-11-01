"""plan-20261101-performance：在临时 SQLite 中对比旧全量路径和新窗口，不读取真实业务库。"""
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def benchmark(path: Path):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path.as_posix()}"
    from sqlalchemy import delete, select
    from app.persistence.database import async_session_factory, engine, Base
    from app.persistence import models  # noqa: F401
    from app.persistence.models.message import Message, Session
    from app.services.message_service import _to_out
    from app.services.message_window import page
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    reports = []
    for mb in (5, 20, 40):
        async with async_session_factory() as db:
            await db.execute(delete(Message))
            await db.execute(delete(Session))
            session = Session(title="临时性能测试")
            db.add(session)
            await db.flush()
            sid = session.id
            # ASCII 让标称 MB 与正文 UTF-8 字节数一致。
            body = "message window benchmark " * 1366
            count = mb * 1024 * 1024 // len(body)
            db.add_all([Message(session_id=sid, turn_id=i // 10 + 1, sender_type="agent", msg_type="text", content={"text": body}) for i in range(count)])
            await db.commit()
            full_times, page_times = [], []
            for iteration in range(4):
                started = time.perf_counter()
                rows = list((await db.execute(select(Message).where(Message.session_id == sid).order_by(Message.id.asc()))).scalars())
                raw_full = json.dumps([_to_out(m).model_dump() for m in rows]).encode()
                full_ms = (time.perf_counter() - started) * 1000
                # 避免 ORM identity map 给第二个方案带来额外命中。
                del rows
                db.expunge_all()
                started = time.perf_counter()
                raw_page = json.dumps(await page(db, sid)).encode()
                page_ms = (time.perf_counter() - started) * 1000
                if iteration:
                    full_times.append(full_ms); page_times.append(page_ms)
            report = {"size_mb": mb, "messages": count, "full_ms_median": round(statistics.median(full_times), 2),
                      "page_ms_median": round(statistics.median(page_times), 2), "full_bytes": len(raw_full), "page_bytes": len(raw_page),
                      "transfer_reduction_percent": round((1 - len(raw_page) / len(raw_full)) * 100, 2)}
            reports.append(report)
            print(json.dumps(report), flush=True)
    await engine.dispose()
    return reports


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="chatcoder-window-benchmark-") as folder:
        reports = asyncio.run(benchmark(Path(folder) / "benchmark.db"))
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
