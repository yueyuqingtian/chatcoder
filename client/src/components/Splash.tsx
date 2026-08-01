/** 启动蒙层：Logo 渐变缩放 0.9->1 + 淡入 400ms -> 主界面淡入。 */
import { useEffect, useState } from "react";

export function Splash({ onDone }: { onDone: () => void }) {
  const [phase, setPhase] = useState<"in" | "out">("in");

  useEffect(() => {
    const t1 = setTimeout(() => setPhase("out"), 600);
    const t2 = setTimeout(onDone, 900);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, [onDone]);

  return (
    <div className={`splash ${phase === "out" ? "splash-out" : ""}`}>
      <div className="splash-logo">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <rect x="6" y="6" width="36" height="36" rx="10" fill="url(#splash-grad)" />
          <path d="M18 20l-4 4 4 4M30 20l4 4-4 4M26 16l-4 16" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          <defs>
            <linearGradient id="splash-grad" x1="6" y1="6" x2="42" y2="42" gradientUnits="userSpaceOnUse">
              <stop stopColor="#4836FF" />
              <stop offset="1" stopColor="#CE53FF" />
            </linearGradient>
          </defs>
        </svg>
        <span className="splash-text">ChatCoder</span>
      </div>
      <style>{`
        .splash {
          position: fixed; inset: 0; z-index: 9999;
          display: flex; align-items: center; justify-content: center;
          background: var(--bg-main);
          transition: opacity 0.3s var(--curve);
        }
        .splash-out { opacity: 0; pointer-events: none; }
        .splash-logo {
          display: flex; flex-direction: column; align-items: center; gap: 12px;
          animation: splash-in 0.5s var(--curve);
        }
        .splash-text {
          font-size: 18px; font-weight: 700; letter-spacing: 0.5px;
          background: var(--accent-grad);
          -webkit-background-clip: text; background-clip: text;
          -webkit-text-fill-color: transparent;
        }
        @keyframes splash-in {
          from { opacity: 0; transform: scale(0.9); }
          to { opacity: 1; transform: scale(1); }
        }
      `}</style>
    </div>
  );
}
