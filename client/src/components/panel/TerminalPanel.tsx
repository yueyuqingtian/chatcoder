/** 终端面板（v4 新增）：§3.5 通过 Electron IPC PTY 运行命令。
 * 不依赖 xterm.js，用自实现的 div 渲染 + input 输入，满足"运行命令与脚本"需求。
 * 主进程 child_process.spawn 起 pwsh/cmd，stdout 经 IPC 转发到此组件。
 */
import { useEffect, useRef, useState } from "react";

interface TerminalPanelProps {
  cwd?: string;
}

interface PtySession {
  id: number;
  lines: string[];
}

export function TerminalPanel({ cwd }: TerminalPanelProps) {
  const [session, setSession] = useState<PtySession | null>(null);
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const api = typeof window !== "undefined" ? window.chatcoderAPI : undefined;

  // 启动 PTY
  useEffect(() => {
    if (!api?.ptySpawn) return;
    let alive = true;
    api.ptySpawn({ cwd }).then((res: { id: number; error?: string }) => {
      if (!alive || !res.id) return;
      setSession({ id: res.id, lines: [] });
    }).catch(() => {});

    const offData = api.onPtyData?.((id: number, data: string) => {
      setSession((prev) => {
        if (!prev || prev.id !== id) return prev;
        return { ...prev, lines: [...prev.lines, data] };
      });
    });
    const offExit = api.onPtyExit?.((id: number) => {
      setSession((prev) => {
        if (!prev || prev.id !== id) return prev;
        return { ...prev, lines: [...prev.lines, "\r\n[进程已退出]\r\n"] };
      });
    });

    return () => {
      alive = false;
      offData?.();
      offExit?.();
    };
  }, [api, cwd]);

  // 自动滚到底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [session?.lines]);

  // 卸载时 kill
  useEffect(() => {
    return () => {
      if (session?.id) api?.ptyKill?.(session.id);
    };
  }, [session?.id, api]);

  const handleKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && session) {
      api?.ptyWrite?.(session.id, input + "\r\n");
      setInput("");
    }
  };

  if (!api?.ptySpawn) {
    return <div className="rp-body"><div className="rp-empty">终端需要桌面版环境</div></div>;
  }

  return (
    <div className="terminal-panel">
      <div className="terminal-output" ref={scrollRef}>
        {session?.lines.map((l, i) => (
          <pre key={i} className="terminal-line">{l}</pre>
        ))}
      </div>
      <div className="terminal-input-row">
        <span className="terminal-prompt">$</span>
        <input
          className="terminal-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKey}
          placeholder="输入命令…"
          autoFocus
        />
      </div>
    </div>
  );
}
