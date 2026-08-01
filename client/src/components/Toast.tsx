import { useEffect } from "react";
import { useChatStore } from "../store/chat";

export function Toast() {
  const error = useChatStore((s) => s.error);

  useEffect(() => {
    if (!error) return;
    const timer = setTimeout(() => {
      useChatStore.setState({ error: null });
    }, 4000);
    return () => clearTimeout(timer);
  }, [error]);

  if (!error) return null;

  return (
    <div className="toast">
      <div className="toast-content">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="12" />
          <line x1="12" y1="16" x2="12.01" y2="16" />
        </svg>
        <span>{error}</span>
      </div>
      <button className="toast-close" onClick={() => useChatStore.setState({ error: null })} title="关闭">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
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
        .toast-content { display: flex; align-items: flex-start; gap: 8px; flex: 1; min-width: 0; }
        .toast-content svg { color: var(--error); margin-top: 1px; }
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
      `}</style>
    </div>
  );
}
