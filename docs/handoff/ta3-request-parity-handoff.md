# 交接：ta3 供应商请求构造对齐官方客户端（plan-598-2482）

- 初版：2026-10-28；分支：`master`（工作区改动，尚未提交）
- 计划文档：`ai/chatcoder-plan-598-2482.md`（`ai/` 被 `.gitignore` 忽略，仅本机有）。本文自带全部要点，不依赖计划文档阅读。
- 官方参考源码：`tmp_ta3_ref/dist/main/`（由 `tmp_ta3_ref/extract_all.cjs` 从官方 `app.asar` 解出，`.gitignore` 的 `/tmp_*/` 已覆盖，不入库。换机后需重新解包）。
- 目标：使用 ta3 供应商时，chatcoder 发往牛码平台的请求构造与官方客户端 **Ta+3 牛码 0.7.5** 保持 90% 以上一致性。

## 一、为什么做这件事

平台 LLM 通道按请求特征做风控。此前被判「非牛码体系工具使用」并冻结账号，排查发现 chatcoder 的 ta3 伪装层与官方客户端在**四个维度存在系统性偏差**，其中三项偏差方向是"比官方更显眼"：

| 维度 | 官方 | 改造前 chatcoder |
|---|---|---|
| MCP 工具命名 | `mcp__{server}__{tool}` + 描述 `[MCP: 名]` | `McpXxx` + 中文模板描述（方向相反） |
| 工具面数量 | 31 个（agent 模式实测）+ MCP | 72 个（过滤只作用于提示词口径，实际 schema 全量下发） |
| 系统提示词 | 单条 system，段结构固定（20 段） | 多条 system（规则走 developer 通道）+ 大量自造段（38 段） |
| 请求体细节 | Anthropic 三处 `cache_control`、kimi 发 `adaptive` | 无 `cache_control`、kimi 只发 `output_config` |

## 二、已完成（代码已在仓库中，未提交）

### 1. 请求头与请求体（`server/app/models/providers/ta3.py`）

- **去 `User-Agent`**：官方 LLM 请求头无该键（`modelClient.js:186-232` 两个 `build*Headers` 均未设置），此前硬编码 `Chrome/126 + Electron/31` 是稳定版本指纹。新增开关 `ta3_send_user_agent`（默认 `False`）。
- **Anthropic `system` 改为单元素块数组 + `cache_control`**：对齐 `anthropicAdapter.js:289-291`。
- **messages 末块 `cache_control`**：官方 `contentToBlocks()` 把字符串 content 包成 `[{type:'text',text}]` 后对末块打点；此前 `_mark_message_tail` 只处理 content 已是 list 的情况，纯字符串被静默跳过。已在 `_mark_message_tail` 内做字符串→text block 归一。
- **tools 末项 `cache_control`**：对齐 `anthropicAdapter.js:336-339`。
- **`max_tokens` 2048 兜底**：对齐 `anthropicAdapter.js:299`（新增 `_DEFAULT_MAX_TOKENS`）。
- **thinking 分支重写**：与官方 `anthropicAdapter.js:302-335` 同口径——目录 `thinkingEnabled !== true` 不发；档位 `none` 发 `{type:'disabled'}` 并保留 temperature；否则 `anthropic-enabled/manual` 走 `{type:'enabled',budget_tokens}`，其余走 `{type:'adaptive'}` + `output_config:{effort}` 并**删除** temperature/top_p。删除原 kimi 特判（官方没有 kimi 分支）。
- **`_thinking_enabled` 口径**：改为只看目录 `completionOptions.thinkingEnabled === true`，不再要求 `request.thinking` 为真（官方从不看该标志）。
- **`_apply_thinking_openai` 修正**：目录开启但档位为 `none` 时发 `disabled`（此前会误发 `enabled`）；qwen 系仍用 `enable_thinking` 布尔。
- **DeepSeek 路径补 `stream_options`**：官方 `deepSeekAdapter.js:35-42` 强制 `{include_usage:true}`，此前只在 `zai` 下发。新增 `_DEEPSEEK_IDENT`。
- **OpenAI/DeepSeek 路径恒发 `temperature`**：官方 `openAiAdapter.buildRequestBody` 的 `temperature: toNumber(..., 0.1)` 无条件写入、thinking 分支只追加字段从不删温度；此前 `request_temperature` 在 thinking 开启时返回 `None` 导致丢字段，现在该路径补默认温度（**只有 Anthropic 面才在思考开启时删温度**）。
- **出站脱敏改为内置拦截**：`ta3_sanitize_outbound` 默认改 `True`；另加不依赖开关的兜底——请求体确实含竞品签名（`you are claude code` 类）且被 400/403 拒绝时，自动强制脱敏并**重发一次**（`_force_sanitize_text/_force_sanitize_structure/_has_risk_signature`）。

### 2. 工具面收敛 + 拓展走 MCP 通道

- **新建 `server/app/models/providers/ta3_official_tools.py`**：`TA3_OFFICIAL_BUILTIN_TOOLS` 15 项官方内置名白名单（`Bash/Diff/Edit/List/Read/ReadSkill/Search/SubAgent/TodoWrite/WebSearch/Write/ask_user/generate_project_memory/get_file_outline/get_project_memory`），只收「本项目真实存在且官方也有」的工具。
- **`ta3_tool_aliases.py` 重写**：`TO_TA3`（15 条，值必须在白名单内，文件末尾有运行时自检会直接 raise）、`TO_TA3_EXTENSION`（59 条，映射到 `mcp__workspace-tools__{真实名}`）、`FROM_TA3`（内置 + 拓展 + `FROM_TA3_EXTRA` 共 78 条反查）。**删除 `DYNAMIC_SCHEMA_ALIASES`**。
- **`ta3_mcp.py` 重写**：命名改官方形态 `mcp__{server}__{tool}`（原 `McpXxx`）；描述改 `[MCP: {server}] {原文}`（原中文模板），并清洗描述里的产品名（`chatcoder` → `workspace`）；新增 `EXTENSION_SERVER_ID = "workspace-tools"`、`build_extension_tool()`、`ta3_mcp_name()`、`split_ta3_mcp_name()`。
- **`ta3_tool_schemas.py` 重写**：`TA3_NATIVE_SCHEMAS` 只保留官方 15 项并逐项对齐参数（`Read` 600/2000、`Search` 补 `offset`/`max_matches`、`WebSearch` enum 改 `bing/google/duckduckgo`、`TodoWrite` 补 `owner`、`SubAgent` 逐字对齐官方含 `inherit_context`）；`disguise_tools()` 三路分派：官方内置→快照 / 本项目独有→拓展 MCP / 真 MCP→`mcp__{server}__{tool}`，其余剔除。
- **`engine.py`**：ta3 工具面过滤下移到**最终 `tool_schemas` 下发点**（原来只过滤 `_available_tools`，实际 schema 全量下发）。
- **`output_paging.py`**：`tool_ref()` 同步接入 `TO_TA3_EXTENSION`，否则续读提示会引导模型调用一个模型侧不存在的工具名。

### 3. 提示词对齐官方

- **`ta3_fusion.py`**：`build_ta3_system_prompt` 只保留「平台下发主体 + `## 任务列表`」两段；**删除** `## 工具使用纪律`、`<runtime-context>`、`## 子代理使用`、流程规范段（`CHATCODER_ADDENDUM*`）、语言首尾双锚。三段 ADDENDUM 常量仍保留（被既有单测引用），但 ta3 不再调用。
- **`context_manager.py`**：ta3 下（`ta3_meta is not None`）跳过 `## Current Goal`、`## Reply Language`、`## 派发子代理`、`## 操控子代理`、legacy 提示、`## Rule Documents`/`## Global Rules`/`## Project Rules` 三段、`Working directory`+`## Tool Usage Rules`、`## Project Structure`、`## Available MCP Servers`、`## Available Plugins`、`## Code Symbol Index`、Shell 环境、规则锚点。
- **ta3 改用官方记忆注入**（新增 `_build_ta3_memory_injection`）：`<memory_instructions>` 说明段（逐字对齐 `memory/promptBuilder.js:12-39`）+ `<system-reminder>` 里的「## 全局记忆索引 / ## 项目记忆索引」，**只列索引不注入正文**（标题+文件名+一句描述），索引按官方规则 200 行 / 25000 字节截断并加 WARNING，>1 天条目附陈旧提醒。全局层取 `user/feedback`，项目层取 `project/reference`（本会话 session 层并入项目段，官方无该概念）。
- **ta3 改用官方 `<project-context>`**（新增 `_build_ta3_project_context`）：**只**注入工作区 `AGENTS.md` + 用户级 `~/.claude/CLAUDE.md`，32KB 上限，`join('\n')`（对齐 `systemPromptAssembly.js:198-223`）；不再注入 CLAUDE.md（工作区）/ `.cursorrules` / `CODEBUDDY.md` / `.codebuddy/rules` 等 chatcoder 自有规则源。

### 4. 其他

- **`agent_loop.py`**：`spawn_subagent` 解析 `inherit_context` 并透传；`context_manager.build_subagent_context` 新增该参数，`true` 时用 `_load_completed_turns_snapshot()` 加载**已完成轮次**快照（不含当前轮，创建时快照语义对齐官方）。
- **`core/config.py`**：新增 `ta3_send_user_agent=False`、`ta3_sanitize_outbound=True`。

## 三、双模型协议差异（重要，易踩坑）

官方 `providers.js:122-128` 的 `builtInProviders` 顺序是 `[deepSeekProvider, anthropicProvider, openAiProvider]`——**DeepSeek 优先级高于 Anthropic**。

| 模型 | provider | 实际协议 | 端点 |
|---|---|---|---|
| `kimi-k3` | kimi | Anthropic Messages | `…/ai/dispatch/v2/anthropic/v1/messages` |
| `deepseek-flash` | deepseek | **DeepSeek / OpenAI 兼容** | `…/ai/dispatch/v2/chat/completions` |

`deepseek-flash` 走 OpenAI 面：`messages` 数组内含 system、`tools[].function`、`tool_choice: "auto"`、`stream_options: {include_usage:true}`、`temperature: 0.1`、`thinking: {type:'enabled'}`。
`kimi-k3` 走 Anthropic 面：`system` 块数组带 `cache_control`、`tools[].input_schema`、`thinking: {type:'adaptive'}` + `output_config:{effort}`、无 `temperature`。

chatcoder 侧的协议判定在 `server/app/auth/ta3/catalog.py::_is_anthropic`（provider∈{anthropic,kimi} 或 identity 含 anthropic/claude/kimi 或 apiBase 含 `/anthropic`），判定本身正确；**做 harness 时不要强制 `_anthropic = True`**，否则会把 deepseek-flash 错当成 Anthropic 协议。

## 四、验证记录

- **ta3 相关测试 196 项全绿**；非 ta3 相关（prompt/provider/context/memory）**284 项全绿**。
- **字段级一致性实测**（两边都跑真实代码：官方用 app.asar 解包源码 + 真实平台下发目录驱动官方插件；chatcoder 走 `build_main_context` + `Ta3Provider`）：

| 字段 | kimi-k3 官方/我方 | deepseek-flash 官方/我方 |
|---|---|---|
| `model` / `stream` | ✓ / ✓ | ✓ / ✓ |
| `temperature` | 未下发 / 未下发 | 0.1 / 0.1 |
| `max_tokens` | 32768 / 32768 | 65536 / 65536 |
| `thinking` | adaptive / adaptive | enabled / enabled |
| `output_config` | effort=high / 同 | 未下发 / 未下发 |
| `tool_choice` | 未下发 / 未下发 | auto / auto |
| `stream_options` | 未下发 / 未下发 | include_usage / 同 |
| body 顶层字段数 | 8 / 8 | 10 / 10 |
| system 段标题集合 | 20 段 = 20 段（双向差集为空） | 同 |

- **记忆注入格式**：用内存库写入 6 条样本（3 全局 + 3 项目）验证，输出格式与官方一致（`<memory_instructions>` + `<system-reminder>` 下「## 全局记忆索引」「## 项目记忆索引」两层）。

**未验证**：
- 没有抓真实请求包，上述是代码层形态一致性，**不能证明服务端一定放行**。账号解封后需实测一次。
- `inherit_context=true` 的快照加载依赖 `Turn.status == "completed"` 的数据，需真实委派一次确认。
- 浏览器 / 桌面 / 工作流等拓展工具经 MCP 通道后的实际调用链（模型按 `mcp__workspace-tools__xxx` 调用 → 入站还原 → 执行）未在真实会话走通。

## 五、剩余差异（均非字段构造问题）

| 项 | 现状 | 原因 |
|---|---|---|
| 请求头 7 vs 8 | chatcoder 多 `x-ws-id` | 官方在传入 `wsFp` 时也会发；harness 未给官方传工作区指纹 |
| tools 31 vs 72 | 共同 15 个官方名 | chatcoder 多 57 个是拓展 MCP 通道；官方少 16 个（`EditDocument`/`EditWordDocument`/`RevertFile`/`get_goal`/`create_goal`/`update_goal`/`job_*`/`list_agents`/`send_message`/`interrupt_agent`/`read_file_range`/`recall_history`/`single_find_and_replace`/`submit_understanding`）是 chatcoder 无对应实现 |
| system 字符数 17617 vs 10084 | `<project-context>` 相当（5280 vs 5209） | 差异在记忆段：官方 13797 字符（本机几十条真实记忆），harness 只写 6 条样本（1035 字符）——数据量差异，格式已一致 |

## 六、未完成清单（建议顺序）

1. **实测解封后的真实请求**：以阶段「请求头 + MCP 命名」相关改动为主，先跑一次确认不再 403，再评估是否需要进一步对齐。
2. **补 16 个官方有、chatcoder 没有的工具**（如 `read_file_range`/`recall_history`/`get_goal` 系列/`job_*` 系列）——需要真实实现才能进白名单，否则只是幽灵映射（本轮已刻意剔除这类条目）。
3. **`tools[].cache_control` 只打在末项**：官方 `markTailWithCacheControl` 同样只打末项（`anthropicAdapter.js:261-271`），两边一致；但官方 `tools` 数量少，实测官方那 31 项的末项已带标记，无需额外改动。若后续官方改为多点打标需再比对。
4. **验证拓展 MCP 通道的完整调用链**（见 §四 未验证第 3 条）。
5. **`CHANGELOG.md` 更新**（按 `AGENTS.md` §3 规范，归入 `package.json` 当前版本区块，不擅自升版本号）。

## 七、环境与注意

- **不要结束承载当前会话的 Electron 进程 / 后端，不要重置数据库**（`AGENTS.md` §6 红线）。
- **官方源码解包**：`tmp_ta3_ref/` 是本轮临时解包目录，`.gitignore` 已覆盖。重新生成用 `node tmp_ta3_ref/extract_all.cjs`（保留 dist + 必要 node_modules，跳过 renderer 与二进制）。**若目录已被清理，两个对照 harness 会失效**，需先解包。
- **对照 harness 位置**（均不入库，`.gitignore` 的 `/tmp_*.{py,cjs,mjs,txt}` 覆盖）：
  - `tmp_ta3_ref/harness_official.mjs` —— 官方侧。用真实平台下发目录（`%APPDATA%\Ta+3 牛码\auth-session.json` 的 catalog）驱动官方 `ta3DefaultPersonasPlugin` + 24 个内置工具插件，再调官方 `resolveBuiltInProvider` / `buildBaseMessages` / `getChatTools`。**关键**：需要给 cordis `Service` 提供 `ctx.reflect.provide` / `ctx.effect` 桩，并 `setActivePluginRuntimeAccess` 注入 `listTools/listExternalTools/resolvePersona` 等。
  - `tmp_cmp_chatcoder.py` —— chatcoder 侧。走 `build_main_context` + `Ta3Provider`，**必须钳住 `context_manager._resolve_ta3_model_meta`** 返回真实 meta（否则 ta3 分支不生效，会跑出普通主提示词）。记忆样本写内存库，不碰真实库。
  - `tmp_gen_full_request.py` —— 生成两边全量入参 txt（按模型真实协议渲染 Anthropic / OpenAI 两种形态）。
  - `tmp_verify_memory.py` —— 单独验证记忆注入与 `<project-context>` 格式。
- **对照产物**（供人工逐段比对）：`tmp_ta3_full_request_{kimi-k3,deepseek-flash}_{官方,chatcoder}.txt`、`tmp_ta3_full_request_对比汇总.txt`、`tmp_cmp_report.md`。
- **测试基线**：`python -m pytest server/tests -k ta3 -q` 应得 196 passed；`-k "prompt or provider or context or memory"` 应得 284 passed。
- **本轮改过 5 个陈旧断言**（都是与新行为冲突的旧契约，非放松验证）：`test_ta3_stream_handling.py` 的 kimi effort `medium→low` 特判、kimi 关闭不发 thinking、未指定档位不发 thinking；`test_reply_language.py` 的 ta3 语言双锚+段序；`test_output_paging.py` 的拓展工具名。另有 `test_goal_mode.py` 与 `test_ta3_{aliases,mcp,sanitization,schema_parity}.py` 随工具伪装层重构同步改写，并新增 `test_ta3_official_parity.py`（16 项，守护白名单/拓展通道/四路分派/无 UA）。
- `server/app/gateway/schemas.py` 与 `server/tests/test_memory_entry_out.py` 的改动**早于本轮**（`MemoryEntryOut.session_id` 允许 None），不属于本轮范围，勿误认为是 ta3 改动。
