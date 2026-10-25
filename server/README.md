# chatcoder server

AI 编码代理工作台的服务端：代理运行时、工具执行、任务编排、会话调度与模型网关。

桌面版由 Electron 以 `127.0.0.1` 拉起（默认端口 12973），源码开发时也可独立启动。

## 技术栈

- **FastAPI** + **Uvicorn**（异步 Web 框架，REST + WebSocket）
- **SQLAlchemy 2.0**（async）+ **SQLite / aiosqlite**（WAL 模式，桌面端单写者）
- **OpenAI SDK**（兼容 DeepSeek / GLM 等任意 OpenAI 协议接口）
- **Playwright**（可选，内置浏览器与后台兜底浏览器）

不依赖 PostgreSQL、Redis 或 Qdrant：`DATABASE_URL` 默认即 SQLite 相对路径，项目自带建表、幂等迁移与种子数据。

## 目录结构

```
app/
├── core/            # 配置、常量、枚举、日志
├── gateway/         # 网关层：routers（REST）+ ws（WebSocket 流式事件）
├── orchestration/   # 编排层：agent loop、工具、审批、压缩、子代理、工作流
├── models/          # 模型网关：供应商 / 模型 / 凭据
├── services/        # 业务服务：项目、会话、Git、索引、记忆、调度、用量…
├── mcp_servers/     # 内置 MCP：数据库连接、开发调试
├── persistence/     # 数据库引擎、ORM 模型、迁移与种子
├── auth/            # 鉴权
├── data/            # 内置数据（规则、模板等）
├── index_worker.py  # 符号索引 worker 入口
└── main.py          # FastAPI 应用工厂（启动时建表 + 迁移 + 种子）
```

## 开发

```powershell
# 1. 安装依赖
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. 配置环境变量（模板默认 SQLite，无需改数据库）
Copy-Item ..\.env.example .env

# 3. 启动服务（首次启动自动建表、迁移与种子）
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

访问 <http://127.0.0.1:8000/docs> 查看 API 文档。

- 数据库落在启动目录：`server/chatcoder.db`；工作区默认 `server/workspace`。
- 需要手工建表时可用 `python -m scripts.init_db`，正常启动不需要。
- 内置浏览器工具需要 Playwright 与 Chromium：

```powershell
pip install -e ".[browser]"
python -m playwright install chromium
```

## 测试与检查

```powershell
pytest                      # 后端测试
ruff check app              # 代码检查
mypy app                    # 类型检查（strict=False）
```

## 打包

发布打包使用 PyInstaller（锁定的依赖清单见 `requirements-build.txt`）：

```powershell
.\.venv\Scripts\pip.exe install -r requirements-build.txt
.\.venv\Scripts\pyinstaller.exe chatcoder-server.spec --noconfirm --clean
.\.venv\Scripts\pyinstaller.exe chatcoder-index-worker.spec --noconfirm --clean
```

打包后的入口是 `run_server.py`：

- 数据统一写入 `%LOCALAPPDATA%\chatcoder`（数据库、工作区、日志），避免安装在只读目录时无法写入；
- 数据库固定为 `...\chatcoder\chatcoder.db`，并加单实例锁防止多进程同时写 SQLite；
- 日志写入 `...\chatcoder\logs\server.log`，崩溃堆栈写入 `crash.log`；
- 同一 exe 通过 `--mcp-server <name>` 兼任内置 MCP 子进程入口。

## 架构说明

详见 [docs/technical-design.md](../docs/technical-design.md)。

**核心边界**：服务端负责编排、工具执行与持久化；桌面端负责窗口、终端、浏览器面板等交互载体与本地 IPC。模型请求由服务端按用户配置的供应商与凭据直连，密钥不写入仓库。
