/** 应用 Logo（plan-114-450）：与 electron/build/icon.png 同一设计——
 * 蓝色圆角方块 + 嗒嗒从底边探出大半个头，眼睛往上看；无描边，体积靠受光渐变与柔和投影。
 * 供侧栏 / 标题栏 / 关于页复用。按显示尺寸分两档：≤20px 去掉呆毛与腮红、眼睛放大，保证 16~20px 可辨认。
 * variant="outline" 为单色剪影（跟随 currentColor），供低透明度水印使用。 */
import { useId } from "react";
import { DADA_BODY, DADA_EYE, DADA_TUFT } from "./mascot/Dada";

/** 图标底色：用户选定蓝色；中心亮、四周深的径向渐变，与启动页同一画法 */
export const LOGO_BLUE = "#5B7BE0";
export const LOGO_BLUE_LIGHT = "#7C9BF0";

export function AppLogo({
  size = 20,
  className,
  variant = "solid",
}: {
  size?: number;
  className?: string;
  variant?: "solid" | "outline";
}) {
  const uid = useId().replace(/:/g, "");
  const tiny = size <= 20;
  // 嗒嗒以 (60,66) 为中心缩放后放到方块 (50, cy)，下半身被方块底边裁掉
  const k = tiny ? 0.98 : 0.8;
  const cy = tiny ? 70 : 76;
  const eyeK = tiny ? 1.35 : 1;
  const head = (fill: string, eye: string) => (
    <g transform={`translate(50 ${cy}) scale(${k}) translate(-60 -66)`}>
      {!tiny && <path d={DADA_TUFT} fill={fill} />}
      <path d={DADA_BODY} fill={fill} />
      {!tiny && variant === "solid" && (
        <g fill="#FF8FA8" opacity={0.38}>
          <ellipse cx="35" cy="77" rx="7" ry="4" />
          <ellipse cx="85" cy="77" rx="7" ry="4" />
        </g>
      )}
      <g fill={eye}>
        <ellipse cx="48" cy="62" rx={7 * eyeK} ry={10.4 * eyeK} />
        <ellipse cx="72" cy="62" rx={7 * eyeK} ry={10.4 * eyeK} />
      </g>
    </g>
  );

  if (variant === "outline") {
    return (
      <svg width={size} height={size} viewBox="0 0 100 100" className={className} role="img" aria-label="chatcoder">
        <defs>
          <clipPath id={`lc${uid}`}><rect width="100" height="100" rx="23" /></clipPath>
          <mask id={`lm${uid}`}>
            <rect width="100" height="100" fill="#fff" />
            {head("#000", "#fff")}
          </mask>
        </defs>
        <rect width="100" height="100" rx="23" fill="currentColor" mask={`url(#lm${uid})`} clipPath={`url(#lc${uid})`} />
      </svg>
    );
  }

  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className={className} role="img" aria-label="chatcoder">
      <defs>
        <clipPath id={`lc${uid}`}><rect width="100" height="100" rx="23" /></clipPath>
        <radialGradient id={`lb${uid}`} cx="50%" cy="38%" r="75%">
          <stop offset="0" stopColor={LOGO_BLUE_LIGHT} />
          <stop offset="1" stopColor={LOGO_BLUE} />
        </radialGradient>
        <radialGradient id={`lg${uid}`} cx="40%" cy="32%" r="78%">
          <stop offset="0" stopColor="#FFFFFF" />
          <stop offset="0.62" stopColor="#F3F5FB" />
          <stop offset="1" stopColor="#D9DFEF" />
        </radialGradient>
        <filter id={`ls${uid}`} x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy={tiny ? 1.5 : 3} stdDeviation={tiny ? 1.5 : 3.5} floodColor="#000" floodOpacity="0.22" />
        </filter>
      </defs>
      <g clipPath={`url(#lc${uid})`}>
        <rect width="100" height="100" fill={`url(#lb${uid})`} />
        <g filter={`url(#ls${uid})`}>{head(`url(#lg${uid})`, DADA_EYE)}</g>
      </g>
    </svg>
  );
}
