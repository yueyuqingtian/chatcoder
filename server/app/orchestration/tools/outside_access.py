"""工作区之外的读取路径解析（v36 plan-321-1600 R2 引入，plan-75-332 改造）。

改造前：本模块为 fs_read / fs_list / fs_grep 的越界读取单独弹一张审批卡，
内部自带一套判定（完全访问沙箱免审 → exec_policy 规则 → 全局自动审批开关 → 弹窗）。
与 executor 的审批门重复，表现为"同一个项目里存在两种审批入口"。

现在：工具**不再拦截任何访问**。越界读取直接解析并放行，是否询问用户由
`approval_policy.decide()` 统一裁决：

- 动作类别 `read_outside`；
- 询问审批 → 弹审批卡（由输入框位置的统一审批组件呈现）；
- 自动审批 / 完全访问 → 直接放行。

写盘方向的越界由 `approval_policy` 归为 `write_outside`（自动审批下仍需确认）。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_outside_target(ctx, raw_path: str) -> tuple[Path | None, str]:
    """解析一个绝对/相对路径（**不限工作区边界**）。

    返回 (target, error)：
    - target 非 None：放行（Path 对象，调用方自行做存在性/类型检查）；
    - target 为 None 且 error 非空：路径本身非法（空串、无法 realpath 等）。

    注意：这里只做纯路径解析，不做任何授权判断——工具的职责是"解析并执行"，
    是否该问用户由审批策略决定。
    """
    from app.orchestration.tools.safe_path import resolve_loose

    try:
        target_str = resolve_loose(raw_path, getattr(ctx, "workspace_root", None))
    except Exception:  # noqa: BLE001 —— 解析异常按非法路径处理，不让工具崩在参数上
        logger.debug("[outside] 路径解析异常 %r", raw_path, exc_info=True)
        return None, f"路径非法或无法解析: {raw_path}"
    if not target_str:
        return None, f"路径非法或无法解析: {raw_path}"
    return Path(target_str), ""
