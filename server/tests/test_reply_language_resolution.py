"""回复语言解析边界处理（plan-19-82 步骤7）集成测试。

覆盖 _resolve_session_language：
1. 本轮有文本 → 按其语言
2. 本轮空/纯附件 → 回退最近一条含文本的 user 消息
3. 跳过目标续跑轮的系统提醒消息（其语言不代表用户）
4. 无任何信号 → auto
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.enums import MsgType, SenderType
from app.persistence.database import Base
from app.persistence.models.message import Message


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/lang.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _add_user_msg(db: AsyncSession, session_id: int, text: str,
                        goal_continuation: bool = False) -> None:
    content = {"text": text}
    if goal_continuation:
        content["goal_continuation"] = True
    db.add(Message(
        session_id=session_id, turn_id=1, thread_id=None,
        sender_type=SenderType.USER.value, sender_id=None,
        msg_type=MsgType.TEXT.value, content=content,
    ))
    await db.flush()


async def test_primary_text_wins(db):
    from app.orchestration.context_manager import _resolve_session_language
    assert await _resolve_session_language(db, 1, "帮我修复这个 bug") == "zh"
    assert await _resolve_session_language(db, 1, "fix this bug please") == "en"


async def test_empty_falls_back_to_recent_user_message(db):
    from app.orchestration.context_manager import _resolve_session_language
    sid = 100
    await _add_user_msg(db, sid, "请把这个函数重构一下")
    # 本轮为空（纯附件场景）→ 回退到最近用户消息（中文）
    assert await _resolve_session_language(db, sid, "") == "zh"


async def test_skips_goal_continuation_reminder(db):
    from app.orchestration.context_manager import _resolve_session_language
    sid = 200
    await _add_user_msg(db, sid, "please continue the work")   # 英文真实用户消息
    await _add_user_msg(db, sid, "[系统提醒] 会话目标尚未完成，请继续推进", goal_continuation=True)
    # 本轮为空时，最新一条是续跑提醒（写死中文），必须跳过它取真实用户语言（英文）
    assert await _resolve_session_language(db, sid, "") == "en"


async def test_no_signal_returns_auto(db):
    from app.orchestration.context_manager import _resolve_session_language
    sid = 300
    await _add_user_msg(db, sid, "12345 !!!")
    assert await _resolve_session_language(db, sid, "") == "auto"
