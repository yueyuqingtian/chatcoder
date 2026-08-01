"""Turn 级快照（回滚/撤销核心，v2）。"""
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class TurnSnapshot(Base):
    __tablename__ = "turn_snapshots"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int] = mapped_column(BigInteger, unique=True)  # 该快照保护的 turn
    user_message_id: Mapped[int | None] = mapped_column(BigInteger)  # 该 turn 的用户消息 id（回填输入框用）
    git_head: Mapped[str | None] = mapped_column(String(64))  # 快照时 git HEAD（None=非 git 仓库）
    file_list: Mapped[list | None] = mapped_column(JSON)  # turn 内 fs_write 的 checkpoint 清单
    new_files: Mapped[list | None] = mapped_column(JSON)  # turn 内新建的文件（回滚时删除）
    rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
