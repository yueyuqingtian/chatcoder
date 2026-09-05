/** PlanCard: 计划确认卡（对齐原型图一：流式紧凑卡片，按时间线固定持久化，执行中/执行后不消失） */
import { memo, useEffect, useRef, useState } from "react";
import { IconClipboard } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { api, type MessageOut } from "../../api/client";

export const PlanCard = memo(function PlanCard({
  turnId,
  fallbackPlan,
  embedded = false,
  msg,
}: {
  turnId?: number | null;
  fallbackPlan?: any;
  /** 是否内嵌在 TurnGroup 内部（为 true 时外层不带 .turn-group 容器避免重复间距） */
  embedded?: boolean;
  /** plan-865: 时间线 PLAN 消息（预览/确认）——按数据库位置直驱卡片，无需 plansByTurn 兜底 */
  msg?: MessageOut | null;
}) {
  const plansByTurn = useChatStore((s) => s.plansByTurn);
  const pendingPlan = useChatStore((s) => s.pendingPlan);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const streamingBuffers = useChatStore((s) => s.streamingBuffers);

  // 时间线消息兜底：content 提供 plan_doc_path/plan_status（历史消息无卡片数据时卡片仍可渲染）
  const msgPlan = msg
    ? (() => {
        const c = (msg.content ?? {}) as Record<string, unknown>;
        const st = String(c.plan_status ?? "");
        const mStatus: "awaiting_confirmation" | "confirmed" | "cancelled" | "superseded" =
          st === "confirmed" ? "confirmed" : st === "cancelled" ? "cancelled"
          : st === "superseded" ? "superseded" : "awaiting_confirmation";
        return {
          turnId,
          task: "任务执行计划",
          planDocPath: String(c.plan_doc_path ?? ""),
          status: mStatus,
        };
      })()
    : null;

  const plan =
    (turnId != null ? plansByTurn[turnId] : null) ||
    msgPlan ||
    fallbackPlan ||
    (pendingPlan?.turnId === turnId ? pendingPlan : null);

  // Hooks 必须在条件 return 之前调用（React 规则）：plan 从无到有时 hooks 数量不变才不会崩溃
  // plan-644: 去掉会话级旧命名兜底（chatcoder-plan-<sid>.md 已废弃，避免打开错误文件）；
  // 计划卡必有真实路径（后端持久化 plan_doc_path / task.proposed 广播）
  const planDocPath = plan?.planDocPath || "ai/chatcoder-plan.md";
  const title = plan?.task || "任务执行计划";
  const isRunningThisTurn = Boolean(runningTurnId && turnId && runningTurnId === turnId);
  // plan-633: 执行阶段复用规划 turn（execute_confirmed_plan 不换 turn），runningTurnId === turnId
  // 无法区分「规划流式」与「执行流式」--仅未确认/未取消时流式内容才视为规划文本；
  // 确认后 turn 再运行一律视为执行阶段，卡片固定展示计划文档内容，不吞执行流式文本。
  const isAwaiting = plan?.status === "awaiting_confirmation" || (pendingPlan && pendingPlan.turnId === turnId);
  const isConfirmed = plan?.status === "confirmed";
  const isCancelled = plan?.status === "cancelled";
  /** plan-644: 已被更新方案取代（后端 turn.plan_status=superseded） */
  const isSuperseded = plan?.status === "superseded";
  const isPlanningStream = isRunningThisTurn && !isConfirmed && !isCancelled && !isSuperseded;
  const streamText = isPlanningStream ? Object.values(streamingBuffers).join("") : "";

  const [fileContent, setFileContent] = useState<string>("");
  const contentRef = useRef<HTMLDivElement>(null);
  const [isOverflowing, setIsOverflowing] = useState(false);

  useEffect(() => {
    let active = true;
    if (currentProjectId && planDocPath) {
      api
        .readProjectFile(currentProjectId, planDocPath)
        .then((res) => {
          if (active && res?.content) setFileContent(res.content);
        })
        .catch(() => {});
    }
    return () => {
      active = false;
    };
    // plan-633: 依赖含 isRunningThisTurn--执行结束（runningTurnId 离开本 turn）时重读计划 md，
    // 卡片展示 AI 执行期间更新过的最新文档内容（执行中保持挂载时快照）。
  }, [currentProjectId, planDocPath, streamText, isRunningThisTurn]);

  // displayMarkdown 需在条件 return 之前计算：溢出检测 useEffect 依赖它（hooks 顺序约束）
  const displayMarkdown =
    streamText ||
    fileContent ||
    `已在 \`${planDocPath}\` 生成方案文档，点击下方按钮可在右侧面板预览完整计划。`;

  // 内容溢出时才启用底部渐隐遮罩（plan-632: 对齐图二）；流式规划内容持续增长，随 displayMarkdown 重新检测；
  // overflow: hidden 下 scrollHeight 仍反映内容总高度，遮罩启用后检测依然有效
  useEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    setIsOverflowing(el.scrollHeight > el.clientHeight + 1);
  }, [displayMarkdown]);

  if (!plan) return null;

  const cardNode = (
    <div className="plan-inline-card">
      <div className="plan-inline-head">
        <div className="plan-inline-head-left">
          <span className="plan-inline-icon"><IconClipboard size={13} /></span>
          <span className="plan-inline-title">计划：{title}</span>
        </div>
        {isPlanningStream ? (
          <span className="plan-inline-badge running">规划中…</span>
        ) : isAwaiting ? (
          <span className="plan-inline-badge awaiting">待确认</span>
        ) : isConfirmed ? (
          <span className="plan-inline-badge confirmed">✓ 已确认执行</span>
        ) : isCancelled ? (
          <span className="plan-inline-badge cancelled">已调整</span>
        ) : isSuperseded ? (
          <span className="plan-inline-badge superseded">已被新方案取代</span>
        ) : null}
      </div>

      <div
        ref={contentRef}
        className={isOverflowing ? "plan-compact-markdown is-faded" : "plan-compact-markdown"}
      >
        <MarkdownContent>{displayMarkdown}</MarkdownContent>
      </div>

      <div className="plan-inline-footer">
        <div className="plan-inline-path-hint">
          <span>已创建计划</span>
          <code>{planDocPath}</code>
        </div>
        <div className="plan-inline-actions">
          <button
            type="button"
            className="plan-inline-view"
            onClick={() => {
              usePanelStore.getState().setPreviewPath(planDocPath);
              usePanelStore.getState().openPanel();
              usePanelStore.getState().openTab("files");
            }}
          >
            查看完整计划 →
          </button>
        </div>
      </div>
    </div>
  );

  if (embedded) return cardNode;
  return <div className="turn-group">{cardNode}</div>;
});
