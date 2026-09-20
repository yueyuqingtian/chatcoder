/** DebugPanel —— 右侧面板「调试」（plan-282-1441 #8 的 Arthas 落地）。
 *
 * 用户在 AI 调试期间的观察窗口（需求：AI 调试时面板要做好可视化）：
 *  - Arthas 会话：PID / HTTP 端口 / 版本 / 空闲时长（可与 IDEA 调试并存）
 *  - 本机 Java 进程列表：点一下即可 attach（AI 走 java_attach_process 做同样的事）
 *  - 观测命中流水：watch/trace 命中的时间、类#方法、耗时与现场值（Arthas 渲染好的文本）
 *  - Web(CDP) / Java(JDWP) 断点会话：停在哪一行 + 调用栈 + 变量 + 继续/步进
 *
 * 数据来源：
 *  - 实时事件 store.arthasState / store.debugState（由 arthas.event / debug.paused 广播写入）
 *  - 主动查询 /api/debug/arthas/*（状态、进程列表、配置探测）
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ArthasConfigOut, type ArthasProcessOut, type ArthasStatusOut, type DebugStatusOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconBug, IconPlay, IconRefresh, IconStop } from "../icons";

function fmtTs(ts?: number | string | null): string {
  if (ts == null) return "";
  if (typeof ts === "string") return ts;
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return String(ts);
  }
}

/** Web/Java 断点会话状态块（含继续/步进控制） */
function BreakpointStatus({ label, status, target, onAct }: {
  label: string;
  status: DebugStatusOut | null | undefined;
  target: "web" | "java";
  onAct: (action: string, extra?: Record<string, unknown>) => void;
}) {
  if (!status?.connected) return null;
  return (
    <div className="dbg-status">
      <div className="dbg-status-head">
        <span className={`dbg-dot${status.paused ? " paused" : " live"}`} />
        <span className="dbg-status-title">{label}</span>
        <span className="dbg-status-desc">
          {status.paused ? "已暂停" : "运行中"} · 断点 {status.breakpoints} · 命中 {status.hitCount}
        </span>
      </div>
      {status.paused && status.line != null && (
        <div className="dbg-location">
          已暂停：<code>{status.file || "(未知文件)"}:{status.line}</code>
          {status.function ? <span className="dbg-fn">（{status.function}）</span> : null}
        </div>
      )}
      {status.stack?.length > 0 && (
        <div className="dbg-block">
          <div className="dbg-block-title">调用栈</div>
          {status.stack.slice(0, 6).map((f, i) => (
            <div className="dbg-stack-row" key={i}>
              <span className="dbg-stack-idx">#{i}</span>
              <span className="dbg-stack-fn">{f.function || "(anonymous)"}</span>
              <span className="dbg-stack-loc">{f.url || ""}{f.line != null ? `:${f.line}` : ""}</span>
            </div>
          ))}
        </div>
      )}
      {status.variables?.length > 0 && (
        <div className="dbg-block">
          <div className="dbg-block-title">变量（{status.variables.length}）</div>
          {status.variables.slice(0, 12).map((v, i) => (
            <div className="dbg-var-row" key={i}>
              <code>{v.name}</code>
              <span>= {String(v.value ?? "")}</span>
            </div>
          ))}
        </div>
      )}
      <div className="dbg-actions">
        <button className="btn btn-ghost btn-xs" onClick={() => onAct("resume")}>
          <IconPlay size={11} /> 继续
        </button>
        {target === "web" && (
          <>
            <button className="btn btn-ghost btn-xs" onClick={() => onAct("step", { action: "over" })}>跳过</button>
            <button className="btn btn-ghost btn-xs" onClick={() => onAct("step", { action: "into" })}>步入</button>
            <button className="btn btn-ghost btn-xs" onClick={() => onAct("step", { action: "out" })}>跳出</button>
          </>
        )}
        <button className="btn btn-ghost btn-xs danger" onClick={() => onAct("stop")}>
          <IconStop size={11} /> 停止
        </button>
      </div>
    </div>
  );
}

export function DebugPanel() {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const debugState = useChatStore((s) => s.debugState);
  const arthasState = useChatStore((s) => s.arthasState);
  const [status, setStatus] = useState<ArthasStatusOut | null>(null);
  const [procs, setProcs] = useState<ArthasProcessOut[] | null>(null);
  const [cfg, setCfg] = useState<ArthasConfigOut | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const attached = status?.attached ?? arthasState?.attached ?? false;
  const port = status?.http_port ?? arthasState?.http_port ?? null;
  const pid = status?.pid ?? arthasState?.pid ?? null;
  const entries = useMemo(() => arthasState?.entries ?? [], [arthasState]);

  const refresh = useCallback(async (withProcs = false) => {
    if (currentSessionId == null) return;
    try {
      const st = await api.arthasStatus(currentSessionId);
      setStatus(st);
      if (st.attached && withProcs) setProcs(null);
    } catch { /* 未连接/网络异常不打断 UI */ }
  }, [currentSessionId]);

  /** 配置与环境探测（只在挂载时取一次：面板顶部要告诉用户"JDK 是否就绪"） */
  useEffect(() => {
    void api.arthasConfig().then(setCfg).catch(() => setCfg(null));
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  /** 广播事件到达时刷新状态（AI 在别处 attach/断开也能同步） */
  useEffect(() => {
    const onEvent = () => { void refresh(); };
    window.addEventListener("chatcoder:arthas-event", onEvent);
    window.addEventListener("chatcoder:debug-paused", onEvent);
    return () => {
      window.removeEventListener("chatcoder:arthas-event", onEvent);
      window.removeEventListener("chatcoder:debug-paused", onEvent);
    };
  }, [refresh]);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setNotice(null);
    try {
      await fn();
      await refresh(true);
    } catch (e) {
      setNotice(String(e));
    } finally {
      setBusy("");
    }
  };

  const loadProcs = () => act("procs", async () => {
    if (currentSessionId == null) return;
    const data = await api.arthasProcesses(currentSessionId, cfg?.java_home || undefined);
    setProcs(data.processes || []);
    if (!data.ok && data.error) setNotice(data.error);
  });

  const attach = (p: ArthasProcessOut) => act(`attach-${p.pid}`, async () => {
    if (currentSessionId == null) return;
    await api.arthasAttach(currentSessionId, p.pid);
  });

  const detach = () => act("stop", async () => {
    if (currentSessionId == null) return;
    const res = await api.arthasStop(currentSessionId);
    setNotice(res.message || "已断开");
  });

  const bpAct = (target: "web" | "java", action: string, extra: Record<string, unknown> = {}) => {
    void act(`${target}-${action}`, async () => {
      if (currentSessionId == null) return;
      await api.debugAction(target, action, currentSessionId, extra);
    });
  };

  return (
    <div className="dbgp">
      <section className="db-section">
        <div className="db-section-head">
          <span><IconBug size={13} /> Arthas 现场诊断</span>
          <button className="btn btn-ghost btn-xs" onClick={() => void refresh(true)} disabled={!!busy}>
            <IconRefresh size={12} /> 刷新
          </button>
        </div>
        <div className="dbg-hint">
          走 Attach API，**可与 IDEA 的调试并存**（IDEA 占用 JDWP 时 JDWP 调试连不上，Arthas 不受影响）。
          用 <code>/</code> 引用「开发调试」连接器，让 AI 调 <code>java_list_processes</code> →{" "}
          <code>java_attach_process</code> → <code>arthas_watch</code> 即可观测方法现场。
        </div>

        <div className="dbg-status">
          <div className="dbg-status-head">
            <span className={`dbg-dot${attached ? " live" : ""}`} />
            <span className="dbg-status-title">会话</span>
            <span className="dbg-status-desc">
              {attached
                ? `已连接 PID ${pid ?? "?"} · HTTP ${port ?? "?"} · Arthas ${status?.version || arthasState?.version || "?"}`
                : "未连接"}
              {attached && status?.main_class ? ` · ${status.main_class}` : ""}
            </span>
          </div>
          {attached ? (
            <div className="dbg-actions">
              <button className="btn btn-ghost btn-xs danger" onClick={() => void detach()} disabled={!!busy}>
                <IconStop size={11} /> 断开并还原增强类
              </button>
            </div>
          ) : (
            <div className="dbg-actions">
              <button className="btn btn-ghost btn-xs" onClick={() => void loadProcs()} disabled={!!busy}>
                {busy === "procs" ? "扫描中…" : "扫描本机 Java 进程"}
              </button>
            </div>
          )}
        </div>

        {procs && !attached && (
          procs.length === 0
            ? <div className="dbgp-empty">{notice || "没有发现可 attach 的 Java 进程。"}</div>
            : (
              <div className="dbgp-procs">
                {procs.map((p) => (
                  <div className="dbgp-proc" key={p.pid}>
                    <span className="dbgp-proc-pid">{p.pid}</span>
                    <span className="dbgp-proc-main" title={p.main_class}>{p.main_class}</span>
                    {p.debugging && <span className="dbgp-badge warn" title={`JDWP ${p.jdwp_address || ""}`}>调试中</span>}
                    <button className="btn btn-ghost btn-xs"
                      onClick={() => void attach(p)} disabled={!!busy}>
                      {busy === `attach-${p.pid}` ? "连接中…" : "Attach"}
                    </button>
                  </div>
                ))}
              </div>
            )
        )}

        {cfg && !attached && (
          <div className="dbgp-cfg">
            <div>JDK：<code>{cfg.java_home_resolved || "未找到"}</code>
              {cfg.java_home_source ? <span className="dbgp-dim">（{cfg.java_home_source}）</span> : null}</div>
            <div>Arthas 启动器：<code>{cfg.boot_jar_resolved || cfg.boot_jar_source}</code></div>
            <div className="dbgp-dim">
              端口段 8563-8599 · 空闲 {cfg.idle_timeout_sec ?? 900}s 自动回收 · 已禁用命令
              <code>{cfg.disabled_commands}</code>
            </div>
          </div>
        )}
        {notice && <div className="dbgp-notice">{notice}</div>}
      </section>

      <section className="db-section">
        <div className="db-section-head">
          <span>观测命中（{entries.length}）</span>
          {arthasState?.summary && <span className="dbgp-dim">{arthasState.summary}</span>}
        </div>
        {entries.length === 0 ? (
          <div className="dbgp-empty">
            尚未观测到命中。AI 用 <code>arthas_watch</code> 提交观测并触发业务后，
            命中记录（时间 / 类#方法 / 耗时 / 现场值）会实时出现在这里。
          </div>
        ) : (
          <div className="dbgp-entries">
            {entries.map((e, i) => (
              <div className="dbgp-entry" key={i}>
                <div className="dbgp-entry-head">
                  <span className="dbgp-entry-time">{fmtTs(e.ts)}</span>
                  <code className="dbgp-entry-target">{e.class}#{e.method}</code>
                  {e.access_point && <span className="dbgp-badge">{e.access_point}</span>}
                  {e.cost != null && <span className="dbgp-cost">{e.cost}ms</span>}
                </div>
                {e.value != null && <pre className="dbgp-entry-value">{String(e.value)}</pre>}
                {e.throwable != null && <pre className="dbgp-entry-throw">{String(e.throwable)}</pre>}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="db-section">
        <div className="db-section-head"><span>断点会话</span></div>
        {!debugState || (!debugState.web?.connected && !debugState.java?.connected) ? (
          <div className="dbgp-empty">
            暂无 Web(CDP) / Java(JDWP) 断点会话。
            <span className="dbgp-dim">（IDEA 调试期间 JDWP 通道被占用，这类真断点连不上，请改用 Arthas 观测）</span>
          </div>
        ) : (
          <>
            <BreakpointStatus label="Web（CDP）" status={debugState.web} target="web"
              onAct={(a, extra) => bpAct("web", a, extra)} />
            <BreakpointStatus label="Java（JDWP）" status={debugState.java} target="java"
              onAct={(a, extra) => bpAct("java", a, extra)} />
          </>
        )}
      </section>
    </div>
  );
}
