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


class RollbackWrite(Base):
    """写盘记录（v9 精确回滚）：记录每次写盘操作前后文件内容。

    - old_content：写盘前文件内容；None 表示该文件是本次新建（回滚=删除）
    - new_content：写盘后文件内容
    回滚时据此生成「反向 diff」，只撤销 AI 引入的改动，
    用户在同一文件上的手动改动会被保留（重叠冲突时跳过并提示）。
    """

    __tablename__ = "rollback_writes"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int] = mapped_column(BigInteger)  # 写入所属 turn（含子代理写盘）
    tool: Mapped[str] = mapped_column(String(64))  # fs_write / editor_apply_diff / multi_file_edit
    path: Mapped[str] = mapped_column(String(512))  # 目标文件相对路径
    old_content: Mapped[str | None] = mapped_column(String)  # 写盘前内容（None=新建文件或二进制）
    new_content: Mapped[str | None] = mapped_column(String)  # 写盘后内容
    # v2.2 (plan-88): 二进制/超限文件——不存文本前后内容，回滚走 checkpoint 备份恢复
    binary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
