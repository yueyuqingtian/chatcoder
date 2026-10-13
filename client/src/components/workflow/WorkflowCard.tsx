/** 聊天流中的工作流卡：计划确认、进度与操作都在对话里完成（不打断阅读），详情进右侧「工作流」标签。
 * 草稿显示计划正文（子工作流、角色链、写入范围、换模板）；运行中默认折叠为子工作流级轨道，
 * 展开才列出每个子工作流的步骤与评估问题。卡片只记录程序编号，内容来自工作流 store 的快照，
 * 刷新或断线重连后仍能恢复。
 */
import { memo, useEffect, useState } from "react";
import type { WorkflowNodeOut } from "../../api/client";
import { IxWorkflow } from "../iconkit/StackIcons";
import { usePanelStore } from "../../store/panel";
import { useWorkflowStore } from "../../store/workflow";
import { Badge, Button } from "../ui";
import { WorkflowActions } from "./WorkflowActions";
import { WorkflowFlowTrack, WorkflowStepTrack } from "./WorkflowStepTrack";
import { IssueList, WorkflowGate } from "./WorkflowGate";
import { WorkflowPlanBody } from "./WorkflowPlanCard";
import {
  allNodes,
  currentNote,
  dependencyLabel,
  estimatedConcurrency,
  flowStatusOf,
  gateOf,
  issuesOf,
  progressOf,
  retryCountdown,
  runStatusView,
} from "./workflowModel";

/** 网络重试的倒计时需要随时间刷新：只有存在重试中的步骤时才启动计时 */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

export const WorkflowCard = memo(function WorkflowCard({ programId }: { programId: number }) {
  const snap = useWorkflowStore((s) => s.snapshots[programId]);
  const loadProgram = useWorkflowStore((s) => s.loadProgram);
  const notice = useWorkflowStore((s) => s.notices[programId]);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!snap) void loadProgram(programId);
  }, [programId, snap, loadProgram]);

  const retrying = snap ? allNodes(snap).find((n) => n.status === "recovering") : undefined;
  const now = useNow(!!retrying);

  // 快照未到之前占住与正常卡片相近的高度，避免加载完成时消息流跳动
  if (!snap) {
    return <div className="wf-card is-loading" aria-busy="true">正在加载工作流…</div>;
  }

  const { run } = snap;
  const view = runStatusView(run.status, run.closed_reason);
  const { done, total } = progressOf(snap);
  const flowsDone = snap.flows.filter((f) => flowStatusOf(f.nodes) === "done").length;
  const isDraft = run.status === "draft";
  const adjusted = run.status === "cancelled" && run.closed_reason === "adjusted";
  const gate = gateOf(snap);
  const openDetail = () => {
    usePanelStore.getState().openTab("workflow");
    useWorkflowStore.getState().focus(programId);
  };

  return (
    <section className="wf-card" data-status={run.status} aria-label={`工作流：${run.title}`}>
      <header className="wf-card-head">
        <span className="wf-card-ic" aria-hidden><IxWorkflow size={15} /></span>
        <div className="wf-card-titles">
          <div className="wf-card-title">{run.title || "未命名工作流"}</div>
          <div className="wf-card-meta">
            {isDraft
              ? `${snap.flows.length} 个子工作流 · 预计并发 ${estimatedConcurrency(snap)}`
              : `已完成 ${flowsDone}/${snap.flows.length} 个子工作流 · ${done}/${total} 步`}
            {run.rework_total > 0 && <> · 返工 {run.rework_total} 次</>}
          </div>
        </div>
        <Badge tone={view.tone} variant={view.variant} size="sm">{view.label}</Badge>
      </header>

      {isDraft ? (
        <>
          {run.rationale && <p className="wf-card-rationale">{run.rationale}</p>}
          <WorkflowPlanBody snap={snap} />
        </>
      ) : (
        <>
          <WorkflowFlowTrack snap={snap} />
          {retrying ? (
            <RetryLine node={retrying} now={now} />
          ) : (
            <p className="wf-card-note" aria-live="polite">{currentNote(snap)}</p>
          )}
          {notice && <p className="wf-card-notice" role="status">{notice}</p>}
          {gate && <WorkflowGate snap={snap} compact />}
          {expanded && (
            <div className="wf-card-detail">
              {snap.flows.map((flow) => {
                const dep = dependencyLabel(snap, flow.run.package_key);
                const issues = flow.nodes.filter((n) => n.kind === "review").flatMap(issuesOf);
                return (
                  <div className="wf-card-flow" key={flow.run.id}>
                    <div className="wf-card-flow-head">
                      <span className="wf-card-flow-name">{flow.run.title || flow.run.package_key}</span>
                      {flow.run.isolation === "worktree" && <span className="wf-card-flow-tag">独立工作树</span>}
                      {dep && <span className="wf-card-flow-dep">{dep}</span>}
                    </div>
                    <WorkflowStepTrack nodes={flow.nodes} compact />
                    {issues.length > 0 && <IssueList issues={issues} limit={3} />}
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      <footer className="wf-card-foot">
        {!adjusted && <WorkflowActions snap={snap} size="xs" />}
        {!isDraft && (
          <Button
            variant="ghost"
            size="xs"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? "收起步骤" : "展开步骤"}
          </Button>
        )}
        <Button variant="ghost" size="xs" onClick={openDetail}>查看详情</Button>
      </footer>
    </section>
  );
});

/** 网络重试：说明重试到第几次、还有多久再试；倒计时槽位固定宽度，数字变化不引起抖动 */
function RetryLine({ node, now }: { node: WorkflowNodeOut; now: number }) {
  const countdown = retryCountdown(node.next_retry_at, now);
  return (
    <p className="wf-card-note wf-card-retry" aria-live="polite">
      <span>网络波动，正在重试：{node.title}</span>
      {node.reason && <span className="wf-retry-attempt">{node.reason}</span>}
      <span className="wf-countdown" aria-label="距离下次重试">{countdown}</span>
    </p>
  );
}
