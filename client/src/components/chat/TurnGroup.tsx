/** TurnGroup：单个 turn 容器。
 * - 思考块按时间顺序穿插在消息与工具调用之间
 * - 「已工作」计时条是 AI 回复顶部状态块（计划 turn 与普通 turn 口径一致）；
 *   计划卡固定渲染在规划段之后、执行折叠流之上，刷新前后位置不漂移
 * - 工作过程（思考/工具/子代理）支持折叠，任务完成后自动默认折叠（对齐图 8）；
 *   计划 turn 的规划段与执行段共享同一折叠状态（plan-655），无"上面展开、中间折叠"割裂
 * - 最终回答（Markdown/产物/摘要）与计划卡始终展示
 * - 操作行挂到消息下方
 */
import { useCallback, memo, useEffect, useState, useMemo } from "react";
import { CompactCard } from "./CompactCard";
import { MessageActions } from "./MessageActions";
import { PlanCard } from "./PlanCard";
import type { SubagentMetaLite } from "./SubagentCard";
import { PluginSlot } from "../../plugins/registry";
import { MarkdownContent } from "../MarkdownContent";
import { IconRotateCcw, IconArrowToggle } from "../icons";
import type { TimelineEntry, TurnItem } from "./timeline";
import { msgText } from "./timeline";
import { useChatStore } from "../../store/chat";
import { parseUtc } from "../../utils/time";
import { AttachmentCard, attachmentsOf } from "./AttachmentCard";

/** 「已工作 X 分 X 秒」计时条与工作过程折叠切换（对齐图 8） */
function WorkTimer({
  turnId,
  isRunning,
  hasProcess,
  collapsed,
  onToggleCollapsed,
}: {
  turnId: number | null;
  isRunning: boolean;
  hasProcess?: boolean;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}) {
  const turn = useChatStore((s) => s.turns.find((t) => t.id === turnId));
  const [, tick] = useState(0);
  useEffect(() => {
    if (!isRunning) return;
    const t = setInterval(() => tick((v) => v + 1), 1000);
    return () => clearInterval(t);
  }, [isRunning]);
  if (!turn?.started_at) return null;
  const start = parseUtc(turn.started_at);
  if (!start) return null;

  let end = Date.now();
  if (!isRunning && turn.completed_at) {
    const completed = parseUtc(turn.completed_at);
    if (completed > 0) end = completed;
  }
  const sec = Math.max(0, Math.round((end - start) / 1000));
  const min = Math.floor(sec / 60);
  const label = min > 0 ? `${min} 分 ${sec % 60} 秒` : `${sec} 秒`;

  return (
    <div
      className={`turn-worktime${hasProcess ? " clickable" : ""}`}
      onClick={hasProcess ? onToggleCollapsed : undefined}
      title={hasProcess ? (collapsed ? "展开工作过程" : "折叠工作过程") : undefined}
    >
      <span>已工作 {label}</span>
      {hasProcess && (
        <span className="turn-worktime-arrow">
          <IconArrowToggle open={!collapsed} size={11} />
        </span>
      )}
    </div>
  );
}

export const TurnGroup = memo(function TurnGroup({
  entry,
  isRunning,
  rolledBack = false,
  subagents,
  actions = "full",
  hasPlan = false,
  planAnchorMsgId = null,
}: {
  entry: Extract<TimelineEntry, { kind: "turn" }>;
  isRunning: boolean;
  rolledBack?: boolean;
  subagents?: SubagentMetaLite[];
  actions?: "full" | "copy-only" | "none";
  hasPlan?: boolean;
  /** plan-604: 锚定消息 id（方案汇报正文）——锚点及之前的 AI 项为「规划段」，计划卡渲染在规划段末尾 */
  planAnchorMsgId?: number | null;
}) {
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  const items = entry.items;
  const turnId = entry.turnId;

  const aiItemsWithIndex = useMemo(() => {
    return items
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => item.kind !== "user");
  }, [items]);

  // v41: 首条用户消息（turn 触发消息）固定渲染在 turn 顶部用户消息区；
  // 其余 user item 为运行中注入，就地渲染在时间序位置（见 flowItems）
  const firstUserIdx = useMemo(() => items.findIndex((it) => it.kind === "user"), [items]);

  // 找到最后一个 text 项作为 AI 的最终总结汇报
  let finalReportOriginalIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "text") {
      finalReportOriginalIdx = k;
      break;
    }
  }

  // plan-604: 计划卡固定化——以锚定消息（方案汇报正文；task.proposed 实时锚定与历史恢复同源）
  // 为界把 AI 项切成两段：锚点及之前的「规划段」（含思考/工具/方案文本）始终展开；
  // 锚点未命中时回退 plan-538 口径（开头连续 text 项为规划方案文本），卡片位置仍确定不漂移。
  const planSplitIdx = useMemo(() => {
    if (!hasPlan || planAnchorMsgId == null) return -1;
    for (const { item, index } of aiItemsWithIndex) {
      const mid = "msg" in item ? (item as { msg: { id: number } }).msg.id : null;
      if (mid === planAnchorMsgId) return index;
    }
    return -1;
  }, [hasPlan, planAnchorMsgId, aiItemsWithIndex]);

  const planningItems = useMemo(() => {
    if (!hasPlan) return [];
    if (planSplitIdx >= 0) return aiItemsWithIndex.filter(({ index }) => index <= planSplitIdx);
    const out: typeof aiItemsWithIndex = [];
    for (const e of aiItemsWithIndex) {
      if (e.item.kind === "text") out.push(e);
      else break;
    }
    return out;
  }, [hasPlan, planSplitIdx, aiItemsWithIndex]);

  // v41: flowItems = 除首条用户消息与规划段外的全部 items（含注入用户消息，按落库位置
  // 就地渲染）；此前注入消息被集中提升到 turn 顶部用户消息区，与实际发送位置不符。
  const flowItems = useMemo(() => {
    const skip = new Set(planningItems.map(({ index }) => index));
    return items
      .map((item, index) => ({ item, index }))
      .filter(({ index }) => index !== firstUserIdx && !skip.has(index));
  }, [items, firstUserIdx, planningItems]);

  const hasProcessBeforeFinal = useMemo(() => {
    if (finalReportOriginalIdx < 0) return false;
    return flowItems.some(({ index }) => index < finalReportOriginalIdx);
  }, [flowItems, finalReportOriginalIdx]);

  // plan-655: 统一折叠口径——计划 turn 完成后，规划段（探索/方案编写）与执行段
  // 过程一起折叠，消除"上面展开、中间折叠"的割裂；无最终汇报（异常结束）时
  // 全部展开，保证错误与过程可见。
  const hasPlanningProcess = planningItems.length > 0;
  const hasProcess =
    hasProcessBeforeFinal || (finalReportOriginalIdx >= 0 && hasPlanningProcess);

  // 任务完成且有最终汇报时，工作过程自动折叠；运行中默认展开
  const [userToggledCollapsed, setUserToggledCollapsed] = useState<boolean | null>(null);
  const processCollapsed =
    userToggledCollapsed !== null ? userToggledCollapsed : !isRunning && hasProcess;

  let lastThinkingIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "thinking") {
      lastThinkingIdx = k;
      break;
    }
  }

  const rollbackFn = useCallback(() => {
    if (turnId != null) requestRollbackPreview(turnId);
  }, [turnId, requestRollbackPreview]);
  const onRollback = turnId != null && !rolledBack ? rollbackFn : undefined;

  let lastTextIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "text") {
      lastTextIdx = k;
      break;
    }
  }

  const renderedSubagentNames = new Set<string>();
  for (const it of items) {
    if (it.kind === "subagent") {
      const args = (it.msg.content as Record<string, unknown>)?.args as Record<string, unknown> | undefined;
      const title = String(args?.task_title ?? "");
      if (title) renderedSubagentNames.add(title);
    }
  }

  const unplacedSubagents = (subagents || []).filter(
    (sa) =>
      !renderedSubagentNames.has(sa.name) &&
      !renderedSubagentNames.has(sa.name.replace(/^探索[·:：\s]*/, ""))
  );

  const subagentNode =
    unplacedSubagents.length > 0 ? (
      <div className="turn-subagents">
        {unplacedSubagents.map((sa) => (
          <PluginSlot key={sa.agentId} slot="subagent-card" meta={sa} />
        ))}
      </div>
    ) : null;

  const renderAiItem = (item: TurnItem, i: number) => {
    switch (item.kind) {
      // v41: 注入的用户消息（非首条）就地渲染在时间序位置（运行中由 MessageFlow
      // 剥离到流式段之后，此分支服务 turn 结束后的落库位置渲染）
      case "user":
        return (
          <div key={i} className="turn-item turn-item-user">
            <div className="turn-user-bubble">
              {msgText(item.msg.content) && <div className="turn-user-text">{msgText(item.msg.content)}</div>}
              {attachmentsOf(item.msg.content).map((a) => (
                <AttachmentCard key={a.file_id || a.url} att={a} />
              ))}
            </div>
          </div>
        );
      case "thinking":
        return (
          <PluginSlot
            key={i}
            slot="thinking-block"
            text={msgText(item.msg.content)}
            active={isRunning && i === lastThinkingIdx}
            turnId={entry.turnId ?? undefined}
            agentId={item.msg.sender_id ?? undefined}
          />
        );
      case "tools":
        return <PluginSlot key={i} slot="tool-tree" nodes={item.nodes} />;
      case "subagent": {
        const c = item.msg.content as Record<string, unknown>;
        const args = (c?.args && typeof c.args === "object" ? c.args : {}) as Record<string, unknown>;
        const taskTitle = String(args.task_title ?? "子代理");
        const matched = (subagents || []).find(
          (sa) => sa.name === taskTitle || sa.name === `探索·${taskTitle}` || sa.name.includes(taskTitle)
        );
        const meta: SubagentMetaLite = matched || {
          agentId: Number(item.msg.sender_id ?? item.msg.id),
          name: taskTitle,
          status: "running",
        };
        return (
          <div key={i} className="turn-item turn-item-subagent" style={{ margin: "0 0 0 28px" }}>
            <PluginSlot slot="subagent-card" meta={meta} />
          </div>
        );
      }
      case "text":
        return (
          <div key={i} className="turn-item turn-item-text">
            <div className="turn-agent-text">
              <MarkdownContent>{msgText(item.msg.content)}</MarkdownContent>
            </div>
            {!isRunning && i === lastTextIdx && (
              <MessageActions entry={entry} onRollback={onRollback} scope="ai" actions={actions} />
            )}
          </div>
        );
      case "summary":
        if ((item.msg.content as Record<string, unknown>).checkpoint === true) {
          return <CompactCard key={i} msg={item.msg} />;
        }
        return (
          <div key={i} className="turn-item turn-item-summary">
            <MarkdownContent>{msgText(item.msg.content)}</MarkdownContent>
          </div>
        );
      case "error":
        return (
          <div key={i} className="turn-item turn-item-error">
            <svg className="err-icon" viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
              <path
                fill="currentColor"
                d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"
              />
            </svg>
            <div className="err-body">
              <div className="err-title">执行出错</div>
              <div className="err-msg">{msgText(item.msg.content) || "执行出错"}</div>
            </div>
          </div>
        );
      // v2.2 (对齐 zcode 3.11): 系统分割线（模型切换 / 目标停止提示等）
      case "divider":
        return (
          <div key={i} className="turn-item turn-item-divider">
            <span className="turn-divider-line" />
            <span className="turn-divider-text">{msgText(item.msg.content)}</span>
            <span className="turn-divider-line" />
          </div>
        );
      // plan-671: 目标续跑消息——细分隔线（zcode model-only 语义，不渲染用户气泡）
      case "goal-continuation": {
        const gc = item.msg.content as Record<string, unknown>;
        const gn = Number(gc.goal_turn ?? 0);
        return (
          <div key={i} className="turn-item turn-item-divider goal-continuation-divider">
            <span className="turn-divider-line" />
            <span className="turn-divider-text">{gn > 0 ? `⟳ 目标续跑 · 第 ${gn} 轮` : "⟳ 目标续跑"}</span>
            <span className="turn-divider-line" />
          </div>
        );
      }
      default:
        return null;
    }
  };

  return (
    <div className="turn-group">
      {rolledBack && (
        <div className="turn-rolledback-banner">
          <IconRotateCcw size={12} />
          该轮次已回滚（其改动已撤销，期间消息已清理）
        </div>
      )}

      {/* 用户消息区（v41: 仅首条触发消息；注入消息由 flowItems 就地渲染） */}
      {(() => {
        const firstUser = firstUserIdx >= 0 ? items[firstUserIdx] : null;
        if (!firstUser || firstUser.kind !== "user") return null;
        return (
          <div className="turn-item turn-item-user">
            <div className="turn-user-bubble">
              {msgText(firstUser.msg.content) && (
                <div className="turn-user-text">{msgText(firstUser.msg.content)}</div>
              )}
              {attachmentsOf(firstUser.msg.content).map((a) => (
                <AttachmentCard key={a.file_id || a.url} att={a} />
              ))}
            </div>
            {!isRunning && (
              <MessageActions entry={entry} onRollback={onRollback} scope="user" actions={actions} />
            )}
          </div>
        );
      })()}

      {/* AI 执行区与工作计时条：v40 统一放入单一 flex 容器（.turn-flow gap 节奏），
          工作过程折叠容器只负责隐藏/展开，不再叠加第二套 margin。
          plan-604: 「已工作」计时条回归 AI 回复顶部状态块（v25 口径，计划 turn 与普通 turn 一致）；
          规划段 + 计划卡固定渲染在计时条之下、执行折叠流之上——卡片不再悬在「已工作」上方。
          v41: flowItems 含注入用户消息--AI 尚无落库项但已有注入时也渲染本容器；
          仅剩计划卡（turn 尚无 AI 项）时同样渲染，卡片不再有 turn-flow 外的第二渲染位 */}
      {(flowItems.length > 0 || planningItems.length > 0 || hasPlan) && (
        <div className="turn-flow">
          <WorkTimer
            turnId={turnId}
            isRunning={isRunning}
            hasProcess={hasProcess}
            collapsed={processCollapsed}
            onToggleCollapsed={() => setUserToggledCollapsed(!processCollapsed)}
          />

          {/* 规划段（锚点及之前的 AI 项：方案说明文本与规划期思考/工具）——plan-655
              纳入工作过程折叠容器，与执行段共享折叠状态；计划卡与最终汇报始终展示 */}
          {hasPlanningProcess && (
            <div className={`turn-process-container${processCollapsed ? " collapsed" : ""}`}>
              {planningItems.map(({ item, index }) => renderAiItem(item, index))}
            </div>
          )}

          {/* 计划卡：紧随规划段，位置固定（task.proposed 实时锚定 = 历史恢复锚定，刷新前后不漂移） */}
          {hasPlan && <PlanCard turnId={turnId} embedded />}
          {subagentNode}

          {/* 工作过程时间线保序折叠：将最终汇报前的所有思考、工具与中间过程说明按原始时间先后顺序折叠（对齐图 8） */}
          {hasProcessBeforeFinal && (
            <div className={`turn-process-container${processCollapsed ? " collapsed" : ""}`}>
              {flowItems.filter(({ index }) => index < finalReportOriginalIdx).map(({ item, index }) => renderAiItem(item, index))}
            </div>
          )}

          {/* 最终结果与汇报（最终 Markdown 文本、产物、摘要等），折叠时始终展示在下方 */}
          {flowItems
            .filter(({ index }) => !hasProcessBeforeFinal || index >= finalReportOriginalIdx)
            .map(({ item, index }) => renderAiItem(item, index))}
        </div>
      )}
    </div>
  );
});
