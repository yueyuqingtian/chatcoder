/** plan-20261114-message-flow：独立派发行保留实时进度，整轮耗时统一由工作过程入口展示。 */
import { memo, useMemo } from "react";
import type { MessageOut } from "@chatcoder/shared";
import { usePanelStore } from "../../store/panel";
import { useChatStore } from "../../store/chat";
import { IconChevronRight } from "../icons";
import { Tooltip } from "../ui";
import { subagentState, type SubagentState } from "./subagentStatus";
import { SubagentAvatar, SUBAGENT_ROLE_LABEL, resolveSubagentRole } from "./SubagentAvatar";
import { subagentActivity } from "./subagentActivity";

const STATE_TEXT: Record<SubagentState, string> = {
  running: "执行中",
  done: "已完成",
  failed: "失败",
  stopped: "已停止",
  pending: "待开始",
};

export interface SubagentMetaLite {
  agentId: number;
  name: string;
  status: string;
  error?: string | null;
  dispatchFailed?: boolean;
  turnId?: number | null;
  programId?: number | null;
  spawnMessageId?: number | null;
  startedAt?: string | null;
  endedAt?: string | null;
  workflowKind?: string | null;
  roleArgs?: { mode?: string; explore?: boolean; purpose?: string; agent_type?: string };
}

export const SubagentCard = memo(function SubagentCard({ meta }: { meta: SubagentMetaLite }) {
  const openSubagent = usePanelStore((s) => s.openSubagent);
  const status = useChatStore((s) => s.subagentMeta[meta.agentId]?.status ?? meta.status);
  const state = subagentState(status);
  const running = state === "running";
  const failed = state === "failed";
  const stopped = state === "stopped";
  const done = state === "done";



  const errText = (failed || stopped) && meta.error ? String(meta.error).trim() : "";
  const dispatchFailed = meta.dispatchFailed === true;
  const clickable = !dispatchFailed && meta.agentId !== 0;

  const role = resolveSubagentRole({
    name: meta.name,
    workflowKind: meta.workflowKind,
    roleArgs: meta.roleArgs,
  });
  const roleLabel = SUBAGENT_ROLE_LABEL[role];
  const displayTitle = meta.name.replace(/^探索[·:：\s]*/, "") || meta.name;

  // plan-20261114-message-flow：只在本线程输入变化时重算，主会话逐字更新不会扫描所有子代理历史。
  const activitySelector = useMemo(() => {
    let previousMessages: readonly MessageOut[] | undefined;
    let previousThinking = "", previousText = "";
    let previousResults: unknown;
    let cached = "";
    return (s: ReturnType<typeof useChatStore.getState>) => {
      const messages = s.subagentMessages[meta.agentId];
      const thinking = running ? s.subagentThinking[meta.agentId] ?? "" : "";
      const text = running ? s.subagentStreams[meta.agentId] ?? "" : "";
      if (messages === previousMessages && thinking === previousThinking && text === previousText && s.runningToolResults === previousResults) return cached;
      previousMessages = messages; previousThinking = thinking; previousText = text; previousResults = s.runningToolResults;
      return cached = subagentActivity({ messages, thinking, text, running, results: s.runningToolResults });
    };
  }, [meta.agentId, running]);
  const rawActivity = useChatStore(activitySelector);
  const sepIdx = rawActivity.indexOf("|");
  const isActionShine = running && sepIdx > 0 && rawActivity.slice(0, sepIdx) === "1";
  const activityText = sepIdx >= 0 ? rawActivity.slice(sepIdx + 1) : rawActivity;


  const stateText = dispatchFailed ? "派发失败" : STATE_TEXT[state];
  const stateClass = running ? "is-run" : done ? "is-ok" : failed ? "is-fail" : "";

  const secondLine = errText
    ? errText
    : activityText
      ? activityText
      : running
        ? "子代理正在执行…"
        : state === "pending"
          ? "等待调度开始…"
          : done
            ? "点击查看完整子代理会话"
            : "点击查看执行记录";

  const tip = dispatchFailed
    ? `${meta.name}（派发失败：${errText || "未知原因"}）`
    : errText
      ? `${meta.name}（${stopped ? "已停止" : "失败"}：${errText}）点击查看完整会话`
      : `${meta.name} · ${roleLabel}（点击查看完整会话）`;

  return (
    <div className="tc-node tc-subagent">
      <Tooltip content={tip}>
        <div
          className={"subagent-row" + (clickable ? " clickable" : "")}
          data-tool-state={running ? "running" : failed ? "fail" : "done"}
          role={clickable ? "button" : undefined} tabIndex={clickable ? 0 : undefined}
          onKeyDown={(e) => { if (clickable && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openSubagent(meta.agentId, meta.name); } }}
          onClick={clickable ? () => openSubagent(meta.agentId, meta.name) : undefined}
        >
          <SubagentAvatar role={role} running={running} />
          <div className="sa-main">
            <div className="sa-line1">
              <span className="sa-title">{displayTitle}</span>
              <span className="sa-role-text">· {roleLabel}</span>
              <span className={`sa-state ${stateClass}`}>{stateText}</span>
            </div>
            <div className={"sa-line2" + (errText ? " is-error" : "")}>
              <span className={isActionShine ? "text-shine" : undefined}>{secondLine}</span>
            </div>
          </div>
          {clickable && (
            <span className="sa-open-hint" aria-hidden>
              <IconChevronRight size={11} />
            </span>
          )}
        </div>
      </Tooltip>
    </div>
  );
});
