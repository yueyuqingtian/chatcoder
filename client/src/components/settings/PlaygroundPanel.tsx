/** 设置 → 嗒嗒乐园（plan-114-452：three.js 精修版）。
 * 一整块沉浸式舞台：场景随主题世界换「表面」与布景，嗒嗒跟着鼠标跑、会跳、会被戳，碰到发光星星就收集。
 * 玻璃质感的小浮条悬在舞台上（计数 / 重来 / 起手提示），舞台之外没有多余的底栏——画面本身就是全部。
 * three 体积较大，舞台经 React.lazy 按需加载，不进设置页首屏包；加载中 / 不支持 WebGL 时舞台区用同尺寸占位，卡片不跳变。 */
import { lazy, Suspense, useCallback, useState } from "react";
import { useThemeStore } from "../../store/theme";
import { DadaLoader, Empty, IconButton } from "../ui";
import { IconRotateCcw } from "../icons";

const Stage = lazy(() => import("./playground/Stage"));

/** 浮条里的小星星：与场景里的收集物同形（圆润八面体） */
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
  const [collected, setCollected] = useState(0);
  const [resetKey, setResetKey] = useState(0);
  const [unsupported, setUnsupported] = useState(false);
  const [played, setPlayed] = useState(false);
  const onCollect = useCallback((n: number) => setCollected((c) => c + n), []);
  const onInteract = useCallback(() => setPlayed(true), []);
  const onUnsupported = useCallback(() => setUnsupported(true), []);

  const reset = () => { setCollected(0); setResetKey((k) => k + 1); };

  return (
    <div className="pg-card">
      <div className="pg-stage">
        {unsupported ? (
          <div className="pg-fallback"><Empty mood="oops" title="这里画不出乐园" desc="当前环境不支持 WebGL，换到桌面版或开启硬件加速后再来玩" /></div>
        ) : (
          <>
            <Suspense fallback={<div className="pg-fallback"><DadaLoader label="正在布置乐园…" /></div>}>
              <Stage world={world} theme={theme} resetKey={resetKey} onCollect={onCollect} onInteract={onInteract} onUnsupported={onUnsupported} />
            </Suspense>
            <div className="pg-glare" aria-hidden />
            <div className="pg-hud pg-hud-tl" aria-live="polite">
              <StarGlyph />
              <span className="pg-count">{collected}</span>
            </div>
            <div className="pg-hud pg-hud-tr">
              <IconButton size="sm" title="重来：嗒嗒回到中央，星星重新散落" icon={<IconRotateCcw size={14} />} onClick={reset} disabled={collected === 0} />
            </div>
            {/* 起手提示：固定槽位，只切透明度；玩过之后不再出现 */}
            <div className="pg-hud pg-hint" data-show={!played || undefined} aria-hidden>移动鼠标，嗒嗒会跟过去 · 点击让它跳一下 · 收集发光的小星星</div>
          </>
        )}
      </div>
    </div>
  );
}
