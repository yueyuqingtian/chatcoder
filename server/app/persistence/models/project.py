"""项目（工作目录）：v2 项目任务驱动的顶层实体。"""
from sqlalchemy import BigInteger, Boolean, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)  # 工作目录末段，可改
    path: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)  # 绝对路径，创建后不可改
    rules_docs: Mapped[list | None] = mapped_column(JSON)  # 手动配置的规则文档相对路径
    auto_scan_rules: Mapped[bool] = mapped_column(Boolean, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # plan-282-1441（#5 工作树）：工作树复用 Project 表——它"就是一个工作区"，
    # session.project_id 指向它即可正常建会话、跑引擎（引擎已按 project.path 解析工作区），
    # 左侧面板也天然能渲染。用 is_worktree 与普通项目区分（不参与"添加项目"去重与索引扫描）。
    is_worktree: Mapped[bool] = mapped_column(Boolean, default=False)
    # 工作树所属的主项目；普通项目为 None。
    parent_project_id: Mapped[int | None] = mapped_column(BigInteger)
    # git 分支名（工作树才有），便于列表直接展示。
    worktree_branch: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str] = mapped_column(server_default=func.now(), onupdate=func.now())
