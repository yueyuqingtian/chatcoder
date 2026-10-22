/** TurnGroup：单个 turn 容器。
 * - 思考块按时间顺序穿插在消息与工具调用之间
 * - 「已工作」计时条是 AI 回复顶部状态块（计划 turn 与普通 turn 口径一致）；
 *   计划卡固定渲染在规划段之后、执行折叠流之上，刷新前后位置不漂移
 * - 工作过程（思考/工具/子代理）支持折叠，任务完成后自动默认折叠（对齐图 8）；
 *   计划 turn 的规划段与执行段共享同一折叠状态（plan-655），无"上面展开、中间折叠"割裂
 * - 最终回答（Markdown/产物/摘要）与计划卡始终展示

 */
import { useCallback, memo, useEffect, useState, useMemo, Fragment, type KeyboardEvent } from "react";
import { useRunningTicker } from "./useRunningTicker";
import { MessageActions } from "./MessageActions";
import type { SubagentMetaLite } from "./SubagentCard";
import { WorkflowCard } from "../workflow/WorkflowCard";
import { ToolShotCard } from "./ToolShotCard";
import { WorkflowNotice } from "../workflow/WorkflowNotice";
import { PluginSlot } from "../../plugins/registry";
import { MarkdownContent } from "../MarkdownContent";
import { ErrorCard } from "./ErrorCard";
import { IconRotateCcw, IconArrowToggle } from "../icons";
import { Tooltip } from "../ui";
import type { FlowSegment, TimelineEntry, TurnItem } from "./timeline";
import {
  msgText,
  getTurnById,
  SYSTEM_CONTINUATION_KINDS,
  isOutsideProcess,
  placeWorkflowNodeCards,
  splitFlowSegments,
} from "./timeline";
import { useChatStore } from "../../store/chat";
import { TurnChangesCard } from "./TurnChangesCard";
import { parseUtc } from "../../utils/time";
import { MessageImageGrid, attachmentsOf } from "./AttachmentCard";
import { UserBubble } from "./UserBubble";
import { recordComponentRender } from "../../perf/metrics";

/** plan-20261020-bgtask：系统唤醒一轮时的分隔线文案（按唤醒原因）。缺省 = 子代理完成唤醒（历史口径）。 */
const WAKEUP_DIVIDER_TEXT: Record<string, string> = {
  bg_command_done: "后台命令已完成，继续执行",
  subsession_question: "子会话有新提问，继续处理",
  // plan-20261022-release-ci：派出去的子会话全部结束 → 叫醒主代理汇总
  subsession_result: "子会话已全部结束，继续汇总",
  main_reply: "主会话已答复，继续执行",
};

/** plan-20261022-subagent-dispatch-fail：这次 spawn_subagent 的结果是否表示「派发失败」。
 *
 *  背景：子代理卡片的**存在**由工具调用消息决定（调用先落库、再执行），而被后端拒绝的派发
 *  不会产生任何子代理记录。此前这类卡片匹配不到 meta 就兜底成「执行中」，且沿用主代理的
 *  sender_id 当 agentId——点击后打开一个空面板，看起来像「并发上限没生效」。
 *  成功派发返回 "Subagent #N dispatched…" / "Exploration subagent #N finished…"，
 *  失败一律以 "Error:" 开头。 */
function subagentDispatchError(result?: Extract<TurnItem, { kind: "subagent" }>["result"]): string | null {
  if (!result) return null;
  const rc = (result.content ?? {}) as Record<string, unknown>;
  const output = typeof rc.output === "string" ? rc.output : "";
  const firstLine = output.trim().split("\n")[0]?.trim() ?? "";
  const errText = typeof rc.error === "string" ? rc.error.trim() : "";
  // 历史消息里 ok 恒为 true（旧后端缺陷），因此同时保留文本前缀判定
  const failed = rc.ok === false || /^Error[：:]/i.test(firstLine);
  if (!failed) return null;
  return (firstLine || errText || "派发失败").replace(/^Error[：:]\s*/i, "").slice(0, 200);
}

/** plan-282-1421：turn 的「已结束」状态集合——AI 操作行必须在本轮进入这些状态后才显示。 */
const TERMINAL_TURN_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "interrupted",
  "cancelled",
  "rolled_back",
]);

/** 「已工作 X 分 X 秒」计时条与工作过程折叠切换（对齐图 8）。
 *
 * v36 修复（用户反馈）：flow="subagent"（右面板）时起止时间改取 subagentMeta
 * （与面板头部「用时」同源）。此前固定查主会话 `turns`，而右面板的 turnId 是子代理
 * 消息的 turn_id：切会话/刷新/主会话列表未覆盖该 id 时 `turn` 为空 → 整条 return null，
 * 计时条消失且过程区再无折叠入口。现在无时间数据也不再整条消失（退化为「工作过程」
 * 文案），保证折叠入口始终可点。 */
function WorkTimer({
  turnIds,
  agentId,
  flow = "main",
  isRunning,
  endFloorMs = 0,
  canCollapse,
  collapsed,
  onToggleCollapsed,
}: {
  /** 整轮的 turn id（含并入的续跑轮）：计时从最早开始算起，到最晚完成为止 */
  turnIds: readonly number[];
  agentId?: number;
  flow?: "main" | "subagent";
  /** 整轮是否仍在进行（含主代理已收工、后台工作流 / 子代理仍在跑）：进行中计时走动、箭头隐藏 */
  isRunning: boolean;
  /** plan-20261014-actions-wait：整轮含后台程序时的终点下限（毫秒）——已结束时终点取「最晚完成轮」与它较晚者。
   *  工作流在后台跑，主代理那一轮早已结束；用户中途停止工作流、之后也没有唤醒轮时，只取最晚完成轮会让用时
   *  从走动中的数字倒退回主代理那一轮的时长。0 = 无。 */
  endFloorMs?: number;
  /** 本轮结构上可折叠（有过程且有最终汇报）：据此预留箭头槽位，运行中也占位、不显示 */
  canCollapse?: boolean;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}) {
  // plan-20261014-fix4：运行中不可折叠——箭头槽位保留（隐藏），完成后淡入，计时条宽度不跳动
  const toggleable = Boolean(canCollapse) && !isRunning;
  const subagentFlow = flow === "subagent";
  // plan-75-334 阶段1：只订阅**起止时间 primitive**。此前返回整个 turn / subagentMeta 对象，
  // turns 列表或 meta 上任何字段变化都会让每个可见计时条重渲染；primitive 只在起止时间
  // 真正变化时才触发更新，共享秒级 ticker 的行为不变。
  const subStart = useChatStore((s) =>
    subagentFlow && agentId != null ? s.subagentMeta[agentId]?.startedAt ?? null : null
  );
  const subEnd = useChatStore((s) =>
    subagentFlow && agentId != null ? s.subagentMeta[agentId]?.endedAt ?? null : null
  );
  // 整轮起止（plan-282-1530）：起点取最早开始，终点取最晚完成——续跑轮并入后计时不因唤醒而重置
  const turnStart = useChatStore((s) => {
    if (subagentFlow) return null;
    let best: string | null = null;
    let bestMs = Infinity;
    for (const id of turnIds) {
      const raw = getTurnById(s.turns, id)?.started_at;
      const ms = raw ? parseUtc(raw) : 0;
      if (ms > 0 && ms < bestMs) { best = raw ?? null; bestMs = ms; }
    }
    return best;
  });
  const turnEnd = useChatStore((s) => {
    if (subagentFlow) return null;
    let best: string | null = null;
    let bestMs = 0;
    for (const id of turnIds) {
      const raw = getTurnById(s.turns, id)?.completed_at;
      const ms = raw ? parseUtc(raw) : 0;
      if (ms > bestMs) { best = raw ?? null; bestMs = ms; }
    }
    return best;
  });
  // S8c：改用共享秒级 ticker（原先每处各自 setInterval，运行期多处独立唤醒 + 各自重渲染）。
  useRunningTicker(isRunning);

  // 起止时间：子代理面板取 meta（事件 + REST 回填），主会话取 turn 行
  const startRaw = subagentFlow ? subStart : turnStart;
  const endRaw = subagentFlow ? subEnd : turnEnd;
  const start = startRaw ? parseUtc(startRaw) : 0;

  let label = "";
  if (start > 0) {
    let end = Date.now();
    if (!isRunning && endRaw) {
      const completed = parseUtc(endRaw);
      if (completed > 0) end = Math.max(completed, endFloorMs);
    }
    const sec = Math.max(0, Math.round((end - start) / 1000));
    const min = Math.floor(sec / 60);
    label = min > 0 ? `${min} 分 ${sec % 60} 秒` : `${sec} 秒`;
  }

  // 既无时间数据又不可折叠时：主消息流维持旧行为（不渲染，避免乐观发送期闪现徒条）；
  // 子代理面板的条是唯一折叠入口——只要可折叠就继续渲染（无秒数时展示「工作过程」）。
  if (!label && !canCollapse) return null;

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!toggleable || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    onToggleCollapsed?.();
  };

  // plan-20261016-ux（问题 8）：外包一层整行容器，下边画浅色分隔线，把「工作过程」与下方内容分出层次
  // （对齐参考图 4）；运行中同样显示，整轮结束时不出现线条跳入。可点击区域仍只是文字本身。
  return (
    <div className="turn-worktime-bar">
      <Tooltip content={collapsed ? "展开工作过程" : "折叠工作过程"} disabled={!toggleable}>
        <div
          className={`turn-worktime${toggleable ? " clickable" : ""}`}
          onClick={toggleable ? onToggleCollapsed : undefined}
          onKeyDown={onKeyDown}
          role={toggleable ? "button" : undefined}
          tabIndex={toggleable ? 0 : undefined}
          aria-expanded={toggleable ? !collapsed : undefined}
        >
          <span>{label ? `已工作 ${label}` : "工作过程"}</span>
          {canCollapse && (
            <span className={`turn-worktime-arrow${toggleable ? "" : " is-hidden"}`} aria-hidden>
              <IconArrowToggle open={!collapsed} size={11} />
            </span>
          )}
        </div>
      </Tooltip>
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
  flow = "main",
  agentId,
  globalRunning,
  latestUserId,
  flowErrorTurnId,
  hiddenTurnErrors,
  turnStatus,
  awaitingBackground = false,
  backgroundEndedAt = 0,
}: {
  entry: Extract<TimelineEntry, { kind: "turn" }>;
  isRunning: boolean;
  rolledBack?: boolean;
  subagents?: SubagentMetaLite[];
  actions?: "full" | "copy-only" | "none";
  hasPlan?: boolean;
  /** plan-282-1421：数据来源。subagent 面板的 turn 状态不来自主会话 turns 列表，
   *  因此不能用主会话的全局运行态推断"是否已结束"。 */
  flow?: "main" | "subagent";
  /** v36 修复：子代理线程 id（=agentId）。右面板计时条改取 subagentMeta 的起止时间，
   *  不再依赖主会话 turns（主会话列表缺该 turn 时计时条会整条消失、无法折叠）。 */
  agentId?: number;
  /** plan-75-334 阶段1：父层统一计算的全局派生值（必需）。
   *  此前每个 TurnGroup 各自订阅 isRunning / flowError / hiddenTurnErrors / turns / messages，
   *  运行期任何相关 store 更新都会让所有可见 turn 重复执行 selector；
   *  现在由 MainMessageFlow / SubagentMessageFlow 各算一次、经 props 传下来。 */
  globalRunning: boolean;
  latestUserId: number;
  flowErrorTurnId: number | null;
  hiddenTurnErrors: number[];
  turnStatus: string | undefined;
  /** plan-20261014-actions-wait：本条目是消息流尾部一轮，且后台工作流 / 子代理仍在跑——
   *  主代理已收工但整轮没有结束（结束后服务端会唤醒续跑并并入本轮）。由 MainMessageFlow 统一计算后传入；
   *  缺省 false，子代理面板等其它宿主不受影响。 */
  awaitingBackground?: boolean;
  /** plan-20261014-actions-wait：本会话已启动的工作流里最晚的结束时刻（毫秒，0 = 无），仅尾部一轮传入。
   *  整轮结束后「已工作」用时的终点不早于它（见 WorkTimer 的 endFloorMs）。 */
  backgroundEndedAt?: number;
}) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("turnGroup");
  
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  const items = entry.items;
  const turnId = entry.turnId;
  /** 本条目覆盖的全部 turn（一轮可能并入了续跑轮）；计时、错误归属都按整轮计算 */
  const roundIds: readonly number[] = entry.turnIds ?? (turnId != null ? [turnId] : []);

  /** plan-41-228：turn 内 ERROR 消息与末尾错误卡（FlowErrorCard）是同一错误的两个渲染位。
   *  flowError 命中本轮任一 turn（或已被「关闭」）时由末尾卡独家呈现（带重试/关闭），
   *  这里不再重复渲染同一条错误。 */
  const suppressTurnError =
    flow === "main" && turnId != null
    && roundIds.some((id) => flowErrorTurnId === id || hiddenTurnErrors.includes(id));

  // v41: 首条用户消息（turn 触发消息）固定渲染在 turn 顶部用户消息区；
  // 其余 user item 为运行中注入，就地渲染在时间序位置（见 flowItems）
  const firstUserIdx = useMemo(() => items.findIndex((it) => it.kind === "user"), [items]);
  // plan-20261015-browser-agent：本轮全部截图卡（大图查看器在本轮截图之间左右切换）
  const shotLeaves = useMemo(
    () => items.flatMap((it) => (it.kind === "shot" ? [it.leaf] : [])),
    [items],
  );

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
  // plan-75-334 阶段1：本 turn 行状态由父层统一查询后传入（原先每个 TurnGroup 各自查 turns）
  const turnRowStatus = turnStatus;
  const abnormalTurn = turnRowStatus === "interrupted" || turnRowStatus === "failed" || turnRowStatus === "rolled_back";

  /** plan-282-1416（问题2 根治）：操作行门控由「全局 isRunning」改为「本 turn 行状态」。
   *  仅当**该条目确实属于正在运行的那个 turn**时才隐藏操作行——
   *  异常中断 / 手动停止 / 回滚 / 失败（interrupted/failed/rolled_back）、
   *  以及不隶属任何 turn 的独立用户消息（turnId == null）一律可见，
   *  不再因 runningTurnId 残留在旧值而永久丢失复制/回滚按钮。
   *
   *  plan-282-1422（执行期误显按钮根治）：判据从「行状态必须为 running」放宽为
   *  「行状态不得是终态」。原因是计划确认执行**复用规划 turn**（后端
   *  execute_confirmed_plan 不换 turn），该 turn 的行状态会停留在
   *  awaiting_confirmation，而 runningTurnId（=宿主传入的 isRunning）已经指向它；
   *  旧写法于是把「正在执行」误判为「已结束」，让该 turn 的首条用户消息——
   *  恰好是全局最近一条用户消息（操作行 is-latest 常显）——在执行期间
   *  异常常驻复制/回滚按钮。终态优先仍保证陈旧 runningTurnId 不会永久吞掉按钮。
   *
   *  subagent 面板例外（与 turnFinished 同口径）：其 turn 行状态来自 subagentMeta 而非
   *  主会话 turns 列表，用主会话行状态判断会取到无关值——直接用本面板运行态。 */
  const turnRunning =
    entry.turnId == null
      ? false
      : flow === "subagent"
        ? isRunning
        : isRunning && !TERMINAL_TURN_STATUSES.has(turnRowStatus ?? "");

  /** plan-282-1421：AI 操作行**必须等本轮真正结束**才显示。
   *
   *  旧门控用 !turnRunning，在以下两个窗口会短暂放行，导致按钮随流式内容
   *  上下跳动（新增条目不断改变 lastAiItemIdx，操作行跟着块尾移动）：
   *   ① 乐观发送到 turn.started 之间：runningTurnId 尚未置位、turn 行状态也为空；
   *   ② 结束事件与 turns 列表刷新之间的空档：状态未落库。
   *  现在改为「必须确认终态」：turn 行状态已知时只认终态；状态未知时要求全局不在运行中。
   *  这样执行中（含上述两个窗口）一律不渲染，彻底消除跳动。
   *
   *  subagent 面板例外：其 turn 状态不来自主会话 turns 列表（而是 subagentMeta），
   *  故直接用宿主传入的本面板运行态，与改造前行为一致。
   *
   *  plan-282-1422：turnRunning（本 turn 确在运行）优先判「未结束」——计划确认执行
   *  复用规划 turn 且行状态可能仍是 awaiting_confirmation，若只看行状态会误判已结束。
   *
   *  plan-20261014-actions-wait：主代理发起工作流 / 子代理后即收工，本 turn 行状态早已是 completed、
   *  isRunning 也是 false，但整轮并没有结束——后台跑完后服务端还会唤醒续跑，回复并入本轮。
   *  只看 turn 终态会让操作行在消息流底部「等待响应…」之上提前出现，唤醒后又消失 / 下移；
   *  故后台仍在跑（awaitingBackground，仅尾部一轮）一律视为未结束，与输入框的忙碌口径一致。 */
  const turnFinished =
    flow === "subagent"
      ? !isRunning
      : entry.turnId == null
        ? true
        : turnRunning || awaitingBackground
          ? false
          : turnRowStatus != null
            ? TERMINAL_TURN_STATUSES.has(turnRowStatus)
            : !globalRunning;

  // 执行过程项 = 除首条用户消息外、位于最终汇报之前的所有 AI 项
  const processItems = useMemo(
    () => (finalReportOriginalIdx >= 0
      ? items
          .map((item, index) => ({ item, index }))
          .filter(({ item, index }) => index !== firstUserIdx && index < finalReportOriginalIdx && !isOutsideProcess(item))
      : []),
    [items, firstUserIdx, finalReportOriginalIdx]
  );
  // v0.3.1: 仅当存在过程项且存在最终汇报文本且非异常时才形成「折叠过程 + 直显汇报」结构；
  // 若无最终汇报文本（运行中/异常中断/纯工具调用无 text 总结），所有项全部直显展开，绝不误折叠/误吞
  const hasProcess = processItems.length > 0 && finalReportOriginalIdx >= 0 && !abnormalTurn;

  // 任务完成且有最终汇报时，工作过程自动折叠；运行中默认展开。
  // v0.3.1 (plan-190-898): 方案等待用户确认阶段（awaiting_confirmation）必须保持展开，
  // 确保计划卡预览在规划阶段完成后始终展现在最底部；用户手动点击计时条折叠时才尊重手动状态。
  // 子代理面板的 turn 行状态取自主会话 turns——计划等待语义不适用，否则面板会被一直保持展开
  const isAwaitingConfirmation = flow !== "subagent" && turnRowStatus === "awaiting_confirmation";
  const [userToggledCollapsed, setUserToggledCollapsed] = useState<boolean | null>(null);
  /** plan-41-198: turn 内是否存在运行期注入的用户消息（非首条）。
   *  它属于用户输入而非 AI 执行过程，被默认折叠（max-height:0 + opacity:0）后
   *  用户会以为消息没发出去；故含注入消息的 turn 完成后默认展开，仍可由计时条手动收起。 */
  const hasInjectedUser = useMemo(
    () => items.some((it, index) => it.kind === "user" && index !== firstUserIdx),
    [items, firstUserIdx]
  );
  /** plan-20261014-actions-wait：整轮是否仍在进行——本 turn 在跑，或尾部一轮在等后台（工作流 / 子代理 / 待送达的唤醒）。
   *  计时条、折叠箭头与过程自动折叠都以它为准：主代理收工后后台仍在跑的期间，计时继续走动、箭头隐藏、过程保持展开，
   *  整轮结束后才停表并折叠（与「运行中不可折叠」同一规则）；此前这些按主代理那一轮的结束判断，
   *  工作流后台运行期间计时停住、箭头出现、过程已折叠，唤醒轮结束时用时又一下跳到含工作流的总时长。
   *  思考块的「进行中」仍只认主代理真实在跑（isRunning）。 */
  const roundActive = isRunning || awaitingBackground;
  /** plan-20261014-fix4（用户反馈「运行中也可以折叠」）：运行中强制展开、不可折叠；
   *  每次进入运行都清掉手动折叠记录——本轮（含并入的续跑轮）完成后按默认规则自动折叠，之后才可手动切换。 */
  useEffect(() => {
    if (roundActive) setUserToggledCollapsed(null);
  }, [roundActive]);
  const processCollapsed = roundActive
    ? false
    : userToggledCollapsed !== null
      ? userToggledCollapsed
      : (hasProcess && !isAwaitingConfirmation && !hasInjectedUser);

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

  /** plan-282-1416（问题3 根治）：最后一个 AI 内容项的 index（可含 tools 等无 msg 的项）。
   *  AI 操作行必须紧跟 AI 内容块末尾插入，而不是挂在 .turn-flow 底部——
   *  否则运行中注入的用户消息（时间序在其后）会被这条操作行「包」进来，
   *  表现为「AI 和用户被当成一个整体共用一条复制/点赞行」。 */
  let lastAiItemIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind !== "user") { lastAiItemIdx = k; break; }
  }
  /** 操作行锚点：最后一个非用户、且不是系统续跑的 AI 项。仅含续跑（唤醒通知尚无回复）的轮次没有锚点，不渲染操作行，
   *  避免通知下方出现一排与回复无关的复制 / 重新生成按钮。 */
  let actionAnchorIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    const kind = items[k].kind;
    if (kind !== "user" && !SYSTEM_CONTINUATION_KINDS.has(kind)) { actionAnchorIdx = k; break; }
  }

  /** 安全取项的消息 id（tools / plan 分组项无 msg，回落到 -1 仅供 key 使用） */
  const itemMsgId = (item: TurnItem): number =>
    "msg" in item && item.msg ? Number(item.msg.id) : -1;

  /** plan-282-1416（问题2）：全局最近一条用户消息 id——该条操作行常显，
   *  保证任务异常终止 / 手动停止后无需 hover 就能看到复制与回滚。
   *  plan-75-334 阶段1：改由父层一次性扫描消息尾部后经 props 传入（不再每实例重复扫描）。 */

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

  const unplacedSubagents = (subagents || [])
    .filter(
      (sa) =>
        !renderedSubagentNames.has(sa.name) &&
        !renderedSubagentNames.has(sa.name.replace(/^探索[·:：\s]*/, ""))
    )
    // plan-330-1648 M5-b: 按 agentId 升序（= 派发创建序）稳定排序，刷新前后顺序一致
    .sort((a, b) => a.agentId - b.agentId);

  /** plan-20261013-wf-order：工作流节点子代理按派发时刻挂到其创建位置对应的条目之后（无派发时刻时挂到所属工作流卡之后）；
   *  挂靠不到的仍走兜底槽位。挂靠的条目之后由挂靠段渲染这些卡片（见 splitFlowSegments）。 */
  const { attached: nodeCardsByItem, unplaced: legacyUnplaced } = placeWorkflowNodeCards(items, unplacedSubagents);
  const attachedIdx: ReadonlySet<number> = new Set(nodeCardsByItem.keys());

  const subagentNode =
    legacyUnplaced.length > 0 ? (
      <div className="turn-subagents">
        {legacyUnplaced.map((sa) => (
          <PluginSlot key={sa.agentId} slot="subagent-card" meta={sa} />
        ))}
      </div>
    ) : null;

  /** plan-330-1648 M5-b: 未落位卡片优先挂在**最后一个 AI 内容项之后**（与工具行同序）；
   *  仅当该 turn 没有任何 AI 内容项（纯用户消息等）时，才回落到过程容器位置，保证卡片不丢。 */
  const subagentFallbackInContainer = items.every((it) => it.kind === "user");

  // v0.3.1: 外层容器渲染守卫——只要存在任何非首条用户消息的项、计划卡或子代理，必须完整渲染 AI 回复区
  const hasAnyAiContent =
    items.some((_, index) => index !== firstUserIdx) || hasPlan || subagentNode != null;

  /** v36 修复：折叠入口可用性。主消息流维持原口径（有最终汇报才折叠）；
   *  子代理面板放宽为「存在 AI 内容即可折叠」——终态但无结构化汇报（失败/取消/纯工具）
   *  时 hasProcess=false，此前既无计时条也无折叠入口（用户反馈「点击后条消失、无法折叠」）。 */
  const canCollapse = hasProcess || (flow === "subagent" && hasAnyAiContent);

  /** plan-282-1416（问题3）：把 AI 操作行渲染在「AI 内容块末尾」。
   *  plan-282-1421：门控改为 turnFinished——执行中（含乐观发送与状态未落库的空档）
   *  一律不渲染，避免按钮随流式新增条目上下跳动；必须等本轮进入终态才出现。 */
  /** plan-20261018-turn-changes：本轮是否有文件变更卡片。
   *  有卡片时把 AI 操作行移到卡片下方（用户要求：复制按钮组在变更卡片之下）；
   *  没有卡片时保持原位置（紧跟最后一个 AI 内容项），否则运行中注入的用户消息
   *  会被这条操作行包进来（见 plan-282-1416）。selector 返回布尔，值不变不重渲染。 */
  const hasChangeCards = useChatStore((s) =>
    roundIds.some((id) => (s.turnChanges[id]?.length ?? 0) > 0),
  );

  /** 条目的尾部：未落位的兜底子代理卡（最后一个 AI 内容项之后）与 AI 操作行（锚点条目，终态后显示）。
   *  挂靠了节点卡的条目，尾部改由挂靠段渲染，使操作行位于节点卡之后。 */
  const renderItemTail = (i: number) => (
    <Fragment key={`tail-${i}`}>
      {/* plan-330-1648 M5-b: 未落位的子代理卡片渲染在**最后一个 AI 内容项之后**，
          而不是过程容器末尾——否则卡片会出现在 AI 文本上方（用户反馈问题1）。 */}
      {i === lastAiItemIdx && subagentNode}
      {i === actionAnchorIdx && turnFinished && !hasChangeCards && (
        <MessageActions
          entry={entry}
          onRollback={onRollback}
          scope="ai"
          actions={actions}
          ownerId={itemMsgId(items[i])}
        />
      )}
    </Fragment>
  );

  /** 挂靠段（plan-20261013-wf-order）：该条目挂靠的节点子代理卡（按派发先后，与其它常显项同为常显），
   *  随后是该条目的尾部。 */
  const renderAttachedSegment = (i: number) => (
    <Fragment key={`att-${i}`}>
      {nodeCardsByItem.get(i)?.map((sa) => (
        <div key={`node-sa-${sa.agentId}`} className="turn-item turn-item-subagent">
          <PluginSlot slot="subagent-card" meta={sa} />
        </div>
      ))}
      {renderItemTail(i)}
    </Fragment>
  );

  /** 段内的一项：条目本身；无挂靠时尾部紧随其后，有挂靠时尾部由挂靠段渲染（条目为空时挂靠段照常渲染）。 */
  const renderSegmentItem = (i: number) => {
    const node = renderAiItem(items[i], i);
    if (node == null) return null;
    return (
      <Fragment key={`ai-${i}`}>
        {node}
        {!attachedIdx.has(i) && renderItemTail(i)}
      </Fragment>
    );
  };

  const renderAiItem = (item: TurnItem, i: number) => {    switch (item.kind) {
      // v41 → 本轮：注入的用户消息（非首条）**就地渲染在时间序位置**。
      // 旧实现在运行中由 MessageFlow 把它剥离到流式段下方的独立槽位，导致
      // "发送的消息没有固定位置、会被刷到下方"；现在它始终按 id 序留在时间线内。
      case "user":
        return (
          <div key={i} className="turn-item turn-item-user">
            {/* 会话 228-1142: 图片网格在气泡外部、靠右（对齐参考图 1）；文件卡与文本留在气泡内 */}
            <MessageImageGrid atts={attachmentsOf(item.msg.content)} />
            <UserBubble msg={item.msg} />
            {/* plan-282-1416（问题2 根因 A）：turn 内的非首条用户消息（运行中注入 / 立即发送）
                此前完全没有操作行，导致「最近一条用户消息」永远没有复制/回滚。
                现补齐：归属该条消息自身，且与 turn 是否运行无关（终态即可见）。 */}
            {!turnRunning && (
              <MessageActions
                entry={entry}
                onRollback={onRollback}
                scope="user"
                actions={actions}
                alwaysVisible={item.msg.id === latestUserId}
                ownerId={item.msg.id}
              />
            )}
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
            msgId={Number(item.msg.id)}
          />
        );
      case "tools":
        return <PluginSlot key={i} slot="tool-tree" nodes={item.nodes} />;
      case "workflow":
        return (
          <div key={i} className="turn-item turn-item-workflow">
            <WorkflowCard programId={item.programId} />
          </div>
        );
      case "shot":
        // plan-20261015-browser-agent：截图卡固定在该次调用的时间线位置（常显，不随工作过程折叠）
        return (
          <div key={i} className="turn-item turn-item-shot">
            <ToolShotCard leaf={item.leaf} siblings={shotLeaves} />
          </div>
        );
      case "subagent": {
        const c = item.msg.content as Record<string, unknown>;
        const args = (c?.args && typeof c.args === "object" ? c.args : {}) as Record<string, unknown>;
        const taskTitle = String(args.task_title ?? "子代理");
        const matched = (subagents || []).find(
          (sa) => sa.name === taskTitle || sa.name === `探索·${taskTitle}` || sa.name.includes(taskTitle)
        );
        const dispatchError = subagentDispatchError(item.result);
        const meta: SubagentMetaLite = matched || (dispatchError
          // plan-20261022-subagent-dispatch-fail：派发被拒 → 明确的失败行 + 原因，
          // 不再兜底成「执行中」；agentId=0 表示没有可打开的子代理会话。
          ? { agentId: 0, name: taskTitle, status: "failed", error: dispatchError, dispatchFailed: true }
          : { agentId: Number(item.msg.sender_id ?? item.msg.id), name: taskTitle, status: "running" });
        // v36 修复：去掉内联 28px 左缩进——子代理行此前比工具调用行右移 28px，
        // 与「子代理卡片与工具调用行共用同一左侧边界」的规范不一致（用户反馈未对齐）。
        return (
          <div key={i} className="turn-item turn-item-subagent">
            <PluginSlot slot="subagent-card" meta={meta} />
          </div>
        );
      }
      case "text":
        // plan-156-619 S5d：子会话按普通会话渲染——不再把最终汇报换成六节结构化橙卡。
        // 子会话本身就是完整会话，它的输出应当与主会话同排版，否则用户会看到两套视觉语言。
        return (
          <div key={i} className="turn-item turn-item-text">
            <div className="turn-agent-text">
              <MarkdownContent>{msgText(item.msg.content)}</MarkdownContent>
            </div>
          </div>
        );
      case "summary":
        if ((item.msg.content as Record<string, unknown>).checkpoint === true) {
          // plan-282-1421：压缩卡走插件 slot（可被外挂组件替换）
          return <PluginSlot key={i} slot="compact-card" msg={item.msg} />;
        }
        return (
          <div key={i} className="turn-item turn-item-summary">
            {/* v36 (plan-321-1600 M2): 补上 .turn-agent-text 包裹——与 case "text" 同结构。
                否则 summary 消息拿不到 .turn-agent-text .md-body 下的完整 markdown 排版
                （标题/列表/代码块样式全丢，退化为浏览器默认样式）。 */}
            <div className="turn-agent-text">
              <MarkdownContent>{msgText(item.msg.content)}</MarkdownContent>
            </div>
          </div>
        );
      case "error":
        // plan-41-228：与末尾错误卡重复的那条不再渲染（见 suppressTurnError）
        if (suppressTurnError) return null;
        return (
          <div key={i} className="turn-item-wrap">
            <ErrorCard text={msgText(item.msg.content) || "执行出错"} />
          </div>
        );
      case "plan": {
        // plan-865: 计划预览/确认消息——按数据库时间线位置渲染；同 turn 多条 plan 消息
        // 只在最后一条位置渲染卡片（携带最新状态），之前的渲染为细提示行
        const isLastPlan = i === lastPlanItemIdx;
        return isLastPlan ? (
          <PluginSlot key={i} slot="plan-card" turnId={entry.turnId ?? undefined} embedded msg={item.msg} />
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
            <span className="turn-divider-text">{gn > 0 ? `目标续跑 · 第 ${gn} 轮` : "目标续跑"}</span>
            <span className="turn-divider-line" />
          </div>
        );
      }
      // v39: 子代理完成唤醒消息——细分隔线（系统生成，对应上方「等待子代理结束…」）
      // plan-20261020-bgtask：这条唤醒消息也被后台命令完成 / 子会话提问复用，文案按 wakeup_reason 区分——
      // 此前一律写「子代理已完成」，没有子代理的会话里也这么说
      case "subagent-wakeup": {
        const wakeReason = String((item.msg.content as Record<string, unknown>).wakeup_reason ?? "");
        return (
          <div key={i} className="turn-item turn-item-divider subagent-wakeup-divider">
            <span className="turn-divider-line" />
            <span className="turn-divider-text">{WAKEUP_DIVIDER_TEXT[wakeReason] ?? "子代理已完成，继续执行"}</span>
            <span className="turn-divider-line" />
          </div>
        );
      }
      // 工作流唤醒消息：系统生成的进展通知（终态或需要决定时），以可展开的工具调用行呈现
      case "workflow-wakeup":
        return (
          <div key={i} className="turn-item">
            <WorkflowNotice text={msgText(item.msg.content)} />
          </div>
        );
      default:
        return null;
    }
  };

  /** 渲染执行段（见 splitFlowSegments）：常显段原位渲染；过程段装入可折叠容器，collapsed 只作用于过程段。
   *  尾部内容（内嵌计划卡、兜底子代理卡）挂在最后一个过程段末尾，与原先过程容器内的位置一致。 */
  const renderFlowSegments = (segments: FlowSegment[], opts: { collapsed: boolean; wrap: boolean }) => {
    const needTail = opts.wrap && ((hasPlan && !hasPlanMsg) || subagentFallbackInContainer);
    // 没有任何过程项却需要承载尾部内容时，补一个空过程段
    const list: FlowSegment[] =
      needTail && !segments.some((s) => s.kind === "process")
        ? [...segments, { kind: "process", indices: [] }]
        : segments;
    let lastProcess = -1;
    list.forEach((s, n) => {
      if (s.kind === "process") lastProcess = n;
    });
    return list.map((seg, n) => {
      if (seg.kind === "sticky") return renderSegmentItem(seg.index);
      if (seg.kind === "attached") return renderAttachedSegment(seg.index);
      if (!opts.wrap) return seg.indices.map((index) => renderSegmentItem(index));
      const isLast = n === lastProcess;
      return (
        <div
          key={`proc-${seg.indices.length > 0 ? seg.indices[0] : "tail"}`}
          className={`turn-process-container${opts.collapsed ? " collapsed" : ""}`}
        >
          {/* plan-353-1738 M4a：内层承载 flex + gap 布局，外层（grid）只负责 0fr↔1fr 的真实高度过渡 */}
          <div className="turn-process-inner">
            {seg.indices.map((index) => renderSegmentItem(index))}
            {isLast && hasPlan && !hasPlanMsg && <PluginSlot slot="plan-card" turnId={turnId} embedded />}
            {isLast && subagentFallbackInContainer ? subagentNode : null}
          </div>
        </div>
      );
    });
  };

  return (
    <div className={`turn-group${turnFinished && flow === "main" && !rolledBack && hasChangeCards ? " has-changes" : ""}`}>
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
            {/* 会话 228-1142: 图片网格在气泡外部、靠右（对齐参考图 1）；文件卡与文本留在气泡内 */}
            <MessageImageGrid atts={attachmentsOf(firstUser.msg.content)} />
            <UserBubble msg={firstUser.msg} />
            {/* plan-282-1416（问题2）：门控改本 turn 行状态；最近一条用户消息常显 */}
            {!turnRunning && (
              <MessageActions
                entry={entry}
                onRollback={onRollback}
                scope="user"
                actions={actions}
                alwaysVisible={firstUser.msg.id === latestUserId}
                ownerId={firstUser.msg.id}
              />
            )}
          </div>
        );
      })()}

      {/* AI 执行区与工作计时条：v40 统一放入单一 flex 容器（.turn-flow gap 节奏），
          plan-865/v0.3.1 折叠口径：有最终汇报时，折叠容器只收「最终汇报之前的 AI 执行过程」
          （思考/工具/中间说明/计划预览消息及卡片），最终汇报与操作行始终展示；
          无最终汇报（纯工具、执行中、异常中断等）时全量直显，绝对不吞工具调用与思考块。 */}
      {/* plan-1094: 运行中即使尚无任何 AI 内容落库（发送初期/纯思考阶段）
          也渲染 turn-flow，保证「已工作 N 秒」计时条不缺失 */}
      {(hasAnyAiContent || roundActive) && (
        <div className="turn-flow">
          {/* plan-1094: 运行中即使尚无最终汇报文本（hasProcess=false）也必须渲染
              WorkTimer，否则发送初期/纯思考阶段「已工作 N 秒」计时条缺失 */}
          {(canCollapse || roundActive) && (
            <WorkTimer
              turnIds={roundIds}
              agentId={agentId}
              flow={flow}
              isRunning={roundActive}
              endFloorMs={backgroundEndedAt}
              canCollapse={canCollapse}
              collapsed={processCollapsed}
              onToggleCollapsed={() => setUserToggledCollapsed(!processCollapsed)}
            />
          )}

          {/* 1. 执行段（plan-20261013-wf-order）：过程项按时间线先后分段，工作流卡及其节点卡常显、原位留在段间，
              不随折叠 / 最终汇报落库移动。有最终汇报时只折叠最终汇报之前的过程段；
              无最终汇报（运行中 / 异常 / 纯工具调用）全部直显，子代理面板例外——允许折叠（v36 修复：此前没有折叠入口）。 */}
          {hasProcess ? (
            <>
              {renderFlowSegments(splitFlowSegments(items, 0, finalReportOriginalIdx, firstUserIdx, attachedIdx), {
                collapsed: processCollapsed,
                wrap: true,
              })}
              {/* 3. 最终汇报（最终 text）与其后的确认/分割线等：有过程时直显在折叠条下方（不进入折叠容器） */}
              {renderFlowSegments(
                splitFlowSegments(items, finalReportOriginalIdx, items.length, firstUserIdx, attachedIdx),
                { collapsed: false, wrap: false },
              )}
            </>
          ) : (
            renderFlowSegments(splitFlowSegments(items, 0, items.length, firstUserIdx, attachedIdx), {
              collapsed: flow === "subagent" && processCollapsed,
              wrap: true,
            })
          )}

          {/* plan-282-1416（问题3 根治）：AI 操作行不再挂在 .turn-flow 底部——
              它已由 renderAiItemWithActions 紧跟最后一个 AI 内容项输出，
              因此运行中注入的用户消息（时间序在后）不再被这条操作行包进来。 */}
        </div>
      )}

      {/* plan-20261018-turn-changes：本轮文件变更卡片。
          只在本轮进入终态后展示（运行中不闪），且只读该轮自己的写盘记录——
          上一轮的卡片内容随轮次固定，不会因为新轮次完成而被覆盖。
          合并续跑轮（roundIds 多个）时逐轮各出一张卡，撤销仍是按轮粒度的批次。 */}
      {turnFinished && flow === "main" && !rolledBack && roundIds.length > 0 && (
        <div className="turn-changes-stack">
          {roundIds.map((id) => <TurnChangesCard key={id} turnId={id} />)}
        </div>
      )}

      {/* plan-20261018-turn-changes：有变更卡片时，本轮 AI 操作行（复制 / 赞 / 踩 / 重新生成 / 回滚）
          改挂到卡片下方，形成「内容 → 变更清单 → 操作行」的顺序。
          无卡片时仍由 renderItemTail 紧跟最后一个 AI 内容项，避免把运行中注入的用户消息包进来。 */}
      {turnFinished && hasChangeCards && actionAnchorIdx >= 0 && (
        <div className="turn-changes-actions">
          <MessageActions
            entry={entry}
            onRollback={onRollback}
            scope="ai"
            actions={actions}
            ownerId={itemMsgId(items[actionAnchorIdx])}
          />
        </div>
      )}
    </div>
  );
});
