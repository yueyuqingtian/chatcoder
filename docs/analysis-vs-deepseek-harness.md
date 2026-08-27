# chatcoder 与 deepseek-harness 核心机制对比分析报告

> 版本：v1.0
> 日期：2026-08-27
> 范围：上下文构建、持久化、压缩、回复（agent 循环）、计算（token 估算/计量）五个维度
> 对比对象：
> - chatcoder（Python FastAPI + SQLAlchemy + Electron/React），本仓库
> - deepseek-harness（TypeScript monorepo + cordis 插件体系），`D:\aitools\deepseekharness\deepseek-harness`

---

## 0. 总体结论（TL;DR）

两个项目在解决同一问题（长会话上下文管理与 Agent 循环）上走了两条截然不同的架构路线：

| 维度 | chatcoder | deepseek-harness |
|---|---|---|
| 架构范式 | **状态存储型**（关系型 DB，消息行 + JSON 列） | **事件溯源型**（append-only 事件日志 + surface 投影） |
| 上下文构建 | 函数式拼装（分层 developer 片段 + 三层可见性 + 窗口选取） | 注册式组装（system-prompt 注册表 + scope + waterfall 拦截） |
| 持久化 | SQLite/PostgreSQL 行存，软阴影（压缩保留原文可还原） | JSONL(zstd)/SQLite 事件日志，surface 硬替换（日志可审计） |
| 压缩 | 内存规范化 + 落库式压缩（已对齐 harness 的 region/checkpoint/阴影定价） | 抽象 CompactionEngine + 事务化压缩（锁事件 + 稳定性检查 + 两阶段提交） |
| 回复 | 单体 agent_loop（1600+ 行过程式循环） | 事件驱动 agent-loop（cordis 中间件 + llm/stream waterfall） |
| 计算 | 字节/4 启发式估算 + 校准系数补偿 | token-meter 统一测量（估算 + API usage 投影 + O(1) 增量 fold） |

**关键事实**：chatcoder 的 `docs/context-management.md`（v30）本身就是**参照 deepseek-harness 的 compaction 能力缝隙**设计的落库式压缩实现，因此压缩维度两者高度同构；差异集中在底层架构（事件溯源 vs 行存储）与压缩事务的严谨程度（锁/稳定性/回放）上。

---

## 1. 上下文构建

### 1.1 chatcoder：三层可见性 + 分层 developer 片段

**入口**：[context.py](/D:/aitools/chatcoder/chatcoder/server/app/orchestration/context.py:352)（子代理/任务路径）、[context_manager.py](/D:/aitools/chatcoder/chatcoder/server/app/orchestration/context_manager.py:203)（主代理路径）。

三层上下文可见性（任务驱动）：

1. **L1 全局摘要**：`session.shared_context.summary`（由 context_memory 后台渐进摘要维护），无摘要时取最近 20 条主群消息拼接（`_layer1_global_summary`）。
2. **L2 任务相关**：任务自身 + thread 滑动窗口历史（token 预算贪心选取，`select_messages_by_token_budget`）+ 父任务产物摘要交接（`_parent_artifacts_brief`，含文件清单显式交接）。
3. **L3 RAG 检索**：从知识库检索相关文档注入（`rag.py`）。

主代理上下文（`build_main_context`）按"注意力递减"顺序拼装 developer 片段：

```
1. Current Goal         2. Working Directory & Tool Rules（含 shell 环境提示）
3. Project Structure    4. Project Rules（多文件规则文档，8000 字符上限）
4.1 Global Rules        5. Session Memory（最近 8 个 turn 摘要 + 记忆条目）
6. 主会话 LLM 摘要      7. Conversation Checkpoints（压缩块，仅注入一次）
8. 历史消息窗口（token 预算）9. Skills/MCP 摘要
```

特点：
- **多代理群聊体系**：团队进度同步（`_team_progress_brief` 列出全部任务状态/负责人/交付物）、上游产物交接、Agent learned_facts 经验传承。
- **多模型感知**：每个 Agent 绑定模型的 `context_window` 驱动所有窗口预算（`get_agent_context_window`）。
- **并行 IO**：无依赖查询用独立 DB session 并行执行（v1.0 降低启动延迟）。

### 1.2 deepseek-harness：注册式组装 + 事件投影

**入口**：system-prompt 注册表（`packages/core/system-prompt`）、agent-instructions（`packages/context/agent-instructions`）、session surface 投影（`packages/core/session/surface.ts`）。

- **system-prompt 注册表**：各插件向 `ctx.systemPrompt.section()` 注册有序段（`order` 字段，如 -100 为 harness 身份、0 为 persona、100-199 为工具指引），组装时按序拼接 + `{{variable}}` 插值 + `system-prompt/assemble` waterfall 可拦截改写。动态上下文（PromptContext）分语义形式：`instructions` / `catalog` / `snapshot` / `notice` / `relay` / `recall`，由生产者声明并持久化。
- **agent-instructions**：工作区指令（AGENTS.md 等）在**显式字节预算**内渲染（UTF-8 截断保护码点），变更增量渲染（文件变更只重发变化部分，带 digest 校验），`<system-reminder>` 帧封装。
- **surface 投影**：模型可见消息不是"存的"，而是从事件日志**投影**出来的——只有 `user/message`、`assistant/message`、`tool/result` 三类事件进入 surface（`deriveEventMessage`），投影规则统一、可外部重建。

### 1.3 差异小结

| 维度 | chatcoder | deepseek-harness |
|---|---|---|
| 组装方式 | 函数式硬编码顺序（python 代码内拼接） | 注册式（插件声明 order + scope 隔离 + waterfall 扩展） |
| 系统提示 | 单一大字符串 + developer 段 | 结构化 sections + 变量插值 + 语义化动态上下文 |
| 规则文档 | 手工加载 + 自动探测，字符上限截断 | 字节预算 + 变更增量 + digest，且状态持久化可 reconcile |
| 历史注入 | DB 行 + token 预算窗口贪心选取 | 事件日志 → surface 投影（天然与持久化同源） |
| 多代理 | 群聊 + 任务编排 + 团队进度同步 | 子代理（subagent descriptor/投影），无群聊协作概念 |

---

## 2. 持久化

### 2.1 chatcoder：关系型行存储

- **模型**：`sessions` / `messages` / `turns` / `tasks` / `artifacts` 等 20+ 表（[message.py](/D:/aitools/chatcoder/chatcoder/server/app/persistence/models/message.py:8)）。消息以 JSON 列存 `content`（text/tool_call/tool_result 按 `msg_type` 区分）。
- **摘要/压缩状态**：`sessions.shared_context` JSON 列（`summaries` / `summarized_ids` / `summary` / `compacted_ids` / `compactions` / `injected_compactions`）。
- **并发控制**：乐观锁——写入前 `db.refresh(session)` 重读，检测到已被其他进程摘要则跳过（[context_memory.py](/D:/aitools/chatcoder/chatcoder/server/app/orchestration/context_memory.py:318)）。
- **软删除/回滚**：`messages.deleted` 标记，回滚服务（rollback_service）支持撤销工具修改。
- **软阴影**：压缩不删消息，`compacted_ids` 遮蔽 + 可还原（`restore_compaction`）。
- 后端：SQLite（默认）/ PostgreSQL（docker-compose）。

### 2.2 deepseek-harness：事件溯源

- **事件日志为唯一真相源**：所有会话事件（`user/message`、`assistant/message`、`tool/result`、`turn/start`、`request/header`、`compaction/start|summary|end`、`compaction/prune`……）追加写入。
- **JSONL 后端**（`session-persistence-jsonl`）：每会话一个 append-only 文件，**zstd 帧压缩**（带校验和、torn-tail 恢复、win32 持久化发布语义）、连续 `assistant/chunk` 打包为 packed rows（实测日志缩小 ~60%）。
- **SQLite 后端**（`session-persistence-sqlite`）可选。
- **PersistenceCoordinator**（`session-persistence/coordinator.ts`）：write-behind 批量写、事件合并窗口、revision 标识（dev:ino:size:mtime 快照）防写穿。
- **surface 与日志分离**：日志记录一切（含被替换的原始事件），surface 是模型可见投影；`isAppendSurfaceEvent` 区分"人类 transcript"（append）与"模型视图"（replace 后）。被压缩的旧事件**物理仍在日志**，可审计可回放，但模型不再看到。
- **可重构性**：任意时刻的模型请求 = `request/header`（envelope）+ 日志前缀的 surface 投影，纯函数可重建（reconstructability 设计原则）。

### 2.3 差异小结

| 维度 | chatcoder | deepseek-harness |
|---|---|---|
| 范式 | 状态存储（行 + JSON 列） | 事件溯源（append-only 日志 + 投影） |
| 写入 | 每条消息一条 INSERT + WS 广播 | 事件 append（批量 coalescing + zstd 压缩） |
| 查询能力 | SQL 强（按内容/时间/线程查询） | 需扫描日志或自建投影缓存 |
| 审计/回放 | 无事件级回放（消息即最终态） | 天然支持（日志可重放重建任意视图） |
| 断线补偿 | WS 消息双写 + refreshMessages 兜底 | 事件回放 + 投影缓存（session-projection） |
| 体积 | JSON 行膨胀（无压缩） | zstd 帧 + packed rows（~60% 缩小） |
| 压缩后原文 | 软阴影保留 + REST 还原接口 | 日志保留（可审计），surface 硬替换 |

---

## 3. 压缩

两个项目压缩机制高度同源（chatcoder v30 明确对齐 harness 设计），以下逐点对比。

### 3.1 内存级规范化（每轮请求前）

**chatcoder**（[compaction.py](/D:/aitools/chatcoder/chatcoder/server/app/orchestration/compaction.py:128)）：
- `repair_tool_call_ids`：重分配重复/空 tool_call id（Gemini 严格网关 400 根因自愈）。
- `ensure_tool_pairing`：补缺失 tool result（标注"结果丢失"而非注入假数据）、删孤立 result。
- `normalize_tool_sequence`：强制 assistant(tool_calls) 后紧跟 tool 消息。
- `build_api_copy`：基于原始历史构造 API 副本（不改库）；预算内**默认不折叠**（防模型重读），超预算才折叠较早 tool result。
- `_micro_compact`：单条超大工具结果 → sha256 占位符 + 落盘 `.compact-cache`，提示可用 fs_read 恢复。

**deepseek-harness**：没有独立的"每轮内存变换"层——因为 surface 投影天然保证消息序列合法（事件模型约束），配对维护发生在投影与压缩边界（`toolPairingBalancedBefore/After`）。

### 3.2 落库式/持久化压缩

**chatcoder**（[context_compressor.py](/D:/aitools/chatcoder/chatcoder/server/app/orchestration/context_compressor.py:275) `compact_session`）：
1. 候选 = 未摘要、未压缩的 text/tool_call/tool_result。
2. `select_compactable_range`：尾部向前累计 token 至 `retain_tokens`（默认 `0.16 × context_window`）→ 向后找**配对平衡切点**（`_is_pairing_balanced`：压缩区内 call 闭合、保留区无孤立 result）。
3. 收益检查：可回收 > `max(2000, 0.05 × window)`。
4. LLM 结构化摘要：**KV 缓存友好重放**（tool_call → assistant(tool_calls)，tool_result → tool 消息）+ `COMPACTION_PROMPT`（8 段 checkpoint）+ `<compacted-summary>` 帧 + `CHECKPOINT_PREAMBLE`；失败降级硬编码摘要。
5. 落库：插入 SUMMARY 消息（带 `compaction_id/index/shadowed_ids/shadowed_tokens/saved_tokens`）+ 更新 `shared_context.compacted_ids/compactions`（乐观锁防并发覆盖）。
6. WS 事件协议：`compact.started → compact.summary（阴影定价）→ compact.completed`。
7. **软阴影**：原消息保留，构建上下文时排除；支持 `restore_compaction` 还原 + `compaction_index/view` AI 工具按需取原文（信息无损）。
8. 触发：step 前置估算（90% 阈值，校准系数）、API 真实 usage（90%）、溢出恢复（context-overflow，保留最近 6 回合，限次重试）。
9. 渐进摘要并行：`maybe_summarize_main_session`（0.35 × ctx 阈值）+ 超级摘要（旧摘要合并，防长期记忆丢失）。

**deepseek-harness**（`packages/compaction`）：
- **抽象服务** `CompactionEngine`：`compactIfNeeded(agent, trigger, signal)` / `compactNow`（手动）/ `compactRegion(start, end, agent)`；`compaction-basic` 为默认实现。
- **触发**：`agent/pre-step` 中间件（pressure）、`agent/request-error`（仅 `CONTEXT_WINDOW_EXCEEDED_CODE`，限次重试，成功响应重置序列）。
- **region 选择**（region.ts `selectCompactableRange`）：尾部 retain 预算 + `toolPairingBalancedBefore` 边界（向前找平衡切点，方向与 chatcoder 相反但等价）；token-meter surface 与会话 surface 强一致校验。
- **事务化执行**（`compactSurfaceRegion`）：
  - `compaction/start` 事件 = **持久化锁**（直到 `compaction/end`），owner 归属当前 turn（自动）或 null（手动事务）；
  - 异步摘要期间**稳定性检查**（`assertWholeSurfaceUnchanged` / `assertSelectedSpanStable`，SurfaceChangedError 区分于摘要失败）；
  - 两阶段：commit 阶段 append `compaction/summary`（含阴影定价：shadowedRange/shadowedSeqs/shadowedTokenCount + provider/model/usage）+ 紧随的 `user/message` 替换（**契约：替换必须紧邻定价事件**）；
  - 失败分类：`busy / cancelled / changed / summary / commit / persistence`（ManualCompactionError 六类）。
- **摘要**（summarizer.ts）：9 段 COMPACTION_INSTRUCTION（比 chatcoder 多 "Errors and Fixes" 拆分语义，并显式要求合并旧 checkpoint）+ 指令作为**最后一条 user 消息**追加在重放对话之后——真正的"请求前缀复用"，系统提示/工具 schemas 原样带上。
- **模型无关修剪**：`compaction-tool-result-pruner`（删无引用结果、截断超长结果，零 LLM 成本，先于 LLM 摘要）。
- **模型级策略**（config.ts `modelPolicies`）：按 provider/model 定制 thresholdRatio / retainRatio / maxTokens / 重试次数。
- **手动压缩**：`/compact` 命令（compactNow，idle 会话 + 事务锁）。

### 3.3 压缩差异小结

| 维度 | chatcoder | deepseek-harness |
|---|---|---|
| 范围选择 | 候选列表索引 + 配对平衡 | surface 位置 span + 配对平衡 + meter 强一致校验 |
| 事务性 | 乐观锁（refresh 合并） | 显式持久化锁事件 + 稳定性检查 + 两阶段提交 |
| 失败分类 | 无显式分类（成功/失败回退内存压缩） | 6 类错误码（busy/changed/commit/…） |
| 触发钩子 | agent_loop 内嵌（估算/真实/溢出三处） | 事件中间件（pre-step / request-error）可叠加 |
| 摘要前缀 | 重放消息序列（无系统提示/tools） | 完整重放（system + tools + 消息）+ 指令作尾条，KV cache 最大化复用 |
| checkpoint 结构 | 8 段 | 9 段 + 显式合并旧 checkpoint 规则 |
| 压缩后原文 | 软阴影保留 + REST 还原 + AI 工具按需取回 | surface 硬替换（日志可审计，无用户还原入口） |
| 模型无关修剪 | 无（roadmap 项） | 有（tool-result-pruner） |
| 手动压缩 | 无（roadmap 项） | 有（compactNow + 事务锁） |
| 回放 | 无 | 事件日志天然支持 |

---

## 4. 回复（Agent 循环）

### 4.1 chatcoder：单体过程式循环

[agent_loop.py](/D:/aitools/chatcoder/chatcoder/server/app/orchestration/agent_loop.py:362) `run_agent_loop`（1600+ 行，单文件）：

1. **step 循环**：每步先做上下文规范化（repair → pair → normalize → build_api_copy）。
2. **前置压缩检查**：估算（校准系数）≥ 90% 阈值 → `_compact_persistent_or_fallback`（落库优先，失败回退内存 auto_compact/emergency_compact）。
3. **流式调用与广播**：`_stream_chat_and_broadcast` 实时转发 `thinking.delta` / `token.delta`（WS），流式内联 `<thinking>` 实时剥离。
4. **响应健康检查**：空响应/超时中断/输出截断必须对用户可见（`_response_failure_reason`）；fatal 空响应按**思考档位降档重试**（high→low→off，配置化次数）。
5. **错误分类自愈**：function call 400（重复 id/配对问题）→ 仅重分配 id 重试；context overflow → 紧急压缩后重试；超时分类（ta3 ReadTimeout / chunk timeout）。
6. **注入式干预**：todo 提醒（N 步未更新）、重复工具调用提醒、空转预警（DiminishingReturnsDetector：3 步无工具 + 相似内容判定）。
7. **落库与事件**：每条消息 `create_message` 落库 + `message.created` 广播；turn 结束生成摘要 + artifact。
8. 支持子代理（task/thread 维度的消息分桶）、取消（cancel_event 流式中断）。

### 4.2 deepseek-harness：事件驱动 + 中间件

- **agent-loop**（`packages/core/agent-loop`）：通过 cordis 事件驱动，生命周期事件（`agent/pre-step`、`agent/post-step`、`agent/status`、`agent/request-error`、`session/event`）以 **waterfall/serial 中间件**形式被各插件订阅——压缩、重试、路由、审计都是"插件"，可叠加组合。
- **llm/stream waterfall**（`packages/llm/llm`）：每次模型调用经过可拦截的流（retry、replay、routing 插件）；请求以 `markAgentLoopRequest` 标记并 **deep-freeze**（不可变，内容纯函数自会话日志）。
- **BlockAssembler**：流式块组装为 ContentBlock；finish_reason/usage 结构化。
- **错误分类**：LlmError 统一 code（AUTH / RATE_LIMIT / NO_ADAPTER / CONTEXT_WINDOW_EXCEEDED 等），适配器失败归一化（adapter-failure.ts）。
- **request/header 事件**：每次请求的完整 envelope（system/tools/config）写入日志——"哪个模型、什么提示、多少 token"全程可重构。
- **子代理**：descriptor（身份/模式声明）+ 投影（时长/身份统计）+ in-process/out-of-process 驱动 + ACP/Claude Code/Codex 适配器，跑在独立 session 上。
- **计划（plan）**：plan 包与 plan-mode 提供规划-执行分离模式（chatcoder 也有 permission_mode=plan 对应）。

### 4.3 差异小结

| 维度 | chatcoder | deepseek-harness |
|---|---|---|
| 结构 | 单文件过程式（隐式顺序） | 事件中间件（插件声明式、可叠加） |
| 可扩展性 | 改 agent_loop 源码 | 订阅事件/注册插件（业务解耦） |
| 请求可重构性 | 无（重建需重新查询 DB 组装） | 请求 = header 事件 + surface 投影（纯函数） |
| 流式 | WS 双广播（thinking/content） | 事件日志 + 投影（前端订阅增量） |
| 重试 | 思考降档重试 + 400 自愈 + 超时分类 | retry 插件（waterfall 拦截）+ 结构化错误码 |
| 空转检测 | DiminishingReturnsDetector（预警/停止） | 无显式等价物（依赖模型行为） |
| 干预注入 | todo 提醒/重复调用提醒（硬编码点） | 事件钩子（任何插件可注入） |

---

## 5. 计算（token 估算/计量）

### 5.1 chatcoder：启发式估算 + 校准

[token_counter.py](/D:/aitools/chatcoder/chatcoder/server/app/orchestration/token_counter.py:28)：

- `_approx_token_count`：UTF-8 字节 / 4（对齐 codex `APPROX_BYTES_PER_TOKEN`，中英文混合天然适配）。
- 消息结构开销：每消息 +4、tool_call +8、tool_result +8。
- 预算分配：有效输入 95%、压缩阈值 90%、压缩后目标 45%（对齐 codex）。
- 窗口预算：主窗口 0.80×ctx、摘要触发 0.35×ctx、摘要批量 0.35×ctx、thread 0.85×ctx（比例集中到 settings）。
- **per-agent 窗口**：模型级 `context_window` 解析（`get_agent_context_window`），500K/1M/200K 各自管理。
- 用量分类：`estimate_breakdown` 7 类（system/tools/history/tool_results/thinking/input）。
- **校准系数**：`_calib_factor` 修正估算与 API 真实 prompt_tokens 的系统性偏差（估算驱动压缩触发，真实值驱动展示/后续判断）。

### 5.2 deepseek-harness：token-meter 统一计量

`packages/llm/token-meter`：

- **统一估算器**（estimate.ts）：chars/4 + 结构开销，ContentBlock 递归计价；system/tools 从 request header 计价。
- **TokenMeter 服务**：`measure()` 给出 surface 节点级 token（per-node 价格），供压缩选区和定价共享同一把尺子——**估算、压缩阴影定价、前端占用显示三者数字必然一致**。
- **O(1) 增量投影**（surface-projection fold）：每个事件到达后 O(1) 更新总占用，无需全量重算；`contextBreakdown` 投影（system/tools/messages 三类）同样 O(1)。
- **阴影价格衔接**：`compaction/prune` / `compaction/summary` 事件声明"被替换范围的启发式价格"，替换消息落地后投影按声明递减——压缩后占用数字自动正确。
- **usage-projection**：API 真实 usage 也投影进日志，估算与真实两条口径并存、可对照。

### 5.3 差异小结

| 维度 | chatcoder | deepseek-harness |
|---|---|---|
| 方法 | 字节/4 估算 + 校准系数 | 固定密度估算 + API usage 双口径 |
| 粒度 | 消息级（按类型加结构开销） | 内容块级递归（text/reasoning/tool-call/tool-result） |
| 增量性 | 无（每次全量 sum） | O(1) fold 增量投影 |
| 一致性 | 估算/压缩/展示各自计算（可能偏差） | 单一估算器 + 阴影价格协议（必然一致） |
| 窗口策略 | per-agent 模型窗口 + 比例预算 | 无显式窗口（surface 全长计量 + 压缩控制） |

---

## 6. 优缺点分析

### 6.1 chatcoder 的优点

1. **持久化完整且可操作**：关系型 DB 提供 SQL 查询、回滚、审计；压缩为**软阴影**——原文物理保留，支持一键还原（restore_compaction），deepseek-harness 的硬替换没有用户级还原能力。
2. **信息无损压缩闭环**：`compaction_index` / `compaction_view` 工具让 AI 可按需取回压缩前原文，不依赖摘要记忆；checkpoint 只注入一次（`injected_compactions`），压缩块可审计可展开。
3. **多代理/多任务场景成熟**：群聊 + 任务编排 + 团队进度同步 + 上游产物交接 + learned_facts 经验传承，远超 harness 的子代理体系。
4. **错误恢复务实**：空响应思考降档重试、function call 400 自愈、超时分类、空转检测——全部针对真实网关问题打磨，工程经验密集。
5. **部署轻**：Python + SQLite 单进程即可跑，无 monorepo/构建链负担。
6. **压缩机制已对齐参考实现**：region 选择、配对平衡、checkpoint 帧、阴影定价、事件协议均已落地，且测试覆盖完整（test_context_compressor / test_compression_service）。

### 6.2 chatcoder 的缺点

1. **无事件溯源**：消息落库即最终态，无法回放"某次请求当时发了什么"；KV cache 前缀稳定性差——每次 turn 全量重建，任何新摘要/checkpoint 注入都会改变前缀，缓存命中率不如 harness 的 surface 增量模型。
2. **压缩事务不严谨**：乐观锁只能防"同时摘要同一批消息"，无法防并发压缩交叉、无稳定性检查（摘要期间会话变化不检测）、无两阶段提交；压缩失败只有"回退内存式"一种处理，无失败分类与重试语义。
3. **token 计量多口径**：估算（校准系数）与 API 真实值分离，压缩阴影定价用估算、展示可能用真实，三者可能不一致；无 O(1) 增量，超大会话每次重建全量 sum 成本高。
4. **单文件 agent_loop 膨胀**：1600+ 行过程式代码，扩展（新压缩策略/新干预）必须改源码，无插件机制。
5. **消息行存储膨胀**：无压缩、无批量写入优化；超大会话（数万消息）查询/重建慢。
6. **KV 缓存复用不彻底**：摘要重放已做，但未重放系统提示与工具 schemas（与 harness 的完整前缀复用有差距）；无工具结果修剪器（model-free prune）。
7. **无手动压缩命令、无模型级压缩策略**（均在 roadmap）。

### 6.3 deepseek-harness 的优点

1. **事件溯源架构**：append-only 日志 = 唯一真相源；任意时刻模型输入可纯函数重建（reconstructability）；断线补偿、回放、审计、多端（CLI/Web）共享同一事件流。
2. **压缩事务工程化**：持久化锁事件、稳定性检查、两阶段提交、6 类失败分类、限次重试、模型无关修剪、手动命令——完整且严谨。
3. **KV cache 最大化复用**：摘要请求完整重放（system + tools + 消息前缀）+ 指令作尾条；请求 header 规范化保证前缀稳定。
4. **统一计量**：token-meter 单一口径 + O(1) 投影 + 阴影价格协议，估算/压缩/展示必然一致。
5. **插件化扩展**：cordis 中间件体系让压缩、重试、路由、审计全部可插拔叠加，系统提示注册表 + scope 支持多租户/多预设。
6. **持久化体积优化**：zstd 帧压缩 + packed chunks（~60% 缩小），torn-tail 恢复等文件系统级健壮性处理。
7. **子代理生态**：in-process / out-of-process / ACP / Claude Code / Codex 多驱动。

### 6.4 deepseek-harness 的缺点

1. **复杂度极高**：事件溯源 + 投影 + scope + cordis 插件体系，学习曲线陡峭，单点问题排查需要理解整个事件流；类型体操密集（fused dispatch、merge-extensible union）。
2. **查询能力弱**：JSONL 日志做内容搜索/跨会话统计需自建扫描或投影缓存，不如关系型 SQL 直接。
3. **压缩不可还原**：surface 硬替换后模型视图立即丢失原文（虽然日志可审计），无用户级"还原压缩块"交互，也无 AI 按需取回原文的工具（chatcoder 的 compaction_view 是更实用的设计）。
4. **无群聊/多任务协作**：subagent 是严格父子层级，无任务编排、团队进度同步、RAG 知识库注入。
5. **无内置空转检测/响应健康检查**：对"模型反复空转、空响应、输出截断"等真实问题的防御依赖插件生态，默认实现不如 chatcoder 的 DiminishingReturnsDetector + 降档重试直接。
6. **部署重**：Node monorepo（pnpm workspace + cordis + sdk-runtime），环境要求高。
7. **"完美前缀"依赖 provider**：KV cache 复用收益取决于网关是否真做 prefix cache，且重放整个 system+tools 在工具集庞大时本身有成本。

---

## 7. 借鉴建议（chatcoder 可吸收的点）

按性价比排序：

1. **压缩事务加固**（低成本高收益）：给 `compact_session` 增加显式事务锁标记（如 `shared_context.compaction_lock` + 超时）、摘要前后对比 `compacted_ids` 的稳定性检查、失败分类返回（busy/changed/summary），替代纯乐观锁。
2. **统一 token 计量口径**（中成本）：把估算/压缩阴影定价/前端展示收敛到同一函数族（现状估算与真实 usage 已分离，可加"以真实 usage 回填校准"的投影机制）。
3. **工具结果修剪器**（低成本，roadmap 已有）：实现 model-free pruner（删无引用结果、截断超长结果），在 LLM 摘要前先做一轮无成本修剪。
4. **模型级压缩策略**（中成本，roadmap 已有）：`modelPolicies` 按 provider/model 定制 thresholdRatio / retainRatio / maxTokens。
5. **请求前缀日志**（高成本）：记录每次请求的 header（system/tools/model/usage）到独立表，实现"请求可重构"与 KV 缓存前缀统计。
6. **手动压缩命令**（低成本）：`/compact` 命令映射到 `compact_session`（idle 时执行 + 锁）。

---

## 8. 附：关键文件索引

| 维度 | chatcoder | deepseek-harness |
|---|---|---|
| 上下文构建 | `server/app/orchestration/context.py`、`context_manager.py`、`context_memory.py`、`rag.py` | `packages/core/system-prompt/src/index.ts`、`packages/context/agent-instructions/src/render.ts`、`packages/core/session/src/surface.ts` |
| 持久化 | `server/app/persistence/models/message.py`、`turn.py`、`database.py` | `packages/session/session-persistence-jsonl/src/index.ts`、`session-persistence/src/coordinator.ts` |
| 压缩 | `context_compressor.py`、`compaction.py`、`services/compression_service.py`、`tools/compaction_view.py` | `packages/compaction/compaction/src/index.ts`、`compaction-basic/src/region.ts|summarizer.ts|config.ts`、`compaction-tool-result-pruner/src/index.ts` |
| 回复 | `orchestration/agent_loop.py` | `packages/core/agent-loop/src/agent.ts`、`packages/llm/llm/src/index.ts` |
| 计算 | `orchestration/token_counter.py` | `packages/llm/token-meter/src/estimate.ts`、`surface-projection.ts`、`breakdown-projection.ts` |

> 注：chatcoder 侧实现细节可同时参考 `docs/context-management.md`（v30 落库式压缩设计）、`docs/reference-analysis.md`（早期对照分析）。
