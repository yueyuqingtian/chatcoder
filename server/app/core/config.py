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
    # v14: 附件上传目录(文件实际落盘位置, AI 通过 read_attachment 工具读取)
    uploads_dir: str = "./uploads"
    # v0.9: 默认模型上下文窗口(对齐 deepseek-harness 默认 1M；模型级 context_window 配置会覆盖)
    default_context_window: int = 1000000
    # v3.0: 上下文压缩相关配置（对齐 codex openai_models.rs）
    # 是否启用自动压缩
    context_compaction_enabled: bool = True
    # v6.1: 对齐 codex -- auto_compact_token_limit = context_window * 90%
    auto_compact_threshold_ratio: float = 0.90
    # v15: API 副本折叠阈值 —— 上下文占用低于此比例时 build_api_copy 不折叠任何
    # tool result（保留全部读取内容，根治"模型失忆反复读文件"）；高于此比例才
    # 按 keep_recent_groups 折叠旧工具输出，与下层 auto_compact(90%) 形成两级降级。
    api_copy_fold_ratio: float = 0.70
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
    # 注意：thinking 模式（DeepSeek/GLM 系）下 temperature 会被 provider 层移除
    # （网关不支持/固定内部采样，对齐 deepseek-harness/zcode），此处仅作用于未开思考的轮次。
    agent_tool_temperature: float = 0.3
    agent_text_temperature: float = 0.7

    # v6.5: 模型单次输出最大 token 数。部分网关默认值很小（如 4096），
    # 导致长报告/多工具调用被截断。显式设置避免网关默认值截断。
    # 设为 0 则不传（用网关默认）。
    # 对齐 deepseek-harness（默认 256k）/zcode（deepseek-v4 默认 128k）：
    # 默认提到 131072（glm-5.2 网关上限约 65536-131072，131072 安全）。
    agent_max_output_tokens: int = 131072

    # v28: 模型流式响应空闲超时（秒）——SSE 流两次数据帧之间的最大间隔。
    # 根因修复：kimi-k3/grok-4.6 等长思考模型在思考阶段 SSE 可静默超过 30 秒，
    # 旧硬编码 30s 触发 httpx.ReadTimeout，回退非流式后空响应被静默吞掉，
    # 表现为"运行中突然停止且无报错"。默认 180s 覆盖长思考，可按模型/网关调整。
    # ta3_stream_idle_timeout: ta3 提供者（Anthropic/OpenAI 双协议）的 httpx read 超时。
    # provider_stream_idle_timeout: openai_compatible 提供者每个 stream chunk 的超时。
    # v28.1: 默认 180s → 300s——high 思考档位（agent_reasoning_effort 默认 high）
    # 的长思考模型思考阶段可静默超过 3 分钟，180s 仍会被误杀导致"长思考报错"。
    ta3_stream_idle_timeout: int = 300
    provider_stream_idle_timeout: int = 300

    # v3.0 (plan-88): 计划模式是否允许访问工作区外路径。
    # 默认关闭：plan 模式下 terminal_exec 的 cwd 被限制在工作区内，越界静默回退。
    # 开启后 plan 会话中的命令可以访问工作区外目录（执行记录标记 outside_access 供审计）。
    plan_mode_allow_outside_access: bool = False

    # v32 (plan-89): 沙箱模式（设置中心「常规」可配置，config.json 持久化）——
    # 三态：workspace-write 默认走审批门；read-only 拒绝写盘与高危命令（最高优先级，
    # 即使自动批准也不可绕过）；danger-full-access 免审批卡。
    # 与现有设置的关系：danger-full-access / auto_approve_tools 均尊重
    # force_approval_tools（"始终需要审批的工具"是最高例外，仍弹审批卡）。
    # 生效优先级：项目 .chatcoder/config.toml 或 profile 显式配置 > 此处全局设置。
    sandbox_mode: str = "workspace-write"

    # v2.2 (plan-88): 回滚写盘记录上限——超过该大小的文件不写入 RollbackWrite 文本
    # 前后内容（视为二进制走 checkpoint 兜底），避免大文件膨胀数据库。
    rollback_record_max_bytes: int = 1048576

    # v2.2 (plan-88): checkpoint 生命周期管理（.chatcoder/checkpoints 目录膨胀治理）
    # - checkpoint_cleanup_on_rollback: 回滚成功后删除该 turn 的 checkpoint 文件
    # - checkpoint_retention_days: GC 删除超过 N 天未访问（mtime）的 checkpoint
    # - checkpoint_max_files / checkpoint_max_mb: GC 总量上限，超出按 mtime 从旧删
    # - checkpoint_gc_interval_turns: 每 N 个 turn 完成触发一次 GC（当前工作区）
    checkpoint_cleanup_on_rollback: bool = True
    checkpoint_retention_days: int = 14
    checkpoint_max_files: int = 2000
    checkpoint_max_mb: int = 200
    checkpoint_gc_interval_turns: int = 20

    # v29 (plan-78): 空响应/超时中断自动重试 + 思考档位降级 ——
    # kimi-k3 等长思考模型在思考阶段被网关提前终结 SSE 流（无任何产出帧）时，
    # 旧行为直接把 turn 标记 fatal 终止（"模型返回空响应 (finish_reason=stop)"）。
    # 空响应多为瞬时故障，重试 + 降思考档位往往能恢复：
    # - agent_empty_response_retries: 空响应重试次数（0 = 关闭重试）
    # - agent_empty_retry_efforts: 每次重试的 effort 降档序列（逗号分隔；
    #   空项表示不传 effort；"none" 表示关闭 thinking）
    # - ta3_kimi_thinking_effort: kimi 系思考档位兜底（kimi 官方档位 low/high/max，
    #   默认保守取 low，避免 high/max 长思考放大网关断流概率）
    # - ta3_thinking_watchdog: 思考看门狗（秒）——思考阶段（已收到 thinking 帧、
    #   尚未收到 content/tool 帧）连续空闲超过此值主动终止并走重试降级，
    #   对齐参考项目 ta3-new-coder 的 thinking 看门狗 120s。
    agent_empty_response_retries: int = 2
    agent_empty_retry_efforts: str = "low,none"
    # v35: 重试间隔（秒）——空响应/瞬时故障重试前等待，避免背靠背重试打爆网关
    agent_retry_interval_seconds: float = 10.0
    ta3_kimi_thinking_effort: str = "low"
    ta3_thinking_watchdog: int = 240

    # v21: thinking 模式（对齐 deepseek-harness serialize.ts / zcode）
    # - agent_thinking_enabled: 主开关，开启后对支持 thinking 的网关（DeepSeek/GLM/Kimi 系）
    #   发送 thinking:{type:"enabled", budget_tokens}，确保推理模型真正进入思考模式。
    #   （此前只发 reasoning_effort，DeepSeek 官方 API 不识别该字段，思考模式从未生效）
    # - agent_thinking_budget_tokens: 默认思考预算（zcode 默认 1024；effort 档位另有映射）
    agent_thinking_enabled: bool = True
    agent_thinking_budget_tokens: int = 1024

    # v21: 工具执行超时（秒）——旧硬编码 120s 对编译/测试/安装经常不够，
    # 超时取消后模型看不到结果只能猜测，观感"变笨"。改为可配置，默认 600s。
    tool_exec_timeout_sec: int = 600

    # v1.1: 工具输出字符阈值分级（痛点1：替代全局 3000 硬截断）
    # v15: fs_read 上限提到 16000，与内存/落库截断对齐，避免大文件一次读不全被迫分页重读
    tool_output_chars_read: int = 16000        # fs_read / fs_list
    tool_output_chars_grep: int = 16000        # fs_grep / codebase_search
    tool_output_chars_terminal: int = 12000    # terminal_exec
    tool_output_chars_web: int = 8000          # web_fetch / web_search
    tool_output_chars_write: int = 500          # fs_write 回执
    tool_output_chars_default: int = 8000       # 兜底

    # v1.1: 增强搜索（ripgrep）；关闭或无 rg 时 fs_grep 回退纯 Python 逐行匹配
    enhanced_search: bool = True
    # v1.1: 消息流展示开关（后端缓存值，前端启动时拉取）
    show_todos: bool = True
    show_reasoning: bool = True

    # v2.2: AI 主动生成记忆开关（设置中心「记忆」面板；关闭后 turn 结束不再自动提取记忆）
    auto_memory_enabled: bool = True

    # v1.1: 上下文预算集中化（痛点5：替代散落的魔法常量）
    # v21: 主会话窗口比例 0.15 → 0.30 —— 旧值导致超过 15% 窗口的历史被静默丢弃
    # （无摘要兜底），多轮任务越聊越"失忆"。对齐 harness 的长会话保持策略。
    context_main_window_ratio: float = 0.30
    context_main_summarize_ratio: float = 0.15     # 主会话摘要触发阈值比例
    context_main_summarize_batch_ratio: float = 0.10  # 每次摘要目标 token 量比例
    context_thread_window_ratio: float = 0.30      # 子代理线程窗口比例
    context_auto_compact_threshold_ratio: float = 0.90  # 自动压缩触发阈值比例
    # v6.1: reasoning_effort 控制推理深度（对齐 codex ReasoningEffort enum）
    # codex 默认 Medium；可选 none/minimal/low/medium/high/xhigh/max
    # none 表示不传（用模型默认，兼容不支持 reasoning 的模型/网关）
    # v21: 默认 medium → high —— 对齐 deepseek-harness 默认 high/max，
    # 与 thinking 模式联动后确保推理深度达标。
    agent_reasoning_effort: str = "high"
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
    # v10: 单个 turn 的子代理数量上限——主代理 spawn_subagent 的硬性限制。
    # 超过上限时拒绝新子代理并提示主代理合并/串行处理，防止无限拆分导致资源失控。
    max_subagents_per_turn: int = 6
    # v13: 任务规划与拆分
    task_split_confirm: bool = True
    # v2.2 (对齐 zcode 3.9): todo 提醒间隔——模型维护的执行清单连续 N 步未更新时
    # 注入 system 提醒（防"开清单后跑偏"，对齐 ZCode buildTodoReminderBody）
    todo_reminder_interval: int = 3
    complexity_direct_max_chars: int = 15
    complexity_llm_timeout: float = 15.0
    task_fail_policy: str = "continue"  # continue | abort
    task_retry_count: int = 1
    plan_mode_auto_split: bool = True

    # v23: ta3（Ta+3 牛码）供应商 —— Electron 同族 UA（风控伪装，可覆盖）
    ta3_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "ta3-new-coder-desktop/1.0.0 Chrome/126.0.0.0 Electron/31.0.0 Safari/537.36"
    )

    # v24: workbuddy（腾讯 CodeBuddy/WorkBuddy）供应商
    workbuddy_endpoint: str = "https://copilot.tencent.com"  # 可覆盖（staging 调试）
    workbuddy_user_agent: str = (
        "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"
    )

    # v25: TRAE SOLO CN（字节）供应商 —— 方案 docs/plan-trae-solo-provider-integration.md
    trae_agent_endpoint: str = "https://trae-api-cn.mchost.guru"   # Agent/LLM API 主机
    trae_account_endpoint: str = "https://api.trae.cn"             # 账号/认证 API
    trae_console_host: str = "https://www.trae.cn"                 # 授权页
    trae_client_id: str = "en1oxy7wnw8j9n"  # SOLO stable（product.json authConfig）
    trae_ide_version: str = "0.1.51"                               # 对齐 appVersion（模拟环境用）
    trae_app_id: str = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"      # x-app-id 固定值
    trae_app_version_code: str = "20260806"                        # x-app-version-code
    trae_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "TRAE-SOLO-CN/0.1.51 Chrome/124.0.0.0 Electron/31.0.0 Safari/537.36"
    )

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
    def agent_empty_retry_effort_list(self) -> list[str]:
        """v29 (plan-78): 解析空响应重试的 effort 降档序列。

        空项 = 不传 effort（沿用默认思考）；"none" = 关闭 thinking。
        """
        return [t.strip() for t in self.agent_empty_retry_efforts.split(",") if t.strip()]

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
