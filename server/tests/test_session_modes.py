"""会话模式（执行模式 × 权限模式）落盘回归测试（plan-75-332 R3）。

背景：`create_session` 的 legacy 分支（显式 INSERT）正确写了 approval_mode，
但正常分支构造 `Session(...)` 时漏传该字段，直接落到 ORM 默认值 "ask"——
表现为用户「空态首页选完全访问，发送消息后变成询问审批」。
`fork_session` 同样漏传，分支会话会丢掉源会话的两种模式（回到默认 agent + ask）。

本组测试覆盖三条写入路径：创建 / 分支 / 更新。
"""
import pytest

from app.persistence.database import async_session_factory
from app.services import session_service


@pytest.mark.asyncio
async def test_create_session_persists_approval_mode():
    """新建会话必须落准首页所选的权限模式与执行模式。"""
    async with async_session_factory() as db:
        sid = await session_service.create_session(
            db, project_id=None, title="[测试] 模式落盘",
            permission_mode="plan", approval_mode="full",
        )
        try:
            s = await session_service.get_session(db, sid)
            assert s is not None
            assert s.approval_mode == "full"
            assert s.permission_mode == "plan"
        finally:
            await session_service.delete_session_permanent(db, sid)


@pytest.mark.asyncio
async def test_create_session_defaults_to_ask():
    """未指定权限模式时回落最保守的询问审批；执行模式回落 agent（旧名 default 已废弃）。"""
    async with async_session_factory() as db:
        sid = await session_service.create_session(
            db, project_id=None, title="[测试] 默认模式",
        )
        try:
            s = await session_service.get_session(db, sid)
            assert s is not None
            assert s.approval_mode == "ask"
            assert s.permission_mode == "agent"
        finally:
            await session_service.delete_session_permanent(db, sid)


@pytest.mark.asyncio
async def test_create_session_normalizes_legacy_mode_names():
    """存量旧值（default / accept_edits）写入时归一化为 agent，不落脏值。"""
    async with async_session_factory() as db:
        sid = await session_service.create_session(
            db, project_id=None, title="[测试] 旧值归一化",
            permission_mode="accept_edits", approval_mode="danger-full-access",
        )
        try:
            s = await session_service.get_session(db, sid)
            assert s is not None
            assert s.permission_mode == "agent"
            assert s.approval_mode == "full"
        finally:
            await session_service.delete_session_permanent(db, sid)


@pytest.mark.asyncio
async def test_fork_session_inherits_modes():
    """分支会话继承源会话的两种模式（修复前会丢成 agent + 询问审批）。"""
    async with async_session_factory() as db:
        src_id = await session_service.create_session(
            db, project_id=None, title="[测试] 源会话",
            permission_mode="plan", approval_mode="full",
        )
        forked_id = None
        try:
            forked_id = await session_service.fork_session(db, src_id)
            assert forked_id != src_id
            child = await session_service.get_session(db, forked_id)
            assert child is not None
            assert child.approval_mode == "full"
            assert child.permission_mode == "plan"
        finally:
            if forked_id is not None:
                await session_service.delete_session_permanent(db, forked_id)
            await session_service.delete_session_permanent(db, src_id)


@pytest.mark.asyncio
async def test_update_session_approval_mode_persists():
    """切换权限模式（PATCH 路径）必须落盘——否则界面显示与实际执行不一致。"""
    async with async_session_factory() as db:
        sid = await session_service.create_session(
            db, project_id=None, title="[测试] 更新模式",
        )
        try:
            await session_service.update_session(db, sid, approval_mode="full")
            s = await session_service.get_session(db, sid)
            assert s is not None
            assert s.approval_mode == "full"
        finally:
            await session_service.delete_session_permanent(db, sid)


# ─────────── plan-75-332 R7：自定义模式保留与旧值兼容（代码审查发现） ───────────


def test_normalize_execution_mode_keeps_custom_mode():
    """未识别值必须原样保留——模式名取值域是「内置 3 档 ∪ 用户自定义」。

    修复前未识别值一律回落 agent：自定义模式在保存（会话 PATCH）与回显时被
    改写成「智能体模式」，用户配置的自定义模式实际不可用。
    """
    from app.orchestration.approval_policy import normalize_execution_mode as n

    # 内置 3 档原样
    assert n("readonly") == "readonly"
    assert n("plan") == "plan"
    assert n("agent") == "agent"
    # 空值回落内置默认档
    assert n(None) == "agent"
    assert n("") == "agent"
    assert n("   ") == "agent"
    # 旧值别名映射到新值
    assert n("default") == "agent"
    assert n("accept_edits") == "agent"
    assert n("full-access") == "agent"
    # 自定义模式名原样保留（不得改写成内置档）
    assert n("frontend-polish") == "frontend-polish"
    assert n("release-ops") == "release-ops"


def test_normalize_approval_mode_matrix():
    """权限模式无自定义取值域：空值/未识别回落最保守的 ask，旧值映射到新值。"""
    from app.orchestration.approval_policy import normalize_approval_mode as n

    assert n("ask") == "ask"
    assert n("auto") == "auto"
    assert n("full") == "full"
    assert n(None) == "ask"
    assert n("") == "ask"
    assert n("no_such_mode") == "ask"
    assert n("danger-full-access") == "full"


@pytest.mark.asyncio
async def test_create_session_keeps_custom_mode():
    """自定义执行模式必须原样落盘（保存环节不得被改写成内置档）。"""
    async with async_session_factory() as db:
        sid = await session_service.create_session(
            db, project_id=None, title="[测试] 自定义模式落盘",
            permission_mode="frontend-polish", approval_mode="auto",
        )
        try:
            s = await session_service.get_session(db, sid)
            assert s is not None
            assert s.permission_mode == "frontend-polish"
            assert s.approval_mode == "auto"
        finally:
            await session_service.delete_session_permanent(db, sid)


@pytest.mark.asyncio
async def test_fork_session_normalizes_legacy_source():
    """分支继承存量旧值时归一化，旧值不随分支继续扩散。

    直接构造旧值源会话（create_session 会归一化，无法用它造出旧值源）。
    """
    from app.persistence.models.message import Session as SessionModel

    async with async_session_factory() as db:
        src = SessionModel(
            project_id=None, title="[测试] 旧值源",
            permission_mode="default", approval_mode="ask",
        )
        db.add(src)
        await db.flush()
        src_id = src.id
        await db.commit()
        forked_id = None
        try:
            forked_id = await session_service.fork_session(db, src_id)
            child = await session_service.get_session(db, forked_id)
            assert child is not None
            assert child.permission_mode == "agent"   # 旧值 default → agent
            assert child.approval_mode == "ask"
        finally:
            if forked_id is not None:
                await session_service.delete_session_permanent(db, forked_id)
            await session_service.delete_session_permanent(db, src_id)
