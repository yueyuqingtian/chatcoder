/** 等待用户决定的闸门：原因、评估问题清单、冲突三栏对照、越界改动清单或并入冲突清单，
 * 可选说明与决定按钮。闸门只在用户确认后才会推进；中止属于破坏性操作，先经二次确认。
 */
import { useState } from "react";
import type { WorkflowProgramOut } from "../../api/client";
import { ConfirmDialog } from "../ConfirmDialog";
import { useWorkflowStore, type GateDecision } from "../../store/workflow";
import { Badge, Button, Textarea } from "../ui";
import { gateOf } from "./workflowModel";

interface IssueItem {
  severity?: string;
  file?: string;
  line?: number | null;
  problem?: string;
  fix_hint?: string;
}

/** 节点上等待决定的载荷：写入冲突（conflict）、越界改动（scope_violation）、并入主工作区（integration） */
interface GatePayload {
  kind?: string;
  path?: string;
  base?: string;
  current?: string;
  pending?: string;
  reason?: string;
  message?: string;
  files?: string[];
  paths?: string[];
  command?: string;
}

const SEVERITY_LABEL: Record<string, string> = { blocker: "阻塞", major: "重要", minor: "次要" };
const PANE_LIMIT = 1600;
const PATH_LIMIT = 8;

function ConflictPanes({ conflict }: { conflict: GatePayload }) {
  const panes: Array<{ key: string; title: string; hint: string; text: string }> = [
    { key: "base", title: "读取时", hint: "基线", text: conflict.base ?? "" },
    { key: "current", title: "当前磁盘", hint: "其它节点的改动", text: conflict.current ?? "" },
    { key: "pending", title: "本次待写入", hint: "本节点想写入的内容", text: conflict.pending ?? "" },
  ];
  return (
    <div className="wf-conflict" role="group" aria-label="冲突对照">
      {conflict.path && <div className="wf-conflict-path">{conflict.path}</div>}
      <div className="wf-conflict-panes">
        {panes.map((pane) => (
          <figure key={pane.key} className="wf-conflict-pane">
            <figcaption className="wf-conflict-cap">
              <span className="wf-conflict-title">{pane.title}</span>
              <span className="wf-conflict-hint">{pane.hint}</span>
            </figcaption>
            <pre className="wf-conflict-body">
              {pane.text ? pane.text.slice(0, PANE_LIMIT) : "（空）"}
              {pane.text.length > PANE_LIMIT ? "\n…" : ""}
            </pre>
          </figure>
        ))}
      </div>
    </div>
  );
}

/** 越界改动 / 冲突文件清单：命令（若有）与前若干个路径，其余折叠为数量 */
function PathList({ label, paths, command }: { label: string; paths: string[]; command?: string }) {
  return (
    <div className="wf-scope" role="group" aria-label={label}>
      {command && (
        <div className="wf-scope-cmd-row">
          <span className="wf-gate-field-label">执行的命令</span>
          <code className="wf-scope-cmd">{command}</code>
        </div>
      )}
      <span className="wf-gate-field-label">{label}</span>
      <ul className="wf-scope-list">
        {paths.slice(0, PATH_LIMIT).map((path) => (
          <li key={path} className="wf-scope-path">{path}</li>
        ))}
      </ul>
      {paths.length > PATH_LIMIT && <span className="wf-scope-more">还有 {paths.length - PATH_LIMIT} 个文件</span>}
    </div>
  );
}

/** 评估问题清单：默认只露出前 limit 条，其余可展开（设计 §7.7） */
export function IssueList({ issues, limit }: { issues: IssueItem[]; limit: number }) {
  const [all, setAll] = useState(false);
  if (issues.length === 0) return null;
  const shown = all ? issues : issues.slice(0, limit);
  return (
    <div className="wf-issues-wrap">
    <ul className="wf-issues" aria-label="评估问题">
      {shown.map((item, i) => (
        <li key={i} className="wf-issue">
          <span className="wf-issue-sev" data-sev={item.severity ?? "minor"}>
            {SEVERITY_LABEL[item.severity ?? "minor"] ?? "问题"}
          </span>
          <span className="wf-issue-body">
            <span className="wf-issue-text">{item.problem}</span>
            {item.file && <span className="wf-issue-loc">{item.file}{item.line ? `:${item.line}` : ""}</span>}
            {item.fix_hint && <span className="wf-issue-fix">建议：{item.fix_hint}</span>}
          </span>
        </li>
      ))}
    </ul>
    {issues.length > limit && (
      <button type="button" className="wf-issues-more" aria-expanded={all} onClick={() => setAll((v) => !v)}>
        {all ? "收起问题" : `展开全部 ${issues.length} 条问题`}
      </button>
    )}
    </div>
  );
}

const BADGE: Record<string, string> = {
  conflict: "写入冲突",
  scope_violation: "越界改动",
  integration: "并入主工作区",
  verdict: "需要决定",
  gate: "需要决定",
};

export function WorkflowGate({ snap, compact = false }: { snap: WorkflowProgramOut; compact?: boolean }) {
  const gate = gateOf(snap);
  const busy = useWorkflowStore((s) => !!s.busy[snap.run.id]);
  const decideGate = useWorkflowStore((s) => s.decideGate);
  const [note, setNote] = useState("");
  const [confirmAbort, setConfirmAbort] = useState(false);
  if (!gate) return null;

  const { flowKey, node } = gate;
  const payload = (node.conflict ?? null) as GatePayload | null;
  const kind = payload ? (payload.kind ?? "conflict") : "verdict";
  const issues = (node.verdict?.issues ?? []) as IssueItem[];
  const reason = node.reason || node.question || "需要你确认当前结果";
  const roundsLeft = node.max_rework != null ? Math.max(0, node.max_rework - node.rework_round) : null;
  const labels: { primary: string; secondary?: string; primaryDecision: GateDecision; secondaryDecision?: GateDecision } =
    kind === "scope_violation"
      ? { primary: "纳入范围并继续", primaryDecision: "include" }
      : kind === "gate"
        ? { primary: "继续", primaryDecision: "approve" }
      : kind === "integration"
        ? { primary: "再试一次", primaryDecision: "rework" }
        : kind === "conflict"
          ? { primary: "保留现有内容", secondary: "再试一次", primaryDecision: "approve", secondaryDecision: "rework" }
          : node.rework_to
            ? {
                primary: "采纳当前结果",
                secondary: roundsLeft != null ? `再返工一轮（剩余 ${roundsLeft} 次）` : "再返工一轮",
                primaryDecision: "approve",
                secondaryDecision: "rework",
              }
            // 没有返工目标（如集成阶段的评估）时，返工无处可去，只保留采纳
            : { primary: "采纳当前结果", primaryDecision: "approve" };
  const notePlaceholder =
    kind === "scope_violation"
      ? "例如：这些文件确实是本步骤需要的，保留即可"
      : kind === "integration"
        ? "例如：已经处理了主工作区的本地改动"
        : "例如：保留对方的改动，只补充我负责的部分";
  const decide = (decision: GateDecision) => {
    void decideGate(snap.run.id, flowKey, node.key, decision, note.trim());
    setNote("");
  };

  return (
    <section className={"wf-gate" + (compact ? " is-compact" : "")} aria-label="等待你的决定">
      <header className="wf-gate-head">
        <span className="wf-gate-title">{node.title} · 等待你的决定</span>
        <Badge tone="warning" variant="soft" size="xs">{BADGE[kind] ?? "需要决定"}</Badge>
      </header>
      <p className="wf-gate-reason">{reason}</p>
      {kind === "conflict" && payload && <ConflictPanes conflict={payload} />}
      {kind === "scope_violation" && (
        <PathList label="越界改动的文件" paths={payload?.paths ?? []} command={payload?.command} />
      )}
      {kind === "integration" && (payload?.files?.length ?? 0) > 0 && (
        <PathList label="冲突文件" paths={payload?.files ?? []} />
      )}
      {kind === "verdict" && <IssueList issues={issues} limit={compact ? 3 : 5} />}
      <label className="wf-gate-field">
        <span className="wf-gate-field-label">说明（可选）</span>
        <Textarea
          className="wf-gate-note"
          rows={2}
          value={note}
          disabled={busy}
          placeholder={notePlaceholder}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <div className="wf-actions" role="group" aria-label="决定">
        <Button variant="primary" size="sm" disabled={busy} onClick={() => decide(labels.primaryDecision)}>
          {labels.primary}
        </Button>
        {labels.secondary && labels.secondaryDecision && (
          <Button variant="secondary" size="sm" disabled={busy} onClick={() => decide(labels.secondaryDecision!)}>
            {labels.secondary}
          </Button>
        )}
        <Button variant="danger-ghost" size="sm" disabled={busy} onClick={() => setConfirmAbort(true)}>
          中止
        </Button>
      </div>
      <ConfirmDialog
        open={confirmAbort}
        title="中止这一步？"
        message={
          kind === "integration"
            ? "中止后该子工作流停止，并入主工作区的改动不会发生；它的工作树会被移除，但分支保留，便于你之后查看。"
            : "中止后该子工作流停止，已完成的改动保留在工作区，其它子工作流不受影响。"
        }
        confirmLabel="中止"
        danger
        onCancel={() => setConfirmAbort(false)}
        onConfirm={() => {
          setConfirmAbort(false);
          decide("abort");
        }}
      />
    </section>
  );
}
