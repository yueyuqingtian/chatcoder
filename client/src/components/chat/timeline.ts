/**
 * 消息时间线聚合（v4 全量重写）：将扁平消息列表重建为 TurnGroup 结构。
 *
 * 核心规则：
 * - 按 turn_id 分组；turn 出现顺序由该组首条消息在输入数组中的位置决定（非 turn_id 数值大小）
 *   —— 这修复了"AI 回复显示在用户消息上方"的顺序 bug（§9.1）
 * - turn 内：用户消息排在开头（用户提问在前，AI 回复在后）
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
  /** 连续探索类工具（读/搜/列）≥2 次合并为一行「探索 · N 文件」 */
  | { kind: "explore"; leaves: ToolLeaf[] };

/** 探索类工具：连续出现时合并展示；写入/终端等操作永不合并 */
const EXPLORE_TOOLS = new Set([
  "fs_read", "fs_list", "fs_grep", "codebase_search",
  "web_search", "web_fetch", "memory_search", "view_image", "read_attachment",
]);

export type TurnItem =
  | { kind: "user"; msg: MessageOut }
  | { kind: "thinking"; msg: MessageOut }
  | { kind: "text"; msg: MessageOut }
  | { kind: "tools"; nodes: ToolNode[] }
  | { kind: "artifacts"; msgs: MessageOut[] }
  | { kind: "summary"; msg: MessageOut }
  | { kind: "error"; msg: MessageOut }
  /** v2.2 (对齐 zcode 3.11): 系统分割线（模型切换等 divider） */
  | { kind: "divider"; msg: MessageOut };

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
    ok: result ? Boolean(rc.ok) : null,
    output: typeof rc.output === "string" ? rc.output : "",
    error: typeof rc.error === "string" ? rc.error : null,
    durationMs: rc.duration_ms != null ? Number(rc.duration_ms) : null,
    changeStat: statRaw && typeof statRaw.path === "string"
      ? { path: statRaw.path, additions: Number(statRaw.additions ?? 0), deletions: Number(statRaw.deletions ?? 0) }
      : null,
  };
}

/** 将某 turn 的工具调用/结果消息聚合成 ToolNode[]。
 *  v15: 连续探索类调用 ≥2 合并为 explore 节点（zcode「探索 · N 文件」）；
 *  写入/终端等操作类调用始终独占一行（完整展示命令/文件与 +N -M 统计）。 */
function buildToolTree(calls: MessageOut[], resultsByKey: Map<string, MessageOut>): ToolNode[] {
  const nodes: ToolNode[] = [];
  let i = 0;
  const leafOf = (c: MessageOut) =>
    buildLeaf(c, resultsByKey.get(String((c.content as Record<string, unknown>).call_key ?? c.id)));
  while (i < calls.length) {
    const firstLeaf = leafOf(calls[i]);
    if (EXPLORE_TOOLS.has(firstLeaf.tool)) {
      // 收集连续探索调用
      let j = i + 1;
      while (j < calls.length) {
        const lj = leafOf(calls[j]);
        if (!EXPLORE_TOOLS.has(lj.tool)) break;
        j++;
      }
      const leaves = calls.slice(i, j).map(leafOf);
      if (leaves.length >= 2) {
        nodes.push({ kind: "explore", leaves });
      } else {
        nodes.push({ kind: "leaf", leaf: leaves[0] });
      }
      i = j;
    } else {
      nodes.push({ kind: "leaf", leaf: firstLeaf });
      i++;
    }
  }
  return nodes;
}

/** 从扁平消息构建时间线。
 *  关键修复：turn 排序依据是该 turn 首条消息在输入数组中的原始位置，而非 turn_id 数值。
 *  这保证用户消息（先落库）所在的 turn 始终排在 AI 回复 turn 之前。
 */
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

/** turn 内消息归类：用户消息在开头；相邻 tool_call 合并 cluster；系统消息 → divider。 */
function buildTurnItems(msgs: MessageOut[]): TurnItem[] {
  // §3.3: 相邻 tool_call 合并为 tool-cluster，AI 文字切段时新开 cluster
  const items: TurnItem[] = [];
  const pendingTools: MessageOut[] = [];
  const resultsByKey = new Map<string, MessageOut>();
  const pendingArtifacts: MessageOut[] = [];
  let hasArtifacts = false;

  // flush 待处理工具调用为一个 tools item（cluster）
  const flushTools = () => {
    if (pendingTools.length > 0) {
      const nodes = buildToolTree(pendingTools, resultsByKey);
      items.push({ kind: "tools", nodes });
      pendingTools.length = 0;
    }
  };

  // §3.3: 用户消息排在 turn 开头（用户提问在前，AI 回复在后）
  const userMsgs = msgs.filter((m) => m.sender_type === SenderType.User);
  for (const u of userMsgs) {
    items.push({ kind: "user", msg: u });
  }

  // AI 消息按顺序归类
  for (const m of msgs) {
    if (m.sender_type === SenderType.User) continue; // 跳过用户消息（已处理）
    const c = m.content as Record<string, unknown>;
    if (m.msg_type === MsgType.ToolCall) {
      pendingTools.push(m);
    } else if (m.msg_type === MsgType.ToolResult) {
      const key = String(c.call_key ?? "");
      resultsByKey.set(key, m);
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
      flushTools();
      pendingArtifacts.push(m);
      hasArtifacts = true;
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

  // 4. 产物聚合到 turn 末尾
  if (hasArtifacts && pendingArtifacts.length > 0) {
    items.push({ kind: "artifacts", msgs: pendingArtifacts });
  }

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
