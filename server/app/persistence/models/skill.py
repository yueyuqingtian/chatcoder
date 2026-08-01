"""v3.6: Skills（技能）与 MCP Server 数据模型。

Skills 是可复用的领域知识包/工作流，来源包括：
- 内置技能
- 扫描自外部工具：Codex (.codex/skills/), CodeBuddy (.codebuddy/skills/),
  Qoder (.qoder/skills/), Trae (.trae/skills/)
- 用户自定义

MCP Server 是 Model Context Protocol 服务端配置，来源同理。
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import ARRAY

from app.persistence.database import Base


class Skill(Base):
    """技能定义 —— 可注入 Agent 的领域知识/工作流/指令模板。

    一个 Skill 包含：
    - name: 唯一标识
    - source: 来源（builtin / codex / codebuddy / qoder / trae / custom）
    - path: 在文件系统中的原始路径（扫描来源的技能）
    - content: 技能内容（Markdown/YAML 解析后的指令文本）
    - trigger: 触发条件描述
    - tools: 该技能依赖的工具列表
    - is_active: 是否启用
    - auto_load: 是否自动加载到匹配的 Agent
    """

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # 来源类型: builtin / codex / codebuddy / qoder / trae / custom
    source: Mapped[str] = mapped_column(String(40), default="custom", index=True)
    # 原始文件路径（扫描来源的技能才有）
    path: Mapped[str | None] = mapped_column(String(512))
    # 技能内容（指令文本/SOP/领域知识）
    content: Mapped[str | None] = mapped_column(Text)
    # 触发条件描述（供 Leader 匹配时使用）
    trigger: Mapped[str | None] = mapped_column(Text)
    # 依赖工具列表
    tools: Mapped[list | None] = mapped_column(JSON)
    # 标签（用于分类和搜索）
    tags: Mapped[list | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 是否自动加载（匹配时自动注入到 Agent 上下文）
    auto_load: Mapped[bool] = mapped_column(Boolean, default=True)
    # 元数据（来源扫描的额外信息）
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str | None] = mapped_column(server_default=func.now(), onupdate=func.now())


class McpServer(Base):
    """MCP Server 配置 —— Model Context Protocol 服务端。

    记录每个 MCP Server 的连接配置，支持：
    - 扫描自外部工具（Codex/CodeBuddy/Qoder/Trae 的 MCP 配置）
    - 用户自定义 MCP
    - 每个 Agent 可绑定不同的 MCP 子集
    """

    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # 来源类型: builtin / codex / codebuddy / qoder / trae / custom
    source: Mapped[str] = mapped_column(String(40), default="custom", index=True)
    # 传输方式: stdio / sse / websocket
    transport: Mapped[str] = mapped_column(String(20), default="stdio")
    # stdio: 可执行命令; sse/websocket: URL
    command: Mapped[str | None] = mapped_column(String(512))
    args: Mapped[list | None] = mapped_column(JSON)
    env: Mapped[dict | None] = mapped_column(JSON)
    url: Mapped[str | None] = mapped_column(String(512))
    # MCP 提供的工具列表（从 MCP server list_tools 获取）
    tools: Mapped[list | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 原始配置文件路径
    path: Mapped[str | None] = mapped_column(String(512))
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str | None] = mapped_column(server_default=func.now(), onupdate=func.now())
