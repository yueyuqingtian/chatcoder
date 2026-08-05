import { useEffect, useState } from "react";
import { useChatStore } from "../store/chat";
import { IconInfo, IconX } from "./icons";

export function Toast() {
  const error = useChatStore((s) => s.error);
  // 离场状态：先播放退出动画，再真正卸载，避免 toast 生硬消失
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    if (!error) { setLeaving(false); return; }
    const timer = setTimeout(() => setLeaving(true), 4000);
    return () => clearTimeout(timer);
  }, [error]);

  useEffect(() => {
    if (!leaving) return;
    const t = setTimeout(() => {
      useChatStore.setState({ error: null });
      setLeaving(false);
    }, 220);
    return () => clearTimeout(t);
  }, [leaving]);

  if (!error) return null;

  return (
    <div className={"toast" + (leaving ? " toast-out" : "")}>
      <div className="toast-content">
        <IconInfo size={14} />
        <span>{error}</span>
      </div>
      <button className="toast-close" onClick={() => setLeaving(true)} title="关闭">
        <IconX size={12} />
      </button>
      <style>{`
        .toast {
          position: fixed; top: 54px; right: 20px; z-index: 2000;
          display: flex; align-items: center; gap: 10px;
          padding: 12px 14px 12px 16px; border-radius: var(--radius);
          background: var(--bg-muted); border: 1px solid var(--border-light);
          border-left: 3px solid var(--error);
          box-shadow: var(--shadow-lg);
          font-size: var(--fs-sm); color: var(--text);
          max-width: 380px; word-break: break-word;
          animation: toast-in var(--dur-normal) var(--ease-out);
        }
        .toast.toast-out { animation: toast-out var(--dur-normal) var(--ease-out) forwards; }
        .toast-content { display: flex; align-items: flex-start; gap: 8px; flex: 1; min-width: 0; }
        .toast-content svg { color: var(--error); margin-top: 1px; flex-shrink: 0; }
        .toast-close {
          flex-shrink: 0; width: 24px; height: 24px; padding: 0;
          display: flex; align-items: center; justify-content: center;
          color: var(--text-muted); border-radius: var(--radius-xs);
          transition: background var(--dur-fast) var(--ease-out),
                      color var(--dur-fast) var(--ease-out);
        }
        .toast-close:hover { background: var(--bg-hover); color: var(--text); transform: none; }
        @keyframes toast-in {
          from { opacity: 0; transform: translateX(16px); }
          to { opacity: 1; transform: translateX(0); }
        }
        @keyframes toast-out {
          from { opacity: 1; transform: translateX(0); }
          to { opacity: 0; transform: translateX(16px); }
        }
      `}</style>
    </div>
  );
}
