"""内置 MCP 服务包（plan-282-1441 #7/#8）。

每个模块都是独立可执行进程，按 `python -m app.mcp_servers.<name>` 启动：
- `database`  —— 数据库连接（MySQL / PostgreSQL / SQL Server）
- `debugger`  —— 开发调试（接口调用 / Web CDP / Java JDWP / Arthas 现场观测）
"""
