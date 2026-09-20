"""压缩目标闭环（plan-19-82 步骤5）测试。

覆盖：
1. compact_session 迭代压缩，压缩后估算总占用落入目标区间（默认 10%-15%）
2. 无可压缩范围时安全退出（不死循环）
3. 固定开销超目标时返回 overhead_over_target 告警字段
4. checkpoint 滚动合并：活跃块数受限，旧块标记 merged 且原文仍可回看
5. 语言对齐：language=zh 时摘要/前言为中文
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.enums import MsgType, SenderType
from app.persistence.database import Base
from app.persistence.models.message import Message, Session
from app.services import compression_service


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/compact_target.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()
    _we.configure(None)


async def _mk_session(db: AsyncSession, ctx: dict | None = None) -> Session:
    s = Session(project_id=1, title="compact-target")
    if ctx is not None:
        s.shared_context = ctx
    db.add(s)
    await db.flush()
    await db.refresh(s)
    return s


async def _mk_long_history(db: AsyncSession, session_id: int, rounds: int = 40,
                           tool_chars: int = 3000) -> None:
    """构造长历史：user + assistant文本 + tool_call + tool_result，交替若干轮。"""
    key = 0
    for r in range(rounds):
        db.add(Message(
            session_id=session_id, turn_id=1, thread_id=None,
            sender_type=SenderType.USER.value, sender_id=None,
            msg_type=MsgType.TEXT.value, content={"text": f"用户问题 {r} " + "问" * 50},
        ))
        db.add(Message(
            session_id=session_id, turn_id=1, thread_id=None,
            sender_type=SenderType.AGENT.value, sender_id=None,
            msg_type=MsgType.TEXT.value, content={"text": f"助手说明 {r} " + "答" * 50},
        ))
        k = f"call_{key}"
        key += 1
        db.add(Message(
            session_id=session_id, turn_id=1, thread_id=None,
            sender_type=SenderType.AGENT.value, sender_id=None,
            msg_type=MsgType.TOOL_CALL.value,
            content={"tool": "fs_read", "args": {"path": f"src/f{r}.py"}, "call_key": k},
        ))
        db.add(Message(
            session_id=session_id, turn_id=1, thread_id=None,
            sender_type=SenderType.AGENT.value, sender_id=None,
            msg_type=MsgType.TOOL_RESULT.value,
            content={"tool": "fs_read", "output": "x" * tool_chars, "call_key": k},
        ))
    await db.flush()


async def test_compact_reaches_target_range(db):
    """压缩后估算总占用应落入目标区间（默认 10%-15%）。"""
    from app.orchestration.context_compressor import compact_session
    from app.orchestration.token_counter import messages_token_total

    session = await _mk_session(db, ctx={})
    await _mk_long_history(db, session.id, rounds=40, tool_chars=3000)
    await db.commit()

    from app.orchestration.context_memory import _fetch_main_messages
    all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
    total = messages_token_total(all_msgs)
    # 选取窗口使总量约为窗口的 90%（模拟触发压缩时）
    window = int(total / 0.90)

    result = await compact_session(
        db, session=session, provider=None, context_window=window,
        used_tokens=total, trigger="pressure", language="zh",
        fixed_overhead_tokens=0,
    )
    assert result is not None
    # 迭代跑过（多轮压缩）
    assert result["rounds"] >= 1
    # 压缩后占用落入目标上界内（15%）
    assert result["post_compact_tokens"] <= result["target_max_tokens"]
    assert result["post_compact_ratio"] <= 15.5
    assert result["target_reached"] is True


async def test_compact_no_range_returns_none(db):
    """无可压缩范围时返回 None（安全退出，不死循环）。"""
    from app.orchestration.context_compressor import compact_session

    session = await _mk_session(db, ctx={})
    # 仅 3 条候选（<4 阈值）
    for i in range(3):
        db.add(Message(
            session_id=session.id, turn_id=1, thread_id=None,
            sender_type=SenderType.USER.value, sender_id=None,
            msg_type=MsgType.TEXT.value, content={"text": f"m{i}"},
        ))
    await db.commit()
    result = await compact_session(db, session=session, provider=None,
                                   context_window=1000, used_tokens=900, language="zh")
    assert result is None


async def test_compact_overhead_over_target_flag(db):
    """固定开销超过目标上界时，返回 overhead_over_target=True 告警。"""
    from app.orchestration.context_compressor import compact_session
    from app.orchestration.context_memory import _fetch_main_messages
    from app.orchestration.token_counter import messages_token_total

    session = await _mk_session(db, ctx={})
    await _mk_long_history(db, session.id, rounds=30, tool_chars=3000)
    await db.commit()

    all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
    total = messages_token_total(all_msgs)
    window = int(total / 0.90)
    # 固定开销设为窗口的 20% > 目标上界 15%
    result = await compact_session(
        db, session=session, provider=None, context_window=window,
        used_tokens=total, language="zh", fixed_overhead_tokens=int(window * 0.20),
    )
    assert result is not None
    assert result["overhead_over_target"] is True
    assert result["fixed_overhead_tokens"] == int(window * 0.20)


async def test_compact_rollup_merges_old_checkpoints(db):
    """多次压缩后活跃块数受限，旧块标记 merged 且原文仍可回看。"""
    from app.orchestration.context_compressor import compact_session
    from app.orchestration.context_memory import _fetch_main_messages
    from app.orchestration.token_counter import messages_token_total, get_compact_target_max_tokens

    session = await _mk_session(db, ctx={})

    # 每次压缩前都追加一批长历史，确保每次都有可压范围（模拟多轮任务持续累积）
    async def _compact_once():
        await _mk_long_history(db, session.id, rounds=20, tool_chars=3000)
        await db.commit()
        await db.refresh(session)
        all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
        total = messages_token_total(all_msgs)
        window = max(1000, int(total / 0.90))
        # 目标上界：保证 total 明显超过它（否则无需压缩）
        if total <= get_compact_target_max_tokens(window):
            window = max(1000, int(total / 0.90))
        return await compact_session(db, session=session, provider=None,
                                     context_window=window, used_tokens=total,
                                     language="zh", fixed_overhead_tokens=0)

    first = await _compact_once()
    assert first is not None
    second = await _compact_once()
    assert second is not None
    third = await _compact_once()
    assert third is not None

    await db.refresh(session)
    entries = await compression_service.list_compaction_index(db, session.id)
    active = [e for e in entries if not e.get("merged") and not e.get("restored")]
    merged = [e for e in entries if e.get("merged")]
    assert len(active) <= 2, f"活跃块应受限，实际 {len(active)}"
    assert merged, "旧块应被标记 merged"
    # 被合并块的原文仍可回看（软阴影不物理删除）
    target = merged[0]
    msgs = await compression_service.get_compacted_messages(
        db, session.id, target["compaction_id"],
    )
    assert msgs, "已合并块的原文仍应可回看"


async def test_compact_language_zh(db):
    """language=zh 时摘要与 checkpoint 前言为中文。"""
    from app.orchestration.context_compressor import compact_session
    from app.orchestration.context_memory import _fetch_main_messages
    from app.orchestration.token_counter import messages_token_total
    from app.persistence.models.message import Message
    from sqlalchemy import select

    session = await _mk_session(db, ctx={})
    await _mk_long_history(db, session.id, rounds=30, tool_chars=3000)
    await db.commit()

    all_msgs = await _fetch_main_messages(db, session.id, limit=2000)
    total = messages_token_total(all_msgs)
    # 令总量约为窗口 90%（真实触发场景：远超目标 15%）
    window = int(total / 0.90)

    result = await compact_session(db, session=session, provider=None, context_window=window,
                                   used_tokens=total, language="zh", fixed_overhead_tokens=0)
    assert result is not None
    assert "以下是之前对话的摘要" in result["summary"]
    # SUMMARY 消息里应含中文前言
    res = await db.execute(
        select(Message).where(Message.msg_type == MsgType.SUMMARY.value)
        .order_by(Message.id.desc()).limit(1)
    )
    m = res.scalars().first()
    assert m is not None and isinstance(m.content, dict)
    assert "检查点" in m.content["text"]
