"""工作流运行时数据模型（设计 §5.2）。

程序（Program）与子工作流（Flow）共用 workflow_runs（以 parent_run_id 区分）；节点、执行尝试、
增量检查点日志、运行事件、计划修订各自成表。所有写入经 run_write_locked 单写线程完成。
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"), index=True)
    # None = 程序（Program）；非空 = 子工作流（Flow），指向所属程序
    parent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("workflow_runs.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(10), default="program")  # program / flow
    package_key: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(80), default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(String(300), default="")
    based_on: Mapped[str | None] = mapped_column(String(80))
    # draft / queued / running / waiting_human / paused / succeeded / failed / cancelled
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    # network / restart / user / turn_stop / budget / error
    pause_kind: Mapped[str | None] = mapped_column(String(20))
    pause_reason: Mapped[str | None] = mapped_column(String(300))
    # 结束原因：user（用户取消）/ adjusted（已按新的要求调整，由新计划替代）
    closed_reason: Mapped[str | None] = mapped_column(String(32))
    paused_at: Mapped[str | None] = mapped_column(String(40))
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    rework_total: Mapped[int] = mapped_column(Integer, default=0)
    token_used: Mapped[int] = mapped_column(BigInteger, default=0)
    token_budget: Mapped[int] = mapped_column(BigInteger, default=0)
    workspace: Mapped[str] = mapped_column(String(512), default="")
    isolation: Mapped[str] = mapped_column(String(10), default="shared")
    depends_on_json: Mapped[list | None] = mapped_column(JSON)
    # 创建时固化的图（续跑不随模板或后续修订的临时状态变化）；修订后随 plan_rev 更新
    graph_json: Mapped[dict | None] = mapped_column(JSON)
    plan_rev: Mapped[int] = mapped_column(Integer, default=1)
    fingerprint_json: Mapped[dict | None] = mapped_column(JSON)  # 暂停时的工作区指纹（漂移检测）
    origin_turn_id: Mapped[int | None] = mapped_column(BigInteger)
    last_turn_id: Mapped[int | None] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    started_at: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[str | None] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
    error_json: Mapped[dict | None] = mapped_column(JSON)


class WorkflowNode(Base):
    __tablename__ = "workflow_nodes"
    __table_args__ = (UniqueConstraint("run_id", "node_key", name="uq_workflow_node_key"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workflow_runs.id"), index=True)
    node_key: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(80), default="")
    kind: Mapped[str] = mapped_column(
        String(12)
    )  # explore / execute / review / check / gate / merge
    role: Mapped[str | None] = mapped_column(String(64))
    task: Mapped[str] = mapped_column(Text, default="")
    inputs_json: Mapped[list | None] = mapped_column(JSON)
    acceptance_json: Mapped[list | None] = mapped_column(JSON)
    write_scope_json: Mapped[list | None] = mapped_column(JSON)
    commands_json: Mapped[list | None] = mapped_column(JSON)
    rework_to: Mapped[str | None] = mapped_column(String(40))
    max_rework: Mapped[int | None] = mapped_column(Integer)
    question: Mapped[str | None] = mapped_column(String(300))
    # pending / queued / running / recovering / waiting_approval / waiting_human /
    # done / failed / interrupted / cancelled / skipped
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reason: Mapped[str | None] = mapped_column(String(120))  # 排队或阻塞原因（展示用）
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    rework_round: Mapped[int] = mapped_column(Integer, default=0)
    output_json: Mapped[dict | None] = mapped_column(JSON)
    verdict_json: Mapped[dict | None] = mapped_column(JSON)
    checks_json: Mapped[list | None] = mapped_column(JSON)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agents.id"))
    next_retry_at: Mapped[str | None] = mapped_column(String(40))
    last_heartbeat_at: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[str | None] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
    error_json: Mapped[dict | None] = mapped_column(JSON)
    token_used: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    # 下一次尝试的模式与说明（返工 / 续跑 / 格式修复），重启后仍可恢复
    pending_json: Mapped[dict | None] = mapped_column(JSON)
    # 待用户决定的冲突 / 越界载荷（L4 冲突闸门：基线、当前版本、待写入版本）
    conflict_json: Mapped[dict | None] = mapped_column(JSON)


class WorkflowAttempt(Base):
    """节点的一次实际执行（fresh / checkpoint 续跑 / rework 返工 / manual 手动重试）。"""

    __tablename__ = "workflow_attempts"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    node_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workflow_nodes.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(
        String(12), default="fresh"
    )  # fresh / checkpoint / rework / manual
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agents.id"))
    # running / done / failed / interrupted / cancelled / stalled
    status: Mapped[str] = mapped_column(String(12), default="running")
    reason: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[str | None] = mapped_column(String(40))
    ended_at: Mapped[str | None] = mapped_column(String(40))
    tool_trace_json: Mapped[list | None] = mapped_column(
        JSON
    )  # 工具调用的开始 / 结束记录（结果未知对账）


class WorkflowMessage(Base):
    """增量检查点日志（§6.2）：每步追加 delta，压缩时追加 snapshot；续跑 = 最近快照 + 其后增量。"""

    __tablename__ = "workflow_messages"
    __table_args__ = (Index("ix_workflow_messages_attempt_seq", "attempt_id", "seq"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workflow_attempts.id"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))  # snapshot / delta
    payload_json: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class WorkflowEvent(Base):
    """运行级持久化事件日志（权威来源；WS 只做实时推送）。"""

    __tablename__ = "workflow_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_workflow_event_seq"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workflow_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(40))
    node_key: Mapped[str | None] = mapped_column(String(40))
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class WorkflowTemplate(Base):
    """用户模板：从已有计划“存为模板”，之后可用 based_on: user:<key> 引用（设计 §4.5 第 8 点）。"""

    __tablename__ = "workflow_templates"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    key: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(String(300), default="")
    graph_json: Mapped[dict | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str | None] = mapped_column(String(40))


class WorkflowPlanRevision(Base):
    """计划修订历史（amend_workflow 每次 +1，经同一套校验）。"""

    __tablename__ = "workflow_plan_revisions"
    __table_args__ = (UniqueConstraint("run_id", "rev", name="uq_workflow_plan_rev"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workflow_runs.id"), index=True)
    rev: Mapped[int] = mapped_column(Integer)
    graph_json: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class WorkflowToolCall(Base):
    """工具调用台账（§6.5）：调用开始即落库，结束时补写结果；中断续跑时据此对账“结果未知”的操作。"""

    __tablename__ = "workflow_tool_calls"
    __table_args__ = (Index("ix_workflow_tool_calls_node", "node_id"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workflow_attempts.id"))
    node_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("workflow_nodes.id"))
    llm_call_id: Mapped[str] = mapped_column(String(128), default="")  # 模型给出的工具调用编号
    tool: Mapped[str] = mapped_column(String(64))
    args_preview: Mapped[str] = mapped_column(String(300), default="")
    # 写入类操作的文件效果（执行前的文件哈希与预期内容哈希），续跑对账时据此核对；其它工具为空
    effect_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # started / done / failed / reconciled
    started_at: Mapped[str] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
