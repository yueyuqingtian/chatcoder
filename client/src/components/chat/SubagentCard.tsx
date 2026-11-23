/** plan-20261114-message-flow：独立派发行保留实时进度，整轮耗时统一由工作过程入口展示。 */
import { memo, useMemo } from "react";
import type { MessageOut } from "@chatcoder/shared";
import { usePanelStore } from "../../store/panel";
import { useChatStore } from "../../store/chat";
import { IconAlertCircle, IconCheck, IconChevronRight } from "../icons";
import { Tooltip } from "../ui";
import { parseUtc } from "../../utils/time";
import { useRunningTicker } from "./useRunningTicker";
import { subagentState, type SubagentState } from "./subagentStatus";
import { SubagentAvatar, SUBAGENT_ROLE_LABEL, resolveSubagentRole, type SubagentRoleArgs } from "./SubagentAvatar";
import { subagentActivity } from "./subagentActivity";

function fmtElapsed(startMs: number, endMs: number): string {
  if (!startMs || endMs < startMs) return "";
  const sec = Math.max(0, Math.round((endMs - startMs) / 1000));
  const min = Math.floor(sec / 60);
  return min > 0 ? `${min}m${sec % 60}s` : `${sec}s`;
}

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
  subagentMode?: string | null;
  subagentRole?: string | null;
  subagentProfileName?: string | null;
  roleArgs?: SubagentRoleArgs;
}

export const SubagentCard = memo(function SubagentCard({ meta }: { meta: SubagentMetaLite }) {
  const openSubagent = usePanelStore((s) => s.openSubagent);
  const status = useChatStore((s) => s.subagentMeta[meta.agentId]?.status ?? meta.status);
  const startedAtRaw = useChatStore((s) => s.subagentMeta[meta.agentId]?.startedAt ?? meta.startedAt ?? null);
  const endedAtRaw = useChatStore((s) => s.subagentMeta[meta.agentId]?.endedAt ?? meta.endedAt ?? null);
  const storeMode = useChatStore((s) => s.subagentMeta[meta.agentId]?.subagentMode ?? null);
  const storeRole = useChatStore((s) => s.subagentMeta[meta.agentId]?.subagentRole ?? null);
  const state = subagentState(status);
  const running = state === "running";
  const failed = state === "failed";
  const stopped = state === "stopped";
  const done = state === "done";
  useRunningTicker(running);

  const startMs = startedAtRaw ? parseUtc(startedAtRaw) : 0;
  const endMs = running ? Date.now() : (endedAtRaw ? parseUtc(endedAtRaw) : 0);
  const durationText = startMs > 0 && endMs > 0 ? fmtElapsed(startMs, endMs) : "";

  const errText = (failed || stopped) && meta.error ? String(meta.error).trim() : "";
  const dispatchFailed = meta.dispatchFailed === true;
  const clickable = !dispatchFailed && meta.agentId !== 0;

  const role = resolveSubagentRole({
    name: meta.name,
    workflowKind: meta.workflowKind,
    subagentMode: meta.subagentMode ?? storeMode,
    subagentRole: meta.subagentRole ?? storeRole,
    roleArgs: meta.roleArgs,
  });
  const profileName = useChatStore((s) => s.subagentMeta[meta.agentId]?.subagentProfileName);
  const roleLabel = meta.subagentProfileName ?? profileName ?? SUBAGENT_ROLE_LABEL[role];
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
            ? "已完成子代理任务"
            : "查看执行记录";

  const tip = dispatchFailed
    ? `${meta.name}（派发失败：${errText || "未知原因"}）`
    : errText
      ? `${meta.name}（${stopped ? "已停止" : "失败"}：${errText}）点击查看完整会话`
      : `${meta.name} · ${roleLabel}（点击查看完整会话）`;

  return (
    <div className="tc-node tc-subagent">
      <Tooltip content={tip}>
        <div
          className={"subagent-card" + (clickable ? " clickable" : "")}
          data-tool-state={running ? "running" : failed ? "fail" : "done"}
          role={clickable ? "button" : undefined}
          tabIndex={clickable ? 0 : undefined}
          onKeyDown={(e) => { if (clickable && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openSubagent(meta.agentId, meta.name); } }}
          onClick={clickable ? () => openSubagent(meta.agentId, meta.name) : undefined}
        >
          <SubagentAvatar role={role} running={running} />
          <div className="sa-body">
            <div className="sa-top">
              <span className="sa-title">{displayTitle}</span>
              <span className="sa-role-pill">{roleLabel}</span>
              <span className="sa-right">
                <span className={`sa-state ${stateClass}`}>
                  {running && <span className="sa-dot-run" aria-hidden />}
                  {done && <IconCheck size={12} />}
                  {failed && <IconAlertCircle size={12} />}
                  <span>{stateText}{durationText ? ` · ${durationText}` : ""}</span>
                </span>
                {clickable && (
                  <span className="sa-open-hint" aria-hidden>
                    <IconChevronRight size={13} />
                  </span>
                )}
              </span>
            </div>
            <div className={"sa-sub" + (errText ? " is-error" : "")}>
              <span className={isActionShine ? "text-shine" : undefined}>{secondLine}</span>
            </div>
          </div>
        </div>
      </Tooltip>
    </div>
  );
});
