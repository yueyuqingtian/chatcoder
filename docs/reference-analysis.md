# 参考项目分析：deepseek-harness 上下文管理机制

> 本文档是「参照 deepseek-harness 优化 chatcoder 上下文管理」的前置调研产物，
> 梳理参考项目的会话持久化、上下文构建、压缩机制与前端消息流设计，
> 并与 chatcoder 现状做差距对照，为 `docs/context-management.md` 方案文档提供依据。

## 1. 参考项目定位

deepseek-harness（`@deepseek-ai/dsh-*`）是一个基于 vendored Cordis 的插件化 Agent Harness：
**一切皆插件**。与本课题相关的三个能力包：

| 包 | 职责 |
|---|---|
| `packages/session/*` | 会话数据层：事件日志持久化、投影（title/telemetry）、写穿批处理 |
| `packages/compaction/*` | 上下文压缩能力缝隙：`compaction`（服务定义）+ `compaction-basic`（实现）+ `compaction-tool-result-pruner`（模型无关修剪）+ `command-compact`（手动命令） |
| `packages/token-meter` | 统一 token 计量（surface 定价、单消息估算），压缩与保留策略共用同一个计量器 |

核心设计原则（写进其 AGENTS.md / 架构文档）：
- **模型可见 ⟺ 已记录**：任何到达模型请求的内容都必须能从会话日志重建；新的模型可见输入必须产生会话事件。
- **压缩是"阴影替换"而非删除**：被压缩的历史不删除，而是被一条带 `compactionId` 的 checkpoint 用户消息遮蔽（shadow）。
- **压缩是事务**：`compaction/start`（持锁）→ `compaction/summary`（记录摘要与阴影定价）→ `user/message`（落 checkpoint）→ `compaction/end`（释放锁），全部落日志。

## 2. 会话持久化（session-persistence）

### 2.1 事件日志（session-persistence-jsonl / sqlite 双实现）

- 会话 = 单调递增 `seq` 的**事件流**，每个事件是声明式记录（`user/message`、`assistant/message`、`tool/call`、`tool/result`、`compaction/start`…）。
- `SessionEventMap` 通过 declaration merging 扩展：新增事件类型只需在包内声明，类型系统强制所有消费方同步。
- 持久化后端：`session-persistence-jsonl`（zstd 压缩、win32 安全写）+ `session-persistence-sqlite`（monotonic `SCHEMA_VERSION`）。
- `SESSION_FORMAT_VERSION` 保持 0，无兼容承诺；只有结构性格式变更才升级版本号。
- **模型可见 ⟺ 已记录** 是硬性不变式：例如 tool 结果进入模型输入前必须先落 `tool/result` 事件。

### 2.2 写穿批处理（write-behind）

`SessionWriteBehind` 是单会话写控制器：

- `enqueue(event)`：结构化克隆进 pending 队列，启动固定 `maxDelayMs` 批处理窗口。
- `flush()`：取消批处理窗口，建立共享 barrier，排空到静止点才 resolve（供压缩等事务的持久化检查点使用）。
- 失败保留：写失败把该批重新塞回队列头，暂停自动路径（`automaticPaused`），后台失败只上报不打断生产者。
- 收益：高频流式事件（token delta）不每帧落盘，但事务性操作（压缩、checkpoint）可显式 `flush()` 保证持久。

### 2.3 投影（projection）

`session-projection` 把事件流投影为可查询的派生状态（会话标题、遥测等），
同一事件源多投影共存，消费方不直接读原始日志。

## 3. 压缩能力缝隙（compaction）

### 3.1 服务定义（`@deepseek-ai/dsh-compaction`）

抽象基类 `CompactionEngine` 定义三个入口：

```ts
abstract compactIfNeeded(agent, trigger, signal): Promise<CompactionResult | null>
abstract compactNow(agent, signal, sourceCommandId?): Promise<CompactionResult | null>
abstract compactRegion(start, end, agent, signal?): Promise<CompactionResult>
```

- `trigger: 'pressure' | 'context-overflow'`：压力（step 边界预检）与溢出（API 确认的 `CONTEXT_WINDOW_EXCEEDED` 错误）两类触发。
- `ManualCompactionError` 分类失败：`busy / cancelled / changed / summary / commit / persistence`——压缩锁、跨度变更、摘要收缩失败、提交失败、持久化失败被精确区分。
- 工具配对边界校验工具：`toolPairingBalancedBefore / toolPairingBalancedAfter`，保证压缩范围不拆散 assistant(tool_calls) 与其 tool 结果。
- 结果类型 `CompactionResult`：`compactionId`、`startSeq/summarySeq/endSeq`、`summary`、`shadowedRange`、`shadowedSeqs`（按 surface 顺序）、`shadowedTokenCount`。

### 3.2 事件协议（compaction/types.ts）

四个日志事件，构成可审计的压缩事务：

| 事件 | 载荷 | 语义 |
|---|---|---|
| `compaction/start` | `compactionId, turn, sourceCommandId?` | 开锁；`turn: null` 表示两次 turn 之间的手动压缩 |
| `compaction/summary` | `compactionId, summary, shadowedRange, shadowedSeqs, shadowedTokenCount, provider, model, maxTokens?, usage?, rawOutput?` | 记录摘要内容、阴影定价与摘要调用的事实（哪个 provider/model 写的、花了多少 token），日志中可重建该次调用 |
| `user/message`（checkpoint） | content = `frameSummary(summary)`，source = `compactCheckpointSource(compactionId)` | 实际替换：该消息遮蔽被压缩范围，作为接手模型的既定背景 |
| `compaction/end` | `compactionId, error?` | 关锁；`error` 记录失败尝试 |
| `compaction/prune` | `shadowedRange, shadowedSeqs, shadowedTokenCount` | 模型无关修剪的阴影价格（tool-result-pruner 用） |

关键约束：**`compaction/summary` 之后必须紧跟替换 `user/message`**——阴影定价字段是替换消息的 shadow price，消费方可直接配对。

### 3.3 region 选择（compaction-basic/region.ts）

`selectCompactableRange(session, measurement, retainTokens)`：

1. 从 surface 尾部向前累加 token，直到达到 `retainTokens`（最近尾部预算，原样保留）。
2. 向前回退到 `toolPairingBalancedBefore` 成立的位置（不拆散工具回合）。
3. 返回 `{ start, end }`（按 surface 位置，非 seq 数值区间）；无可压缩范围返回 null。

`compactSurfaceRegion` 是唯一压缩事务实现：
- 同步校验（跨度合法、无并发锁）→ `compaction/start` → 快照定价 → 异步摘要 → 稳定性复检（`whole-surface` / `selected-span` 两种规则，防异步期间历史被改写）→ `user/message` 替换 → `compaction/end`。
- 失败时也保证恰好一次 `compaction/end`（携带 errorChain），让未匹配的 start 可被检测。

### 3.4 摘要器（compaction-basic/summarizer.ts）

- **KV cache 复用**：摘要请求 = 重放被压缩范围的对话前缀（复用对话自己的 system prompt、tools、leading messages）+ 追加最终 user 指令。这样辅助调用是最后一次请求的真前缀，provider 的 prefix cache 被复用而非失效。
- 结构化 8 段 checkpoint 格式（`COMPACTION_INSTRUCTION`）：

  ```
  ## Primary Request and Intent
  ## Key Technical Concepts
  ## Files and Code
  ## Errors and Fixes
  ## Pending Jobs
  ## Current Work
  ## Next Step
  ## Critical Context
  ```
  规则：保留精确路径/命令/错误串/标识符/数值/签名；不提及压缩本身；已有 `<compacted-summary>` 块时合并而非复制。
- 产出帧：`<compacted-summary>…</compacted-summary>` 包裹，替换消息带 `CHECKPOINT_PREAMBLE`（"这是自动生成的 checkpoint，视为既定背景，继续任务不要复述"）。
- 失败即失败（fail-closed）：无文本产出抛错；`max-tokens` 截断抛 `MAX_TOKENS`；图片输出拒绝。

### 3.5 策略配置（compaction-basic/config.ts）

- 顶层默认 + `modelPolicies` 按 `provider/model` 精确覆盖，全部加载期校验（非法键/重复策略/比例冲突即报错）。
- 关键预算：
  - `thresholdRatio`（默认 0.8）：压力触发阈值 = contextWindow × ratio。
  - `retainRatio`（默认 0.16）或 `retainTokens`（二选一）：原样保留的尾部预算。
  - `maxTokens`（默认 8192）：摘要输出上限。
  - `compactionRetries`（默认 1）、`maxOverflowRetries`（默认 1）：压缩重试与溢出恢复重试次数。
  - `auto`（默认 true）：是否注册自动压缩监听。
- 不变量：`retainTokens < thresholdTokens`，否则配置报错。

### 3.6 自动触发与溢出恢复（compaction-basic/index.ts）

- `agent/pre-step` 瀑布监听：每步边界前调用 `compactIfNeeded('pressure')`，用 `tokenMeter.measure(session)` 的最近一次请求口径计量，超过阈值才压缩；失败只告警不中断 turn。
- `agent/request-error` 监听：`failure.code === CONTEXT_WINDOW_EXCEEDED_CODE` 时走 `compactIfNeeded('context-overflow')`，成功且 surface 有进展则 `{ kind: 'retry' }`；`maxOverflowRetries` 次后放弃。空闲/新 assistant 消息会重置重试计数。
- 压力路径：先做模型无关修剪（prune）重新计量，再选范围压缩；一次压缩后仍超阈值则按 `compactionRetries` 重试。

### 3.7 模型无关修剪（compaction-tool-result-pruner）

- 在压力/溢出压缩前先执行无 LLM 成本的修剪：删除不再被引用的 tool 结果、截断超长结果。
- 每次修剪产出 `compaction/prune` 阴影价格事件，替换消息紧随其后。

### 3.8 手动压缩（command-compact）

- `compactNow` 要求 agent idle，通过 `runMaintenance` 只在空闲时运行，后续唤醒输入排队等待其结算。
- 事务与自动路径共用 `compaction/start…end` 锁；`sourceCommandId` 贯穿结果，供命令结果展示。

## 4. 与 chatcoder 现状的差距对照

| 维度 | deepseek-harness | chatcoder 现状 | 差距 |
|---|---|---|---|
| 会话持久化 | 事件日志（seq 单调、声明式、双后端） | SQLite：messages/turns/sessions 表 + `session.shared_context` JSON | 消息已完整落库（含 thinking/tool_call/tool_result），但压缩产物不落库 |
| 上下文构建 | surface 投影 + 重放 | `context_manager.build_main_context` 分层注入 + token 预算窗口 | 构建已较完整；压缩产物（summary）仅作为 shared_context 文本注入，不产生消息流实体 |
| 压缩触发 | pre-step 压力 + overflow 恢复（带重试） | step 内估算预检 + API 真实 prompt_tokens 后检 + overflow 硬编码摘要 | 有压力/溢出双路径，但溢出无重试计数、无模型无关修剪先行 |
| 压缩范围选择 | token 驱动的 region 选择 + tool 配对边界 | 按"回合数"切分（保留最近 N 回合），非 token 预算 | 回合切分不精确，长回合可能保留过多；无配对边界检查（用 ensure_tool_pairing 兜底） |
| 压缩持久化 | `compaction/start→summary→user/message→end` 事务 + 阴影定价 | `auto_compact` 仅改内存 messages，下次 turn 全量重建 → 每次 turn 重复压缩 | **核心差距**：压缩不落库，摘要无法复用，KV cache 每次失效 |
| 摘要格式 | 8 段结构化 checkpoint + `<compacted-summary>` 帧 + checkpoint preamble | COMPACTION_PROMPT handoff summary（文本流）+ compact-boundary 标记 | 已有 handoff 摘要；缺结构化分段与 checkpoint 语义 |
| KV cache 复用 | 摘要请求重放对话前缀 | 摘要请求只有 system+transcript | 参考项目可省摘要调用成本 |
| 前端展示 | 事件驱动，压缩前后都有日志可投影 | `compact.started/completed` 只置 `isCompacting` 转圈；`MsgType.Summary` 有渲染但无压缩卡片 | 压缩过程无细节（范围/占用/收益），压缩后消息流无折叠痕迹 |
| 计量 | token-meter 统一 surface 定价，压缩/保留共用 | `token_counter` 字节/4 估算 + 校准系数 | 已有估算与校准；缺"阴影定价"概念 |
| 手动压缩 | `compactNow` + 命令 | 无 | 缺 |
| 模型策略 | `modelPolicies` 按 provider/model 定制 | 全局 settings 比例 | 缺按模型定制 |

## 5. 可落地的借鉴点（适配 chatcoder 现有架构）

在不推翻现有分层的前提下，逐项移植：

1. **压缩落库事务**：`auto_compact` 从"内存变换"改为"落库事务"——插入 `MsgType.SUMMARY` 消息（content 带 `compaction_id/shadowed_range/stat`），在 `session.shared_context` 记录 `compacted_ids`（被遮蔽消息 id 集合）+ `compactions` 列表（含阴影定价），并在重建上下文时跳过 compacted 消息、注入对应 SUMMARY 消息。这与现有 `summarized_ids` 机制同构，可合并演进。
2. **region 选择**：把 `auto_compact` 的回合切分改为 token 预算贪心 + tool 配对边界对齐（新 `context_compressor.select_compactable_range`）。
3. **KV cache 复用摘要**：摘要请求改为"重放被压缩消息的 user/assistant/tool 序列 + 末尾追加压缩指令"（复用对话 system 不变式）。
4. **结构化 checkpoint**：COMPACTION_PROMPT 升级为 8 段结构 + `<compacted-summary>` 帧 + checkpoint preamble。
5. **事件协议增强**：`compact.started` 携带 used_tokens/context_window/retain_tokens/shadowed 计划；新增 `compact.summary` 事件（compaction_id/shadowed_range/shadowed_seqs/saved_tokens/summary 预览）；`compact.completed` 携带同样字段。前端据此渲染"压缩中"卡片与"压缩完成"摘要卡。
6. **溢出恢复重试**：context overflow 时走 LLM 摘要压缩 + 限次重试（替代一次性硬编码 emergency_compact）。
7. **前端消息流**：`timeline` 增加压缩边界条目（SUMMARY 消息渲染为可展开/折叠的 checkpoint 卡），被遮蔽消息渲染为灰色折叠占位行；`TurnGroup` 复用现有 `summary` 分支扩展卡片样式。

> 完整方案（目标架构、数据模型、接口、前端状态机、迁移步骤、验证方式）见 [docs/context-management.md](context-management.md)。
