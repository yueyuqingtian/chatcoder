"""变更审核记录（v11）：按 turn 持久化文件审核状态。

与 RollbackWrite 解耦：RollbackWrite 是「写盘前后内容」的精确回滚依据（变更数据源），
FileReview 仅记录「用户对某个 turn 的某文件是否已人工审核」，二者互不影响。
刷新/重开会话后审核状态从数据库恢复。
"""
from sqlalchemy import BigInteger, Boolean, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class FileReview(Base):
    __tablename__ = "file_reviews"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    turn_id: Mapped[int] = mapped_column(BigInteger)  # 所属 turn（含子代理写盘）
    path: Mapped[str] = mapped_column(String(512))  # 目标文件相对路径
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否已人工审核
    updated_at: Mapped[str] = mapped_column(server_default=func.now())

    __table_args__ = (UniqueConstraint("turn_id", "path", name="uq_file_review_turn_path"),)