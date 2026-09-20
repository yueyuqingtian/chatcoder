"""ORM 模型聚合（v2），供应用统一导入。"""
from app.persistence.models.agent import Agent
from app.persistence.models.audit import AuditLog
from app.persistence.models.config import ConfigProfile
from app.persistence.models.db_connection import DbConnection, DbPolicy
from app.persistence.models.debug_setting import DebugSetting
from app.persistence.models.exec_policy import ExecPolicyRule
from app.persistence.models.hook import HookConfig
from app.persistence.models.knowledge import KnowledgeBase, KnowledgeDoc
from app.persistence.models.memory import MemoryEntry
from app.persistence.models.message import Message, Session
from app.persistence.models.model_reg import Model, Provider, ProviderCredential
from app.persistence.models.project import Project
from app.persistence.models.review import FileReview
from app.persistence.models.rollback import RollbackWrite, TurnSnapshot
from app.persistence.models.scheduled import ScheduledTask
from app.persistence.models.skill import McpServer, Skill
from app.persistence.models.subagent_profile import SubagentProfile
from app.persistence.models.ta3_auth import Ta3Auth
from app.persistence.models.task import Artifact, Task
# plan-282-1441：ToolCall 此前**遗漏**在本聚合之外——Base.metadata 里没有 tool_calls 表，
# 新建库不会建该表（幂等键机制在全新部署上静默失效）。补上导入即完成注册。
from app.persistence.models.tool_call import ToolCall
from app.persistence.models.trae_auth import TraeAuth
from app.persistence.models.tenant import Tenant, User
from app.persistence.models.turn import Turn
from app.persistence.models.usage_record import UsageRecord
from app.persistence.models.workbuddy_auth import WorkBuddyAuth

__all__ = [
    "Tenant",
    "User",
    "Project",
    "Session",
    "Turn",
    "Agent",
    "Message",
    "Task",
    "Artifact",
    "Model",
    "Provider",
    "ProviderCredential",
    "ScheduledTask",
    "ConfigProfile",
    "DbConnection",
    "DbPolicy",
    "DebugSetting",
    "ExecPolicyRule",
    "HookConfig",
    "AuditLog",
    "MemoryEntry",
    "TurnSnapshot",
    "RollbackWrite",
    "FileReview",
    "KnowledgeBase",
    "KnowledgeDoc",
    "Skill",
    "McpServer",
    "SubagentProfile",
    "Ta3Auth",
    "WorkBuddyAuth",
    "ToolCall",
    "TraeAuth",
    "UsageRecord",
]
