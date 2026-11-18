"""在临时 SQLite 中对比提交基线和当前工作区的设置接口。"""
import argparse
import asyncio
import importlib
import math
import statistics
import subprocess
import sys
import tempfile
import types
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import httpx
from fastapi import FastAPI
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))


def load_baseline(path: str, ref: str):
    suffix = path.removeprefix("server/").removesuffix(".py").replace("/", ".")
    module = types.ModuleType(suffix + "_baseline")
    module.__package__ = suffix.rsplit(".", 1)[0]
    sys.modules[module.__name__] = module
    source = subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=ROOT)
    exec(compile(source, path, "exec"), module.__dict__)
    return module


def p95(values):
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1] * 1000, 2)


async def run(args, folder: Path):
    from app.gateway.ws import manager
    from app.models.schemas import ChatMessage
    from app.orchestration.token_counter import estimate_messages_tokens
    from app.persistence.models.model_reg import Model, Provider, ProviderCredential
    from app.persistence.models.usage_record import UsageRecord

    db = create_async_engine(f"sqlite+aiosqlite:///{(folder / 'fixture.db').as_posix()}", poolclass=NullPool)
    async with db.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        for table in (Provider, Model, ProviderCredential, UsageRecord):
            await conn.run_sync(table.__table__.create)
        await conn.exec_driver_sql("CREATE TABLE bench_writes (id INTEGER PRIMARY KEY, body TEXT)")
        for i in range(1, 25):
            await conn.execute(insert(Provider), [{"id": i, "name": f"供应商 {i}"}])
            await conn.execute(insert(ProviderCredential), [{"provider_id": i}] * 3)
            await conn.execute(insert(Model), [
                {"id": i, "provider_id": i, "name": f"模型 {i}", "source_type": "openai"}
            ])
        anchor = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=12)
        for offset in range(0, args.rows, 1000):
            batch = [
                {"created_at": anchor - timedelta(seconds=i * 53), "model_id": 1 + i % 24,
                 "provider_name": "" if i % 3 else f"供应商 {1 + i % 24}",
                 "prompt_tokens": 2400, "completion_tokens": 100, "cached_tokens": i % 2000}
                for i in range(offset, min(args.rows, offset + 1000))
            ]
            await conn.execute(insert(UsageRecord), batch)

    count = ContextVar("queries", default=None)

    @event.listens_for(db.sync_engine, "before_cursor_execute")
    def count_sql(*_):
        sample = count.get()
        if sample is not None:
            sample[0] += 1

    factory = async_sessionmaker(db)

    async def inject_db():
        async with factory() as session:
            yield session

    history = [
        ChatMessage(role="user" if i % 2 == 0 else "assistant", content="本地压测上下文 " * 120)
        for i in range(320)
    ]
    config = folder / "config.json"
    config.write_text('{"language":"zh","global_rules":"本地基线规则"}', encoding="utf-8")
    endpoints = {
        "常规设置": "/settings/global", "供应商": "/providers", "用量": "/usage/stats?days=30",
    }
    columns = ("settings", "providers", "usage")
    modes = {}
    for name in ("基线", "当前"):
        app = FastAPI()
        for key in columns:
            mod = (load_baseline(f"server/app/gateway/routers/{key}.py", args.baseline)
                   if name == "基线" else importlib.import_module(f"app.gateway.routers.{key}"))
            if key == "settings":
                mod._USER_CONFIG_PATH = config
            if hasattr(mod, "get_db"):
                app.dependency_overrides[mod.get_db] = inject_db
            app.include_router(mod.router)
        history_module = (load_baseline("server/app/orchestration/history_sequence.py", args.baseline)
                          if name == "基线" else importlib.import_module("app.orchestration.history_sequence"))
        modes[name] = (app, history_module)

    async def measure(app, history_module):
        stop = asyncio.Event()
        lag, cancel = [], []

        async def stream():
            while not stop.is_set():
                for _ in range(20):
                    await manager.broadcast(991, {"event": "token.delta", "payload": {"delta": "测试输出"}})
                await asyncio.sleep(0.005)

        async def write():
            while not stop.is_set():
                async with db.begin() as conn:
                    await conn.exec_driver_sql("INSERT INTO bench_writes(body) VALUES (?)", [("工具结果" * 20,)] * 24)
                await asyncio.sleep(0.02)

        async def project():
            while not stop.is_set():
                history_module.prepare_history(history)
                estimate_messages_tokens(history)
                await asyncio.sleep(0.018)

        async def pulse():
            while not stop.is_set():
                deadline = perf_counter() + 0.01
                await asyncio.sleep(0.01)
                lag.append(max(0.0, perf_counter() - deadline))
                waiting = asyncio.create_task(asyncio.sleep(60))
                await asyncio.sleep(0)
                started = perf_counter()
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                cancel.append(perf_counter() - started)

        tasks = [asyncio.create_task(worker()) for worker in (stream, write, project, pulse)]
        response_times = defaultdict(list)
        queries = defaultdict(list)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as client:
                async def probe(label, path):
                    for _ in range(args.samples):
                        counter = [0]
                        token = count.set(counter)
                        started = perf_counter()
                        try:
                            response = await client.get(path)
                            response.raise_for_status()
                            response.json()
                        finally:
                            count.reset(token)
                        response_times[label].append(perf_counter() - started)
                        queries[label].append(counter[0])
                        await asyncio.sleep(0.01)

                await asyncio.gather(*(probe(label, path) for label, path in endpoints.items()))
        finally:
            stop.set()
            await asyncio.gather(*tasks)
        return {**{label: p95(times) for label, times in response_times.items()},
                "事件循环": p95(lag), "取消调度": p95(cancel),
                "SQL": {label: max(values) for label, values in queries.items()}}

    series = defaultdict(list)
    for index in range(args.rounds):
        for name in (("基线", "当前") if index % 2 == 0 else ("当前", "基线")):
            report = await measure(*modes[name])
            series[name].append(report)
            print(f"第 {index + 1} 轮 {name}: {report}", flush=True)
    for name, rounds in series.items():
        median = {key: statistics.median(row[key] for row in rounds) for key in rounds[0] if key != "SQL"}
        print(f"{name}各轮 P95 中位数（毫秒）: {median}")
    await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="HEAD")
    parser.add_argument("--rows", type=int, default=40_000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--samples", type=int, default=16)
    with tempfile.TemporaryDirectory(prefix="chatcoder-runtime-") as temporary:
        asyncio.run(run(parser.parse_args(), Path(temporary)))
