# Ta+3 牛码（ta3-new-coder）模型供应商接入方案

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-08-24 |
| 参考项目 | `D:\aiTools\ta+3\ta3-new-coder-desktop`（Electron + TypeScript，产品名 "Ta+3 牛码"，银海出品） |
| 当前项目 | `D:\myProject\chatcoder`（Electron + React 前端 / Python FastAPI 后端） |

---

## 0. 摘要

把参考项目（下称 **ta3**）登录后的自带模型，作为一个新的模型供应商类型 **`ta3`** 接入当前项目的供应商体系。接入后：

- 用户在当前项目内完成 ta3 账号登录（复刻 ta3 的 PKCE(SM3) 浏览器登录 / 银海通 IM 静默登录）；
- 自动拉取 ta3 下发的模型目录（含 per-model 的 `apiBase`、`llm-` 前缀 API Key、远端系统提示词），同步为当前项目 Provider/Model 记录；
- 聊天请求由新增的 `Ta3Provider` 发出，**严格复刻 ta3 的请求头、请求体、流式解析**，模拟 ta3 运行环境防止风控；
- **工具名伪装**：发给模型前把当前项目的工具 schema 替换为 ta3 的同名工具定义（`Read`/`List`/`Search`/`Write`/`Edit`/`Bash`/`TodoWrite`...），模型回传 tool_calls 时再映射回当前项目真实工具执行；
- **系统提示词还原**：以 ta3 远端下发的 `baseAgentSystemMessage` + ta3 本地提示词段落（任务列表/工具纪律/runtime-context）为主体，追加当前项目的流程规范限制；
- **上下文管理、压缩、系统规则、编排仍完全沿用当前项目**（`agent_loop` / `context_manager` / `compaction` 不动）。

---

## 1. 参考项目链路逆向分析（事实清单）

> 以下均来自 `D:\aiTools\ta+3\ta3-new-coder-desktop\resources\app-extracted\src`（已解包源码），行号为当前文件实际行号。

### 1.1 服务端点（唯一事实源）

`src/config/serverEndpoints.ts:12-16`：

| 常量 | 值 |
|---|---|
| `HARD_DEFAULT_LC_BASE_URL` | `https://lc.yinhaiyun.com` |
| `HARD_DEFAULT_CONTEXT_PATH` | `/newcoder` |
| `HARD_DEFAULT_IM_SERVICE_URL` | `http://localhost:13631/getuid`（银海通本地 IM） |
| 可配置优先级 | 用户设置文件 `desktop-settings.json` > 环境变量 `AI_CODING_*` > 硬编码默认 |

派生：`apiBase = {lcBaseUrl}{contextPath}` = `https://lc.yinhaiyun.com/newcoder`。

应用标识（`src/appInfo.ts`）：appId `com.yinhai.ta3newcoder.desktop`，productName `Ta+3 牛码`。

### 1.2 登录流程

`src/authService.ts:386-483`（`startBrowserLogin`）+ `src/auth/settings.ts:44-46`：

**路径 A：银海通 IM 静默登录**（`authService.ts:321-357`，用户装了银海通时优先）
1. `GET http://localhost:13631/getuid` → `{success, data: {uid}}`（SM2 加密 uid，1800ms 超时）
2. `POST {baseUrl}/newcoder/aiContinueLogin`，请求头 `Authorization: <uid>`（裸值，body 空）
3. 响应 `{data: {authToken: "ide-session-...", loginId, label, isAdmin}}`；`authToken` 是业务凭证

**路径 B：浏览器 PKCE(SM3) 登录**（无银海通时）
1. `code_verifier` = 43 字符 base64url 随机串（32 字节）；`code_challenge = base64url(SM3(verifier))`，**无填充**（`src/auth/pkce.ts:23-26`，SM3 用纯 JS `sm-crypto`，因为 Node OpenSSL 不含 sm3）
2. 打开 `{apiBase}/api/oauth/authorize?response_type=code&client_id=ide-vscode&redirect_uri=http://localhost:{随机端口}&code_challenge={challenge}&code_challenge_method=SM3&state={uuid}`
   - `client_id` 固定 **`ide-vscode`**（`auth/settings.ts:44`，复用服务端已注册 ClientRegistry）
   - 本地回调 server 监听 `localhost` 随机端口，路径 `/callback`（`src/auth/callbackServer.ts:130-160`），浏览器页面会把 code/token 转发到 `/callback?code=...&state=...`
   - 同时并行 IM 轮询（每 2.5s）兜底，90s 总超时
3. `POST {apiBase}/api/oauth/token`，`Content-Type: application/x-www-form-urlencoded`，body：
   `grant_type=authorization_code&code=...&code_verifier=...&redirect_uri=...&client_id=ide-vscode`
4. 响应 `{access_token(ide-session- 前缀，opaque 非 JWT), refresh_token, expires_in, login_id, user_id, org_id, user_name}`

**Token 刷新**（`src/auth/refresh.ts:96-105`）：
`POST {apiBase}/api/oauth/token`，`grant_type=refresh_token&refresh_token=...&client_id=ide-vscode`；refresh_token 轮转（每次返回新 refresh_token）；`invalid_grant` 时清会话重走登录。

**会话存储**（`src/auth/sessionStore.ts`）：Electron `userData/auth-session.json`，`safeStorage` 加密 accessToken/refreshToken。

### 1.3 业务接口鉴权契约

`src/auth/http.ts:91-103`：
- 业务接口（AiAuthenticationFilter）：`Authorization: <ide-session-...>` **裸值，不带 Bearer**，另加 `X-Client-Type: app`
- LLM 通道（KeyGenerator）：**只认 `llm-` 前缀 key**，`ide-session-` 会被直接拒

### 1.4 模型目录获取

`src/authService.ts:484-586`（`fetchYinhaiCatalog`），按序尝试候选端点（首个成功即用）：

1. 组织列表：`GET {apiBase}/ide/list-organizations`（候选 `/ai/continue/ide/list-organizations` GET/POST），头 `Authorization: <token>` + `X-Client-Type: app`
2. 各组织配置：`GET {apiBase}/ide/list-assistants?organizationId={encoded}`（候选同上），同头
3. 响应中 `assistants[].configResult.config`（或 `config`）含：
   - `models[]`：每个模型含 `model/modelName`（请求名）、`provider/providerName`、**`apiBase`（LLM 网关地址，远端下发）**、**`apiKey`（`llm-` 前缀，per-model 下发）**、`roles`（chat/edit/apply/...）、`completionOptions`（temperature/maxTokens/thinkingEnabled 等）、`chatOptions.baseAgentSystemMessage`（**远端系统提示词**）、`requestOptions.headers`（额外请求头）
   - `modelsByRole` / `selectedModelByRole`、`prompts[]`（斜杠命令）、`rules[]`
4. 归一化逻辑在 `src/auth/catalog.ts`（`normalizeYinhaiCatalog`）：模型 id 取 `title || id || model || name`，请求名取 `model || modelName || modelId || deployment || id || name || title`（`src/chat/modelClient.ts:26-34`）

### 1.5 模型请求（风控核心）

`src/chat/streamRequest.ts:415-470`（`requestModel`）+ `src/chat/modelClient.ts:160-215`：

**协议选择**（`modelClient.ts:56-68`）：identity（provider+model 名拼接）含 `anthropic|claude|kimi` 或 apiBase 含 `/anthropic` → Anthropic 协议；否则 OpenAI 兼容。

**OpenAI 兼容**：
- 端点：`{model.apiBase}/chat/completions`（apiBase 已以 `/` 结尾处理）
- 头（`modelClient.ts:163-175` `buildModelHeaders`）：
  ```
  Content-Type: application/json
  Accept: text/event-stream, application/json
  X-Call-Source: APP
  Authorization: Bearer {llm-key}
  api-key: {llm-key}
  ```
- 体（`streamRequest.ts:124-180` `buildRequestBody`）：
  ```json
  {
    "model": "{请求名}",
    "messages": [...],            // 工具轮回传 reasoning_content，普通轮剥离（applyPlainTurnReasoningPolicy）
    "stream": true,
    "temperature": 0.1,           // completionOptions.temperature 默认 0.1
    "max_tokens": ...,
    "tools": [...],               // OpenAI function 格式
    "tool_choice": "auto",
    "thinking": {"type": "enabled"},  // thinkingEnabled 模型（qwen 系用 enable_thinking 布尔）
    "reasoning_effort": "..."
  }
  ```
  - `zai` 供应商额外加 `stream_options: {include_usage: true}` 和 `tool_stream: true`
- 流式解析：SSE `data:` 帧；`reasoning_content/reasoning/thinking` 字段视为思考；`[DONE]` 或 `finish_reason` 终止；尾帧 `usage` 提取

**Anthropic 协议**：
- 端点：`{apiBase}/v1/messages`（apiBase 已含 v1 时直接 `messages`）
- 头（`modelClient.ts:196-213`）：`x-api-key: {llm-key}`、`anthropic-version: 2023-06-01`、`X-Call-Source: APP`（Azure 端点用 `api-key`）
- 体（`anthropicAdapter.ts:392-460`）：`{model, messages(blocks), system, stream: true, temperature, max_tokens: 2048, thinking: {type: enabled, budget_tokens}, output_config: {effort}, tools: [{name, description, input_schema}]}`
  - Kimi 模型特判：temperature 0.2、思考用 `output_config.effort`（low/high/max）不发 thinking 块
- 流式事件：`content_block_delta`（text_delta / input_json_delta / thinking_delta）、`message_stop`

**超时**：首 chunk 30s / 流 idle 30s / thinking 看门狗 120s（`streamRequest.ts:10-13`）。

**UA 说明**：ta3 主进程用 Electron `fetch`（net 栈），UA 为 Electron/Chromium 默认 UA，未自定义。Python 侧必须手动设置同族 UA（见 5.3）。

### 1.6 系统提示词框架

`src/chatService.ts:283-306`（`buildBaseMessages`），system 消息 = 以下段落 `\n\n` 连接：

| 段落 | 来源 | 说明 |
|---|---|---|
| 基础系统提示词 | `model.chatOptions.baseAgentSystemMessage`（远端下发，plan 模式用 `basePlanSystemMessage`） | 主体，英文/中文由远端决定 |
| 任务列表纪律 | `chatService.ts:121-135`（本地硬编码，中文） | TodoWrite 使用规范 |
| 工具使用纪律 | `chatService.ts:137-157`（本地硬编码，中文） | 禁止幻觉式工具调用 |
| 测试会话段落 | `chatService.ts:263-281` | 仅 `conversationKind === 'test'` 注入（test_* 工具） |
| 技能段落 | `skillService.getAvailableSkillsSystemSection()` | |
| 记忆段落 | `memoryService.buildMemoryInjection()` | |
| runtime-context 快照 | `chatService.ts:159-173`：`<runtime-context>` XML，当前时间（本地+ISO）+ 工作目录，"supersedes any earlier" 语义 | |

上下文文件 `<context-files>` 与技能 `<context-skills>` 注入最后一条 user 消息前部（`chatService.ts:377-403`）。

### 1.7 工具定义（完整清单，OpenAI function 格式）

`src/tools/toolDefinitions/`（index.ts 聚合 34 个）：

| 分组 | 工具名 |
|---|---|
| core.ts | `Read` `List` `Search` `Diff` `ReadSkill` `get_project_memory` `generate_project_memory` `read_file_range` `get_file_outline` |
| edit.ts | `Write` `Edit` `Bash` `RevertFile` `single_find_and_replace` |
| task.ts | `TodoWrite` `SubAgent` `SubAgentAsync` `TaskQuery` `TaskCancel` |
| webSearch.ts | `WebSearch` |
| testing.ts | `test_inspect` `test_create_record` `test_prepare_record` `test_confirm_plan` `test_start` `test_status` `test_review` `test_stop` `test_save_asset` `test_link_assets` `test_verify_healing` |

schema 风格：中文 description；参数命名混合（`filepath`/`dirPath`/`query` snake 与 camel 混用）；定义含 `displayTitle`/`readonly`/`group`/`defaultPermission` 元字段（发给模型前 `toOpenAiTool` 只取 `type`+`function`）。

### 1.8 请求时序总览

```
[登录]  GET :13631/getuid (可选) ──▶ POST /newcoder/aiContinueLogin (Authorization: uid)
        或 打开 /newcoder/api/oauth/authorize (PKCE-SM3, client_id=ide-vscode)
            ──▶ 本地 localhost 回调收 code ──▶ POST /newcoder/api/oauth/token
            ──▶ access_token(ide-session-) + refresh_token

[目录]  GET /newcoder/ide/list-organizations        (Authorization: 裸值 + X-Client-Type: app)
        GET /newcoder/ide/list-assistants?orgId=...  ──▶ config.models[] (apiBase + llm-key + 系统提示词)

[对话]  POST {model.apiBase}/chat/completions        (Authorization: Bearer llm-... + api-key + X-Call-Source: APP)
        或  POST {model.apiBase}/v1/messages          (x-api-key + anthropic-version + X-Call-Source: APP)
        SSE 流式返回
```

---

## 2. 当前项目接入点分析

### 2.1 供应商数据模型

`server/app/persistence/models/model_reg.py`：
- `Provider`：`name` / `base_url` / `api_key` / **`api_format`（现取值 `openai` | `anthropic`）** / `is_active`
- `Model`：`provider_id` / `name` / `context_window` / `api_format` / **`api_key`（v2.0 per-model key，本地明文）** / `reasoning_efforts` / `is_multimodal`

### 2.2 Provider 抽象与路由

- `server/app/models/base.py:13`：`ModelProvider(ABC)`，方法 `chat` / `stream` / `stream_structured`（agent_loop 主路径用 `stream_structured`）
- `server/app/models/registry.py:26-44` `_build_provider(api_key, base_url, model, api_format)`：按 `api_format` 分派 `OpenAICompatibleProvider` / `AnthropicProvider`；**新增 ta3 类型只需在此加分派**
- `registry.py:96-110` `get_provider_for_model`：`model.provider_id` → Provider 记录 → 用其 `base_url/api_key/api_format` 构造
- `server/app/models/providers/openai_compatible.py`：OpenAI SDK（stream=True + include_usage），含 thinking 参数、reasoning_content 回传、DSML/退化文本兜底解析——ta3 可大量复用其流收集骨架

### 2.3 聊天链路（不动）

`server/app/orchestration/agent_loop.py:145-161` 解析 provider → `provider.stream_structured(request)` → 工具执行（`orchestration/tools/registry.py`）→ 循环；压缩在 `orchestration/compaction.py`；上下文组装在 `orchestration/context_manager.py`；系统提示词在 `orchestration/prompts/`（英文 `MAIN_SYSTEM_PROMPT`）。

### 2.4 工具名（真实执行名）

`server/app/orchestration/tools/registry.py` 注册：`fs_read` `fs_list` `fs_write` `terminal_exec` `editor_apply_diff` `web_fetch` `ci_run` `memory_search` `git_diff` `fs_grep` `web_search` `view_image` `read_attachment` `todo_write` `multi_file_edit` `git` `codebase_search` `ask_user_question`；另有 `browser_navigate` `browser_screenshot`（`tools/browser.py`）、子代理 `spawn_subagent` / `collect_results`（`subagent_tools.py`，由 agent_loop 特判执行）、`mcp_*` 动态工具。

### 2.5 前端

- 供应商管理：`client/src/components/settings/ModelsPanel.tsx`（api_format 下拉仅 openai/anthropic，行 78）
- 模型选择：`client/src/components/chat/ModelPicker.tsx`
- 供应商 API：`server/app/gateway/routers/providers.py`（CRUD + `/scan` + `/test`）

### 2.6 工具 schema 注入点

`agent_loop` 收到的 `tool_schemas` 由 engine 组装（`tool_registry.all_schemas()` + 子代理工具 + MCP），透传给 `ChatRequest.tools`。**工具名伪装层应放在"schema 组装之后、ChatRequest 构造之前"，或在 Provider 内部转换**（见 5.4 决策）。

---

## 3. 总体架构

```
┌─────────────────────────── chatcoder（当前项目）────────────────────────────┐
│                                                                              │
│  前端 ModelsPanel/ModelPicker          后端 FastAPI                           │
│  ├─ "Ta+3 牛码" 供应商类型             ├─ app/auth/ta3/          ★新增        │
│  │   └─ 登录按钮 → 打开系统浏览器       │   ├─ pkce.py   (SM3 PKCE)           │
│  │       (Electron shell.openExternal) │   ├─ oauth.py  (authorize/token)    │
│  └─ 模型列表展示 ta3 分组              │   ├─ catalog.py(list-org/assistant) │
│                                        │   └─ session.py(token 存储/刷新)    │
│                                        ├─ app/models/providers/ta3.py ★新增 │
│                                        │   └─ Ta3Provider(ModelProvider)     │
│                                        │       ├─ 请求头/请求体伪装           │
│                                        │       ├─ 工具名双向映射              │
│                                        │       └─ OpenAI/Anthropic 双协议     │
│                                        ├─ agent_loop / context_manager /     │
│                                        │   compaction / prompts    不动      │
│                                        └─ routers/providers.py   扩展        │
└──────────────────────────────────────────────────────────────────────────────┘
          │ 登录/目录(token)                     │ LLM 请求(llm-key)
          ▼                                      ▼
   https://lc.yinhaiyun.com/newcoder       {model.apiBase}（远端下发）
   /api/oauth/*  /ide/list-*               /chat/completions | /v1/messages
```

**职责边界**（按用户要求）：
- ta3 侧负责：登录、token 刷新、模型目录同步、LLM 请求收发（含伪装）
- 当前项目负责：上下文组装、压缩摘要、系统规则注入、工具执行、审批、消息流、任务管理

---

## 4. 数据模型变更

### 4.1 Provider 表

1. `api_format` 扩展枚举值 **`ta3`**（前端下拉同步加选项 "ta3（Ta+3 牛码）"）。
2. 复用字段语义：
   - `base_url` = ta3 apiBase（`https://lc.yinhaiyun.com/newcoder`，允许用户改环境）
   - `api_key` = 不用（ta3 登录态独立存储，见 4.3）
3. 新增字段（Provider 级，均可空）：
   - `auth_status` TEXT（`pending` | `logged_in`）
   - `account_label` TEXT（登录账号显示名）

### 4.2 Model 表（ta3 供应商下的模型）

复用现有字段：
- `name` = ta3 模型请求名（`model/modelName`）
- `api_key` = per-model `llm-` key（复用 v2.0 per-model key 通道；**注意 key 会过期/轮换，每次目录同步时更新**）
- `context_window` / `reasoning_efforts` / `is_multimodal` 从目录元数据回填
- `base_url` = per-model `apiBase`（Model 表本就有 base_url 字段，ta3 模型写入下发值；请求时优先 Model.base_url，缺省回退 Provider.base_url）

新增字段（Model 级，JSON）：
- `ta3_meta` JSON：`{ "systemMessage": "...", "anthropic": bool, "provider": "...", "completionOptions": {...}, "requestHeaders": {...}, "title": "..." }`
  - `systemMessage` = 远端 `chatOptions.baseAgentSystemMessage`（系统提示词还原的数据源）
  - `anthropic` = 协议判定结果（预计算，避免每次请求重复判定）

### 4.3 ta3 登录态存储

新表 `ta3_auth`（单行，tenant 级）：
```python
class Ta3Auth(Base):
    __tablename__ = "ta3_auth"
    id: int                      # 主键，恒为 1
    provider_id: int             # 关联 Provider
    access_token: str | None     # ide-session-...
    refresh_token: str | None
    account: JSON                # {id, label, loginId, orgId...}
    catalog: JSON | None         # 最近一次目录原文缓存
    expires_at: datetime | None  # 可选
    updated_at: datetime
```
> 参考项目用 Electron safeStorage 加密；当前项目桌面版 API key 本就明文存 SQLite（`model_reg.py` 注释），保持同等安全级别即可，不做额外加密（后续可加）。

### 4.4 工具伪装映射存储

代码内常量表（`app/models/providers/ta3_tool_aliases.py`），不做 DB 配置——映射是静态的，改映射=改代码。

---

## 5. 详细设计

### 5.1 认证模块 `app/auth/ta3/`

```
app/auth/ta3/
├── __init__.py
├── pkce.py      # gen_code_verifier() + sm3_challenge()  【Python SM3】
├── oauth.py     # start_browser_login() / exchange_code() / refresh_token()
├── catalog.py   # list_organizations() / list_assistants() / normalize_catalog()
├── session.py   # load/save/clear Ta3Auth；ensure_token()（401 自动 refresh）
└── callback.py  # 本地回调 HTTP server（uvicorn/starlette 或裸 asyncio）
```

**SM3 实现**：`pip install gmssl`（国密标准库，`gmssl.sm3.sm3_hash`）；打包 (PyInstaller) 注意 hidden imports。备选：vendored 纯 Python SM3 单文件（约 100 行），零依赖更稳——**推荐 vendored**（避免打包环境问题）。

**登录流程**（后端驱动，前端触发）：
1. 前端 `POST /api/providers/{id}/ta3/login/start` → 后端：
   - 生成 verifier/challenge/state
   - 启动本地回调 server（`localhost` 随机端口，路由 `/callback`，支持 query `?code=&state=`；同时返回等待协程）
   - 返回 `{authorize_url, state}` 给前端
2. 前端 Electron 主进程 `shell.openExternal(authorize_url)`（走系统默认浏览器，与 ta3 行为一致）
3. 浏览器登录成功 → 回调 server 收到 code → 校验 state → `POST {apiBase}/api/oauth/token`（form：grant_type=authorization_code, code, code_verifier, redirect_uri, client_id=ide-vscode）
4. 存 `ta3_auth`（access/refresh/account），拉取目录（5.2），`Provider.auth_status='logged_in'`
5. 前端轮询 `GET /api/providers/{id}/ta3/login/status` 或由 WS 推送结果

**IM 静默登录增强**（可选，Phase 3）：`GET http://localhost:13631/getuid` 成功则直接 `POST /newcoder/aiContinueLogin`（头 `Authorization: <uid>`），跳过浏览器。失败静默降级到 PKCE。对齐 `authService.ts:744-762` `startLogin` 的优先级逻辑。

**Token 刷新**：`session.ensure_token()`——所有业务请求（目录）401 时调用 `grant_type=refresh_token` 换新（refresh_token 轮转，存回 DB）；`invalid_grant` 时置 `auth_status='pending'` 并抛"需要重新登录"。加 in-flight 锁防并发 stampede（对齐 `authService.ts:787-792`）。

### 5.2 目录同步 `app/auth/ta3/catalog.py`

```
sync_ta3_models(db, provider_id) -> list[Model]
 1. token = ensure_token()
 2. GET {apiBase}/ide/list-organizations     # Authorization: 裸值 + X-Client-Type: app
    └─ 失败按参考项目顺序尝试 /ai/continue/ide/list-organizations (GET→POST)
 3. 逐 org：GET {apiBase}/ide/list-assistants?organizationId=...
 4. 解析 assistants[].configResult.config：
    models[] → 逐模型：
      request_name = model || modelName || modelId || id || name || title
      api_key     = apiKey (llm-)
      base_url    = apiBase
      anthropic   = identity 含 anthropic|claude|kimi 或 apiBase 含 /anthropic
      system_msg  = chatOptions.baseAgentSystemMessage
 5. upsert 到 Model 表（provider_id=ta3 provider，按 name 匹配更新 api_key/ta3_meta）
```

前端入口：ModelsPanel ta3 供应商卡片加 **"同步模型"** 按钮 → `POST /api/providers/{id}/ta3/sync`。供应商通用 `/scan` 端点不适用于 ta3（目录不是 OpenAI `/models` 格式），ta3 供应商隐藏"扫描"按钮改为"同步"。

### 5.3 Ta3Provider `app/models/providers/ta3.py`

继承/组合 `OpenAICompatibleProvider` 的**流收集骨架**但**不走 OpenAI SDK**（SDK 会自带 `User-Agent: OpenAI/Python`、`x-stainless-*` 头，风控指纹明显），改用 `httpx.AsyncClient` 裸请求，完全控制头与体：

```python
class Ta3Provider(ModelProvider):
    name = "ta3"

    def __init__(self, *, api_key: str, base_url: str, model: str, meta: dict):
        self._api_key = api_key      # llm-...
        self._base_url = base_url    # per-model apiBase
        self._meta = meta            # ta3_meta: {anthropic, systemMessage, completionOptions, ...}
        self._anthropic = bool(meta.get("anthropic"))
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None))  # 读不设限，chunk 级看门狗
```

**请求头构造**（复刻 `modelClient.ts`，逐字段）：

OpenAI 兼容模型：
```
Content-Type: application/json
Accept: text/event-stream, application/json
X-Call-Source: APP
Authorization: Bearer {llm-key}
api-key: {llm-key}
User-Agent: {ta3 UA}
+ meta.requestHeaders 逐项追加（远端 requestOptions.headers）
```

Anthropic 模型：
```
Content-Type: application/json
Accept: text/event-stream, application/json
X-Call-Source: APP
x-api-key: {llm-key}
anthropic-version: 2023-06-01
User-Agent: {ta3 UA}
+ meta.requestHeaders
```

**UA 策略**：ta3 是 Electron 应用，主进程 fetch（undici/Electron net）UA 形如 `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ta3-new-coder-desktop/x.y.z Chrome/zz.z.zzzz Electron/vv.v Electron`。方案：取参考项目 `package.json` 的版本与当前 Chrome 主流版本号拼接成同族 UA，**作为配置项**（`settings.ta3_user_agent`）可覆盖；默认值写入 `.env.example`。httpx 禁用自动 gzip 之外的压缩头注入（`Accept-Encoding: gzip, deflate`，与参考项目一致的兼容策略，对齐 `openai_compatible.py:50-52` 的 brotli 教训）。

**请求体构造**（复刻 `buildRequestBody` / `buildAnthropicRequestBody`）：

OpenAI 兼容：
```python
body = {
  "model": request.model,
  "messages": self._convert_messages(request),   # 工具轮回传 reasoning_content，普通轮剥离
  "stream": True,
  "temperature": opts.get("temperature", 0.1),
  "tools": tool_schemas,                          # 已伪装（5.4）
  "tool_choice": "auto",
}
# thinking 模型（completionOptions.thinkingEnabled）:
#   qwen/dashscope 系 → body["enable_thinking"]=True; body["reasoning_effort"]=effort
#   其他 → body["thinking"]={"type":"enabled"}; body["reasoning_effort"]=effort
#   effort=none → body["thinking"]={"type":"disabled"}
# zai 供应商 → body["stream_options"]={"include_usage":True}; body["tool_stream"]=True
body = {k: v for k, v in body.items() if v not in (None, "")}   # pruneRequestBody
```

Anthropic：messages→blocks 转换（text/tool_use/thinking blocks）、system 单列、`max_tokens: 2048`、thinking `{type: enabled, budget_tokens: max(2048, 0.8*max_tokens)}` + `output_config.effort`、Kimi 特判（temp 0.2、仅 output_config.effort、thinking 块强制每轮回传）。首版可只做 OpenAI 兼容分支，Anthropic 分支 Phase 2 补（目录里若无 anthropic 协议模型则顺延）。

**流式解析**：SSE 手动解析（`data:` 帧 → JSON），对齐 `streamRequest.ts:224-254`：
- `choices[0].delta.content` → content
- `delta.reasoning_content / reasoning / thinking` → thinking
- `delta.tool_calls` 增量拼装 → tool_calls（**name 反伪装映射回真实名**）
- `finish_reason` → 终止；尾帧 `usage`（prompt/completion/reasoning_tokens_details）
- 看门狗：首 chunk 30s、chunk 间隔 30s、thinking 120s（对齐 `streamRequest.ts:10-13`）
- `stream_structured` 产出格式与 `OpenAICompatibleProvider.stream_structured` 完全一致（`{"type": "thinking"|"content"|"done", ...}`），agent_loop 零改动

**复用兜底解析**：把 `openai_compatible.py` 的 `_parse_dsml_tool_calls` / `_parse_degraded_tool_calls` 提为公共函数供 Ta3Provider 复用（退化文本里出现的工具名是伪装名，同样过反伪装映射）。

**注册**：`registry.py::_build_provider` 加分支：
```python
if api_format == "ta3":
    from app.models.providers.ta3 import Ta3Provider
    return Ta3Provider(api_key=api_key, base_url=base_url, model=model, meta=ta3_meta)
```
`ta3_meta` 来源：`get_provider_for_model` 构造时从 `Model.ta3_meta`（getattr 安全读取）与 `Model.base_url`（per-model 覆盖 Provider.base_url）取。

### 5.4 工具名伪装层（核心）

**放置位置决策**：在 **Ta3Provider 内部**做双向转换（而非 agent_loop 组装处）。理由：
1. 仅 ta3 供应商需要伪装，其他供应商不受影响；
2. ChatRequest.messages 里历史 assistant.tool_calls 的工具名也必须一致伪装（provider 内统一处理最可靠）；
3. agent_loop / engine / 工具注册表零改动，符合"编排沿用当前项目"。

**映射表** `app/models/providers/ta3_tool_aliases.py`：

```python
# 真实名（当前项目执行名） → 伪装名（ta3 工具名）
TO_TA3 = {
    "fs_read":              "Read",
    "fs_list":              "List",
    "fs_grep":              "Search",
    "git_diff":             "Diff",
    "fs_write":             "Write",
    "editor_apply_diff":    "Edit",
    "multi_file_edit":      "Edit",          # 描述并档（见下）
    "terminal_exec":        "Bash",
    "web_search":           "WebSearch",
    "todo_write":           "TodoWrite",
    "spawn_subagent":       "SubAgent",
    "memory_search":        "get_project_memory",
    "read_attachment":      "read_file_range",
    "view_image":           "read_file_range",   # 描述并档
    "codebase_search":      "get_file_outline",  # 描述并档（Phase1 可不映射）
    "git":                  "Bash",          # git 操作按 Bash 语义伪装
    "ci_run":               "Bash",
    "browser_navigate":     "WebSearch",     # 近义伪装（Phase 2 评估）
    "browser_screenshot":   "read_file_range",
    "web_fetch":            "WebSearch",     # 近义伪装
    "ask_user_question":    "TodoWrite",     # 不佳——见"未映射工具"策略
}
FROM_TA3 = {v: k for k, v in TO_TA3.items()}   # 反查；并档的取主映射
```

> 上面近义伪装仅为占位建议，实现时按下述原则逐条定稿：
> - **语义完全一致** → 直接映射（fs_read→Read 等）
> - **ta3 无对应工具** → 两种策略择一：(a) 不发给 ta3 模型（从 tools 列表剔除，系统提示词中说明用替代工具完成）；(b) 近义伪装。默认选 (a) 更安全（模型调用一个 schema 与真实行为不符的工具会混乱），仅 `web_fetch`/`view_image` 这类高频必需的用 (b) 并改写 description。
> - `ask_user_question`、`collect_results`、`mcp_*`、`test_*`：ta3 模式下默认剔除（ask_user 场景由当前项目审批 UI 承担）；MCP 工具若必须暴露，统一伪装挂到 `Bash` 语义外不可行——Phase 1 明确不支持，Phase 2 再议。

**Schema 还原**：不止改名字，直接用 ta3 的原生定义。新建 `app/models/providers/ta3_tool_schemas.py`，把参考项目 `toolDefinitions/core.ts`、`edit.ts`、`task.ts`、`webSearch.ts` 的 `{type, function:{name, description, parameters}}` **原样移植为 Python 常量**（保留中文 description 原文）。请求时：

```python
def disguise_tools(tool_schemas, whitelist_real_names) -> list[dict]:
    out = []
    for real in tool_schemas:
        t = real["function"]["name"]
        alias = TO_TA3.get(t)
        if alias is None:      # 无映射 → 剔除
            continue
        out.append(TA3_NATIVE_SCHEMAS[alias])   # ta3 原生 schema
    return out
```

并在部分原生 description 末尾**追加当前项目流程规范**（用户要求"加入一些当前项目的流程规范限制"），例如：
- `Bash`（原 description 后追加）：`长驻进程（dev server/watch/后端服务）命令必须在后台运行模式启动。`（terminal_exec 的真实约束）
- `Edit` 追加：`优先精确替换，替换 oldString 必须与文件内容精确匹配。`（editor_apply_diff 的真实约束）
- 未启用子代理时剔除 `SubAgent`。

**消息双向转换**：
- 出站（发给 ta3）：messages 里 `assistant.tool_calls[].function.name` → `TO_TA3`；`tool` 消息无需改名（tool_call_id 关联），但若某 tool_call 未映射（历史数据），该 assistant/tool 对按"消息协议修复"策略改写为 user 文本描述（复用 `compaction.ensure_tool_pairing` 思路，provider 内做最后防线兜底）。
- 入站（模型返回）：`tool_calls[].name` → `FROM_TA3`（映射回真实执行名）；未知名（模型幻觉出 ta3 有但当前项目没有的工具，如 `RevertFile`/`SubAgentAsync`/`test_*`）→ 返回结构化错误文本 "工具不可用"（参考项目 executor 对未注册工具也是报错语义），不中断循环。

### 5.5 系统提示词还原与融合

新建 `app/orchestration/prompts/ta3_fusion.py`：

```python
def build_ta3_system_prompt(model_meta: dict, current_prompt: str, workspace: str) -> str:
    sections = [
        model_meta.get("systemMessage") or "",          # ① ta3 远端下发主体（还原）
        TA3_TASK_MANAGEMENT_SECTION,                    # ② ta3 任务列表纪律（原文移植）
        TA3_TOOL_DISCIPLINE_SECTION,                    # ③ ta3 工具纪律（原文移植）
        build_runtime_snapshot(workspace),              # ④ <runtime-context> 快照（ta3 格式）
        build_chatcoder_addendum(current_prompt),       # ⑤ 当前项目流程规范（精简）
    ]
    return "\n\n".join(s for s in sections if s)
```

- ②③ 从 `chatService.ts:121-157` 原文移植（中文，TodoWrite 规范 + 工具纪律）；
- ④ 复刻 `<runtime-context>` 格式（当前时间 zh-CN 本地格式 + ISO + 工作目录 + supersede 声明）；
- ⑤ 从当前项目 `MAIN_SYSTEM_PROMPT` 提取**不依赖工具名**的通用规范并翻译为与 ta3 提示词一致的语言风格：回复用简体中文、无表情符号、简洁直接、结论先行、改动文件带 `path:line` 引用、编辑前先读文件、每步验证等；**剔除**其中引用 `fs_grep/fs_read` 等真实工具名的段落（已被 ②③+伪装 schema 取代）；
- 子代理引导：若启用子代理，追加 ta3 `SubAgent` 工具使用段落（从 `task.ts` description 提炼）。

**注入点**：`context_manager.build_main_context` 里当 session 选中的模型属于 ta3 供应商（或 provider 是 Ta3Provider）时，`bundle.system` 用 `build_ta3_system_prompt(...)` 替换 `build_main_system_prompt(...)`。developer 分层注入（Current Goal/Project Structure/Rules/Memory）**保持不动**（沿用当前项目上下文管理）。

### 5.6 上下文/压缩沿用

- `compaction.py`（auto_compact / emergency_compact / build_api_copy / ensure_tool_pairing）在 provider 之前作用于真实消息（真实工具名），与供应商无关——不动。
- 压缩用的摘要请求若也路由到 ta3 模型，走同一 Ta3Provider（同样伪装），无需特判。
- `token_counter` 估算与窗口：ta3 模型 `context_window` 从目录回填，压缩阈值逻辑自动生效。

### 5.7 前端改造

`client/src/components/settings/ModelsPanel.tsx`：
1. api_format 下拉加 `<option value="ta3">ta3（Ta+3 牛码）</option>`（行 78 附近）；
2. 选择 ta3 时表单变化：隐藏 api_key 输入，显示**[登录 Ta+3 账号]**按钮 + 登录状态（账号名/待登录）+ **[同步模型]**按钮；
3. 登录点击 → `POST /providers/{id}/ta3/login/start` → 拿到 authorize_url → `window.electronAPI.openExternal(url)`（Electron preload 已有能力则复用；没有则在 electron/ 主进程加 IPC handler）→ 轮询 status 展示结果；
4. ta3 供应商卡片隐藏 [扫描] [测试] 按钮，改为 [同步模型]；
5. 模型列表展示 `Model.name` + ta3 标识 tag。

`client/src/components/chat/ModelPicker.tsx`：无需结构改动（ta3 模型已入 Model 表，按供应商分组自然出现）；加个小标识可选。

### 5.8 API 设计（后端新增路由，挂在 `providers.py` 或新 `ta3_auth.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/providers/{id}/ta3/login/start` | 启动登录：起回调 server，返回 `{authorize_url, state, expires_in}` |
| POST | `/api/providers/{id}/ta3/login/cancel` | 取消登录（关回调 server） |
| GET  | `/api/providers/{id}/ta3/login/status` | `{status: pending|logged_in|failed, account?}` |
| POST | `/api/providers/{id}/ta3/logout` | 清 ta3_auth |
| POST | `/api/providers/{id}/ta3/sync` | 同步目录（list-organizations + list-assistants → upsert Models），返回模型数 |
| GET  | `/api/providers/{id}/ta3/models/raw` | 调试用：目录原文（脱敏 key） |

### 5.9 风控规避清单（实施验收项）

| # | 项 | 措施 | 依据 |
|---|---|---|---|
| 1 | Authorization 形态 | 业务接口裸值 `Authorization: <ide-session->`（无 Bearer）+ `X-Client-Type: app`；LLM 接口 `Bearer llm-` + `api-key` 双头 | http.ts:91-103 / modelClient.ts:163 |
| 2 | X-Call-Source | LLM 请求必带 `X-Call-Source: APP` | modelClient.ts:168 |
| 3 | UA | httpx 默认 UA 禁用，设置为 Electron 同族 UA（配置项） | 1.5 UA 说明 |
| 4 | 请求体指纹 | temperature 默认 0.1、prune 空字段、thinking 字段按模型系别（qwen enable_thinking / 其他 thinking-object / kimi output_config）、zai 加 stream_options+tool_stream | streamRequest.ts:124-196 |
| 5 | PKCE 算法 | SM3（非 SHA256），challenge base64url 无填充，verifier 43 字符 | pkce.ts |
| 6 | client_id | 固定 `ide-vscode` | settings.ts:44 |
| 7 | redirect_uri | `http://localhost:{随机端口}`（loopback，非 127.0.0.1） | callbackServer.ts |
| 8 | SSE 解析 | `data:` 帧、`[DONE]`、finish_reason 语义与 ta3 一致 | streamRequest.ts |
| 9 | 时序 | 目录同步串行请求逐 org（ta3 行为一致）；LLM 请求不并发轰炸（agent_loop 本身串行） | authService.ts:509-536 |
| 10 | 错误处理 | 401→refresh（in-flight 锁）→重试一次；invalid_grant→要求重新登录；429/5xx 指数退避（复用现有 retry 语义） | authService.ts:786-818 |
| 11 | httpx 指纹细节 | `Accept-Encoding: gzip, deflate`（不带 br）；禁用 httpx 自动注入的默认头覆盖我们设置的头 | openai_compatible.py:50 教训 |

---

## 6. 实施计划

### Phase 1：认证 + 目录 + 基础对话（MVP，先跑通）

1. `app/auth/ta3/pkce.py`（vendored SM3 + PKCE 工具，单测：对照参考项目 sm-crypto 输出向量）
2. `app/auth/ta3/session.py` + `ta3_auth` 表（Alembic/同步建表，与项目现有迁移方式一致）
3. `app/auth/ta3/oauth.py` + `callback.py`：浏览器 PKCE 登录全链路
4. `app/auth/ta3/catalog.py`：list-organizations / list-assistants / normalize（移植 `auth/catalog.ts` 归一化逻辑的必要部分）
5. DB 变更：Provider.auth_status/account_label、Model.ta3_meta、api_format='ta3'
6. `ta3_tool_aliases.py` + `ta3_tool_schemas.py`（移植 ta3 原生 schema）
7. `Ta3Provider`（OpenAI 兼容分支：头/体/流式/工具双向映射/兜底解析复用）
8. `registry.py` 分派 + `Model.base_url` per-model 覆盖逻辑
9. `prompts/ta3_fusion.py` + `context_manager` 注入开关
10. 路由 `ta3` login/sync API + ModelsPanel 前端改造
11. 端到端联调：登录→同步→选模型→发消息→工具调用往返→思考流→压缩

### Phase 2：完善与加固

- Anthropic 协议分支（/v1/messages、blocks 转换、thinking/output_config、Kimi 特判）
- IM 静默登录（:13631/getuid + aiContinueLogin）
- 未映射工具的 description 改写与并档策略定稿（web_fetch/view_image/browser_*）
- 子代理伪装（spawn_subagent→SubAgent 描述对齐 + 系统提示词 SubAgent 段落）
- refresh_token 轮换过期→重登录的自动化 UX（前端提示横幅）
- thinking 看门狗、退避重试参数化

### Phase 3：增强（可选）

- 多组织（organization）切换支持（当前按参考项目默认选第一个有 assistants 的 org）
- ta3 prompts/rules（斜杠命令）同步进当前项目技能/规则体系
- 打包验证（PyInstaller hidden imports、SM3 纯 Python 无 C 依赖天然安全）

### 验收标准

- [ ] 浏览器登录成功拿到 ide-session token，重启应用后免登录（refresh 可用）
- [ ] 目录同步后 ModelPicker 出现 ta3 分组，模型可用 llm-key 发起对话
- [ ] 对话中模型调用的工具名是 ta3 工具名（抓包确认请求体 tools 为 Read/List/...），实际执行的是当前项目工具（消息流卡片正常）
- [ ] 系统提示词首段为远端 baseAgentSystemMessage，含任务列表/工具纪律/runtime-context 段落与当前项目规范追加段
- [ ] 上下文压缩、审批、子代理（若启用）等当前项目流程行为不变
- [ ] 与参考项目同时使用同一账号不互相踢线（token 独立）

---

## 7. 风险与待确认事项

| # | 风险/待确认 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **apiBase/apiKey 实际值未知**：由 list-assistants 动态下发，未登录无法静态确认 LLM 网关域名与鉴权细节 | 请求伪装细节可能有偏差 | Phase 1 第一步先真机登录抓目录（可先用参考项目本体登录后从其 `auth-session.json`/日志核对字段），再定稿请求实现 |
| 2 | 服务端可能校验更多指纹（TLS 指纹 JA3、请求频率、账号单点登录数） | 风控拦截 | UA/头已对齐；TLS 指纹 httpx(ssl) 与 Electron(Chromium BoringSSL) 有差异，若被拦截需评估 curl_cffi（impersonate chrome）替换 httpx——预留 `Ta3Provider._client` 工厂位 |
| 3 | `llm-` key 生命周期未知（是否短时效） | 长会话中断 | 每次目录同步刷新 key；401 时自动 re-sync 目录再重试 |
| 4 | Anthropic 协议模型在目录中是否存在未知 | Phase 2 工作量 | 首版仅 OpenAI 兼容分支，遇到再补 |
| 5 | SM3 实现正确性 | 无法登录 | vendored 实现用 RFC/国标测试向量 + 参考项目 sm-crypto 对照向量做单测 |
| 6 | 多账号/多环境（测试环境 base url） | 配置项 | Provider.base_url 可编辑；env `AI_CODING_LC_BASE_URL` 语义对齐参考项目 |
| 7 | Electron `shell.openExternal` 通道是否存在 | 前端登录体验 | 检查 electron/preload；若无则加 3 行 IPC handler |
| 8 | 法务/合规：模拟客户端使用第三方模型服务 | 账号风险 | 用户自有账号、自有密钥，自担风险；本方案仅做协议兼容 |

---

## 8. 涉及文件清单（改动汇总）

**新增**：
```
server/app/auth/ta3/__init__.py
server/app/auth/ta3/pkce.py            # SM3+PKCE（vendored，含测试向量）
server/app/auth/ta3/oauth.py
server/app/auth/ta3/callback.py
server/app/app/auth/ta3/catalog.py
server/app/auth/ta3/session.py
server/app/models/providers/ta3.py     # Ta3Provider
server/app/models/providers/ta3_tool_aliases.py
server/app/models/providers/ta3_tool_schemas.py   # ta3 原生工具 schema 移植
server/app/orchestration/prompts/ta3_fusion.py
server/app/gateway/routers/ta3_auth.py # 或并入 providers.py
server/tests/test_ta3_pkce.py
server/tests/test_ta3_aliases.py
```

**修改**：
```
server/app/persistence/models/model_reg.py     # Provider.auth_status/account_label; Model.ta3_meta
server/app/models/registry.py                  # api_format='ta3' 分派; per-model base_url
server/app/orchestration/context_manager.py    # ta3 模型时 system prompt 切换
server/app/gateway/routers/providers.py        # ta3 路由注册
server/app/main.py                             # 路由挂载（如新建 router）
client/src/components/settings/ModelsPanel.tsx # ta3 类型表单/登录/同步按钮
electron/（如需）                               # openExternal IPC
server/.env.example                            # TA3_USER_AGENT 等配置
```

**不动**：agent_loop.py、compaction.py、tools/registry.py、engine.py、MessageFlow 等全部聊天链路。
