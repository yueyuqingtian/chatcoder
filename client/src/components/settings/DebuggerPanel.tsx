/** DebuggerPanel —— 开发调试配置（plan-282-1441 #8）。
 *
 * 作为内置 MCP「开发调试」的详情页，嵌入「拓展 → 连接器」的配置弹窗中：
 *  - 内置 MCP 启用开关（默认关闭）
 *  - Web 前端调试：调试端口、当前状态（停在哪一行）
 *  - Java 调试：JDWP 主机/端口、**启动参数一键复制**
 *  - 实时调试状态：命中位置、调用栈、变量（来自 debug.paused 事件与状态查询）
 *
 * 用户可见性：这里展示的"已暂停：文件:行号 + 调用栈 + 变量"正是需求里
 * "能看到断点进行到哪一行代码"的落点；AI 侧的调试过程也会同步反映在此。
 *
 * 排版（plan-20261017-goal12 项 10）：两个分组（Web / Java）各自是分组卡，端口走两列栅格，
 * 调试状态作为卡内子区块展示；外壳见 ConnectorShell。
 */
import { useCallback, useEffect, useState } from "react";
import { api, type DebugStatusOut, type McpServerOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { Button, DialogSection, Field, InsetCard, InsetRow, Input, Tooltip } from "../ui";
import { IconBug, IconRefresh, IconTerminal } from "../icons";
import { ConnectorMaster } from "./ConnectorShell";

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

  /** 修改即保存（300ms 防抖）：纯端口配置不需要显式保存按钮（设置中心统一规范，见 CLAUDE.md §2） */
  useEffect(() => {
    if (!dirty) return;
    const timer = window.setTimeout(() => { void save(); }, 300);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- save 每次渲染重建，仅依据配置值触发
  }, [dirty, webPort, host, javaPort]);

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
    <div className={`cx-status${st?.connected ? "" : " is-idle"}`}>
      <div className="cx-status-head">
        <span className={`dbg-dot${st?.paused ? " paused" : st?.connected ? " live" : ""}`} />
        <span className="cx-status-title">{label}</span>
        <span className="cx-status-desc">
          {!st?.connected
            ? (currentSessionId == null ? "在会话中可查看实时状态" : "未连接")
            : `${st.paused ? "已暂停" : "运行中"} · 断点 ${st.breakpoints} · 命中 ${st.hitCount} 次`}
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
              <span className="cx-var-value">= {String(v.value ?? "")}</span>
              {v.type ? <span className="dbg-var-type">{v.type}</span> : null}
            </div>
          ))}
        </div>
      )}
      {st?.connected && (
        <div className="cx-status-actions">
          <Button variant="ghost" size="sm" onClick={() => void act(target, "resume")}>继续</Button>
          {target === "web" && (
            <>
              <Button variant="ghost" size="sm" onClick={() => void act(target, "step", { action: "over" })}>跳过</Button>
              <Button variant="ghost" size="sm" onClick={() => void act(target, "step", { action: "into" })}>步入</Button>
              <Button variant="ghost" size="sm" onClick={() => void act(target, "step", { action: "out" })}>跳出</Button>
            </>
          )}
          <Button variant="danger-ghost" size="sm" onClick={() => void act(target, "stop")}>停止调试</Button>
        </div>
      )}
    </div>
  );

  return (
    <div className="cx-panel">
      {server && (
        <ConnectorMaster
          icon={<IconBug size={16} />}
          title={server.display_name || server.name}
          desc={server.description || "让 AI 调用接口、在代码中设断点读取现场数据。"}
          checked={server.is_active}
          onChange={(v) => void toggleServer(v)}
        />
      )}

      <DialogSection
        title="Web 前端调试"
        aside={(
          <Tooltip content="重新读取调试状态" side="top">
            <Button variant="ghost" size="sm" icon={<IconRefresh size={13} />} onClick={() => void refresh()} disabled={busy}>
              刷新状态
            </Button>
          </Tooltip>
        )}
      >
        <InsetCard>
          <InsetRow
            title="调试端口"
            desc={`浏览器需开启远程调试端口（Chrome / Edge 启动参数 --remote-debugging-port=${webPort}，或使用内置浏览器面板）`}
            actions={(
              <Input type="number" value={String(webPort)} className="cx-port"
                onChange={(e) => setWebPort(Number(e.target.value) || 9222)} aria-label="Web 调试端口" />
            )}
          />
          <div className="cx-block">{renderStatus("Web（CDP）", webStatus, "web")}</div>
        </InsetCard>
      </DialogSection>

      <DialogSection title="Java 调试" aside="以调试模式启动 JVM 后附加">
        <InsetCard>
          <div className="cx-jdwp">
            <div className="cx-jdwp-head">
              <span className="cx-jdwp-title">JDWP 启动参数</span>
              <Button variant="ghost" size="sm" icon={<IconTerminal size={13} />} onClick={() => void copyJdwp()}>
                {copied ? "已复制" : "复制"}
              </Button>
            </div>
            <code className="cx-jdwp-code">{JDWP_HINT(javaPort)}</code>
          </div>
          <div className="cx-form-grid is-address cx-block">
            <Field label="主机">
              <Input value={host} aria-label="JDWP 主机" onChange={(e) => setHost(e.target.value)} />
            </Field>
            <Field label="端口">
              <Input type="number" value={String(javaPort)} aria-label="JDWP 端口"
                onChange={(e) => setJavaPort(Number(e.target.value) || 5005)} />
            </Field>
          </div>
          <div className="cx-block">{renderStatus("Java（JDWP）", javaStatus, "java")}</div>
        </InsetCard>
      </DialogSection>

      {/* 保存状态提示：修改即保存，这里只反馈结果（配置落库后重新进入自动读回） */}
      <div className="settings-resource-desc" aria-live="polite">
        {saving ? "保存中…" : saveMsg || (savedAt ? `已保存（${savedAt}）` : "修改即保存，配置会写入数据库，重新进入自动读回")}
      </div>
    </div>
  );
}
