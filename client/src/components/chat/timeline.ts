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
  | { kind: "goal-continuation"; msg: MessageOut };

export type TimelineEntry =
  | { kind: "turn"; turnId: number | null; items: TurnItem[] }
  | { kind: "standalone"; msg: MessageOut };

export function msgText(c: Record<string, unknown>): string {
  const t = c.text;
  return typeof t === "string" ? t : "";
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
      entries.push({ kind: "turn", turnId: null, items: [{ kind: "user", msg: m }] });
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
      items.push(uc.goal_continuation === true
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
      flushTools();
      items.push({ kind: "text", msg: m });
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
