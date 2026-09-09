/** 应用 Logo（与 electron/build/icon.png 同一设计）：
 * 渐变圆角方块 + 白色对话气泡 + `</>` 代码符号（chat + coder）。
 * 供侧栏 / 标题栏 / 启动蒙层 / 关于页复用，深浅色主题下均清晰。
 * plan-1092: variant="outline" 为纯描边线稿（无渐变实心底，笔画走 currentColor），
 * 供空态首页水印使用——实心底在低透明度下会叠加成可见色块，线稿则与背景融合。 */
import { useId } from "react";

export function AppLogo({
  size = 20,
  className,
  variant = "solid",
}: {
  size?: number;
  className?: string;
  /** solid = 渐变实心底（默认，现有 5 处调用零改动）；outline = 单色描边线稿 */
  variant?: "solid" | "outline";
}) {
  const gid = useId();
  const stroke = variant === "outline" ? "currentColor" : "#fff";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 512 512"
      fill="none"
      className={className}
      role="img"
      aria-label="chatcoder"
    >
      {variant === "solid" && (
        <>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="512" y2="512" gradientUnits="userSpaceOnUse">
              <stop stopColor="#1B2552" />
              <stop offset="1" stopColor="#7C3AED" />
            </linearGradient>
          </defs>
          <rect width="512" height="512" rx="115" fill={`url(#${gid})`} />
        </>
      )}
      {/* 对话气泡（描边）+ 左下尾巴 */}
      <rect x="128" y="138" width="256" height="180" rx="46" stroke={stroke} strokeWidth="22" />
      <path d="M206 314 L168 372 L262 314 Z" fill={stroke} />
      {/* `</>` 代码符号（圆头粗线） */}
      <path
        d="M186 228 L218 204 M186 228 L218 252 M272 194 L240 262 M326 228 L294 204 M326 228 L294 252"
        stroke={stroke}
        strokeWidth="19"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
