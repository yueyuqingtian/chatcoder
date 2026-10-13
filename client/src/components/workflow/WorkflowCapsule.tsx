/** 工作流在输入框胶囊中的数据与浮层内容。胶囊本体与浮层开合由 chat/ComposerCapsule.tsx 统一管理。
 *
 * 焦点工作流的优先级：暂停（可继续）> 待你决定 > 待确认 > 运行中；同一会话的其它进行中工作流列在浮层里。
 * 有待决定的写入冲突时，段内显示“冲突 N”。 */
import { useEffect } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { useWorkflowStore } from "../../store/workflow";
import { IxWorkflow } from "../iconkit/StackIcons";
import { Button } from "../ui";
import { currentNote, gateOf, runStatusView } from "./workflowModel";

const ACTIVE = new Set(["running", "waiting_human", "paused", "draft"]);

function rank(status: string): number {
  if (status === "paused") return 0;
  if (status === "waiting_human") return 1;
  if (status === "draft") return 2;
  return 3;
}

export function useWorkflowCapsule() {
  const sessionId = useChatStore((s) => s.currentSessionId);
  const runs = useWorkflowStore((s) => (sessionId == null ? undefined : s.bySession[sessionId]));
  const snapshots = useWorkflowStore((s) => s.snapshots);
  const busy = useWorkflowStore((s) => s.busy);
  const loadSession = useWorkflowStore((s) => s.loadSession);
  const loadProgram = useWorkflowStore((s) => s.loadProgram);
  const requestResume = useWorkflowStore((s) => s.requestResume);

  const active = (runs ?? [])
    .filter((r) => ACTIVE.has(r.status))
    .sort((a, b) => rank(a.status) - rank(b.status) || b.id - a.id);
  const focus = active[0] ?? null;
  const focusId = focus?.id ?? null;
  const focusSnap = focusId != null ? snapshots[focusId] : undefined;

  useEffect(() => {
    if (sessionId != null) void loadSession(sessionId);
  }, [sessionId, loadSession]);
  // 当前动作与冲突信息需要快照：焦点运行的快照缺失时补拉一次
  useEffect(() => {
    if (focusId != null && !focusSnap) void loadProgram(focusId);
  }, [focusId, focusSnap, loadProgram]);

  const view = focus ? runStatusView(focus.status, focus.closed_reason) : null;
  const progress = focus?.progress ?? { done: 0, total: 0 };
  const title = focus?.title || "未命名工作流";
  const conflicts = focusSnap && gateOf(focusSnap)?.node.conflict?.kind === "conflict" ? 1 : 0;
  const stateText = !focus
    ? ""
    : focus.status === "paused"
      ? "已暂停"
      : focusSnap
        ? currentNote(focusSnap)
        : view?.label ?? "";

  const openDetail = () => {
    if (focusId == null) return;
    // 同 WorkflowCard：右侧面板收起时先展开，否则只切了标签，看不到工作流面板
    const panel = usePanelStore.getState();
    panel.openPanel();
    panel.openTab("workflow");
    useWorkflowStore.getState().focus(focusId);
  };

  return {
    visible: focus != null,
    focus,
    active,
    view,
    progress,
    title,
    stateText,
    conflicts,
    busy,
    resume: requestResume,
    openDetail,
  };
}

export type WorkflowCapsuleState = ReturnType<typeof useWorkflowCapsule>;

/** 工作流浮层：本会话进行中 / 待处理的工作流，逐条显示状态、步数与（暂停时）继续 */
export function WorkflowPopBody({ wf, onOpenDetail }: {
  wf: WorkflowCapsuleState;
  onOpenDetail: () => void;
}) {
  return (
    <>
      <div className="tcap-pop-head">
        <span className="tcap-pop-title">工作流</span>
        <Button variant="ghost" size="xs" onClick={onOpenDetail}>打开详情</Button>
      </div>
      <div className="tcap-pop-body" role="list" aria-label="本会话的工作流">
        {wf.active.map((run) => {
          const v = runStatusView(run.status, run.closed_reason);
          const p = run.progress ?? { done: 0, total: 0 };
          return (
            <div className="wf-cap-row" role="listitem" key={run.id}>
              <span className="wf-cap-row-icon" aria-hidden><IxWorkflow size={13} /></span>
              <span className="wf-cap-row-title">{run.title || "未命名工作流"}</span>
              <span className="tcap-state" data-tone={v.tone}><span className="tcap-state-text">{v.label}</span></span>
              <span className="wf-cap-row-count">{p.done}/{p.total} 步</span>
              {run.status === "paused" && (
                <Button variant="primary" size="xs" disabled={!!wf.busy[run.id]} onClick={() => void wf.resume(run.id)}>
                  继续
                </Button>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}
