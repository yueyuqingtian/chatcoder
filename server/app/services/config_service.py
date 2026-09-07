"""配置分层与命名 profile 服务（D6）。"""
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.config import ConfigProfile

_PROJECT_CONFIG_FILE = ".chatcoder/config.toml"

# 合并默认值（低优先）
_DEFAULTS: dict = {
    "approval_policy": "on-request",
    "sandbox_mode": "workspace-write",
    "writable_paths": [],
    "web_search": "cached",
}


async def list_profiles(db: AsyncSession, project_id: int | None = None) -> list[ConfigProfile]:
    stmt = select(ConfigProfile)
    if project_id is not None:
        stmt = stmt.where(ConfigProfile.scope == "project", ConfigProfile.project_id == project_id)
    res = await db.execute(stmt.order_by(ConfigProfile.id))
    return list(res.scalars().all())


async def create_profile(db: AsyncSession, *, name: str, scope: str = "global",
                         project_id: int | None = None, data: dict | None = None) -> int:
    """创建 profile（写引擎单写线程），返回 id。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        profile = ConfigProfile(name=name, scope=scope, project_id=project_id,
                                data=data or {}, is_active=False)
        s.add(profile)
        s.flush()
        pid = profile.id
        s.commit()
        return pid

    return await run_write_locked(patch, label="profile.create")


async def update_profile(db: AsyncSession, profile_id: int, **kwargs) -> bool:
    """更新 profile（写引擎单写线程；激活时同 scope 其它 profile 取消激活）。
    返回可否找到。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        profile = s.get(ConfigProfile, profile_id)
        if profile is None:
            return False
        if kwargs.get("data") is not None:
            profile.data = {**profile.data, **kwargs["data"]}
        if kwargs.get("is_active") is not None:
            # 激活时取消同 scope 其他 profile
            if kwargs["is_active"]:
                res = s.execute(select(ConfigProfile).where(ConfigProfile.scope == profile.scope))
                for p in res.scalars().all():
                    p.is_active = False
            profile.is_active = kwargs["is_active"]
        s.commit()
        return True

    return await run_write_locked(patch, label=f"profile.update.{profile_id}")


async def delete_profile(db: AsyncSession, profile_id: int) -> bool:
    """删除 profile（写引擎单写线程）。返回可否找到。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        profile = s.get(ConfigProfile, profile_id)
        if profile is None:
            return False
        s.delete(profile)
        s.commit()
        return True

    return await run_write_locked(patch, label=f"profile.delete.{profile_id}")


def _load_project_toml(project_path: str) -> dict:
    """读取项目 .chatcoder/config.toml（只读，不落库）。"""
    path = Path(project_path) / _PROJECT_CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


async def effective_config(db: AsyncSession, project_path: str | None = None,
                           project_id: int | None = None) -> dict:
    """合并顺序（低→高）：默认 → 全局激活 profile → 项目 toml → 项目激活 profile。"""
    cfg = dict(_DEFAULTS)

    res = await db.execute(
        select(ConfigProfile).where(ConfigProfile.scope == "global", ConfigProfile.is_active == True)  # noqa: E712
    )
    for p in res.scalars().all():
        cfg = _deep_merge(cfg, p.data)

    if project_path:
        cfg = _deep_merge(cfg, _load_project_toml(project_path))

    if project_id:
        res = await db.execute(
            select(ConfigProfile).where(
                ConfigProfile.scope == "project",
                ConfigProfile.project_id == project_id,
                ConfigProfile.is_active == True,  # noqa: E712
            )
        )
        for p in res.scalars().all():
            cfg = _deep_merge(cfg, p.data)

    return cfg
