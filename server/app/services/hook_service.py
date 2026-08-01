"""钩子执行器与配置服务（D5，Claude Code 风格）。"""
import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import HookEvent
from app.persistence.models.hook import HookConfig

logger = logging.getLogger(__name__)

_HOOK_TIMEOUT = 10.0


async def list_hooks(db: AsyncSession) -> list[HookConfig]:
    res = await db.execute(select(HookConfig).order_by(HookConfig.id))
    return list(res.scalars().all())


async def create_hook(db: AsyncSession, *, event: str, command: str,
                      matcher: str | None = None, enabled: bool = True) -> HookConfig:
    if event not in [e.value for e in HookEvent]:
        raise ValueError(f"未知钩子事件: {event}")
    hook = HookConfig(event=event, command=command, matcher=matcher, enabled=enabled)
    db.add(hook)
    await db.flush()
    return hook


async def update_hook(db: AsyncSession, hook_id: int, **kwargs) -> HookConfig | None:
    hook = await db.get(HookConfig, hook_id)
    if hook is None:
        return None
    for k, v in kwargs.items():
        if v is not None:
            setattr(hook, k, v)
    await db.flush()
    return hook


async def delete_hook(db: AsyncSession, hook_id: int) -> bool:
    hook = await db.get(HookConfig, hook_id)
    if hook is None:
        return False
    await db.delete(hook)
    await db.flush()
    return True


async def run_hooks(db: AsyncSession, event: str, payload: dict,
                    matcher_key: str | None = None) -> list[dict]:
    """执行匹配事件与 matcher 的钩子。

    matcher_key 传入（如工具名）时，仅执行 matcher 为空或等于该值的钩子。
    返回各钩子的解析响应；失败 fail-open（记日志，不阻断）。
    """
    hooks = await list_hooks(db)
    matched = [h for h in hooks if h.enabled and h.event == event]
    if matcher_key:
        matched = [h for h in matched if not h.matcher or h.matcher == matcher_key]
    results: list[dict] = []
    for hook in matched:
        try:
            outcome = await _exec_hook(hook.command, payload)
            results.append(outcome)
            logger.info("[hook] event=%s cmd=%s decision=%s", event, hook.command, outcome.get("decision"))
        except Exception as e:
            logger.warning("[hook] 执行失败(不阻断): %s", e)
    return results


async def _exec_hook(command: str, payload: dict) -> dict:
    """执行 shell 命令，stdin 传 JSON payload，stdout 解析响应。"""
    stdin_data = json.dumps(payload, ensure_ascii=False)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(stdin_data.encode("utf-8")), timeout=_HOOK_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"hook 超时({_HOOK_TIMEOUT}s)")
    out = stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"stdout": out}
