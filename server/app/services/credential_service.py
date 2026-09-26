"""plan-248-1258 M2.2/M2.3: 供应商凭据（多 Key / 多账号）与代理解析服务。

职责：
1. 凭据 CRUD（增删改查、启停、排序）；
2. 凭据尝试序解析（按 priority，跳过禁用/冷却中的凭据，成功后粘性优先）；
3. 失败上报（写 status=cooldown + last_error + cooldown_until，供轮询跳过）；
4. 代理解析（供应商级 proxy_mode/proxy_url → 实际代理 URL 或 None）。

设计：本模块只做数据与规则，不构造 provider 实例（那是 registry 的职责）；
agent_loop 在遇到可重试错误时调用 next_credential() 取下一凭据重建 provider。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.models.model_reg import Provider, ProviderCredential

logger = logging.getLogger(__name__)

# 凭据失败默认冷却时长（秒）——读全局设置，缺省 5 分钟
DEFAULT_COOLDOWN_SECONDS = 300
# plan-290: 默认「连续失败几次才冷却」——瞬时抖动不应立刻踢掉凭据
DEFAULT_FAIL_THRESHOLD = 3
# 视为"可切换凭据"的错误类型标记（由 provider 层归一化后的错误文本匹配）
_RETRYABLE_HINTS = (
    "401", "403", "429", "500", "502", "503", "504",
    "unauthorized", "forbidden", "rate limit", "quota", "insufficient",
    "connection", "timeout", "overloaded",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def cooldown_seconds() -> int:
    return int(getattr(settings, "provider_credential_cooldown_seconds", 0) or DEFAULT_COOLDOWN_SECONDS)


def fail_threshold() -> int:
    """连续失败几次才冷却（默认 3）。<=1 表示首次失败即冷却（旧行为）。"""
    raw = getattr(settings, "provider_credential_fail_threshold", None)
    if raw is None:
        return DEFAULT_FAIL_THRESHOLD
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_FAIL_THRESHOLD


def is_retryable_error(err: object) -> bool:
    """判断错误是否值得切换凭据重试（认证/限流/5xx/网络）。"""
    text = str(err).lower()
    return any(h in text for h in _RETRYABLE_HINTS)


def _sort_key(cred: ProviderCredential) -> tuple:
    """尝试序：上次成功过优先（粘性）→ priority 小 → id 小。"""
    sticky = 0 if cred.last_ok_at else 1
    return (sticky, cred.priority or 0, cred.id or 0)


def _round_robin_key(cred: ProviderCredential) -> tuple:
    """轮询序：忽略粘性，仅按 priority 小 → id 小（严格优先级顺序）。"""
    return (cred.priority or 0, cred.id or 0)


# plan-271-1364 M3.1: 轮询模式的进程内游标（provider_id → 上次取用的凭据 id）。
# 只用于"分散用量"的顺序轮转，重启归零属于可接受语义（见方案 §七）。
_rr_cursor: dict[int, int] = {}


def order_credentials(creds: list[ProviderCredential], strategy: str = "sticky",
                      rotate: bool = False, provider_id: int | None = None,
                      exclude: set[int] | None = None) -> list[ProviderCredential]:
    """按取用策略返回尝试序列表。

    - strategy="sticky"（默认）：粘性优先，行为与 available_credentials 一致；
    - strategy="round_robin"：仅按 (priority, id) 排序；
    - rotate=True（仅轮询模式生效）：从进程内游标的后一位开始，使多次调用依次落在
      不同凭据上；rotate 只在真正取用凭据的路径调用，避免展示路径推进游标。
    """
    ok = list(creds)
    if exclude:
        ok = [c for c in ok if c.id not in exclude]
    if (strategy or "sticky").lower() != "round_robin":
        ok.sort(key=_sort_key)
        return ok

    ok.sort(key=_round_robin_key)
    if not rotate or not ok or provider_id is None:
        return ok
    last_id = _rr_cursor.get(provider_id)
    if last_id is not None:
        idx = next((i for i, c in enumerate(ok) if c.id == last_id), None)
        if idx is not None:
            ok = ok[idx + 1:] + ok[:idx + 1]
    _rr_cursor[provider_id] = ok[0].id or 0
    return ok


def available_credentials(creds: list[ProviderCredential], strategy: str = "sticky",
                          provider_id: int | None = None,
                          rotate: bool = False) -> list[ProviderCredential]:
    """过滤出当前可用凭据（启用且不在冷却期内），按尝试序排序。

    plan-271-1364 M3.1: 排序委托 order_credentials（strategy 决定粘性/轮转）；
    默认 sticky 且不 rotate，展示路径（计数/扫描取 key）行为与改造前完全一致。

    plan-290 单凭据豁免：该供应商只有这一条凭据时，冷却等于"整个供应商不可用"、
    没有备选可切——此时忽略其冷却状态照常返回，给"再试一次"的机会。这一层兜底同时
    修复历史遗留的冷却行（旧版本已把唯一凭据置为 cooldown 的库）。
    """
    now = _now()
    single = len(creds) <= 1  # 仅此一条：永不因冷却被跳过
    ok: list[ProviderCredential] = []
    for c in creds:
        if not c.is_active:
            continue
        if not single and (c.status or "ok") == "cooldown":
            until = _parse_iso(c.cooldown_until)
            if until is not None and until > now:
                continue  # 冷却中，跳过
        ok.append(c)
    return order_credentials(ok, strategy=strategy, rotate=rotate, provider_id=provider_id)


async def list_credentials(db: AsyncSession, provider_id: int) -> list[ProviderCredential]:
    # populate_existing：写引擎（独立线程会话）更新后强制刷新，避免读到陈旧 status/cooldown
    res = await db.execute(
        select(ProviderCredential)
        .where(ProviderCredential.provider_id == provider_id)
        .order_by(ProviderCredential.priority.asc(), ProviderCredential.id.asc())
        .execution_options(populate_existing=True)
    )
    return list(res.scalars().all())


async def get_credential(db: AsyncSession, credential_id: int) -> ProviderCredential | None:
    """按 id 取凭据。

    populate_existing：写引擎（独立会话/线程）更新后，本会话身份映射里的旧对象
    不会被自动刷新；强制回填保证调用方读到最新 status/cooldown。
    """
    from sqlalchemy import select

    res = await db.execute(
        select(ProviderCredential)
        .where(ProviderCredential.id == credential_id)
        .execution_options(populate_existing=True)
    )
    return res.scalars().first()


async def create_credential(db: AsyncSession, provider_id: int, **fields) -> int:
    from app.persistence.database import run_write_locked

    def patch(s):
        # priority 缺省取当前最大值 +1（新凭据排最后）
        existing = list(
            s.execute(
                select(ProviderCredential.priority).where(
                    ProviderCredential.provider_id == provider_id
                )
            ).scalars().all()
        )
        default_priority = (max(existing) + 1) if existing else 0
        # 显式弹出 status：调用方（路由）可能同时传入 status，若一并进 **fields
        # 会与下面的 status= 重复赋值抛 TypeError（历史缺陷，导致「添加 Key」500）。
        explicit_status = fields.pop("status", None)
        explicit_priority = fields.pop("priority", None)
        cred = ProviderCredential(
            provider_id=provider_id,
            # priority 为 None（前端未填/显式传 null）时回落默认排队尾
            priority=default_priority if explicit_priority is None else explicit_priority,
            status=explicit_status or ("ok" if fields.get("api_key") else "disabled"),
            **fields,
        )
        s.add(cred)
        s.flush()
        cid = cred.id
        s.commit()
        return cid

    return await run_write_locked(patch, label=f"credential.create.{provider_id}")


async def create_account_credential(db: AsyncSession, provider_id: int, *,
                                    label: str | None = None,
                                    extra: dict | None = None,
                                    account: dict | None = None) -> int:
    """plan-271-1364 M2.2: 为 OAuth 登录账号显式建一条「账号凭据」并返回其 id。

    与 create_credential 的差异：账号凭据没有 api_key，若走 create_credential 会被
    判成 status="disabled"（永远不可用）。此处显式 status="ok"、is_active=True，
    供登录流程拿到 credential_id 后把 workbuddy_auth 行挂上来，实现多账号。
    """
    from app.persistence.database import run_write_locked

    snapshot = dict(account or {})
    if extra:
        snapshot.update(extra)

    def patch(s):
        existing = list(
            s.execute(
                select(ProviderCredential.priority).where(
                    ProviderCredential.provider_id == provider_id
                )
            ).scalars().all()
        )
        default_priority = (max(existing) + 1) if existing else 0
        cred = ProviderCredential(
            provider_id=provider_id,
            label=label,
            api_key=None,
            token_ref=None,
            priority=default_priority,
            is_active=True,
            status="ok",
            extra=snapshot or None,
        )
        s.add(cred)
        s.flush()
        cid = cred.id
        s.commit()
        return cid

    return await run_write_locked(patch, label=f"credential.create_account.{provider_id}")


async def update_credential(db: AsyncSession, credential_id: int, **fields) -> bool:
    """只更新非 None 字段；api_key 传空字符串表示清除。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        cred = s.get(ProviderCredential, credential_id)
        if cred is None:
            return False
        for k, v in fields.items():
            if v is None:
                continue
            if k == "api_key" and v == "":
                setattr(cred, k, None)
            else:
                setattr(cred, k, v)
        # 手动启用且有 key/账号 → 复位为 ok（清冷却）
        # plan-271-1364: 账号凭据（无 api_key/token_ref，靠 extra 标记）同样可复位
        if fields.get("is_active") is True and (cred.api_key or cred.token_ref or cred.extra):
            cred.status = "ok"
            cred.cooldown_until = None
            cred.fail_count = 0  # plan-290: 重新启用即重置连续失败计数
        s.commit()
        return True

    return await run_write_locked(patch, label=f"credential.update.{credential_id}")


async def delete_credential(db: AsyncSession, credential_id: int) -> bool:
    from app.persistence.database import run_write_locked

    def patch(s):
        cred = s.get(ProviderCredential, credential_id)
        if cred is None:
            return False
        # plan-271-1364 M1.2: 级联清理该凭据关联的 OAuth 登录态行——
        # 否则残留孤儿 token 行，_load_auth_for_credential 的 legacy 回落可能命中旧账号。
        _purge_auth_rows(s, credential_id)
        s.delete(cred)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"credential.delete.{credential_id}")


def _purge_auth_rows(session, credential_id: int) -> int:
    """删除 workbuddy_auth / ta3_auth / trae_auth 中归属该凭据的行（同事务内）。

    用 select 逐个删除而非裸 SQL，保持 ORM 会话状态一致；表名固定，不走用户输入。
    """
    from app.persistence.models.ta3_auth import Ta3Auth
    from app.persistence.models.trae_auth import TraeAuth
    from app.persistence.models.workbuddy_auth import WorkBuddyAuth

    removed = 0
    for model in (WorkBuddyAuth, Ta3Auth, TraeAuth):
        rows = session.execute(
            select(model).where(model.credential_id == credential_id)
        ).scalars().all()
        for row in rows:
            session.delete(row)
            removed += 1
    return removed


def purge_provider_credentials(session, provider_id: int) -> int:
    """删除某供应商的全部凭据行（连同其 OAuth 登录态行），返回删除条数。

    plan-81-345: 删除供应商时必须调用——provider_credentials.provider_id 无外键约束，
    漏删会残留孤儿行；SQLite 会复用被删供应商的 id（rowid 复用），
    孤儿行会被误挂到之后新建的供应商上（用户看到「新供应商凭空多出一个旧 Key」）。
    仅在写锁（run_write_locked 的 patch）内调用，不单独提交。
    """
    rows = list(
        session.execute(
            select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
        ).scalars().all()
    )
    for cred in rows:
        _purge_auth_rows(session, cred.id)
        session.delete(cred)
    return len(rows)


def purge_orphan_credentials(session) -> int:
    """清理 provider_id 已不存在于 providers 表的孤儿凭据（连同其 OAuth 登录态行）。

    plan-81-345: 历史版本删除供应商时漏删凭据；SQLite 复用被删供应商的 id 时，
    孤儿行会被误挂到新建的供应商上。新建供应商前调用一次即可自愈（幂等）。
    仅在写锁内调用，不单独提交。
    """
    known = set(session.execute(select(Provider.id)).scalars().all())
    rows = list(session.execute(select(ProviderCredential)).scalars().all())
    removed = 0
    for cred in rows:
        if cred.provider_id in known:
            continue
        _purge_auth_rows(session, cred.id)
        session.delete(cred)
        removed += 1
    return removed


async def mark_failed(db: AsyncSession, credential_id: int, error: str) -> bool:
    """上报凭据一次失败。返回是否**因此进入了冷却**。

    plan-290 两条规则：
    1. **连续失败达阈值才冷却**：单次抖动（网络闪断、网关偶发 5xx、单次超时）只累计
       fail_count 并记录 last_error，凭据仍留在可用池；连续失败达
       provider_credential_fail_threshold（默认 3）才置 cooldown。避免一次抖动就让
       可用 Key/账号被闲置 5 分钟。
    2. **只有一个凭据时永不冷却**：该供应商仅此一条凭据时，冷却等于"整个供应商不可用"，
       没有任何备选可切。此时只记录错误、不置 cooldown——保留"再试一次"的机会，
       否则一次网络抖动就会让用户彻底无法调用模型。
    """
    from sqlalchemy import func as _func

    from app.persistence.database import run_write_locked

    until = _iso(_now() + timedelta(seconds=cooldown_seconds()))
    threshold = fail_threshold()

    def patch(s):
        cred = s.get(ProviderCredential, credential_id)
        if cred is None:
            return False
        cred.last_error = str(error)[:300]
        cred.fail_count = int(cred.fail_count or 0) + 1
        # 单凭据供应商：冷却即全局不可用，无备选可切 → 永不冷却
        total = s.execute(
            select(_func.count(ProviderCredential.id)).where(
                ProviderCredential.provider_id == cred.provider_id
            )
        ).scalar() or 0
        if int(total) <= 1:
            logger.warning(
                "[credential] #%s 失败(第 %d 次)但该供应商仅此一条凭据——不冷却: %s",
                credential_id, cred.fail_count, str(error)[:120])
            cred.status = "ok"
            cred.cooldown_until = None
            s.commit()
            return False
        if cred.fail_count >= threshold:
            cred.status = "cooldown"
            cred.cooldown_until = until
            s.commit()
            return True
        # 未达阈值：保留可用，仅留痕（下一次调用可继续尝试）
        cred.status = "ok"
        cred.cooldown_until = None
        s.commit()
        return False

    return await run_write_locked(patch, label=f"credential.fail.{credential_id}")


async def reset_credential(db: AsyncSession, credential_id: int) -> bool:
    """手动复位凭据：清冷却与错误，恢复为可用（plan-282-1441）。

    与 mark_ok 的区别：mark_ok 是"成功调用后的记账"，会刷新 last_ok_at 影响粘性序；
    本函数只做"清掉不可用状态"，供用户点一下就把卡住的凭据救回来，不改变调用历史。
    同时把 is_active 置回 True —— 用户点"恢复可用"的意图就是让它重新参与轮询。
    """
    from app.persistence.database import run_write_locked

    def patch(s):
        cred = s.get(ProviderCredential, credential_id)
        if cred is None:
            return False
        cred.status = "ok"
        cred.cooldown_until = None
        cred.last_error = None
        cred.fail_count = 0  # plan-290: 手动复位同时清零连续失败计数
        cred.is_active = True
        s.commit()
        return True

    return await run_write_locked(patch, label=f"credential.reset.{credential_id}")


async def mark_ok(db: AsyncSession, credential_id: int) -> None:
    """标记凭据成功（清冷却、刷新粘性时间、清零连续失败计数）。"""
    from app.persistence.database import run_write_locked

    now = _iso(_now())

    def patch(s):
        cred = s.get(ProviderCredential, credential_id)
        if cred is None:
            return
        cred.status = "ok"
        cred.last_error = None
        cred.cooldown_until = None
        cred.last_ok_at = now
        cred.fail_count = 0  # plan-290: 一旦成功即重置连续失败计数
        s.commit()

    await run_write_locked(patch, label=f"credential.ok.{credential_id}")


async def set_credits(db: AsyncSession, credential_id: int, credits: float | None) -> None:
    """写入积分缓存（workbuddy 等账号）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        cred = s.get(ProviderCredential, credential_id)
        if cred is None:
            return
        cred.credits = credits
        s.commit()

    await run_write_locked(patch, label=f"credential.credits.{credential_id}")


# ── 代理解析（M2.3）──

def resolve_proxy(provider: Provider | None) -> str | None:
    """按供应商代理配置返回实际代理 URL；None 表示不使用代理。

    - inherit：跟随全局（settings.http_proxy → 环境变量）；
    - global ：强制走全局代理（未配置时等价 inherit）；
    - custom ：用供应商自己的 proxy_url（空则回落全局，防误配导致直连失败）；
    - direct ：直连，明确禁用代理（含忽略环境变量）。
    """
    mode = (getattr(provider, "proxy_mode", None) or "inherit").lower()
    custom = (getattr(provider, "proxy_url", None) or "").strip()

    def _global() -> str | None:
        from app.core.http_client import get_proxy_url

        return get_proxy_url()

    if mode == "direct":
        return None
    if mode == "custom":
        return custom or _global()
    if mode == "global":
        return _global()
    return _global()


def proxy_disabled(provider: Provider | None) -> bool:
    """direct 模式需显式关闭 httpx 的环境代理读取（trust_env=False）。"""
    return (getattr(provider, "proxy_mode", None) or "").lower() == "direct"
