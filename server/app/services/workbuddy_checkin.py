"""plan-248-1258 M2.6: WorkBuddy「Buddy 加油站」每日自动签到循环。

需求：打开软件后后台自动签到（workbuddy 账号），并在模型页面查看积分。

实现要点：
- 进程启动后延迟片刻（等 DB 初始化/迁移完成）执行首轮签到，此后每 12 小时检查一次；
- 每个 workbuddy 供应商的每条「已登录」凭据各签到一次（多账号）；
- 用 (credential_id, 日期) 去重，保证当天只签一次（进程重启不会重复签）；
- 全程静默降级：任何异常只记日志，不阻塞服务、不影响会话。

开关：settings.workbuddy_auto_checkin（默认 True）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)

# 首轮签到延迟（等启动初始化 + 迁移完成）
_INITIAL_DELAY_S = 45
# 检查间隔（每 12h 检查一次，跨日边界后当天必然被覆盖）
_INTERVAL_S = 12 * 3600
# 单账号失败重试上限（防风控）
_MAX_RETRY_PER_DAY = 3

_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None
# 当日已处理记录：{(credential_id, date_str): attempts}
_done: dict[tuple[int | None, str], int] = {}


async def _checkin_all() -> int:
    """对所有 workbuddy 供应商的已登录凭据执行签到（当日去重）。返回成功数。"""
    from sqlalchemy import select

    from app.auth.workbuddy import credits as wb_credits
    from app.persistence.database import async_session_factory
    from app.persistence.models.model_reg import Provider, ProviderCredential
    from app.services import credential_service

    today = date.today().isoformat()
    done = 0
    async with async_session_factory() as db:
        res = await db.execute(select(Provider).where(Provider.api_format == "workbuddy"))
        providers = list(res.scalars().all())
        for p in providers:
            if not p.is_active:
                continue
            if (p.auth_status or "") != "logged_in":
                continue
            creds = await credential_service.list_credentials(db, p.id)
            if not creds:
                # 旧数据无凭据行：以 provider 级账号签到一次
                key = (None, today)
                if _done.get(key, 0) >= _MAX_RETRY_PER_DAY:
                    continue
                try:
                    r = await wb_credits.claim_for_credential(db, p, None)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[wb-checkin] provider=%s 签到异常: %s", p.id, e)
                    _done[key] = _done.get(key, 0) + 1
                    continue
                _done[key] = _done.get(key, 0) + 1
                if r.get("status") in ("claimed", "already_claimed"):
                    done += 1
                    logger.info("[wb-checkin] provider=%s 签到结果=%s", p.id, r.get("status"))
                continue
            for c in creds:
                if not c.is_active:
                    continue
                key = (c.id, today)
                # 当天已成功过（次数计入过）则跳过
                if _done.get(key, 0) >= _MAX_RETRY_PER_DAY:
                    continue
                try:
                    r = await wb_credits.claim_for_credential(db, p, c)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[wb-checkin] 凭据 #%s 签到异常: %s", c.id, e)
                    _done[key] = _done.get(key, 0) + 1
                    continue
                status = r.get("status")
                _done[key] = _done.get(key, 0) + 1
                if status in ("claimed", "already_claimed"):
                    done += 1
                    logger.info("[wb-checkin] 凭据 #%s(%s) 签到结果=%s credits=%s",
                                c.id, c.label, status, r.get("credits"))
                elif status == "login_required":
                    logger.info("[wb-checkin] 凭据 #%s 未登录，跳过", c.id)
    # 清理非当日记录，避免字典无限增长
    for k in list(_done.keys()):
        if k[1] != today:
            _done.pop(k, None)
    return done


async def _loop() -> None:
    assert _stop is not None
    logger.info("[wb-checkin] WorkBuddy 每日签到循环启动")
    # 首轮延迟（等 DB 初始化完成）
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_stop.wait(), timeout=_INITIAL_DELAY_S)
    if _stop.is_set():
        return
    while not _stop.is_set():
        try:
            n = await _checkin_all()
            logger.info("[wb-checkin] 本轮签到完成：成功 %d 个账号", n)
        except Exception:
            logger.exception("[wb-checkin] 签到循环异常（不阻塞服务）")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_stop.wait(), timeout=_INTERVAL_S)
    logger.info("[wb-checkin] WorkBuddy 每日签到循环已停止")


async def start() -> None:
    """启动自动签到循环（幂等）。由 main.py 的 lifespan 调用。"""
    global _task, _stop
    if _task is not None and not _task.done():
        return
    if not getattr(settings, "workbuddy_auto_checkin", True):
        logger.info("[wb-checkin] 自动签到已被配置禁用")
        return
    _stop = asyncio.Event()
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    """停止签到循环。"""
    global _task, _stop
    if _stop is not None:
        _stop.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
        except Exception:
            logger.debug("[wb-checkin] 停止时异常", exc_info=True)
    _task = None
    _stop = None


async def checkin_now() -> dict:
    """手动触发一次全量签到（供设置页按钮）。返回统计。"""
    n = await _checkin_all()
    return {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat(), "accounts": n}
