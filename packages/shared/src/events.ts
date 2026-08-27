/**
 * WS 事件协议契约（v2.1，对齐 zcode-alignment-master-plan-v2 第 3.2 节）。
 *
 * 规则：
 * - 服务端广播的每条事件带单调递增 `seq`（per session），用于断线补偿；
 * - 前端 handleWs 用 switch 穷举 ServerWsEvent，default 走 never 兜底；
 * - 新增事件必须先在此登记，并在 server/app/gateway/schemas.py 镜像定义。
 */

// ── 通用信封 ──

export interface WsEnvelope {
  event: string;
  /** 服务端注入的会话级单调事件序号（断线补偿依据，per session） */
  seq?: number;
  payload: Record<string, unknown>;
}

// ── 服务端 → 客户端 ──

export type ServerWsEvent =
  | { event: "message.created"; payload: { msg: Record<string, unknown> } }
  | { event: "turn.started"; payload: { turn_id: number } }
  | { event: "turn.updated"; payload: { turn_id: number; status: string } }
  | { event: "turn.completed"; payload: { turn_id: number; summary?: string | null; artifact_ids?: number[] } }
  | { event: "turn.interrupted"; payload: { turn_id: number; last_message_id?: number | null } }
  | { event: "turn.rolled_back"; payload: { turn_id: number; rolled_back_msgs?: number; file_recovery?: Record<string, unknown> } }
  | { event: "agent.started"; payload: { agent_id: number; kind?: string; name?: string; turn_id?: number | null } }
  | { event: "agent.updated"; payload: { agent_id: number; status?: string; tool?: string; step?: number } }
  | { event: "agent.completed"; payload: { agent_id: number; summary?: string | null; artifact_ids?: number[] } }
  | { event: "thinking.delta"; payload: { agent_id: number; turn_id?: number | null; delta: string } }
  | { event: "thinking.done"; payload: { agent_id: number; turn_id?: number | null; full_text?: string } }
  | { event: "token.delta"; payload: { agent_id: number; turn_id?: number | null; delta: string } }
  | { event: "token.done"; payload: { agent_id: number; turn_id?: number | null; full_text?: string } }
  /** v35: turn 级瞬态状态（重试/恢复提示），前端流式状态行展示；text 空串 = 清除 */
  | { event: "turn.status"; payload: { turn_id: number; thread_id?: number | null; text: string } }
  | { event: "tool.call"; payload: { turn_id: number; agent_id: number; tool: string; args_preview?: string; args_partial?: string } }
  | { event: "tool.result"; payload: { turn_id: number; tool: string; ok: boolean; duration_ms?: number; output_preview?: string; change_stat?: { path: string; additions: number; deletions: number } } }
  | { event: "file.change"; payload: { turn_id: number; path?: string } }
  | { event: "todo.updated"; payload: { turn_id: number; todos: unknown[]; persisted: boolean } }
  | { event: "task.proposed"; payload: { turn_id: number; request_task_id?: number; group_task_id?: number; reasons?: string[]; plan_doc_path?: string } }
  | { event: "task.planned"; payload: { turn_id: number; steps?: unknown[] } }
  | { event: "task.updated"; payload: { task_id: number; status?: string; note?: string | null } }
  | { event: "usage.update"; payload: Record<string, unknown> }
  | { event: "compact.started"; payload: { agent_id?: number; turn_id?: number; used_tokens?: number; context_window?: number; ratio?: number } }
  | { event: "compact.summary"; payload: CompactSummaryPayload }
  | { event: "compact.completed"; payload: { agent_id?: number; turn_id?: number } & Partial<CompactSummaryPayload> }
  | { event: "approval.request"; payload: { approval_id: string; detail: Record<string, unknown> } }
  | { event: "approval.response"; payload: { approval_id: string; approved: boolean } }
  | { event: "api.retry"; payload: { attempt: number; wait_ms: number; reason?: string } }
  | { event: "config.changed"; payload: { profile_id: number; changed_keys: string[] } }
  | { event: "scheduled.triggered"; payload: { task_id: number; turn_id: number } }
  | { event: "session.updated"; payload: { session_id: number; title?: string; permission_mode?: string } }
  | { event: "session.completed"; payload: { session_id: number } }
  | { event: "error"; payload: { code?: string; message?: string } }
  /** 服务端对客户端请求的确认（approval/cancel/sync 等） */
  | { event: "ack"; payload: { ref: string; ok?: boolean; resolved?: boolean } }
  /** 断线补偿补发（ws.py 重放缓冲区事件时以原始事件直发，本类型仅作文档标记） */
  | { event: "sync.response"; payload: { last_seq: number; count: number } };

export type ServerEventName = ServerWsEvent["event"];

/**
 * 压缩结果载荷（compact.summary / compact.completed）。
 * 阴影定价：shadowed_range/shadowed_seqs/shadowed_tokens 描述被压缩遮蔽的消息范围，
 * saved_tokens 为压缩节省的 token 数，供前端渲染压缩卡片（对齐 deepseek-harness
 * CompactionResult.shadowedRange/Seqs/TokenCount）。
 */
export interface CompactSummaryPayload {
  agent_id?: number;
  turn_id?: number;
  compaction_id?: string;
  /** v30.1: 压缩块序号（会话内从 1 起，AI compaction_index/view 工具用） */
  index?: number;
  shadowed_range?: [number, number] | null;
  shadowed_seqs?: number[];
  shadowed_tokens?: number;
  saved_tokens?: number;
  summary_message_id?: number;
  summary?: string;
  trigger?: string;
  used_tokens?: number;
  context_window?: number;
  ratio?: number;
}

/** 需要端到端有序的事件（seq 断点重放时前端按序处理） */
export const ORDERED_EVENTS: ReadonlySet<string> = new Set([
  "message.created",
  "token.delta",
  "token.done",
  "thinking.delta",
  "thinking.done",
  "tool.call",
  "tool.result",
  "todo.updated",
  "task.updated",
  "task.proposed",
  "task.planned",
  "turn.started",
  "turn.updated",
  "turn.completed",
  "agent.started",
  "agent.updated",
  "agent.completed",
  "usage.update",
  "compact.started",
  "compact.summary",
  "compact.completed",
  "approval.request",
  "approval.response",
  "api.retry",
]);

// ── 客户端 → 服务端 ──

export type ClientWsEvent =
  | { event: "approval.response"; payload: { approval_id: string; approved: boolean } }
  | { event: "terminal.input"; payload: { id: string; data: string } }
  | { event: "browser.command"; payload: { id: string; cmd: string; payload: Record<string, unknown> } }
  | { event: "cancel"; payload: { turn_id: number } }
  /** 断线补偿请求：重连后带 last_seq 请求补发 */
  | { event: "sync.request"; payload: { last_seq: number } };
