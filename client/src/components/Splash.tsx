/** 启动蒙层：Logo 渐变缩放 0.9->1 + 淡入 400ms -> 主界面淡入。
 * 背景用 var(--bg-main) 跟随应用深浅色主题；图标与桌面图标同设计（AppLogo）。 */
import { useEffect, useState } from "react";
import { AppLogo } from "./AppLogo";

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
        <AppLogo size={48} />
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
