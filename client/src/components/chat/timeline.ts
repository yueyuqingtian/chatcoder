/**
 * 消息时间线聚合（v4 全量重写）：将扁平消息列表重建为 TurnGroup 结构。
 *
 * 核心规则：
 * - 按 turn_id 分组；turn 出现顺序由该组首条消息在输入数组中的位置决定（非 turn_id 数值大小）
 *   —— 这修复了"AI 回复显示在用户消息上方"的顺序 bug（§9.1）
 * - turn 内：消息严格按落库时间序（id 升序）渲染——正常 turn 用户消息天然在开头；
 *   任务执行中注入（排队发送）的用户消息落在实际发送位置，不再强制提升到 turn 顶部
 * - 相邻 tool_call/tool_result 合并为 tool-cluster；AI 文字切段时新开 cluster
 * - tool_tree 中同工具连续 ≥2 次聚为 group 节点
 * - 思考内容（thinking）不进入主消息流文本，由 ThinkingBlock 独立消费
 */
import type { MessageOut } from "../../api/client";
import { MsgType, SenderType } from "@chatcoder/shared";
import type { TurnOut } from "@chatcoder/shared";
import { recordDerivation } from "../../perf/metrics";

export interface ToolLeaf {
  callKey: string;
  tool: string;
  args: Record<string, unknown>;
  agentName: string;
  /** v2.2: 所属线程（子代理消息 thread_id=agent_id，任务卡步骤点击穿透定位用） */
  threadId: number | null;
  /** v19: 所属 turn（写操作行展开拉取 diff 用） */
  turnId: number | null;
  /** null = 结果尚未返回 */
  ok: boolean | null;
  output: string;
  error: string | null;
  durationMs: number | null;
  /** v2.2: 行级变更统计（写盘工具，+N -M 摘要） */
  changeStat: { path: string; additions: number; deletions: number } | null;
}

export type ToolNode =
  | { kind: "group"; tool: string; count: number; leaves: ToolLeaf[] }
  | { kind: "leaf"; leaf: ToolLeaf }
  /** v19: 两次思考间连续非写操作 ≥2 合并为一行（运行中动词滚动，完成后摘要可展开） */
  | { kind: "action-cluster"; leaves: ToolLeaf[] }
  /** v19: 紧邻同文件的连续写操作合并为一行（+N -M 累加） */
  | { kind: "write-merged"; leaves: ToolLeaf[] };

/** v19: 写入类工具——独占一行（用户要求"一个写操作就是一行"），仅紧邻同文件可合并 */
export const WRITE_TOOLS = new Set(["fs_write", "editor_apply_diff", "multi_file_edit"]);

/** v25: 是否按"写操作行"展示——白名单写盘工具，或非白名单工具但结果携带 change_stat
 * （模型用 terminal_exec 等"工具伪装"改文件，服务端检测到变更后在 tool.result 附 change_stat，
 * 前端据此把该行渲染为可展开 diff，而不是普通命令输出）。 */
export function isWriteLeaf(leaf: ToolLeaf): boolean {
  return WRITE_TOOLS.has(leaf.tool) || leaf.changeStat != null;
}

/** 搜索类工具（合并行摘要统计用） */
export const SEARCH_TOOLS = new Set(["fs_grep", "codebase_search", "web_search", "memory_search"]);

/** 终端/命令类工具（合并行摘要统计用） */
export const RUN_TOOLS = new Set(["terminal_exec", "ci_run", "shell_exec"]);

export type TurnItem =
  | { kind: "user"; msg: MessageOut }
  | { kind: "thinking"; msg: MessageOut }
  | { kind: "text"; msg: MessageOut }
  /** firstId：工具簇首条调用消息的 id，用于按创建先后定位节点子代理卡（见 firstIdOf） */
  | { kind: "tools"; nodes: ToolNode[]; firstId?: number }
  | { kind: "subagent"; msg: MessageOut }
  | { kind: "summary"; msg: MessageOut }
  | { kind: "error"; msg: MessageOut }
  /** v2.2 (对齐 zcode 3.11): 系统分割线（模型切换等 divider） */
  | { kind: "divider"; msg: MessageOut }
  /** plan-671: 目标续跑消息（zcode model-only 语义）——渲染为细分隔线而非用户气泡 */
  | { kind: "goal-continuation"; msg: MessageOut }
  /** v39: 子代理完成唤醒消息（系统生成）——同样渲染为细分隔线 */
  | { kind: "subagent-wakeup"; msg: MessageOut }
  /** 工作流唤醒消息（进入终态或需要你决定时的自动续跑）——同样渲染为细分隔线 */
  | { kind: "workflow-wakeup"; msg: MessageOut }
  /** plan-865: 计划预览/确认消息——按数据库时间线位置渲染计划卡 */
  | { kind: "plan"; msg: MessageOut }
  /** 工作流卡：由 plan / start / resume 工作流工具的调用推导出程序编号（turnId 为该工具调用所在轮，
   *  节点子代理卡据此挂靠），卡片状态来自工作流 store */
  | { kind: "workflow"; programId: number; turnId: number | null; firstId?: number };

export type TimelineEntry =
  /** turnIds：并入续跑轮后的全部轮 id（首个即 turnId）；未并入时缺省，见 createRoundMerger */
  | { kind: "turn"; turnId: number | null; turnIds?: number[]; items: TurnItem[] }
  | { kind: "standalone"; msg: MessageOut };

/** 系统续跑条目：工作流 / 子代理唤醒、目标续跑。它们各自成轮，但没有用户消息，是上一段回复的延续。 */
export const SYSTEM_CONTINUATION_KINDS: ReadonlySet<string> = new Set([
  "workflow-wakeup",
  "subagent-wakeup",
  "goal-continuation",
]);

/** 常显项（不进入「工作过程」折叠）：工作流卡与工作流唤醒分隔线——进度、暂停 / 继续需要始终可见。 */
export function isOutsideProcess(item: TurnItem): boolean {
  return item.kind === "workflow" || item.kind === "workflow-wakeup";
}

/** 执行段的切分结果（见 splitFlowSegments）。 */
export type FlowSegment =
  /** 常显项：原位独立渲染（工作流卡、工作流唤醒分隔线） */
  | { kind: "sticky"; index: number }
  /** 过程段：两块常显项之间的连续过程项，整段装入可折叠容器 */
  | { kind: "process"; indices: number[] }
  /** 挂靠段：该项挂靠的节点子代理卡（常显，紧随该项之后；该项的尾部兜底卡与操作行一并渲染在此段） */
  | { kind: "attached"; index: number };

/** 按时间线先后切分 [from, to) 内的 AI 项，跳过 skipIndex（turn 顶部单独渲染的首条用户消息）。
 *
 *  段的先后即时间线先后：常显项各自成段，过程段只夹在它们之间。折叠 / 展开过程段
 *  只改变段内高度，不改变任何项相对于其它段的位置——这是工作流卡不随最终汇报落库而漂移的前提。
 *  挂靠了节点子代理卡的项之后紧跟一个挂靠段，使这些卡片同样原位留在段间。 */
export function splitFlowSegments(
  items: readonly TurnItem[],
  from: number,
  to: number,
  skipIndex: number,
  attached?: ReadonlySet<number>,
): FlowSegment[] {
  const segments: FlowSegment[] = [];
  for (let index = from; index < to; index++) {
    if (index === skipIndex) continue;
    if (isOutsideProcess(items[index])) {
      segments.push({ kind: "sticky", index });
    } else {
      const last = segments[segments.length - 1];
      if (last && last.kind === "process") last.indices.push(index);
      else segments.push({ kind: "process", indices: [index] });
    }
    if (attached?.has(index)) segments.push({ kind: "attached", index });
  }
  return segments;
}

/** 可挂靠到消息流的工作流节点子代理卡片（SubagentMetaLite 的必要字段）。 */
export interface WorkflowNodeCard {
  agentId: number;
  turnId?: number | null;
  programId?: number | null;
  /** 派发时刻：线程首条消息的 id（见 placeWorkflowNodeCards） */
  spawnMessageId?: number | null;
}

/** 条目的起始消息 id，即创建先后的依据：消息类条目取其消息 id；工具簇与工作流卡取其首条工具调用的 id。 */
export function firstIdOf(item: TurnItem): number | null {
  if (item.kind === "tools" || item.kind === "workflow") return item.firstId ?? null;
  return "msg" in item && item.msg ? Number(item.msg.id) : null;
}

/** 工作流节点子代理的落位（plan-20261013-wf-order）。
 *
 *  节点子代理不经 spawn_subagent 工具调用，消息流里没有它的消息。它的创建时刻记为派发时刻
 *  spawnMessageId（线程首条消息的 id）：派发前的主线程消息都已落库（见 _spawn 的 flush 屏障），
 *  其 id 均小于它，派发后产生的消息 id 均大于它。因此：
 *  ① 有派发时刻：挂到派发前最后一个条目之后，即按消息 id 定位的创建位置（首条用户消息不作锚点）；
 *  ② 无派发时刻（旧数据）：挂到同轮、同程序工作流卡之后，同轮优先，否则取该程序在本轮的最后一张卡；
 *  ③ 都找不到的原样放入 unplaced，由调用方兜底。
 *  同一锚点下按子代理创建序（agentId 升序）排列。attached 以 items 下标为键。 */
export function placeWorkflowNodeCards<T extends WorkflowNodeCard>(
  items: readonly TurnItem[],
  cards: readonly T[],
): { attached: Map<number, T[]>; unplaced: T[] } {
  const firstUser = items.findIndex((it) => it.kind === "user");
  const cardItemsByProgram = new Map<number, Array<{ index: number; turnId: number | null }>>();
  items.forEach((item, index) => {
    if (item.kind !== "workflow") return;
    const list = cardItemsByProgram.get(item.programId) ?? [];
    list.push({ index, turnId: item.turnId });
    cardItemsByProgram.set(item.programId, list);
  });

  const attached = new Map<number, T[]>();
  const place = (index: number, card: T) => {
    const list = attached.get(index) ?? [];
    list.push(card);
    attached.set(index, list);
  };
  const unplaced: T[] = [];
  for (const card of [...cards].sort((a, b) => a.agentId - b.agentId)) {
    const spawn = card.spawnMessageId;
    if (spawn != null) {
      let anchor = -1;
      for (let i = 0; i < items.length; i++) {
        if (i === firstUser) continue;
        const id = firstIdOf(items[i]);
        if (id != null && id <= spawn) anchor = i;
      }
      if (anchor >= 0) {
        place(anchor, card);
        continue;
      }
    }
    const candidates = card.programId != null ? cardItemsByProgram.get(card.programId) : undefined;
    if (!candidates || candidates.length === 0) {
      unplaced.push(card);
      continue;
    }
    const sameTurn = candidates.filter((c) => c.turnId === (card.turnId ?? null));
    const pool = sameTurn.length > 0 ? sameTurn : candidates;
    place(pool[pool.length - 1].index, card);
  }
  return { attached, unplaced };
}

type TurnEntry = Extract<TimelineEntry, { kind: "turn" }>;

/** 续跑轮：有 turn_id、没有用户消息、首项是系统续跑（唤醒后模型接着回复的那一轮）。 */
function isContinuationTurn(e: TimelineEntry): e is TurnEntry {
  return e.kind === "turn" && e.turnId != null && e.items.length > 0
    && SYSTEM_CONTINUATION_KINDS.has(e.items[0].kind)
    && !e.items.some((it) => it.kind === "user");
}

/** 把续跑轮并入紧邻的前一个 AI 轮，使「一轮」只有一个「已工作」计时条与一排操作按钮（plan-282-1530 阶段 D）。
 *
 *  基底 = 带 turnId 的非续跑 turn 条目；其后紧邻的续跑轮依次并入，直到遇到用户气泡、独立消息或新的基底。
 *  无基底的续跑轮原样保留。返回新数组；未参与合并的条目保持原引用。
 *  合并结果按「基底引用 + 各续跑轮引用」缓存：流式更新只动尾部一轮时，其余合并轮的引用不变，下游 memo 继续命中。 */
export function createRoundMerger(): (entries: TimelineEntry[]) => TimelineEntry[] {
  const cache = new WeakMap<TurnEntry, { parts: TurnEntry[]; merged: TurnEntry }>();

  const mergedOf = (parts: TurnEntry[]): TurnEntry => {
    const base = parts[0];
    const hit = cache.get(base);
    if (hit && sameRefs(hit.parts, parts)) return hit.merged;
    const merged: TurnEntry = {
      kind: "turn",
      turnId: base.turnId,
      turnIds: parts.map((p) => p.turnId as number),
      items: parts.flatMap((p) => p.items),
    };
    cache.set(base, { parts, merged });
    return merged;
  };

  return (entries) => {
    const out: TimelineEntry[] = [];
    let group: TurnEntry[] | null = null;
    for (const e of entries) {
      if (group && isContinuationTurn(e)) {
        group.push(e);
        continue;
      }
      if (group) {
        out.push(group.length === 1 ? group[0] : mergedOf(group));
        group = null;
      }
      if (e.kind === "turn" && e.turnId != null && !isContinuationTurn(e)) group = [e];
      else out.push(e);
    }
    if (group) out.push(group.length === 1 ? group[0] : mergedOf(group));
    return out;
  };
}

/** turn_id 回填前的续跑孤儿条目：没有 turn_id、只含系统续跑项（唤醒消息先于其轮次广播，当时还没有 turn_id）。
 *  它不会成为独立的一轮——回填后会并入前一轮（见 createRoundMerger / adoptWakeupTurn）。 */
export function isOrphanContinuation(e: TimelineEntry): boolean {
  return e.kind === "turn" && e.turnId == null && e.items.length > 0
    && e.items.every((it) => SYSTEM_CONTINUATION_KINDS.has(it.kind));
}

/** 消息流尾部那一轮的下标（没有则 -1）。跳过尾部的续跑孤儿条目：它马上并入前一轮，
 *  真正的尾部一轮是它之前的那一轮——否则唤醒消息到达的瞬间，上一轮不再是尾部，其「整轮未结束」的判定会闪断。 */
export function tailRoundIndex(entries: readonly TimelineEntry[]): number {
  let i = entries.length - 1;
  while (i >= 0 && isOrphanContinuation(entries[i])) i--;
  return i;
}

export function msgText(c: Record<string, unknown>): string {
  const t = c.text;
  return typeof t === "string" ? t : "";
}

/** plan-31-152 S5-5：取时间线中**最后一条已落库的正文文本**（assistant text item）。
 *
 *  用途：会话运行期间后端会把已完整的内容落库，而前端流式缓冲（streamingBuffers）
 *  要到 turn 结束/下一拍才清空——两者并存时同一条消息会在消息流里显示两份
 *  （用户反馈"偶尔重复显示一条消息，过几秒又变回一条"）。
 *  由 StreamingTail 用本函数结果与流式文本比对，完全一致时不再重复渲染流式正文。
 *
 *  扫描顺序：从最后一个 entry 往前；turn 内从最后一个 item 往前找 text。 */
export function lastPersistedText(entries: TimelineEntry[]): string {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i];
    if (e.kind !== "turn") return msgText(e.msg.content);
    for (let j = e.items.length - 1; j >= 0; j--) {
      const it = e.items[j];
      if (it.kind === "text") return msgText(it.msg.content);
    }
  }
  return "";
}

/** 从 tool_call / tool_result 消息提取统一 leaf。 */
function buildLeaf(call: MessageOut, result?: MessageOut): ToolLeaf {
  const cc = call.content as Record<string, unknown>;
  const rc = (result?.content ?? {}) as Record<string, unknown>;
  const statRaw = rc.change_stat as { path?: unknown; additions?: unknown; deletions?: unknown } | undefined;
  return {
    callKey: String(cc.call_key ?? call.id),
    tool: String(cc.tool ?? "tool"),
    args: (cc.args && typeof cc.args === "object" ? cc.args : {}) as Record<string, unknown>,
    agentName: String(cc.agent_name ?? ""),
    threadId: call.thread_id != null ? Number(call.thread_id) : null,
    turnId: call.turn_id != null ? Number(call.turn_id) : null,
    ok: result ? Boolean(rc.ok) : null,
    output: typeof rc.output === "string" ? rc.output : "",
    error: typeof rc.error === "string" ? rc.error : null,
    durationMs: rc.duration_ms != null ? Number(rc.duration_ms) : null,
    changeStat: statRaw && typeof statRaw.path === "string"
      ? { path: statRaw.path, additions: Number(statRaw.additions ?? 0), deletions: Number(statRaw.deletions ?? 0) }
      : null,
  };
}

/** plan-547: 导出供锚定/展示复用——从工具 leaf 提取规范化的目标路径（正斜杠）。 */
export function leafPathOf(leaf: ToolLeaf): string {
  const p = leaf.args?.path;
  if (typeof p === "string" && p) return p.replace(/\\/g, "/");
  const f = leaf.args?.file_path;
  if (typeof f === "string" && f) return f.replace(/\\/g, "/");
  // v25: 非白名单写盘工具（terminal_exec 等）路径来自 change_stat
  if (leaf.changeStat?.path) return leaf.changeStat.path.replace(/\\/g, "/");
  return "";
}

/** 将某 turn 的工具调用/结果消息聚合成 ToolNode[]。
 *  v19 聚类规则（用户要求）：
 *  - 写操作独占一行；紧邻且同 path 的连续写合并为 write-merged（+N -M 累加）；
 *  - 连续非写操作 ≥2 合并为 action-cluster（运行中动词滚动，完成后摘要可展开）；
 *  - 单个非写操作退化为 leaf。 */
function buildToolTree(calls: MessageOut[], resultsByKey: Map<string, MessageOut>): ToolNode[] {
  const nodes: ToolNode[] = [];
  const leafOf = (c: MessageOut) =>
    buildLeaf(c, resultsByKey.get(String((c.content as Record<string, unknown>).call_key ?? c.id)));
  let i = 0;
  while (i < calls.length) {
    const first = leafOf(calls[i]);
    // v25: 工具伪装写盘（terminal_exec 等带 change_stat）同样按写操作独占一行
    if (isWriteLeaf(first)) {
      // 紧邻同文件连续写合并
      const path = leafPathOf(first);
      let j = i + 1;
      if (path) {
        while (j < calls.length) {
          const lj = leafOf(calls[j]);
          if (!isWriteLeaf(lj) || leafPathOf(lj) !== path) break;
          j++;
        }
      }
      const leaves = calls.slice(i, j).map(leafOf);
      if (leaves.length >= 2) nodes.push({ kind: "write-merged", leaves });
      else nodes.push({ kind: "leaf", leaf: leaves[0] });
      i = j;
    } else {
      // 收集连续非写操作
      let j = i + 1;
      while (j < calls.length) {
        const lj = leafOf(calls[j]);
        if (isWriteLeaf(lj)) break;
        j++;
      }
      const leaves = calls.slice(i, j).map(leafOf);
      if (leaves.length >= 2) nodes.push({ kind: "action-cluster", leaves });
      else nodes.push({ kind: "leaf", leaf: leaves[0] });
      i = j;
    }
  }
  return nodes;
}

/** 从扁平消息构建时间线。
 *  关键修复：turn 排序依据是该 turn 首条消息在输入数组中的原始位置，而非 turn_id 数值。
 *  这保证用户消息（先落库）所在的 turn 始终排在 AI 回复 turn 之前。
 *  v30.1: 被压缩的消息不再过滤隐藏——保留在时间线上（CompactionCard 提供折叠查看），
 *  压缩块 SUMMARY 消息按 id 顺序自然落在被压缩消息之后，时间线排序保持一致。 */
export function buildTimeline(messages: MessageOut[]): TimelineEntry[] {
  // 1. 单遍扫描：turn 消息按 turn_id 聚合（占位 entry 保持首现顺序）；
  //    turn_id 为 null 的消息就地处理——用户消息独立成组；
  //    系统/其他消息并入「前一个」turn，保证模型切换 divider 显示在切换发生的位置，
  //    而不是全部堆到消息流顶部（旧实现把所有 null-turn 消息装进同一个桶导致的 bug）。
  const entries: TimelineEntry[] = [];
  const turnMap = new Map<number, MessageOut[]>();
  const turnEntryById = new Map<number, Extract<TimelineEntry, { kind: "turn" }>>();
  let lastTurnId: number | null = null;

  for (const m of messages) {
    const tid = m.turn_id ?? null;
    if (tid != null) {
      if (!turnMap.has(tid)) {
        turnMap.set(tid, []);
        const entry = { kind: "turn", turnId: tid, items: [] } as Extract<TimelineEntry, { kind: "turn" }>;
        turnEntryById.set(tid, entry);
        entries.push(entry);
      }
      turnMap.get(tid)!.push(m);
      lastTurnId = tid;
      continue;
    }
    if (m.sender_type === SenderType.User) {
      // 唤醒消息（turn_id 回填前/独立到达）按分隔线渲染，不显示为用户气泡
      entries.push({ kind: "turn", turnId: null, items: [userItemOf(m)] });
      continue;
    }
    if (lastTurnId != null) turnMap.get(lastTurnId)!.push(m);
    else entries.push({ kind: "standalone", msg: m });
  }

  // 2. 逐 turn 构建 items（Map 迭代顺序 = 首现顺序）
  for (const [tid, msgs] of turnMap) {
    turnEntryById.get(tid)!.items = buildTurnItems(msgs);
  }

  return entries;
}

/** 引用级比较：两个数组等长且逐项同一引用。 */
function sameRefs<T>(a: readonly T[], b: readonly T[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * 增量版时间线构建器（plan-329-1647 S8b / FlowEngine）。
 *
 * ── 问题 ──
 * 任一条消息落库 ⇒ store 里 messages 换新引用 ⇒ 原实现 buildTimeline 全量重建**所有**
 * entry / item 对象 ⇒ 下游 memo 的 TurnGroup / StandaloneEntry 引用全变 ⇒ 所有可见消息
 * 重渲染（含 markdown / rehype 插件重跑）。会话运行期间每步工具调用都会触发一次。
 *
 * ── 现在 ──
 * 按 turn_id 缓存「上次的消息数组 + 构建结果」。只有当某个 turn 的消息数组**逐项引用**
 * 发生变化（典型：只有最后一个 turn 在追加消息）时才重建它，其余 turn 直接复用上次的
 * entry 对象 ⇒ memo 继续命中，只有真正变化的那一条 turn 重渲染。
 *
 * 用法：一个会话/面板持有一个 builder（`useMemo(() => createTimelineBuilder(), [])`），
 * 每次渲染调用它即可。缓存按 turn_id 键控，已消失的 turn 会被清理，不会无界增长。
 */
export function createTimelineBuilder(): (messages: MessageOut[]) => TimelineEntry[] {
  interface TurnCache { msgs: MessageOut[]; entry: Extract<TimelineEntry, { kind: "turn" }>; }
  const turnCache = new Map<number, TurnCache>();
  // 无 turn_id 的消息（用户消息独立成组 / 前置 standalone）：按消息对象身份缓存
  const msgEntryCache = new WeakMap<MessageOut, TimelineEntry>();

  type Slot = { kind: "turn"; tid: number } | { kind: "entry"; entry: TimelineEntry };

  /** plan-75-334 阶段2：上一次调用的输入快照与结果，作为「追加快路径」的前缀基准。
   *  快路径只在「旧前缀逐项引用一致、长度只增、只动尾部 turn」时启用；
   *  任何前缀变化（REST 刷新、回滚、乐观消息替换、乱序插入）都回退到全量安全路径。 */
  let lastMessages: MessageOut[] | null = null;
  let lastSlots: Slot[] | null = null;
  let lastResult: TimelineEntry[] | null = null;
  /** 上次扫描结束时「最后一条带 turn_id 的消息」所属 tid（null-turn 消息的归属判定沿用此值）。 */
  let lastTurnId: number | null = null;

  /** 全量安全路径：与改造前语义完全一致（逐 turn 按引用复用缓存 entry），也是所有异常情况的落点。 */
  function buildFull(messages: MessageOut[]): TimelineEntry[] {
    // ① 单遍扫描：与 buildTimeline 同一套聚合规则，但只记录「顺序槽位」，
    //    不在这里创建 turn entry 对象（推迟到第 ② 步按缓存决定复用还是新建）。
    const slots: Slot[] = [];
    const turnMsgs = new Map<number, MessageOut[]>();
    const turnOrder: number[] = [];
    let scanLastTid: number | null = null;

    for (const m of messages) {
      const tid = m.turn_id ?? null;
      if (tid != null) {
        let arr = turnMsgs.get(tid);
        if (!arr) {
          arr = [];
          turnMsgs.set(tid, arr);
          turnOrder.push(tid);
          slots.push({ kind: "turn", tid });
        }
        arr.push(m);
        scanLastTid = tid;
        continue;
      }
      if (m.sender_type === SenderType.User) {
        let e = msgEntryCache.get(m);
        if (!e) {
          e = { kind: "turn", turnId: null, items: [userItemOf(m)] };
          msgEntryCache.set(m, e);
        }
        slots.push({ kind: "entry", entry: e });
        continue;
      }
      if (scanLastTid != null) {
        const arr = turnMsgs.get(scanLastTid);
        if (arr) arr.push(m);
        continue;
      }
      let e = msgEntryCache.get(m);
      if (!e) {
        e = { kind: "standalone", msg: m };
        msgEntryCache.set(m, e);
      }
      slots.push({ kind: "entry", entry: e });
    }

    // ② 逐 turn：逐项引用未变则复用上次 entry（memo 命中），否则只重建该 turn
    const resolved = new Map<number, TimelineEntry>();
    for (const tid of turnOrder) {
      const msgs = turnMsgs.get(tid)!;
      const cached = turnCache.get(tid);
      if (cached && sameRefs(cached.msgs, msgs)) {
        cached.msgs = msgs; // 数组对象换了但元素引用一致：更新句柄，保留 entry
        resolved.set(tid, cached.entry);
        continue;
      }
      const entry = { kind: "turn", turnId: tid, items: buildTurnItems(msgs) } as Extract<TimelineEntry, { kind: "turn" }>;
      turnCache.set(tid, { msgs, entry });
      resolved.set(tid, entry);
    }
    // 清理已消失的 turn（例如回滚/切换会话后），避免缓存无界增长
    if (turnCache.size > turnOrder.length) {
      const live = new Set(turnOrder);
      for (const tid of Array.from(turnCache.keys())) if (!live.has(tid)) turnCache.delete(tid);
    }

    const result = slots.map((s) => (s.kind === "entry" ? s.entry : resolved.get(s.tid)!));
    // 记录本次快照，供下一次调用做「追加快路径」的前缀比对
    lastMessages = messages;
    lastSlots = slots;
    lastResult = result;
    lastTurnId = scanLastTid;
    return result;
  }

  /** 追加快路径（plan-75-334 阶段2）：只重建「被追加的尾部 turn」与新增 turn，
   *  其余 slot 与 entry 引用原样复用——长会话追加一条消息不再是 O(N) 全量重建。
   *
   *  成立条件（由 buildCached 保证前缀一致后进入）：新增消息要么延续尾部 turn，
   *  要么开启全新 turn；一旦发现「已存在的旧 turn 被追加」立即返回 null 回退安全路径，
   *  因此刷新、回滚、乱序与乐观消息替换永远不会走这条路。 */
  function tryAppendTail(messages: MessageOut[], oldLen: number): TimelineEntry[] | null {
    const slots = lastSlots!.slice();
    const appendedByTid = new Map<number, MessageOut[]>();
    const append = (tid: number, m: MessageOut) => {
      const list = appendedByTid.get(tid);
      if (list) list.push(m);
      else appendedByTid.set(tid, [m]);
    };
    let fastLastTid = lastTurnId;

    for (let i = oldLen; i < messages.length; i++) {
      const m = messages[i];
      const tid = m.turn_id ?? null;
      if (tid != null) {
        if (tid === fastLastTid) {
          append(tid, m);
        } else if (!turnCache.has(tid)) {
          // 全新 turn：时间线继续向后延伸
          append(tid, m);
          slots.push({ kind: "turn", tid });
          fastLastTid = tid;
        } else {
          // 已存在的旧 turn 在尾部被追加：乱序插入或 REST 合并混入 → 回退安全路径
          return null;
        }
        continue;
      }
      if (m.sender_type === SenderType.User) {
        // 与安全路径一致：null-turn 用户消息独立成组，且不改变「当前 turn」归属
        let e = msgEntryCache.get(m);
        if (!e) {
          e = { kind: "turn", turnId: null, items: [userItemOf(m)] };
          msgEntryCache.set(m, e);
        }
        slots.push({ kind: "entry", entry: e });
        continue;
      }
      if (fastLastTid != null) {
        append(fastLastTid, m);
        continue;
      }
      let e = msgEntryCache.get(m);
      if (!e) {
        e = { kind: "standalone", msg: m };
        msgEntryCache.set(m, e);
      }
      slots.push({ kind: "entry", entry: e });
    }

    // 应用阶段（所有判定已通过）：只重建受影响的 turn，其 msg 数组由旧数组拼接新增项而来，
    // 保证下次即使回退到安全路径，sameRefs 仍能逐项命中并继续复用 entry。
    for (const [tid, extra] of appendedByTid) {
      const cached = turnCache.get(tid);
      const msgs = cached ? [...cached.msgs, ...extra] : extra;
      const entry = { kind: "turn", turnId: tid, items: buildTurnItems(msgs) } as Extract<TimelineEntry, { kind: "turn" }>;
      turnCache.set(tid, { msgs, entry });
    }

    const result = slots.map((s) => (s.kind === "entry" ? s.entry : turnCache.get(s.tid)!.entry));
    lastMessages = messages;
    lastSlots = slots;
    lastResult = result;
    lastTurnId = fastLastTid;
    return result;
  }

  return function buildCached(messages: MessageOut[]): TimelineEntry[] {
    // plan-75-334 阶段0：记录时间线构建次数（仅采集期间统计，零开销）
    recordDerivation("timelineBuild");

    // 同一数组引用（store 未变）⇒ 直接复用上次结果
    if (lastMessages === messages && lastResult) return lastResult;

    // 追加快路径：有上次快照、长度只增、且旧前缀逐项为同一引用
    if (lastMessages && lastSlots && lastResult && messages.length >= lastMessages.length) {
      const oldLen = lastMessages.length;
      let prefixSame = true;
      for (let i = 0; i < oldLen; i++) {
        if (messages[i] !== lastMessages[i]) { prefixSame = false; break; }
      }
      if (prefixSame) {
        const appended = tryAppendTail(messages, oldLen);
        if (appended) return appended;
      }
    }

    // 安全路径：首次调用、REST 刷新、回滚、乱序插入、乐观消息替换、会话切换等
    return buildFull(messages);
  };
}

/** 会在消息流里展示工作流卡的工具（只读的 workflow_status 不出卡片） */
const WORKFLOW_CARD_TOOLS = new Set(["plan_workflow", "start_workflow", "resume_workflow"]);

/** 用户消息的条目类型：子代理 / 工作流唤醒、目标续跑等系统生成的消息不渲染为用户气泡。
 *  完整构建与增量构建共用此判定，保证实时到达的消息与刷新后的显示一致。 */
function userItemOf(m: MessageOut): TurnItem {
  const uc = (m.content ?? {}) as Record<string, unknown>;
  if (uc.subagent_wakeup === true) return { kind: "subagent-wakeup", msg: m };
  if (uc.workflow_wakeup === true) return { kind: "workflow-wakeup", msg: m };
  if (uc.goal_continuation === true) return { kind: "goal-continuation", msg: m };
  return { kind: "user", msg: m };
}

/** 工作流工具调用 → 程序编号：plan 从结果文本取（编号由服务端生成），start / resume 从参数取；
 *  同一轮内同一程序只出一张卡。 */
function workflowProgramsOf(calls: MessageOut[], resultsByKey: Map<string, MessageOut>): number[] {
  const ids: number[] = [];
  for (const call of calls) {
    const cc = call.content as Record<string, unknown>;
    const tool = String(cc.tool ?? "");
    if (!WORKFLOW_CARD_TOOLS.has(tool)) continue;
    let programId = 0;
    if (tool === "plan_workflow") {
      const result = resultsByKey.get(String(cc.call_key ?? call.id));
      const rc = (result?.content ?? {}) as Record<string, unknown>;
      const m = /program_id=(\d+)/.exec(String(rc.output ?? ""));
      programId = m ? Number(m[1]) : 0;
    } else {
      const args = (cc.args && typeof cc.args === "object" ? cc.args : {}) as Record<string, unknown>;
      programId = Number(args.program_id ?? 0);
    }
    if (programId > 0 && !ids.includes(programId)) ids.push(programId);
  }
  return ids;
}

/** turn 内消息归类：严格按消息时间序；相邻 tool_call 合并 cluster；系统消息 → divider。 */
function buildTurnItems(msgs: MessageOut[]): TurnItem[] {
  // §3.3: 相邻 tool_call 合并为 tool-cluster，AI 文字切段时新开 cluster
  const items: TurnItem[] = [];
  const pendingTools: MessageOut[] = [];
  const resultsByKey = new Map<string, MessageOut>();
  // 同一轮内同一程序只出一张卡：plan 与 start 常被助手文字隔开，分属两次 flush
  const cardedPrograms = new Set<number>();
  // 本段消息所属轮（工作流卡记下它，节点子代理卡据此挂靠到同轮的卡）
  const turnId = msgs[0]?.turn_id ?? null;

  // flush 待处理工具调用为一个 tools item（cluster）
  const flushTools = () => {
    if (pendingTools.length > 0) {
      const nodes = buildToolTree(pendingTools, resultsByKey);
      // 簇的创建位置取其首条调用消息（工作流卡与节点子代理卡的定位依据，见 firstIdOf）
      const firstId = Number(pendingTools[0].id);
      items.push({ kind: "tools", nodes, firstId });
      for (const programId of workflowProgramsOf(pendingTools, resultsByKey)) {
        if (cardedPrograms.has(programId)) continue;
        cardedPrograms.add(programId);
        items.push({ kind: "workflow", programId, turnId, firstId });
      }
      pendingTools.length = 0;
    }
  };

  // §3.3 (plan-548): 全部消息按时间序归类。用户消息不再强制提升到 turn 开头——
  // 正常 turn 其 id 最小天然在前；任务执行中排队注入的消息（id 更大）落在实际发送位置
  for (const m of msgs) {
    if (m.sender_type === SenderType.User) {
      flushTools();
      // plan-671 / v39：目标续跑、子代理与工作流唤醒消息都是系统生成的，不渲染为用户气泡
      items.push(userItemOf(m));
      continue;
    }
    const c = m.content as Record<string, unknown>;
    if (m.msg_type === MsgType.ToolCall) {
      if (c.tool === "spawn_subagent") {
        // v22: spawn_subagent 工具调用独立作为时间线条目，按时间轴精准穿插在消息流中
        // （此前被当成普通工具节点混入 ToolTree，且 SubagentCard 全部堆在用户消息下方）
        flushTools();
        items.push({ kind: "subagent", msg: m });
      } else {
        pendingTools.push(m);
      }
    } else if (m.msg_type === MsgType.ToolResult) {
      if (c.tool === "spawn_subagent") {
        // 子代理结果由 SubagentCard 自身展示，不进入 ToolTree
      } else {
        const key = String(c.call_key ?? "");
        resultsByKey.set(key, m);
      }
    } else if (m.msg_type === MsgType.Thinking || (m.msg_type === MsgType.Text && c.thinking === true)) {
      // v7: 兼容两种后端写法——agent_loop 写 MsgType.Thinking；
      // agent_runtime 的 _emit_thread(thinking=True) 写 MsgType.Text + content.thinking=true。
      // 否则思考内容会被当作正文展示在消息流中间，导致"思考块与消息/工具调用位置错乱"。
      flushTools();
      items.push({ kind: "thinking", msg: m });
    } else if (m.msg_type === MsgType.Summary) {
      flushTools();
      items.push({ kind: "summary", msg: m });
    } else if (m.msg_type === MsgType.Error) {
      flushTools();
      items.push({ kind: "error", msg: m });
    } else if (m.msg_type === MsgType.System) {
      // v2.2 (对齐 zcode 3.11): 系统消息（模型切换 divider 等）渲染为分割线
      flushTools();
      items.push({ kind: "divider", msg: m });
    } else if (m.msg_type === MsgType.Artifact) {
      // plan-548: 产物变更统一在输入框上方任务面板（TaskStatusPanel）展示，消息流不再渲染
      continue;
    } else if (m.msg_type === MsgType.Text) {
      flushTools();
      items.push({ kind: "text", msg: m });
    } else if (m.msg_type === MsgType.Plan) {
      // plan-865: 计划预览/确认消息——时间线独立条目，按数据库位置渲染计划卡
      flushTools();
      items.push({ kind: "plan", msg: m });
    } else {
      flushTools();
      items.push({ kind: "text", msg: m });
    }
  }

  // 3. flush 剩余工具调用
  flushTools();

  return items;
}

/** 获取 turn 的首条用户消息摘要（供 JumpDots 浮窗）。 */
export function turnPreview(turn: TimelineEntry & { kind: "turn" }): string {
  const user = turn.items.find((it) => it.kind === "user");
  if (!user || user.kind !== "user") return "";
  const text = msgText(user.msg.content).trim();
  if (text) return text.length > 40 ? `${text.slice(0, 40)}…` : text;
  // v14: 仅附件无文字的消息，摘要显示附件名
  const atts = (user.msg.content as Record<string, unknown>).attachments;
  if (Array.isArray(atts) && atts.length > 0) {
    const names = atts.map((a) => String((a as Record<string, unknown>).filename ?? "")).filter(Boolean);
    const joined = `📎 ${names.join(", ")}`;
    return joined.length > 40 ? `${joined.slice(0, 40)}…` : joined;
  }
  return "";
}

/** plan-334-1661 S3：按 id 取 turn 行（O(1) 索引，替代热路径上的 `turns.find`）。
 *
 * ── 为什么 ──
 * TurnGroup 的两个 selector（WorkTimer 起止时间、本 turn 行状态）在**每个可见 turn** 上
 * 每帧各执行一次，内部 `s.turns.find(...)` 是 O(turns)；长会话（数百~上千 turn）时
 * 叠加为「每帧 × 可见 turn 数 × turns 长度」的纯 CPU 开销，直接与流式帧预算竞争。
 *
 * ── 实现 ──
 * 以 turns 数组**引用**为键缓存 id→turn 的 Map：store 走不可变更新，只有确实改过 turn 行
 * （新 turn / 状态流转等低频事件）才换数组引用 → 重建索引；流式 flush 不改 turns ⇒ 缓存命中。
 * 返回语义与 `find` 一致：取**首个**匹配（正常不存在重复 id）。
 */
const turnIndexCache = new WeakMap<readonly TurnOut[], Map<number, TurnOut>>();

export function getTurnById(
  turns: readonly TurnOut[] | undefined,
  turnId: number | null | undefined,
): TurnOut | undefined {
  if (!turns || turnId == null) return undefined;
  let map = turnIndexCache.get(turns);
  if (!map || map.size !== turns.length) {
    map = new Map<number, TurnOut>();
    for (const t of turns) if (!map.has(t.id)) map.set(t.id, t);
    turnIndexCache.set(turns, map);
  }
  return map.get(turnId);
}
