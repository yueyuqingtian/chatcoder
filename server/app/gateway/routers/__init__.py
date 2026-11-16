"""Routers 聚合入口（v2）。"""
from app.gateway.routers import (  # noqa: F401
    community,
    debug,
    diagnostics,
    exec_policy,
    hooks,
    market,
    memories,
    # plan-693-2868 §6.5 / §7 P4：本机微应用库管理（list / detail / validate / remove / import）
    microapps,
    # protocol 13-external-microapps §2：外置微应用只读接入（list / detail / validate）
    microapps_ext,
    permission_profiles,
    plugins,
    profiles,
    projects,
    scheduled,
    sessions,
    suggestions,
    turns,
)

__all__ = [
    "projects",
    "sessions",
    "turns",
    "suggestions",
    "scheduled",
    "profiles",
    "permission_profiles",
    "exec_policy",
    "hooks",
    "market",
    # plan-693-2868 §6.5 / §7 P5：社区微应用（只读 + 下载安装）
    "community",
    # plan-693-2868 §6.5 / §7 P4：本机微应用库管理（与 community 分工：本机库 vs 远端索引）
    "microapps",
    # protocol 13-external-microapps §2：外置微应用只读接入（list / detail / validate）
    "microapps_ext",
    "memories",
    "diagnostics",
    "plugins",
    "debug",
]
