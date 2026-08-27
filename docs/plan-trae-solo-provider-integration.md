# TRAE SOLO CN（Trae Work）模型供应商接入方案

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-08-25 |
| 参考项目 | `D:\aiTools\TRAE SOLO CN`（字节跳动 TRAE SOLO CN 桌面版，Electron + Rust ai_agent，产品名 "TRAE SOLO CN"） |
| 当前项目 | `D:\myProject\chatcoder`（Electron + React 前端 / Python FastAPI 后端） |
| 对齐先例 | `docs/plan-ta3-provider-integration.md`（Ta+3 牛码）、workbuddy（腾讯 CodeBuddy） |

---

## 实施状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| 逆向分析（登录/目录/调用链路） | ✅ 完成 | 事实清单见 §1（main.js 反混淆 + 本机 ai-agent 日志交叉验证） |
| Phase 0（抓包补全 create_agent_task schema / llm_utils_chat body / batch_get_detail_param 响应） | ⏳ 部分完成 | 已实测：llm_utils_chat 需 `function` 字段；create_agent_task 绑定字段与模型解析规则已还原（见 §5.3 注）；会话历史体系（missing history）仍待打通 |
| Phase 1（登录 + 目录 + llm_utils_chat 对话） | ✅ 已编码 | `app/auth/trae/` 五件套 + `trae_auth` 表 + `TraeProvider` + `trae_auth` 路由 + 前端 ModelsPanel + 测试通过；llm_utils_chat 走 **IDE 额度**（`ide_credits`），用户额度为 0 时返回 4008 quota（显式报错，不再静默）；额度池余量已透出（§额度共享） |
| Phase 2（create_agent_task 编排模式） | ⏳ 待做 | 依赖 Phase 0 补齐会话历史体系（见 §5.3 注）；work 额度 792 充足（§额度共享） |
| 服务端 0.1.56 体系复核（2026-08-26） | ⏳ 部分完成 | model_name 需用目录档位名（`__dev` 后缀，纯名报 "model config is empty"）；新会话卡 summary 模板、真实会话卡 missing history；`sync_history_state` 实测为"空操作"（query_history_state 的 missing 不变）；客户端端点疑似迁移到 `/api/cue_agent/v3/create_agent_task` / `/api/ide/v2/llm_raw_chat`（tt_compress 配置），schema 待抓包对齐 |
| Phase 3（增强） | ⏳ 待做 | 补全/额度展示等 |

---

## 0. 摘要

把 TRAE SOLO CN 登录后的自带模型，作为一个新的模型供应商类型 **`trae`** 接入当前项目的供应商体系（同 ta3 / workbuddy 模式）。接入后：

- 用户在当前项目内完成 TRAE 账号登录（复刻 TRAE SOLO CN 的浏览器授权 + PKCE(S256) + AuthCode 换 token + ECDSA-P256 设备签名刷新）；
- 自动拉取 TRAE 下发的模型目录（`batch_get_detail_param` 内置模型 + `providers` 第三方 BYOK 预设），同步为当前项目 Provider/Model 记录；
- 聊天请求由新增的 `TraeProvider` 发出，复刻 TRAE ai_agent 的请求头（Cloud-IDE-JWT + 设备指纹头）与请求体结构，模拟 TRAE SOLO CN 运行环境；
- **与 ta3/workbuddy 的本质差异**：TRAE SOLO CN 没有暴露纯 OpenAI 兼容的 LLM 端点，内置模型对话走"云端编排"接口（`create_agent_task` SSE + `commit_toolcall_result` 工具回调）。因此接入分两阶段：先打通 **`llm_utils_chat`**（一次性 LLM 调用，结构最接近标准对话，作为 MVP），再实现 **`create_agent_task`** 完整编排流（工具调用映射到当前项目本地工具执行）；
- **上下文管理、压缩、系统规则、审批、编排仍完全沿用当前项目**（`agent_loop` / `context_manager` / `compaction` 不动；create_agent_task 模式下仅复用其工具执行层）。

> 重要前置说明：TRAE SOLO CN 的核心 AI 逻辑在 `modules/ai-agent/ai_agent.dll`（Rust 二进制，264MB），JS 侧只有登录与配置代码。本方案的事实清单来自：`out/main.js`（反混淆还原的 OAuth 全链路）、`product.json`（域名/ClientID）、`out/vs/workbench/workbench.desktop.main.solo-lite.js`、以及本机运行日志（`%APPDATA%\TRAE SOLO CN\logs\*\Modular\ai-agent_*.log`，含全部业务请求 URL/请求体/请求头）。**`create_agent_task` 的完整事件 schema 需按 §7 的抓包步骤补全后再实施 Phase 2**。

---

## 1. 参考项目链路逆向分析（事实清单）

> 证据来源标注：`[main.js]` = `D:\aiTools\TRAE SOLO CN\resources\app\out\main.js`（打包后单行，以下用符号名引用）；`[product.json]` = resources/app/product.json；`[log]` = 本机 ai-agent 运行日志。

### 1.1 应用形态与域名拓扑

**形态**：Electron 壳（Chromium 定制 + aha_net/TTNet 网络栈）+ VS Code fork（icube）+ 本地 Rust 进程 `ai_agent`（`modules/ai-agent/start.bat` 启动，RPC socket 端口 `40005`，见 `meta.json`）。AI 对话 UI 部分远程加载（`api.trae.com.cn`），LLM/业务请求由 ai_agent 进程发出（`[aha_net] send` 日志）。

**域名**（`[product.json] bootConfig`）：

| 用途 | 值 |
|---|---|
| 账号/认证 API（apiHost） | `https://api.trae.cn`（bootConfig.account.trae.normal） |
| 登录页（consoleHost） | `https://www.trae.cn`（bootConfig.consoleHost） |
| iCube IDE 前端 | `https://api.trae.com.cn` |
| **Agent/LLM API 主机** | `https://trae-api-cn.mchost.guru`（CDN 回源 `api5-normal.mchost.guru`） |
| 自定义模型 WS 隧道 | `wss://trae-ws-cn.mchost.guru/custom_model` |
| 消息推送长连接 | `wss://frontier.zijieapi.com/ws/v2` |

**应用标识**（`[product.json]`）：`nameShort` = "TRAE SOLO CN"，`nameAlias` = "TraeWork CN"，`packageType` = "SOLO_CN"，`appVersion` = 0.1.51，`quality` = stable。

**OAuth ClientID**（`[product.json] iCubeApp.authConfig`，SOLO 系列 stable 渠道）：

| 渠道 | ClientID |
|---|---|
| **SOLO stable（本目标）** | `en1oxy7wnw8j9n` |
| SOLO beta | `nn572p8wnw1vd7` |
| TRAE IDE stable | `ono9krqynydwx5` |

### 1.2 登录流程（PKCE-S256 + AuthCode 换 Token）

`[main.js]` `oauth/userLogin/loginUrlBuilder.js`（类 `aie`）+ `oauth/oauthLocalServer.js` + `oauth/marscode/login.js`（`TraeLoginService`）：

1. **本地回调 server**：监听 `127.0.0.1` 随机端口，路径常量 `LOGIN_CONFIRM="/login-confirm"`、`LOGIN_SUCCESS="/login-success"`、`AUTHORIZE="/authorize"`；CORS 放行 `x-jwt-token` 头（`oauthLocalServer.js`）。
2. **PKCE**：`codeVerifier` = base64url(48 字节随机)，`codeChallenge` = base64url(SHA256(verifier))，method = **`S256`**（`auth/common/util.js` `W7e`）。
3. **打开登录页**（`loginUrlBuilder.js`，SOLO Lite 产品 `auth_from=solo`）：

   ```
   https://www.trae.cn/authorization
     ?login_version=1&auth_from=solo&login_channel=native_ide
     &plugin_version={tronBuildVersion}&auth_type=local
     &client_id={ClientID}              # en1oxy7wnw8j9n
     &redirect=0&login_trace_id={uuid}
     &auth_callback_url=http://127.0.0.1:{port}/authorize
     &machine_id={machineId}&device_id={deviceId}
     &x_device_id={deviceId}&x_machine_id={machineId}
     &x_device_brand={deviceModel}&x_device_type={osName}
     &x_os_version={osVersion}&x_app_version={appVersion}
     &x_app_type={stable}
     &code_challenge={challenge}&code_challenge_method=S256
     &hide_saas_login=true              # solo 专属
   ```

4. **登录页回调**：浏览器完成登录后跳转 `http://127.0.0.1:{port}/authorize?authCodeInfo=...`（JSON，含 `AuthCode`、`userTag`；`TraeLoginService` 解析）。
5. **生成设备密钥对**：EC **P-256**（`generateKeyPairSync("ec",{namedCurve:"P-256"})`，`auth/common/util.js` `V7e`），公钥 PEM 随 DeviceInfo 提交，私钥本地留存用于刷新签名。
6. **换 Token**：`POST https://api.trae.cn/trae/api/v3/oauth/ExchangeToken`，JSON body（`exchangeTokenByAuthCode`）：

   ```json
   {
     "ClientID": "en1oxy7wnw8j9n",
     "AuthCode": "...",
     "CodeVerifier": "...",
     "DeviceInfo": {
       "DeviceID": "...", "MachineID": "...",
       "PlatformCode": "SOLO_PC", "DeviceType": "PC",
       "DeviceName": "...", "DeviceModel": "...", "DeviceBrand": "...", "DeviceCPU": "...",
       "ClientVersion": "0.1.51",
       "DevicePublicKey": "-----BEGIN PUBLIC KEY-----...",
       "OSInfo": "...", "OSVersion": "..."
     },
     "IDEVersion": "0.1.51"
   }
   ```

7. **响应**（外层 `{ResponseMetadata, Result}`，`auth/marscode/util.js` `X9e` 组装 + `exchangeToken` 处理）：

   ```json
   { "Result": {
       "Token": "eyJh...",            // JWT 访问令牌
       "RefreshToken": "...",
       "TokenExpireAt": 1234567890,    // 或 TokenExpireDuration（秒）
       "RefreshExpireAt": "...",
       "UserID": "3704...", "ScreenName": "...",
       "AIRegion": "cn", "StoreCountry": "CN"
   } }
   ```

   错误码在 `ResponseMetadata.Error.Code`；`20324/20101/20315/20125/20126/20401/20403` 视为登录态失效（`20401` 设备数超限，`request.js` 常量 `Wx`）。

8. **用户信息**：`POST https://api.trae.cn/cloudide/api/v3/trae/GetUserInfo`（用换到的 Token），SOLO 会话内的 UserInfo 形态（`[log]`）：`{name, token(JWT), region:"cn", user_id, scope:"marscode", login_scope:"Trae"}`。

### 1.3 Token 刷新与校验（ECDSA 设备签名）

**刷新**（`exchangeTokenByRefreshToken`）：`POST https://api.trae.cn/trae/api/v3/oauth/ExchangeToken`，body：

```json
{
  "ClientID": "en1oxy7wnw8j9n",
  "ClientSecret": "",
  "RefreshToken": "...",
  "DeviceInfo": { ...含 DevicePublicKey... },
  "DeviceProof": { "Signature": "...", "Timestamp": 1700000000, "Nonce": "32hex" },
  "IDEVersion": "0.1.51"
}
```

**DeviceProof 签名算法**（`auth/common/util.js` `z7e`，Python 复刻见 §5.1）：

```
payload = "\n".join([method, path, clientId, refreshToken, str(timestamp), nonce])
# 即 "POST\n/trae/api/v3/oauth/ExchangeToken\n{clientId}\n{refreshToken}\n{ts}\n{nonce}"
signature = base64( ECDSA-SHA256_sign(payload, devicePrivateKeyP256) )
timestamp = int(time.time()); nonce = secrets.token_hex(16)
```

**有效性校验**：`POST https://api.trae.cn/cloudide/api/v3/trae/CheckLogin`，body `{IDEVersion, ReqSource:"Lite", GetAIPayHost:true}`，响应 `Result.IsLogin`；失效码同上（含 `MigrateToSG` 字段可忽略）。

**登出**：`POST /trae/api/v3/trae/oauth/ClearRefreshToken`。

### 1.4 业务接口鉴权契约

**JS 侧（Electron 主进程注入，`[main.js]` `R3e` marketplace-headers / solo-lite-websocket-headers）**：

```
Authorization: Cloud-IDE-JWT {token}
X-User-Region: {AIRegion}          # "cn"
```

适用 `https://*/trae/*`、`wss://*/explorer/*` 等市场/工作台请求（GetUserInfo / ExchangeToken / CheckLogin 三个路径被排除，它们自带认证参数）。

**ai_agent 侧（LLM/Agent 业务请求，`[log]` `[HTTPClient] add_header` 完整清单）**：

```
request-traffic-type: prod
x-app-id: 6eefa01c-1036-4c7e-9ca5-d891f63bfcd8     # 固定
x-app-version-code: 20260806                        # 随构建
x-app-version: default
x-custom-trace-id: {32hex}
x-device-id: {deviceId}                             # 设备注册 ID（数字串）
x-device-brand / x-device-cpu: {硬件信息}
x-device-type: windows
x-machine-id: {64hex}                               # 机器指纹哈希
x-ide-version: 0.1.51
x-ide-version-type: stable
x-os-version: Windows 11 Home China
X-Request-ID / X-Trae-Request-ID: {uuid}            # 流式请求
```

> Authorization 头由 ai_agent 从主进程传入的 UserInfo.token 注入（格式同为 Cloud-IDE-JWT 或 Bearer，日志脱敏未打印；**实施 Phase 0 时按 §7 抓包确认**，两种格式都做可配置）。

### 1.5 模型列表获取

**A. 内置模型（登录后免费/订阅额度可用）**：`POST https://trae-api-cn.mchost.guru/api/ide/v1/batch_get_detail_param`（`[log]` `model.model_list` RPC -> `BatchDetailParamRequest`）：

```json
{
  "functions": ["assistant", "solo_agent_lite", "solo_coder", "solo_agent_remote",
                 "solo_work_lite", "solo_work_remote", "solo_design_lite",
                 "solo_design_remote", "builder"],
  "agent_type": "",
  "current_config_info": { "config_name": "", "is_custom_model": false },
  "mode_type": "Manual",
  "access_type": "SoloLite",
  "ab_force_vids": "",
  "ab_autotest_advanced_mode": 0,
  "show_custom_model": true
}
```

响应为按 function 分组的模型列表；模型标识（config_name）格式 **`{provider}//{model}`**，如 `volcengine//doubao-seed-evolving`、`MiniMax-cn//MiniMax-M3`；模型附带 `model_extra_config`（v2/v3 工具开关、压缩参数、max_mode 等，见 `[log]` ModelMgr 解析）与 token 参数（`prompt_max_tokens` 如 524288、`max_tokens` 131072、`multimodal`）。

> 响应完整 JSON schema 未在日志中打印（Rust 端反序列化）。Phase 0 用真实 token 直接调该接口落盘响应样本，补全字段映射（§7）。

**B. 第三方 BYOK 供应商预设**：`POST /api/ide/v1/providers`（body `{}`）-> `Provider[]`（`[log]` 完整样本已捕获）：

```
Provider { id: "deepseek", name: "DeepSeek", models: ["deepseek-v4-pro", ...],
           base_url: "https://api.deepseek.com", api_key_doc, model_list_doc,
           model_detail: [{model_name, display_name}], billing_mode: "paygo",
           provider_icon, client_connect: false, provider_offline_status }
```

含 deepseek / volcengine(-plan/-agent-plan) / bigmodel(-plan) / zai(-plan) / aliyuncs(-plan) / Kimi-CN/Global / MiniMax-cn/global / gitee / siliconflow / openrouter / gemini / xiaomi-mimo / infinigence-ai / anthropic 等。**这些是 BYOK 目录（用户自己的 key），不属于"TRAE 自带模型"，接入时仅作参考不同步**。

**C. 自定义模型（custom_openai_compatible）**：TRAE 支持添加任意 OpenAI 兼容端点：
- `GET /api/ide/v1/get_custom_model_type_config?provider_id=custom_openai_compatible&end_point={encoded_url}` -> `{custom_model_hyper_params_list: [{model_name, max_tokens, prompt_max_tokens, multimodal}], custom_model_type: "gpt5"|"..."}`（`[log]` 完整样本）
- `POST /api/ide/v1/add_custom_model` / `update_custom_model` / `del_custom_model`
- `POST /api/agent/v3/custom_model_connectivity_check`（连通性检查）
- WS 隧道 `wss://trae-ws-cn.mchost.guru/custom_model/tunnel/{uuid}`（客户端中转）

> 用户在 TRAE 中配置的自定义模型（如曾指向 `http://localhost:54756/v1/chat/completions`）走本地直连，不经 TRAE 服务端 -- 与本方案无关。

### 1.6 模型调用（核心，与 ta3/workbuddy 的本质差异）

TRAE SOLO Lite 的对话是 **云端编排（cloud agent）模式**：服务端持有 agent 状态与工具调度，客户端执行工具并回传。`[log]` 确认的全部 LLM 相关端点（均 POST、SSE 流式 fetch_stream_v2）：

| 端点（`https://trae-api-cn.mchost.guru` 前缀） | 用途 |
|---|---|
| `/api/agent/v3/create_agent_task` | **主对话**：创建云端 agent 任务，SSE 返回编排事件流（文本增量 / 工具调用请求 / 状态） |
| `/api/agent/v3/commit_toolcall_result` | **工具回调**：本地执行完工具后提交结果，服务端继续编排（继续 SSE） |
| `/api/agent/v3/interrupt` | 中断当前任务 |
| `/api/agent/v3/workflow/start` | 工作流任务 |
| `/api/agent/v3/llm_utils_chat` | **一次性 LLM 调用**（会话标题/图标生成等 utility），body≈4KB，含完整 client_info/user_info 结构 |
| `/api/agent/v3/query_history_state` / `sync_history_state` | 历史状态同步 |
| `/api/ide/v1/super_completion_query` | 代码补全（inline completion） |
| `/api/ide/v1/cancel_queue_task` | 取消排队任务 |
| `/api/solo_hub/v1/*` | 远程会话 hub（conversations / clis / wsmessages / artifact 上传） |

**start_chat 入参**（本地 RPC -> ai_agent 组装 create_agent_task，`[log]` 完整结构）：

```json
{
  "session_id": "6a7b...", "message_id": "6a7b...",
  "message_content": [],
  "model_name": "custom_openai_compatible//gpt-5.6-luna",   // 或内置 config_name "volcengine//doubao-seed-evolving"
  "agent_type": "solo_agent_lite", "agent_id": "solo_agent_lite",
  "workspace_folders": ["d:\\..."],
  "scene_location": 2,
  "model_auto_selection": { "strategy": "manual" },
  "custom_model": { "provider": "...", "config_name": "...", "ak": "<加密>", "base_url": "...",
                    "use_remote_service": false, "multimodal": true,
                    "prompt_max_tokens": 524288, "max_tokens": 131072, "custom_model_type": "gpt5" },
  "query": "[{\"type\":\"text\",\"data\":{\"content\":\"...\"}}]",
  "client_info": { "project_id": "...", "workspace_folders": [...], "connect_session_id": "...",
                   "agent_task_service_strategy": "cloud_agent", ... },
  "user_info": { "name": "...", "token": "<JWT>", "region": "cn", "user_id": "...", "scope": "marscode" },
  "streamlined_common_params": { "biz_user_id": "...", "device_id": "...", "machine_id": "...",
                                  "region": "CN", "aiRegion": "CN", "quality": "stable",
                                  "app_version": "0.1.51", "product_code": "SOLO_Lite", ... }
}
```

**SSE 事件 schema 未从静态代码还原**（在 ai_agent.dll 内部）。`[log]` 可见事件类型线索：`ModelConfig`（服务端下发模型信息）、文本增量、toolcall 请求、`timing_events_*`。**Phase 2 实施前必须按 §7 抓包补全**。

### 1.7 请求时序总览

```
[登录]  本地 127.0.0.1:{port} 回调 server
        打开 https://www.trae.cn/authorization?...&client_id=en1oxy7wnw8j9n
             &code_challenge={S256}&auth_callback_url=http://127.0.0.1:{port}/authorize
        ──▶ 回调 /authorize?authCodeInfo={AuthCode,...}
        ──▶ POST api.trae.cn/trae/api/v3/oauth/ExchangeToken  (AuthCode+CodeVerifier+DeviceInfo)
        ──▶ Result.Token(JWT) + RefreshToken
        ──▶ POST api.trae.cn/cloudide/api/v3/trae/GetUserInfo (Token)

[刷新]  POST api.trae.cn/trae/api/v3/oauth/ExchangeToken
        (RefreshToken + DeviceProof{ECDSA-P256-SHA256 签名})  ──▶ 新 Token/RefreshToken

[目录]  POST trae-api-cn.mchost.guru/api/ide/v1/batch_get_detail_param
        (Cloud-IDE-JWT + 设备头)  ──▶ 各 function 模型列表（provider//model）

[对话]  POST trae-api-cn.mchost.guru/api/agent/v3/create_agent_task
        (SSE) ──▶ 文本增量/工具调用事件
        工具事件 ──▶ 本地执行 ──▶ POST /api/agent/v3/commit_toolcall_result (SSE 继续)
        中断:   POST /api/agent/v3/interrupt
```

---

## 2. 当前项目接入点分析

### 2.1 供应商数据模型

`server/app/persistence/models/model_reg.py`：`Provider`（name/base_url/api_key/**api_format**/is_active/auth_status/account_label）、`Model`（provider_id/name/base_url/api_key/api_format/context_window/reasoning_efforts/is_multimodal + per-vendor meta JSON 列）。workbuddy 已打通"token 动态注入"模式（Model.api_key = 占位符 `__workbuddy_session__`）。

### 2.2 Provider 抽象与路由

- `server/app/models/base.py:13`：`ModelProvider(ABC)`，`chat` / `stream` / `stream_structured`（agent_loop 主路径）。
- `server/app/models/registry.py`：`_build_provider` 按 `api_format` 分派（openai/anthropic/ta3/workbuddy）；`_build_workbuddy_provider`（registry.py:42-92）是 **TRAE 应完全对齐的样板**：auth 表实时取 token + 401 刷新回调注入 + meta 注入。
- `app/auth/<vendor>/` 三件套样板：`oauth.py`（浏览器登录 + 后台轮询）、`session.py`（auth 表读写/刷新锁）、`catalog.py`（目录同步 upsert Model 表）。
- `app/gateway/routers/workbuddy_auth.py`：`login/start | login/cancel | login/status | logout | sync` 五端点契约。

### 2.3 聊天链路（不动）

`agent_loop` -> `provider.stream_structured(request)` -> 工具执行（`orchestration/tools/registry.py`）-> 循环。TRAE 的 `llm_utils_chat` 模式可无缝套用；`create_agent_task` 模式仅复用工具执行层（编排权在 TRAE 云端，见 §5.4）。

### 2.4 前端

- `client/src/components/settings/ModelsPanel.tsx`：api_format 下拉（openai/anthropic/ta3/workbuddy），需加 "trae"。
- `client/src/components/chat/ModelPicker.tsx`：模型分组展示。
- workbuddy 登录 UI 已有"浏览器登录 + 轮询状态"交互，TRAE 直接复用同一交互模式。

### 2.5 持久化样板

`server/app/persistence/models/ta3_auth.py`（Ta3Auth 单行表：access_token/refresh_token/account/catalog JSON）。workbuddy 复用同构表。TRAE 需要新表存**设备密钥对**（刷新签名依赖），见 §4.3。

---

## 3. 总体架构

```
┌─────────────────────────── chatcoder（当前项目）────────────────────────────┐
│  前端 ModelsPanel/ModelPicker            后端 FastAPI                        │
│  ├─ "TRAE SOLO" 供应商类型              ├─ app/auth/trae/          ★新增     │
│  │   └─ 登录按钮 -> 系统浏览器打开        │   ├─ device.py (EC P-256 密钥对)  │
│  │       授权页(轮询状态)                │   ├─ pkce.py  (S256 PKCE)         │
│  └─ 模型列表展示 trae 分组               │   ├─ oauth.py (授权/ExchangeToken) │
│                                          │   ├─ session.py(存储/刷新/校验)   │
│                                          │   └─ catalog.py(目录同步)         │
│                                          ├─ app/models/providers/trae.py ★  │
│                                          │   └─ TraeProvider                 │
│                                          │       ├─ Cloud-IDE-JWT + 设备头    │
│                                          │       ├─ Phase1: llm_utils_chat   │
│                                          │       └─ Phase2: agent_task 编排  │
│                                          ├─ agent_loop / compaction  不动    │
│                                          └─ routers/trae_auth.py     ★新增  │
└──────────────────────────────────────────────────────────────────────────┘
        │ 登录/目录(JWT)                        │ LLM/编排请求(JWT+设备头)
        ▼                                       ▼
 https://api.trae.cn / www.trae.cn        https://trae-api-cn.mchost.guru
 /trae/api/v3/oauth/ExchangeToken          /api/agent/v3/create_agent_task(SSE)
 /cloudide/api/v3/trae/GetUserInfo         /api/agent/v3/commit_toolcall_result
                                          /api/agent/v3/llm_utils_chat
                                          /api/ide/v1/batch_get_detail_param
```

**职责边界**：
- trae 侧负责：登录、token 刷新（设备签名）、模型目录同步、LLM/编排请求收发与协议适配
- 当前项目负责：上下文组装、压缩、系统规则、工具执行、审批、消息流（Phase 2 中工具执行仍在本项目，编排权移交 TRAE 云端，需在 UI 上明示差异）

---

## 4. 数据模型变更

### 4.1 Provider 表

1. `api_format` 扩展枚举值 **`trae`**（前端下拉加 "TRAE SOLO（字节）"）。
2. 字段语义：
   - `base_url` = Agent API 主机 `https://trae-api-cn.mchost.guru`（默认值由后端注入，用户可改）
   - `api_key` = 不用（登录态独立存储）
   - 复用 `auth_status` / `account_label`
3. 扩展 Provider 级配置（存 `workbuddy_meta` 同风格的 JSON 列或新增 `trae_meta`）：`account_api_base`（默认 https://api.trae.cn）、`console_host`（默认 https://www.trae.cn）、`client_id`（默认 en1oxy7wnw8j9n）、`ide_version`（默认 0.1.51）、`app_id`（默认 6eefa01c-...）——全部可配置以应对版本升级。

### 4.2 Model 表（trae 供应商下的模型）

- `name` = 模型请求名（`provider//model` 去掉前缀后的 model 部分，如 `doubao-seed-evolving`；保留完整 config_name 进 meta）
- `api_key` = 占位符 `"__trae_session__"`（对齐 workbuddy 模式）
- `base_url` = Agent API 主机
- `context_window` = prompt_max_tokens；`max output` / multimodal / reasoning 进 `trae_meta`：

```json
{ "config_name": "volcengine//doubao-seed-evolving",
  "title": "Doubao-Seed-Evolving", "functions": ["solo_agent_lite"],
  "prompt_max_tokens": 524288, "max_tokens": 131072, "multimodal": true,
  "model_extra_config": { ...原样缓存，工具/压缩开关... } }
```

### 4.3 trae 登录态存储

新表 `trae_auth`（对齐 Ta3Auth 结构 + 设备密钥）：

```python
class TraeAuth(Base):
    __tablename__ = "trae_auth"
    id / provider_id
    access_token: str | None        # JWT
    refresh_token: str | None
    token_expires_at: str | None    # ISO
    refresh_expires_at: str | None
    device_private_key: str | None  # EC P-256 私钥 PEM（刷新签名必需）
    device_public_key: str | None
    device_id: str | None           # 设备注册 ID（数字串）
    machine_id: str | None          # 64hex 机器指纹（首登录生成后固化）
    account: JSON                   # {user_id, name, region, aiRegion, store_country}
    catalog: JSON | None            # 最近目录原文
    updated_at: str
```

> 风险控制：machine_id/device_id 每次登录生成一次后必须固化复用（服务端有设备数限制，错误码 20401），不可每次请求随机。

### 4.4 工具伪装映射

Phase 2 需要：TRAE 工具名（`Read`/`Write`/`Edit`/`RunCommand`/`SearchCodebase`/`Grep`/`LS`/`Glob`/`WebSearch`/`WebFetch`/`TodoWrite`/`MultiEdit`/`SearchReplace`/`GetDiagnostics`/`OpenPreview`/`CheckCommandStatus`/`DeleteFile`，来自 `[log]` micro_compact_config.supported_tools）-> 当前项目工具（`fs_read`/`fs_write`/...）。常量表 `app/models/providers/trae_tool_aliases.py`（对齐 ta3_tool_aliases.py 模式）。

---

## 5. 详细设计

### 5.1 认证模块 `app/auth/trae/`

```
app/auth/trae/
├── __init__.py
├── device.py     # gen_device_keypair() -> P-256 PEM 对；sign_device_proof(method, path, clientId, refreshToken, key) -> {Signature, Timestamp, Nonce}
├── pkce.py       # gen_code_verifier() 48B base64url + s256_challenge()
├── callback.py   # 本地回调 server（127.0.0.1 随机端口，/authorize 路径，90s 超时；对齐 ta3 callback.py 骨架）
├── oauth.py      # start_login / exchange_auth_code / refresh_token / check_login / logout
├── session.py    # load_auth / save_auth / ensure_token(提前刷新) / refresh_session(带锁)
└── catalog.py    # sync_trae_models()
```

**登录序列**（oauth.py，复刻 §1.2）：

1. `device = gen_device_keypair()`；`machine_id = sha256(本机稳定特征)`（固化到 trae_auth）；`device_id` 首次登录用 0 兜底，从 GetUserInfo/CheckLogin 响应回填。
2. 起 `/authorize` 回调 server；构造授权 URL（§1.2 第 3 步完整参数）；返回给前端打开系统浏览器。
3. 回调收到 `authCodeInfo`（JSON：AuthCode + userTag）-> `exchange_auth_code`：POST ExchangeToken（§1.2 第 6 步 body）-> 存 TraeAuth。
4. GetUserInfo 补全 account。

**刷新**（session.py `refresh_session`，对齐 workbuddy 刷新锁）：

```python
proof = sign_device_proof("POST", "/trae/api/v3/oauth/ExchangeToken",
                          client_id, refresh_token, device_private_key)
body = {"ClientID": client_id, "ClientSecret": "",
        "RefreshToken": refresh_token, "DeviceInfo": device_info(pub),
        "DeviceProof": proof, "IDEVersion": ide_version}
# 响应处理同 §1.3；错误码命中 {20324,20101,20315,20125,20126,20401,20403} -> 清会话抛 login_required
```

**Python 签名复刻**（cryptography 库）：

```python
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

def sign_device_proof(method, path, client_id, refresh_token, private_key_pem, ) -> dict:
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    ts = int(time.time()); nonce = secrets.token_hex(16)
    payload = "\n".join([method, path, client_id, refresh_token, str(ts), nonce]).encode()
    sig = key.sign(payload, ec.ECDSA(hashes.SHA256()))   # Node crypto.sign("sha256") 等价（DER 编码，服务端兼容）
    return {"Signature": base64.b64encode(sig).decode(), "Timestamp": ts, "Nonce": nonce}
```

> 注意：Node `crypto.sign("sha256", data, ecKey)` 输出 DER 签名；Python cryptography 默认也是 DER，一致。若服务端要求 raw (r||s)，Phase 0 验证时切换 `decode_dss_signature` 拼接。

**ensure_token 策略**：距 `token_expires_at` < 5 分钟即预刷新；401 兜底强制刷新一次重试（对齐 WorkBuddyProvider `_try_refresh_token`）。

### 5.2 目录同步 `app/auth/trae/catalog.py`

```
sync_trae_models(db, provider):
  1. token = ensure_token(...)
  2. POST {agent_host}/api/ide/v1/batch_get_detail_param   # §1.5 A 请求体
     头：Cloud-IDE-JWT + 设备头（§5.3 _base_headers 同源）
     401 -> refresh 一次重试（对齐 workbuddy catalog）
  3. 解析响应（Phase 0 落盘样本后定 schema；预期按 functions 分组）
     - 过滤：仅保留 functions 含 solo_agent_lite/builder 的条目（对话模型）
     - upsert Model 表（§4.2 字段映射），api_format="trae"
  4. 同时可选拉 POST /api/ide/v1/providers 缓存进 trae_auth.catalog（BYOK 参考，不入 Model 表）
```

### 5.3 TraeProvider `app/models/providers/trae.py`

对齐 WorkBuddyProvider 骨架（httpx + 手动 SSE + monitor dict + 401 刷新重试），差异在端点与事件解析：

**公共层**：

```python
class TraeProvider(ModelProvider):
    name = "trae"
    def __init__(self, *, api_key, base_url, model, meta=None, refresh_token=None):
        # api_key = JWT（registry 从 trae_auth 实时取）；refresh_token 回调 401 时刷新

    def _base_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Cloud-IDE-JWT {self._token}",   # Phase 0 确认是否带 Bearer 变体
            "X-User-Region": meta["region"] or "cn",
            "request-traffic-type": "prod",
            "x-app-id": meta["app_id"],                        # 6eefa01c-...
            "x-app-version-code": meta["app_version_code"],
            "x-app-version": "default",
            "x-custom-trace-id": uuid4().hex,
            "x-device-id": meta["device_id"],
            "x-device-brand": meta["device_brand"],
            "x-device-cpu": meta["device_cpu"],
            "x-device-type": "windows",
            "x-machine-id": meta["machine_id"],
            "x-ide-version": meta["ide_version"],
            "x-ide-version-type": "stable",
            "x-os-version": meta["os_version"],
            "x-flow-traceparent": f"04-{trace_id}-{span}-01",
            "X-Request-ID": str(uuid4()),
            "User-Agent": settings.trae_user_agent,            # Electron 同族 UA
        }
```

**Phase 1 -- llm_utils_chat 模式（MVP，已实现 + 实测修正）**：

- 端点：`POST {base}/api/agent/v3/llm_utils_chat`
- **实测结论（2026-08-25，真实 token）**：
  - 请求体必须带 **`function: "chat"`**。缺该字段时服务端返回 `event:error {code:2001, "resolveByUsage function is empty"}` 后直接 done —— **表现为"发送后无回复无报错"**（已修复：`_build_body` 固定注入 `function: "chat"`）。
  - **额度池**：llm_utils_chat 消费 **IDE 额度**（`notify_usage` 事件的 `ide_credits`）。当前账号 `ide_credits=0` → 返回 `code 4008 "Your requests have exceeded the quota."`。TRAE 客户端主对话走 `create_agent_task`（消费 `work_credits`，当前账号 792 充足）—— **所以 Phase 2 才是 TRAE 对话的正路**。
  - `_parse_frame` 已识别 `{code, message}` 业务错误事件并显式抛 `RuntimeError`（修复前静默产出空回复）。

**Phase 2 -- create_agent_task 编排模式（完整能力，实测部分还原）**：

- 端点：`POST {base}/api/agent/v3/create_agent_task`，SSE。
- **实测已还原的必填字段**（Go 反序列化 binding 报错逐个对齐）：
  - 顶层：`conversation_id` / `user_id` / `device_id` / `config_name`（纯模型名）/ `ide_version`（"0.1.51"）/ `user_input`（对象 `{id, type:"text", data:{content}}`，不可传字符串）/ `session_id` / `message_id` / `agent_type`("solo_agent_lite") / `agent_id` / `model_name` / `model_auto_selection` / `query`（消息块 JSON 串）/ `client_info` / `user_info` / `streamlined_common_params`
  - **模型解析规则（2026-08-26 实测修正，服务端 0.1.56 体系）**：
    - **全新会话**（`session_id` 为新 UUID）：`model_name` 必须用目录下发的**档位名**（`provider_model_name`，如 `DeepSeek-V4-Flash-Official__dev`）。纯配置名（`DeepSeek-V4-Flash-Official`）或带 provider 前缀均报 `model config is empty for model name: ...`（8-25 时 manual 纯名可过，8-26 起已失效）。
    - 档位名可过模型解析，但**新会话卡 summary 模板**：`failed to get summary config: failed to get summary template data`。实测与 mode_type/access_type/版本头（0.1.51/0.1.56、20260806-20260901）无关。
    - **真实会话**（客户端创建过的 `session_id`，如 `6a6cff5df62fdd1fcff466b0`）：模型解析 + summary 全过（任意 model_name 均可），但卡 `4000105 missing history count exceeded for session {id}`——服务端要求会话历史已在云端完整。
    - **`sync_history_state` 是"空操作"**：任意 body（含 `message_content`/`history_id_list`/`history_count`）均返回 `History state synced successfully`，但 `query_history_state` 返回的 `missing` 列表不变——无法把历史写入云端。会话历史只能由 TRAE 客户端建立。
    - `message_content` 为空 → `model config is empty`（8-25 结论，8-26 仍适用）。
  - **端点迁移迹象**：`%APPDATA%\TRAE SOLO CN\ahanet\server.json` 的 `tt_compress.equal_path` 含 `/api/cue_agent/v3/create_agent_task` 与 `/api/ide/v2/llm_raw_chat`（客户端 0.1.56 实际压缩路径），`/api/agent/v3/create_agent_task` 可能已非主通道。cue_agent / llm_raw_chat 对当前请求体报 `param is invalid`（schema 未对齐），**需抓包对齐后才可切换**。
  - **剩余阻塞**：会话历史体系（missing history 的解法 = 复刻客户端会话创建/历史同步机制，Phase 0 抓包）。
- TraeProvider 不再走 agent_loop 的模型循环，而是实现为**编排适配器**：
  - `stream_structured` 每次调用 = 创建一个 agent task（用户消息 -> TRAE 云端）
  - SSE 事件流翻译为当前项目事件：文本增量 -> `{"type":"content"}`；思考 -> `{"type":"thinking"}`；TRAE 工具调用请求 -> 按 §4.4 映射为当前项目工具 -> **挂起 SSE，本地执行工具** -> `POST commit_toolcall_result` -> 继续 SSE
  - 中断 -> `POST /api/agent/v3/interrupt`
- 该模式下当前项目的 system prompt/压缩不生效（TRAE 云端持有编排权），仅工具执行、审批、消息流沿用 -- 与 ta3/workbuddy 模式有本质差异，需在模型 meta 标记 `orchestrated: true`，前端模型选择器可加"云端编排"角标。
- **依赖 Phase 0 补全**：会话历史体系（missing history 的解法）。

**超时**：首 chunk 30s / 流 idle 30s（对齐 workbuddy）。

### 5.3a 额度共享（IDE / Work 双池，实测事实）

TRAE 账号同时持有两个额度池（`notify_usage` 事件的 `cn_credits_remain_info`）：

| 池 | 当前余量（2026-08-25 实测） | 消费场景 |
|---|---|---|
| `ide_credits` | **0（已用尽）** | `llm_utils_chat`（utility 调用：标题/图标等）—— chatcoder 当前对话走此池 |
| `work_credits` | **792.138** | `create_agent_task`（TRAE 客户端主对话，云端编排） |

**接入状态**：chatcoder 当前经 `llm_utils_chat`（ide 池）对话——账号 ide 额度已尽 → 返回 4008 `"Your requests have exceeded the quota."`（TraeProvider 已把池余量拼进错误信息，如 `（额度池: ide_credits=0, work_credits=792.138）`）。**work 池只能经 create_agent_task 消费**（Phase 2），打通后 TRAE 对话即走 work 额度。

**max 档位与积分倍率**（2026-08-25 实测，目录 `config_info_list` 字段）：

- `max_mode`（`display_config.max_mode`）：模型支持 max 档（如 DeepSeek-V4-Flash-Official / glm-5.3 / kimi-k3 / qwen3.8-max / qwen-3.7-plus / minimax-m3）
- `context_window_tokens.max`：max 档上下文（如 **1000000 = 1M**）；普通档上下文 = `prompt_max_tokens`（如 168000）
- `reasoning_effort_config.options`：思考档位原值（`light/high/extra_high`），归一化为 `low/high/xhigh`，max_mode 且支持思考时追加 `max` → 同步进 `Model.reasoning_efforts`（前端"思考: low/high/xhigh/max"）
- `display_contact_config.consumption_rate.data.rate`：积分消耗倍率（如 kimi-k3=1.65、qwen3.8-max=1.5、DeepSeek-V4-Flash=0.08）—— **max 档消耗更快即由倍率体现**，存 `trae_meta.consumption_rate`
- 上述均同步进 `trae_meta`（`context_window_max` / `thinking` / `consumption_rate` / `is_available`），`ModelOut` 增加 `trae_max_context` / `trae_consumption_rate` / `trae_available` / `trae_thinking` 字段供前端展示

**可用模型白名单**（用户 2026-08-25 提供，TRAE 客户端实际可用的 16 个）：`catalog._AVAILABLE_MODELS`。目录返回的 64 个 config 含大量工具/占位模型（title_generation / fast_apply / custom_model_* 等），前端模型选择器只展示白名单内模型（`trae_available=true`）。

### 5.4 工具回调映射（Phase 2）

TRAE 工具（`[log]` supported_tools 全集）-> 当前项目工具执行器：

| TRAE 工具 | 当前项目工具 |
|---|---|
| Read / read_file_range | fs_read |
| Write | fs_write |
| Edit / SearchReplace / MultiEdit | editor_apply_diff / multi_file_edit |
| RunCommand / CheckCommandStatus | terminal_exec |
| SearchCodebase / Grep / Glob / LS | codebase_search / fs_grep / fs_list |
| WebSearch / WebFetch | web_search / web_fetch |
| TodoWrite | todo_write |
| DeleteFile | fs_write（删除语义）或新增 |
| GetDiagnostics / OpenPreview | 暂不映射（提交不支持结果） |

映射表常量化（`trae_tool_aliases.py`）；执行走现有审批链（approval.py）。未映射工具收到请求时提交"不支持"结果并告警，不中断任务。

### 5.5 registry 接入

`registry.py` 对齐 workbuddy：

```python
if api_format == "trae":
    return await _build_trae_provider(db, model)   # 实时取 trae_auth token + 注入刷新回调 + meta(device_id/machine_id/...)
```

`_build_trae_provider` 与 `_build_workbuddy_provider`（registry.py:42）同构：auth 表取 token -> 401 -> `trae_session.refresh_session` -> meta 注入设备指纹字段。

### 5.6 API 路由 `app/gateway/routers/trae_auth.py`

对齐 workbuddy_auth.py 五端点：

```
POST /api/providers/{id}/trae/login/start    # 返回授权 URL（前端打开系统浏览器），后台等回调+换token
POST /api/providers/{id}/trae/login/cancel
GET  /api/providers/{id}/trae/login/status   # pending | logged_in | failed
POST /api/providers/{id}/trae/logout         # ClearRefreshToken + 清本地会话
POST /api/providers/{id}/trae/sync           # batch_get_detail_param -> upsert Model 表
```

`main.py` 挂载：`app.include_router(trae_auth.router, prefix="/api", tags=["trae"])`。

### 5.7 前端改造

- `ModelsPanel.tsx`：api_format 下拉加 `trae`（"TRAE SOLO（字节）"）；登录区复用 workbuddy 交互（打开浏览器 + 轮询 status + 显示 account_label）。
- `ModelPicker.tsx`：trae 分组展示；Phase 2 编排模型加"云端编排"标记（禁用本地 system prompt 相关提示）。
- Phase 2 中编排模式下前端工具审批流不变（工具请求事件照常进审批 UI）。

### 5.8 风控规避清单（实施验收项）

1. 设备指纹稳定：machine_id/device_id 固化存储，登录与请求复用同一值（防 20401 设备超限/风控）。
2. ClientID/版本头与真实客户端一致（en1oxy7wnw8j9n / 0.1.51 / stable / SOLO_PC），全部走配置可更新。
3. UA 使用 Electron 同族（对齐 workbuddy `_DEFAULT_WB_UA` 做法），可经 `TRAE_USER_AGENT` 覆盖。
4. 登录走真实浏览器授权页（系统浏览器，与 TRAE 客户端行为一致）。
5. trace 头齐全（x-custom-trace-id / x-flow-traceparent / X-Request-ID 每次随机）。
6. 刷新频率控制：预刷新仅提前 5 分钟；并发刷新加锁（对齐 wb_session）。

---

## 6. 实施计划

### Phase 0：抓包补全事实（前置，阻塞 Phase 1/2 细节）

用 mitmproxy 代理 TRAE SOLO CN（或复用本机日志）采集：

1. `llm_utils_chat` 完整请求体 + SSE 事件样本（标题生成即可触发）。
2. `create_agent_task` 完整请求体（内置模型一次对话）+ SSE 事件全量样本（含工具调用请求、usage、结束事件）。
3. `commit_toolcall_result` 请求体（执行一次会触发工具的对话）。
4. `batch_get_detail_param` 完整响应 JSON（模型 schema 定稿）。
5. 业务请求 Authorization 确切格式（Cloud-IDE-JWT 前缀 vs Bearer）。
6. 刷新签名 DER vs raw 验证（用真实私钥对比 Node 输出）。

### Phase 1：认证 + 目录 + llm_utils_chat 对话（MVP）

1. `app/auth/trae/` 全模块（device/pkce/callback/oauth/session）+ trae_auth 表 + 迁移。
2. `trae_auth.py` 路由 + registry `_build_trae_provider` + ModelsPanel/UI。
3. `catalog.py` 目录同步（batch_get_detail_param -> Model 表）。
4. `TraeProvider` llm_utils_chat 模式（thinking/content/usage；工具能力视抓包结果）。
5. 测试：pytest 覆盖签名算法/PKCE/刷新锁/目录解析（对齐 test_workbuddy_* 套件命名：test_trae_oauth / test_trae_catalog / test_trae_provider）。

### Phase 2：create_agent_task 编排模式（完整 agent 能力）

1. SSE 事件解析器 + 事件->当前项目 agent_events 映射。
2. 工具回调执行链（trae_tool_aliases + 审批 + commit_toolcall_result）。
3. interrupt 中断、错误恢复（断流重试策略）、usage 统计对齐。
4. 前端"云端编排"模式标记与交互适配。

### Phase 3：增强（可选）

- 快速模型（补全 super_completion_query）。
- 多账号设备管理（trai_auth 多行支持 + 设备列表查询）。
- 订阅/额度状态展示（ide_user_pay_status / ide_user_ent_usage 接口已有）。

### 验收标准

- 登录：系统浏览器完成 TRAE 授权，回调自动换 token，重启后免登录（刷新链路可用）。
- 目录：Model 表出现 trae 分组模型（含 context_window/multimodal 元数据）。
- 对话（Phase 1）：选定内置模型完成多轮中文对话，thinking/content 流式渲染，usage 正确计入。
- 对话（Phase 2）：触发工具调用 -> 本地审批执行 -> 结果回传 -> 模型继续输出；中断按钮生效。
- 稳定性：token 过期自动刷新（401 单次重试）；设备指纹跨会话稳定。

---

## 7. 风险与待确认事项

| # | 风险/待确认 | 影响 | 缓解 |
|---|---|---|---|
| 1 | `llm_utils_chat` 可能仅限 utility 场景（模型固定/无工具/长度限制），不适合通用对话 | Phase 1 价值受限 | Phase 0 首先验证；不适用则 Phase 1 直接以 create_agent_task 为 MVP（纯文本场景先跑通事件流） |
| 2 | `create_agent_task` SSE schema 未逆向（在 Rust DLL 内） | Phase 2 阻塞 | Phase 0 抓包；事件类型有日志线索（ModelConfig/timing_events）可交叉验证 |
| 3 | 设备指纹/签名校验严格（DeviceProof 算法细节、DER vs raw） | 刷新失败 | Phase 0 用 Node 与 Python 双端对拍签名；签名失败时错误码会明确提示 |
| 4 | 免费额度/频率限制（Free identity，`[log]` identity_str:"Free"） | 体验 | usage 接口展示额度；限流错误码透传 UI |
| 5 | 服务端版本演进（x-app-version-code 等头随版本变化） | 请求被拒 | 全部头字段走配置；trae_meta 支持热更新 |
| 6 | 编排模式下 system prompt/压缩失效（TRAE 云端持有） | 与其他供应商行为不一致 | UI 明示"云端编排"；Phase 2 验证服务端是否接受 prompt 注入字段（抓包确认） |
| 7 | 法律/ToS：模拟客户端调用非公开接口 | 账号风险 | 同 ta3/workbuddy 先例（内部工具用途）；控制请求频率，不批量并发 |
| 8 | 域名 `mchost.guru` CDN 调度（api5-normal 回源） | 偶发 5xx | httpx 重试 + 备用回源域名可配置 |

---

## 8. 涉及文件清单（改动汇总）

```
server/app/auth/trae/                    ★新增 整目录
  __init__.py / device.py / pkce.py / callback.py / oauth.py / session.py / catalog.py
server/app/models/providers/trae.py      ★新增（llm_utils_chat -> create_agent_task 两模式）
server/app/models/providers/trae_tool_aliases.py   ★新增 Phase 2
server/app/persistence/models/trae_auth.py        ★新增（含设备密钥字段）
server/app/persistence/migrations.py     修改（trae_auth 表）
server/app/models/registry.py            修改（api_format=="trae" 分派 + _build_trae_provider）
server/app/gateway/routers/trae_auth.py  ★新增（login/status/logout/sync 五端点）
server/app/main.py                       修改（挂载路由）
server/app/core/config.py                修改（trae_* 默认配置项）
server/tests/test_trae_*.py              ★新增（oauth/catalog/provider 三套件）
client/src/components/settings/ModelsPanel.tsx   修改（trae 类型 + 登录交互）
client/src/components/chat/ModelPicker.tsx       修改（分组 + 编排标记）
docs/plan-trae-solo-provider-integration.md      本文档
```
