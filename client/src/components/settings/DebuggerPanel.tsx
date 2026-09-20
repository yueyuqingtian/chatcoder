/** DebuggerPanel —— 开发调试配置（plan-282-1441 #8）。
 *
 * 作为内置 MCP「开发调试」的详情页，嵌入「拓展 → 连接器」：
 *  - 内置 MCP 启用开关（默认关闭）
 *  - Web 前端调试：调试端口、当前状态（停在哪一行）
 *  - Java 调试：JDWP 主机/端口、**启动参数一键复制**
 *  - 实时调试状态：命中位置、调用栈、变量（来自 debug.paused 事件与状态查询）
 *
 * 用户可见性：这里展示的"已暂停：文件:行号 + 调用栈 + 变量"正是需求里
 * "能看到断点进行到哪一行代码"的落点；AI 侧的调试过程也会同步反映在此。
 */
import { useCallback, useEffect, useState } from "react";
import { api, type DebugStatusOut, type McpServerOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { Input, Switch } from "../ui";
import { IconBug, IconRefresh, IconTerminal } from "../icons";

const JDWP_HINT = (port: number) =>
  `-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:${port}`;

export function DebuggerPanel({ server }: { server?: McpServerOut }) {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const [webPort, setWebPort] = useState(9222);
  const [host, setHost] = useState("127.0.0.1");
  const [javaPort, setJavaPort] = useState(5005);
  /** 已落库的配置快照——与当前输入比对得出"是否有未保存修改"（修“改了不保存”）。 */
  const [savedCfg, setSavedCfg] = useState({ web_port: 9222, jdwp_host: "127.0.0.1", jdwp_port: 5005 });
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [webStatus, setWebStatus] = useState<DebugStatusOut | null>(null);
  const [javaStatus, setJavaStatus] = useState<DebugStatusOut | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  /** 进入面板即读回已保存配置（此前是硬编码默认值，改完重进就丢） */
  useEffect(() => {
    void api.debugSettings().then((s) => {
      setWebPort(s.web_port);
      setHost(s.jdwp_host);
      setJavaPort(s.jdwp_port);
      setSavedCfg({ web_port: s.web_port, jdwp_host: s.jdwp_host, jdwp_port: s.jdwp_port });
      setSavedAt(s.saved ? (s.updated_at ?? null) : null);
    }).catch(() => { /* 读不到就用默认值，不阻断面板 */ });
  }, []);

  const dirty = webPort !== savedCfg.web_port || host !== savedCfg.jdwp_host
    || javaPort !== savedCfg.jdwp_port;

  /** 保存到数据库（服务端校验端口范围；非法值会被忽略并返回原值） */
  const save = async () => {
    setSaving(true);
    setSaveMsg(null);
    try {
      const res = await api.saveDebugSettings({ web_port: webPort, jdwp_host: host, jdwp_port: javaPort });
      setWebPort(res.web_port);
      setHost(res.jdwp_host);
      setJavaPort(res.jdwp_port);
      setSavedCfg({ web_port: res.web_port, jdwp_host: res.jdwp_host, jdwp_port: res.jdwp_port });
      setSavedAt(res.updated_at ?? null);
      setSaveMsg("已保存到数据库");
    } catch (e) {
      useChatStore.setState({ error: `保存调试配置失败：${String(e)}` });
      setSaveMsg("保存失败，请重试");
    } finally { setSaving(false); }
  };

  /** 状态查询（会话存在时才有意义） */
  const refresh = useCallback(async () => {
    if (currentSessionId == null) return;
    setBusy(true);
    try {
      const [w, j] = await Promise.all([
        api.debugStatus(currentSessionId, "web").catch(() => null),
        api.debugStatus(currentSessionId, "java").catch(() => null),
      ]);
      setWebStatus(w);
      setJavaStatus(j);
    } finally { setBusy(false); }
  }, [currentSessionId]);
  useEffect(() => { void refresh(); }, [refresh]);

  /** 监听 debug.paused：AI 调试命中时，此面板实时更新（用户可见性的关键） */
  useEffect(() => {
    const onPaused = (e: Event) => {
      const detail = (e as CustomEvent).detail as { target?: string } | undefined;
      void refresh();
      void detail;
    };
    window.addEventListener("chatcoder:debug-paused", onPaused);
    return () => window.removeEventListener("chatcoder:debug-paused", onPaused);
  }, [refresh]);

  const toggleServer = async (v: boolean) => {
    if (!server) return;
    try {
      await api.updateMcpServer(server.id, { is_active: v });
      await useChatStore.getState().loadBootstrap();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
  };

  const copyJdwp = async () => {
    try {
      await navigator.clipboard.writeText(JDWP_HINT(javaPort));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch { /* 剪贴板不可用则忽略 */ }
  };

  const act = async (target: "web" | "java", action: string, extra: Record<string, unknown> = {}) => {
    if (currentSessionId == null) return;
    try {
      await api.debugAction(target, action, currentSessionId, extra);
      await refresh();
    } catch (e) {
      useChatStore.setState({ error: `调试操作失败：${String(e)}` });
    }
  };

  const renderStatus = (label: string, st: DebugStatusOut | null, target: "web" | "java") => (
    <div className="dbg-status">
      <div className="dbg-status-head">
        <span className={`dbg-dot${st?.paused ? " paused" : st?.connected ? " live" : ""}`} />
        <span className="dbg-status-title">{label}</span>
        <span className="dbg-status-desc">
          {!st?.connected ? "未连接" : st.paused ? "已暂停" : "运行中"}
          {st?.connected ? ` · 断点 ${st.breakpoints} · 命中 ${st.hitCount} 次` : ""}
        </span>
      </div>
      {st?.connected && st.line != null && (
        <div className="dbg-location">
          已暂停：<code>{st.file || "(未知文件)"}:{st.line}</code>
          {st.function ? <span className="dbg-fn">（{st.function}）</span> : null}
        </div>
      )}
      {st?.stack && st.stack.length > 0 && (
        <div className="dbg-block">
          <div className="dbg-block-title">调用栈</div>
          {st.stack.slice(0, 8).map((f, i) => (
            <div className="dbg-stack-row" key={i}>
              <span className="dbg-stack-idx">#{i}</span>
              <span className="dbg-stack-fn">{f.function || "(anonymous)"}</span>
              <span className="dbg-stack-loc">{f.url || ""}{f.line != null ? `:${f.line}` : ""}</span>
            </div>
          ))}
        </div>
      )}
      {st?.variables && st.variables.length > 0 && (
        <div className="dbg-block">
          <div className="dbg-block-title">变量（{st.variables.length}）</div>
          {st.variables.slice(0, 20).map((v, i) => (
            <div className="dbg-var-row" key={i}>
              <code>{v.name}</code>
              <span>= {String(v.value ?? "")}</span>
              {v.type ? <span className="dbg-var-type">{v.type}</span> : null}
            </div>
          ))}
        </div>
      )}
      {st?.connected && (
        <div className="dbg-actions">
          <button className="btn btn-ghost btn-xs" onClick={() => void act(target, "resume")}>继续</button>
          {target === "web" && (
            <>
              <button className="btn btn-ghost btn-xs" onClick={() => void act(target, "step", { action: "over" })}>跳过</button>
              <button className="btn btn-ghost btn-xs" onClick={() => void act(target, "step", { action: "into" })}>步入</button>
              <button className="btn btn-ghost btn-xs" onClick={() => void act(target, "step", { action: "out" })}>跳出</button>
            </>
          )}
          <button className="btn btn-ghost btn-xs danger" onClick={() => void act(target, "stop")}>停止调试</button>
        </div>
      )}
    </div>
  );

  return (
    <div className="dbg-panel">
      {server && (
        <section className="db-section db-master">
          <div className="db-master-info">
            <div className="db-master-title"><IconBug size={14} /> {server.display_name || server.name}</div>
            <div className="db-master-desc">
              {server.description || "让 AI 调用接口、在代码中设断点读取现场数据。"}
            </div>
          </div>
          <Switch checked={server.is_active} onChange={(v) => void toggleServer(v)} />
        </section>
      )}

      <section className="db-section">
        <div className="db-section-head">
          <span>Web 前端调试</span>
          <button className="btn btn-ghost btn-xs" onClick={() => void refresh()} disabled={busy}>
            <IconRefresh size={12} /> 刷新状态
          </button>
        </div>
        <div className="dbg-hint">
          需先让浏览器开启调试端口（Chrome / Edge 加启动参数
          <code>--remote-debugging-port={webPort}</code>，或用内置浏览器面板），
          然后让 AI 通过 <code>/</code> 调用本连接器。
        </div>
        <div className="db-row">
          <span className="db-label">调试端口</span>
          <Input type="number" value={String(webPort)} className="db-num"
            onChange={(e) => setWebPort(Number(e.target.value) || 9222)} aria-label="Web 调试端口" />
        </div>
        {renderStatus("Web（CDP）", webStatus, "web")}
      </section>

      <section className="db-section">
        <div className="db-section-head"><span>Java 调试</span></div>
        <div className="dbg-hint">
          以调试模式启动 JVM（下方参数），再让 AI 附加并下断点。
        </div>
        <div className="db-row">
          <span className="db-label">JDWP 参数</span>
          <code className="dbg-jdwp">{JDWP_HINT(javaPort)}</code>
          <button className="btn btn-ghost btn-xs" onClick={() => void copyJdwp()}>
            <IconTerminal size={12} /> {copied ? "已复制" : "复制"}
          </button>
        </div>
        <div className="db-row">
          <span className="db-label">主机 / 端口</span>
          <Input value={host} className="db-host" onChange={(e) => setHost(e.target.value)} aria-label="JDWP 主机" />
          <Input type="number" value={String(javaPort)} className="db-num"
            onChange={(e) => setJavaPort(Number(e.target.value) || 5005)} aria-label="JDWP 端口" />
        </div>
        {renderStatus("Java（JDWP）", javaStatus, "java")}
      </section>

      {/* 保存条：配置落库（此前这些输入只存在组件 state 里，重进面板即丢） */}
      <div className="dbg-savebar">
        <span className={`dbg-savebar-hint${dirty ? " dirty" : ""}`}>
          {saving
            ? "保存中…"
            : dirty
            ? "有未保存的修改"
            : saveMsg || (savedAt ? `已保存（${savedAt}）` : "配置会保存到数据库，重新进入面板自动读回")}
        </span>
        <button className="btn btn-primary btn-sm" onClick={() => void save()}
          disabled={!dirty || saving}>
          保存
        </button>
      </div>
    </div>
  );
}
