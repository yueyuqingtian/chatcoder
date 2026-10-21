"""plan-20261021-usage-update：/api/usage/records 调用记录、首字计时与账号快照单测。

覆盖：分页（每页 20 条）、实测 / 估算速度口径、供应商筛选、整体平均值、
流式计时（含纯工具调用）、账号快照与 Key 掩码。
使用 sqlite 临时库 + 直接调用端点函数（绕过 FastAPI 依赖）。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.routers.usage import usage_records
from app.models.base import mark_first_token
from app.models.schemas import ChatMessage, ChatRequest, Usage
from app.persistence.database import Base
from app.persistence.models.message import Message, Session as SessionModel
from app.persistence.models.model_reg import Model, Provider, ProviderCredential
from app.persistence.models.usage_record import UsageRecord
from app.services import usage_service


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/usage_records.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None).replace(microsecond=0)


def _add(db, *, created_at=None, completion=0, prompt=100, cached=0, first_token_ms=None, gen_ms=None,
         provider_name="p", model_name="m", model_id=None, session_id=None, turn_id=None, agent_id=None,
         label="", hint=""):
    rec = UsageRecord(
        model_id=model_id, model_name=model_name, provider_name=provider_name,
        prompt_tokens=prompt, completion_tokens=completion, reasoning_tokens=0, cached_tokens=cached,
        created_at=created_at or _utc_now(), first_token_ms=first_token_ms, gen_ms=gen_ms,
        session_id=session_id, turn_id=turn_id, agent_id=agent_id,
        credential_label=label, credential_hint=hint,
    )
    db.add(rec)
    return rec


async def test_pagination_is_20_per_page_newest_first(db):
    base = _utc_now() - timedelta(minutes=60)
    for i in range(45):
        _add(db, created_at=base + timedelta(seconds=i), completion=i)
    await db.commit()

    p1 = await usage_records(start=None, end=None, days=30, page=1, db=db)
    assert (p1["total"], p1["page"], p1["page_size"], p1["pages"]) == (45, 1, 20, 3)
    assert len(p1["items"]) == 20
    # 最新的在前：completion 44 → 25
    assert [it["completion"] for it in p1["items"]] == list(range(44, 24, -1))

    p3 = await usage_records(start=None, end=None, days=30, page=3, db=db)
    assert len(p3["items"]) == 5
    assert [it["completion"] for it in p3["items"]] == [4, 3, 2, 1, 0]

    # 页码越界：不报错，返回空页但总数仍在
    p9 = await usage_records(start=None, end=None, days=30, page=9, db=db)
    assert p9["items"] == [] and p9["total"] == 45


async def test_page_size_is_clamped(db):
    for _ in range(3):
        _add(db, completion=10)
    await db.commit()
    res = await usage_records(start=None, end=None, days=30, page=1, page_size=9999, db=db)
    assert res["page_size"] == 100
    res = await usage_records(start=None, end=None, days=30, page=1, page_size=0, db=db)
    assert res["page_size"] == 20  # 0 视为未传 → 默认 20


async def test_measured_speed_rules(db):
    # 实测：首字 1.2s、生成 2s、输出 300 → 150 tok/s
    _add(db, completion=300, first_token_ms=1200, gen_ms=2000)
    # 输出不足 100：展示首字 / 耗时，但不给速度
    _add(db, completion=99, first_token_ms=800, gen_ms=500)
    await db.commit()

    res = await usage_records(start=None, end=None, days=30, db=db)
    by_c = {it["completion"]: it for it in res["items"]}
    big, small = by_c[300], by_c[99]
    assert big["first_token_ms"] == 1200 and big["gen_ms"] == 2000
    assert big["speed"] == 150.0 and big["estimated"] is False
    assert small["first_token_ms"] == 800 and small["gen_ms"] == 500
    assert small["speed"] is None and small["estimated"] is False


async def test_estimated_speed_from_message_timestamps(db):
    """旧流水没有首字计时：起点取同一轮上一条用户消息 / 工具结果，只估输出 ≥ 200 的请求。"""
    sess = SessionModel(title="t")
    db.add(sess)
    await db.flush()
    t0 = datetime(2026, 10, 1, 8, 0, 0)

    def msg(mtype, sender, sender_id, at):
        m = Message(session_id=sess.id, turn_id=7, sender_type=sender, sender_id=sender_id,
                    msg_type=mtype, content={}, created_at=at.strftime("%Y-%m-%d %H:%M:%S"))
        db.add(m)

    msg("text", "user", None, t0)                              # 本轮用户消息
    msg("tool_result", "agent", 1, t0 + timedelta(seconds=30))  # 第一步的工具结果
    # 第一步调用：t0 + 10s 落库；第二步调用：t0 + 40s 落库（起点是 +30s 的工具结果 → 10s）
    _add(db, created_at=t0 + timedelta(seconds=10), completion=500, session_id=sess.id, turn_id=7, agent_id=1)
    _add(db, created_at=t0 + timedelta(seconds=40), completion=500, session_id=sess.id, turn_id=7, agent_id=1)
    # 输出不足 200：不估算
    _add(db, created_at=t0 + timedelta(seconds=45), completion=150, session_id=sess.id, turn_id=7, agent_id=1)
    await db.commit()

    res = await usage_records(start="2026-10-01", end="2026-10-01", days=30, db=db)
    items = sorted(res["items"], key=lambda it: it["time"])
    first, second, third = items
    assert first["estimated"] and first["est_ms"] == 10000 and first["speed"] == 50.0
    assert second["estimated"] and second["est_ms"] == 10000 and second["speed"] == 50.0
    assert first["first_token_ms"] is None and first["gen_ms"] is None  # 估算行没有首字计时
    assert third["speed"] is None and third["estimated"] is False


async def test_provider_filter_and_legacy_backfill(db):
    p = Provider(name="alpha")
    db.add(p)
    await db.flush()
    m = Model(name="legacy-model", provider_id=p.id, source_type="openai")
    db.add(m)
    await db.flush()
    _add(db, provider_name="alpha", model_name="m1", completion=1)
    _add(db, provider_name="beta", model_name="m2", completion=2)
    # 历史流水：没有 provider_name，经 model_id 回落到 alpha
    _add(db, provider_name="", model_name="legacy-model", model_id=m.id, completion=3)
    await db.commit()

    res = await usage_records(start=None, end=None, days=30, provider="alpha", db=db)
    assert res["total"] == 2
    assert sorted(it["completion"] for it in res["items"]) == [1, 3]
    assert {it["provider"] for it in res["items"]} == {"alpha"}
    assert res["provider"] == "alpha"

    none = await usage_records(start=None, end=None, days=30, provider="nope", db=db)
    assert none["total"] == 0 and none["items"] == [] and none["pages"] == 1


async def test_summary_covers_whole_filtered_set_not_just_page(db):
    base = _utc_now() - timedelta(minutes=30)
    for i in range(30):  # 跨两页
        _add(db, created_at=base + timedelta(seconds=i), completion=200, first_token_ms=1000, gen_ms=2000)
    # 输出不足 100 的不进平均速度，但首字照常进平均首字
    _add(db, created_at=base, completion=10, first_token_ms=4000, gen_ms=100)
    await db.commit()

    res = await usage_records(start=None, end=None, days=30, db=db)
    s = res["summary"]
    assert s["speed_calls"] == 30
    assert s["avg_speed"] == 100.0  # 30×200 token ÷ 30×2s
    assert s["avg_first_token_ms"] == int((30 * 1000 + 4000) / 31)


async def test_account_fields_are_returned(db):
    _add(db, completion=1, label="备用key", hint="sk-8••••9f2a")
    await db.commit()
    it = (await usage_records(start=None, end=None, days=30, db=db))["items"][0]
    assert it["account"] == "备用key" and it["account_hint"] == "sk-8••••9f2a"
    assert it["time"].endswith("Z")


# ── 账号快照 ──

async def test_credential_snapshot_masks_key_and_hides_oauth_placeholder(db):
    p = Provider(name="svc", api_key="sk-provider-level-1234")
    db.add(p)
    await db.flush()
    c1 = ProviderCredential(provider_id=p.id, label="主号", api_key="sk-abcdefghijkl9f2a")
    c2 = ProviderCredential(provider_id=p.id, label="月与青田", api_key="__workbuddy_session__")
    db.add_all([c1, c2])
    await db.commit()

    cid, label, hint = await usage_service.credential_snapshot(db, c1.id, p, None)
    assert (cid, label) == (c1.id, "主号")
    assert hint == "sk-a••••9f2a" and "abcdefghijkl" not in hint

    cid, label, hint = await usage_service.credential_snapshot(db, c2.id, p, None)
    assert label == "月与青田" and hint == ""  # OAuth 占位值不是真实 Key

    # 没有凭据行：回落供应商自带 Key
    cid, label, hint = await usage_service.credential_snapshot(db, None, p, None)
    assert cid is None and hint == "sk-p••••1234"


def test_mask_key_short_keys():
    assert usage_service.mask_key("") == ""
    assert usage_service.mask_key("abcd1234") == "••••34"
    assert usage_service.mask_key("sk-1234567890") == "sk-1••••7890"


# ── 流式计时 ──

class _FakeProvider:
    """按脚本吐事件：(延迟秒, 事件, 是否标记首个输出帧)。"""

    def __init__(self, steps):
        self.steps = steps

    async def stream_structured(self, request):
        for delay, ev, mark in self.steps:
            await asyncio.sleep(delay)
            if mark:
                mark_first_token(request)
            yield ev


def _req():
    return ChatRequest(messages=[ChatMessage(role="user", content="hi")], model="x")


async def _run_stream(monkeypatch, steps):
    from app.orchestration import agent_loop

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "broadcast", _noop)
    return await agent_loop._stream_chat_and_broadcast(
        _FakeProvider(steps), _req(), session_id=1, turn_id=1, agent_id=1, agent_name="a",
    )


async def test_stream_timing_from_text_events(monkeypatch):
    done = {"type": "done", "content": "hello", "thinking": None, "tool_calls": [],
            "finish_reason": "stop", "usage": Usage(completion_tokens=5)}
    resp = await _run_stream(monkeypatch, [
        (0.15, {"type": "content", "delta": "hel"}, False),
        (0.10, {"type": "content", "delta": "lo"}, False),
        (0.10, done, False),
    ])
    assert 100 <= resp.first_token_ms <= 600
    assert 120 <= resp.gen_ms <= 800


async def test_stream_timing_counts_pure_tool_call_frames(monkeypatch):
    """纯工具调用的回复没有文字事件：Provider 标记的首个输出帧要算首字。"""
    done = {"type": "done", "content": None, "thinking": None,
            "tool_calls": [{"id": "1", "name": "fs_read", "arguments": {}}],
            "finish_reason": "tool_calls", "usage": Usage(completion_tokens=50)}
    resp = await _run_stream(monkeypatch, [
        (0.10, {"type": "noop"}, True),   # Provider 在此处标记首字，但没有文字事件可吐
        (0.20, done, False),
    ])
    assert 60 <= resp.first_token_ms <= 500
    assert 150 <= resp.gen_ms <= 900


async def test_stream_without_done_has_no_timing(monkeypatch):
    resp = await _run_stream(monkeypatch, [(0.01, {"type": "content", "delta": "x"}, False)])
    assert resp.first_token_ms is None and resp.gen_ms is None


async def test_stats_perf_matches_records_summary(db):
    """总览与调用记录表的平均首字 / 平均速度必须同一口径。"""
    from app.gateway.routers.usage import usage_stats

    _add(db, completion=300, first_token_ms=1000, gen_ms=2000)
    _add(db, completion=200, first_token_ms=3000, gen_ms=2000)
    await db.commit()
    st = await usage_stats(start=None, end=None, days=30, db=db)
    rec = await usage_records(start=None, end=None, days=30, db=db)
    assert st["perf"] == rec["summary"]
    assert st["perf"]["avg_first_token_ms"] == 2000
    assert st["perf"]["avg_speed"] == 125.0  # 500 token ÷ 4s
