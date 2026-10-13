/** 子工作流的步骤轨道与子工作流级轨道。
 * 步骤轨道：状态图标 + 步骤名，连线表示先后；评估打回实现的返工用一条虚线弧标出（位置按实际排版测量）。
 * 已分配子代理的步骤可点击，直接打开该子代理的消息流（与普通子代理同一面板）。
 */
import { memo, useLayoutEffect, useRef, useState, type RefObject } from "react";
import type { WorkflowNodeOut, WorkflowProgramOut } from "../../api/client";
import { StatusIcon, type StatusKind } from "../iconkit/StatusIcon";
import { usePanelStore } from "../../store/panel";
import { Tooltip } from "../ui";
import { KIND_LABEL, KIND_SHORT_LABEL, flowStatusOf, nodeStatusView } from "./workflowModel";

const ARC_H = 12;

/** 返工关系：评估（review）节点指向的实现节点，返回 [{ from: 评估的位置, to: 实现的位置 }] */
function reworkPairs(nodes: WorkflowNodeOut[]): Array<{ from: number; to: number }> {
  const index = new Map(nodes.map((n, i) => [n.key, i]));
  const pairs: Array<{ from: number; to: number }> = [];
  nodes.forEach((node, i) => {
    if (!node.rework_to) return;
    const to = index.get(node.rework_to);
    if (to !== undefined && to !== i) pairs.push({ from: i, to });
  });
  return pairs;
}

/** 量出每个步骤的中心位置与轨道宽度；轨道尺寸变化时重新测量 */
function useStepGeometry(
  listRef: RefObject<HTMLOListElement>,
  wrapRef: RefObject<HTMLDivElement>,
  count: number,
) {
  const [geo, setGeo] = useState<{ width: number; centers: number[] }>({ width: 0, centers: [] });
  useLayoutEffect(() => {
    const measure = () => {
      const list = listRef.current;
      const wrap = wrapRef.current;
      if (!list || !wrap) return;
      const centers = Array.from(list.children).map((el) => {
        const item = el as HTMLElement;
        return item.offsetLeft + item.offsetWidth / 2;
      });
      setGeo({ width: wrap.clientWidth, centers });
    };
    measure();
    if (typeof ResizeObserver === "undefined" || !wrapRef.current) return;
    const observer = new ResizeObserver(measure);
    observer.observe(wrapRef.current);
    return () => observer.disconnect();
  }, [listRef, wrapRef, count]);
  return geo;
}

export const WorkflowStepTrack = memo(function WorkflowStepTrack({
  nodes,
  compact = false,
}: {
  nodes: WorkflowNodeOut[];
  compact?: boolean;
}) {
  const listRef = useRef<HTMLOListElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const pairs = reworkPairs(nodes);
  const geo = useStepGeometry(listRef, wrapRef, nodes.length);

  return (
    <div className="wf-track-wrap" ref={wrapRef}>
      <ol ref={listRef} className={"wf-track" + (compact ? " is-compact" : "")} aria-label="步骤">
        {nodes.map((node) => {
          const view = nodeStatusView(node.status);
          const text = `${node.title} · ${KIND_LABEL[node.kind] ?? node.kind} · ${view.label}${node.reason ? ` · ${node.reason}` : ""}`;
          const body = (
            <>
              <span className="wf-step-ic">
                <StatusIcon status={view.icon} size={compact ? 12 : 14} label={view.label} />
              </span>
              <span className="wf-step-name">{KIND_SHORT_LABEL[node.kind] ?? node.kind}</span>
              {node.rework_round > 0 && (
                <span className="wf-step-rework" aria-label={`返工 ${node.rework_round} 次`}>
                  ↺ {node.rework_round}
                  {node.max_rework != null ? `/${node.max_rework}` : ""}
                </span>
              )}
            </>
          );
          return (
            <li key={node.key} className="wf-step" data-status={view.icon}>
              <Tooltip content={text}>
                {node.agent_id != null ? (
                  <button
                    type="button"
                    className="wf-step-btn"
                    onClick={() => {
                      if (node.agent_id != null) usePanelStore.getState().openSubagent(node.agent_id, node.title);
                    }}
                  >
                    {body}
                  </button>
                ) : (
                  <span className="wf-step-btn is-static">{body}</span>
                )}
              </Tooltip>
            </li>
          );
        })}
      </ol>
      {pairs.length > 0 && geo.width > 0 && (
        <svg
          className="wf-track-arcs"
          width={geo.width}
          height={ARC_H}
          viewBox={`0 0 ${geo.width} ${ARC_H}`}
          aria-hidden
        >
          {pairs.map(({ from, to }) => {
            const x1 = geo.centers[to];
            const x2 = geo.centers[from];
            if (x1 === undefined || x2 === undefined) return null;
            return (
              <path
                key={`${from}-${to}`}
                className="wf-track-arc"
                d={`M${x1} 0 C${x1} ${ARC_H * 2}, ${x2} ${ARC_H * 2}, ${x2} 0`}
              />
            );
          })}
        </svg>
      )}
    </div>
  );
});

const FLOW_STATUS: Record<string, { label: string; icon: StatusKind }> = {
  done: { label: "已完成", icon: "done" },
  failed: { label: "失败", icon: "failed" },
  waiting_human: { label: "等待你的决定", icon: "paused" },
  running: { label: "进行中", icon: "running" },
  paused: { label: "已暂停", icon: "paused" },
  queued: { label: "排队中", icon: "queued" },
  pending: { label: "未开始", icon: "pending" },
};

/** 子工作流级轨道：每个子工作流一个节点，用于卡片的折叠态，回答“哪一块做完了、哪一块在做” */
export const WorkflowFlowTrack = memo(function WorkflowFlowTrack({ snap }: { snap: WorkflowProgramOut }) {
  return (
    <ol className="wf-flow-track" aria-label="子工作流进度">
      {snap.flows.map((flow) => {
        const status = flowStatusOf(flow.nodes);
        const view = FLOW_STATUS[status] ?? FLOW_STATUS.pending;
        const name = flow.run.title || flow.run.package_key || "";
        return (
          <li key={flow.run.id} className="wf-flow-step" data-status={status}>
            <Tooltip content={`${name} · ${view.label}`}>
              <span className="wf-flow-step-body">
                <StatusIcon status={view.icon} size={14} label={view.label} />
                <span className="wf-flow-step-name">{name}</span>
              </span>
            </Tooltip>
          </li>
        );
      })}
    </ol>
  );
});
