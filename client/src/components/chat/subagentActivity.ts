/** plan-20261114-message-flow：活动预览以当前流和调用结果为准，不能让历史调用遮住下一阶段。 */
import { MsgType, type MessageOut } from "@chatcoder/shared";
import { normalizeToolName, toolLabel } from "./toolLabels";

export function activityTarget(args: Record<string, unknown>): string {
  const path = args.path ?? args.file_path ?? args.filepath ?? args.directory ?? args.target_directory;
  if (typeof path === "string" && path.trim()) return path.trim().replace(/\\/g, "/").split("/").filter(Boolean).at(-1) ?? path;
  const value = args.command ?? args.cmd ?? args.query ?? args.pattern ?? args.url;
  return typeof value === "string" ? value.trim().split("\n")[0] : "";
}

function callKey(content: Record<string, unknown>): string {
  return String(content.call_key ?? content.tool_call_id ?? content.call_id ?? "");
}

export function subagentActivity({ messages = [], thinking = "", text = "", running, results = {} }: {
  messages?: readonly MessageOut[];
  thinking?: string;
  text?: string;
  running: boolean;
  results?: Readonly<Record<string, unknown>>;
}): string {
  let lastText: string | undefined, lastThinking: string | undefined;
  for (let i = messages.length - 1; i >= 0 && (lastText === undefined || lastThinking === undefined); i--) {
    const message = messages[i];
    const value = typeof message.content.text === "string" ? message.content.text.trim() : "";
    if (message.msg_type === MsgType.Thinking || message.content.thinking === true) lastThinking ??= value;
    else if (message.msg_type === MsgType.Text) lastText ??= value;
  }
  if (running && text.trim() && text.trim() !== lastText) {
    const line = text.trim().split("\n").filter((s) => s.trim()).at(-1)?.trim() ?? "";
    return `1|${line.slice(0, 120)}`;
  }
  if (running && thinking.trim() && thinking.trim() !== lastThinking) return "1|正在思考分析";

  const completed = new Map<string, boolean>();
  let latest = "";
  for (let k = messages.length - 1; k >= 0; k--) {
    const m = messages[k];
    const c = m.content ?? {};
    if (m.msg_type === MsgType.ToolResult) {
      const key = callKey(c);
      if (key) completed.set(key, c.ok !== false);
      continue;
    }
    if (m.msg_type === MsgType.ToolCall) {
      const tool = normalizeToolName(String(c.tool ?? c.tool_name ?? c.name ?? ""));
      const args = (c.args ?? c.arguments ?? {}) as Record<string, unknown>;
      const target = activityTarget(args);
      const key = callKey(c);
      const pending = running && !completed.has(key) && !Object.hasOwn(results, key);
      const action = `${toolLabel(tool).verb}${target ? ` ${target}` : ""}`;
      if (pending) return `1|正在${action}`;
      if (!latest) latest = `0|${completed.get(key) === false || (results[key] as { ok?: boolean } | undefined)?.ok === false ? "执行失败：" : "已"}${action}`;
    } else if (m.msg_type === MsgType.Thinking || c.thinking === true) {
      if (!latest) latest = "0|已完成思考";
    } else if (m.msg_type === MsgType.Text && !latest) {
      const value = typeof c.text === "string" ? c.text.trim() : "";
      if (value) latest = `0|${value.split("\n").find((line) => line.trim())?.trim().slice(0, 120) ?? ""}`;
    }
  }
  return latest;
}
