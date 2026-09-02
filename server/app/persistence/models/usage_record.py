"""模型调用用量流水（v1.1：全软件 token 统计的数据源）。"""
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, func

from app.persistence.database import Base


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, index=True, nullable=True)
    turn_id = Column(Integer, index=True, nullable=True)
    agent_id = Column(Integer, nullable=True)
    model_id = Column(Integer, index=True, nullable=True)
    model_name = Column(String(200), default="")
    # v1.2 (plan-152-704): 供应商显示名，用于区分不同供应商的同名模型
    provider_name = Column(String(120), default="")
    prompt_tokens = Column(BigInteger, default=0)
    completion_tokens = Column(BigInteger, default=0)
    reasoning_tokens = Column(BigInteger, default=0)
    cached_tokens = Column(BigInteger, default=0)
    usage_source = Column(String(16), default="api")  # api | est
    created_at = Column(DateTime, server_default=func.now())
