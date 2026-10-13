/** 右侧「工作流」标签：本会话的运行列表 → 单个运行的详情（目标、子工作流与步骤、待决定的闸门、进展时间线）。
 * 列表与详情都以快照为准；时间线按程序内序号增量拉取，断线后从上次位置补齐。
 * 列表为空时给出两个入口：让嗒嗒规划（发消息给主代理）与从预设开始（生成待确认的骨架）。
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  api,
  type WorkflowAttemptOut,
  type WorkflowEventOut,
  type WorkflowFlowOut,
  type WorkflowNodeOut,
  type WorkflowPoolOut,
  type WorkflowProgramOut,
  type WorkflowRunOut,
} from "../../api/client";
import { IconRingProgress } from "../icons";
import { StatusIcon } from "../iconkit/StatusIcon";
import { usePanelStore } from "../../store/panel";
import { useWorkflowStore } from "../../store/workflow";
import { Badge, Button, DadaLoader, Dialog, Empty, FormDialog, Input, Segmented, Textarea } from "../ui";
import { WorkflowActions } from "./WorkflowActions";
import { WorkflowGate } from "./WorkflowGate";
import { WorkflowStepTrack } from "./WorkflowStepTrack";
import {
  ATTEMPT_MODE_LABEL,
  ATTEMPT_STATUS_LABEL,
  KIND_LABEL,
  currentNote,
  dependencyLabel,
  durationText,
  formatTime,
  isOpenRun,
  nodeStatusView,
  progressOf,
  roleChain,
  runStatusView,
  subflowCount,
} from "./workflowModel";

type ListFilter = "open" | "paused" | "all";

/** 时间线首次加载只取最近的一段，之后按序号向后增量；界面每次露出一页 */
const TIMELINE_TAIL = 60;
const TIMELINE_KEEP = 200;
const TIMELINE_PAGE = 50;

export function WorkflowPanel({ sessionId, visible }: { sessionId: number | null; visible: boolean }) {
  const focusId = useWorkflowStore((s) => s.focusId);
  const focus = useWorkflowStore((s) => s.focus);
  const loadSession = useWorkflowStore((s) => s.loadSession);
  const loadProgram = useWorkflowStore((s) => s.loadProgram);
  const runs = useWorkflowStore((s) => (sessionId == null ? undefined : s.bySession[sessionId]));
  const snap = useWorkflowStore((s) => (focusId == null ? undefined : s.snapshots[focusId]));
  const error = useWorkflowStore((s) => s.error);
  const clearError = useWorkflowStore((s) => s.clearError);
  const pool = useWorkflowStore((s) => s.pool);
  const [filter, setFilter] = useState<ListFilter>("open");

  useEffect(() => {
    if (visible && sessionId != null) void loadSession(sessionId);
  }, [visible, sessionId, loadSession]);
  useEffect(() => {
    if (visible && focusId != null) void loadProgram(focusId);
  }, [visible, focusId, loadProgram]);

  let body: ReactNode;
  if (sessionId == null) {
    body = (
      <Empty
        size="sm"
        mood="calm"
        title="打开一个会话后查看工作流"
        desc="工作流跟随会话展示，切换到对应会话即可看到它的运行"
      />
    );
  } else if (focusId != null && snap == null) {
    body = (
      <div className="wf-panel-loading">
        <DadaLoader label="正在加载工作流…" inline size={28} />
        <Button variant="ghost" size="xs" onClick={() => focus(null)}>返回列表</Button>
      </div>
    );
  } else if (snap != null && sessionId != null && snap.run.session_id === sessionId && focusId != null) {
    body = <WorkflowDetail snap={snap} onBack={() => focus(null)} />;
  } else {
    body = (
      <WorkflowList
        sessionId={sessionId}
        runs={runs}
        filter={filter}
        onFilter={setFilter}
        onOpen={focus}
        loadFailed={runs === undefined && error != null}
        onRetry={() => { if (sessionId != null) void loadSession(sessionId); }}
      />
    );
  }

  return (
    <div className="wf-panel">
      {error && (
        <div className="wf-error" role="alert">
          <span className="wf-error-text">{error}</span>
          <Button variant="ghost" size="xs" onClick={clearError}>知道了</Button>
        </div>
      )}
      {pool && <PoolBar pool={pool} sessionId={sessionId} />}
      {body}
    </div>
  );
}

const limitText = (n: number) => (n === 0 ? "不限" : String(n));

/** 并发占用：全局与本会话的占用 / 上限，排队数；上限为 0（不限）时不画进度条 */
function PoolBar({ pool, sessionId }: { pool: WorkflowPoolOut; sessionId: number | null }) {
  const used = pool.global.in_use;
  const limit = pool.global.limit;
  const sessionUsed = sessionId != null ? (pool.session.in_use[String(sessionId)] ?? 0) : 0;
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  return (
    <div className="wf-pool" aria-label="并发占用">
      <div className="wf-pool-main">
        <span className="wf-pool-label">并发</span>
        <span className="wf-pool-count">{used}/{limitText(limit)}</span>
        <span className="wf-pool-sep" aria-hidden>·</span>
        <span className="wf-pool-label">本会话</span>
        <span className="wf-pool-count">{sessionUsed}/{limitText(pool.session.limit)}</span>
        {pool.queued > 0 && <Badge tone="info" variant="soft" size="xs">排队 {pool.queued}</Badge>}
      </div>
      {limit > 0 && (
        <span className="wf-pool-bar" aria-hidden>
          <span className="wf-pool-fill" style={{ width: `${pct}%` }} />
        </span>
      )}
    </div>
  );
}

function formatTokens(n: number): string {
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`;
  return n.toLocaleString("zh-CN");
}

/** 单次运行的 token 预算进度：超过预算时以警示色显示（运行随即暂停） */
function BudgetBar({ used, budget }: { used: number; budget: number }) {
  const pct = Math.min(100, Math.round((used / budget) * 100));
  return (
    <div className="wf-budget" data-over={used >= budget ? "true" : undefined}>
      <span className="wf-budget-text">token {formatTokens(used)} / 预算 {formatTokens(budget)}</span>
      <span className="wf-budget-bar" aria-hidden>
        <span className="wf-budget-fill" style={{ width: `${pct}%` }} />
      </span>
    </div>
  );
}

function WorkflowList({ sessionId, runs, filter, onFilter, onOpen, loadFailed, onRetry }: {
  sessionId: number;
  runs: WorkflowRunOut[] | undefined;
  filter: ListFilter;
  onFilter: (f: ListFilter) => void;
  onOpen: (programId: number) => void;
  loadFailed: boolean;
  onRetry: () => void;
}) {
  if (runs === undefined) {
    return loadFailed ? (
      <Empty
        size="sm"
        mood="oops"
        title="工作流暂时无法加载"
        desc="请确认后端服务已启动"
        action={<Button variant="secondary" size="xs" onClick={onRetry}>重试</Button>}
      />
    ) : (
      <DadaLoader label="正在加载工作流…" inline size={28} />
    );
  }
  if (runs.length === 0) {
    return (
      <Empty
        title="还没有工作流"
        desc="复杂任务时嗒嗒会自动规划工作流：先给出计划，你确认后再按步骤执行。也可以从预设开始，编辑后再执行。"
        size="md"
        action={<WorkflowStarters sessionId={sessionId} />}
      />
    );
  }
  const counts = {
    open: runs.filter(isOpenRun).length,
    paused: runs.filter((r) => r.status === "paused").length,
    all: runs.length,
  };
  const shown = runs.filter((r) => {
    if (filter === "all") return true;
    if (filter === "paused") return r.status === "paused";
    return isOpenRun(r);
  });
  return (
    <div className="wf-list-wrap">
      <div className="wf-toolbar">
        <Segmented<ListFilter>
          size="sm"
          aria-label="筛选工作流"
          value={filter}
          onChange={onFilter}
          options={[
            { value: "open", label: "进行中", count: counts.open },
            { value: "paused", label: "已暂停", count: counts.paused },
            { value: "all", label: "全部", count: counts.all },
          ]}
        />
      </div>
      {shown.length === 0 ? (
        <Empty size="sm" mood="think" title="这个分类下没有工作流" />
      ) : (
        <ul className="wf-list" aria-label="工作流列表">
          {shown.map((run) => (
            <li key={run.id}>
              <WorkflowRow run={run} onOpen={onOpen} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 列表为空时的两个入口：让嗒嗒规划（默认）与从预设开始 */
function WorkflowStarters({ sessionId }: { sessionId: number }) {
  const [presetOpen, setPresetOpen] = useState(false);
  const askPlanner = async () => {
    // 惰性加载聊天模块，避免面板与聊天之间的循环依赖
    const { useChatStore } = await import("../../store/chat");
    await useChatStore.getState().sendTurn("请为当前任务规划工作流：先说明拆分思路，再用 plan_workflow 给出计划。");
  };
  return (
    <div className="wf-starters">
      <Button variant="primary" size="sm" onClick={() => void askPlanner()}>让嗒嗒规划</Button>
      <Button variant="secondary" size="sm" onClick={() => setPresetOpen(true)}>从预设开始</Button>
      <PresetPicker sessionId={sessionId} open={presetOpen} onClose={() => setPresetOpen(false)} />
    </div>
  );
}

/** 预设骨架：生成一个待确认的工作流，之后可在卡片里替换子工作流的模板，确认后再开始 */
function PresetPicker({ sessionId, open, onClose }: { sessionId: number; open: boolean; onClose: () => void }) {
  const templates = useWorkflowStore((s) => s.templates);
  const loadTemplates = useWorkflowStore((s) => s.loadTemplates);
  const startFromPreset = useWorkflowStore((s) => s.startFromPreset);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  useEffect(() => {
    if (open && templates === null) void loadTemplates();
  }, [open, templates, loadTemplates]);

  const presets = (templates ?? []).filter((t) => t.source === "preset");
  return (
    <Dialog
      open={open}
      onClose={onClose}
      width={520}
      title="从预设开始"
      subtitle="生成一个待确认的工作流：可以先替换子工作流的模板，确认后再开始执行"
      footer={<Button variant="ghost" size="sm" onClick={onClose}>关闭</Button>}
    >
      <ul className="wf-preset-list" aria-label="预设骨架">
        {presets.map((t) => {
          const flows = (t.plan?.flows ?? []) as Array<{ nodes?: Array<{ kind: string }> }>;
          const chain = flows[0]?.nodes ? roleChain(flows[0].nodes) : "";
          return (
            <li key={t.key} className="wf-preset">
              <div className="wf-preset-main">
                <span className="wf-preset-name">{t.name}</span>
                {t.description && <span className="wf-preset-desc">{t.description}</span>}
                <span className="wf-preset-chain">
                  {flows.length} 个子工作流{chain ? ` · ${chain}` : ""}
                </span>
              </div>
              <Button
                variant="secondary"
                size="xs"
                disabled={busyKey !== null}
                onClick={async () => {
                  setBusyKey(t.key);
                  const id = await startFromPreset(sessionId, t.key);
                  setBusyKey(null);
                  if (id != null) onClose();
                }}
              >
                {busyKey === t.key ? "生成中…" : "用这套开始"}
              </Button>
            </li>
          );
        })}
        {templates !== null && presets.length === 0 && (
          <li className="wf-preset-empty">暂时没有可用的预设</li>
        )}
      </ul>
    </Dialog>
  );
}

function WorkflowRow({ run, onOpen }: { run: WorkflowRunOut; onOpen: (programId: number) => void }) {
  const view = runStatusView(run.status, run.closed_reason);
  const progress = run.progress;
  const ratio = progress && progress.total > 0 ? progress.done / progress.total : 0;
  const duration = durationText(run.started_at, run.finished_at, Date.now());
  const flows = subflowCount(run);
  return (
    <button type="button" className="wf-run" data-status={run.status} onClick={() => onOpen(run.id)}>
      <span className="wf-run-main">
        <span className="wf-run-title">{run.title || "未命名工作流"}</span>
        <Badge tone={view.tone} variant={view.variant} size="xs">{view.label}</Badge>
      </span>
      <span className="wf-run-foot">
        <span className="wf-run-progress">
          <IconRingProgress
            value={ratio}
            size={20}
            strokeWidth={2.5}
            indicatorColor="var(--info)"
            trackColor="var(--border)"
          />
          <span className="wf-run-count">{progress?.done ?? 0}/{progress?.total ?? 0} 步</span>
        </span>
        <span className="wf-run-meta">
          {flows > 0 && `${flows} 个子工作流`}
          {duration && `${flows > 0 ? " · " : ""}耗时 ${duration}`}
        </span>
        <span className="wf-run-time">{formatTime(run.updated_at ?? run.started_at)}</span>
      </span>
    </button>
  );
}

function WorkflowDetail({ snap, onBack }: { snap: WorkflowProgramOut; onBack: () => void }) {
  const { run } = snap;
  const view = runStatusView(run.status, run.closed_reason);
  const { done, total } = progressOf(snap);
  const saveTemplate = useWorkflowStore((s) => s.saveTemplate);
  const pool = useWorkflowStore((s) => s.pool);
  const [templateOpen, setTemplateOpen] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [templateNote, setTemplateNote] = useState("");
  const [savedName, setSavedName] = useState<string | null>(null);
  const poolText = pool
    ? `并发 ${pool.global.in_use}/${limitText(pool.global.limit)} · 排队 ${pool.queued}`
    : "";
  const stats = [
    `${done}/${total} 步`,
    run.rework_total > 0 ? `返工 ${run.rework_total} 次` : "",
    run.resume_count > 0 ? `已续跑 ${run.resume_count} 次` : "",
    run.started_at ? `开始于 ${formatTime(run.started_at)}` : "",
    poolText,
  ].filter(Boolean).join(" · ");
  return (
    <div className="wf-detail">
      <div className="wf-detail-top">
        <Button variant="ghost" size="xs" onClick={onBack}>返回列表</Button>
        <span className="wf-detail-top-end">
          {savedName && <span className="wf-saved-hint" role="status">已保存为模板「{savedName}」</span>}
          <Button
            variant="ghost"
            size="xs"
            disabled={!run.graph}
            onClick={() => {
              setTemplateName(run.title || "");
              setTemplateNote("");
              setTemplateOpen(true);
            }}
          >
            存为模板
          </Button>
        </span>
      </div>
      <header className="wf-detail-head">
        <div className="wf-detail-titles">
          <h3 className="wf-detail-title">{run.title || "未命名工作流"}</h3>
          <div className="wf-detail-meta">{stats}</div>
        </div>
        <Badge tone={view.tone} variant={view.variant} size="sm">{view.label}</Badge>
      </header>
      {run.goal && (
        <details className="wf-detail-goal">
          <summary>目标</summary>
          <p>{run.goal}</p>
        </details>
      )}
      {run.token_budget > 0 && <BudgetBar used={run.token_used} budget={run.token_budget} />}
      <p className="wf-detail-note" aria-live="polite">{currentNote(snap)}</p>
      {run.rationale && <p className="wf-detail-rationale">{run.rationale}</p>}
      <div className="wf-detail-actions">
        <WorkflowActions snap={snap} />
      </div>
      <WorkflowGate snap={snap} />
      <section className="wf-detail-flows" aria-label="子工作流">
        {snap.flows.map((flow) => (
          <FlowBlock key={flow.run.id} snap={snap} flow={flow} />
        ))}
      </section>
      <WorkflowTimeline programId={run.id} lastSeq={run.last_seq} />
      <FormDialog
        open={templateOpen}
        onClose={() => setTemplateOpen(false)}
        title="存为模板"
        subtitle="保存后可在「设置 → 工作流」中管理，之后新的工作流可直接复用这套步骤"
        submitLabel="保存"
        submitDisabled={!templateName.trim()}
        width={460}
        onSubmit={async () => {
          const saved = await saveTemplate(run.id, templateName.trim(), templateNote.trim());
          if (saved) {
            setSavedName(saved.name);
            setTemplateOpen(false);
          }
        }}
      >
        <div className="wf-template-form">
          <label className="wf-gate-field">
            <span className="wf-gate-field-label">名称</span>
            <Input value={templateName} onChange={(e) => setTemplateName(e.target.value)} placeholder="例如：后端接口改造" />
          </label>
          <label className="wf-gate-field">
            <span className="wf-gate-field-label">说明（可选）</span>
            <Textarea rows={2} value={templateNote} onChange={(e) => setTemplateNote(e.target.value)} placeholder="这套步骤适合什么样的任务" />
          </label>
        </div>
      </FormDialog>
    </div>
  );
}

function FlowBlock({ snap, flow }: { snap: WorkflowProgramOut; flow: WorkflowFlowOut }) {
  const dep = dependencyLabel(snap, flow.run.package_key);
  return (
    <div className="wf-flow">
      <div className="wf-flow-head">
        <span className="wf-flow-name">{flow.run.title || flow.run.package_key}</span>
        {dep && <span className="wf-flow-dep">{dep}</span>}
      </div>
      <WorkflowStepTrack nodes={flow.nodes} />
      <ul className="wf-node-list" aria-label="步骤明细">
        {flow.nodes.map((node) => (
          <NodeRow key={node.id} programId={snap.run.id} flowKey={flow.run.package_key ?? ""} node={node} />
        ))}
      </ul>
    </div>
  );
}

function NodeRow({ programId, flowKey, node }: { programId: number; flowKey: string; node: WorkflowNodeOut }) {
  const view = nodeStatusView(node.status);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [attempts, setAttempts] = useState<WorkflowAttemptOut[] | null>(null);
  const sub = [
    KIND_LABEL[node.kind] ?? node.kind,
    node.role,
    node.attempt_no > 1 ? `第 ${node.attempt_no} 次` : "",
  ].filter(Boolean).join(" · ");

  useEffect(() => {
    if (!historyOpen || attempts !== null) return;
    let cancelled = false;
    void api.workflowNodeAttempts(programId, flowKey, node.key)
      .then((rows) => { if (!cancelled) setAttempts(rows); })
      .catch(() => { if (!cancelled) setAttempts([]); });
    return () => { cancelled = true; };
  }, [historyOpen, attempts, programId, flowKey, node.key]);

  const content = (
    <>
      <span className="wf-node-ic">
        <StatusIcon status={view.icon} size={14} label={view.label} />
      </span>
      <span className="wf-node-main">
        <span className="wf-node-title">{node.title}</span>
        <span className="wf-node-sub">{sub}</span>
      </span>
      <span className="wf-node-status">{view.label}</span>
    </>
  );
  return (
    <li className="wf-node-row" data-status={view.icon}>
      {node.agent_id != null ? (
        <button
          type="button"
          className="wf-node-btn"
          onClick={() => {
            if (node.agent_id != null) usePanelStore.getState().openSubagent(node.agent_id, node.title);
          }}
        >
          {content}
        </button>
      ) : (
        <div className="wf-node-btn is-static">{content}</div>
      )}
      {node.reason && <div className="wf-node-reason">{node.reason}</div>}
      {node.attempt_no > 0 && (
        <button
          type="button"
          className="wf-attempts-toggle"
          aria-expanded={historyOpen}
          onClick={() => setHistoryOpen((v) => !v)}
        >
          {historyOpen ? "收起尝试记录" : `尝试记录 · ${node.attempt_no} 次`}
        </button>
      )}
      {historyOpen && (
        <AttemptHistory attempts={attempts} />
      )}
    </li>
  );
}

/** 尝试历史（设计 §7.6）：每次尝试的序号、模式、结果、时间与原因 */
export function AttemptHistory({ attempts }: { attempts: WorkflowAttemptOut[] | null }) {
  if (attempts === null) return <div className="wf-attempts-state">正在读取…</div>;
  if (attempts.length === 0) return <div className="wf-attempts-state">暂无记录</div>;
  return (
    <ol className="wf-attempts" aria-label="尝试记录">
      {attempts.map((a) => (
        <li key={a.attempt_no} className="wf-attempt" data-status={a.status}>
          <span className="wf-attempt-head">
            第 {a.attempt_no} 次 · {ATTEMPT_MODE_LABEL[a.mode] ?? a.mode} · {ATTEMPT_STATUS_LABEL[a.status] ?? a.status}
          </span>
          <span className="wf-attempt-time">{formatTime(a.started_at)}</span>
          {a.reason && <span className="wf-attempt-reason">{a.reason}</span>}
        </li>
      ))}
    </ol>
  );
}

function eventText(ev: WorkflowEventOut): string {
  const p = ev.payload;
  switch (ev.type) {
    case "workflow.log.appended":
      return String(p.text ?? "");
    case "workflow.node.updated":
      return `${ev.node_key ?? "步骤"} · ${nodeStatusView(String(p.status ?? "")).label}`;
    case "workflow.run.updated":
      return `运行状态：${runStatusView(String(p.status ?? "")).label}`;
    case "workflow.gate.opened":
      return `等待你的决定：${ev.node_key ?? ""}`;
    default:
      return ev.type;
  }
}

function WorkflowTimeline({ programId, lastSeq }: { programId: number; lastSeq: number }) {
  const [events, setEvents] = useState<WorkflowEventOut[]>([]);
  const [visible, setVisible] = useState(TIMELINE_PAGE);
  // 已拉取到的程序内序号；-1 表示尚未初始化（首次只取最近一段）
  const cursor = useRef(-1);

  useEffect(() => {
    cursor.current = -1;
    setEvents([]);
    setVisible(TIMELINE_PAGE);
  }, [programId]);

  useEffect(() => {
    if (lastSeq <= 0 || cursor.current >= lastSeq) return;
    let cancelled = false;
    const from = cursor.current < 0 ? Math.max(0, lastSeq - TIMELINE_TAIL) : cursor.current;
    void api.workflowEvents(programId, from)
      .then((rows) => {
        if (cancelled || rows.length === 0) return;
        cursor.current = rows[rows.length - 1].seq;
        setEvents((prev) => [...prev, ...rows].slice(-TIMELINE_KEEP));
      })
      .catch(() => { /* 时间线是辅助信息，失败不打断面板 */ });
    return () => { cancelled = true; };
  }, [programId, lastSeq]);

  const ordered = events.slice().reverse();
  return (
    <section className="wf-timeline" aria-label="进展时间线">
      <h4 className="wf-section-title">进展</h4>
      {events.length === 0 ? (
        <Empty size="sm" mood="calm" title="还没有进展记录" />
      ) : (
        <>
          <ol className="wf-events">
            {ordered.slice(0, visible).map((ev) => (
              <li key={ev.seq} className="wf-event">
                <span className="wf-event-time">{formatTime(ev.created_at)}</span>
                <span className="wf-event-text">{eventText(ev)}</span>
              </li>
            ))}
          </ol>
          {ordered.length > visible && (
            <Button variant="ghost" size="xs" onClick={() => setVisible((v) => v + TIMELINE_PAGE)}>
              加载更多（还有 {ordered.length - visible} 条）
            </Button>
          )}
        </>
      )}
    </section>
  );
}
