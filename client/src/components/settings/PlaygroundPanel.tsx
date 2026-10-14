/** 设置 → 嗒嗒乐园（plan-114-452 three.js 精修；plan-playground-v2 六个世界六种玩法）。
 * 一整块沉浸式舞台：场景与玩法随主题世界切换，围绕嗒嗒展开（记忆 / 追逐 / 烤制 / 放风筝 / 打水漂 / 接落樱）。
 * 玻璃质感的小浮条悬在舞台上：左上玩法名与分数、右上本世界最佳与重来、中上即时播报、底部起手提示。
 * three 体积较大，舞台经 React.lazy 按需加载，不进设置页首屏包；加载中 / 不支持 WebGL 时舞台区用同尺寸占位，卡片不跳变。 */
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useThemeStore } from "../../store/theme";
import { DadaLoader, Empty, IconButton } from "../ui";
import { IconRotateCcw } from "../icons";
import { GAME_META } from "./playground/meta";
import type { Tone } from "./playground/games/types";

const Stage = lazy(() => import("./playground/Stage"));
const BEST_KEY = "chatcoder.playground.best.";

function readBest(world: string): number {
  try { return Number(localStorage.getItem(BEST_KEY + world)) || 0; } catch { return 0; }
}
function writeBest(world: string, v: number) {
  try { localStorage.setItem(BEST_KEY + world, String(v)); } catch { /* 存不了就只在本次会话里记 */ }
}

/** 浮条里的小星星 */
function StarGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden className="pg-star-glyph">
      <path d="M12 2c1 5.5 4.5 9 10 10-5.5 1-9 4.5-10 10-1-5.5-4.5-9-10-10 5.5-1 9-4.5 10-10z" fill="currentColor" />
    </svg>
  );
}

export function PlaygroundPanel() {
  const world = useThemeStore((s) => s.world);
  const theme = useThemeStore((s) => s.theme);
  const meta = GAME_META[world] ?? GAME_META.graphite;
  const [score, setScore] = useState(0);
  const [best, setBest] = useState(() => readBest(world));
  const [resetKey, setResetKey] = useState(0);
  const [unsupported, setUnsupported] = useState(false);
  const [played, setPlayed] = useState(false);
  const [callout, setCallout] = useState<{ text: string; tone: Tone; key: number } | null>(null);
  const [bump, setBump] = useState(0);
  const calloutTimer = useRef(0);

  // 换世界 = 换玩法：分数、提示、播报各自归零，最佳成绩按世界读取
  useEffect(() => {
    setScore(0); setPlayed(false); setCallout(null); setBest(readBest(world));
  }, [world]);
  useEffect(() => () => window.clearTimeout(calloutTimer.current), []);

  const onScore = useCallback((n: number) => {
    setScore((s) => {
      const v = s + n;
      setBest((b) => { if (v > b) { writeBest(world, v); return v; } return b; });
      return v;
    });
    setBump((k) => k + 1);
  }, [world]);
  const onSay = useCallback((text: string, tone: Tone) => {
    setCallout((c) => ({ text, tone, key: (c?.key ?? 0) + 1 }));
    window.clearTimeout(calloutTimer.current);
    calloutTimer.current = window.setTimeout(() => setCallout(null), 2200);
  }, []);
  const onInteract = useCallback(() => setPlayed(true), []);
  const onUnsupported = useCallback(() => setUnsupported(true), []);

  const reset = () => { setScore(0); setResetKey((k) => k + 1); };

  return (
    <div className="pg-card">
      <div className="pg-stage">
        {unsupported ? (
          <div className="pg-fallback"><Empty mood="oops" title="这里画不出乐园" desc="当前环境不支持 WebGL，换到桌面版或开启硬件加速后再来玩" /></div>
        ) : (
          <>
            <Suspense fallback={<div className="pg-fallback"><DadaLoader label="正在布置乐园…" /></div>}>
              <Stage world={world} theme={theme} resetKey={resetKey} onScore={onScore} onSay={onSay} onInteract={onInteract} onUnsupported={onUnsupported} />
            </Suspense>
            <div className="pg-glare" aria-hidden />
            <div className="pg-hud pg-hud-tl" aria-live="polite">
              <span className="pg-game">{meta.name}</span>
              <span className="pg-sep" aria-hidden />
              <StarGlyph />
              <span className="pg-count" key={bump} data-bump={bump > 0 || undefined}>{score}</span>
            </div>
            <div className="pg-hud pg-hud-tr">
              <span className="pg-best">最佳 <b>{best}</b> {meta.unit}</span>
              <IconButton size="sm" title="重来：清空本局分数，场景回到初始" icon={<IconRotateCcw size={14} />} onClick={reset} disabled={score === 0} />
            </div>
            {/* 播报：固定槽位，只切透明度与位移 */}
            <div className="pg-callout-slot" aria-live="polite">
              {callout && <div key={callout.key} className="pg-hud pg-callout" data-tone={callout.tone}>{callout.text}</div>}
            </div>
            {/* 起手提示：固定槽位，只切透明度；本世界玩过之后不再出现 */}
            <div className="pg-hud pg-hint" data-show={!played || undefined} aria-hidden>{meta.hint}</div>
          </>
        )}
      </div>
    </div>
  );
}
