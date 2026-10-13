/** 输入框上方的工作流胶囊：本会话有待处理、进行中或暂停的工作流时显示一条进度。
 * 优先级：暂停（可继续）> 待你决定 > 待确认 > 运行中；暂停时常驻「继续」。
 * 点击主体钉住一个浮层，列出本会话的进行中工作流及各自的操作（Esc / 点外部关闭）；
 * 有待决定的写入冲突时显示“冲突 N”。几何与任务胶囊（TaskProgressCapsule）一致，同时出现时抬到其上方。
 */
import { useEffect, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { useWorkflowStore } from "../../store/workflow";
import { IxWorkflow } from "../iconkit/StackIcons";
import { Badge, Button, Popover, Tooltip } from "../ui";
import { currentNote, gateOf, runStatusView } from "./workflowModel";

const ACTIVE = new Set(["running", "waiting_human", "paused", "draft"]);

function rank(status: string): number {
  if (status === "paused") return 0;
  if (status === "waiting_human") return 1;
  if (status === "draft") return 2;
  return 3;
}

export function WorkflowCapsule({ stacked = false, onVisibilityChange }: {
  /** 任务胶囊同时出现时，本胶囊抬到其上方（由 ChatPanel 传入） */
  stacked?: boolean;
  /** 上报本胶囊是否真的渲染，消息流据此按需让位（与任务胶囊口径一致） */
  onVisibilityChange?: (visible: boolean) => void;
} = {}) {
  const sessionId = useChatStore((s) => s.currentSessionId);
  const runs = useWorkflowStore((s) => (sessionId == null ? undefined : s.bySession[sessionId]));
  const snapshots = useWorkflowStore((s) => s.snapshots);
  const busy = useWorkflowStore((s) => s.busy);
  const loadSession = useWorkflowStore((s) => s.loadSession);
  const loadProgram = useWorkflowStore((s) => s.loadProgram);
  const requestResume = useWorkflowStore((s) => s.requestResume);
  const [pinned, setPinned] = useState(false);

  const active = (runs ?? []).filter((r) => ACTIVE.has(r.status));
  const focus = active.length > 0
    ? [...active].sort((a, b) => rank(a.status) - rank(b.status) || b.id - a.id)[0]
    : null;
  const visible = focus != null;
  const focusSnap = focus ? snapshots[focus.id] : undefined;

  useEffect(() => {
    if (sessionId != null) void loadSession(sessionId);
  }, [sessionId, loadSession]);
  // 出现 / 消失都上报；卸载（切会话、切设置页）时收回占位，避免消息流底部留一条死白
  useEffect(() => { onVisibilityChange?.(visible); }, [visible, onVisibilityChange]);
  useEffect(() => () => onVisibilityChange?.(false), [onVisibilityChange]);
  // 当前动作与冲突信息需要快照：焦点运行的快照缺失时补拉一次
  useEffect(() => {
    if (focus && !focusSnap) void loadProgram(focus.id);
  }, [focus, focusSnap, loadProgram]);
  useEffect(() => { if (!visible) setPinned(false); }, [visible]);

  if (!focus) return null;
  const view = runStatusView(focus.status, focus.closed_reason);
  const progress = focus.progress ?? { done: 0, total: 0 };
  const title = focus.title || "未命名工作流";
  const conflicts = focusSnap ? (gateOf(focusSnap)?.node.conflict?.kind === "conflict" ? 1 : 0) : 0;
  const stateText = focus.status === "paused"
    ? "已暂停"
    : focusSnap
      ? currentNote(focusSnap)
      : view.label;
  const open = () => {
    setPinned(false);
    usePanelStore.getState().openTab("workflow");
    useWorkflowStore.getState().focus(focus.id);
  };

  return (
    <div className="wf-capsule-wrap" data-stacked={stacked ? "true" : undefined}>
      <div className="wf-capsule" data-status={focus.status}>
        <button
          type="button"
          className="wf-capsule-main"
          aria-expanded={pinned}
          aria-label={`工作流：${title}，${view.label}`}
          onClick={() => setPinned((v) => !v)}
        >
          <span className="wf-capsule-ic" aria-hidden><IxWorkflow size={13} /></span>
          <span className="wf-capsule-title">工作流 {progress.done}/{progress.total}</span>
          <span className="wf-capsule-sep" aria-hidden>·</span>
          <span className="wf-capsule-state" data-tone={view.tone}>{stateText}</span>
          {conflicts > 0 && <Badge tone="warning" variant="soft" size="xs">冲突 {conflicts}</Badge>}
        </button>
        {active.length > 1 && (
          <Tooltip content={`本会话还有 ${active.length - 1} 个工作流`} side="top" delay={400}>
            <span className="wf-capsule-more">+{active.length - 1}</span>
          </Tooltip>
        )}
        {focus.status === "paused" && (
          <Button
            className="wf-capsule-resume"
            variant="primary"
            size="xs"
            disabled={!!busy[focus.id]}
            onClick={() => void requestResume(focus.id)}
          >
            继续
          </Button>
        )}
      </div>
      <Popover
        open={pinned}
        onClose={() => setPinned(false)}
        placement="top-start"
        className="wf-capsule-pop"
        role="dialog"
        aria-label="工作流"
      >
        <div className="wf-capsule-pop-head">
          <span className="wf-capsule-pop-title">工作流</span>
          <Button variant="ghost" size="xs" onClick={open}>打开详情</Button>
        </div>
        <ul className="wf-capsule-list" aria-label="本会话的工作流">
          {active
            .slice()
            .sort((a, b) => rank(a.status) - rank(b.status) || b.id - a.id)
            .map((run) => {
              const v = runStatusView(run.status, run.closed_reason);
              const p = run.progress ?? { done: 0, total: 0 };
              return (
                <li key={run.id} className="wf-capsule-item" data-status={run.status}>
                  <span className="wf-capsule-item-main">
                    <span className="wf-capsule-item-title">{run.title || "未命名工作流"}</span>
                    <Badge tone={v.tone} variant={v.variant} size="xs">{v.label}</Badge>
                  </span>
                  <span className="wf-capsule-item-count">{p.done}/{p.total} 步</span>
                  {run.status === "paused" && (
                    <Button
                      variant="primary"
                      size="xs"
                      disabled={!!busy[run.id]}
                      onClick={() => void requestResume(run.id)}
                    >
                      继续
                    </Button>
                  )}
                </li>
              );
            })}
        </ul>
      </Popover>
    </div>
  );
}
