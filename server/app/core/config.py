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
    server_port: int = 12973
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
    # v6.1: 对齐 codex -- auto_compact_token_limit = context_window * 90%
    auto_compact_threshold_ratio: float = 0.90
    # v16: 已移除 api_copy_fold_ratio（按占用比例折叠工具结果的隐式压缩）——
    # 历史只在超过上面的压缩阈值后由 agent_loop 的落库式压缩（可见卡片）处理；
    # build_api_copy 仅保留单条超长工具结果的落盘保护，不再按占比改写历史。
    # v0.3: 计划确认门 — 默认硬门,需用户确认拆解后才执行
    auto_confirm_plan: bool = False
    # v0.3: 审批超时(秒),超时自动拒绝
    approval_timeout_sec: int = 300
    # plan-522-2209: 子会话 / 子代理的审批等待上限（秒），0 或负数 = 不超时，只等用户处理或 turn 被取消。
    # 子任务的审批请求往往发生在用户并未停留的会话里，超时自动拒绝会让用户既看不到请求、
    # 也看不到失败原因，只看到子任务莫名中断；因此这一类不设静默超时。
    approval_timeout_sec_child: int = 0
    # v0.3: agent loop 单任务最大步数（仅作为绝对兜底防死循环的熔断值）
    # v3.5: 不再用固定步数限制，改为边际效应递减检测 + token 预算驱动
    # 0 或负数表示不限制步数；设置页已移除该项，固定为不限制（旧配置里的值不再读取）
    agent_max_steps: int = 0
    # 内置浏览器工具开关（设置 → 电脑操控 → 内置浏览器；开启电脑操控时自动打开）。
    # plan-20261015-browser-agent：默认开启——工具操作的是右侧面板里用户看得见、随时可接管的浏览器，
    # 外部网页的点击/输入仍按权限模式审批；关闭后浏览器工具不再下发给模型。
    browser_enabled: bool = True
    # 仅作用于无桌面端连接时的后台兜底浏览器（Playwright）：是否无头运行
    browser_headless: bool = True

    # ── 电脑操控（plan-334-1661）──
    # 总开关：控制 desktop_* 工具集是否可用。
    # plan-20261018-operator-boost：改为默认开启——写操作仍受「仅前台窗口操作」护栏与权限模式审批约束，
    # 用户要求开箱即可让 AI 操控电脑做自测与文件核对。
    desktop_enabled: bool = True
    # 普通电脑操作子开关（点击/输入/截图/UIA 感知等）
    desktop_plain_ops_enabled: bool = True
    # 安全：写操作是否要求目标窗口处于前台。默认开启——这是防止「输入打到错误窗口」
    # 的关键护栏，关闭后风险自负。
    desktop_require_foreground: bool = True
    # 截图默认画质（JPEG）与缩放长边（0 = 自动适配模型可见尺寸）。
    # plan-20261018-operator-boost：画质默认 100；长边 0 起初表示「不缩放」，实测整屏原图会被模型服务端
    # 再次缩小、模型按缩小后的图读坐标，点击整体偏到窗口外——因此 0 改为「自动缩到模型可见尺寸」，
    # 显式设置的值也不会超过该尺寸（见 tools/desktop.py _model_safe_max_dim）。
    desktop_screenshot_quality: int = 100
    desktop_screenshot_max_dim: int = 0
    # 操作路线沉淀：命中已有路线时复用「通用原则」，降低模型思考与推理开销
    desktop_recipe_enabled: bool = True
    # plan-20261019-desktop-precision：坐标口径（auto / pixel / norm1000）。默认按模型族自动选择；
    # plan-20261019-desktop-targeting：改为设置页可见——个别模型名识别不准、或模型对口径不适应（实录里
    # deepseek-flash 在 0–1000 口径下 x 轴换算了、y 轴没换）时，可在「设置 → 电脑操控」里手动指定。
    # 截图元素编号、点击前校验、破坏性操作词表随 OCR 一并移除（识别结果不可用、链路慢数百毫秒）。
    desktop_coord_mode: str = "auto"
    # plan-20261019-desktop-targeting：截图 / 动作后自动附带「页面元素」编号清单（来自无障碍树，含网页内容）。
    # 默认开启；只读 config.json、不在设置页展示——嫌上下文偏长时可手工关闭，关闭后仍可 desktop_snapshot 手动读取。
    desktop_auto_elements: bool = True

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

    # v6.5: 模型单次输出最大 token 数。
    # 不限制输出：默认 0 = 请求不携带 max_tokens 字段，由网关/模型自身输出上限决定；
    # 例外：官方直连 api.anthropic.com 该字段必填，由 anthropic 适配器兜底为软件输出上限 128K。
    # 需要显式上限时（如个别网关默认值很小导致截断）在设置中心「最大输出」中配置。
    agent_max_output_tokens: int = 0

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
    # plan-248-1258 M2.2: 供应商凭据（多 Key/多账号）失败后的冷却时长（秒）。
    # 冷却期内该凭据被轮询跳过，到点自动恢复；避免每次都先撞已挂的 key 再切换。
    provider_credential_cooldown_seconds: int = 300
    # plan-290: 凭据「连续失败几次才冷却」阈值。瞬时抖动（网络闪断、网关偶发 5xx、
    # 单次超时）不应立刻把凭据踢进冷却——否则一次抖动就会让可用账号/Key 被闲置 5 分钟，
    # 表现为「明明有 Key 却报无可用凭据」。连续失败达到该次数才真正冷却；
    # 期间失败只累计计数并记录错误，凭据仍可继续使用（下次调用可再试）。
    # 设为 1 则恢复旧行为（首次失败即冷却）。
    provider_credential_fail_threshold: int = 3

    # plan-248-1258 M2.6: WorkBuddy 每日自动签到（打开软件后后台自动签到）。
    # 默认开启；单测/CI 可关。签到按账号每日一次，失败限次重试。
    workbuddy_auto_checkin: bool = True

    # plan-248-1258 M3.1: 已开启索引的工作区自动增量更新（后台循环）。
    # 默认开启；sha1 短路使无变更时几乎零成本。
    symbol_index_auto_update: bool = True

    # v3.0 (plan-88): 计划模式是否允许访问工作区外路径。
    # plan-75-332：**已废弃**——cwd 越界限制已取消（工具自身不再限制），
    # 越界访问是否需询问由权限模式统一裁决。字段保留仅为兼容旧配置文件，
    # 不参与任何判定。
    plan_mode_allow_outside_access: bool = False

    # v32 (plan-89): 沙箱模式。
    # plan-75-332：**已废弃**——三态沙箱的硬边界已被「执行模式×权限模式」取代
    # （read-only 归执行模式，danger-full-access 归权限模式的「完全访问」）。
    # 字段保留仅为兼容旧配置文件与既有赋值位，不参与任何判定。
    sandbox_mode: str = "workspace-write"

    # v36 (plan-321-1600 R2): 工作目录外读取的自动审批开关。
    # plan-75-332：**已废弃**——越界读取不再由工具弹卡，改由权限模式裁决。
    # 字段保留仅为兼容旧配置文件，不参与任何判定。
    auto_approve_outside_read: bool = False

    # v36 (plan-321-1600 R3): ta3 额度自动透支——模型请求报错时查一次额度，
    # 日额度 >=100% 时自动尝试透支一次（带冷却；日额度后透支本周额度）。
    auto_overdraft_on_quota_exceeded: bool = False

    # plan-75-332: 审批解释专用模型 / 专用思考深度（设置中心「执行策略 → 审批解释」）。
    # 空值 = 跟随会话（会话模型 + 会话思考深度）；非空则覆盖，用于把解释这类
    # 轻量分析交给更快/更便宜的模型。
    approval_explain_model_id: int | None = None
    approval_explain_reasoning_effort: str = ""

    # v46: ta3 额度自动重置——模型请求报错时查一次额度，某窗口 >=100% 时自动提交一次
    # 重置（周/月窗走 /quota/reset，日窗无重置能力则退化为透支）。与上面的自动透支
    # 并列，两者各自独立开关，共用同一套冷却与"尽力而为"语义。
    auto_reset_on_quota_exceeded: bool = False

    # plan-230-1144 M1.1: 定时任务调度循环开关。
    # 此前 scheduled_tasks 表只有 CRUD、无任何消费者，用户建的任务永不执行；
    # 现由 services/scheduler_loop.py 的 tick 循环驱动。默认开启，
    # 单测/CI 或只想建任务不想真跑的场合可关。
    scheduler_enabled: bool = True
    # 错过触发点的宽限期：过期超过该时长视为"错过"，按任务 missed_policy 决定补跑或跳过
    scheduler_missed_grace_sec: int = 300

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
    # - (deprecated) ta3_thinking_watchdog: plan-838 起不再读取——思考/空闲统一使用
    #   ta3_stream_idle_timeout。
    agent_empty_response_retries: int = 2
    agent_empty_retry_efforts: str = "low,none"
    # v45: 统一异常重试策略（任何报错都重试，穷尽后才停止并显示报错）。
    # 间隔序列即重试计划：每个元素是一次重试前的等待秒数，个数即重试次数（不再单独配置次数）。
    # 默认 10,20,30 = 共 3 次重试，分别等 10/20/30 秒；空串 = 不自动重试。
    # 由设置-常规面板写入（/settings/global）并持久化到 config.json。
    agent_retry_intervals: str = "10,20,30"
    ta3_kimi_thinking_effort: str = "low"
    # DEPRECATED (plan-838): 思考看门狗已移除——kimi-k3 与 TA3 其它模型统一使用
    # ta3_stream_idle_timeout(300s) 空闲超时，此字段仅保留以兼容旧配置文件，不再被读取。
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
    # 字节口径（UTF-8）：中文 1 字符≈3 字节，按字符计会低估 token。fs_read 的窗口即其自身的单次预算，
    # 不再套用 MAX_TOOL_OUTPUT_CHARS 的头部截断，避免“窗口→截成前段→再读”的循环。
    tool_output_bytes_read: int = 32768        # fs_read 单次窗口上限（行窗口与字节双上限，先到者为准）
    tool_output_chars_read: int = 16000        # 已弃用：仅保留读取兼容，运行时以 tool_output_bytes_read 为准
    # 其余工具同为字节口径（参照 zcode：grep 20000 字节、终端 30000 字节；网页与兜底取中间值）。
    # 终端保留头部 8KB + 尾部 22KB（报错多在尾部），超出部分落盘并给出续读方式。
    tool_output_bytes_grep: int = 20480        # fs_grep / codebase_search
    tool_output_bytes_terminal: int = 30720    # terminal_exec（头 8KB + 尾 22KB 呈现）
    tool_output_bytes_terminal_tail: int = 22528  # 终端输出尾部保留量
    tool_output_bytes_web: int = 12288         # web_fetch / web_search
    tool_output_bytes_default: int = 16384     # 其它工具兜底
    tool_output_chars_grep: int = 16000        # fs_grep / codebase_search
    tool_output_chars_terminal: int = 12000    # terminal_exec
    tool_output_chars_web: int = 8000          # web_fetch / web_search
    tool_output_chars_write: int = 500          # fs_write 回执
    tool_output_chars_default: int = 8000       # 兜底

    # plan-548-2277：「先读后改」闸门——编辑前检查目标行是否已被本会话读到。
    # warn（默认）：放行，但在工具输出里附警告，并在 data 里标 read_verified=False；
    # strict：拒绝并返回 STALE_READ（错误里给可直接复制的 fs_read 调用）；
    # off：完全不检查。
    # 默认 warn 是有意的：机械批改、跨文件批量替换时逐段先读代价过高，
    # 过严的闸门会逼模型绕去 terminal_exec 写文件，反而失去约束意义。
    # plan-549-2281 备注：这两项是**内部旋钮**（仅环境变量 / .env 可改），刻意不进
    # 设置页——默认值即推荐值，进 UI 会让「strict 一开到处拒绝」变成用户可踩的坑。
    edit_gate_mode: str = "warn"
    # 写盘后按文件后缀跑最小检查（compileall / node --check / 项目 tsc），
    # 把错误摘要回灌模型；连续 2 轮仍未通过则如实报告。纯文档 / 样式默认跳过。
    post_edit_check_enabled: bool = True

    # v1.1: 增强搜索（ripgrep）；关闭或无 rg 时 fs_grep 回退纯 Python 逐行匹配
    enhanced_search: bool = True

    # plan-522-2209：git 网络命令的 SSL 后端。Windows 上默认的 schannel 经 HTTP 代理访问
    # GitHub 会握手挂死（实测同一仓库 git pull 挂 357 秒，换 openssl 后秒回）。
    # auto = Windows 且检测到代理环境变量时用 openssl，其余平台/场景用 git 默认。
    git_ssl_backend: str = "auto"   # auto / schannel / openssl / default
    # v31.2: 代理（HTTP/HTTPS），用于搜索引擎等 web 工具的网络请求；
    # 来源：设置面板 http_proxy 字段（持久化在 ~/.chatcoder/config.json），
    # 运行时写入本字段；启动时从 config 恢复。优先于环境变量 HTTP_PROXY/HTTPS_PROXY。
    http_proxy: str = ""
    # v1.1: 消息流展示开关（后端缓存值，前端启动时拉取）
    show_todos: bool = True
    # plan-156-619 S2：消息区搜索入口按钮（左上角）。默认关闭；关闭只隐藏入口，Ctrl+F 仍可唤起搜索条
    show_message_search: bool = False
    # plan-156-619 S4d：子会话默认模型 / 思考深度（0 / 空 = 跟随主代理）
    subagent_model_id: int = 0
    subagent_reasoning_effort: str = ""
    # plan-156-619 S4e：子代理开关（类型级开关已从 UI 移除，由它统一控制）
    subagent_enabled: bool = True
    # plan-549-2281：subsession_enabled 已删除——子会话创建能力下线，无工具可门控。
    # plan-20261018-operator-boost：GUI 操作（电脑操控 / 内置浏览器的多步操作）交给「操控子会话」执行。
    # 操作本身不需要深度推理，却是延迟大户（每步一次模型往返）；交给低思考档的子会话，
    # 主会话保留高思考深度处理设计、编码与结论。只在下发子会话工具的模式下生效。
    # plan-549-2281：operator_subsession_enabled 已删除——GUI 操作改由子代理承担，
    # 能力可用性由 desktop_enabled / browser_enabled 直接决定。
    # plan-549-2281：「操控子代理」独立开关（原 operator_subsession_enabled 的等价物）：
    # 关掉后不再向主代理注入「界面操作交给操控子代理」的提示，界面操作回退到自己做。
    operator_subagent_enabled: bool = True
    # 操控子代理的思考深度（plan-549-2281 由 operator_reasoning_effort 改名）：
    # "lowest" = 当前模型支持的最低非关闭档（默认）；"follow" = 跟随子代理默认设置；
    # 其余为具体档位（none / minimal / low / medium / high …）。
    operator_subagent_effort: str = "lowest"
    # plan-156-619 S7：综合工具卡各栏目的全局默认展开策略（true = 默认全展开）
    tool_card_sections_expanded: bool = True
    # plan-20261019-toolcard-v2：工具卡默认形态（capsule = 右上角悬浮胶囊 / expanded = 展开卡片）
    tool_card_default_form: str = "capsule"
    # 逐栏默认展开（git / plan / process / subagent / workflow）；缺省的栏目回落 tool_card_sections_expanded
    tool_card_section_defaults: dict[str, bool] = {}

    # v2.2: AI 主动生成记忆开关（设置中心「记忆」面板；关闭后 turn 结束不再自动提取记忆）
    auto_memory_enabled: bool = True

    # v1.1: 上下文预算集中化（痛点5：替代散落的魔法常量）
    # v21: 主会话窗口比例 0.15 → 0.30 —— 旧值导致超过 15% 窗口的历史被静默丢弃
    # （无摘要兜底），多轮任务越聊越"失忆"。对齐 harness 的长会话保持策略。
    context_main_window_ratio: float = 0.30
    context_main_summarize_ratio: float = 0.15     # 主会话摘要触发阈值比例
    context_main_summarize_batch_ratio: float = 0.10  # 每次摘要目标 token 量比例
    context_thread_window_ratio: float = 0.30      # 子代理线程窗口比例
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
    # plan-19-82 步骤5: 压缩目标闭环——压缩后总占用应落入目标区间（默认 10%-15%）。
    # 旧实现只切一刀（保留 16%），压缩后仍可能占 40%（512K 窗口），用户反馈效率过低。
    compact_target_ratio: float = 0.12      # 压缩后目标占用（窗口比例），默认 12%
    compact_target_min_ratio: float = 0.10  # 目标区间下界（10%）
    compact_target_max_ratio: float = 0.15  # 目标区间上界（15%，超此值继续迭代压缩）
    compact_max_rounds: int = 5             # 单次压缩最多迭代轮数（防死循环）
    # v6.0: 主会话超级摘要配置（context_memory）
    super_summary_trigger: int = 5  # 摘要条数超过此值触发超级摘要
    super_summary_keep_latest: int = 3  # 超级摘要保留最新 N 条不压缩

    # v4.6: 单会话 token 预算--超过此值任务自动终止
    # plan-474-2105：单会话子代理并发上限：单个会话内同时运行的子代理数；
    # 达到后新的派发直接拒绝（不再排队等待），设置中心可改（1~30）。
    max_concurrent_subagents: int = 4
    # plan-474-2105：单会话子会话并发上限：单个主会话下同时运行的子会话数；
    # 达到后创建直接拒绝。设置中心可改（1~30）。
    # plan-549-2281：max_subsessions_per_parent 已删除——子会话创建能力下线，
    # 不再存在需要封顶的并发资源（存量历史子会话仍可在左栏打开并运行，但不再计限）。
    # ── 工作流运行时（设计 §8）：并发上限与默认返工次数；0 = 不限（仅并发上限支持）──
    workflow_enabled: bool = True
    # 下一步建议卡（设置 → 常规）：默认关闭；开启后才向主会话的智能体 / 工作流模式注入
    # suggest_next_task 工具与对应提示词（子会话、子代理、只读、计划模式一律不注入）
    suggestion_enabled: bool = False
    workflow_max_concurrent_global: int = 3  # 所有会话的工作流子代理合计，保守默认
    workflow_max_concurrent_per_session: int = 2  # 同一会话合计，保守默认
    workflow_max_parallel_nodes_per_flow: int = 2  # 单个子工作流内并行节点
    workflow_default_max_rework: int = 2
    workflow_token_budget_per_run: int = 0
    # 计划生成后是否需要用户确认才开始执行（关闭则规划后直接开始）
    workflow_confirm_before_start: bool = True
    # 网络中断暂停后是否自动续跑一次（默认关闭：不替用户做决定）
    workflow_auto_resume_on_network: bool = False
    # 组装方式：runtime（主代理自主组装，默认）/ presets_only（只允许基于预设或用户模板）
    workflow_composition_mode: str = "runtime"
    # 评估节点的确定性检查：开启时检查失败不得判通过（改判返工）；关闭则只记录结果
    workflow_require_checks: bool = True
    # 返工前确认（plan-workflow-collab）：评估要求返工时先把结论交给主代理核实、由你确认后再返工；
    # 关闭则按评估结论自动返工
    workflow_rework_confirm: bool = True
    # 同文件真冲突的处理：queue 先自动重做、多次失败再进入合并与决定；ask 每次真冲突都请你决定
    workflow_conflict_policy: str = "queue"
    # 允许自动三方合并（两侧改动区域不重叠时直接合并）；关闭则直接转入重做
    workflow_auto_merge: bool = True
    # 子工作流未声明隔离方式时的默认值：shared 共享工作区 / worktree 独立 git 工作树
    workflow_isolation_default: str = "shared"
    # 热点文件（逗号或换行分隔）：计划期若两个并行子工作流都要写入，直接给出串行化建议
    workflow_hot_files: str = (
        "package.json,package-lock.json,CHANGELOG.md,packages/shared/src/events.ts,"
        "server/app/persistence/migrations.py,server/app/gateway/schemas.py,"
        "client/src/store/i18n.ts,server/app/orchestration/engine.py"
    )
    # plan-20261022-subagent-reject-fast：原来的「并发满先排队等待 N 秒」配置已移除。
    # 实测等待期对主代理完全静默、最终仍会被拒绝，等待没有收益；现在并发满直接拒绝。
    # plan-330-1648 M7: 单个子代理的 token 预算上限（0 = 不限）。
    # spawn 时下发为 run_agent_loop 的 token_budget，超限即熔断并按结构化汇报“预算耗尽”，
    # 避免子代理无人看管地无限消耗（此前 spawn 路径从不传预算）。
    subagent_token_budget: int = 0
    # v43: 主代理「等子代理跑完」的上限（秒）——**只作用于等任务完成，不作用于等人答复**。
    # 语义：后台子代理运行时主代理可继续并行工作；主代理做完自己的事（本步无工具调用）
    # 而子代理仍在跑时，turn 不结束——保持会话运行并阻塞等待（前端显「等待子代理结束…」、
    # 左侧转圈、输入框停止按钮），子代理结束后把完成报告注入同一 turn 继续。
    # 超过该上限仍未结束则不再等待，把已有报告注入并结束 turn（剩余结果走完成唤醒兜底）。
    # 0/负数 = 不限制（一直等到用户停止或子代理全部结束）。
    #
    # plan-565-2323 G16：范围收窄，别再按「子代理等待」笼统理解——三处消费点都与「等人」无关：
    #   · `agent_loop._await_background_subagents`：主代理收工后等子代理结束；
    #   · `agent_loop._run_subagent_tool` 的 collect_results(wait=true)：同上，等子代理跑完；
    #   · `subagent._keepalive_total_timeout`：瞬时故障保活的累计时长上限。
    # 子代理「提问后等主代理/用户答复」**不读这个值**，也没有任何时间上限（用户口径：主代理可能
    # 正在问用户，用户可能很久才回答，应当继续等）。那条等待靠取消传播与会话存活判定兜底，
    # 见 `subagent_comm.wait_reply`。此前两边阈值同为 600s，看门狗必然先杀掉子代理，
    # 是本缺陷的直接成因。
    subagent_wait_timeout_s: float = 1800.0
    # plan-565-2323 G10: 普通子代理的停滞看门狗阈值（秒）——与工作流节点子代理同源同口径
    # （orchestration/workflow/executor_subagent.py 的 STALL_TIMEOUT_SEC = 600）。
    # 子代理超过该时长没有任何步骤/模型调用活动即判失败并停止，避免上游卡住时
    # 子代理永远停在「运行中」（事故：重试耗尽后仍在跑，面板无终止入口）。
    #
    # plan-565-2323 G16：`handle.waiting` 为真的期间不判停滞（只续心跳）——覆盖瞬时故障保活与
    # 「提问后等答复」两种正常等待。等答复既然没有时间上限，这里就绝不能因为「等太久」而杀掉它。
    subagent_stall_timeout_s: float = 600.0
    # v13: 任务规划与拆分
    task_split_confirm: bool = True
    # v2.2 (对齐 zcode 3.9): todo 提醒间隔——模型维护的执行清单连续 N 步未更新时
    # 注入 system 提醒（防"开清单后跑偏"，对齐 ZCode buildTodoReminderBody）
    todo_reminder_interval: int = 3
    # plan-547 C1: 进度提醒间隔——连续 N 步没有面向用户的文字输出时注入 system 提醒
    # （防"长时间静默执行"，用户看不到进展）；0 = 禁用。
    agent_progress_reminder_interval: int = 8
    # plan-19-82 增强（对齐 ZCode runtime-reminders）：语言重申间隔——每 N 个 agent 步
    # 重新注入一次语言锚（ZCode 的 TURNS_BETWEEN_ATTACHMENTS=5 为轮次口径，本项目按
    # 步计）。语言纪律此前只在上下文构建时静态注入一次，长 turn 内锚点随步骤稀释后
    # 不再重申，是确认执行后语言漂移的直接原因。
    #
    # plan-522-2209：默认改为 0（关闭定时重申）。定时重申是「每步都注入一遍完整语言纪律」，
    # 长 turn 下成本随步数线性增长，而真正管用的是按需纠正——agent_loop 每步检测模型实际
    # 输出的语言，一旦与锚定语言不符立即注入（见 _lang_drift_reminders 分支）。保留本开关
    # 供需要更密集约束的场景手动调回。
    language_reminder_interval: int = 0
    # plan-19-82 增强：规则遵循重申间隔——每 N 步重申用户规则优先级，对抗长上下文
    # 下规则段被工具结果挤出注意力范围（对齐 ZCode 对 AGENTS.md 的 OVERRIDE 语义）；
    # 0 = 禁用。默认比语言重申更稀疏（规则比语言更稳定，无需过密）。
    rules_reminder_interval: int = 20
    complexity_direct_max_chars: int = 15
    complexity_llm_timeout: float = 15.0
    task_fail_policy: str = "continue"  # continue | abort
    task_retry_count: int = 1
    plan_mode_auto_split: bool = True
    # plan-644: plan 模式每轮注入的"计划需求全集"字符上限（Plan History，
    # 含各轮用户需求与文档正文）。0 = 禁用注入。
    # plan-1075: 预算超限时先降级已完结轮正文；未完结轮全文永不因预算截断
    # （必要时突破该预算整体注入——未完成内容零丢失优先于 token 预算）。
    plan_history_inject_chars: int = 8000
    # plan-1075: Plan History 单个未完结轮（proposed/confirmed/superseded）文档正文
    # 全文上限；超限从尾部截断并标注可 fs_read 原文档补齐。仅对未完结轮生效。
    plan_history_open_doc_chars: int = 12000

    # 目标模式（对齐 zcode goal-continuation）：
    # - goal_mode_enabled: 总开关，关闭后 turn 完成不再自动续跑
    # - goal_max_continuation_turns: 单目标最大自动续跑轮次（防失控）
    # - goal_continuation_interval_sec: 续跑前间隔（给前端喘息与用户可停窗口）
    goal_mode_enabled: bool = True
    goal_max_continuation_turns: int = 10
    goal_continuation_interval_sec: float = 2.0

    # 后台子代理完成唤醒（对齐 zcode background-task-notifications）：
    # - subagent_wakeup_enabled: 总开关，关闭后后台子代理完成只随下一轮送达
    # - subagent_wakeup_delay_sec: 唤醒前间隔（合并同批完成通知 + 用户可打断窗口）
    subagent_wakeup_enabled: bool = True
    subagent_wakeup_delay_sec: float = 1.5

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
    def agent_retry_interval_list(self) -> list[float]:
        """解析异常重试的间隔序列（秒）：每个元素即一次重试前的等待时长，个数即重试次数。

        空串或全部非法时返回空列表，即不自动重试。
        """
        return parse_retry_intervals(self.agent_retry_intervals)

    @property
    def default_model_ready(self) -> bool:
        return bool(self.default_llm_base_url and self.default_llm_api_key and self.default_llm_model)


RETRY_MAX_COUNT = 20
RETRY_MAX_INTERVAL_SECONDS = 3600


def parse_retry_intervals(raw: str | None) -> list[float]:
    """解析重试间隔标签串（逗号 / 顿号 / 分号 / 空白分隔，兼容中文标点）。

    只保留 0~3600 秒之间的数字，非法项静默跳过，最多保留 20 项。
    设置页已做即时校验，这里是服务端兜底，保证运行时拿到的序列一定合法。
    """
    text = str(raw or "")
    for sep in ("，", "、", "；", ";", "\t", "\n", " ", "　"):
        text = text.replace(sep, ",")
    out: list[float] = []
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            val = float(tok)
        except (TypeError, ValueError):
            continue
        if 0 <= val <= RETRY_MAX_INTERVAL_SECONDS:
            out.append(val)
        if len(out) >= RETRY_MAX_COUNT:
            break
    return out


def format_retry_intervals(values: list[float]) -> str:
    """把间隔序列写回配置串：整数不带小数点（10.0 → 10），与设置页输入口径一致。"""
    return ",".join(f"{v:g}" for v in values)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # plan-549-2281 复盘修复：旧环境变量 APPROVAL_TIMEOUT_SEC_SUBSESSION 随改名被
    # extra="ignore" 静默丢弃——配置文件路径有显式迁移（settings.py），env 路径在这里
    # 兜底：新变量未显式设置时读旧变量，改过 env 的部署升级后行为不回退。
    import os

    if "APPROVAL_TIMEOUT_SEC_CHILD" not in os.environ:
        legacy = os.environ.get("APPROVAL_TIMEOUT_SEC_SUBSESSION")
        if legacy:
            try:
                s.approval_timeout_sec_child = int(legacy)
            except (TypeError, ValueError):
                pass
    return s


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
