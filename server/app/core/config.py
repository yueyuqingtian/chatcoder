"""集中式配置：从环境变量读取，pydantic-settings 校验。"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    server_host: str = "0.0.0.0"
    server_port: int = 8000
    debug: bool = True

    database_url: str = "postgresql+asyncpg://chatcoder:chatcoder_dev@localhost:5432/chatcoder"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    default_llm_provider: str = ""
    default_llm_base_url: str = ""
    default_llm_api_key: str = ""
    default_llm_model: str = ""
    default_llm_api_format: str = "openai"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 168

    cors_origins: str = "http://localhost:5173,http://localhost:4173"
    # 桌面版: 允许所有源(Electron 前端从 file:// 加载,Origin 为 null)
    cors_allow_all: bool = False

    # v0.3: 工作区根目录(服务端工具执行沙箱边界)
    workspace_root: str = "./workspace"
    # v0.9: 默认模型上下文窗口(支持 500k 长上下文)
    default_context_window: int = 500000
    # v3.0: 上下文压缩相关配置（对齐 codex openai_models.rs）
    # 是否启用自动压缩
    context_compaction_enabled: bool = True
    # v6.1: 对齐 codex -- auto_compact_token_limit = context_window * 90%
    auto_compact_threshold_ratio: float = 0.90
    # v0.3: 计划确认门 — 默认硬门,需用户确认拆解后才执行
    auto_confirm_plan: bool = False
    # v0.3: 审批超时(秒),超时自动拒绝
    approval_timeout_sec: int = 300
    # v0.3: agent loop 单任务最大步数（仅作为绝对兜底防死循环的熔断值）
    # v3.5: 不再用固定步数限制，改为边际效应递减检测 + token 预算驱动
    # 此值仅在递减检测失效时作为最后保险触发
    agent_max_steps: int = 1000
    # 验证模式:自动批准副作用工具(fs.write/terminal.exec 等),跳过人工审批门
    # v1.0: 默认关闭，高风险工具始终需要审批
    auto_approve_tools: bool = False
    # v1.0: 强制审批工具列表——即使 auto_approve_tools=True 也不可跳过
    force_approval_tools: str = "terminal_exec,ci_run,browser_navigate,browser_click,browser_type"

    # v6.2: 工具调用温度 —— 不能照抄 codex 的"不传 temperature"！
    # codex 走 Responses API + reasoning 模式，温度被内部处理，不传=安全。
    # 本项目走 Chat Completions API，不传 temperature = 网关默认 1.0，
    # 高温会显著破坏 function calling 格式稳定性 → 模型把工具调用写成纯文本。
    # 因此工具调用场景必须显式低温度（0.3 兼顾格式稳定与复杂推理），
    # 纯文本场景可稍高（0.7）。
    agent_tool_temperature: float = 0.3
    agent_text_temperature: float = 0.7

    # v1.1: 工具输出字符阈值分级（痛点1：替代全局 3000 硬截断）
    tool_output_chars_read: int = 12000        # fs_read / fs_list
    tool_output_chars_grep: int = 16000        # fs_grep / codebase_search
    tool_output_chars_terminal: int = 12000    # terminal_exec
    tool_output_chars_web: int = 8000          # web_fetch / web_search
    tool_output_chars_write: int = 500          # fs_write 回执
    tool_output_chars_default: int = 8000       # 兜底

    # v1.1: 上下文预算集中化（痛点5：替代散落的魔法常量）
    context_main_window_ratio: float = 0.15        # 主会话窗口占 context_window 比例
    context_main_summarize_ratio: float = 0.15     # 主会话摘要触发阈值比例
    context_main_summarize_batch_ratio: float = 0.10  # 每次摘要目标 token 量比例
    context_thread_window_ratio: float = 0.30      # 子代理线程窗口比例
    context_auto_compact_threshold_ratio: float = 0.90  # 自动压缩触发阈值比例
    # v6.1: reasoning_effort 控制推理深度（对齐 codex ReasoningEffort enum）
    # codex 默认 Medium；可选 none/minimal/low/medium/high/xhigh/max
    # none 表示不传（用模型默认，兼容不支持 reasoning 的模型/网关）
    agent_reasoning_effort: str = "medium"
    # v6.1: 是否使用 developer 角色注入上下文片段（对齐 codex ContextualUserFragment）
    # True = 透传 role="developer" 给 API（OpenAI 官方模型支持，格式遵循率最高）
    # False = 转成 system 角色（兼容 DeepSeek/GLM 等只认 system/user/assistant/tool 的网关）
    use_developer_role: bool = False

    # v6.0: 自动压缩配置（对齐 codex/claude code 主动 auto-compact）
    auto_compact_keep_rounds: int = 6  # 压缩时保留最近 N 个完整工具回合
    auto_compact_min_reclaim_tokens: int = 2000  # 可回收 token 低于此值则跳过（避免无价值 LLM 调用）
    # v6.0: 主会话超级摘要配置（context_memory）
    super_summary_trigger: int = 5  # 摘要条数超过此值触发超级摘要
    super_summary_keep_latest: int = 3  # 超级摘要保留最新 N 条不压缩

    # v4.6: 单会话 token 预算--超过此值任务自动终止
    session_token_budget: int = 2_000_000

    @field_validator("cors_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return ",".join(o.strip() for o in v.split(",") if o.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return self.cors_origins.split(",")

    @property
    def force_approval_tools_list(self) -> list[str]:
        """v1.0: 解析强制审批工具列表。"""
        return [t.strip() for t in self.force_approval_tools.split(",") if t.strip()]

    @property
    def default_model_ready(self) -> bool:
        return bool(self.default_llm_base_url and self.default_llm_api_key and self.default_llm_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def update_workspace_root(path: str) -> None:
    """运行时更新工作区根目录(桌面版用户可自由选择)。"""
    settings.workspace_root = path


def resolve_workspace_root(session_workspace: str | None = None) -> str:
    """解析实际工作区根目录:优先 session 级,兜底全局。

    session_workspace 为空字符串或 None 时用全局 settings.workspace_root。
    """
    if session_workspace and session_workspace.strip():
        return session_workspace.strip()
    return settings.workspace_root


# ───────────────────────────────────────────────────────────────────
# 废弃: resolve_llm_params (v3.4) —— 不再按智能等级限制参数
# 保留: max_steps 仍用于兜底熔断，但默认值大幅放宽到 1000


def resolve_max_steps(intelligence_level: int | None = None) -> int:
    """解析 Agent 的步数上限（仅作为兜底熔断，非能力限制）。

    v3.5: 不再按智能等级限制步数。所有等级都使用统一的宽松上限，
    实际终止由边际效应递减检测和 token 预算驱动。
    """
    return settings.agent_max_steps
