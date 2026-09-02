# chatcoder

AI 多 Agent 协同编码工作台（桌面版）—— Electron 桌面壳 + React/Vite 前端 + FastAPI 服务端。

支持多 Agent 任务编排、会话级模型覆盖、子代理管控、上下文压缩、产物审核与回滚、工具沙箱执行、BYOK 模型网关等。

## 目录结构

| 目录 | 说明 |
|---|---|
| `electron/` | Electron 桌面壳（主进程、preload、PTY 终端） |
| `client/` | 前端（React 18 + Vite 5 + TypeScript） |
| `server/` | 服务端（FastAPI + SQLAlchemy；任务编排、会话调度、模型网关） |
| `packages/shared` | 前后端共享类型与事件定义 |
| `docs/` | PRD、技术设计、变更日志 |

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Node.js | >= 18 | 前端与 Electron |
| Python | >= 3.10 | 服务端 |
| Docker | 可选 | 开发模式需要 PostgreSQL / Redis / Qdrant |

## 快速开始（开发模式）

### 1. 克隆仓库

```bash
git clone https://github.com/yueyuqingtian/chatcoder.git
cd chatcoder
```

### 2. 启动依赖服务（PostgreSQL / Redis / Qdrant）

```bash
docker compose up -d
```

### 3. 后端

```bash
cd server
python -m venv .venv
.venv\Scripts\activate          # Windows 激活虚拟环境
pip install -e ".[dev]"

# 生成环境变量模板（.env 为隐私文件，不会提交）
copy ..\.env.example .env       # Windows
cp ../.env.example .env         # Linux / macOS
# 编辑 .env：DEFAULT_LLM_* 可留空，用户可在界面按需配置 BYOK

# 初始化数据库（建表）
python -m scripts.init_db

# 启动服务，API 文档见 http://localhost:8000/docs
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. 前端

```bash
cd client
npm install
npm run dev                     # http://localhost:5173
```

## 桌面端打包（Windows）

```powershell
# 一键脚本：构建前端 -> PyInstaller 打包后端 -> electron-builder 出 NSIS 安装包
powershell -ExecutionPolicy Bypass -File build-release.ps1
# 产物: v6/chatcoder-Setup-<version>.exe + latest.yml + .blockmap
```

或分步执行：

```bash
npm install
# 构建前端（client/dist）
npm run build:frontend
# 打包后端（需 server/.venv 已安装 pyinstaller）
npm run build:backend
# 打包桌面应用（v6/ 目录）
npm run dist
```

## 自动更新（GitHub Releases）

桌面端已内置自动更新（electron-updater）：启动 30 秒后检查、之后每 4 小时检查一次；
侧栏设置按钮右侧在发现新版本时出现深绿更新徽标，设置「关于」页可手动检查并一键安装。

发布新版本（**必须先把 package.json 的 version 递增**，打 tag 为 `v<version>`）：

```powershell
# 方式一：一键脚本打包并发布（需 gh CLI 已登录：gh auth login）
powershell -ExecutionPolicy Bypass -File build-release.ps1 -Publish

# 方式二：先手动打包，再单独发布
powershell -ExecutionPolicy Bypass -File build-release.ps1
gh release create v<version> `
  v6/chatcoder-Setup-<version>.exe v6/latest.yml v6/chatcoder-Setup-<version>.exe.blockmap `
  --title "v<version>" --notes "ChatCoder v<version>"
```

注意：

- Release 资产必须包含 `latest.yml`、`.exe`、`.blockmap` 三件套，且资产名与
  `latest.yml` 中 `url` 一致（package.json `nsis.artifactName` 已保证无空格，勿改回）。
- 更新检查走 GitHub 公开 API（匿名限流 60 次/小时），请勿把检查间隔改得过密。
- 安装包未签名时，用户首次安装/更新会触发 Windows SmartScreen 提示（与手动下载一致）。

## 隐私说明

- `.env`、`server/.env` 及各类密钥文件均已在 `.gitignore` 中忽略，**不会提交到仓库**。
- 仓库仅提交 `.env.example` 模板；真实密钥由使用者在本地 `copy/cp` 生成并自行填写。
- 运行数据（数据库、工作区、日志）默认写入本地可写目录，不入库、不外传。

## 文档

- [产品需求 PRD](docs/PRD.md)
- [技术设计文档](docs/technical-design.md)
- [变更日志](docs/changelog-v1.0.md)
- [服务端说明](server/README.md)
