# chatcoder

本地优先的 AI 编码代理工作台（Windows 桌面版）。

把「提需求 → 读代码 → 改文件 → 跑命令 → 自测 → 交付」交给一个可审计的代理会话：复杂任务可以拆给子代理并行推进，也可以用工作流把「探索 / 实现 / 评估」编排成可回溯的流程图。

- **单机运行**：Electron 桌面壳自带后端，数据落在本机 SQLite 单文件，不需要安装数据库、缓存或向量服务。
- **密钥自持（BYOK）**：供应商、模型与凭据都由你在界面里配置，模型请求由本机直连。
- **改动可审**：计划确认、逐轮变更审核、回滚与操作审批都有留痕，关键动作看得见。

## 功能一览

### 任务式编码会话

- 项目 → 会话两级管理：新建任务（`Ctrl+N`）、命令中心（`Ctrl+K`）、置顶、重命名、按时间或项目分组、归档与恢复。
- 消息流覆盖完整过程：流式正文、思考过程、工具调用卡片（命令可复制、可折叠）、计划卡、审批卡、子代理卡、逐轮变更卡、错误卡与后续任务建议。
- 计划确认门：复杂任务先给出拆解与写入范围，确认后才执行（可在设置中改为自动确认）。
- 产物审核与回滚：按轮次查看改了哪些文件，可整轮回滚；子代理与工作流在等待你决定时会在侧栏标出。

### 代理运行时与工具

- 文件读写、批量编辑、精确替换、目录浏览、附件与图片读取。
- 终端命令、后台进程管理、Git 操作、CI 检查（lint / test / build）。
- 代码搜索：符号索引（函数 / 类 / 方法定位）、增强搜索（随包内置 ripgrep）、代码库关键词检索。
- 记忆与知识：会话 / 项目 / 全局三层记忆，知识库检索，压缩后的细节归档可回查。
- 网页搜索与抓取、内置浏览器与电脑操控工具（见下文）。
- 安全边界：文件与命令工具限制在工作区根目录内，越界访问需显式放行；写文件、执行命令等副作用动作按执行策略走审批。
- 上下文管理：超过阈值自动压缩成可见卡片，历史细节转入归档，避免长会话失控。

### 子代理

- 后台并行派发，可选「只读」模式做调研审阅，或「智能体」模式直接改代码。
- 需要你拍板时会主动提问并暂停等待，答复后继续；其待审批操作会标注来源（子代理编号 + 任务标题）。
- 完成后把结论汇报回主会话，结果可折叠查看。

### 工作流

- 图式编排：`explore`（探索）/ `execute`（实现）/ `review`（评估）等节点，支持输入依赖、写入范围隔离、评估不通过自动返工。
- 内置预设（如「探索 · 实现 · 评估」）作为起点，也可在设置里编辑自定义模板。
- 运行中可在会话内与侧栏看到进度、检查点、冲突与等待决定的状态。

### 模型接入

- 供应商 → 模型 → 凭据三级管理，兼容 OpenAI 协议（DeepSeek、GLM、自建网关等）。
- 多凭据轮换、额度与冷却状态、Provider 登录型账号（如 ta3 / trae / kiro / workbuddy）的额度展示。
- 会话级模型覆盖、推理强度与上下文窗口配置、token 用量统计。

### 扩展能力

- **技能**：按需加载的领域知识包，支持从市场安装、从 Git 仓库导入。
- **MCP 连接器**：内置数据库连接器（MySQL / PostgreSQL / SQL Server，用于查询你已有的业务库）与开发调试连接器，也可接入外部 MCP 服务。
- **插件**：可贡献命令、设置页、侧栏与面板槽位；统一在「拓展」页管理。
- **市场**：浏览、安装、更新技能与插件，也支持本地安装包。

### 内置浏览器与电脑操控

- 内置浏览器开在右侧面板，AI 与用户共用同一个页面，你随时可以接管；支持导航、快照、截图、点击、输入、滚动、拖拽、批量动作、下载与操作路线沉淀。
- 电脑操控：窗口枚举、无障碍快照、截图、点击 / 输入 / 按键 / 滚动 / 拖拽、启动程序；写操作默认要求目标窗口在前台，避免输入打到错误窗口。
- 操作路线（recipe）会沉淀为可复用的通用做法，下一次同类操作直接复用。

### 开发面板

- 左右面板可拖拽的三栏布局；右侧面板包含文件树、Git、终端、浏览器、调试、子代理与任务摘要。
- Git：状态、差异、提交、拉取 / 推送、分支切换，以及工作树的创建与合并。
- 终端：真 PTY（node-pty + xterm），多标签、可调 Shell、内置等宽字体。
- 调试：浏览器 CDP 调试与 Java JDWP / Arthas 现场诊断，断点与暂停位置可回到界面。

### 自动化与统计

- 定时任务：cron 表达式 + 常用模板，运行期间可保持电脑唤醒。
- 钩子：在事件节点挂自定义命令。
- 使用统计：按会话 / 模型查看 token 与上下文占用。
- 诊断：健康检查、系统状态、日志入口。

### 桌面体验

- 主题、外观、字号与字体、毛玻璃与启动动画。
- 桌面宠物：独立置顶浮窗展示任务状态，可一键打开会话或设置。
- 自动更新：自建主源 + GitHub 备源，断点续传与安装包校验。

## 技术栈

| 层 | 技术 |
|---|---|
| 桌面壳 | Electron 44（主进程、preload、node-pty、electron-updater） |
| 前端 | React 18 + TypeScript + Vite 5 + zustand + Tailwind 4 + Radix UI + Monaco + xterm |
| 后端 | Python 3.10+、FastAPI + Uvicorn、SQLAlchemy 2 异步 ORM |
| 存储 | SQLite（aiosqlite，WAL 模式）单文件，**不依赖 PostgreSQL / Redis / Qdrant** |
| 通信 | REST + WebSocket（流式事件） |
| 打包 | PyInstaller（后端）+ electron-builder（NSIS x64 安装包） |

## 架构

```
Electron 主进程（electron/）
  ├─ 窗口、标题栏、主题与毛玻璃
  ├─ node-pty 终端、桌面宠物浮窗、自动更新、外部应用启动
  └─ 拉起并守护本地后端
        │ spawn
        ▼
chatcoder-server.exe（server/，FastAPI + Uvicorn）
  ├─ gateway/        REST + WebSocket（流式事件、审批、上传）
  ├─ orchestration/  代理循环、工具执行、权限审批、上下文压缩、子代理、工作流
  ├─ services/       项目 / 会话 / Git / 索引 / 记忆 / 调度 / 用量 / 自动化
  ├─ models/         供应商与模型网关（OpenAI 兼容）
  ├─ mcp_servers/    内置数据库连接与开发调试连接器
  └─ persistence/    SQLAlchemy + SQLite（WAL、单写者）
        ▲
        │ REST / WebSocket（/api、/ws）
        ▼
React 渲染层（client/，构建后由 Electron 以 file:// 加载）
```

后端默认监听 `127.0.0.1:12973`，端口被占用时自动向后顺延（最多 50 个）。开发模式下前端由 Vite 提供，`/api` 与 `/ws` 代理到本机 8000 端口。

## 目录结构

| 目录 | 说明 |
|---|---|
| `electron/` | 桌面主进程、preload、PTY、宠物窗口、更新管理 |
| `client/` | React 前端：消息流、左右面板、设置中心、宠物页 |
| `server/` | Python 后端：路由、编排、业务服务、持久化与迁移 |
| `packages/shared` | 前后端共享事件与类型定义 |
| `tools/` | 打包辅助脚本（桌面操控内核构建、安装包自检） |
| `docs/` | 产品设计、技术方案、发布流程 |
| `ai/` | 迭代计划与设计记录 |

## 环境要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Node.js | >= 18（建议 20 LTS 及以上） | 前端构建与 Electron 打包 |
| Python | >= 3.10（发布打包使用 3.13） | 本地后端 |
| Windows | 10 / 11 x64 | 发行包只提供 NSIS x64 安装程序 |

不需要 Docker，也不需要 PostgreSQL、Redis 或 Qdrant。

## 快速开始（源码开发）

### 1. 获取代码

```powershell
git clone https://github.com/yueyuqingtian/chatcoder.git
cd chatcoder
```

### 2. 启动后端

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# .env 为本地隐私文件，不会提交；模板默认使用 SQLite
Copy-Item ..\.env.example .env

# 启动服务；首次启动自动建表、幂等迁移与种子数据
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- API 文档：<http://127.0.0.1:8000/docs>
- 数据库默认是 `server/chatcoder.db`（`DATABASE_URL=sqlite+aiosqlite:///./chatcoder.db`），删除该文件即可得到全新环境。
- `python -m scripts.init_db` 仅在需要手工建表时使用，正常启动不需要。
- 想用与桌面版一致的打包入口，可执行 `python run_server.py`（默认 12973 端口，数据库落在 `server/chatcoder.db`）。

### 3. 启动前端

```powershell
cd client
npm install
npm run dev
```

打开 <http://localhost:5173>。Vite 会把 `/api`、`/ws` 代理到 `http://localhost:8000`；后端换端口时用环境变量 `CHATCODER_BACKEND` 指定，例如：

```powershell
$env:CHATCODER_BACKEND = "http://127.0.0.1:12973"
npm run dev
```

### 4. 运行桌面壳（可选）

桌面壳加载的是构建产物，会拉起 `server/dist/chatcoder-server/chatcoder-server.exe`：

```powershell
# 在仓库根目录
npm install
npm run build:frontend
npm run build:backend     # 需要 server/.venv 已安装 pyinstaller
npm start
```

内置浏览器与电脑操控的额外准备：

```powershell
# 内置浏览器（Playwright Chromium）
cd server
pip install -e ".[browser]"
python -m playwright install chromium
```

```powershell
# 电脑操控内核（.NET Framework 自带的 csc.exe 即可；在仓库根目录执行）
powershell -ExecutionPolicy Bypass -File tools\desktop-core\build.ps1 -OutDir server\vendor\desktop-core
```

## 桌面端打包（Windows）

一键脚本会依次完成：内置能力预检（Chromium / 桌面操控内核 / ripgrep）→ 构建前端 → PyInstaller 打包后端与索引 worker → 产物自检 → electron-builder 生成 NSIS 安装包 → 安装包自检。

```powershell
powershell -ExecutionPolicy Bypass -File build-release.ps1
```

产物目录取 `package.json` 的 `build.directories.output`，包含 `chatcoder-Setup-<version>.exe`、`latest.yml`、`.blockmap` 三件套。

也可以分步执行：

```powershell
npm install
npm run build:frontend
npm run build:backend
npm run dist
```

后端打包依赖锁定在 `server/requirements-build.txt`（含 PyInstaller），装好后再执行打包：

```powershell
cd server
.\.venv\Scripts\pip.exe install -r requirements-build.txt
```

## 自动更新

桌面端内置自动更新（electron-updater）：启动 3 秒后检查，之后每 20 分钟检查一次；发现新版本时侧栏设置按钮右侧出现更新徽标，设置「关于」页可手动检查并一键安装。

更新源采用「自建主源 + GitHub 备源」双通道，自动回退：

- 主源（自建）：`https://service.guyueyu.asia/updates/`，静态文件分发，国内直连；
  文件由服务器定时从 GitHub Releases 拉取并校验 sha512，与 GitHub 上的安装包一致；
- 备源：GitHub Releases，主源不可用时自动切换重试；
- 更新历史同样三级回落：自建源静态数据 → GitHub API → 本地 `CHANGELOG.md`。

发布新版本：先更新 `package.json` 的 version 与 `CHANGELOG.md` 并提交，然后推送 tag，GitHub Actions 自动打包发布：

```powershell
git tag v<version>
git push origin v<version>
```

完整说明（手动重跑、本地兜底打包、自建源定时同步、仓库 Secret）见 [发布流程](docs/release.md)。

注意：

- Release 资产必须包含 `latest.yml`、`.exe`、`.blockmap`，且资产名与 `latest.yml` 中的 `url` 一致（`artifactName` 已保证无空格）。
- `latest.yml` 内置本版本更新说明（取自 `CHANGELOG.md`），三条更新线路的客户端都能看到「更新了什么」。
- 自建源只保留最新版本安装包与最近 10 个 blockmap，磁盘占用基本恒定。
- 安装包未签名时，首次安装 / 更新会触发 Windows SmartScreen 提示（与手动下载一致）。

## 数据、配置与隐私

桌面版数据全部写入当前用户的本地可写目录：

| 内容 | 位置（Windows） | 说明 |
|---|---|---|
| 数据库 | `%LOCALAPPDATA%\chatcoder\chatcoder.db` | SQLite 单文件（WAL），会话、消息、任务、用量等 |
| 工作区 | `%LOCALAPPDATA%\chatcoder\workspace` | 文件工具与终端命令的沙箱根目录 |
| 用户配置 | `%LOCALAPPDATA%\chatcoder\config.json` | 界面里修改的设置 |
| 服务端日志 | `%LOCALAPPDATA%\chatcoder\logs\`（`server.log` / `crash.log`） | 后端启动、工具异常与崩溃堆栈 |
| 主进程日志 | `%APPDATA%\chatcoder\logs\`（`main.log` / `backend.log`） | 窗口、终端、更新，以及后端 stdout/stderr |
| 环境变量 | 安装目录下 `server\chatcoder-server\.env` | 打包时带入，模板见 `.env.example` |

源码开发时，数据库与工作区默认落在 `server/` 目录下（`server/chatcoder.db`、`server/workspace`）；用 `run_server.py` 启动时日志在 `server/logs/`。

隐私约定：

- `.env`、`server/.env`、密钥文件、数据库、工作区与日志均在 `.gitignore` 中忽略，**不会提交到仓库**。
- 仓库仅提交 `.env.example` 模板，真实密钥由使用者在本地复制后自行填写。
- 模型请求由本机直连你配置的供应商，项目方不中转模型 token；除更新检查、内置搜索等联网工具外，本地数据不会上传。

## 常见问题

**后端没起来 / 界面一直停在加载页**

先看 `backend.log` 与 `crash.log`。桌面版启动时会主动清理残留的 `chatcoder-server.exe` 与占用端口的孤儿进程，再拉起当前版本后端。

**端口被占用**

桌面版从 12973 起自动向后寻找空闲端口；开发模式下改用其他端口时，用 `CHATCODER_BACKEND` 告诉前端。

**内置浏览器不可用**

设置 →「电脑操控」里检测内核；开发态确认已执行 `python -m playwright install chromium`。发布包的 Chromium 随安装包分发，不需要额外下载。

**提示电脑操控内核缺失**

说明安装包不完整（缺少 `desktop-core`）。打包脚本有产物守门，正常发布包不会出现；自行打包时先执行 `tools\desktop-core\build.ps1`。

**首次安装被 SmartScreen 拦截**

安装包未做代码签名，点「更多信息 → 仍要运行」即可，与手动下载的安装包行为一致。

## 开发约定与提交前检查

```powershell
# 前端：类型检查 + 设计令牌检查 + 构建
cd client
npm run typecheck
npm run build

# 回到仓库根目录
cd ..

# Electron 主进程语法检查
node --check electron/main.cjs

# 后端测试
cd server
pytest
```

- 注释与界面文案使用简体中文；样式改动遵循 `client/src/styles/` 下的设计令牌。
- 提交前至少保证 `typecheck` 与 `build` 通过；涉及 Electron 主进程时补 `node --check`。
- 不修改与本任务无关的模块，顺带发现的问题记录到计划文档另行处理。

## 文档

- [更新日志](CHANGELOG.md)（应用内「更新历史」的离线数据源）
- [产品需求 PRD](docs/PRD.md)（早期产品定位草案）
- [技术设计](docs/technical-design.md)
- [上下文管理](docs/context-management.md)
- [沙箱设计](docs/sandbox-design.md)
- [发布流程](docs/release.md)
- [服务端说明](server/README.md)
