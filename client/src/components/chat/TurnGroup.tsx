/** TurnGroup：单个 turn 容器。
 * - 思考块按时间顺序穿插在消息与工具调用之间
 * - 「已工作」计时条是 AI 回复顶部状态块（计划 turn 与普通 turn 口径一致）；
 *   计划卡固定渲染在规划段之后、执行折叠流之上，刷新前后位置不漂移
 * - 工作过程（思考/工具/子代理）支持折叠，任务完成后自动默认折叠（对齐图 8）；
 *   计划 turn 的规划段与执行段共享同一折叠状态（plan-655），无"上面展开、中间折叠"割裂
 * - 最终回答（Markdown/产物/摘要）与计划卡始终展示

 */
import { useCallback, memo, useState, useMemo, Fragment } from "react";
import { useRunningTicker } from "./useRunningTicker";
import { MessageActions } from "./MessageActions";
import type { SubagentMetaLite } from "./SubagentCard";
import { PluginSlot } from "../../plugins/registry";
import { MarkdownContent } from "../MarkdownContent";
import { SubagentReportCard } from "./SubagentReportCard";
import { IconRotateCcw, IconArrowToggle, IconAlertCircle } from "../icons";
import type { TimelineEntry, TurnItem } from "./timeline";
import { msgText, getTurnById } from "./timeline";
import { useChatStore } from "../../store/chat";
import { parseUtc } from "../../utils/time";
import { MessageImageGrid, MessageFileCards, TokenText, RefChips, refsOf, stripRefLines, attachmentsOf } from "./AttachmentCard";
import { recordComponentRender } from "../../perf/metrics";

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
  turnId,
  agentId,
  flow = "main",
  isRunning,
  canCollapse,
  collapsed,
  onToggleCollapsed,
}: {
  turnId: number | null;
  agentId?: number;
  flow?: "main" | "subagent";
  isRunning: boolean;
  canCollapse?: boolean;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
}) {
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
  const turnStart = useChatStore((s) =>
    subagentFlow ? null : getTurnById(s.turns, turnId)?.started_at ?? null
  );
  const turnEnd = useChatStore((s) =>
    subagentFlow ? null : getTurnById(s.turns, turnId)?.completed_at ?? null
  );
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
      if (completed > 0) end = completed;
    }
    const sec = Math.max(0, Math.round((end - start) / 1000));
    const min = Math.floor(sec / 60);
    label = min > 0 ? `${min} 分 ${sec % 60} 秒` : `${sec} 秒`;
  }

  // 既无时间数据又不可折叠时：主消息流维持旧行为（不渲染，避免乐观发送期闪现徒条）；
  // 子代理面板的条是唯一折叠入口——只要可折叠就继续渲染（无秒数时展示「工作过程」）。
  if (!label && !canCollapse) return null;

  return (
    <div
      className={`turn-worktime${canCollapse ? " clickable" : ""}`}
      onClick={canCollapse ? onToggleCollapsed : undefined}
      title={canCollapse ? (collapsed ? "展开工作过程" : "折叠工作过程") : undefined}
    >
      <span>{label ? `已工作 ${label}` : "工作过程"}</span>
      {canCollapse && (
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
  flow = "main",
  agentId,
  reportMessageId,
  globalRunning,
  latestUserId,
  flowErrorTurnId,
  hiddenTurnErrors,
  turnStatus,
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
  /** v36 (plan-321-1600 M2)：子代理面板的最终汇报消息 id——命中时改由结构化汇报卡片渲染；
   *  主消息流（flow="main"）不传该参数，渲染完全不变。 */
  reportMessageId?: number;
  /** plan-75-334 阶段1：父层统一计算的全局派生值（必需）。
   *  此前每个 TurnGroup 各自订阅 isRunning / flowError / hiddenTurnErrors / turns / messages，
   *  运行期任何相关 store 更新都会让所有可见 turn 重复执行 selector；
   *  现在由 MainMessageFlow / SubagentMessageFlow 各算一次、经 props 传下来。 */
  globalRunning: boolean;
  latestUserId: number;
  flowErrorTurnId: number | null;
  hiddenTurnErrors: number[];
  turnStatus: string | undefined;
}) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("turnGroup");
  
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  const items = entry.items;
  const turnId = entry.turnId;

  /** plan-41-228：turn 内 ERROR 消息与末尾错误卡（FlowErrorCard）是同一错误的两个渲染位。
   *  flowError 命中本 turn（或该 turn 已被「关闭」）时由末尾卡独家呈现（带重试/关闭），
   *  这里不再重复渲染同一条错误。 */
  const suppressTurnError =
    flow === "main" && turnId != null
    && (flowErrorTurnId === turnId || hiddenTurnErrors.includes(turnId));

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
   *  复用规划 turn 且行状态可能仍是 awaiting_confirmation，若只看行状态会误判已结束。 */
  const turnFinished =
    flow === "subagent"
      ? !isRunning
      : entry.turnId == null
        ? true
        : turnRunning
          ? false
          : turnRowStatus != null
            ? TERMINAL_TURN_STATUSES.has(turnRowStatus)
            : !globalRunning;

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
  const processCollapsed =
    userToggledCollapsed !== null
      ? userToggledCollapsed
      : (!isRunning && hasProcess && !isAwaitingConfirmation && !hasInjectedUser);

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

  const subagentNode =
    unplacedSubagents.length > 0 ? (
      <div className="turn-subagents">
        {unplacedSubagents.map((sa) => (
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
  const renderAiItemWithActions = (item: TurnItem, i: number) => {
    const node = renderAiItem(item, i);
    if (node == null) return null;
    return (
      <Fragment key={`ai-${i}`}>
        {node}
        {/* plan-330-1648 M5-b: 未落位的子代理卡片渲染在**最后一个 AI 内容项之后**，
            而不是过程容器末尾——否则卡片会出现在 AI 文本上方（用户反馈问题1）。 */}
        {i === lastAiItemIdx && subagentNode}
        {i === lastAiItemIdx && turnFinished && (
          <MessageActions
            entry={entry}
            onRollback={onRollback}
            scope="ai"
            actions={actions}
            ownerId={itemMsgId(item)}
          />
        )}
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
            <div className="turn-user-bubble">
              <MessageFileCards atts={attachmentsOf(item.msg.content)} />
              {/* plan-308-1542 需求2：引用芯片 */}
              <RefChips refs={refsOf(item.msg.content)} />
              {stripRefLines(msgText(item.msg.content), refsOf(item.msg.content).length > 0) && (
                <div className="turn-user-text">
                  <TokenText text={stripRefLines(msgText(item.msg.content), refsOf(item.msg.content).length > 0)} />
                </div>
              )}
            </div>
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
        // v36 修复：去掉内联 28px 左缩进——子代理行此前比工具调用行右移 28px，
        // 与「子代理卡片与工具调用行共用同一左侧边界」的规范不一致（用户反馈未对齐）。
        return (
          <div key={i} className="turn-item turn-item-subagent">
            <PluginSlot slot="subagent-card" meta={meta} />
          </div>
        );
      }
      case "text":
        // v36 (plan-321-1600 M2): 子代理面板的最终汇报交结构化卡片（文件芯片/验证/验收/风险）；
        // 主流不传 reportMessageId，渲染路径不变。
        if (flow === "subagent" && reportMessageId != null && item.msg.id === reportMessageId) {
          return (
            <div key={i} className="turn-item turn-item-text">
              <SubagentReportCard text={msgText(item.msg.content)} />
            </div>
          );
        }
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
          <div key={i} className="turn-item turn-item-error">
            {/* plan-282-1416：内联 SVG 收编为图标库 IconAlertCircle */}
            <IconAlertCircle size={15} className="err-icon" />
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
            <span className="turn-divider-text">{gn > 0 ? `⟳ 目标续跑 · 第 ${gn} 轮` : "⟳ 目标续跑"}</span>
            <span className="turn-divider-line" />
          </div>
        );
      }
      // v39: 子代理完成唤醒消息——细分隔线（系统生成，对应上方「等待子代理结束…」）
      case "subagent-wakeup": {
        return (
          <div key={i} className="turn-item turn-item-divider subagent-wakeup-divider">
            <span className="turn-divider-line" />
            <span className="turn-divider-text">⟳ 子代理已完成，继续执行</span>
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
            {/* 会话 228-1142: 图片网格在气泡外部、靠右（对齐参考图 1）；文件卡与文本留在气泡内 */}
            <MessageImageGrid atts={attachmentsOf(firstUser.msg.content)} />
            <div className="turn-user-bubble">
              <MessageFileCards atts={attachmentsOf(firstUser.msg.content)} />
              {/* plan-308-1542 需求2：引用芯片 */}
              <RefChips refs={refsOf(firstUser.msg.content)} />
              {stripRefLines(msgText(firstUser.msg.content), refsOf(firstUser.msg.content).length > 0) && (
                <div className="turn-user-text">
                  <TokenText text={stripRefLines(msgText(firstUser.msg.content), refsOf(firstUser.msg.content).length > 0)} />
                </div>
              )}
            </div>
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
      {(hasAnyAiContent || isRunning) && (
        <div className="turn-flow">
          {/* plan-1094: 运行中即使尚无最终汇报文本（hasProcess=false）也必须渲染
              WorkTimer，否则发送初期/纯思考阶段「已工作 N 秒」计时条缺失 */}
          {(canCollapse || isRunning) && (
            <WorkTimer
              turnId={turnId}
              agentId={agentId}
              flow={flow}
              isRunning={isRunning}
              canCollapse={canCollapse}
              collapsed={processCollapsed}
              onToggleCollapsed={() => setUserToggledCollapsed(!processCollapsed)}
            />
          )}

          {/* 1. 有最终汇报时：过程项进入可折叠容器 */}
          {hasProcess && (
            <div className={`turn-process-container${processCollapsed ? " collapsed" : ""}`}>
              {processItems.map(({ item, index }) => renderAiItemWithActions(item, index))}
              {hasPlan && !hasPlanMsg && <PluginSlot slot="plan-card" turnId={turnId} embedded />}
              {subagentFallbackInContainer ? subagentNode : null}
            </div>
          )}

          {/* 2. 无最终汇报时（运行中/异常/纯工具调用无 text 总结）：所有项直显展开，杜绝隐形；
              子代理面板例外——允许折叠（v36 修复：此前没有折叠入口，长过程无法收起） */}
          {!hasProcess && (
            <div className={`turn-process-container${flow === "subagent" && processCollapsed ? " collapsed" : ""}`}>
              {items.map((item, index) =>
                index !== firstUserIdx ? renderAiItemWithActions(item, index) : null
              )}
              {hasPlan && !hasPlanMsg && <PluginSlot slot="plan-card" turnId={turnId} embedded />}
              {subagentFallbackInContainer ? subagentNode : null}
            </div>
          )}

          {/* 3. 最终汇报（最终 text）与其后的确认/分割线等：有过程时直显在折叠条下方 */}
          {hasProcess &&
            items.map((item, index) =>
              index !== firstUserIdx && index >= finalReportOriginalIdx
                ? renderAiItemWithActions(item, index)
                : null
            )}

          {/* plan-282-1416（问题3 根治）：AI 操作行不再挂在 .turn-flow 底部——
              它已由 renderAiItemWithActions 紧跟最后一个 AI 内容项输出，
              因此运行中注入的用户消息（时间序在后）不再被这条操作行包进来。 */}
        </div>
      )}
    </div>
  );
});
