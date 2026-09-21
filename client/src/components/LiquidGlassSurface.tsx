/** plan-308-1555 M8：液态玻璃折射层（基于 @tomagranate/liquid-glass 的 SVG feDisplacementMap 引擎）。
 *
 * ── 为什么做成"叠加层"而不是"包裹容器" ──
 * 该库的 `.lq` 结构会强制 `position:relative; overflow:hidden; isolation:isolate`，
 * 并把内容放进 `height:100%` 的 `.lq-content`。直接包裹侧栏/右面板会改变它们的
 * 布局与滚动行为（尤其侧栏本身是 flex 纵向 + overflow:hidden 的滚动容器）。
 * 因此这里把折射层做成**绝对定位的纯装饰覆盖层**（pointer-events:none），
 * 现有 DOM 与布局完全不动，只在视觉上叠加玻璃质感——风险最低。
 *
 * ── 诚实边界（必须写清，避免再次"期望它折射桌面却没看到"）──
 * 折射原理是对元素**背后的一份背景副本**（`.lq-backdrop`）做位移变形；
 * 而浏览器**无法采样窗口外的桌面像素**（平台限制）。因此：
 *   * "把桌面模糊后透出"由 Electron 系统材质承担（Win11 DWM acrylic，
 *      本机实测回读 = acrylic(3)，确实生效）；
 *   * 本组件只负责**玻璃自身质感**：边缘折射变形、色散、高光描边。
 * 若要"真的折射桌面"，唯一路线是 desktopCapturer 抓屏 + WebGL 纹理（代价高，后续可选）。
 *
 * ── 性能与可访问性守卫 ──
 * 仅在「开启毛玻璃 + 液态风格 + 动画档位允许 + 窗口聚焦且非拖动中」时启用；
 * 否则渲染 null（零 DOM、零滤镜开销）。
 */
import { useEffect, useState } from "react";
import { useGlass } from "@tomagranate/liquid-glass";
import { useUiStore } from "../store/ui";

/** 是否应当启用折射（毛玻璃开 + 液态风格 + 动画档位允许 + 窗口聚焦且非拖动中）。 */
export function useRefractionEnabled(): boolean {
  const glassmorphism = useUiStore((s) => s.glassmorphism);
  const glassStyle = useUiStore((s) => s.glassStyle);
  const motionLevel = useUiStore((s) => s.motionLevel);
  const [active, setActive] = useState(true);

  useEffect(() => {
    let timer: number | undefined;
    const onFocus = () => setActive(true);
    const onBlur = () => setActive(false);
    // 拖动/缩放期间暂停：Acrylic 本身在拖动时开销较大，叠加滤镜会明显掉帧
    const onResize = () => {
      setActive(false);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setActive(true), 220);
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("resize", onResize);
      window.clearTimeout(timer);
    };
  }, []);

  if (!glassmorphism) return false;
  if ((glassStyle || "liquid") !== "liquid") return false;
  if (motionLevel === "reduced" || motionLevel === "off") return false;
  return active;
}

export interface LiquidGlassOverlayProps {
  /** 圆角（px），需与宿主容器一致，否则折射边缘会错位 */
  radius?: number;
  /** 折射边缘厚度（px）：越大边缘"弯曲"越明显 */
  depth?: number;
  /** 色彩色散强度 0..1（玻璃边缘的彩虹感） */
  chroma?: number;
  className?: string;
}

/**
 * 液态玻璃装饰层：绝对定位铺满父容器，只负责折射与高光。
 * 父容器需 `position: relative`（侧栏/面板已满足）。
 * 未启用时返回 null——不产生任何 DOM 与滤镜开销。
 */
export function LiquidGlassOverlay({
  radius = 12, depth = 14, chroma = 0.35, className,
}: LiquidGlassOverlayProps) {
  const enabled = useRefractionEnabled();
  const g = useGlass<HTMLDivElement>({
    radius,
    depth,
    scale: 24,     // 位移强度：边缘可见但不夸张
    blur: 2,       // 轻微磨砂（真正的桌面模糊由系统材质负责）
    chroma,        // 色散
    rimLight: 0.5, // 边缘受光
  });

  if (!enabled) return null;

  return (
    <div
      ref={g.hostRef as React.RefObject<HTMLDivElement>}
      className={`lq lg-overlay ${className || ""}`}
      style={{
        position: "absolute",
        inset: 0,
        borderRadius: radius,
        overflow: "hidden",
        pointerEvents: "none",
        zIndex: 3, // 盖在面板底色之上、内容之下（内容另有更高层级时不遮挡交互）
      }}
      aria-hidden="true"
    >
      <div ref={g.refractionRef} className="lq-refraction">
        <div ref={g.backdropRef} className="lq-backdrop" />
      </div>
      <div ref={g.sheenRef} className="lq-sheen" />
    </div>
  );
}
