"""任务复杂度评估与结构化拆分（v38 已退役）。

plan-482: 分步决策权完全交给模型——主代理通过 todo_write 自主维护执行清单，
系统不再做复杂度评估（evaluate_complexity）与请求拆解（decompose_request）。
历史实现见 git 记录；保留本模块避免历史导入路径报错。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlannedStep:
    """历史拆分步骤结构（保留供旧数据/旧测试引用，无活跃生产者）。"""
    title: str
    summary: str = ""
    acceptance: str = ""
    depends_on: list[int] = field(default_factory=list)
    estimate: int | None = None
    supported: bool = True
    unsupported_reason: str | None = None
