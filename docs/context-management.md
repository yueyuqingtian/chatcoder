# chatcoder 上下文管理优化方案

> 版本：v30
> 状态：已实现（本文档描述的实现均已落地并通过单测/类型检查/构建验证）
> 参考：`docs/reference-analysis.md`（deepseek-harness 上下文管理机制分析）

## 1. 背景与目标

chatcoder 的 Agent 循环面临长会话失忆与上下文超限问题。现有机制已具备基础能力
（分层上下文构建、turn 摘要、内存级 auto_compact、token 预算窗口），但与参考项目
deepseek-harness 相比存在核心差距：

| 问题 | 现状 | 后果 |
|---|---|---|
| 压缩不持久化 | `compaction.auto_compact` 只改内存 messages | 下轮 turn 全量重建历史 → 每次 turn 重复压缩、摘要不可复用、KV cache 每次失效 |
| 压缩无阴影定价 | `compact.started/completed` 仅带占用字段 | 前端无法展示"压缩了什么、省了多少 token" |
| 压缩后消息流无痕迹 | DB 无 SUMMARY 消息 | 刷新/重开后看不到压缩发生点，压缩过程不可审计 |
| 压缩范围不精确 | 按回合数切分（保留最近 N 回合） | 长回合可能保留过多/过少，与 token 预算脱节 |

本方案参照 deepseek-harness 的 compaction 能力缝隙，为 chatcoder 实现一套
**落库式上下文压缩 + 完整事件协议 + 前端状态机**：

1. **上下文持久化**：压缩产物落库（SUMMARY checkpoint 消息 + `shared_context.compacted_ids`），跨轮/重启可复用，下轮重建自动跳过被压缩消息。
2. **完整上下文构建**：构建时注入全部压缩 checkpoint（仅一次）+ 排除被压缩消息，信息不丢失。
3. **思考块/工具调用持久化**：保持现有 thinking/tool_call/tool_result 独立消息落库；压缩摘要时思考块不重复计入。
4. **完善的压缩机制**：token 预算驱动的 region 选择 + tool 配对边界对齐 + 结构化 8 段 checkpoint + 溢出恢复限次重试。
5. **压缩时前端状态**：`compact.started` → 消息流尾部"压缩中"进度卡（占用/比例）。
6. **压缩后前端展示**：`compact.summary` → 压缩完成卡片（遮蔽条数/节省 token/可展开 checkpoint 正文）；被压缩消息折叠隐藏。

## 2. 目标架构

```
┌────────────────────────── Agent 循环（agent_loop.py）──────────────────────────┐
│                                                                                 │
│  前置估算压缩        API 真实占用压缩          溢出恢复（context overflow）       │
│  （step 请求前）      （response usage 后）      （API 报错分支）                 │
│        │                    │                          │                        │
│        └──────► _compact_persistent_or_fallback ◄──────┘                        │
│                          │          │                                           │
│                  优先落库式压缩      失败回退旧内存式                              │
│                  context_compressor  compaction.auto_compact/                   │
│                  compact_session /   emergency_compact                           │
│                  emergency_compact_session                                      │
└──────────────────────────┬───────────────────┬──────────────────────────────────┘
                           │                   │
           落库写入（DB）                    WS 广播（事件协议）
                           │                   │
┌──────────────────────────▼───────────────────▼──────────────────────────────────┐
│  shared_context:          messages 表:          compact.started（占用信息）      │
│  ├ compacted_ids[]        ├ SUMMARY checkpoint  compact.summary（阴影定价）     │
│  ├ compactions[]          └ 原消息保留（软阴影）  compact.completed（收尾+刷新）  │
│  └ injected_compactions                                                            │
└──────────────────────────────────────────────────────────────────────────────────┘
                           │
                    下轮上下文重建（context_manager.build_main_context）
                           │
                   ├ 跳过 compacted_ids 消息
                   ├ 注入 compactions → checkpoint 摘要（仅一次，写回 injected_compactions）
                   └ 历史窗口只取未压缩消息 → token 预算窗口
                           │
                   ┌───────▼────────┐
                   │  前端消息流      │
                   │  CompactingCard │  ← compact.started（压缩中）
                   │  CompactCard    │  ← SUMMARY checkpoint（压缩完成，可展开）
                   │  被遮蔽消息隐藏   │  ← collectShadowedIds
                   └────────────────┘
```

## 3. 数据模型

### 3.1 `messages` 表（既有表，新增一种用途）

压缩产物以 `MsgType.SUMMARY` 消息落库，`content` 结构：

```json
{
  "text": "<CHECKPOINT_PREAMBLE>\n\n<compacted-summary>\n…8 段结构化摘要…\n</compacted-summary>",
  "compaction_id": "cp-1a2b3c4d5e6f",
  "index": 1,
  "checkpoint": true,
  "trigger": "pressure | context-overflow",
  "shadowed_ids": [12, 13, 14, 15],
  "shadowed_tokens": 3200,
  "saved_tokens": 2900,
  "summary_tokens": 300
}
```

字段语义（对齐 deepseek-harness `CompactionResult` 阴影定价）：
- `index`：压缩块索引（会话内序号，从 1 起）——AI 按索引查看压缩前会话的定位键。
- `shadowed_ids`：被压缩遮蔽的消息 id 集合（按时间序）。
- `shadowed_tokens`：遮蔽内容的估算 token 总量（压缩前占用）。
- `saved_tokens`：节省 token = shadowed_tokens − summary 估算 token。
- `checkpoint: true`：前端据此渲染压缩块卡片（区别于普通 summary）。
- `restored: true`：该压缩块已被还原（被压缩消息重新参与上下文构建）。

### 3.2 `sessions.shared_context`（既有 JSON 列，扩展 3 个键）

```json
{
  "summarized_ids": [2, 5, 8],
  "summaries": [],
  "summary": "…",
  "compacted_ids": [12, 13, 14, 15],
  "compactions": [
    {
      "compaction_id": "cp-1a2b3c4d5e6f",
      "index": 1,
      "summary_message_id": 42,
      "shadowed_ids": [12, 13, 14, 15],
      "shadowed_tokens": 3200,
      "saved_tokens": 2900,
      "trigger": "pressure",
      "created_at": "2026-08-26T18:00:00"
    }
  ],
  "injected_compactions": ["cp-1a2b3c4d5e6f"]
}
```

- `compacted_ids`：已压缩遮蔽的消息 id（构建上下文时排除）。
- `compactions`：压缩记录（阴影定价审计清单 + AI 索引，按发生顺序追加）。
- `injected_compactions`：已注入过当前模型的 checkpoint 集合（防跨轮重复注入浪费 token）。

> 与既有 `summarized_ids`（context_memory 后台渐进摘要）同构且互补：
> 渐进摘要是"消息→文本摘要"（不产生消息流实体），落库式压缩是"消息→checkpoint 消息"（产生消息流实体 + 遮蔽标记）。两者互相排除，不重复压缩。

## 4. 上下文构建（context_manager.py）

`build_main_context` 新增两处逻辑：

1. **checkpoint 注入**（在 Session Summary 注入之后）：读取 `compactions`，
   对每个未注入过的压缩记录，取 `summary_message_id` 对应的 SUMMARY 消息正文，
   合并注入为 `## Conversation Checkpoints (compacted spans)` developer 片段；
   注入后把 `compaction_id` 写入 `injected_compactions` 并 flush（仅注入一次）。
2. **历史窗口过滤**：`_fetch_main_messages` 拉全量后，同时排除
   `summarized_ids` 与 `compacted_ids`，再走 token 预算贪心选取。

效果：被压缩的历史不再进入窗口（窗口让位给新内容），其信息由 checkpoint 承载；
checkpoint 只注入一次，后续轮次不再重复占用。

## 5. 压缩流水线（context_compressor.py）

### 5.1 触发时机（agent_loop.py 接入）

统一入口 `_compact_persistent_or_fallback(db, *, session_id, provider, agent_window,
used_tokens, agent_id, agent_name, turn_id, messages, trigger)`：

| 触发点 | 位置 | trigger | 说明 |
|---|---|---|---|
| 前置估算压缩 | step 请求前（估算 token ≥ 阈值 90%） | `pressure` | 用校准后估算值 |
| 真实占用压缩 | API 响应后（usage.prompt_tokens ≥ 90%） | `pressure` | 用 API 真实值 |
| 溢出恢复 | API 报 context overflow 错误 | `context-overflow` | 保留最近 6 回合 |

成功路径：落库 + 广播 `compact.summary` + 注入 checkpoint 到当前内存 messages
（本轮剩余 step 立即可见）；失败路径：回退旧内存式 `auto_compact` /
`emergency_compact`（保持向后兼容）。

### 5.2 region 选择（select_compactable_range）

参照 deepseek-harness `region.ts`：

1. 候选 = 主线程未摘要、未压缩的 text/tool_call/tool_result 消息。
2. 从尾部向前累计 token 至 `retain_tokens`（默认 `context_window × 0.16`）得初始切点。
3. 从切点向后找第一个**配对平衡**位置（`_is_pairing_balanced`）：
   - 压缩区 `[0, k-1]` 内每个 tool_call 都必须在压缩区内配对闭合；
   - 保留区 `[k:]` 内每个 tool_result 的配对 call 也必须在保留区内且在其前。
4. 收益检查：可回收 token > `max(2000, context_window × 0.05)` 才压缩
   （避免无价值 LLM 调用）。

### 5.3 摘要生成

- **KV 缓存友好路径**：把待压缩消息结构化为 user/assistant/tool 重放序列
  （tool_call → assistant(tool_calls)，tool_result → tool 消息），
  以 `COMPACTION_PROMPT` 为 system 前缀 + 末尾追加指令。比纯文本 transcript
  保留更多工具调用结构信息，摘要质量更高。
- **结构化 8 段 checkpoint**（`COMPACTION_PROMPT` 升级版，参照 deepseek-harness）：

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
  要求保留精确路径/命令/错误串/标识符/数值；已有 `<compacted-summary>` 帧时合并而非复制。
- **降级路径**：LLM 失败/无 provider 时用硬编码摘要（工具统计 + 关键内容截取）。
- 产出帧：`CHECKPOINT_PREAMBLE + <compacted-summary>…</compacted-summary>`。

### 5.4 落库事务（阴影替换）

1. 插入 SUMMARY 消息（见 §3.1，`create_message` 广播 `message.created`）。
2. 乐观更新 `shared_context`：`compacted_ids ∪= shadowed_ids`，
   `compactions.append(记录)`（重读 session 防并发覆盖）。
3. 广播 `compact.summary`（阴影定价）+ `compact.completed`。

> 语义：**软阴影**——被压缩消息保留在 DB（可审计、可回滚），构建上下文时排除；
> checkpoint 消息作为时间线上的压缩标记。与 deepseek-harness 的
> `user/message` 替换语义等价（模型可见内容一致），差异仅在原始消息的物理保留。

### 5.5 溢出恢复（emergency_compact_session）

- 不按 16% 保留预算，强制保留最近 6 个工具回合（对齐旧 emergency 语义）。
- 仍走 LLM 摘要 + 落库 + 广播；`agent_loop` 重试该次请求（沿用现有 retry 机制）。
- 相比旧硬编码 `emergency_compact`：摘要质量更高、产物持久化、可审计。

## 6. 压缩块索引、原文还原与 AI 接口（v30.1）

### 6.1 压缩块索引

每次压缩产出一个压缩块，携带会话内递增的 `index`（从 1 起）：
- SUMMARY 消息 `content.index`；
- `shared_context.compactions[i].index`；
- WS 事件 `compact.summary/completed` 载荷 `index`。

索引是 AI 定位压缩前会话的键：`compaction_index` 返回全部压缩块（序号/遮蔽范围/
节省 token/摘要预览），`compaction_view` 按 `index` 或 `compaction_id` 取原文。

### 6.2 原文还原（软阴影 → 还原）

被压缩消息物理保留在 messages 表，`restore_compaction` 让其重新参与上下文构建：

1. 从 `shared_context.compacted_ids` 移除 shadowed_ids；
2. 从 `compactions` 移除该压缩块记录；
3. 从 `injected_compactions` 移除该块（还原后 checkpoint 不再注入）；
4. SUMMARY 消息标记 `restored=True`（卡片显示"已还原"，前端隐藏还原按钮）。

### 6.3 REST 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/sessions/{id}/compactions` | 压缩块索引列表（CompactionIndexOut） |
| GET | `/sessions/{id}/compactions/{cid}/messages` | 被压缩消息完整原文（MessageOut[]） |
| POST | `/sessions/{id}/compactions/{cid}/restore` | 还原压缩块（返回 restored_messages） |

### 6.4 AI 工具（注册于 tool_registry）

| 工具 | 参数 | 说明 |
|---|---|---|
| `compaction_index` | 无 | 列出会话内全部压缩块索引；需要回忆早期会话先调用它 |
| `compaction_view` | `index` 或 `compaction_id` | 按索引取压缩前原始消息（完整原文，含工具调用/结果） |

两者均 low risk 免审批（纯读）。AI 在压缩后的长会话中可按需调用，
不必依赖 checkpoint 摘要的记忆，信息无损。

## 7. 前端状态机

### 7.1 压缩中（CompactingCard）

- 触发：`compact.started`（store 置 `isCompacting` + `compactingInfo`）。
- 展示：消息流尾部（streamingNode 之后）进度卡：
  `正在压缩上下文… 占用 N tokens（X%）/ 窗口`。
- 结束：`compact.completed` 清状态；`ComposerCore` 现有"正在压缩上下文…"徽标保留。

### 7.2 压缩块卡片（CompactCard，工具调用统计风格）

- 数据源：落库的 SUMMARY checkpoint 消息（`message.created` 实时加入 /
  `compact.completed` 后 `refreshMessages` 兜底）。
- 时间线：**被压缩消息不隐藏、不折叠**——仍按消息 id 时间序正常展示；
  SUMMARY 压缩块消息按其 id 自然落在被压缩消息之后，时间线排序一致。
- 渲染（`TurnGroup` summary 分支 → `CompactCard`），对齐 ToolTree 工具调用行风格：
  - 折叠态一行：`[压缩图标] 上下文压缩 #序号 │ N 条消息 · 节省 X tokens │ [压力/溢出] ▸`；
  - 展开态：压缩索引元信息（遮蔽 ID 范围/原占用/`compaction_view` 提示）+
    checkpoint 8 段摘要（Markdown）+ **压缩前消息原文列表**（展开时按需从
    `GET /sessions/{id}/compactions/{cid}/messages` 拉取，每条含角色与内容）+
    「还原被压缩消息」按钮（调用 restore 接口后刷新消息流）。
- 已还原的压缩块：标题旁标"已还原"，隐藏还原按钮，保留原文可查看。

### 7.3 数据流

```
compact.started ──► store.isCompacting=true ──► MessageFlow trailingNode=CompactingCard
compact.summary ──► store.lastCompact（含 index）
message.created(SUMMARY checkpoint) ──► store.messages ──► buildTimeline ──► CompactCard（工具调用风格）
compact.completed ──► isCompacting=false + refreshMessages
展开压缩块 ──► GET /sessions/{id}/compactions/{cid}/messages ──► 原文列表
还原压缩块 ──► POST /sessions/{id}/compactions/{cid}/restore ──► refreshMessages
AI 按需 ──► compaction_index / compaction_view 工具
```

## 8. 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 压缩产物落库 vs 仅内存 | 落库 | 跨轮复用、可审计、KV cache 收益、重启不丢 |
| 本轮生效 vs 下轮生效 | 本轮注入 checkpoint + 下轮重建生效 | 内存 messages 无 id 无法精确替换被压缩消息；注入 checkpoint 保证本轮模型知情，安全不丢消息 |
| 被压缩消息展示 | 保留在时间线（不隐藏），压缩块卡片收纳索引/摘要/原文 | 用户可随时回看原文；压缩块是折叠入口与还原出口，满足"可还原"与"工具调用风格展示" |
| 软阴影 vs 硬删除 | 软阴影（保留原消息，构建时排除；可还原） | 可回滚、可审计；`restore_compaction` 一键恢复上下文参与 |
| AI 按索引查看 | `compaction_index` + `compaction_view` 工具 | 压缩不丢信息：AI 可精确拉取压缩前原始消息，而非依赖摘要记忆 |
| 与 context_memory 关系 | 并存互补 | 渐进摘要（后台兜底） + 落库压缩（按需精确） |
| 压缩范围 | token 预算 + 配对平衡 | 对齐 deepseek-harness region 选择，不拆散工具回合 |
| 溢出恢复 | LLM 摘要 + 限次重试 | 替代一次性硬编码，摘要质量与持久性双提升 |

## 9. 配置项

沿用既有 settings（无需新增配置，`default_context_window` 决定所有比例）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `default_context_window` | 1000000 | 窗口基准（模型级 `context_window` 优先） |
| `auto_compact_threshold_ratio` | 0.90 | 压力压缩触发阈值（×窗口） |
| `auto_compact_keep_rounds` | 6 | 溢出恢复保留最近回合数 |
| `auto_compact_min_reclaim_tokens` | 2000 | 最小回收收益（落库压缩取 max(2000, 窗口×5%)） |
| `context_main_window_ratio` | 0.30 | 主窗口预算（×窗口） |

落库式压缩的保留预算硬编码为 `窗口 × 16%`（对齐 deepseek-harness 默认
`retainRatio=0.16`），后续可提升为 settings 配置。

## 10. 兼容性与迁移

- **向后兼容**：`_compact_persistent_or_fallback` 失败时回退旧 `auto_compact` /
  `emergency_compact`，旧会话无 `compacted_ids` 时行为与 v29 一致。
- **旧数据**：已存在会话的 `shared_context` 无新键 → 走原逻辑；新键只会在
  首次压缩后出现。
- **事件兼容**：`compact.started/completed` 载荷扩展为可选字段，旧前端忽略新字段；
  新前端对旧载荷（无阴影定价）降级为纯转圈展示。
- **消息类型**：SUMMARY 消息类型既有，timeline/TurnGroup 已有 `summary` 分支，
  仅新增 `checkpoint` 标记判定，无协议破坏。

## 11. 验证方案

### 11.1 单测（已实现）

`server/tests/test_context_compressor.py`：
- `_is_pairing_balanced`：完整链/空链平衡；切在 call/result 之间不平衡。
- `select_compactable_range`：span 尾部是平衡切点；保留区 token ≥ 预算；
  空链/预算过大返回 None。
- `_build_transcript`：包含工具名、文件路径、用户文本。

`server/tests/test_compression_service.py`（v30.1）：
- `list_compaction_index`：空会话返回 []；带压缩块时返回索引（含摘要预览剥帧）；
- `get_compacted_messages`：按 compaction_id 取原文；未知 id 抛 KeyError；
- `restore_compaction`：还原后 compacted_ids/compactions/injected 移除，
  SUMMARY 标记 restored；未知 id 抛 KeyError；
- AI 工具：`_message_to_text` 各消息类型可读；`compaction_index/compaction_view`
  schema 参数合法。

### 11.2 回归（已跑通）

- `server`：`test_context_summary_persistence`、`test_inline_thinking`、
  `test_context_compressor`、`test_compression_service` 全绿；
  所有改动文件 `py_compile` 通过。
- `client`：`tsc --noEmit` 通过；`vite build` 成功。

### 11.3 手动验收路径

1. 长会话跑至占用 ≥ 90% → 观察 `compact.started` 后消息流尾部出现
   "正在压缩上下文…"卡片（带占用/比例）。
2. 压缩完成 → 消息流出现工具调用风格的 `上下文压缩 #1 │ N 条消息 · 节省 X tokens`
   卡片（被压缩消息仍按时间线保留展示）；展开可见 8 段 checkpoint +
   压缩前消息原文列表。
3. 点击「还原被压缩消息」→ 卡片标记"已还原"，下轮上下文重建重新包含这些消息
   （日志 compacted=N 减少）。
4. 给 AI 一条指令"查看会话第 1 个压缩块压缩前的对话" → AI 依次调用
   `compaction_index`（拿到索引）与 `compaction_view`（拿到原文）。
5. 重启服务 → 新 turn 上下文构建日志显示 `compacted=N`，checkpoint 只注入一次
   （`injected_compactions` 记录）；压缩块卡片/还原入口从 DB 重建仍可用。

## 12. 后续演进（Roadmap）

1. **模型级策略**：`modelPolicies` 按 provider/model 定制 thresholdRatio /
   retainRatio / maxTokens（对齐 deepseek-harness config.ts，当前硬编码 16% 保留）。
2. **手动压缩命令**：`/compact` 命令（`compactNow` 语义，idle 会话 + 事务锁），
   供用户主动压缩长会话。
3. **事件日志化**：把压缩事务（start/summary/end）纳入 seq 单调事件流，
   断线补偿可回放（当前用 WS 广播 + DB 消息双写）。
4. **模型无关修剪**：压缩前先执行 tool-result-pruner（删除无引用结果、
   截断超长结果），无 LLM 成本。
5. **KV cache 复用**：摘要请求复用对话前缀（需 provider 支持 prefix cache 时
   才有效，当前 DeepSeek 网关按需评估）。

## 13. 文件清单

| 文件 | 改动 |
|---|---|
| `server/app/orchestration/context_compressor.py` | 新增：region 选择、落库式压缩事务、溢出恢复、阴影定价、压缩块索引（index） |
| `server/app/services/compression_service.py` | 新增：压缩块索引查询、原文还原、restore 事务 |
| `server/app/orchestration/tools/compaction_view.py` | 新增：compaction_index / compaction_view AI 工具 |
| `server/app/orchestration/tools/registry.py` | 注册压缩查看工具 |
| `server/app/orchestration/context_manager.py` | checkpoint 注入（仅一次）+ compacted_ids 过滤 |
| `server/app/orchestration/context_memory.py` | 渐进摘要排除 compacted_ids；JSON 列新 dict 赋值 |
| `server/app/orchestration/agent_loop.py` | 三处压缩点接入落库式压缩（含回退）；compact.summary 带 index |
| `server/app/orchestration/prompts/summary.py` | COMPACTION_PROMPT 8 段结构化 + CHECKPOINT_PREAMBLE + 帧标签 |
| `server/app/gateway/schemas.py` | EvCompactEvent 扩展 + compact.summary + CompactionIndexOut |
| `server/app/gateway/routers/sessions.py` | compactions 索引/原文/还原 3 个 REST 端点 |
| `server/tests/test_context_compressor.py` | 新增单测 |
| `server/tests/test_compression_service.py` | 新增：索引/还原/AI 工具单测 |
| `packages/shared/src/events.ts` | CompactSummaryPayload（含 index）+ compact.summary 登记 |
| `packages/shared/src/index.ts` | CompactionIndexOut 类型 |
| `client/src/api/client.ts` | listCompactions / getCompactedMessages / restoreCompaction |
| `client/src/store/chat.ts` | compactingInfo/lastCompact 状态 + 事件处理 |
| `client/src/components/chat/CompactCard.tsx` | 压缩块卡片（工具调用统计风格 + 原文还原）+ 压缩中卡片 |
| `client/src/components/chat/timeline.ts` | v30.1：撤销压缩消息隐藏（保留时间线展示） |
| `client/src/components/chat/TurnGroup.tsx` | summary 分支 → CompactCard |
| `client/src/components/chat/MessageFlow.tsx` | trailingNode 压缩中卡片（移除 shadowed 过滤） |
| `client/src/components/icons.tsx` | IconCompress |
| `client/src/styles/global.css` | 压缩卡片样式（含原文列表/还原按钮） |
| `docs/reference-analysis.md` | 参考项目分析 |
