/** 等待确认的步骤：一句话说明 + 评估问题 / 冲突对照 / 越界或并入清单 + 决定按钮。
 *
 * plan-workflow-collab：闸门不再内嵌输入框——补充说明统一走聊天输入框（支持附件与图片），
 * 由嗒嗒核实后经 decide_workflow 转交对应步骤；按钮只保留“一键决定”。中止属于破坏性操作，先经二次确认。
 */
import { useState } from "react";
import type { WorkflowProgramOut } from "../../api/client";
import { ConfirmDialog } from "../ConfirmDialog";
import { useWorkflowStore, type GateDecision } from "../../store/workflow";
import { Badge, Button, Tooltip } from "../ui";
import { flowTitleOf, gateOf } from "./workflowModel";

interface IssueItem {
  severity?: string;
  file?: string;
  line?: number | null;
  problem?: string;
  fix_hint?: string;
}

/** 节点上等待决定的载荷：评估（review）、写入冲突（conflict）、越界改动（scope_violation）、并入主工作区（integration）、闸门（gate） */
interface GatePayload {
  kind?: string;
  stage?: string;
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

const SEVERITY_LABEL: Record<string, string> = { blocker: "阻断", major: "重要", minor: "次要" };
const SEVERITY_RANK: Record<string, number> = { blocker: 0, major: 1, minor: 2 };
const PANE_LIMIT = 1600;
const PATH_LIMIT = 8;

function ConflictPanes({ conflict }: { conflict: GatePayload }) {
  const panes: Array<{ key: string; title: string; hint: string; text: string }> = [
    { key: "base", title: "读取时", hint: "基线", text: conflict.base ?? "" },
    { key: "current", title: "当前磁盘", hint: "其它步骤的改动", text: conflict.current ?? "" },
    { key: "pending", title: "本次待写入", hint: "这一步想写入的内容", text: conflict.pending ?? "" },
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

/** 评估问题清单：按严重程度排序，默认只露出前 limit 条，其余可展开（设计 §7.7） */
export function IssueList({ issues, limit }: { issues: IssueItem[]; limit: number }) {
  const [all, setAll] = useState(false);
  if (issues.length === 0) return null;
  const sorted = [...issues].sort(
    (a, b) => (SEVERITY_RANK[a.severity ?? "minor"] ?? 3) - (SEVERITY_RANK[b.severity ?? "minor"] ?? 3),
  );
  const shown = all ? sorted : sorted.slice(0, limit);
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
          {all ? "收起" : `展开全部 ${issues.length} 条`}
        </button>
      )}
    </div>
  );
}

/** 把引用写进聊天输入框并聚焦：补充说明、附件与图片都在输入框里完成 */
function askInComposer(text: string) {
  window.dispatchEvent(new CustomEvent("chatcoder:composer-prefill", { detail: { text } }));
}

interface GateView {
  badge: string;
  title: string;
  /** 说明这一步“为什么停下、点了按钮会发生什么” */
  hint: string;
  primary: { label: string; decision: GateDecision };
  secondary?: { label: string; decision: GateDecision };
  /** 预填到输入框的引用前缀 */
  quote: string;
}

function countMajor(issues: IssueItem[]): number {
  return issues.filter((i) => i.severity === "blocker" || i.severity === "major").length;
}

export function WorkflowGate({ snap, compact = false }: { snap: WorkflowProgramOut; compact?: boolean }) {
  const gate = gateOf(snap);
  const busy = useWorkflowStore((s) => !!s.busy[snap.run.id]);
  const decideGate = useWorkflowStore((s) => s.decideGate);
  const [confirmAbort, setConfirmAbort] = useState(false);
  if (!gate) return null;

  const { flowKey, node } = gate;
  const payload = (node.conflict ?? null) as GatePayload | null;
  // 旧数据里评估待定没有载荷，按评估处理
  const kind = payload?.kind ?? (node.kind === "review" ? "review" : "conflict");
  const issues = (node.verdict?.issues ?? []) as IssueItem[];
  const where = `${flowTitleOf(snap, node)} · ${node.title}`;
  const roundsLeft = node.max_rework != null ? Math.max(0, node.max_rework - node.rework_round) : null;
  const reworkTarget = node.rework_to
    ? (snap.flows.flatMap((f) => f.nodes).find((n) => n.key === node.rework_to)?.title ?? node.rework_to)
    : "";

  let view: GateView;
  if (kind === "review") {
    const major = countMajor(issues);
    const stage = payload?.stage ?? "confirm";
    const title =
      stage === "exhausted"
        ? `返工 ${node.rework_round} 次后仍未通过`
        : stage === "escalate"
          ? "评估需要你来判断"
          : `评估发现 ${major || issues.length} 个需要修改的问题`;
    view = {
      badge: "待你确认",
      title,
      hint: node.rework_to
        ? `嗒嗒会先核实这些问题并给出建议。确认返工后，问题交回「${reworkTarget}」修改，改完由同一个评估者复审。`
        : "这一步没有可返工的实现步骤，可以采纳当前结果继续，或中止这个子工作流。",
      primary: node.rework_to
        ? {
            label: stage === "confirm" && roundsLeft != null ? `返工（剩 ${roundsLeft} 次）` : "再返工一轮",
            decision: "rework",
          }
        : { label: "采纳当前结果", decision: "approve" },
      secondary: node.rework_to ? { label: "采纳当前结果", decision: "approve" } : undefined,
      quote: `关于工作流「${where}」的评估问题：`,
    };
  } else if (kind === "scope_violation") {
    view = {
      badge: "越界改动",
      title: "命令改动了写入范围以外的文件",
      hint: "纳入范围后，这一步会核对这些改动再继续；不需要这些改动可以中止。",
      primary: { label: "纳入范围并继续", decision: "include" },
      quote: `关于工作流「${where}」的越界改动：`,
    };
  } else if (kind === "gate") {
    view = {
      badge: "需要决定",
      title: node.question || node.reason || "是否继续？",
      hint: "继续后，后面的步骤开始执行。",
      primary: { label: "继续", decision: "approve" },
      quote: `关于工作流「${where}」：`,
    };
  } else if (kind === "integration") {
    view = {
      badge: "并入主工作区",
      title: "改动暂时无法并入主工作区",
      hint: node.reason || "处理好主工作区的本地改动后再试一次。",
      primary: { label: "再试一次", decision: "rework" },
      quote: `关于工作流「${where}」的并入问题：`,
    };
  } else {
    view = {
      badge: "写入冲突",
      title: "两个步骤改到了同一处",
      hint: "保留现有内容会放弃这一步的写入；再试一次会让这一步基于最新内容重做。",
      primary: { label: "保留现有内容", decision: "approve" },
      secondary: { label: "再试一次", decision: "rework" },
      quote: `关于工作流「${where}」的写入冲突：`,
    };
  }

  const decide = (decision: GateDecision) => {
    void decideGate(snap.run.id, flowKey, node.key, decision, "");
  };

  return (
    <section className={"wf-gate" + (compact ? " is-compact" : "")} aria-label="等待你确认">
      <header className="wf-gate-head">
        <div className="wf-gate-titles">
          <span className="wf-gate-title">{view.title}</span>
          <span className="wf-gate-where">{where}</span>
        </div>
        {/* 卡片头部已显示“待你确认”，卡片内只保留能补充信息的徽标（如写入冲突） */}
        {!(compact && kind === "review") && (
          <Badge tone="warning" variant="soft" size="xs">{view.badge}</Badge>
        )}
      </header>
      {kind === "review" && <IssueList issues={issues} limit={compact ? 3 : 6} />}
      {kind === "conflict" && payload && <ConflictPanes conflict={payload} />}
      {kind === "scope_violation" && (
        <PathList label="越界改动的文件" paths={payload?.paths ?? []} command={payload?.command} />
      )}
      {kind === "integration" && (payload?.files?.length ?? 0) > 0 && (
        <PathList label="冲突文件" paths={payload?.files ?? []} />
      )}
      <p className="wf-gate-hint">{view.hint}</p>
      <div className="wf-gate-foot">
        <div className="wf-actions" role="group" aria-label="决定">
          <Button variant="primary" size="sm" disabled={busy} onClick={() => decide(view.primary.decision)}>
            {view.primary.label}
          </Button>
          {view.secondary && (
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => decide(view.secondary!.decision)}>
              {view.secondary.label}
            </Button>
          )}
          <Button variant="danger-ghost" size="sm" disabled={busy} onClick={() => setConfirmAbort(true)}>
            中止
          </Button>
        </div>
        <Tooltip content="在输入框里补充要求、指出误判，可附图片或文件" side="top">
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => askInComposer(view.quote)}>
            补充说明
          </Button>
        </Tooltip>
      </div>
      <ConfirmDialog
        open={confirmAbort}
        title="中止这个子工作流？"
        message={
          kind === "integration"
            ? "中止后该子工作流停止，并入主工作区的改动不会发生；它的工作树会被移除，但分支保留，便于之后查看。"
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
