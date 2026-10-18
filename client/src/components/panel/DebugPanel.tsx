/** DebugPanel —— 右侧面板「调试」。
 *
 * 面板回答三个问题：现在连没连上目标进程、AI 或我正在观测什么、断点设在哪里。
 *  - 连接：Arthas 会话（PID / 端口 / 版本）与本机 Java 进程列表（可直接 Attach），可与 IDEA 调试并存；
 *  - 观测：进行中的观测作业（可中断）+ 命中流水（时间、类#方法、耗时、现场值）；
 *  - 断点：IDEA 工程断点（一键「观测」：由断点推导方法并建立持续观测）与本软件断点（Web / Java）；
 *  - 断点会话：Web(CDP) / Java(JDWP) 真断点连接时才出现（停在哪一行、调用栈、变量、继续/步进）。
 *
 * 数据来源：store.arthasState / store.debugState（实时广播），以及 /api/debug/* 主动查询。 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type ArthasConfigOut,
  type ArthasEntryOut,
  type ArthasProcessOut,
  type ArthasStatusOut,
  type DebugStatusOut,
} from "../../api/client";
import { useChatStore } from "../../store/chat";
import { Button } from "../ui";
import { IconBug, IconPlay, IconRefresh, IconStop } from "../icons";

/** 观测表达式：与服务端 _WATCH_DEFAULT_EXPRESS 保持一致。Arthas 的异常属性名是 throwExp，写成 throwable 会让 watch 立即失败。 */
const WATCH_EXPRESS = "{params, returnObj, throwExp}";

type BpRow = { id: string; file?: string | null; line?: number | null; class?: string | null; source?: string };
type IdeaBp = { id: string; file: string; line: number | null; enabled?: boolean };
type MethodRef = { cls: string; method: string; target: string };

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function fmtTs(ts?: number | string | null): string {
  if (ts == null) return "";
  if (typeof ts === "string") return ts;
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return String(ts);
  }
}

function baseName(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || path;
}

/** 从 `watch cls method ...` 解析目标方法：用来把观测作业与 IDEA 断点对应起来。 */
function parseWatchTarget(command: string | undefined): MethodRef | null {
  const m = /^watch\s+(\S+)\s+(\S+)/.exec((command || "").trim());
  return m ? { cls: m[1], method: m[2], target: `${m[1]}#${m[2]}` } : null;
}

function refFromTarget(target: string): MethodRef {
  const [cls, method = ""] = target.split("#");
  return { cls, method, target };
}

function bpKey(file: string, line: number): string {
  return `${file}:${line}`;
}

/** 一条观测命中：时间 / 类#方法 / 耗时 / 现场值（Arthas 渲染好的多行文本）。 */
function HitRow({ entry: e }: { entry: ArthasEntryOut }) {
  return (
    <li className="dbgp-hit">
      <div className="dbgp-hit-head">
        <span className="dbgp-hit-time">{fmtTs(e.ts)}</span>
        <code className="dbgp-hit-target">{e.class}#{e.method}</code>
        {e.access_point && <span className="dbgp-tag">{e.access_point}</span>}
        {e.cost != null && <span className="dbgp-hit-cost">{e.cost} ms</span>}
      </div>
      {e.value != null && <pre className="dbgp-hit-value">{String(e.value)}</pre>}
      {e.throwable != null && <pre className="dbgp-hit-value is-error">{String(e.throwable)}</pre>}
    </li>
  );
}

/** Web(CDP) / Java(JDWP) 真断点会话：暂停位置 + 调用栈 + 变量 + 继续/步进。 */
function SessionCard({ label, status, target, onAct }: {
  label: string;
  status: DebugStatusOut;
  target: "web" | "java";
  onAct: (action: string, extra?: Record<string, unknown>) => void;
}) {
  return (
    <div className="dbgp-session">
      <div className="dbgp-card-head">
        <span className={`dbg-dot${status.paused ? " paused" : " live"}`} aria-hidden />
        <span className="dbgp-card-title">{label}</span>
        <span className="dbgp-card-meta">
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
      <div className="dbgp-card-actions">
        <Button size="xs" variant="secondary" icon={<IconPlay size={11} />} onClick={() => onAct("resume")}>继续</Button>
        {target === "web" && (
          <>
            <Button size="xs" variant="ghost" onClick={() => onAct("step", { action: "over" })}>跳过</Button>
            <Button size="xs" variant="ghost" onClick={() => onAct("step", { action: "into" })}>步入</Button>
            <Button size="xs" variant="ghost" onClick={() => onAct("step", { action: "out" })}>跳出</Button>
          </>
        )}
        <Button size="xs" variant="danger-ghost" icon={<IconStop size={11} />} onClick={() => onAct("stop")}>停止</Button>
      </div>
    </div>
  );
}

export function DebugPanel({ sessionId }: {
  /** 标签所属会话（undefined=跟随当前会话）：保活渲染下 Arthas 操作必须落到标签所属会话。 */
  sessionId?: number | null;
} = {}) {
  const followedSessionId = useChatStore((s) => s.currentSessionId);
  const currentSessionId = sessionId !== undefined ? sessionId : followedSessionId;
  const debugState = useChatStore((s) => s.debugState);
  const arthasState = useChatStore((s) => s.arthasState);
  const clearArthasEntries = useChatStore((s) => s.clearArthasEntries);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const projects = useChatStore((s) => s.projects);

  const [status, setStatus] = useState<ArthasStatusOut | null>(null);
  const [procs, setProcs] = useState<ArthasProcessOut[] | null>(null);
  const [cfg, setCfg] = useState<ArthasConfigOut | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [bps, setBps] = useState<{ web: BpRow[]; java: BpRow[] }>({ web: [], java: [] });
  const [ideaBps, setIdeaBps] = useState<IdeaBp[]>([]);
  const [ideaNote, setIdeaNote] = useState("");
  const [ideaRunning, setIdeaRunning] = useState(false);
  const [ideaSessions, setIdeaSessions] = useState<Array<{ pid: number; main_class: string; jdwp_port: number }>>([]);
  /** IDEA 断点 → 所在方法（null=无法定位）。仅用于展示「观测中」与定位结果，点击「观测」时会重新解析。 */
  const [resolved, setResolved] = useState<Record<string, MethodRef | null>>({});

  const attached = status?.attached ?? arthasState?.attached ?? false;
  const port = status?.http_port ?? arthasState?.http_port ?? null;
  const pid = status?.pid ?? arthasState?.pid ?? null;
  const version = status?.version || arthasState?.version || null;
  const jobs = useMemo(() => status?.jobs ?? [], [status]);
  const entries = useMemo(() => arthasState?.entries ?? [], [arthasState]);
  /** 目标方法 → 作业 id：判断某个断点是否已有观测，并能直接中断它。 */
  const jobByTarget = useMemo(() => {
    const m = new Map<string, string | number>();
    for (const j of jobs) {
      const t = parseWatchTarget(j.command)?.target;
      if (t) m.set(t, j.job_id);
    }
    return m;
  }, [jobs]);

  const projectPath = useMemo(() => {
    const p = projects.find((x) => x.id === currentProjectId);
    return p?.path ?? "";
  }, [projects, currentProjectId]);

  const refresh = useCallback(async () => {
    if (currentSessionId == null) return;
    try {
      setStatus(await api.arthasStatus(currentSessionId));
    } catch { /* 未连接/网络异常不打断 UI */ }
  }, [currentSessionId]);

  const refreshBreakpoints = useCallback(async () => {
    if (currentSessionId == null) return;
    try {
      const [w, j] = await Promise.all([
        api.debugBreakpoints(currentSessionId, "web").catch(() => ({ breakpoints: [] as BpRow[] })),
        api.debugBreakpoints(currentSessionId, "java").catch(() => ({ breakpoints: [] as BpRow[] })),
      ]);
      setBps({ web: (w.breakpoints || []) as BpRow[], java: (j.breakpoints || []) as BpRow[] });
    } catch { /* 未连接时静默 */ }
  }, [currentSessionId]);

  const loadIdea = useCallback(async () => {
    if (!projectPath) return;
    try {
      const res = await api.ideaBreakpoints(projectPath);
      const list = (res.breakpoints || []) as IdeaBp[];
      setIdeaBps(list);
      setIdeaNote(res.available === false ? (res.reason || "未找到 IDEA 工程配置（.idea/workspace.xml）") : "");
      const sess = await api.ideaDebugSession(projectPath).catch(() => null);
      if (sess) {
        setIdeaRunning(sess.idea_running);
        setIdeaSessions(sess.sessions || []);
      }
      // 逐条解析所在方法（只读本地源码，成本很低）：让「观测中」状态在面板重新打开后仍然正确
      const pairs = await Promise.all(
        list.filter((b) => b.line != null).map(async (b) => {
          try {
            const m = await api.ideaMethodAtLine(projectPath, b.file, b.line as number);
            return [bpKey(b.file, b.line as number), m.ok && m.target ? refFromTarget(m.target) : null] as const;
          } catch {
            return null;
          }
        }),
      );
      const next: Record<string, MethodRef | null> = {};
      for (const p of pairs) if (p) next[p[0]] = p[1];
      setResolved(next);
    } catch { /* 非阻塞：无 IDEA 工程时静默 */ }
  }, [projectPath]);

  /** 配置与环境探测只在挂载时取一次：顶部要告诉用户"JDK 是否就绪"。 */
  useEffect(() => {
    void api.arthasConfig().then(setCfg).catch(() => setCfg(null));
  }, []);

  // 切换会话时清掉上一个会话的进程列表与提示
  useEffect(() => {
    setProcs(null);
    setNotice(null);
  }, [currentSessionId]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { void refreshBreakpoints(); }, [refreshBreakpoints]);
  useEffect(() => { void loadIdea(); }, [loadIdea]);

  /** 连接状态变化（attach / 断开 / 作业起止）才刷新状态。
   *  命中（observed / no_hit）是高频事件，由 store 直接写入流水，这里不逐条发请求。 */
  useEffect(() => {
    const onArthas = (e: Event) => {
      const phase = String((e as CustomEvent).detail?.phase || "");
      if (phase === "observed" || phase === "no_hit") return;
      void refresh();
    };
    const onPaused = () => {
      void refresh();
      void refreshBreakpoints();
    };
    window.addEventListener("chatcoder:arthas-event", onArthas);
    window.addEventListener("chatcoder:debug-paused", onPaused);
    return () => {
      window.removeEventListener("chatcoder:arthas-event", onArthas);
      window.removeEventListener("chatcoder:debug-paused", onPaused);
    };
  }, [refresh, refreshBreakpoints]);

  /** 统一的忙碌态包装：操作期间禁用相关按钮，失败在面板顶部提示。 */
  const act = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setNotice(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setNotice(errText(e));
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
    const res = await api.arthasAttach(currentSessionId, p.pid);
    if (!res.ok) throw new Error(String((res as { error?: unknown }).error ?? "Attach 失败"));
    setProcs(null);
  });

  const detach = () => act("stop", async () => {
    if (currentSessionId == null) return;
    const res = await api.arthasStop(currentSessionId);
    setNotice(res.message || "已断开");
  });

  const stopJob = (jobId: string | number) => act(`job-${jobId}`, async () => {
    if (currentSessionId == null) return;
    const res = await api.arthasInterrupt(currentSessionId, jobId);
    if (!res.ok) setNotice(res.error || "中断失败");
  });

  const bpAct = (target: "web" | "java", action: string, extra: Record<string, unknown> = {}) =>
    act(`${target}-${action}`, async () => {
      if (currentSessionId == null) return;
      await api.debugAction(target, action, currentSessionId, extra);
    });

  const removeBp = (target: "web" | "java", id: string) => act(`rm-${target}-${id}`, async () => {
    if (currentSessionId == null) return;
    const res = await api.debugRemoveBreakpoint(currentSessionId, target, id);
    if (!res.ok) setNotice(res.error || "删除断点失败");
    await refreshBreakpoints();
  });

  const clearBps = (target: "web" | "java") => act(`clear-${target}`, async () => {
    if (currentSessionId == null) return;
    await api.debugClearBreakpoints(currentSessionId, target);
    await refreshBreakpoints();
  });

  /** 对一个 IDEA 断点建立持续观测：解析所在方法 → watch 异步作业（命中由后台泵推到本面板）。 */
  const observeBp = (b: IdeaBp) => {
    if (!projectPath || currentSessionId == null || b.line == null) return;
    if (!attached) {
      setNotice("请先在「Arthas 连接」中扫描并 Attach 到目标 Java 进程，再建立观测。");
      return;
    }
    void act(`observe-${bpKey(b.file, b.line)}`, async () => {
      const m = await api.ideaMethodAtLine(projectPath, b.file, b.line as number);
      if (!m.ok || !m.target) {
        setNotice(m.error || "无法识别该断点所在的方法，请到「观测」里手动输入 watch 命令。");
        return;
      }
      const ref = refFromTarget(m.target);
      const res = await api.arthasExec(currentSessionId,
        `watch ${ref.cls} ${ref.method} '${WATCH_EXPRESS}' -x 2`, { observe: true });
      if (!res.ok) {
        setNotice(res.error || "建立观测失败");
        return;
      }
      setNotice(`已为 ${ref.target} 建立观测：该方法被调用时，结果会出现在「观测」的命中里。`);
    });
  };

  const sessionCards = (
    [
      { key: "web" as const, label: "Web（CDP）", status: debugState?.web },
      { key: "java" as const, label: "Java（JDWP）", status: debugState?.java },
    ]
  ).filter((c) => c.status?.connected);

  const localRows: Array<BpRow & { target: "web" | "java" }> = [
    ...bps.web.map((r) => ({ ...r, target: "web" as const })),
    ...bps.java.map((r) => ({ ...r, target: "java" as const })),
  ];

  return (
    <div className="dbgp">
      {notice && (
        <div className="dbgp-notice" role="status">
          <span className="dbgp-notice-text">{notice}</span>
          <Button size="xs" variant="ghost" onClick={() => setNotice(null)}>知道了</Button>
        </div>
      )}

      {/* ── 连接 ── */}
      <section className="dbgp-card">
        <div className="dbgp-card-head">
          <span className={`dbgp-dot${attached ? " is-live" : ""}`} aria-hidden />
          <span className="dbgp-card-title"><IconBug size={13} /> Arthas 连接</span>
          <span className="dbgp-card-meta">
            {attached ? `PID ${pid ?? "?"} · 端口 ${port ?? "?"} · v${version ?? "?"}` : "未连接"}
          </span>
          <div className="dbgp-card-actions">
            {attached ? (
              <Button size="xs" variant="danger-ghost" icon={<IconStop size={11} />}
                loading={busy === "stop"} disabled={!!busy} onClick={() => void detach()}>
                断开
              </Button>
            ) : (
              <Button size="xs" variant="secondary" loading={busy === "procs"}
                disabled={!!busy || currentSessionId == null} onClick={() => void loadProcs()}>
                扫描 Java 进程
              </Button>
            )}
            <Button size="xs" variant="ghost" iconOnly icon={<IconRefresh size={12} />}
              aria-label="刷新连接状态" disabled={!!busy} onClick={() => void refresh()} />
          </div>
        </div>
        {attached ? (
          status?.main_class ? (
            <div className="dbgp-status-line">主类 <code>{status.main_class}</code></div>
          ) : null
        ) : (
          <>
            <p className="dbgp-hint">
              通过 Attach API 连接目标 JVM；IDEA 占用 JDWP 时仍可用 Arthas 观测。
            </p>
            {procs && (procs.length === 0 ? (
              <div className="dbgp-empty">没有发现可连接的 Java 进程，请先启动目标应用。</div>
            ) : (
              <ul className="dbgp-procs">
                {procs.map((p) => (
                  <li className="dbgp-proc" key={p.pid}>
                    <span className="dbgp-proc-pid">{p.pid}</span>
                    <span className="dbgp-proc-main">{p.main_class || "(未知主类)"}</span>
                    {p.debugging && <span className="dbgp-tag is-warn">调试中</span>}
                    <Button size="xs" variant="secondary" loading={busy === `attach-${p.pid}`}
                      disabled={!!busy} onClick={() => void attach(p)}>
                      Attach
                    </Button>
                  </li>
                ))}
              </ul>
            ))}
            {cfg && (
              <details className="dbgp-env">
                <summary>运行环境</summary>
                <div>JDK：<code>{cfg.java_home_resolved || "未找到"}</code>
                  {cfg.java_home_source ? <span className="dbgp-dim">（{cfg.java_home_source}）</span> : null}</div>
                <div>Arthas 启动器：<code>{cfg.boot_jar_resolved || cfg.boot_jar_source}</code></div>
                <div className="dbgp-dim">
                  端口段 8563-8599 · 空闲 {cfg.idle_timeout_sec ?? 900}s 自动回收 · 已禁用命令 <code>{cfg.disabled_commands}</code>
                </div>
              </details>
            )}
          </>
        )}
      </section>

      {/* ── 观测：进行中的作业 + 命中流水 ── */}
      <section className="dbgp-card">
        <div className="dbgp-card-head">
          <span className="dbgp-card-title">观测</span>
          <span className="dbgp-card-meta">
            {jobs.length > 0 ? `${jobs.length} 个作业` : "无作业"} · {entries.length} 条命中
          </span>
          <div className="dbgp-card-actions">
            {entries.length > 0 && (
              <Button size="xs" variant="ghost" onClick={() => clearArthasEntries()}>清空命中</Button>
            )}
          </div>
        </div>
        {jobs.length > 0 && (
          <div className="dbgp-group">
            <div className="dbgp-subhead">进行中的作业</div>
            <ul className="dbgp-jobs">
              {jobs.map((j) => (
                <li className="dbgp-job" key={String(j.job_id)}>
                  <code className="dbgp-job-cmd">{j.command}</code>
                  <Button size="xs" variant="danger-ghost" loading={busy === `job-${j.job_id}`}
                    disabled={!!busy} onClick={() => void stopJob(j.job_id)}>
                    停止
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="dbgp-group">
          <div className="dbgp-subhead dbgp-subhead-row">
            <span>命中</span>
            {entries.length > 0 && <span className="dbgp-dim">{entries.length} 条</span>}
          </div>
          {entries.length === 0 ? (
            <div className="dbgp-empty">
              {jobs.length > 0
                ? "观测已开始，等待目标方法被调用…"
                : "还没有观测命中。在下方 IDEA 断点上点「观测」，或让 AI 调用 arthas_watch。"}
            </div>
          ) : (
            <ol className="dbgp-hits">
              {entries.map((e, i) => <HitRow key={`${i}-${String(e.ts ?? "")}`} entry={e} />)}
            </ol>
          )}
        </div>
      </section>

      {/* ── 断点：IDEA 工程断点 + 本软件断点 ── */}
      <section className="dbgp-card">
        <div className="dbgp-card-head">
          <span className="dbgp-card-title">断点</span>
          <span className="dbgp-card-meta">
            {projectPath ? (ideaRunning ? "IDEA 运行中" : "IDEA 未运行") : "未选择项目"}
          </span>
          <div className="dbgp-card-actions">
            <Button size="xs" variant="ghost" iconOnly icon={<IconRefresh size={12} />}
              aria-label="刷新断点" disabled={!!busy}
              onClick={() => { void refreshBreakpoints(); void loadIdea(); }} />
          </div>
        </div>

        <div className="dbgp-group">
          <div className="dbgp-subhead">IDEA 工程断点</div>
        {!projectPath ? (
          <div className="dbgp-empty">选择项目后，这里会读取它在 IDEA 中设置的断点（.idea/workspace.xml）。</div>
        ) : ideaNote ? (
          <div className="dbgp-empty">{ideaNote}</div>
        ) : ideaBps.length === 0 ? (
          <div className="dbgp-empty">IDEA 工程里还没有断点（或尚未在 IDEA 中打开过该工程）。</div>
        ) : (
          <ul className="dbgp-bps">
            {ideaBps.map((b) => {
              const key = b.line != null ? bpKey(b.file, b.line) : b.id;
              const ref = b.line != null ? resolved[bpKey(b.file, b.line)] : undefined;
              const jobId = ref ? jobByTarget.get(ref.target) : undefined;
              return (
                <li className="dbgp-bp" key={key}>
                  <div className="dbgp-bp-main">
                    <div className="dbgp-bp-line1">
                      <span className="dbgp-bp-file">{baseName(b.file)}</span>
                      <span className="dbgp-bp-lineno">:{b.line}</span>
                      {jobId != null && <span className="dbgp-tag is-ok">观测中</span>}
                      {ref === null && <span className="dbgp-tag is-warn">不在方法体内</span>}
                    </div>
                    <div className="dbgp-bp-dir">{b.file}</div>
                    {ref && <code className="dbgp-bp-target">{ref.target}</code>}
                  </div>
                  <div className="dbgp-bp-actions">
                    {jobId != null ? (
                      <Button size="xs" variant="danger-ghost" disabled={!!busy}
                        onClick={() => void stopJob(jobId)}>
                        停止观测
                      </Button>
                    ) : (
                      <Button size="xs" variant="secondary" disabled={!attached || !!busy || b.line == null}
                        loading={busy === `observe-${key}`} onClick={() => observeBp(b)}>
                        观测
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        {ideaSessions.length > 0 && (
          <p className="dbgp-hint">
            检测到 {ideaSessions.length} 个 JDWP 调试会话：IDEA 占用了 JDWP 通道，观测请使用 Arthas（上面的「观测」按钮）。
          </p>
        )}

        </div>

        <div className="dbgp-group">
          <div className="dbgp-subhead dbgp-subhead-row">
            <span>本软件断点（Web / Java）</span>
          {localRows.length > 0 && (
            <Button size="xs" variant="ghost" disabled={!!busy} onClick={() => { void clearBps("web"); void clearBps("java"); }}>
              全部清除
            </Button>
          )}
        </div>
        {localRows.length === 0 ? (
          <div className="dbgp-empty">暂无本软件断点。</div>
        ) : (
          <ul className="dbgp-bps">
            {localRows.map((b) => (
              <li className="dbgp-bp" key={`${b.target}-${b.id}`}>
                <div className="dbgp-bp-main">
                  <div className="dbgp-bp-line1">
                    <span className="dbgp-bp-file">{b.file || b.class || "(未知)"}</span>
                    {b.line != null && <span className="dbgp-bp-lineno">:{b.line}</span>}
                    <span className="dbgp-tag">{b.target === "web" ? "Web" : "Java"}</span>
                  </div>
                </div>
                <div className="dbgp-bp-actions">
                  <Button size="xs" variant="danger-ghost" loading={busy === `rm-${b.target}-${b.id}`}
                    disabled={!!busy} onClick={() => void removeBp(b.target, b.id)}>
                    删除
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
        </div>
      </section>

      {/* ── 断点会话：仅真断点连接时出现 ── */}
      {sessionCards.length > 0 && (
        <section className="dbgp-card">
          <div className="dbgp-card-head">
            <span className="dbgp-card-title">断点会话</span>
          </div>
          {sessionCards.map((c) => (
            <SessionCard key={c.key} label={c.label} status={c.status as DebugStatusOut} target={c.key}
              onAct={(a, extra) => void bpAct(c.key, a, extra)} />
          ))}
        </section>
      )}
    </div>
  );
}
