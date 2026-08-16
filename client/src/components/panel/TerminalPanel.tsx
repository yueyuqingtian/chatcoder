/** 终端面板（v2.2 对齐 zcode 3.15）：xterm.js 终端模拟器 + node-pty 真终端。
 * 特性：
 * - ANSI 颜色 / 光标控制 / 全屏交互程序（vim、less、top）——依赖主进程 node-pty
 * - 面板尺寸变化自动 fit + 同步后端 cols/rows（pty:resize）
 * - 多终端标签并存（RightPanel 保活渲染，每个 tab 独立 PTY 会话）
 */
import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api as backendApi } from "../../api/client";
import { useChatStore } from "../../store/chat";
import type { PanelTab } from "../../store/panel";

interface TerminalPanelProps {
  tab: PanelTab;
}

function readCssVar(name: string, fallback: string): string {
  if (typeof document === "undefined") return fallback;
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch { return fallback; }
}

export function TerminalPanel({ tab }: TerminalPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const ptyIdRef = useRef<number | null>(null);
  const api = typeof window !== "undefined" ? window.chatcoderAPI : undefined;

  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const sessions = useChatStore((s) => s.sessions);

  // 终端工作目录 = 当前会话所属项目目录
  const session = sessions.find((s) => s.id === currentSessionId);
  const project = projects.find((p) => p.id === (session?.project_id ?? currentProjectId));
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
      // v2.2: 集成终端 Shell 选择读全局设置（auto 交给主进程按平台默认解析）
      backendApi.getGlobalSettings().then((g) => {
        if (disposed) return;
        const shell = g.terminal_shell && g.terminal_shell !== "auto" ? g.terminal_shell : undefined;
        return api!.ptySpawn!({ cwd, cols, rows, shell }).then((res: { id: number; error?: string }) => {
          if (disposed || !res.id) return;
          ptyIdRef.current = res.id;
        });
      }).catch(() => {
        if (disposed) return;
        api!.ptySpawn!({ cwd, cols, rows }).then((res: { id: number; error?: string }) => {
          if (disposed || !res.id) return;
          ptyIdRef.current = res.id;
        }).catch(() => {});
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
    const ro = new ResizeObserver(() => {
      if (!containerRef.current) return;
      try {
        fit.fit();
        if (ptyIdRef.current != null) {
          api?.ptyResize?.(ptyIdRef.current, term.cols, term.rows);
        }
      } catch {}
    });
    ro.observe(container);

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      ro.disconnect();
      offData?.();
      offExit?.();
      dataSub.dispose();
      if (ptyIdRef.current != null) api?.ptyKill?.(ptyIdRef.current);
      ptyIdRef.current = null;
      term.dispose();
      termRef.current = null;
    };
    // tab.instance 变化（多开）时重建独立 PTY
  }, [api, cwd, tab.instance]);

  if (!api?.ptySpawn) {
    return <div className="rp-body"><div className="rp-empty">终端需要桌面版环境</div></div>;
  }

  return <div ref={containerRef} className="terminal-xterm" />;
}
