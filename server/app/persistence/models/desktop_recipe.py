"""电脑操控「操作路线」持久化（plan-334-1661）。

为什么需要它：让模型每次操控同一个应用时重新思考「先点哪里、再输什么」是纯粹的浪费。
把成功的操作路线按「应用名 + 操作意图」沉淀下来，下次命中时直接把**通用原则**喂给模型，
模型只需按原则执行，不必重新推演——这才是真正降低推理开销的地方。

关键设计取舍：
- **存通用原则，不存坐标回放。** 界面会变，硬编码坐标必然失效；而「搜索歌曲要先用顶部
  搜索框，输入后按回车，结果列表第一项通常是目标」这类原则是稳定的。因此 principle 是
  本表的核心字段，steps 只作为辅助参考（记录当时的实际操作序列，便于人工核对）。
- **按 (app_name, intent) 去重。** 同一应用同一意图只保留一条，重复命中时累加 usage_count
  而不是新增记录，避免表被同类路线堆满。
- **用户可管理。** app_name/intent/principle/pitfalls 均可编辑，支持批量删除与导入导出。
"""
from sqlalchemy import JSON, BigInteger, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class DesktopRecipe(Base):
    """一条电脑操控操作路线。"""

    __tablename__ = "desktop_recipes"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    # 应用标识：进程名（如 QQMusic）或窗口标题关键词。用进程名更稳定。
    app_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 操作意图：一句话目标（如「搜索并播放指定歌曲」）
    intent: Mapped[str] = mapped_column(String(255), nullable=False)
    # 通用原则：本表最重要的字段——跨界面版本仍成立的操作知识
    principle: Mapped[str] = mapped_column(Text, nullable=False)
    # 已知坑点：这次踩到的、下次要避开的（可空）
    pitfalls: Mapped[str | None] = mapped_column(Text)
    # 当时的实际操作序列（辅助参考，非执行依据）
    steps: Mapped[list | None] = mapped_column(JSON)
    # 命中复用的次数与最后使用时间：用于排序与「哪些路线真正有用」的判断
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[str | None] = mapped_column(String(40))
    # 来源：agent（AI 沉淀）/ user（用户手写/编辑过）
    source: Mapped[str] = mapped_column(String(20), default="agent")
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (
        # 去重依据：同一应用同一意图只留一条
        Index("ix_desktop_recipes_app_intent", "app_name", "intent"),
    )
