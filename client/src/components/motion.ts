/**
 * v0.9.1: 动画系统统一入口(纯 CSS 方案,无需 framer-motion 依赖)。
 *
 * 提供:
 * - ANIM_TOKEN: 动效 token 引用(供内联 style 用)
 * - ANIM_CLASS: 通用动画 class 名(对应 global.css 的 @keyframes)
 * - staggerDelay: 列表入场错峰延迟计算
 *
 * 用法:
 *   import { ANIM_CLASS, staggerDelay } from "./motion";
 *   <div className={ANIM_CLASS.slideInLeft} style={{ animationDelay: staggerDelay(i) }}>...</div>
 */

export const ANIM_TOKEN = {
  easeOutExpo: "var(--ease-out-expo)",
  easeSpring: "var(--ease-spring)",
  easeInOut: "var(--ease-in-out)",
  durFast: "var(--dur-fast)",
  durNormal: "var(--dur-normal)",
  durSlow: "var(--dur-slow)",
} as const;

export const ANIM_CLASS = {
  slideInLeft: "anim-slide-in-left",
  scaleIn: "anim-scale-in",
  slideUp: "anim-slide-up",
  spinning: "spinning",
} as const;

/** 列表入场错峰:每项延迟 40ms,封顶 240ms */
export function staggerDelay(index: number, step = 40, max = 240): string {
  return `${Math.min(index * step, max)}ms`;
}
