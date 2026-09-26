/** 终端面板（v2.2 对齐 zcode 3.15）：xterm.js 终端模拟器 + node-pty 真终端。
 * 特性：
 * - ANSI 颜色 / 光标控制 / 全屏交互程序（vim、less、top）——依赖主进程 node-pty
 * - 面板尺寸变化自动 fit + 同步后端 cols/rows（pty:resize）
 * - 多终端标签并存（RightPanel 保活渲染，每个 tab 独立 PTY 会话）
 */
import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api as backendApi } from "../../api/client";
import { isBusy } from "../../perf/bus";
import { registerReconcileTask, RECONCILE_ORDER } from "../../perf/reconcile";
import { recordDerivation } from "../../perf/metrics";
import { useChatStore } from "../../store/chat";
import type { PanelTab } from "../../store/panel";

interface TerminalPanelProps {
  tab: PanelTab;
  /** plan-41-233：标签所属会话（跨会话保活渲染时不能跟随当前会话漂移——
   *  否则切换会话会改动 cwd、命中 effect 依赖并重建 PTY，终端被重置）。 */
  sessionId?: number | null;
  /** plan-75-334 阶段3：面板是否可见。隐藏时仅暂停 fit / 尺寸同步等**布局工作**，
   *  PTY 进程、数据订阅与滚动缓冲全部保留（恢复可见时补一次 fit）。
   *  注意：不得进入 PTY 建连 effect 的依赖，否则隐藏/显示会重建终端。 */
  visible?: boolean;
}

function readCssVar(name: string, fallback: string): string {
  if (typeof document === "undefined") return fallback;
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch { return fallback; }
}

export function TerminalPanel({ tab, sessionId, visible = true }: TerminalPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const ptyIdRef = useRef<number | null>(null);
  /** plan-75-334 阶段3：可见性镜像（供 PTY effect 内的回调读取，不进依赖）。 */
  const visibleRef = useRef(visible);
  /** plan-75-334 阶段3：PTY effect 内定义的 fit 函数句柄——恢复可见时由外层补一次。 */
  const doFitRef = useRef<(() => void) | null>(null);
  const api = typeof window !== "undefined" ? window.chatcoderAPI : undefined;
  // v19: spawn 失败可见反馈（此前静默吞错导致白屏无提示）
  const [spawnError, setSpawnError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);

  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const sessions = useChatStore((s) => s.sessions);

  // 终端工作目录 = 标签所属会话的项目目录（无所属会话时退回当前项目）
  const ownerSession = sessionId != null ? sessions.find((s) => s.id === sessionId) : undefined;
  const project = projects.find((p) => p.id === (ownerSession?.project_id ?? currentProjectId));
  const cwd = project?.path;

  useEffect(() => {
    if (!api?.ptySpawn || !containerRef.current) return;
    const container = containerRef.current;

    const term = new Terminal({
      fontFamily: '"Cascadia Code", Consolas, "Courier New", monospace',
      fontSize: 13,
      lineHeight: 1.2,
      cursorBlink: true,
      convertEol: true,
      scrollback: 5000,
      theme: {
        background: readCssVar("--bg-elevated", "#1e1e24"),
        foreground: readCssVar("--text-1", "#e8e8ea"),
        cursor: readCssVar("--accent-2", "#8a4dff"),
        cursorAccent: readCssVar("--accent-contrast", "#ffffff"),
        selectionBackground: "rgba(138,77,255,0.25)",
        black: "#282828", red: "#e06c75", green: "#98c379", yellow: "#d19a66",
        blue: "#61afef", magenta: "#c678dd", cyan: "#56b6c2", white: "#abb2bf",
        brightBlack: "#5c6370", brightRed: "#e06c75", brightGreen: "#98c379",
        brightYellow: "#d19a66", brightBlue: "#61afef", brightMagenta: "#c678dd",
        brightCyan: "#56b6c2", brightWhite: "#ffffff",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);
    termRef.current = term;

    let disposed = false;
    let offData: (() => void) | undefined;
    let offExit: (() => void) | undefined;

    function spawnPty() {
      const cols = term.cols || 80;
      const rows = term.rows || 24;
      // v19: spawn 失败不再静默——红色错误行 + 重试覆盖层
      const handleRes = (res: { id: number; error?: string }) => {
        if (disposed) return;
        if (!res.id) {
          const msg = res.error || "无法启动终端进程";
          setSpawnError(msg);
          term.write(`\r\n\x1b[31m[终端错误] ${msg}\x1b[0m\r\n`);
          return;
        }
        setSpawnError(null);
        ptyIdRef.current = res.id;
      };
      // v2.2: 集成终端 Shell 选择读全局设置（auto 交给主进程按平台默认解析）
      backendApi.getGlobalSettings().then((g) => {
        if (disposed) return;
        const shell = g.terminal_shell && g.terminal_shell !== "auto" ? g.terminal_shell : undefined;
        return api!.ptySpawn!({ cwd, cols, rows, shell }).then(handleRes);
      }).catch(() => {
        if (disposed) return;
        api!.ptySpawn!({ cwd, cols, rows }).then(handleRes).catch((e) => {
          if (!disposed) setSpawnError(String(e));
        });
      });
      offData = api!.onPtyData?.((id, data) => {
        if (ptyIdRef.current !== id) return;
        term.write(data);
      });
      offExit = api!.onPtyExit?.((id) => {
        if (ptyIdRef.current !== id) return;
        term.write("\r\n\x1b[90m[进程已退出]\x1b[0m\r\n");
      });
    }

    // 首次 fit：等布局完成后发起 PTY
    const raf = requestAnimationFrame(() => {
      try { fit.fit(); } catch {}
      spawnPty();
    });

    // 键盘输入 → PTY
    const dataSub = term.onData((d) => {
      if (ptyIdRef.current != null) api?.ptyWrite?.(ptyIdRef.current, d);
    });

    // 面板尺寸变化 → fit + 同步后端
    //
    // 卡顿治理（用户反馈"拖动/尺寸变化时非常卡顿，尤其会话正在运行时"）：
    //   xterm 的 fit() 会重新计算行列并重排整个终端缓冲（含已回滚的历史行），
    //   单次成本随会话输出量增长——它正是"会话正在运行时拖拽特别卡"的主要贡献者之一。
    //   窗口/面板拖拽期间宽度每帧都在变，逐帧 fit 等于把这段重活乘以帧数。
    //   这里改为：运动中只**记账**，等运动停止后补一次 fit（终态一次即可，
    //   中间帧的行列数本来也不会被用户看到）。判定统一走 PerfBus（S3）——
    //   此前这里只认「拖分隔条 / 窗口运动」，漏了「面板折叠过渡」，现已合并。
    let fitTimer = 0;
    let ptyResizeTimer = 0;
    const inMotion = () => isBusy();
    const doFit = () => {
      if (!containerRef.current) return;
      // plan-75-334 阶段3：隐藏期不做布局工作（容器尺寸为 0，fit 无意义且会触发重排）
      if (!visibleRef.current) return;
      try {
        recordDerivation("terminalFit");
        fit.fit();
        if (ptyIdRef.current != null) {
          api?.ptyResize?.(ptyIdRef.current, term.cols, term.rows);
        }
      } catch { /* 尺寸为 0 等瞬时状态忽略 */ }
    };
    doFitRef.current = doFit;
    /** 运动结束后的收尾 fit：双帧 rAF 后执行，确保容器 clientWidth 已是终值。
     *  事件驱动（主进程 chatcoder:window-motion / 分隔条 pointerup）替代旧的 120ms 轮询，
     *  拖完立刻收敛，不再多等一个固定延时。 */
    const settleFit = () => {
      if (fitTimer) { window.clearTimeout(fitTimer); fitTimer = 0; }
      if (ptyResizeTimer) { window.clearTimeout(ptyResizeTimer); ptyResizeTimer = 0; }
      ptyResizeTimer = window.setTimeout(() => {
        ptyResizeTimer = 0;
        if (disposed || inMotion()) return;
        doFit();
      }, 16);
    };
    // RFL-6（S6）：注册进唯一收敛序列——order 50 终端 fit。
    // 直接 doFit（不再走 settleFit 的 16ms 延时）：收敛序列本身在 rAF 内，
    // 且 order 20/30 已先提交虚拟器尺寸，此时容器宽度已是终值。
    // 原有事件监听保留：窗口缩放/补间路径仍走它们。
    const offReconcile = registerReconcileTask("terminal-fit", RECONCILE_ORDER.terminalFit,
      "终端 fit", () => { if (!disposed) doFit(); });
    const onMotionEnd = (e: Event) => {
      if ((e as CustomEvent<{ active?: boolean }>).detail?.active === false) settleFit();
    };
    const onPointerUp = () => { if (!inMotion()) settleFit(); };
    window.addEventListener("chatcoder:window-motion", onMotionEnd);
    window.addEventListener("pointerup", onPointerUp, true);
    const ro = new ResizeObserver(() => {
      if (!containerRef.current) return;
      if (inMotion()) return; // 运动中不 fit：结束事件会补一次（终态一次即可）
      // 非运动态：RO 本身已按帧合并，直接 fit（保持原有即时语义）
      if (fitTimer) { window.clearTimeout(fitTimer); fitTimer = 0; }
      if (ptyResizeTimer) { window.clearTimeout(ptyResizeTimer); ptyResizeTimer = 0; }
      doFit();
    });
    ro.observe(container);

    return () => {
      disposed = true;
      doFitRef.current = null;
      offReconcile();
      cancelAnimationFrame(raf);
      if (fitTimer) window.clearTimeout(fitTimer);
      if (ptyResizeTimer) window.clearTimeout(ptyResizeTimer);
      window.removeEventListener("chatcoder:window-motion", onMotionEnd);
      window.removeEventListener("pointerup", onPointerUp, true);
      ro.disconnect();
      offData?.();
      offExit?.();
      dataSub.dispose();
      if (ptyIdRef.current != null) api?.ptyKill?.(ptyIdRef.current);
      ptyIdRef.current = null;
      term.dispose();
      termRef.current = null;
    };
    // tab.instance 变化（多开）时重建独立 PTY；retryTick 触发重试重建
  }, [api, cwd, tab.instance, retryTick]);

  // plan-75-334 阶段3：同步可见性镜像，并在恢复可见时补一次 fit。
  // 延迟到下一帧执行：display:none → block 的尺寸需要一帧才能量到。
  useEffect(() => {
    visibleRef.current = visible;
    if (!visible) return;
    const raf = requestAnimationFrame(() => { doFitRef.current?.(); });
    return () => cancelAnimationFrame(raf);
  }, [visible]);

  if (!api?.ptySpawn) {
    return <div className="rp-body"><div className="rp-empty">终端需要桌面版环境</div></div>;
  }

  return (
    <div className="terminal-wrap">
      <div ref={containerRef} className="terminal-xterm" />
      {spawnError && (
        <div className="terminal-error-overlay">
          <div className="terminal-error-msg">终端启动失败：{spawnError}</div>
          <button className="btn btn-ghost btn-sm" onClick={() => { setSpawnError(null); setRetryTick((v) => v + 1); }}>
            重试
          </button>
        </div>
      )}
    </div>
  );
}
