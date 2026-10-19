/** v20: 子代理详情面板——壳 + 头部，消息体交给共享 message-flow 插件（source="subagent"）。
 * 数据组装（REST 历史 + 实时桶合并去重）/ 加载态 / 滚动 / 流式渲染全部下沉到消息流插件，
 * 与主界面共用同一注册插件（参考 deepseek-harness「同一渲染引擎 + 数据注入」模式）。
 * 头部显示状态文案（执行中/已完成/失败），对齐消息流子代理卡片图标体系。
 *
 * 【头部收敛】工作流节点的头部只留两行：所属路径 + 尝试 / 评估结论 + 详情开关，以及一行摘要。
 * 计划、验证、评估问题与尝试记录收在“详情”里按需展开；展开区有高度上限并在内部滚动，
 * 不把消息流压没。补充说明输入移到面板底部，与主代理输入框同一位置、同一视觉。
 */
import { useEffect, useRef, useState } from "react";
import { api, type WorkflowAttemptOut } from "../../api/client";
import { AttemptHistory } from "../workflow/WorkflowPanel";
import { IssueList } from "../workflow/WorkflowGate";
import { subscribeTick } from "../chat/useRunningTicker";
import { useChatStore } from "../../store/chat";
import { PluginSlot } from "../../plugins/registry";
import { parseUtc } from "../../utils/time";
import { IconCheck, IconChevronDown, IconCpu, IconSpinner, IconX } from "../icons";
import { Button, Empty, Tooltip } from "../ui";
import { IconSendStop } from "../iconkit/MorphIcons";
import type { WorkflowNodeBrief } from "../../api/client";

/** 评估结论的展示文案（颜色由 data-verdict 决定） */
const VERDICT_LABEL: Record<string, string> = {
  pass: "评估通过",
  rework: "评估要求返工",
  escalate: "需要你决定",
};

/** 摘要的一行预览：去掉代码块与 Markdown 标记并压成单行（完整内容在消息流里） */
function summaryLine(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[#*`>~|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** 工作流节点头：第一行是所属路径（完整显示，超出才省略）；第二行是尝试次数、评估结论与“详情”开关；
 * 摘要、计划、验证、评估问题与尝试记录都收在详情里，默认不占消息流的空间 */
function WorkflowNodeHead({ wf }: { wf: WorkflowNodeBrief }) {
  const trail = [wf.program_title, wf.flow_title, wf.node_title].filter(Boolean).join(" › ");
  const [detailOpen, setDetailOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [attempts, setAttempts] = useState<WorkflowAttemptOut[] | null>(null);
  const canHistory = wf.program_id != null && !!wf.flow_key;
  const plan = wf.detail?.plan ?? [];
  const verification = wf.detail?.verification ?? [];
  const issues = wf.detail?.issues ?? [];
  const summary = wf.summary ? summaryLine(wf.summary) : "";
  const hasDetail = !!summary || plan.length > 0 || verification.length > 0 || issues.length > 0 || canHistory;
  const attemptNo = Math.max(1, wf.attempt_no);
  const headRef = useRef<HTMLDivElement>(null);

  // 详情是浮层：展开时不挤压下方消息流，点击外部或按 Esc 收起（与其它浮层一致）
  useEffect(() => {
    if (!detailOpen) return;
    const onDown = (e: PointerEvent) => {
      if (headRef.current && !headRef.current.contains(e.target as Node)) setDetailOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setDetailOpen(false); };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [detailOpen]);

  useEffect(() => {
    if (!historyOpen || !canHistory || attempts !== null || wf.program_id == null) return;
    let cancelled = false;
    void api.workflowNodeAttempts(wf.program_id, wf.flow_key, wf.node_key)
      .then((rows) => { if (!cancelled) setAttempts(rows); })
      .catch(() => { if (!cancelled) setAttempts([]); });
    return () => { cancelled = true; };
  }, [historyOpen, canHistory, attempts, wf.program_id, wf.flow_key, wf.node_key]);

  return (
    <div ref={headRef} className="subagent-wf-head" aria-label="工作流节点">
      <Tooltip content={trail} side="bottom" delay={400}>
        <span className="subagent-wf-trail">{trail}</span>
      </Tooltip>
      <div className="subagent-wf-bar">
        <span className="subagent-wf-chips">
          <span className="subagent-wf-chip">第 {attemptNo} 次尝试</span>
          {wf.verdict && (
            <span className="subagent-wf-chip" data-verdict={wf.verdict}>{VERDICT_LABEL[wf.verdict] ?? wf.verdict}</span>
          )}
        </span>
        {hasDetail && (
          <Button
            variant="ghost"
            size="xs"
            className="subagent-wf-toggle"
            aria-expanded={detailOpen}
            aria-controls={`wf-detail-${wf.node_id}`}
            onClick={() => setDetailOpen((v) => !v)}
          >
            {issues.length > 0 ? `${issues.length} 条问题` : "详情"}
            <IconChevronDown size={12} className="subagent-wf-caret" />
          </Button>
        )}
      </div>
      {detailOpen && hasDetail && (
        <div className="subagent-wf-detail" id={`wf-detail-${wf.node_id}`}>
          {summary && <p className="subagent-wf-brief">{summary}</p>}
          {(wf.mode || wf.rework_round > 0) && (
            <p className="subagent-wf-meta">
              {wf.mode && <span>模式 {wf.mode}</span>}
              {wf.rework_round > 0 && <span>返工 {wf.rework_round} 次</span>}
            </p>
          )}
          {plan.length > 0 && (
            <div className="subagent-wf-section">
              <span className="subagent-wf-section-title">计划</span>
              <ol className="subagent-wf-list">{plan.map((item, i) => <li key={i}>{item}</li>)}</ol>
            </div>
          )}
          {verification.length > 0 && (
            <div className="subagent-wf-section">
              <span className="subagent-wf-section-title">验证</span>
              <ul className="subagent-wf-list">{verification.map((item, i) => <li key={i}>{item}</li>)}</ul>
            </div>
          )}
          {issues.length > 0 && (
            <div className="subagent-wf-section">
              <span className="subagent-wf-section-title">评估问题</span>
              <IssueList issues={issues} limit={3} />
            </div>
          )}
          {canHistory && (
            <div className="subagent-wf-section">
              <Button
                variant="ghost"
                size="xs"
                aria-expanded={historyOpen}
                onClick={() => setHistoryOpen((v) => !v)}
              >
                {historyOpen ? "收起尝试记录" : `尝试记录 · ${attemptNo} 次`}
              </Button>
              {historyOpen && <AttemptHistory attempts={attempts} />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** 运行中节点的补充说明：面板底部的输入卡，与主代理输入框同一位置与视觉，只保留输入与发送 */
function NodeNoteComposer({ wf }: { wf: WorkflowNodeBrief }) {
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [state, setState] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const submit = async () => {
    const text = note.trim();
    if (!text || sending || wf.program_id == null) return;
    setSending(true);
    setState(null);
    try {
      await api.addWorkflowNodeNote(wf.program_id, wf.flow_key, wf.node_key, text);
      setNote("");
      setState({ tone: "ok", text: "已加入，下一步生效" });
    } catch (err) {
      setState({ tone: "error", text: err instanceof Error ? err.message : "发送失败" });
    } finally {
      setSending(false);
    }
  };

  return (
    <form
      className="subagent-note"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="subagent-note-card">
        <textarea
          className="subagent-note-input"
          aria-label="补充说明（下一步生效）"
          rows={1}
          value={note}
          disabled={sending}
          placeholder="补充说明，下一步生效"
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => {
            // 与主代理输入框一致：Enter 发送，Shift+Enter 换行；输入法组字中不发送
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <Tooltip content="发送（Enter）" side="top">
          <button
            type="submit"
            className="cc-send is-send"
            disabled={!note.trim() || sending}
            aria-label="发送补充说明"
          >
            <IconSendStop mode="send" size={16} />
          </button>
        </Tooltip>
      </div>
      {state && <span className="subagent-note-state" data-tone={state.tone} role="status">{state.text}</span>}
    </form>
  );
}

export function SubagentPanel({ threadId, agentName, visible = true }: {
  threadId?: number;
  agentName?: string;
  /** plan-75-334 阶段3：面板是否可见。隐藏时仅暂停秒级计时与流式视觉更新，
   *  线程数据、消息与滚动位置全部保留（恢复可见时补算一次）。 */
  visible?: boolean;
}) {
  const meta = useChatStore((s) => (threadId != null ? s.subagentMeta[threadId] : undefined));
  const liveCount = useChatStore((s) => (threadId != null ? s.subagentMessages[threadId]?.length ?? 0 : 0));
  const running = meta?.status === "running" || meta?.status === "in_progress";
  const failed = meta?.status === "failed" || meta?.status === "cancelled";
  const done = meta?.status === "done";

  // v36 (plan-321-1600 M2): 头部“用时”在运行中需每秒刷新（否则计时不动）。
  // S8c：订阅共享秒级 ticker（与 ToolTree / WorkTimer 共用同一个 setInterval）。
  // plan-75-334 阶段3：面板不可见时不订阅——隐藏标签不再每秒 setState。
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!running || !visible) return;
    setNowMs(Date.now()); // 恢复可见时先对齐一次，再开始秒级刷新
    return subscribeTick((now) => setNowMs(now));
  }, [running, visible]);

  // v36: 用时 = 起止时间差（运行中取当前时间；无起始时间时不展示）
  const startedMs = meta?.startedAt ? parseUtc(meta.startedAt) : null;
  const endedMs = meta?.endedAt ? parseUtc(meta.endedAt) : null;
  let elapsedLabel = "";
  if (startedMs != null) {
    const sec = Math.max(0, Math.round(((endedMs ?? nowMs) - startedMs) / 1000));
    const min = Math.floor(sec / 60);
    elapsedLabel = min > 0 ? `${min} 分 ${sec % 60} 秒` : `${sec} 秒`;
  }

  if (threadId == null) return <Empty size="sm" title="未指定子代理" />;

  // plan-20261019-subagent-ghost: 没有元信息、或已结束却没有任何线程消息的条目，
  // 说明它没有可查看的过程（典型来源：历史版本把主代理完成事件误记成「子代理 #N」）。
  // 旧实现仍然渲染一个空消息流，用户点开只看到一片空白，分不清是加载失败还是记录不存在。
  // 只在「消息数为 0」或「没有任何归属锚点且计数尚未回填」时判定为空——
  // 有 turn/task 归属、计数还没回填的条目先交给消息流的加载态，避免误报。
  const hasAnchor = meta != null && (meta.turnId != null || meta.taskId != null
    || meta.programId != null || meta.spawnMessageId != null);
  const noOutput = !running && liveCount === 0
    && (meta?.messageCount === 0 || (!hasAnchor && meta?.messageCount == null));
  if (!meta || noOutput) {
    return (
      <div className="subagent-panel">
        <div className="subagent-panel-top">
          <div className="subagent-panel-head">
            <div className="subagent-panel-title">
              <span className="subagent-panel-icon"><IconCpu size={13} /></span>
              <span className="subagent-panel-name">{agentName || meta?.name || `子代理 #${threadId}`}</span>
              <span className="subagent-panel-count">无记录</span>
            </div>
          </div>
        </div>
        <div className="subagent-panel-body">
          <Empty
            size="sm"
            title="这个子代理没有可查看的过程"
            desc="它可能已被清理，或不属于当前会话。可以关闭此标签，主会话的执行不受影响。"
          />
        </div>
      </div>
    );
  }

  const wf = meta?.workflow;
  // 与主代理输入框对齐：补充说明只在节点仍在执行时提供（完成后不再出现输入卡）
  const canNote = !!wf && wf.program_id != null && !!wf.flow_key && (wf.status === "running" || wf.status === "recovering");

  return (
    <div className="subagent-panel">
      <div className="subagent-panel-top">
        <div className="subagent-panel-head">
          <div className="subagent-panel-title">
            <span className="subagent-panel-icon"><IconCpu size={13} /></span>
            <span className="subagent-panel-name">{agentName || meta?.name || `子代理 #${threadId}`}</span>
            <span className="subagent-panel-count">{liveCount} 条消息</span>
          </div>
          {/* 第二行：状态与元信息。图标与状态文字同包一组、不换行；窄面板下按项省略，不把图标拆到另一行 */}
          <div className="subagent-panel-facts">
            <span className="subagent-panel-status" data-state={running ? "wait" : done ? "ok" : failed ? "fail" : "idle"}>
              {running && <IconSpinner size={11} />}
              {done && <IconCheck size={11} />}
              {failed && <IconX size={11} />}
              {/* 失败原因已写在状态文案里，不再用原生 title 重复提示 */}
              <span className="subagent-panel-status-text">
                {running ? "执行中…" : done ? "已完成"
                  : failed ? (meta?.error ? `失败：${meta.error}` : "失败")
                  : meta?.status ?? ""}
              </span>
            </span>
            {/* v36 (plan-321-1600 M2): 用时 / 变更文件数 / token 用量 */}
            {elapsedLabel && (
              <span className="subagent-panel-meta">{running ? "已用 " : "用时 "}{elapsedLabel}</span>
            )}
            {meta?.filesCount ? <span className="subagent-panel-meta">{meta.filesCount} 个文件</span> : null}
            {meta?.tokens ? <span className="subagent-panel-meta">{meta.tokens} tokens</span> : null}
          </div>
        </div>
        {wf && <WorkflowNodeHead wf={wf} />}
      </div>
      <div className="subagent-panel-body">
        {/* v20: 消息体共享 message-flow 插件（source=subagent，操作仅复制）
            plan-75-334 阶段3：透传 visible——隐藏时不推进流式视觉更新，
            恢复可见时以当前缓冲一次性对齐（历史消息与线程数据不丢）。 */}
        <PluginSlot slot="message-flow" source="subagent" threadId={threadId} visible={visible} className="subagent-flow" />
      </div>
      {canNote && wf && <NodeNoteComposer wf={wf} />}
    </div>
  );
}
