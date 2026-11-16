/** plan-20261114-message-flow：隔离回放真实消息组件，不访问用户数据库或模型。 */
import { useState } from "react";
import { createRoot } from "react-dom/client";
import type { MessageOut, TurnOut } from "../api/client";
import { useChatStore } from "../store/chat";
import { registerBuiltinPlugins } from "../plugins/builtin";
import { buildTimeline } from "../components/chat/timeline";
import { TurnGroup } from "../components/chat/TurnGroup";
import { StreamingTail } from "../components/chat/StreamingTail";
import { handOffThinkingExpand } from "../store/flowExpand";
import { Button } from "../components/ui/Button";
import "../styles/fonts.css";
import "../styles/global.css";
import "../styles/components.css";
import "../styles/flow.css";
import "../styles/motion.css";

window.fetch = (async () => new Response("[]", { headers: { "Content-Type": "application/json" } })) as typeof fetch;
registerBuiltinPlugins();
const start = Date.now() - 125000;
const msg = (id: number, type: string, content: Record<string, unknown>, user = false): MessageOut => ({
  id, session_id: 990001, turn_id: 990001, thread_id: null, sender_type: user ? "user" : "agent",
  sender_id: user ? null : 1, msg_type: type, content, created_at: new Date(start + id * 1000).toISOString(),
});
const base = [
  msg(1, "text", { text: "请检查消息流并修复显示问题。" }, true),
  msg(2, "text", { text: "我先检查相关实现。" }),
  msg(3, "thinking", { text: "先查找消息的顺序和折叠分组。" }),
  msg(4, "tool_call", { tool: "fs_read", call_key: "qa-read", args: { path: "client/src/components/chat/TurnGroup.tsx" } }),
  msg(5, "tool_result", { tool: "fs_read", call_key: "qa-read", ok: true, output: "读取完成，待核对工具与思考顺序。" }),
  msg(6, "tool_call", { tool: "spawn_subagent", call_key: "qa-spawn", args: { task_title: "核对消息时序", explore: true } }),
  msg(7, "tool_result", { tool: "spawn_subagent", call_key: "qa-spawn", ok: true, output: "Subagent #990002 started" }),
  msg(8, "thinking", { text: "读取完成，现在运行类型检查。" }),
  msg(9, "tool_call", { tool: "terminal_exec", call_key: "qa-run", args: { command: "npm run typecheck" } }),
];
function App() {
  const [stage, setStage] = useState(0);
  const [narrow, setNarrow] = useState(false);
  const [compact, setCompact] = useState(false);
  const apply = (next: number) => {
    setStage(next);
    const messages = next === 4 ? base.filter((m) => m.msg_type !== "text" || m.sender_type === "user") : [...base];
    if (next >= 2 && next !== 4) messages.push(msg(10, "thinking", { text: "检查结果正常，开始整理结论。" }));
    if (next === 3) messages.push(msg(11, "text", { text: "消息流检查完成，相邻过程已合并，子代理进度实时更新。" }));
    const running = next < 3;
    const child = next === 0 ? [msg(20, "tool_call", { tool: "fs_grep", call_key: "qa-child", args: { query: "thinking" } })]
      : [msg(20, "tool_call", { tool: "fs_grep", call_key: "qa-child", args: { query: "thinking" } }), msg(21, "tool_result", { call_key: "qa-child", ok: true })];
    if (next === 2) handOffThinkingExpand(10);
    useChatStore.setState({ currentSessionId: 990001, messages, isRunning: running, runningTurnId: running ? 990001 : null,
      turns: [{ id: 990001, session_id: 990001, status: running ? "running" : "completed", started_at: new Date(start).toISOString(), completed_at: running ? null : new Date(start + 125000).toISOString() } as TurnOut],
      runningToolResults: next > 0 ? { "qa-run": { ok: true } } : {},
      thinkingBuffers: next === 1 ? { 1: "检查结果正常，开始整理结论。" } : {},
      subagentMessages: { 990002: child }, subagentThinking: next === 1 ? { 990002: "核对新的顺序" } : {},
      subagentStreams: next === 2 ? { 990002: "已核对读取\n正在检查折叠边界" } : {},
      subagentMeta: { 990002: { agentId: 990002, name: "核对消息时序", status: running ? "running" : "completed", turnId: 990001 } as never },
    });
  };
  const messages = useChatStore((s) => s.messages);
  const running = stage < 3;
  const entry = buildTimeline(messages)[0];
  return <main style={{ padding: 28, maxWidth: 1000, margin: "0 auto" }}>
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 24 }}>
      {["运行工具", "继续思考", "思考落库", "完成任务", "纯工具完成"].map((label, i) => <Button key={label} onClick={() => apply(i)}>{label}</Button>)}
      <Button onClick={() => setNarrow(!narrow)}>切换窄宽</Button>
      <Button onClick={() => { setCompact(!compact); document.documentElement.style.setProperty("--chat-line-height", compact ? "2.2" : "1.5"); }}>切换间距</Button>
      <Button onClick={() => { document.documentElement.dataset.motion = document.documentElement.dataset.motion === "off" ? "full" : "off"; }}>切换动画</Button>
    </div>
    <section aria-label="消息流回放" style={{ width: narrow ? 340 : "100%", maxWidth: "100%" }}>
      {entry?.kind === "turn" && <TurnGroup entry={entry} isRunning={running} globalRunning={running} latestUserId={1}
        flowErrorTurnId={null} hiddenTurnErrors={[]} turnStatus={running ? "running" : "completed"}
        subagents={[{ agentId: 990002, name: "核对消息时序", status: running ? "running" : "completed", turnId: 990001 }]} />}
      <StreamingTail active={running && Boolean(entry)} thinkingInTimeline workStartMs={start} />
    </section>
  </main>;
}
createRoot(document.getElementById("root")!).render(<App />);
