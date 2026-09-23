"""工作目录之外的读取授权（v36 plan-321-1600 R2）。

背景与口径（用户明确要求）：
- 内置搜索/读取工具（fs_read / fs_list / fs_grep）在**所有权限模式**下都支持读取
  工作目录以外的路径，但需要用户审批；
- 支持「自动审批」配置（settings.auto_approve_outside_read）；
- 沙箱模式为完全访问（danger-full-access）时一律免审；
- 审批弹窗复用既有能力：用户点「当前会话允许 / 始终允许」→ 生成工具级
  exec_policy allow 规则（ws._remember_approval），下次同工具自动放行。

仅放开**只读**能力：写盘工具仍严格限制在工作区内（safe_resolve / safe_resolve_parent）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def authorize_outside_read(
    ctx, raw_path: str, *, tool_name: str,
) -> tuple[object | None, str]:
    """为工作区外路径申请读取授权。

    返回 (target, error)：
    - target 非 None：放行（Path 对象，调用方自行做存在性/类型检查）；
    - target 为 None 且 error 非空：拒绝，error 是给模型看的文本。

    判定顺序（与 executor 审批门同源）：完全访问免审 → exec_policy 规则
    （含审批卡「当前会话/始终允许」生成的规则）→ 全局自动审批配置 → 弹窗审批。
    """
    from pathlib import Path

    from app.orchestration.tools.safe_path import resolve_loose

    target_str = resolve_loose(raw_path, getattr(ctx, "workspace_root", None))
    if not target_str:
        return None, f"路径非法或无法解析: {raw_path}"
    target = Path(target_str)

    sandbox = getattr(ctx, "sandbox_mode", "workspace-write") or "workspace-write"

    # 1) 完全访问沙箱：一律免审（用户口径）
    if sandbox == "danger-full-access":
        logger.info("[outside] danger-full-access 免审放行 %s 读取 %s", tool_name, raw_path)
        return target, ""

    # 2) 执行策略规则：deny 直接拒绝；allow（含审批卡记住的规则）直接放行
    if getattr(ctx, "db", None) is not None:
        try:
            from app.services import exec_policy_service

            rules = await exec_policy_service.list_rules(ctx.db, session_id=ctx.session_id)
            decision, just = exec_policy_service.match_tool_rule(rules, tool_name)
            if decision == "deny":
                return None, just or f"执行策略已禁止读取工作区外路径（{tool_name}）"
            if decision == "allow":
                return target, ""
        except Exception:
            logger.warning("[outside] 规则匹配异常(忽略)", exc_info=True)

    # 3) 全局自动审批配置（设置中心可开关）
    try:
        from app.core.config import settings

        if bool(getattr(settings, "auto_approve_outside_read", False)):
            logger.info("[outside] 自动审批开启，放行 %s 读取 %s", tool_name, raw_path)
            return target, ""
    except Exception:
        logger.debug("[outside] 读取自动审批配置失败(按需审批处理)", exc_info=True)

    # 4) 弹窗审批（用户可选「仅本次 / 当前会话允许 / 始终允许」）
    try:
        from app.orchestration.approval import approval_manager

        approval_id = approval_manager.new_id()
        detail = {
            "call_key": f"outside_read:{tool_name}",
            "tool": tool_name,
            "args": {"path": raw_path},
            "risk_level": "medium",
            "kind": "tool_call",
            "agent_id": getattr(ctx, "agent_id", None),
            "agent_name": getattr(ctx, "agent_name", ""),
            "task_id": getattr(ctx, "task_id", None),
            "session_id": getattr(ctx, "session_id", None),
            # 前端据此弹窗按「工作区外读取」渲染（展示路径，不展示 JSON）
            "outside_read": True,
            "summary": (
                f"请求读取工作目录之外的路径：{raw_path}"
                f"（工作目录：{getattr(ctx, 'workspace_root', '')}）"
            ),
        }
        approved = await approval_manager.request(approval_id=approval_id, detail=detail)
    except Exception:
        logger.warning("[outside] 审批请求失败(按拒绝处理)", exc_info=True)
        approved = False

    if not approved:
        return None, (
            f"读取工作区外路径的请求未获批准（{raw_path}）。"
            "不要重复申请同一路径；如确有必要，请先用一句话向用户说明理由，"
            "否则请改读工作目录内的文件。"
        )
    return target, ""
