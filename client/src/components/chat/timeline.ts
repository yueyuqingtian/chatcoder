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
  /** null = 结果尚未返回 */
  ok: boolean | null;
  output: string;
  error: string | null;
  durationMs: number | null;
}

export type ToolNode =
  | { kind: "group"; tool: string; count: number; leaves: ToolLeaf[] }
  | { kind: "leaf"; leaf: ToolLeaf };

export type TurnItem =
  | { kind: "user"; msg: MessageOut }
  | { kind: "thinking"; msg: MessageOut }
  | { kind: "text"; msg: MessageOut }
  | { kind: "tools"; nodes: ToolNode[] }
  | { kind: "artifacts"; msgs: MessageOut[] }
  | { kind: "summary"; msg: MessageOut }
  | { kind: "error"; msg: MessageOut };

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
  return {
    callKey: String(cc.call_key ?? call.id),
    tool: String(cc.tool ?? "tool"),
    args: (cc.args && typeof cc.args === "object" ? cc.args : {}) as Record<string, unknown>,
    agentName: String(cc.agent_name ?? ""),
    ok: result ? Boolean(rc.ok) : null,
    output: typeof rc.output === "string" ? rc.output : "",
    error: typeof rc.error === "string" ? rc.error : null,
    durationMs: rc.duration_ms != null ? Number(rc.duration_ms) : null,
  };
}

/** 将某 turn 的工具调用/结果消息聚合成 ToolNode[]。 */
function buildToolTree(calls: MessageOut[], resultsByKey: Map<string, MessageOut>): ToolNode[] {
  const nodes: ToolNode[] = [];
  let i = 0;
  while (i < calls.length) {
    const first = calls[i];
    const firstLeaf = buildLeaf(first, resultsByKey.get(String((first.content as Record<string, unknown>).call_key ?? first.id)));
    // 统计连续同工具调用（跳过结果消息）
    let j = i + 1;
    let count = 1;
    while (j < calls.length) {
      const cj = calls[j].content as Record<string, unknown>;
      if (String(cj.tool) === firstLeaf.tool) {
        count++;
        j++;
      } else {
        break;
      }
    }
    if (count >= 2) {
      const leaves = calls.slice(i, j).map((c) =>
        buildLeaf(c, resultsByKey.get(String((c.content as Record<string, unknown>).call_key ?? c.id))),
      );
      nodes.push({ kind: "group", tool: firstLeaf.tool, count, leaves });
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
  // 1. 按 turn_id 分组，同时记录每个 turn 首次出现的位置（保持输入顺序）
  const turnOrder: (number | null)[] = [];
  const turnMap = new Map<number | null, MessageOut[]>();
  for (const m of messages) {
    const key = m.turn_id ?? null;
    if (!turnMap.has(key)) {
      turnMap.set(key, []);
      turnOrder.push(key);
    }
    turnMap.get(key)!.push(m);
  }

  const entries: TimelineEntry[] = [];
  // 记录上一个有 turn_id 的组消息数组，用于无 turn_id 消息就近归属
  let lastTurnMsgs: MessageOut[] | null = null;

  for (const tid of turnOrder) {
    const msgs = turnMap.get(tid)!;
    if (tid == null) {
      // 无 turn_id：user 类型独立成组；其余并入上一个 turn
      const userMsgs = msgs.filter((m) => m.sender_type === SenderType.User);
      const others = msgs.filter((m) => m.sender_type !== SenderType.User);
      for (const u of userMsgs) {
        entries.push({ kind: "turn", turnId: null, items: [{ kind: "user", msg: u }] });
      }
      if (others.length > 0 && lastTurnMsgs) {
        lastTurnMsgs.push(...others);
      } else if (others.length > 0) {
        for (const m of others) {
          entries.push({ kind: "standalone", msg: m });
        }
      }
      continue;
    }

    // 2. turn 内按消息顺序归类
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

    lastTurnMsgs = msgs;
    entries.push({ kind: "turn", turnId: tid, items });
  }

  return entries;
}

/** 获取 turn 的首条用户消息摘要（供 JumpDots 浮窗）。 */
export function turnPreview(turn: TimelineEntry & { kind: "turn" }): string {
  const user = turn.items.find((it) => it.kind === "user");
  if (!user || user.kind !== "user") return "";
  const text = msgText(user.msg.content).trim();
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}
