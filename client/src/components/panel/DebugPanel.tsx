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

/** plan-308-1542 需求7-A：断点明细区块（用户要求"可见断点的情况"）。
 *  数据来源：store.debugState[target].breakpointList（debug.paused / breakpoints_changed 广播与主动刷新共同维护）。 */
function BreakpointList({ label, target, rows, onRemove, onClear }: {
  label: string;
  target: "web" | "java";
  rows: Array<{ id: string; file?: string | null; line?: number | null; class?: string | null; source?: string }>;
  onRemove: (id: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="dbg-bps">
      <div className="dbg-bps-head">
        <span className="dbgp-sec-title">{label}（{rows.length}）</span>
        {rows.length > 0 && (
          <button className="btn btn-ghost btn-xs" onClick={onClear} title="清空该会话全部断点">
            全部清除
          </button>
        )}
      </div>
      {rows.length === 0 ? (
        <div className="dbgp-empty">当前无{label}断点。</div>
      ) : (
        rows.map((b) => (
          <div className="dbg-bp-row" key={`${target}-${b.id}`}>
            <span className={`dbg-bp-src${b.source === "idea" ? " is-idea" : ""}`}>
              {b.source === "idea" ? "IDEA" : "本软件"}
            </span>
            <code className="dbg-bp-loc" title={b.file || b.class || ""}>
              {(b.file || b.class || "?")}{b.line != null ? `:${b.line}` : ""}
            </code>
            <button className="dbg-bp-del" onClick={() => onRemove(b.id)} title="删除该断点">
              删除
            </button>
          </div>
        ))
      )}
    </div>
  );
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
    /* plan-17-78（D3）：暂停时才需要高亮块承载调用栈/变量；仅连接的运行态用紧凑行，
       避免在 360px 面板里铺出一块空荡灰底。 */
    <div className={`dbg-status${status.paused ? "" : " is-compact"}`}>
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

export function DebugPanel({ sessionId }: {
  /** plan-41-233：标签所属会话（undefined=跟随当前会话）——保活渲染下 Arthas 操作
   *  必须落到标签所属会话，不再读全局 currentSessionId。 */
  sessionId?: number | null;
} = {}) {
  const followedSessionId = useChatStore((s) => s.currentSessionId);
  const currentSessionId = sessionId !== undefined ? sessionId : followedSessionId;
  const debugState = useChatStore((s) => s.debugState);
  const arthasState = useChatStore((s) => s.arthasState);
  // plan-308-1542 需求7-B：IDEA 联动（工程路径取当前项目 path）
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const projects = useChatStore((s) => s.projects);
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

  // plan-308-1542 需求7-A：断点明细列表（本地态 + store 广播双源）。
  const [bps, setBps] = useState<{ web: Array<{ id: string; file?: string | null; line?: number | null; source?: string }>; java: Array<{ id: string; class?: string | null; line?: number | null; source?: string }> }>({ web: [], java: [] });
  const bpsWeb = bps.web;
  const bpsJava = bps.java;

  const refreshBreakpoints = useCallback(async () => {
    if (currentSessionId == null) return;
    try {
      const [w, j] = await Promise.all([
        api.debugBreakpoints(currentSessionId, "web").catch(() => ({ breakpoints: [] as never[] })),
        api.debugBreakpoints(currentSessionId, "java").catch(() => ({ breakpoints: [] as never[] })),
      ]);
      setBps({
        web: (w.breakpoints || []) as never,
        java: (j.breakpoints || []) as never,
      });
    } catch { /* 未连接时静默 */ }
  }, [currentSessionId]);

  // 进入面板与调试事件到达时刷新断点列表
  useEffect(() => { void refreshBreakpoints(); }, [refreshBreakpoints]);
  useEffect(() => {
    const onEvt = () => { void refreshBreakpoints(); };
    window.addEventListener("chatcoder:debug-paused", onEvt);
    return () => window.removeEventListener("chatcoder:debug-paused", onEvt);
  }, [refreshBreakpoints]);

  /** 删除单个断点（失败在面板内联提示）。 */
  const removeBp = async (target: "web" | "java", id: string) => {
    if (currentSessionId == null) return;
    try {
      const res = await api.debugRemoveBreakpoint(currentSessionId, target, id);
      if (!res.ok) setNotice(res.error || "删除断点失败");
      await refreshBreakpoints();
    } catch (e) {
      setNotice(String(e));
    }
  };

  /** 清空某会话全部断点。 */
  const clearBps = async (target: "web" | "java") => {
    if (currentSessionId == null) return;
    try {
      await api.debugClearBreakpoints(currentSessionId, target);
      await refreshBreakpoints();
    } catch (e) {
      setNotice(String(e));
    }
  };

  // ── plan-308-1542 需求7-B：IntelliJ IDEA 双向断点通道 ──
  // 读 IDEA 断点（workspace.xml）→ 面板可见；一键把 IDEA 断点转为 Arthas 观测
  // （IDEA 调试独占 JDWP，但 Arthas 走 Attach API 可并存，命中即本软件可见）。
  const projectPath = useMemo(() => {
    const p = projects.find((x) => x.id === currentProjectId);
    return p?.path ?? "";
  }, [projects, currentProjectId]);
  const [ideaBps, setIdeaBps] = useState<Array<{ id: string; file: string; line: number | null; enabled?: boolean }>>([]);
  const [ideaNote, setIdeaNote] = useState<string>("");
  const [ideaSessions, setIdeaSessions] = useState<Array<{ pid: number; main_class: string; jdwp_port: number }>>([]);
  const [ideaRunning, setIdeaRunning] = useState(false);

  const loadIdea = useCallback(async () => {
    if (!projectPath) return;
    try {
      const res = await api.ideaBreakpoints(projectPath);
      setIdeaBps(res.breakpoints || []);
      if (res.available === false) {
        setIdeaNote(res.reason || "未找到 IDEA 工程配置（.idea/workspace.xml）");
      } else {
        setIdeaNote("");
      }
      const sess = await api.ideaDebugSession(projectPath).catch(() => null);
      if (sess) {
        setIdeaRunning(sess.idea_running);
        setIdeaSessions(sess.sessions || []);
      }
    } catch { /* 非阻塞：无 IDEA 工程时静默 */ }
  }, [projectPath]);

  useEffect(() => { void loadIdea(); }, [loadIdea]);

  /** 为某个 IDEA 断点建立 Arthas 方法级观测（命中即本软件可见）。 */
  const watchIdeaBp = async (bp: { file: string; line: number | null }) => {
    if (!projectPath || bp.line == null) return;
    setNotice(null);
    try {
      const m = await api.ideaMethodAtLine(projectPath, bp.file, bp.line);
      if (!m.ok || !m.target) {
        setNotice(m.error || "未能识别该方法，请手动指定 class#method");
        return;
      }
      const [cls, method] = m.target.split("#");
      // 未 attach 时先提示用户 attach（Arthas 会话按 PID 建立）
      if (!attached) {
        setNotice(`已识别 ${m.target}；请先「扫描本机 Java 进程」并 Attach，再建立观测`);
        return;
      }
      // Arthas 观测命令：watch（命中即上报，可与 IDEA 调试并存）
      await api.arthasExec(currentSessionId as number,
        `watch ${cls} ${method} '{params, returnObj, throwable}' -x 2`);
      setNotice(`已为 ${m.target} 建立 Arthas 观测：IDEA 命中断点时会显示在「观测命中」`);
    } catch (e) {
      setNotice(String(e));
    }
  };

  return (
    <div className="dbgp">
      <section className="db-section">
        <div className="db-section-head">
          <span className="dbgp-sec-title"><IconBug size={13} /> Arthas 现场诊断</span>
          <button className="btn btn-ghost btn-xs" onClick={() => void refresh(true)} disabled={!!busy}>
            <IconRefresh size={12} /> 刷新
          </button>
        </div>
        <div className="dbg-hint">
          走 Attach API，<b>可与 IDEA 的调试并存</b>（IDEA 占用 JDWP 时 JDWP 调试连不上，Arthas 不受影响）。
          用 <code>/</code> 引用「开发调试」连接器，让 AI 调 <code>java_list_processes</code> →{" "}
          <code>java_attach_process</code> → <code>arthas_watch</code> 即可观测方法现场。
        </div>

        <div className={`dbg-status${attached ? "" : " is-idle"}`}>
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
          <span className="dbgp-sec-title">观测命中（{entries.length}）</span>
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
        <div className="db-section-head"><span className="dbgp-sec-title">断点会话</span></div>
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

      {/* plan-308-1542 需求7-A：断点明细（用户要求"让用户可见断点的情况"）。
          此前面板只显示断点数量，看不到具体断在哪个文件哪一行，也无法逐条删除。 */}
      <section className="db-section">
        <div className="db-section-head">
          <span className="dbgp-sec-title">断点列表</span>
          <button className="btn btn-ghost btn-xs" onClick={() => void refreshBreakpoints()} disabled={!!busy}>
            <IconRefresh size={12} /> 刷新
          </button>
        </div>
        <BreakpointList label="Web（CDP）" target="web"
          rows={bpsWeb}
          onRemove={(id) => void removeBp("web", id)}
          onClear={() => void clearBps("web")} />
        <BreakpointList label="Java（JDWP）" target="java"
          rows={bpsJava}
          onRemove={(id) => void removeBp("java", id)}
          onClear={() => void clearBps("java")} />
      </section>

      {/* plan-308-1542 需求7-B：与 IntelliJ IDEA 的双向断点通道。
          - 读：展示 IDEA 工程内已配置的断点（workspace.xml）
          - 联动：一键由「文件:行」推导 class#method 并建立 Arthas 观测
            （IDEA 调试独占 JDWP，但 Arthas 走 Attach API 可并存 → 命中即本软件可见） */}
      <section className="db-section">
        <div className="db-section-head">
          <span className="dbgp-sec-title">IDEA 联动</span>
          <button className="btn btn-ghost btn-xs" onClick={() => void loadIdea()} disabled={!projectPath}>
            <IconRefresh size={12} /> 刷新
          </button>
        </div>
        {!projectPath ? (
          <div className="dbgp-empty">未选择项目，无法读取 IDEA 配置。</div>
        ) : (
          <>
            <div className="dbg-idea-head">
              <span className={`dbg-bp-src${ideaRunning ? " is-idea" : ""}`}>
                {ideaRunning ? "IDEA 运行中" : "IDEA 未运行"}
              </span>
              {ideaSessions.length > 0 && (
                <span className="dbgp-dim">
                  JDWP 调试会话 {ideaSessions.length} 个（通道被占用 → 观测走 Arthas）
                </span>
              )}
            </div>
            {ideaNote && <div className="dbgp-notice">{ideaNote}</div>}
            {ideaBps.length === 0 ? (
              <div className="dbgp-empty">
                IDEA 工程内暂无断点（或未在 IDEA 中打开过该工程）。
              </div>
            ) : (
              ideaBps.map((b) => (
                <div className="dbg-bp-row" key={b.id}>
                  <span className="dbg-bp-src is-idea">IDEA</span>
                  <code className="dbg-bp-loc" title={b.file}>{b.file}:{b.line}</code>
                  <button className="dbg-bp-del" onClick={() => void watchIdeaBp(b)}
                    title="由该断点推导方法并建立 Arthas 观测（IDEA 命中即可在本软件看到）">
                    建立观测
                  </button>
                </div>
              ))
            )}
          </>
        )}
      </section>
    </div>
  );
}
