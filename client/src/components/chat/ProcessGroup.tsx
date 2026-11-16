/** plan-20261114-message-flow：流式思考与相邻工具共享一个稳定的时序组；逐字订阅只留在活动组。 */
import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useChatStore } from "../../store/chat";
import { useMergedThinkingText } from "../../store/streamMerged";
import { dropFlowExpand, liveThinkingKey, processGroupKey, thinkingKey, useFlowGroupExpand } from "../../store/flowExpand";
import { IconChevronRight } from "../icons";
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { ChatCollapse } from "./ChatCollapse";
import { ThinkingBlock } from "./ThinkingBlock";
import { ThinkingTicker } from "./ThinkingTicker";
import { ThinkingExpanded } from "./ThinkingExpanded";
import { firstIdOf, type TurnItem } from "./timeline";
import { processGroupState } from "./processGroupState";
import { useRunningTicker } from "./useRunningTicker";

interface Props {
  items: readonly TurnItem[];
  indices: number[];
  turnId: number | null;
  agentId?: number;
  flow: "main" | "subagent";
  active: boolean;
  liveThinking?: boolean;
  renderItem: (index: number, grouped?: boolean) => ReactNode;
}

const GroupView = memo(function GroupView({ items, indices, turnId, agentId, flow, active, renderItem, thinking = "" }: Props & { thinking?: string }) {
  const anchor = indices.length ? firstIdOf(items[indices[0]]) ?? indices[0] : "live";
  const key = indices.length ? processGroupKey(flow, turnId, agentId, anchor) : liveThinkingKey(flow === "subagent" ? agentId : undefined);
  // plan-20261114-message-flow：单行并入组或思考落库后，继承用户已经展开的内容。
  const memberKeys = indices.flatMap((index) => {
    const item = items[index];
    if (item.kind === "thinking") return [thinkingKey(item.msg.id)];
    if (item.kind === "tools") return item.nodes.flatMap((node) => node.kind === "leaf" ? [node.leaf.callKey] : node.leaves.map((leaf) => leaf.callKey));
    return [];
  });
  const [open, setOpen] = useFlowGroupExpand(key, memberKeys);
  const results = useChatStore((s) => s.runningToolResults);
  const state = useMemo(() => processGroupState(items, indices, active, results), [items, indices, active, results]);
  const seen = useRef(new Set(state.calls.map((call) => call.key)));
  // plan-tool-preview-restore：整轮运行不代表该组仍在执行；挂载时已有调用不补播，真实待完成项由 previews 展示。
  const [queue, setQueue] = useState<Array<{ key: string; text: string }>>([]);
  useEffect(() => {
    const added = state.calls.filter((call) => !seen.current.has(call.key));
    state.calls.forEach((call) => seen.current.add(call.key));
    if (active && !open && added.length) setQueue((old) => [...old, ...added]);
  }, [state.calls, active, open]);
  useEffect(() => {
    if (open || !active) { setQueue((old) => old.length ? [] : old); return; }
    if (!queue.length) return;
    const timer = window.setTimeout(() => setQueue((old) => old.slice(1)), 1200);
    return () => window.clearTimeout(timer);
  }, [open, active, queue]);
  const tick = useRunningTicker(state.previews.length > 1);
  /* plan-723-3029 R2：预览必须再受 active 约束一次。
   * state.previews 本身已经依赖 active（见 processGroupState 里 `pending = active ? … : []`），
   * 但 queue 是**组件本地 state**（下方 useState 初始化、effect 里追加、再由 effect 在 paint 之后才清空），
   * 渲染期不受 active 约束——active 由真翻假时会多亮一帧预览扫光。 */
  const preview = active ? (queue[0]?.text ?? state.previews[tick % Math.max(1, state.previews.length)]) : undefined;
  const live = active && Boolean(thinking.trim());
  const running = Boolean(preview) || live;
  useEffect(() => () => { if (anchor === "live") dropFlowExpand(key); }, [anchor, key]);

  return (
    <div className="tc-node tc-process-group">
      <div className={`tc-row has-output tc-process-head${open ? " expanded" : ""}`}
        role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(!open); } }}>
        <span className="tc-icon"><ToolGlyph tool={state.tool} size={16} /></span>
        {!open && preview ? (
          <span className="tc-process-preview text-shine" key={preview}>{preview}</span>
        ) : !open && live ? (
          <>
            <span className="tc-verb text-shine">正在思考</span>
            <span className="thinking-ticker-wrap"><ThinkingTicker text={thinking} /></span>
          </>
        ) : (
          <>
            <span className="tc-verb">{state.verb}</span>
            {state.summary && <><span className="tc-sep" aria-hidden>·</span><span className="tc-explore-summary">{state.summary}</span></>}
          </>
        )}
        <span className={`tc-chevron${open ? " open" : ""}`} aria-hidden><IconChevronRight size={11} /></span>
      </div>
      <ChatCollapse open={open}>
        <div className="tc-process-detail" data-running={running || undefined}>
          {indices.map((index) => renderItem(index, true))}
          {live && (indices.length === 0
            ? <ThinkingExpanded text={thinking} />
            : <ThinkingBlock text={thinking} active msgId={0} agentId={flow === "subagent" ? agentId : undefined} />)}
        </div>
      </ChatCollapse>
    </div>
  );
});

function MainLiveGroup(props: Props) {
  const thinking = useMergedThinkingText(Boolean(props.liveThinking));
  return <GroupView {...props} thinking={thinking} />;
}

function SubagentLiveGroup(props: Props) {
  const thinking = useChatStore((s) => props.liveThinking && props.agentId != null ? s.subagentThinking[props.agentId] ?? "" : "");
  return <GroupView {...props} thinking={thinking} />;
}

export function ProcessGroup(props: Props) {
  return props.flow === "main" ? <MainLiveGroup {...props} /> : <SubagentLiveGroup {...props} />;
}
