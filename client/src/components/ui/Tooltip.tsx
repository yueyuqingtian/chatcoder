/** Tooltip —— 统一轻量提示（Radix Tooltip，plan-282-1416 / plan-41-197 S0/S1）
 *
 * 用于标题栏/侧栏图标按钮、会话标题全文等「悬停查看」场景：以自有浮层替代原生 title，
 * 键盘聚焦同样可见（Radix 原生支持）。
 *
 * S0 调整：展示延迟 400ms → 200ms；视觉为设计化圆角浮层。
 *
 * S1（plan-41-197，本轮）——用户反馈「原生方形丑陋 title 块 + 悬停有明显延迟」：
 *  1. 延迟收紧：首次 --tooltip-delay(120ms)、同排切换 --tooltip-skip-delay(60ms)，均由令牌单源控制；
 *  2. 视觉升级：--r-pop 圆角卡片 + 描边 + shadow-lg + 箭头 + 双向显隐动画（.ui-tooltip）；
 *  3. `title` 便捷属性：直接承接原生 title 语义；
 *  4. `NativeTitleTooltip`：根节点一次性接管**存量原生 title**——指针进入时暂存并移除
 *     原生 title（浏览器自带的方形浮块约 1s 才出现且样式不可控），改由同一套圆角浮层展示，
 *     指针离开后还原属性。存量消费点因此无需逐处改造即可获得统一观感。
 *
 * plan-64-292（本轮修订）——接管后的浮层被反馈「位置不智能 / 内容溢出 / 与原生方块同现」：
 *  A. 定位改为**先测浮层实际尺寸、再决策落点**：默认元素下方居中，下方装不下且上方更宽裕时
 *     翻转到上方，四向均夹取进视口——旧实现按固定 48px 判断翻转、只做水平夹取，
 *     多行长文本（长命令行）在视口上/下边缘会被切掉；
 *  B. 浮层文本允许任意字符折行（长命令/长路径不再撑破卡片），宽度上限随视口收缩；
 *  C. 接管期间用 MutationObserver 守住 title（React 重渲染写回即再次移除），
 *     并以低频巡检跟随元素重挂载（虚拟化回收后按指针位置重新接管）——旧实现只处理
 *     「指针恰好再次移入」，流式重渲染与列表回收场景会让原生方块漏出。
 */
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface TooltipProps {
  /** 浮层内容；与 `title` 二选一（同时给出时以 content 为准） */
  content?: ReactNode;
  /** 便捷属性：等价于原生 title 的纯文本提示（迁移存量 title 用） */
  title?: string | number | null;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  /** 首次展示延迟（ms）；默认取令牌 --tooltip-delay */
  delay?: number;
  disabled?: boolean;
}

/** 读取 CSS 令牌（--tooltip-delay / --tooltip-skip-delay），保证延迟与设计令牌单源 */
function readDelayToken(name: string, fallback: number): number {
  if (typeof window === "undefined") return fallback;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const ms = Number.parseFloat(raw);
  return Number.isFinite(ms) ? ms : fallback;
}

/** 顶层 Provider（App 内挂载一次）；此处保留独立 Provider 以便局部使用不报错 */
export function TooltipProvider({ children }: { children: ReactNode }) {
  return (
    <RadixTooltip.Provider
      delayDuration={readDelayToken("--tooltip-delay", 120)}
      skipDelayDuration={readDelayToken("--tooltip-skip-delay", 60)}
    >
      {children}
    </RadixTooltip.Provider>
  );
}

export function Tooltip({ content, title, children, side = "bottom", delay, disabled = false }: TooltipProps) {
  const body = content ?? (title === null || title === undefined || title === "" ? null : String(title));
  if (disabled || !body) return <>{children}</>;
  return (
    <RadixTooltip.Root delayDuration={delay ?? readDelayToken("--tooltip-delay", 120)}>
      <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
      <RadixTooltip.Portal>
        <RadixTooltip.Content className="ui-tooltip" side={side} sideOffset={8} collisionPadding={8}>
          {body}
          <RadixTooltip.Arrow className="ui-tooltip-arrow" width={12} height={6} />
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}

/* ── 存量原生 title 的统一接管 ───────────────────────────────── */

/** 暂存原生 title 的 dataset 键（接管期间移除原生属性，离开时还原） */
const STASH_KEY = "tipStash";
/** 浮层与触发元素的间距（px） */
const GAP = 8;
/** 浮层距视口边缘的最小留白（px） */
const EDGE = 8;
/** 巡检间隔（ms）：跟随元素重挂载/位置漂移；仅在悬停期间运行，开销可忽略 */
const POLL_MS = 200;

/** 触发元素的视口矩形快照 */
interface AnchorRect { left: number; top: number; right: number; bottom: number; }

/** 可被接管的元素（plan-64-294）：图标按钮的图标是 <svg> 子树，提示属性也可能挂在
 *  svg 根上，因此向上查找与接管都不能只认 HTMLElement——否则指针落在图标上会被判成
 *  「没有 owner」，浮层当场关闭、浏览器原生方形浮块随后漏出。
 *  （HTMLElement | SVGElement 都有 dataset，故 STASH_KEY 仍可挂在 dataset 上。） */
type TipOwner = HTMLElement | SVGElement;

interface NativeTip {
  text: string;
  /** 触发元素矩形（滚动/resize 时同步更新；最终落点 = 本矩形 + 浮层实测尺寸） */
  anchor: AnchorRect;
}

function rectOf(el: Element): AnchorRect {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
}

function rectMoved(a: AnchorRect, b: AnchorRect): boolean {
  return (
    Math.abs(a.left - b.left) > 0.5 || Math.abs(a.top - b.top) > 0.5 ||
    Math.abs(a.right - b.right) > 0.5 || Math.abs(a.bottom - b.bottom) > 0.5
  );
}

function clamp(v: number, min: number, max: number): number {
  return Math.min(Math.max(v, min), max);
}

/** 接管全站原生 `title`：指针进入时改用统一圆角浮层展示（plan-41-197 S1）
 *
 * 只做「纯文本提示」的接管，且仅在指针悬停期间移除原生属性——键盘聚焦行为与
 * 无障碍名称（aria-label / title 播报）不受影响。新增代码仍建议直接用 <Tooltip>。
 *
 * plan-64-292 修订：定位两段式（先测尺寸再决策 + 四向夹取）、文本任意折行防溢出、
 * MutationObserver + 巡检双保险杜绝「原生方块与本浮层同现」。
 */
export function NativeTitleTooltip() {
  const [tip, setTip] = useState<NativeTip | null>(null);
  /** 浮层实测尺寸——拿到后才能算出上下翻转与四向夹取的最终落点 */
  const [box, setBox] = useState<{ w: number; h: number } | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  const ownerRef = useRef<TipOwner | null>(null);
  const guardRef = useRef<MutationObserver | null>(null);
  const timerRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);
  /** 最近一次指针坐标：元素被回收后据此找回当前悬停的元素 */
  const pointRef = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    function cancelTimer() {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    }

    function stopPoll() {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }

    function disarmGuard() {
      guardRef.current?.disconnect();
      guardRef.current = null;
    }

    /** 还原被暂存的原生 title（元素可能已卸载，故做存在性判断） */
    function restore() {
      const el = ownerRef.current;
      if (el && el.dataset[STASH_KEY] !== undefined) {
        el.setAttribute("title", el.dataset[STASH_KEY] ?? "");
        delete el.dataset[STASH_KEY];
      }
      ownerRef.current = null;
    }

    function hide() {
      cancelTimer();
      stopPoll();
      // 守卫自 plan-64-294 起是 document 级常驻监听：只在组件卸载时断开。
      // 若随 hide() 一起断开，「指针已悬停、title 才被写入」这类漏网场景的兜底会当场失效。
      restore();
      setTip(null);
    }

    /** 从事件目标向上找最近的「带原生 title 或已被接管」的元素。
     *
     *  plan-64-294 修复：此前只认 `HTMLElement`，指针落在按钮**中间的图标**
     *  （<svg>/<path>/<circle>）上时直接被判成「没有 owner」，浮层随即被 hide() 关掉；
     *  而浏览器仍认为指针在按钮子树内，约 1s 后弹出原生方形浮块——用户看到的就是
     *  「聚焦按钮中间是新版浮层、挪到边缘/图标上却变回原生方框」。
     *  现在接受任意 Element（含 SVG 及其图形子元素）并沿父链上溯。 */
    function findOwner(target: EventTarget | null): TipOwner | null {
      const start = target instanceof Element
        ? target
        : target instanceof Node
          ? target.parentElement
          : null;
      let el: Element | null = start;
      while (el && el !== document.documentElement) {
        if ((el instanceof HTMLElement || el instanceof SVGElement)
          && (el.dataset[STASH_KEY] !== undefined || el.hasAttribute("title"))) return el;
        el = el.parentElement;
      }
      return null;
    }

    /** 展示：记录文本与锚点（落点由渲染期的 useMemo 依实测尺寸算出） */
    function measure(el: TipOwner, text: string) {
      if (!el.isConnected) return; // 元素已被回收：不展示陈旧位置
      setTip({ text, anchor: rectOf(el) });
    }

    /** 位置同步：元素在同一视口内移动（滚动/布局变化）时跟随重算 */
    function syncAnchor(el: TipOwner) {
      if (!el.isConnected) return;
      const anchor = rectOf(el);
      setTip((prev) => (prev && rectMoved(prev.anchor, anchor) ? { ...prev, anchor } : prev));
    }

    /** 强制刷新锚点（resize 场景：即使元素没动，视口尺寸也变了，需要重算落点） */
    function refreshAnchor(el: TipOwner) {
      if (!el.isConnected) return;
      const anchor = rectOf(el);
      setTip((prev) => (prev ? { ...prev, anchor } : prev));
    }

    /** 接管守卫（plan-64-294：由「元素级、按需挂」升级为 document 级常驻监听）——
     *  只观察 title 属性变化，覆盖两类会让原生方形浮块漏出的场景：
     *  ① 接管期间 React 把 title 写回 DOM（原元素级 observer 已覆盖）；
     *  ② 指针**已经停在该元素上**之后 title 才被写入（如进度标题由空字符串变成有值、
     *     流式重渲染刷新了提示文本）：此时不会再触发 pointerover，元素级 observer
     *     也来不及挂上，旧实现只能等浏览器原生方块冒出来。
     *     这里按「最后已知指针坐标」做一次命中测试，命中才接管、且不再等延迟。 */
    function startGuard() {
      disarmGuard();
      const mo = new MutationObserver((records) => {
        for (const r of records) {
          const el = r.target;
          if (!(el instanceof HTMLElement || el instanceof SVGElement)) continue;
          const fresh = el.getAttribute("title");
          if (fresh == null) continue; // title 已被移除（含我们自己的那一次），无需处理
          if (el === ownerRef.current) {
            el.removeAttribute("title");
            const text = fresh.trim();
            if (!text) continue;
            el.dataset[STASH_KEY] = text;
            setTip((prev) => (prev && prev.text !== text ? { ...prev, text } : prev));
            continue;
          }
          if (ownerRef.current) continue; // 正在接管别的元素：不抢占
          const text = fresh.trim();
          if (!text) continue;
          const pt = pointRef.current;
          // 指针不在视口内：不打扰（坐标取自最后一次 pointerover/pointermove）
          if (!pt || pt.x < 0 || pt.y < 0 || pt.x > window.innerWidth || pt.y > window.innerHeight) continue;
          const under = document.elementFromPoint(pt.x, pt.y);
          if (under && (under === el || el.contains(under))) attach(el, true);
        }
      });
      mo.observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ["title"] });
      guardRef.current = mo;
    }

    /** 巡检：元素被重挂载/回收（虚拟化、流式重渲染）时按指针位置重新接管；
     *  仍在文档中则同步锚点。仅悬停期间运行，避免常驻开销。 */
    function startPoll() {
      if (pollRef.current !== null) return;
      pollRef.current = window.setInterval(() => {
        const owner = ownerRef.current;
        if (!owner) { stopPoll(); return; }
        if (!owner.isConnected) {
          const pt = pointRef.current;
          const under = pt ? document.elementFromPoint(pt.x, pt.y) : null;
          const next = findOwner(under);
          if (next && next !== owner) {
            const text = (next.dataset[STASH_KEY] ?? next.getAttribute("title") ?? "").trim();
            // 用户本来就在悬停：直接展示，不再等一次延迟
            if (text) { attach(next, true); return; }
          }
          hide();
          return;
        }
        syncAnchor(owner);
      }, POLL_MS);
    }

    /** 接管一个元素：暂存并移除原生 title，启动巡检，随后展示浮层 */
    function attach(el: TipOwner, immediate = false) {
      const text = (el.dataset[STASH_KEY] ?? el.getAttribute("title") ?? "").trim();
      if (!text) return;
      // 暂存并移除原生 title：否则浏览器的方形浮块会与本浮层同时出现
      el.dataset[STASH_KEY] = text;
      el.removeAttribute("title");
      ownerRef.current = el;
      startPoll();
      cancelTimer();
      if (immediate) measure(el, text);
      else timerRef.current = window.setTimeout(() => measure(el, text), readDelayToken("--tooltip-delay", 120));
    }

    function onOver(ev: PointerEvent) {
      pointRef.current = { x: ev.clientX, y: ev.clientY };
      const owner = findOwner(ev.target);
      if (owner && owner === ownerRef.current) { syncAnchor(owner); return; } // 元素内移动：沿用当前浮层
      hide();
      if (owner) attach(owner);
    }

    function onOut(ev: PointerEvent) {
      const owner = ownerRef.current;
      if (!owner) return;
      const next = ev.relatedTarget;
      if (next instanceof Node && owner.contains(next)) return; // 移入子元素不算离开
      hide();
    }

    /** 指针在元素内移动时同步坐标：元素被回收后按「最后已知位置」找回接管目标 */
    function onMove(ev: PointerEvent) {
      if (ownerRef.current) pointRef.current = { x: ev.clientX, y: ev.clientY };
    }

    /** 滚动时元素位置会变：跟随重算（采集阶段，覆盖内层滚动容器） */
    function onScroll() {
      const owner = ownerRef.current;
      if (!owner) return;
      if (!owner.isConnected) { hide(); return; }
      syncAnchor(owner);
    }

    function onResize() {
      const owner = ownerRef.current;
      if (!owner) return;
      if (!owner.isConnected) { hide(); return; }
      refreshAnchor(owner);
    }

    startGuard();
    document.addEventListener("pointerover", onOver, true);
    document.addEventListener("pointerout", onOut, true);
    document.addEventListener("pointermove", onMove, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("pointerover", onOver, true);
      document.removeEventListener("pointerout", onOut, true);
      document.removeEventListener("pointermove", onMove, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
      hide();
      disarmGuard();
    };
  }, []);

  /** 最终落点：默认元素下方居中；下方装不下且上方更宽裕时翻转到上方；
   *  四向夹取进视口（旧实现只做水平夹取，长浮层贴边时会被切掉）。 */
  const pos = useMemo(() => {
    if (!tip) return null;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const w = box?.w ?? 0;
    const h = box?.h ?? 0;
    const a = tip.anchor;
    const spaceBelow = vh - EDGE - (a.bottom + GAP);
    const spaceAbove = a.top - GAP - EDGE;
    const below = h <= spaceBelow || spaceBelow >= spaceAbove;
    const top = below ? a.bottom + GAP : a.top - GAP - h;
    return {
      left: clamp(a.left + (a.right - a.left) / 2 - w / 2, EDGE, Math.max(EDGE, vw - EDGE - w)),
      top: clamp(top, EDGE, Math.max(EDGE, vh - EDGE - h)),
    };
  }, [tip, box]);

  /** 测量浮层实际尺寸：首帧测得后重算落点（useLayoutEffect 在 paint 前完成，用户无感知）。
   *
   *  plan-64-293 修复——此前的写法是 `useLayoutEffect(..., [tip, box])` 里 `setBox(...)`：
   *  box 既是输入又是输出，再加固定定位元素「宽度随 left 变化」的 shrink-to-fit 特性，
   *  测量与落点互相反馈，嵌套更新一路撞到 React 上限（错误 #185）。三处一起收口：
   *  ① 依赖只留 tip（输入），输出绝不参与依赖；
   *  ② 尺寸未变则**不派发** setState（用 ref 比对，比依赖 React 的同值 bail-out 更早一步）；
   *  ③ 用 offsetWidth/offsetHeight 代替 getBoundingClientRect——后者含 CSS transform
   *     （入场缩放动画期间逐帧变化），前者只反映布局尺寸；配合 CSS 侧改 transform 定位，
   *     布局尺寸与落点彻底解耦（见 components.css 的 .ui-tooltip-anchor）。
   *  尺寸的后续变化（文本更长、视口收窄）由 ResizeObserver 补测。 */
  const boxRef = useRef<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const el = innerRef.current;
    if (!tip || !el) return;
    const measure = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const prev = boxRef.current;
      if (prev && prev.w === w && prev.h === h) return; // 尺寸未变：不派发任何更新
      boxRef.current = { w, h };
      setBox({ w, h });
    };
    measure();
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(measure);
      ro.observe(el);
      return () => ro.disconnect();
    }
  }, [tip]);

  if (!tip || !pos) return null;
  // Portal 到 body：固定定位不受祖先 transform/滤镜影响（面板/页面切换动画会改变包含块）。
  // plan-64-293：位移走 transform、top/left 固定为 0——若用 left/top 定位，落点变化会改变
  // 固定定位元素的可用宽度（shrink-to-fit），实测尺寸随之变化并反过来改落点，形成无限
  // 嵌套更新（错误 #185）。transform 不参与布局，测量与定位因此互不干扰。
  return createPortal(
    <div
      className="ui-tooltip-anchor"
      style={{ transform: `translate3d(${pos.left}px, ${pos.top}px, 0)` }}
    >
      <div ref={innerRef} className="ui-tooltip" role="tooltip">{tip.text}</div>
    </div>,
    document.body,
  );
}
