"""plan-1075: 多轮计划迭代"AI 语义继承与合并"——注入层行为测试。

覆盖（对应方案文档验收标准 1）：
- 多轮混合状态（done/superseded/confirmed/proposed）下，所有未完结轮
  "状态行+用户需求+文档正文全文"完整出现在注入文本中；done 轮仅标题+需求行。
- 预算超限时最先消失的是最早已完结轮正文（整块丢弃），未完结轮永不因预算截断
  （可突破 plan_history_inject_chars 预算整体注入）。
- summary_only 模式：未完结轮不含文档正文，仅状态行+需求行；exclude_turn_id 生效。
- 头部继承规则行存在（注入文案与提示词对齐 Collect/Merge/Replan 语义）。
- 单轮正文上限 plan_history_open_doc_chars：超限尾部截断并标注 fs_read 补齐。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration.context_manager import (
    _PLAN_HISTORY_RULE_HEADER,
    _collect_plan_history,
)
from app.persistence.database import Base
from app.persistence.models import Turn  # noqa: F401 注册全部模型到 Base.metadata
from app.persistence.models.task import Task
from app.persistence.models.turn import Turn as TurnModel


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/plan1075.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _mk_plan(ws, name, text):
    p = ws / "ai" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


async def _mk_plan_turns(db, session_id, specs):
    """specs: [(turn_id, status, doc_path, req_title, req_desc)]"""
    for tid, status, doc_path, title, desc in specs:
        db.add(TurnModel(id=tid, session_id=session_id, status="completed",
                         plan_doc_path=doc_path, plan_status=status))
        db.add(Task(session_id=session_id, turn_id=tid, kind="request",
                    title=title, description=desc))
    await db.commit()


async def test_open_rounds_fulltext_priority_over_budget(db, workspace, monkeypatch):
    """多轮混合：未完结轮状态行+需求+正文全文完整出现；done 轮仅标题+需求行；
    预算压到极小（1200）时未完结轮全文仍完整（突破预算），done 轮整块被降级丢弃。"""
    from app.core.config import settings as app_settings

    _mk_plan(workspace, "chatcoder-plan-7-1.md", "# 方案一\n已完成需求X\n" + "长" * 800)
    _mk_plan(workspace, "chatcoder-plan-7-2.md", "# 方案二\n被取代需求Y\n" + "中" * 800)
    _mk_plan(workspace, "chatcoder-plan-7-3.md", "# 方案三\n确认执行需求Z\n" + "细" * 800)
    _mk_plan(workspace, "chatcoder-plan-7-4.md", "# 方案四\n新增需求W\n" + "新" * 800)
    await _mk_plan_turns(db, 7, [
        (1, "done", "ai/chatcoder-plan-7-1.md", "请求1", "第一轮需求"),
        (2, "superseded", "ai/chatcoder-plan-7-2.md", "请求2", "第二轮需求"),
        (3, "confirmed", "ai/chatcoder-plan-7-3.md", "请求3", "第三轮需求"),
        (4, "proposed", "ai/chatcoder-plan-7-4.md", "请求4", "第四轮需求"),
    ])
    sess = SimpleNamespace(id=7)

    # 正常预算（默认 8000）：全部轮保留
    monkeypatch.setattr(app_settings, "plan_history_inject_chars", 8000, raising=False)
    history = await _collect_plan_history(db, sess, str(workspace))
    assert _PLAN_HISTORY_RULE_HEADER in history          # 头部继承规则行
    assert "### Turn 2 [superseded]" in history
    assert "### Turn 3 [confirmed]" in history
    assert "### Turn 4 [proposed]" in history
    assert "已执行完成" in history and "待确认" in history   # 状态语义标注
    assert "新增需求W" in history and "确认执行需求Z" in history
    assert "第二轮需求" in history                        # 用户需求行
    assert "被取代需求Y" in history                       # superseded 轮正文全文
    assert "已完成需求X" not in history                   # done 轮仅标题，不给正文

    # 极小预算：未完结轮全文完整保留（突破预算），已完结轮最先被丢弃
    monkeypatch.setattr(app_settings, "plan_history_inject_chars", 1200, raising=False)
    small = await _collect_plan_history(db, sess, str(workspace))
    for marker in ("新增需求W", "确认执行需求Z", "被取代需求Y"):
        assert marker in small, f"未完结轮全文因预算被截断: {marker}"
    assert "已完成需求X" not in small                     # done 轮整块丢弃
    assert "### Turn 2 [superseded]" in small             # 状态行仍在


async def test_summary_only_and_exclude_turn(db, workspace):
    """summary_only：未完结轮仅状态行+需求行，无正文；exclude_turn_id 排除指定轮。"""
    _mk_plan(workspace, "chatcoder-plan-8-1.md", "# 方案一\n机密正文A" + "密" * 400)
    _mk_plan(workspace, "chatcoder-plan-8-2.md", "# 方案二\n正文B" + "文" * 400)
    await _mk_plan_turns(db, 8, [
        (1, "confirmed", "ai/chatcoder-plan-8-1.md", "请求1", "执行中需求"),
        (2, "proposed", "ai/chatcoder-plan-8-2.md", "请求2", "待确认需求"),
    ])
    sess = SimpleNamespace(id=8)

    summary = await _collect_plan_history(db, sess, str(workspace), summary_only=True)
    assert "### Turn 1 [confirmed]" in summary
    assert "执行中需求" in summary
    assert "机密正文A" not in summary                     # 摘要模式无正文
    assert "正文B" not in summary
    assert _PLAN_HISTORY_RULE_HEADER in summary

    excluded = await _collect_plan_history(
        db, sess, str(workspace), summary_only=True, exclude_turn_id=1,
    )
    assert "Turn 1" not in excluded                       # 当前执行轮被排除
    assert "Turn 2" in excluded


async def test_open_doc_chars_truncates_tail(db, workspace, monkeypatch):
    """单轮正文上限 plan_history_open_doc_chars：超限尾部截断并标注 fs_read 补齐。"""
    from app.core.config import settings as app_settings

    _mk_plan(workspace, "chatcoder-plan-9-1.md", "# 方案\n" + "长" * 5000)
    await _mk_plan_turns(db, 9, [
        (1, "proposed", "ai/chatcoder-plan-9-1.md", "请求1", "长文档需求"),
    ])
    monkeypatch.setattr(app_settings, "plan_history_open_doc_chars", 1000, raising=False)
    monkeypatch.setattr(app_settings, "plan_history_inject_chars", 100000, raising=False)

    history = await _collect_plan_history(db, SimpleNamespace(id=9), str(workspace))
    assert "正文因超限截断" in history
    assert "fs_read" in history
    # 头部部分保留（截头不截尾）
    assert "# 方案" in history
    # 全长 5001 字符不可能都在（1000 上限 + 标注）
    assert history.count("长") < 5000


async def test_empty_and_no_history(db, workspace):
    """无计划轮返回空串；未完结轮文档缺失时给出可读占位而非崩溃。"""
    assert await _collect_plan_history(db, SimpleNamespace(id=10), str(workspace)) == ""

    await _mk_plan_turns(db, 10, [
        (1, "proposed", "ai/chatcoder-plan-10-1.md", "请求1", "丢失文档需求"),
    ])
    history = await _collect_plan_history(db, SimpleNamespace(id=10), str(workspace))
    assert "### Turn 1 [proposed]" in history
    assert "丢失文档需求" in history
    assert "文档不存在或读取失败" in history
