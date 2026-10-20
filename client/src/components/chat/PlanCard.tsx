/** PlanCard: 计划确认卡（流式紧凑卡片，按时间线固定持久化，执行中/执行后不消失）
 *
 * plan-20261020-pts（按用户给的参考图）：扁平白卡，无阴影——
 *  · 左上「计划」小标签（含状态），右上复制按钮；
 *  · 正文直接从方案文档开头渲染（方案标题即正文第一行），底部逐渐淡出；
 *  · 底部居中悬浮一颗胶囊按钮「查看完整计划 →」；
 *  · 待确认时卡片底部多一行「取消 / 确认执行」（参考图没有确认流程，这是本软件必需的决策入口）。
 * 预览去掉文档里的状态注释（<!-- plan-status … -->，此前被原样显示成一行乱码）。 */
import { memo, useEffect, useRef, useState } from "react";
import { IconArrowRight, IconCheck, IconClipboard, IconCopy } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { Button, Collapse, IconButton, Tooltip } from "../ui";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { api, type MessageOut } from "../../api/client";

/** plan-334-1661 S1：规划流式文本的节流读取窗口（ms）。
 *  store 的流式缓冲每次 flush（约每帧）换新引用，不节流会让卡片每帧 join 全文并重渲染。 */
const STREAM_TEXT_THROTTLE_MS = 120;

type PlanState = "planning" | "awaiting" | "confirmed" | "cancelled" | "superseded" | "idle";

/** 状态文案与色调：颜色只落在状态点上，文字保持中性 */
const STATUS_META: Record<Exclude<PlanState, "idle">, { label: string; tone: "info" | "warning" | "success" | "neutral" }> = {
  planning: { label: "规划中", tone: "info" },
  awaiting: { label: "待确认", tone: "warning" },
  confirmed: { label: "已确认执行", tone: "success" },
  cancelled: { label: "已调整", tone: "neutral" },
  superseded: { label: "已被新方案取代", tone: "neutral" },
};

/** 计划文档预处理：去掉 HTML 注释（状态标记 <!-- plan-status … -->）与开头空行。
 *  流式中途注释可能尚未闭合，未闭合的部分同样不显示。 */
function cleanPlanDoc(md: string): string {
  return md.replace(/<!--[\s\S]*?(?:-->|$)/g, "").replace(/^\s+/, "");
}

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
  /** plan-334-1661 S1：只订阅「流式正文总长度」（数字）。
   *  此前订阅整个 streamingBuffers map——该 map 每次 flush（约每帧）换新引用，
   *  卡片于是每帧重渲染；改为数字比较后，长度不变即不触发重渲染。 */
  const streamLen = useChatStore((s) => {
    let n = 0;
    const m = s.streamingBuffers;
    for (const k in m) n += m[k].length;
    return n;
  });
  // plan-282-1434（B1）：确认/取消动作由卡片自身承载（原先只有输入框上方横幅持有）
  const confirmPlanTurn = useChatStore((s) => s.confirmPlanTurn);
  const dismissPlan = useChatStore((s) => s.dismissPlan);

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

  /** plan-334-1661 S1：规划流式文本按 STREAM_TEXT_THROTTLE_MS 节流读入本地 state，
   *  替代原先「渲染期 Object.values(...).join("") + 每帧随 map 变化重渲染」。
   *  依赖 streamLen（数字）：节流窗口内的多次 flush 只读取一次。 */
  const [streamText, setStreamText] = useState("");
  const lastStreamSyncRef = useRef(0);
  useEffect(() => {
    if (!isPlanningStream) {
      setStreamText("");
      return;
    }
    const wait = Math.max(0, STREAM_TEXT_THROTTLE_MS - (performance.now() - lastStreamSyncRef.current));
    const timer = window.setTimeout(() => {
      lastStreamSyncRef.current = performance.now();
      setStreamText(Object.values(useChatStore.getState().streamingBuffers).join(""));
    }, wait);
    return () => window.clearTimeout(timer);
  }, [streamLen, isPlanningStream]);

  const [fileContent, setFileContent] = useState<string>("");
  const contentRef = useRef<HTMLDivElement>(null);
  const [isOverflowing, setIsOverflowing] = useState(false);
  /** 右上复制按钮的反馈：复制成功后 1.5 秒内显示对勾 */
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(t);
  }, [copied]);

  useEffect(() => {
    let active = true;
    // plan-334-1661 S1：规划流式期间卡片展示的就是实时流式文本，无需读取文档；
    // 此前依赖 streamText（每帧变化）⇒ 规划期间**每帧一次** readProjectFile 请求。
    // 现在只在「非规划流式」阶段读一次（进入待确认 / 执行中 / 执行结束各一次）。
    if (!isPlanningStream && currentProjectId && planDocPath) {
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
    // plan-334-1661 S1: 依赖由 streamText 改为 isPlanningStream（布尔），去掉每帧抖动源。
  }, [currentProjectId, planDocPath, isPlanningStream, isRunningThisTurn]);

  // displayMarkdown 需在条件 return 之前计算：溢出检测 useEffect 依赖它（hooks 顺序约束）
  const planMarkdown = cleanPlanDoc(streamText || fileContent);
  const displayMarkdown =
    planMarkdown || `已在 \`${planDocPath}\` 生成方案文档，点击「查看完整计划」可在右侧面板预览。`;

  // 内容溢出时才启用底部渐隐遮罩（plan-632）；流式规划内容持续增长，随 displayMarkdown 重新检测；
  // overflow: hidden 下 scrollHeight 仍反映内容总高度，遮罩启用后检测依然有效
  useEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    setIsOverflowing(el.scrollHeight > el.clientHeight + 1);
  }, [displayMarkdown]);

  if (!plan) return null;

  const state: PlanState = isPlanningStream ? "planning"
    : isAwaiting ? "awaiting"
    : isConfirmed ? "confirmed"
    : isCancelled ? "cancelled"
    : isSuperseded ? "superseded"
    : "idle";
  const status = state === "idle" ? null : STATUS_META[state];
  const openFullPlan = () => {
    usePanelStore.getState().openPlanPreview(planDocPath);
    usePanelStore.getState().openPanel();
    usePanelStore.getState().openTab("files");
  };
  const copyPlan = async () => {
    try {
      await navigator.clipboard.writeText(planMarkdown || planDocPath);
      setCopied(true);
    } catch { /* 剪贴板不可用（权限 / 非安全上下文）时静默 */ }
  };

  const cardNode = (
    <div
      className={"plan-card" + (isOverflowing ? " is-clipped" : "")}
      data-state={state}
      aria-label={`计划：${title}`}
    >
      <div className="plan-card-head">
        <span className="plan-card-label">
          <IconClipboard size={14} />
          <span>计划</span>
          {status && (
            <span className="plan-card-status" data-tone={status.tone}>
              <span className="plan-card-dot" aria-hidden />
              {status.label}
            </span>
          )}
        </span>
        <IconButton
          size="xs"
          className="plan-card-copy"
          icon={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
          title={copied ? "已复制" : "复制计划"}
          onClick={() => void copyPlan()}
        />
      </div>

      <div className="plan-card-body">
        <div ref={contentRef} className={"plan-card-preview" + (isOverflowing ? " is-faded" : "")}>
          {/* plan-334-1661 修复：规划流式中间态（每 120ms 一段新文本、永远不会命中）关闭
              Markdown LRU 缓存，避免写满缓存挤掉可复用的历史条目；稳定文本（读盘/落库）正常缓存。 */}
          <MarkdownContent cache={!(isPlanningStream && streamText)}>{displayMarkdown}</MarkdownContent>
        </div>
        {/* 正文被截断时悬浮在渐隐区上方；短计划不截断，按钮跟在正文后面，不压字 */}
        <div className="plan-card-float">
          <Tooltip content={planDocPath} side="top">
            <Button variant="primary" size="md" pill onClick={openFullPlan}>
              查看完整计划
              <IconArrowRight size={14} />
            </Button>
          </Tooltip>
        </div>
      </div>

      {/* plan-282-1434（B1）：确认动作收在卡片内部——仅"待确认"状态展开，
          发新消息后 store 会把该卡置 cancelled（status 变化），这一行随之收起。
          计划卡动作显式传 turnId：重启/历史恢复的卡片没有 pendingPlan，
          只依赖 store 的 pendingPlan 会让按钮点了没反应。 */}
      <Collapse open={Boolean(isAwaiting)} lazy>
        <div className="plan-card-decide">
          <span className="plan-card-decide-hint">确认后将按此计划开始执行</span>
          <div className="plan-card-actions">
            <Button variant="secondary" size="sm" onClick={() => void dismissPlan(turnId ?? undefined)}>取消</Button>
            <Button variant="primary" size="sm" onClick={() => void confirmPlanTurn(true, turnId ?? undefined)}>确认执行</Button>
          </div>
        </div>
      </Collapse>
    </div>
  );

  if (embedded) return cardNode;
  return <div className="turn-group">{cardNode}</div>;
});
