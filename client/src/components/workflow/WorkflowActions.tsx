/** 工作流操作按钮与确认弹层：按钮只列出当前状态下可执行的动作（聊天卡片、胶囊与右侧面板共用）。
 * 草稿的“调整”打开说明弹层，由主代理重新规划；放弃、中止属于破坏性操作，先经二次确认。
 * 开始、继续前的确认弹层由 WorkflowConfirmHost 统一挂载一次，避免同一运行的多个入口各开一层。
 */
import { useState } from "react";
import type { WorkflowDriftDiffOut, WorkflowProgramOut } from "../../api/client";
import { ConfirmDialog } from "../ConfirmDialog";
import { useWorkflowStore } from "../../store/workflow";
import { Button, Textarea } from "../ui";
import { Dialog } from "../ui/Dialog";
import { changedFilesSummary, failedNodeOf } from "./workflowModel";

type ActionSpec = {
  key: string;
  label: string;
  variant: "primary" | "secondary" | "danger-ghost";
  onClick: () => void;
};

export function WorkflowActions({ snap, size = "sm" }: { snap: WorkflowProgramOut; size?: "xs" | "sm" }) {
  const runId = snap.run.id;
  const busy = useWorkflowStore((s) => !!s.busy[runId]);
  const act = useWorkflowStore((s) => s.act);
  const requestResume = useWorkflowStore((s) => s.requestResume);
  const retryNode = useWorkflowStore((s) => s.retryNode);
  const [dropOpen, setDropOpen] = useState(false);
  const [adjustOpen, setAdjustOpen] = useState(false);

  const status = snap.run.status;
  const buttons: ActionSpec[] = [];
  if (status === "draft") {
    buttons.push({ key: "start", label: "开始执行", variant: "primary", onClick: () => void act(runId, "start") });
    buttons.push({ key: "adjust", label: "调整", variant: "secondary", onClick: () => setAdjustOpen(true) });
    buttons.push({ key: "drop-draft", label: "取消", variant: "danger-ghost", onClick: () => void act(runId, "cancel") });
  } else if (status === "running") {
    buttons.push({ key: "pause", label: "暂停", variant: "secondary", onClick: () => void act(runId, "pause") });
  } else if (status === "paused") {
    buttons.push({ key: "resume", label: "继续", variant: "primary", onClick: () => void requestResume(runId) });
    buttons.push({ key: "drop", label: "放弃", variant: "danger-ghost", onClick: () => setDropOpen(true) });
  } else if (status === "failed") {
    // 失败的是某一个步骤：只重试它，其它已完成的步骤保持不动
    const failed = failedNodeOf(snap);
    if (failed) {
      buttons.push({
        key: "retry-checkpoint",
        label: "从断点继续",
        variant: "primary",
        onClick: () => void retryNode(runId, failed.flowKey, failed.node.key, "checkpoint"),
      });
      buttons.push({
        key: "retry-fresh",
        label: "从头重做该步骤",
        variant: "secondary",
        onClick: () => void retryNode(runId, failed.flowKey, failed.node.key, "fresh"),
      });
    }
    buttons.push({ key: "drop", label: "放弃", variant: "danger-ghost", onClick: () => setDropOpen(true) });
  }

  return (
    <>
      {buttons.length > 0 && (
        <div className="wf-actions" role="group" aria-label="工作流操作">
          {buttons.map((b) => (
            <Button key={b.key} variant={b.variant} size={size} disabled={busy} onClick={b.onClick}>
              {b.label}
            </Button>
          ))}
        </div>
      )}
      <WorkflowAdjustDialog programId={runId} open={adjustOpen} onClose={() => setAdjustOpen(false)} />
      <ConfirmDialog
        open={dropOpen}
        title="放弃这个工作流？"
        message="放弃后不能再继续，已完成的改动保留在工作区。"
        confirmLabel="放弃"
        danger
        onCancel={() => setDropOpen(false)}
        onConfirm={() => {
          setDropOpen(false);
          void act(runId, "cancel");
        }}
      />
    </>
  );
}

/** 草稿的“调整”：说明想怎么改，发给主代理重新规划；成功后旧草稿标记为“已调整”。 */
export function WorkflowAdjustDialog({
  programId,
  open,
  onClose,
}: {
  programId: number;
  open: boolean;
  onClose: () => void;
}) {
  const adjustDraft = useWorkflowStore((s) => s.adjustDraft);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [failed, setFailed] = useState(false);

  const submit = async () => {
    setSending(true);
    setFailed(false);
    const ok = await adjustDraft(programId, text);
    setSending(false);
    if (ok) {
      setText("");
      onClose();
    } else {
      setFailed(true);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      width={480}
      title="调整工作流计划"
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose}>取消</Button>
          <Button variant="primary" size="sm" disabled={!text.trim() || sending} onClick={() => void submit()}>
            发送给嗒嗒
          </Button>
        </>
      }
    >
      <div className="wf-adjust">
        <p className="wf-adjust-lead">
          说说想怎么改，嗒嗒会基于你的要求重新规划。这版计划随即标记为“已调整”。
        </p>
        <Textarea
          rows={4}
          autoFocus
          value={text}
          disabled={sending}
          placeholder="例如：把联调拆成单独一步；前端先不做兑换页"
          onChange={(e) => setText(e.target.value)}
        />
        {failed && (
          <p className="wf-adjust-error" role="alert">
            消息没有发出去，计划保留，请稍后再试。
          </p>
        )}
      </div>
    </Dialog>
  );
}

/** 工作流的全局确认弹层：只在工作区布局中挂载一次，聊天卡片、胶囊与右侧面板共用同一份状态。 */
export function WorkflowConfirmHost() {
  const localChanges = useWorkflowStore((s) => s.localChanges);
  const confirmLocalChanges = useWorkflowStore((s) => s.confirmLocalChanges);
  const cancelLocalChanges = useWorkflowStore((s) => s.cancelLocalChanges);
  const drift = useWorkflowStore((s) => s.drift);
  const driftDiff = useWorkflowStore((s) => s.driftDiff);
  const confirmDrift = useWorkflowStore((s) => s.confirmDrift);
  const cancelDrift = useWorkflowStore((s) => s.cancelDrift);
  const openDriftDiff = useWorkflowStore((s) => s.openDriftDiff);
  const closeDriftDiff = useWorkflowStore((s) => s.closeDriftDiff);

  return (
    <>
      <Dialog
        open={!!localChanges}
        onClose={cancelLocalChanges}
        width={440}
        title="开始前请确认"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={cancelLocalChanges}>取消</Button>
            <Button variant="secondary" size="sm" onClick={() => void confirmLocalChanges(true)}>
              改用共享工作区
            </Button>
            <Button variant="primary" size="sm" onClick={() => void confirmLocalChanges(false)}>
              仍然开始
            </Button>
          </>
        }
      >
        {localChanges && <LocalChangesBody files={localChanges.files} />}
      </Dialog>
      <Dialog
        open={!!drift}
        onClose={cancelDrift}
        width={480}
        title="继续前请确认"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={cancelDrift}>取消</Button>
            <Button variant="primary" size="sm" onClick={() => void confirmDrift()}>仍然继续</Button>
          </>
        }
      >
        {drift && (
          <DriftBody
            files={drift.files}
            head={drift.head}
            active={driftDiff}
            onView={(path) => void openDriftDiff(path)}
            onHide={closeDriftDiff}
          />
        )}
      </Dialog>
    </>
  );
}

/** 主工作区未提交改动的说明：把两种开始方式的差别写清楚，由用户选择 */
function LocalChangesBody({ files }: { files: string[] }) {
  const { shown, more } = changedFilesSummary(files);
  return (
    <div className="wf-confirm">
      <p className="wf-confirm-lead">
        主工作区有 {files.length} 个文件尚未提交。子工作流会以这些改动为起点执行。
      </p>
      <ul className="wf-confirm-files" aria-label="未提交的文件">
        {shown.map((f) => (
          <li key={f}>{f}</li>
        ))}
        {more > 0 && <li className="wf-confirm-more">等 {files.length} 个文件</li>}
      </ul>
      <dl className="wf-confirm-options">
        <div>
          <dt>改用共享工作区</dt>
          <dd>子工作流直接在主工作区执行，不做并入。</dd>
        </div>
        <div>
          <dt>仍然开始</dt>
          <dd>子工作流在独立工作树中执行，完成后把修改写入主工作区；不提交，你的改动保持原样。</dd>
        </div>
      </dl>
    </div>
  );
}

/** 续跑前的漂移：改动文件（可逐个查看自暂停以来的差异）与仓库的新提交 */
function DriftBody({
  files,
  head,
  active,
  onView,
  onHide,
}: {
  files: string[];
  head: { from: string | null; to: string | null } | null;
  active: { path: string; data: WorkflowDriftDiffOut | null; loading: boolean } | null;
  onView: (path: string) => void;
  onHide: () => void;
}) {
  const { shown, more } = changedFilesSummary(files);
  return (
    <div className="wf-drift">
      <p className="wf-drift-lead">
        工作区在暂停期间有改动，继续可能覆盖这些改动。
      </p>
      {head && (
        <p className="wf-drift-head">
          仓库在暂停期间有新的提交（{head.from ?? "?"} → {head.to ?? "?"}），继续不会改动这些提交。
        </p>
      )}
      <ul className="wf-drift-files" aria-label="暂停期间改动的文件">
        {shown.map((path) => {
          const open = active?.path === path;
          return (
            <li key={path} className="wf-drift-file" data-open={open}>
              <span className="wf-drift-path">{path}</span>
              <Button
                variant="ghost"
                size="xs"
                aria-expanded={open}
                onClick={() => (open ? onHide() : onView(path))}
              >
                {open ? "收起差异" : "查看差异"}
              </Button>
            </li>
          );
        })}
        {more > 0 && <li className="wf-drift-more">等 {files.length} 个文件</li>}
      </ul>
      {active && <DriftDiffView state={active} />}
    </div>
  );
}

function DriftDiffView({
  state,
}: {
  state: { path: string; data: WorkflowDriftDiffOut | null; loading: boolean };
}) {
  if (state.loading) return <div className="wf-drift-state">正在读取差异…</div>;
  const data = state.data;
  if (!data) return null;
  if (!data.diff) return <div className="wf-drift-state">{data.reason ?? "无法展示差异"}</div>;
  return (
    <pre className="wf-drift-diff" aria-label={`${state.path} 的差异`}>
      {data.diff.split("\n").map((line, i) => (
        <span key={i} className="wf-diff-line" data-kind={diffKind(line)}>
          {line}
        </span>
      ))}
    </pre>
  );
}

function diffKind(line: string): "meta" | "hunk" | "add" | "del" | "ctx" {
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}
