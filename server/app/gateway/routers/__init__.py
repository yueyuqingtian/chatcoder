"""Routers 聚合入口（v2）。"""
from app.gateway.routers import (  # noqa: F401
    diagnostics,
    exec_policy,
    hooks,
    memories,
    permission_profiles,
    profiles,
    projects,
    scheduled,
    sessions,
    turns,
)

__all__ = [
    "projects",
    "sessions",
    "turns",
    "scheduled",
    "profiles",
    "permission_profiles",
    "exec_policy",
    "hooks",
    "memories",
    "diagnostics",
]
