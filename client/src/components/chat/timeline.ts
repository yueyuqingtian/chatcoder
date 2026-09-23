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
  | { kind: "tools"; nodes: ToolNode[] }
  | { kind: "subagent"; msg: MessageOut }
  | { kind: "summary"; msg: MessageOut }
  | { kind: "error"; msg: MessageOut }
  /** v2.2 (对齐 zcode 3.11): 系统分割线（模型切换等 divider） */
  | { kind: "divider"; msg: MessageOut }
  /** plan-671: 目标续跑消息（zcode model-only 语义）——渲染为细分隔线而非用户气泡 */
  | { kind: "goal-continuation"; msg: MessageOut }
  /** v39: 子代理完成唤醒消息（系统生成）——同样渲染为细分隔线 */
  | { kind: "subagent-wakeup"; msg: MessageOut }
  /** plan-865: 计划预览/确认消息——按数据库时间线位置渲染计划卡 */
  | { kind: "plan"; msg: MessageOut };

export type TimelineEntry =
  | { kind: "turn"; turnId: number | null; items: TurnItem[] }
  | { kind: "standalone"; msg: MessageOut };

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
      // v39: 子代理完成唤醒消息（turn_id 回填前/独立到达）按分隔线渲染，不显示为用户气泡
      const ucU = m.content as Record<string, unknown>;
      entries.push({
        kind: "turn", turnId: null,
        items: ucU?.subagent_wakeup === true
          ? [{ kind: "subagent-wakeup", msg: m }]
          : [{ kind: "user", msg: m }],
      });
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

  return function buildCached(messages: MessageOut[]): TimelineEntry[] {
    // ① 单遍扫描：与 buildTimeline 同一套聚合规则，但只记录「顺序槽位」，
    //    不在这里创建 turn entry 对象（推迟到第 ② 步按缓存决定复用还是新建）。
    type Slot = { kind: "turn"; tid: number } | { kind: "entry"; entry: TimelineEntry };
    const slots: Slot[] = [];
    const turnMsgs = new Map<number, MessageOut[]>();
    const turnOrder: number[] = [];
    let lastTurnId: number | null = null;

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
        lastTurnId = tid;
        continue;
      }
      if (m.sender_type === SenderType.User) {
        let e = msgEntryCache.get(m);
        if (!e) {
          e = { kind: "turn", turnId: null, items: [{ kind: "user", msg: m }] };
          msgEntryCache.set(m, e);
        }
        slots.push({ kind: "entry", entry: e });
        continue;
      }
      if (lastTurnId != null) {
        const arr = turnMsgs.get(lastTurnId);
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

    return slots.map((s) => (s.kind === "entry" ? s.entry : resolved.get(s.tid)!));
  };
}

/** turn 内消息归类：严格按消息时间序；相邻 tool_call 合并 cluster；系统消息 → divider。 */
function buildTurnItems(msgs: MessageOut[]): TurnItem[] {
  // §3.3: 相邻 tool_call 合并为 tool-cluster，AI 文字切段时新开 cluster
  const items: TurnItem[] = [];
  const pendingTools: MessageOut[] = [];
  const resultsByKey = new Map<string, MessageOut>();

  // flush 待处理工具调用为一个 tools item（cluster）
  const flushTools = () => {
    if (pendingTools.length > 0) {
      const nodes = buildToolTree(pendingTools, resultsByKey);
      items.push({ kind: "tools", nodes });
      pendingTools.length = 0;
    }
  };

  // §3.3 (plan-548): 全部消息按时间序归类。用户消息不再强制提升到 turn 开头——
  // 正常 turn 其 id 最小天然在前；任务执行中排队注入的消息（id 更大）落在实际发送位置
  for (const m of msgs) {
    if (m.sender_type === SenderType.User) {
      flushTools();
      // plan-671: 目标续跑消息不渲染为用户气泡（对齐 zcode providerContextOnly）
      const uc = m.content as Record<string, unknown>;
      // v39: 子代理完成唤醒消息同样不渲染为用户气泡（系统生成，对应「等待子代理结束…」的续跑轮）
      items.push(uc.subagent_wakeup === true
        ? { kind: "subagent-wakeup", msg: m }
        : uc.goal_continuation === true
          ? { kind: "goal-continuation", msg: m }
          : { kind: "user", msg: m });
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
