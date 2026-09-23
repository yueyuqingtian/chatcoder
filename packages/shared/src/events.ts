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
  | { event: "turn.started"; payload: { turn_id: number; session_id?: number } }
  | { event: "turn.updated"; payload: { turn_id: number; status: string; session_id?: number } }
  | { event: "turn.completed"; payload: { turn_id: number; summary?: string | null; artifact_ids?: number[]; session_id?: number } }
  | { event: "turn.interrupted"; payload: { turn_id: number; last_message_id?: number | null; session_id?: number } }
  | { event: "turn.failed"; payload: { turn_id: number; status?: string; summary?: string | null; error?: string | null; session_id?: number } }
  | { event: "turn.rolled_back"; payload: { turn_id: number; rolled_back_msgs?: number; file_recovery?: Record<string, unknown> } }
  | { event: "agent.started"; payload: { agent_id: number; kind?: string; name?: string; turn_id?: number | null } }
  | { event: "agent.updated"; payload: { agent_id: number; status?: string; tool?: string; step?: number } }
  | { event: "agent.completed"; payload: { agent_id: number; summary?: string | null; artifact_ids?: number[] } }
  /** plan-330-1648 M7: 子代理失败/取消（主消息流卡片与右面板展示原因） */
  | { event: "subagent.failed"; payload: { agent_id: number; status?: string; error?: string | null } }
  | { event: "thinking.delta"; payload: { agent_id: number; turn_id?: number | null; delta: string } }
  | { event: "thinking.done"; payload: { agent_id: number; turn_id?: number | null; full_text?: string } }
  | { event: "token.delta"; payload: { agent_id: number; turn_id?: number | null; delta: string } }
  | { event: "token.done"; payload: { agent_id: number; turn_id?: number | null; full_text?: string } }
  /** v35: turn 级瞬态状态（重试/恢复提示），前端流式状态行展示；text 空串 = 清除 */
  | { event: "turn.status"; payload: { turn_id: number; thread_id?: number | null; text: string } }
  | { event: "tool.call"; payload: { turn_id: number; agent_id: number; tool: string; args_preview?: string; args_partial?: string } }
  | { event: "tool.result"; payload: { turn_id: number; tool: string; ok: boolean; duration_ms?: number; output_preview?: string; change_stat?: { path: string; additions: number; deletions: number }; call_key?: string } }
  /** v7(B): 运行中工具（terminal_exec 等）逐帧增量输出——前端实时展示 */
  | { event: "tool.output"; payload: { turn_id: number; tool: string; call_key?: string; chunk: string } }
  | { event: "file.change"; payload: { turn_id: number; path?: string } }
  | { event: "todo.updated"; payload: { turn_id: number; todos: unknown[]; persisted: boolean } }
  /** v38 (plan-482): 语义改为「方案文档已生成、等待用户确认」——
   *  不再携带拆分步骤（steps 恒为空，保留字段避免老前端断协议）。 */
  | { event: "task.proposed"; payload: { turn_id: number; request_task_id?: number; group_task_id?: number; reasons?: string[]; plan_doc_path?: string; steps?: unknown[] } }
  | { event: "task.planned"; payload: { turn_id: number; steps?: unknown[] } }
  | { event: "task.updated"; payload: { task_id: number; status?: string; note?: string | null } }
  | { event: "usage.update"; payload: Record<string, unknown> }
  /* plan-282-1441（#8）：调试命中事件——让用户看到"断点停在哪一行"。
     由 services/debug_service 在 AI 调试命中断点时广播。 */
  | { event: "debug.paused"; payload: {
      session_id?: number;
      target: "web" | "java" | string;
      phase: "paused" | "resumed" | "stopped" | "timeout" | string;
      file?: string | null;
      line?: number | null;
      function?: string | null;
      breakpoints?: number;
      hitCount?: number;
      reason?: string | null;
      stack?: Array<{ function?: string; url?: string; line?: number | null; index?: number | null }>;
      variables?: Array<{ name?: string; value?: unknown; type?: string }>;
    } }
  /* plan-282-1441（Arthas 方案）：Java 现场诊断事件。
     走 Attach API，与 IDEA 的 JDWP 调试并存；服务端 arthas_service 在
     attach/提交观测/拉取到命中/断开时广播，右侧「调试」面板据此实时可视化。 */
  | { event: "arthas.event"; payload: {
      session_id?: number;
      phase: "attached" | "detached" | "job_started" | "observed" | "error" | string;
      pid?: number;
      http_port?: number;
      version?: string | null;
      command?: string;
      job_id?: string | number | null;
      summary?: string;
      entries?: Array<{
        type?: string;
        ts?: number;
        cost?: number;
        class?: string;
        method?: string;
        location?: string;
        params?: unknown;
        return_obj?: unknown;
        throwable?: unknown;
        children?: unknown[];
      }>;
      time_expired?: boolean;
      reason?: string;
      at?: number;
    } }
  | { event: "compact.started"; payload: { agent_id?: number; turn_id?: number; used_tokens?: number; context_window?: number; ratio?: number } }
  /* plan-282-1441：上下文回收提示——工具结果折叠/超长结果落盘后告知用户，
     避免"占用突然降几十 k"被误认为丢历史（此前的隐藏压缩）。 */
  | { event: "context.folded"; payload: {
      agent_id?: number;
      agent_name?: string;
      turn_id?: number;
      folded_results?: number;
      est_tokens_saved?: number;
      budget_tokens?: number | null;
      context_window?: number;
    } }
  | { event: "compact.summary"; payload: CompactSummaryPayload }
  | { event: "compact.completed"; payload: { agent_id?: number; turn_id?: number } & Partial<CompactSummaryPayload> }
  | { event: "approval.request"; payload: { approval_id: string; detail: Record<string, unknown> } }
  | { event: "approval.response"; payload: { approval_id: string; approved: boolean } }
  | { event: "api.retry"; payload: { attempt: number; wait_ms: number; reason?: string } }
  /* plan-308-1542 需求3-A：AI 自动合并的实时进度（用户要求"像消息流那样展示 AI 进度、
     工具调用、消息，并汇报结果"）。后端边执行边广播，前端在合并弹窗内实时追加行。 */
  | { event: "merge.progress"; payload: {
      merge_id: string;
      session_id?: number;
      direction?: "to_main" | "from_main" | string;
      phase: "prepare" | "detect" | "file_start" | "tool" | "file_done" | "apply" | "done" | "error" | string;
      path?: string | null;
      index?: number;
      total?: number;
      tool?: string | null;
      detail?: string | null;
      ok?: boolean | null;
      elapsed_ms?: number | null;
      summary?: MergeReport | null;
    } }
  | { event: "config.changed"; payload: { profile_id: number; changed_keys: string[] } }
  | { event: "scheduled.triggered"; payload: { task_id: number; turn_id: number } }
  | { event: "session.updated"; payload: { session_id: number; title?: string; permission_mode?: string; last_activity_at?: string | null } }
  /** plan-547: 运行中 turn 的用户消息注入确认（前端按 request_id 移除排队项） */
  | { event: "user_input.injected"; payload: { turn_id: number; request_id?: string | null } }
  /** plan-671: 目标模式（对齐 zcode goal-continuation）——目标设定/取消/完成/续跑/停止 */
  | { event: "goal.updated"; payload: { text?: string | null; status: string; turns_used?: number } }
  | { event: "goal.completed"; payload: { turn_id?: number | null; summary?: string } }
  | { event: "goal.continued"; payload: { turn_id: number; prev_turn_id?: number; turns_used: number } }
  | { event: "goal.stopped"; payload: { turn_id?: number | null; reason?: string; max_turns?: number } }
  /** v37: turn 结束即摘除会话运行标记；last_activity_at 供侧栏按最近活动重排。
   *  v39: subagent_pending——后台子代理仍在跑时前端保持运行标记并显示等待子代理态。 */
  | { event: "session.completed"; payload: { session_id: number; last_activity_at?: string | null; subagent_pending?: number } }
  /** v39: 后台子代理运行数变化（>0 保持运行态并显示「等待子代理结束…」；=0 摘除） */
  | { event: "subagent.pending"; payload: { session_id: number; pending: number } }
  /** v39: 子代理完成唤醒——服务端自动创建新 turn 送达完成报告（前端渲染分隔线） */
  | { event: "subagent.wakeup"; payload: { session_id: number; turn_id: number } }
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

/**
 * plan-308-1542 需求3-A：AI 自动合并的**汇总报告**（merge.progress 的 done 载荷）。
 * 前端据此渲染"自动合并 N / git 合并 N / AI 解决 N / 失败 N / 跳过 N + 总耗时"报告卡。
 */
export interface MergeReport {
  total: number;
  /** 由 git 干净合入（含单侧改动） */
  git: number;
  /** AI 介入并解决 */
  ai: number;
  /** AI 未能解决 / 真冲突待人工 */
  conflicted: number;
  failed: number;
  skipped: number;
  elapsed_ms: number;
  engine?: string;
  files: Array<{
    path: string;
    result: "git" | "ai" | "manual" | "failed" | "skipped";
    reason?: string;
    ms?: number;
  }>;
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
  "subagent.failed",
  "subagent.pending",
  "subagent.wakeup",
  "usage.update",
  "debug.paused",
  // plan-308-1542：AI 合并进度（有序，保证前端进度行不跳序）
  "merge.progress",
  "compact.started",
  "context.folded",
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
