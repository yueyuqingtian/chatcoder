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
    # plan-20261021-usage-update：调用记录表按时间倒序分页，created_at 需要索引
    created_at = Column(DateTime, server_default=func.now(), index=True)
    # plan-20261021-usage-update：调用性能（毫秒）。旧流水为空，前端显示「—」或按时间戳估算。
    # first_token_ms = 请求发起 → 首个输出帧；gen_ms = 首个输出帧 → 流结束（速度 = 输出 token ÷ gen_ms）
    first_token_ms = Column(Integer, nullable=True)
    gen_ms = Column(Integer, nullable=True)
    # plan-20261021-usage-update：本次调用实际使用的账号 / Key 快照（凭据之后被改名或删除不影响历史展示）
    credential_id = Column(Integer, nullable=True)
    credential_label = Column(String(120), default="")
    credential_hint = Column(String(40), default="")
