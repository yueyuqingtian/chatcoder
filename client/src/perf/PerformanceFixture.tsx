/** plan-20261101-performance：独立回放入口，使用真实组件与假数据，绝不连接用户数据库。 */
import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { MessageOut, TurnOut } from "../api/client";
import { useChatStore } from "../store/chat";
import { initialMessages } from "../store/messageHistory";
import { registerBuiltinPlugins } from "../plugins/builtin";
import { MessageFlow } from "../components/chat/MessageFlow";
import { SubagentPanel } from "../components/panel/SubagentPanel";
import { ResizeHandle } from "../components/ResizeHandle";
import { IxBot } from "../components/iconkit/StackIcons";
import { Button, Dialog, Input } from "../components/ui";
import * as metrics from "./metrics";
import "../styles/fonts.css";
import "../styles/global.css";
import "../styles/components.css";
import "../styles/panel.css";
import "../styles/flow.css";
import "../styles/motion.css";
import "./performance-fixture.css";

const SID = 910001;
let history: MessageOut[] = [];
let child: MessageOut[] = [];
let revision = 0;
let sequence = 0;
const message = (turn: number, type: string, content: Record<string, unknown>, user = false): MessageOut => ({
  id: ++sequence, session_id: SID, turn_id: turn, thread_id: null, sender_type: user ? "user" : "agent",
  sender_id: 1, msg_type: type, content, token_usage: 0, created_at: "2026-11-01T05:00:00Z",
});
function fixture(rounds: number, heavy: boolean) {
  sequence = 0;
  history = [];
  const turns: TurnOut[] = [];
  for (let index = 1; index <= rounds; index++) {
    history.push(message(index, "text", { text: `请检查第 ${index} 轮的渲染与操作。` }, true));
    for (let tool = 0; tool < (heavy && index === rounds ? 1000 : 2); tool++) {
      const call_key = `call-${index}-${tool}`;
      history.push(message(index, "tool_call", { tool: "fs_read", call_key, args: { path: `src/module-${tool}.ts` } }));
      history.push(message(index, "tool_result", { tool: "fs_read", call_key, output: "检查通过，文件内容完整。", ok: true }));
    }
    history.push(message(index, "text", { text: `### 第 ${index} 轮检查结果\n\n已完成消息结构和操作路径检查。滚动阅读时保持位置，运行反馈与正文分层展示。\n\n` +
      (heavy && index === rounds ? "```ts\n" + "const value = '长代码窗口与完整复制测试';\n".repeat(5000) + "```\n\n| 项目 | 状态 |\n| --- | --- |\n" + "| 长表格 | 已完成 |\n".repeat(400) : "- 操作入口清楚\n- 消息顺序正确\n- 面板可以实时拖动") }));
    turns.push({ id: index, session_id: SID, status: "completed", started_at: "2026-11-01T05:00:00Z", completed_at: "2026-11-01T05:00:10Z" } as TurnOut);
  }
  child = history.slice(-6).map((item) => ({ ...item, thread_id: 91 }));
  useChatStore.setState({ currentSessionId: SID, messages: [], turns, isRunning: false, runningTurnId: null,
    sessionLoading: false, subagentMessages: { 91: child }, subagentMeta: { 91: {
      agentId: 91, name: "核查长上下文消息流与面板实时拖拽体验", status: "completed", messageCount: child.length,
      startedAt: "2026-11-01T05:00:00Z", endedAt: "2026-11-01T05:00:10Z", tokens: 3134942, filesCount: 3,
    } as never } });
}

window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  if (init?.signal?.aborted) throw new DOMException("已取消", "AbortError");
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
  const source = url.searchParams.has("thread_id") ? child : history;
  let result: unknown = [];
  if (url.pathname.endsWith("/messages/page")) {
    const before = Number(url.searchParams.get("before_id")) || Infinity;
    const after = Number(url.searchParams.get("after_id")) || 0;
    const around = Number(url.searchParams.get("around_id")) || 0;
    let rows = source.filter((m) => m.id < before && m.id > after);
    if (around) { const pos = source.findIndex((m) => m.id >= around); rows = source.slice(Math.max(0, pos - 50), pos + 50); }
    else rows = after ? rows.slice(0, 100) : rows.slice(-100);
    result = { items: rows, context: [], before_id: rows[0]?.id ?? null, after_id: rows.at(-1)?.id ?? null,
      has_before: source.some((m) => m.id < (rows[0]?.id ?? 0)), has_after: source.some((m) => m.id > (rows.at(-1)?.id ?? Infinity)), revision, boundary_turn_ids: [] };
  } else if (url.pathname.endsWith("/messages/search")) {
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    result = { items: source.filter((m) => String(m.content?.text ?? m.content?.output ?? "").toLowerCase().includes(q)).slice(-100).reverse().map((m) => ({ id: m.id, turn_id: m.turn_id, snippet: "回放搜索命中" })), has_more: false, before_id: null };
  } else if (url.pathname.endsWith("/subagents")) result = [];
  else if (url.pathname.endsWith("/messages")) result = source;
  return new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json" } });
}) as typeof fetch;
registerBuiltinPlugins();
fixture(100, false);
void initialMessages(SID).then((messages) => useChatStore.setState({ messages }));

function FixtureApp() {
  const left = useRef<HTMLDivElement>(null), right = useRef<HTMLDivElement>(null);
  const [leftWidth, setLeftWidth] = useState(260), [rightWidth, setRightWidth] = useState(400);
  const [rounds, setRounds] = useState(100), [dialog, setDialog] = useState(false), [report, setReport] = useState("尚未采集");
  const [rate, setRate] = useState(0);
  const [hidden, setHidden] = useState(false);
  const [dark, setDark] = useState(false);
  const samplingTimer = useRef(0);
  const text = useRef("");
  const arrivals = useRef<Array<{ length: number; at: number }>>([]);
  const latencies = useRef<number[]>([]);
  const sample = () => {
    if (document.visibilityState !== "visible") { setReport("请将回放页面切换到前台后采集；后台标签限频数据不计入验收。"); return; }
    metrics.reset(); metrics.start(); setReport("正在采集 10 秒，请滚动或拖拽面板…");
    window.clearTimeout(samplingTimer.current);
    samplingTimer.current = window.setTimeout(() => {
      metrics.stop(); const delays = latencies.current.slice().sort((a, b) => a - b);
      setReport(JSON.stringify({ ...metrics.snapshot(), visibility: document.visibilityState,
        localDisplayP95Ms: delays.length ? Math.round(delays[Math.floor((delays.length - 1) * .95)]) : null,
        localDisplaySamples: delays.length, domNodes: document.querySelectorAll(".message-flow *").length,
        mountedBlocks: document.querySelectorAll(".viewport-block[data-mounted]").length, totalBlocks: document.querySelectorAll(".viewport-block").length }, null, 2));
    }, 10000);
  };
  useEffect(() => {
    if (!rate) return;
    text.current = "";
    arrivals.current = []; latencies.current = [];
    useChatStore.setState({ isRunning: true, runningTurnId: rounds + 1, streamingBuffers: {}, thinkingBuffers: {} });
    const interval = window.setInterval(() => {
      for (let index = 0; index < Math.max(1, Math.round(rate / 30)); index++) {
        const delta = "输出更新：保持拖拽跟手与消息顺序。\n\n";
        text.current += delta;
        arrivals.current.push({ length: text.current.length, at: performance.now() });
        useChatStore.getState().handleWs("token.delta", { session_id: SID, agent_id: 1, delta });
      }
    }, 1000 / 30);
    return () => { window.clearInterval(interval); useChatStore.setState({ isRunning: false, streamingBuffers: {}, runningTurnId: null }); };
  }, [rate, rounds]);
  useEffect(() => {
    const observer = new MutationObserver(() => {
      const shown = Number(document.querySelector(".perf-main .streaming-tail .md-body-stream")?.getAttribute("data-rendered-length") ?? 0);
      let count = 0;
      while (count < arrivals.current.length && arrivals.current[count].length <= shown) {
        latencies.current.push(performance.now() - arrivals.current[count++].at);
      }
      if (count) arrivals.current.splice(0, count);
    });
    observer.observe(document.getElementById("root")!, { attributes: true, attributeFilter: ["data-rendered-length"], subtree: true });
    return () => observer.disconnect();
  }, []);
  useEffect(() => () => { window.clearTimeout(samplingTimer.current); metrics.stop(); }, []);
  const load = async (count: number, heavy = false) => {
    setRate(0); setRounds(count); fixture(count, heavy);
    useChatStore.setState({ messages: await initialMessages(SID) });
  };
  return <div className="perf-fixture">
    <header className="perf-toolbar">
      <strong>消息流回归工作台</strong>
      {[100, 500, 2000].map((count) => <Button key={count} size="xs" onClick={() => void load(count)}>{count} 轮</Button>)}
      <Button size="xs" onClick={() => void load(1, true)}>长代码与表格</Button>
      <Button size="xs" onClick={() => useChatStore.setState({ messages: history })}>加载全部（压力测试）</Button>
      <Button size="xs" onClick={() => setRate(rate ? 0 : 300)}>{rate ? "停止回放" : "300 事件/秒"}</Button>
      <Button size="xs" onClick={sample}>采集 10 秒</Button>
      <Button size="xs" onClick={() => setDialog(true)}>检查弹窗</Button>
      <Button size="xs" onClick={() => { setDark(!dark); document.documentElement.dataset.theme = dark ? "light" : "dark"; }}>切换主题</Button>
      <Button size="xs" onClick={() => setHidden(!hidden)}>{hidden ? "显示子代理" : "隐藏子代理"}</Button>
    </header>
    <div className="perf-workspace">
      <aside ref={left} style={{ width: leftWidth, flexBasis: leftWidth }} className="perf-sidebar">
        <strong>chatcoder</strong><p>长会话与流式回放</p>
        <Button block onClick={() => void load(rounds)}>重新加载末页</Button>
        <p>回放数据保存在内存，不连接真实后端。</p>
        <details><summary>采集报告</summary><pre>{report}</pre></details>
      </aside>
      <ResizeHandle side="left" baseWidth={leftWidth} minWidth={180} maxWidth={400} panelEl={left} onCommit={setLeftWidth} />
      <main className="perf-main"><header>前端展示与性能优化 <span>{rounds} 轮历史</span></header>
        <MessageFlow source="main" features={{ suggestionCard: false }} />
        <footer><Input placeholder="输入任务目标…" /><Button variant="primary" onClick={() => setDialog(true)}>发送</Button></footer>
      </main>
      <ResizeHandle side="right" baseWidth={rightWidth} minWidth={280} maxWidth={1000} panelEl={right} onCommit={setRightWidth} freezeRefs={[right]} />
      <aside ref={right} className="perf-right" style={{ width: rightWidth, flexBasis: rightWidth }}>
        <header><IxBot size={14} /> 子代理</header>
        <div className="perf-subagent" style={{ display: hidden ? "none" : "flex" }}><SubagentPanel threadId={91} visible={!hidden} /></div>
      </aside>
    </div>
    <Dialog open={dialog} onClose={() => setDialog(false)} size="compact" title="操作确认" subtitle="检查焦点、间距与关闭行为。"
      footer={<><Button onClick={() => setDialog(false)}>取消</Button><Button variant="primary" onClick={() => setDialog(false)}>确认</Button></>}>
      <Input aria-label="示例输入" placeholder="输入内容后按 Esc 关闭" />
    </Dialog>
    <output className="perf-status" aria-live="polite">{report === "尚未采集" ? "回放就绪" : report}</output>
  </div>;
}
createRoot(document.getElementById("root")!).render(<FixtureApp />);
