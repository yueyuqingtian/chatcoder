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
}: {
  entry: Extract<TimelineEntry, { kind: "turn" }>;
  isRunning: boolean;
  rolledBack?: boolean;
  subagents?: SubagentMetaLite[];
  actions?: "full" | "copy-only" | "none";
  hasPlan?: boolean;
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

  // plan-865: 规划段/执行段统一为「执行过程」折叠（见 processItems）——不再按锚点切分，
  // 计划卡由时间线 PLAN 消息（case "plan"）或旧会话 plansByTurn 兜底渲染。

  // plan-865 折叠口径：只折叠「最终汇报（最后一条 text）之前」的 AI 执行过程
  // （思考/工具/中间说明/计划预览消息及计划卡）；最终汇报与操作行始终展示。
  // 异常中断（interrupted/failed/rolled_back）不折叠（错误与过程必须可见）。
  const turnRowStatus = useChatStore((s) =>
    entry.turnId != null ? s.turns.find((t) => t.id === entry.turnId)?.status : undefined
  );
  const abnormalTurn = turnRowStatus === "interrupted" || turnRowStatus === "failed" || turnRowStatus === "rolled_back";

  // 执行过程项 = 除首条用户消息外、位于最终汇报之前的所有 AI 项
  const processItems = useMemo(
    () => (finalReportOriginalIdx >= 0
      ? items
          .map((item, index) => ({ item, index }))
          .filter(({ index }) => index !== firstUserIdx && index < finalReportOriginalIdx)
      : []),
    [items, firstUserIdx, finalReportOriginalIdx]
  );
  // v0.3.1: 仅当存在过程项且存在最终汇报文本且非异常时才形成「折叠过程 + 直显汇报」结构；
  // 若无最终汇报文本（运行中/异常中断/纯工具调用无 text 总结），所有项全部直显展开，绝不误折叠/误吞
  const hasProcess = processItems.length > 0 && finalReportOriginalIdx >= 0 && !abnormalTurn;

  // 任务完成且有最终汇报时，工作过程自动折叠；运行中默认展开。
  // v0.3.1 (plan-190-898): 方案等待用户确认阶段（awaiting_confirmation）必须保持展开，
  // 确保计划卡预览在规划阶段完成后始终展现在最底部；用户手动点击计时条折叠时才尊重手动状态。
  const isAwaitingConfirmation = turnRowStatus === "awaiting_confirmation";
  const [userToggledCollapsed, setUserToggledCollapsed] = useState<boolean | null>(null);
  const processCollapsed =
    userToggledCollapsed !== null
      ? userToggledCollapsed
      : (!isRunning && hasProcess && !isAwaitingConfirmation);

  let lastThinkingIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "thinking") {
      lastThinkingIdx = k;
      break;
    }
  }

  // plan-865: 时间线 PLAN 消息（预览/确认）——最后一条位置渲染计划卡；
  // hasPlanMsg=true 时不再走 plansByTurn 兜底渲染位，避免重复卡片
  let lastPlanItemIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "plan") { lastPlanItemIdx = k; break; }
  }
  const hasPlanMsg = lastPlanItemIdx >= 0;

  const rollbackFn = useCallback(() => {
    if (turnId != null) requestRollbackPreview(turnId);
  }, [turnId, requestRollbackPreview]);
  const onRollback = turnId != null && !rolledBack ? rollbackFn : undefined;

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

  // v0.3.1: 外层容器渲染守卫——只要存在任何非首条用户消息的项、计划卡或子代理，必须完整渲染 AI 回复区
  const hasAnyAiContent =
    items.some((_, index) => index !== firstUserIdx) || hasPlan || subagentNode != null;

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
      case "plan": {
        // plan-865: 计划预览/确认消息——按数据库时间线位置渲染；同 turn 多条 plan 消息
        // 只在最后一条位置渲染卡片（携带最新状态），之前的渲染为细提示行
        const isLastPlan = i === lastPlanItemIdx;
        return isLastPlan ? (
          <PlanCard key={i} turnId={entry.turnId ?? undefined} embedded msg={item.msg} />
        ) : (
          <div key={i} className="turn-item plan-msg-item">
            <span>{msgText(item.msg.content)}</span>
          </div>
        );
      }
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
          plan-865/v0.3.1 折叠口径：有最终汇报时，折叠容器只收「最终汇报之前的 AI 执行过程」
          （思考/工具/中间说明/计划预览消息及卡片），最终汇报与操作行始终展示；
          无最终汇报（纯工具、执行中、异常中断等）时全量直显，绝对不吞工具调用与思考块。 */}
      {hasAnyAiContent && (
        <div className="turn-flow">
          {hasProcess && (
            <WorkTimer
              turnId={turnId}
              isRunning={isRunning}
              hasProcess={hasProcess}
              collapsed={processCollapsed}
              onToggleCollapsed={() => setUserToggledCollapsed(!processCollapsed)}
            />
          )}

          {/* 1. 有最终汇报时：过程项进入可折叠容器 */}
          {hasProcess && (
            <div className={`turn-process-container${processCollapsed ? " collapsed" : ""}`}>
              {processItems.map(({ item, index }) => renderAiItem(item, index))}
              {hasPlan && !hasPlanMsg && <PlanCard turnId={turnId} embedded />}
              {subagentNode}
            </div>
          )}

          {/* 2. 无最终汇报时（运行中/异常/纯工具调用无 text 总结）：所有项直显展开，杜绝隐形 */}
          {!hasProcess && (
            <div className="turn-process-container">
              {items.map((item, index) =>
                index !== firstUserIdx ? renderAiItem(item, index) : null
              )}
              {hasPlan && !hasPlanMsg && <PlanCard turnId={turnId} embedded />}
              {subagentNode}
            </div>
          )}

          {/* 3. 最终汇报（最终 text）与其后的确认/分割线等：有过程时直显在折叠条下方 */}
          {hasProcess &&
            items.map((item, index) =>
              index !== firstUserIdx && index >= finalReportOriginalIdx
                ? renderAiItem(item, index)
                : null
            )}

          {/* 问题3: AI 操作行（复制/赞踩/重试）以整个 AI 回复块为整体，展示在 turn-flow 底部。
               历史/已结束消息始终显示；运行中不展示。任务异常中断（无最终 text）也能出现。 */}
          {!isRunning && aiItemsWithIndex.length > 0 && (
            <MessageActions entry={entry} onRollback={onRollback} scope="ai" actions={actions} />
          )}
        </div>
      )}
    </div>
  );
});
