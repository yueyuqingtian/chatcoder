"""智能组队服务（v3：已废弃，保留预设数据供历史引用）。

v3 重构改为「项目内任务驱动」，不再需要设置团队（见需求 1）。
原 Team/TeamAgent/AgentTemplate 模型已移除。
_PROFILES / EXTRA_TEMPLATES 数据保留，避免历史测试与引用崩溃；
create_smart_team / _ensure_templates 改为桩，调用即抛 RuntimeError。
"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


_PROFILES: dict[str, dict[str, Any]] = {
    "web_app": {
        "name": "Web 应用开发小组",
        "description": "全栈 Web 应用开发团队，覆盖产品、设计、前后端、测试、审查",
        "roles": [
            "leader",
            "ui_designer",
            "frontend",
            "backend",
            "qa",
            "reviewer",
        ],
        "leader_role": "leader",
        "workflow": [
            "需求分析与产品设计",
            "UI/UX 设计",
            "技术架构设计",
            "前端开发",
            "后端开发",
            "测试验证",
            "代码审查与交付",
        ],
    },
    "backend_service": {
        "name": "后端服务开发小组",
        "description": "专注后端 API 与服务开发的团队",
        "roles": [
            "leader",
            "architect",
            "backend",
            "qa",
            "reviewer",
        ],
        "leader_role": "leader",
        "workflow": [
            "需求分析",
            "架构设计",
            "后端实现",
            "测试验证",
            "代码审查与交付",
        ],
    },
    "frontend_only": {
        "name": "前端开发小组",
        "description": "专注前端界面与交互的团队",
        "roles": [
            "leader",
            "ui_designer",
            "frontend",
            "qa",
            "reviewer",
        ],
        "leader_role": "leader",
        "workflow": [
            "需求分析",
            "UI 设计",
            "前端实现",
            "测试验证",
            "代码审查与交付",
        ],
    },
    "fullstack_minimal": {
        "name": "极简全栈小组",
        "description": "最小可行团队：Leader + 全栈开发 + 审查",
        "roles": [
            "leader",
            "fullstack",
            "reviewer",
        ],
        "leader_role": "leader",
        "workflow": [
            "需求分析与设计",
            "全栈开发",
            "测试与审查",
        ],
    },
    "data_ai": {
        "name": "数据与 AI 项目小组",
        "description": "面向数据分析、机器学习项目的团队",
        "roles": [
            "leader",
            "data_scientist",
            "backend",
            "qa",
            "reviewer",
        ],
        "leader_role": "leader",
        "workflow": [
            "需求分析与数据理解",
            "数据探索与建模",
            "工程化实现",
            "测试验证",
            "代码审查与交付",
        ],
    },
}


EXTRA_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "ui_designer",
        "name": "UI/UX 设计师",
        "role": "ui_designer",
        "system_prompt": (
            "你是团队的 UI/UX 设计师。\n"
            "职责:根据产品需求设计用户界面与交互流程、产出设计稿与设计规范、"
            "关注用户体验与视觉一致性、与前端工程师协作实现设计。\n"
            "产出需包含:页面布局、组件设计、交互说明、设计规范。"
        ),
        "tool_whitelist": ["fs_read", "fs_list", "fs_write", "web_fetch"],
        "default_model_level": 2,
    },
    {
        "key": "fullstack",
        "name": "全栈工程师",
        "role": "fullstack",
        "system_prompt": (
            "你是团队的全栈工程师。\n"
            "职责:独立负责前端与后端开发、搭建项目骨架、实现功能端到端闭环。\n"
            "代码需可运行、结构清晰、必要时附说明文档。"
        ),
        "tool_whitelist": [
            "fs_read", "fs_list", "fs_write",
            "editor_apply_diff", "terminal_exec",
            "web_fetch",
        ],
        "default_model_level": 2,
    },
    {
        "key": "data_scientist",
        "name": "数据科学家",
        "role": "data_scientist",
        "system_prompt": (
            "你是团队的数据科学家。\n"
            "职责:数据分析、特征工程、模型选型与训练、效果评估、数据可视化。\n"
            "产出需包含:分析报告、模型代码、评估指标、可视化结果。"
        ),
        "tool_whitelist": [
            "fs_read", "fs_list", "fs_write",
            "editor_apply_diff", "terminal_exec",
            "web_fetch",
        ],
        "default_model_level": 3,
    },
    {
        "key": "devops",
        "name": "DevOps 工程师",
        "role": "devops",
        "system_prompt": (
            "你是团队的 DevOps 工程师。\n"
            "职责:CI/CD 流水线搭建、部署脚本编写、基础设施配置、监控告警、性能优化。\n"
            "产出需包含:配置文件、部署脚本、流水线定义、操作文档。"
        ),
        "tool_whitelist": [
            "fs_read", "fs_list", "fs_write",
            "editor_apply_diff", "terminal_exec",
            "ci_run",
        ],
        "default_model_level": 2,
    },
]


async def list_profiles() -> list[dict[str, Any]]:
    """列出所有预设团队配置。"""
    return [
        {
            "type": k,
            "name": v["name"],
            "description": v["description"],
            "roles": v["roles"],
            "workflow": v["workflow"],
        }
        for k, v in _PROFILES.items()
    ]


async def create_smart_team(
    db: AsyncSession,
    *,
    project_type: str = "web_app",
    team_name: str | None = None,
    project_description: str = "",
) -> dict[str, Any]:
    """v3：团队功能已废弃，调用即抛错。"""
    raise RuntimeError("团队功能已在 v3 移除，改为项目内任务驱动")


async def _ensure_templates(
    db: AsyncSession, roles: list[str]
) -> dict[str, Any]:
    """v3：团队功能已废弃，调用即抛错。"""
    raise RuntimeError("团队功能已在 v3 移除，改为项目内任务驱动")


_DEFAULT_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "pm_leader",
        "name": "产品经理 / Leader",
        "role": "leader",
        "system_prompt": (
            "你是 chatcoder 开发团队的 Leader(兼产品经理)。\n"
            "职责:接收用户需求、澄清歧义、拆解任务 DAG、分配负责人、调度协作、汇聚产物发起审核、冲突裁决。\n"
            "拆解原则:任务粒度适中、依赖清晰、可并行标注、每任务有验收标准。\n"
            "产出需以 JSON Schema 输出,不附加额外说明。"
        ),
        "tool_whitelist": ["fs_read", "fs_list"],
        "default_model_level": 3,
    },
    {
        "key": "architect",
        "name": "架构师",
        "role": "architect",
        "system_prompt": (
            "你是团队的架构师。\n"
            "职责:技术选型、模块划分、接口设计、数据模型设计、关键时序图与 ADR。\n"
            "产出需明确模块边界、依赖关系、风险点。"
        ),
        "tool_whitelist": ["fs_read", "fs_list", "fs_write", "editor_apply_diff"],
        "default_model_level": 3,
    },
    {
        "key": "frontend",
        "name": "前端工程师",
        "role": "frontend",
        "system_prompt": (
            "你是团队的前端工程师。\n"
            "职责:依据设计与接口实现前端界面与交互、编写组件、对接 API。\n"
            "代码需可运行、结构清晰、必要时附简要说明。"
        ),
        "tool_whitelist": ["fs_read", "fs_list", "fs_write", "editor_apply_diff", "terminal_exec"],
        "default_model_level": 2,
    },
    {
        "key": "backend",
        "name": "后端工程师",
        "role": "backend",
        "system_prompt": (
            "你是团队的后端工程师。\n"
            "职责:实现 API、业务逻辑、数据访问层、必要脚本。\n"
            "代码需可运行、含最小可执行示例、关键决策附注释。"
        ),
        "tool_whitelist": ["fs_read", "fs_list", "fs_write", "editor_apply_diff", "terminal_exec"],
        "default_model_level": 2,
    },
    {
        "key": "qa",
        "name": "测试工程师",
        "role": "qa",
        "system_prompt": (
            "你是团队的测试工程师。\n"
            "职责:依据验收标准编写并执行测试用例、报告缺陷、回归验证。\n"
            "产出含测试覆盖点与执行结果。"
        ),
        "tool_whitelist": ["fs_read", "fs_list", "fs_write", "terminal_exec"],
        "default_model_level": 2,
    },
    {
        "key": "reviewer",
        "name": "代码审查员",
        "role": "reviewer",
        "system_prompt": (
            "你是团队的代码审查员(魔鬼代言人)。\n"
            "职责:审查产物质量、强制列出反对意见与风险、给出通过/驳回结论。\n"
            "可用工具:fs.read/fs.list 读代码,ci.run 客观验证 lint/test/build。\n"
            "结论需基于事实,不唯上、不盲从。"
            "输出格式:第一行 PASS 或 REJECT,随后给出理由与证据(引用 ci 结果或代码行)。"
        ),
        "tool_whitelist": ["fs_read", "fs_list", "ci_run"],
        "default_model_level": 2,
    },
]
