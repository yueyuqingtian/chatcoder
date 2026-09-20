"""plan-282-1441 二次修复的回归测试。

覆盖本轮修掉的真实缺陷：
1. ta3 登录后必须**存在凭据行**（否则界面凭据列表永远为空、会话取不到 Key）；
2. 凭据冷却必须**可复位**，且冷却到期后展示层自愈（此前进了冷却就再也用不了）；
3. 启动自愈必须收尾 **running 步骤**（否则重启后胶囊显示"最后一步还在执行"）；
4. 工作树必须支持"根是空仓库、代码在子仓库"的布局（此前报 invalid reference: main）；
5. 规则文档扫描必须覆盖各 CLI 的约定名与目录形态。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import (  # noqa: F401 注册全部模型
    Provider,
)
from app.services import credential_service


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/fix.db"
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


# ── 4) 工作树的默认分支探测：不得盲猜 main ──

@pytest.mark.asyncio
async def test_default_branch_returns_empty_for_empty_repo(tmp_path):
    """空仓库必须返回空串（而不是硬编码 'main'）——否则创建工作树会报
    fatal: invalid reference: main（用户实际遇到的报错）。

    这里直接用手工构造的 .git 结构模拟"有仓库但无提交"：
    真实 git 初始化在测试环境可能不可用，故用最小可用断言。
    """
    import subprocess
    from pathlib import Path

    from app.services import worktree_service as ws

    repo = tmp_path / "empty_repo"
    repo.mkdir()
    try:
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo),
                       capture_output=True, timeout=30, check=True)
    except Exception:
        pytest.skip("本机无 git，跳过该用例")

    branch = await ws._default_branch(str(repo))
    assert branch == "", f"空仓库不应返回猜测的分支名，实际 {branch!r}"
    assert await ws._has_commits(str(repo)) is False

    # 空仓库下创建工作树必须给出**可操作**的提示，而不是 git 原始错误
    with pytest.raises(ValueError) as ei:
        await ws._require_initial_commit(str(repo))
    assert "提交" in str(ei.value)


def test_skip_dirs_cover_build_artifacts():
    """子仓库扫描必须跳过构建产物目录，避免把 dist/node_modules 误判成仓库。"""
    from app.services.worktree_service import _SKIP_DIRS
    for name in ("node_modules", "dist", "build", "target", ".git", "venv"):
        assert name in _SKIP_DIRS


# ── 5) 规则文档扫描：各 CLI 约定名 + 目录形态 ──

@pytest.mark.asyncio
async def test_scan_rules_docs_covers_cli_conventions(tmp_path):
    from app.orchestration.rules_loader import scan_rules_docs

    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "AGENTS.md").write_text("root agents", encoding="utf-8")
    (root / "sub" / "AGENTS.md").write_text("sub agents", encoding="utf-8")
    (root / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
    (root / "CODEBUDDY.md").write_text("codebuddy rules", encoding="utf-8")
    (root / "QODER.md").write_text("qoder rules", encoding="utf-8")
    (root / "GEMINI.md").write_text("gemini rules", encoding="utf-8")
    # 目录形态（Cursor / Trae / CodeBuddy 的 rules 目录）
    (root / ".cursor" / "rules").mkdir(parents=True)
    (root / ".cursor" / "rules" / "a.md").write_text("cursor rule", encoding="utf-8")
    (root / ".trae" / "rules").mkdir(parents=True)
    (root / ".trae" / "rules" / "b.md").write_text("trae rule", encoding="utf-8")

    found = set(await scan_rules_docs(str(root)))
    assert "AGENTS.md" in found
    assert "sub/AGENTS.md" in found, "一级子目录的规则文档此前会被忽略"
    assert "CLAUDE.md" in found
    assert "CODEBUDDY.md" in found, "CodeBuddy 约定名此前不支持"
    assert "QODER.md" in found, "Qoder 约定名此前不支持"
    assert "GEMINI.md" in found
    assert ".cursor/rules/a.md" in found, "目录形态规则此前完全不扫描"
    assert ".trae/rules/b.md" in found


# ── 2) 凭据冷却：复位与到期自愈 ──

@pytest.mark.asyncio
async def test_reset_credential_clears_cooldown(db):
    """复位必须清掉冷却与错误并把凭据重新启用。"""
    provider = Provider(name="p", api_format="openai")
    db.add(provider)
    await db.commit()

    cid = await credential_service.create_credential(
        db, provider.id, label="k1", api_key="sk-x", is_active=True)
    # plan-290: 单凭据供应商永不冷却，故再添一条备选；并把失败累计到阈值才进入冷却
    await credential_service.create_credential(
        db, provider.id, label="k2", api_key="sk-y", is_active=True)
    for _ in range(credential_service.fail_threshold()):
        await credential_service.mark_failed(db, cid, "429 rate limit")

    from app.persistence.models.model_reg import ProviderCredential
    row = await db.get(ProviderCredential, cid)
    await db.refresh(row)
    assert row.status == "cooldown"
    assert row.cooldown_until

    ok = await credential_service.reset_credential(db, cid)
    assert ok is True
    await db.refresh(row)
    assert row.status == "ok"
    assert row.cooldown_until is None
    assert row.last_error is None
    assert row.is_active is True


@pytest.mark.asyncio
async def test_serializer_heals_expired_cooldown(db):
    """冷却已过期时，序列化必须按可用呈现（否则界面永远卡"冷却中"）。"""
    from datetime import datetime, timedelta, timezone

    from app.gateway.routers.providers import _cred_to_out
    from app.persistence.models.model_reg import ProviderCredential

    provider = Provider(name="p2", api_format="openai")
    db.add(provider)
    await db.commit()
    cid = await credential_service.create_credential(
        db, provider.id, label="k2", api_key="sk-y", is_active=True)

    row = await db.get(ProviderCredential, cid)
    row.status = "cooldown"
    row.cooldown_until = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await db.commit()

    out = await _cred_to_out(row)
    assert out.status == "ok", "冷却到期后必须自愈为可用"
    assert out.cooldown_until is None


@pytest.mark.asyncio
async def test_serializer_keeps_active_cooldown(db):
    """冷却**未到期**时必须如实显示冷却中（不能自愈过头）。"""
    from datetime import datetime, timedelta, timezone

    from app.gateway.routers.providers import _cred_to_out
    from app.persistence.models.model_reg import ProviderCredential

    provider = Provider(name="p3", api_format="openai")
    db.add(provider)
    await db.commit()
    cid = await credential_service.create_credential(
        db, provider.id, label="k3", api_key="sk-z", is_active=True)
    row = await db.get(ProviderCredential, cid)
    row.status = "cooldown"
    row.cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    await db.commit()

    out = await _cred_to_out(row)
    assert out.status == "cooldown"
    assert out.cooldown_until


# ── 1) ta3 登录建凭据行 ──

@pytest.mark.asyncio
async def test_ta3_login_creates_account_credential(db):
    """ta3 登录后必须存在「账号凭据」行，且重复调用幂等。"""
    from app.auth.ta3.session import ensure_account_credential
    from app.persistence.models.model_reg import ProviderCredential

    provider = Provider(name="ta3prov", api_format="ta3", base_url="http://x")
    db.add(provider)
    await db.commit()

    cid1 = await ensure_account_credential(db, provider.id, {"label": "牛码"})
    assert cid1 is not None

    rows = list((await db.execute(
        select(ProviderCredential).where(ProviderCredential.provider_id == provider.id)
    )).scalars().all())
    assert len(rows) == 1, "登录应恰好建一条账号凭据"
    assert rows[0].is_active is True
    assert rows[0].status == "ok"
    assert rows[0].label == "牛码"
    assert (rows[0].extra or {}).get("kind") == "ta3_account"

    # 幂等：再次调用复用同一行，不重复创建
    cid2 = await ensure_account_credential(db, provider.id, {"label": "牛码"})
    assert cid2 == cid1
    rows2 = list((await db.execute(
        select(ProviderCredential).where(ProviderCredential.provider_id == provider.id)
    )).scalars().all())
    assert len(rows2) == 1


# ── 3) 启动自愈：收尾 running 步骤 ──

@pytest.mark.asyncio
async def test_heal_orphan_turns_resets_running_steps(db):
    """重启自愈必须把遗留的 running **步骤**收回为 pending。

    否则前端读引擎步骤时看到最后一步"进行中"，胶囊显示"最后一步还在执行"。
    """
    from app.persistence.models.task import Task
    from app.persistence.models.turn import Turn
    from app.persistence.seed import _heal_orphan_turns

    turn = Turn(session_id=1, status="running")
    db.add(turn)
    await db.flush()
    group = Task(session_id=1, turn_id=turn.id, kind="group", title="任务清单", status="running")
    db.add(group)
    await db.flush()
    s1 = Task(session_id=1, turn_id=turn.id, parent_task_id=group.id,
              kind="step", title="步骤1", status="done")
    s2 = Task(session_id=1, turn_id=turn.id, parent_task_id=group.id,
              kind="step", title="步骤2", status="running")
    db.add_all([s1, s2])
    await db.commit()

    await _heal_orphan_turns(db)
    await db.commit()

    await db.refresh(turn)
    await db.refresh(s2)
    await db.refresh(group)
    assert turn.status == "failed"
    assert s2.status == "pending", "遗留的 running 步骤必须收回为 pending"
    assert group.status == "pending", "未全部完成的清单分组不应停在 running"
    assert s1.status == "done", "已完成步骤不得被改动"


@pytest.mark.asyncio
async def test_heal_marks_group_done_when_all_steps_done(db):
    """清单下所有步骤都完成时，分组应显示为已完成（而不是 running）。"""
    from app.persistence.models.task import Task
    from app.persistence.models.turn import Turn
    from app.persistence.seed import _heal_orphan_turns

    turn = Turn(session_id=1, status="completed")
    db.add(turn)
    await db.flush()
    group = Task(session_id=1, turn_id=turn.id, kind="group", title="任务清单", status="running")
    db.add(group)
    await db.flush()
    db.add_all([
        Task(session_id=1, turn_id=turn.id, parent_task_id=group.id,
             kind="step", title="a", status="done"),
        Task(session_id=1, turn_id=turn.id, parent_task_id=group.id,
             kind="step", title="b", status="done"),
    ])
    await db.commit()

    await _heal_orphan_turns(db)
    await db.commit()
    await db.refresh(group)
    assert group.status == "done"
