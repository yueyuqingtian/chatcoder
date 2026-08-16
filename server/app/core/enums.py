"""全局常量与枚举（v2：项目任务驱动架构）。"""
from enum import Enum


class MsgType(str, Enum):
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_GROUP = "tool_group"
    PLAN = "plan"
    SUMMARY = "summary"
    ARTIFACT = "artifact"
    ERROR = "error"
    SYSTEM = "system"


class SenderType(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class TurnStatus(str, Enum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ROLLED_BACK = "rolled_back"


class TaskStatus(str, Enum):
    PROPOSED = "proposed"
    PARTIAL = "partial"
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelSource(str, Enum):
    SYSTEM_DEFAULT = "system_default"
    BYOK = "byok"


class AgentKind(str, Enum):
    MAIN = "main"
    SUB = "sub"


class ApprovalPolicy(str, Enum):
    ON_REQUEST = "on-request"
    AUTO = "auto"
    NEVER = "never"
    REJECT = "reject"


class SandboxMode(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ExecPolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class HookEvent(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PERMISSION_REQUEST = "permission_request"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_END = "turn_end"
    COMPACT = "compact"


class MemoryKind(str, Enum):
    FACT = "fact"
    CONVENTION = "convention"
    PITFALL = "pitfall"
    DECISION = "decision"


class RunLocation(str, Enum):
    CLIENT = "client"
    SERVER = "server"


class ToolName(str, Enum):
    FS_READ = "fs_read"
    FS_WRITE = "fs_write"
    FS_LIST = "fs_list"
    TERMINAL_EXEC = "terminal_exec"
    EDITOR_APPLY_DIFF = "editor_apply_diff"
    GIT_DIFF = "git_diff"
    GREP = "grep"
    WEB_FETCH = "web_fetch"
    WEB_SEARCH = "web_search"
    VIEW_IMAGE = "view_image"
    MEMORY_SEARCH = "memory_search"
    CI_RUN = "ci_run"
    SPAWN_SUBAGENT = "spawn_subagent"
    ASK_SUBAGENT = "ask_subagent"
    COLLECT_RESULTS = "collect_results"
