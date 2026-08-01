# chatcoder server

AI 多 Agent 协同编码工作台 - 服务端（任务编排 + 会话调度 + 模型网关）。

## 技术栈

- **FastAPI** + **Uvicorn**（异步 Web 框架）
- **SQLAlchemy 2.0**（async）+ **asyncpg** + **PostgreSQL**
- **OpenAI SDK**（兼容 DeepSeek / GLM 等任意 OpenAI 兼容 API）
- **Redis** / **Qdrant**（向量库）

## 目录结构

```
app/
├── core/            # 配置、常量、枚举
├── gateway/         # 网关层（REST/WS/鉴权）— 预留 Java 拆分点
│   └── routers/
├── orchestration/   # 编排层（Agent 运行时 / DAG / 发言权）
├── models/          # 模型网关（Provider 抽象 / 多模型路由）
├── persistence/     # 数据库 / ORM 模型
└── main.py          # FastAPI 应用工厂
```

## 开发

```bash
# 1. 启动依赖（PostgreSQL/Redis/Qdrant）
docker compose up -d

# 2. 安装依赖
cd server
python -m venv .venv
.venv\Scripts\activate     # Windows
pip install -e ".[dev]"

# 3. 配置环境变量
cp ../.env.example ../.env
# 编辑 .env 填入默认模型配置（可选）

# 4. 初始化数据库
python -m scripts.init_db

# 5. 启动服务
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000/docs 查看 API 文档。

## 架构说明

详见 [docs/technical-design.md](../docs/technical-design.md)。

**核心边界**：服务端负责编排与调度，客户端负责执行与本地推理（BYOK）。
