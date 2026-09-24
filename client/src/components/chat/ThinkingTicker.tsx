/** ThinkingTicker（v41）：思考流单行滚动视口——ThinkingBlock 与 StreamingText 尾部共用。
 * - 当前行随流式 delta 向右渲染；行超宽后从右往左滚动追逐行尾；
 * - v41 互斥状态机：横滚与换行上滚同一时刻只进行一种——换行触发时横向位移冻结归零，
 *   上滚动画期间完全不做横滚，结束后才恢复（避免双轴叠加造成鬼畜）；
 * - v41 换行合并：一帧内到达多个换行只做一次上滚；上滚进行中再来新行只更新新行内容，
 *   不重启动画（视觉为连续上滚流）；
 * - v41 速度自适应：产字越快横滚追逐越快（上限 2000px/s）、上滚时长越短（160ms→90ms），
 *   快刷时"一行快速渲染→快速滚到下一行"。
 * - plan-334-1661 修复：**测量与动画分离**。
 *   原实现每帧在 rAF 里读 3 个布局属性（line.scrollWidth / view.clientWidth / line.offsetHeight）：
 *   流式期间这些读会把"浏览器尚未结算的整棵消息树脏布局"强制同步结算一次，是流式掉帧与
 *   拖面板卡顿的固定成本。现在几何值全部由 ResizeObserver 在**布局之后**测量并缓存
 *   （该时机不会触发强制同步布局），rAF 只做 transform 写入；文本增长与容器尺寸变化
 *   都会经 RO 更新目标值并唤醒停帧中的循环。
 * - 静止停帧：横滚到位且一段时间无新文本时停止排队（布局读/写入全免），文本或尺寸变化唤醒。
 * - `will-change: transform` 由 CSS 常驻（`.thinking-ticker-strip/.thinking-ticker-line`）：
 *   本组件最多同时存在 1~2 个实例，常驻合成层成本远低于滚动开始时的重新光栅化。
 */
import { memo, useEffect, useRef, useState } from "react";
import { isBusy } from "../../perf/bus";

const SWAP_MS_SLOW = 160;
const SWAP_MS_FAST = 90;
const FAST_RATE = 80; // chars/s，超过视为快刷
/** 静止停帧：横滚到位且该时长内无新文本 ⇒ 停止排队，等唤醒。 */
const IDLE_STOP_MS = 400;
/** 行高兜底值（与 .thinking-ticker-line 的 line-height 一致；正常由 RO 实测覆盖）。 */
const ROW_H = 18;

export const ThinkingTicker = memo(function ThinkingTicker({ text, placeholder = "正在深入思考…" }: {
  text: string;
  placeholder?: string;
}) {
  const viewRef = useRef<HTMLSpanElement>(null);
  const stripRef = useRef<HTMLSpanElement>(null);
  const lineRef = useRef<HTMLSpanElement>(null);
  const [cur, setCur] = useState("");
  const [prev, setPrev] = useState<string | null>(null);
  const curRef = useRef("");
  const prevRef = useRef<string | null>(null);
  const swapRef = useRef<{ start: number; dur: number } | null>(null);
  const lineCountRef = useRef(0);
  const anim = useRef({
    offset: 0,
    rate: 0,
    lastLen: 0,
    lastTs: 0,
    frameTs: 0,
    raf: 0,
    /** 最近一次文本增长时间（静止停帧判据） */
    lastGrowTs: 0,
  });
  /** 几何测量缓存：仅由 RO 回调（布局后）写入；rAF 循环只读，不触发强制同步布局。 */
  const metrics = useRef({ scrollW: 0, viewW: 0, lineH: ROW_H });
  /** rAF 循环启动器：由 rAF effect 赋值，文本/尺寸变化时唤醒（停帧态）。 */
  const startRef = useRef<() => void>(() => {});

  // 文本变化：EMA 测产字速率（chars/s）+ 当前行提取 + 换行检测（互斥状态机）
  useEffect(() => {
    const a = anim.current;
    const now = performance.now();
    a.lastGrowTs = now; // 文本有变化即视为"活着"，唤醒可能已停帧的循环
    if (a.lastTs) {
      const dt = (now - a.lastTs) / 1000;
      if (dt > 0.008) {
        const inst = Math.max(0, text.length - a.lastLen) / dt;
        a.rate = a.rate > 0 ? a.rate * 0.65 + inst * 0.35 : inst;
      }
    }
    a.lastLen = text.length;
    a.lastTs = now;

    // 行数统计：手动计数避免每帧 split 分配大数组（思考文本可达数十 KB）
    let count = text ? 1 : 0;
    for (let i = 0; i < text.length; i++) {
      if (text.charCodeAt(i) === 10) count++;
    }
    const line = text.slice(text.lastIndexOf("\n") + 1);

    if (count < lineCountRef.current) {
      // 内容重置（新一段思考/落库清缓冲）：直接跳变
      lineCountRef.current = count;
      curRef.current = line;
      setCur(line);
      prevRef.current = null;
      setPrev(null);
      swapRef.current = null;
      a.offset = 0;
    } else if (count > lineCountRef.current && lineCountRef.current > 0) {
      // 换行 → 进入「上滚」模式（横滚冻结）
      lineCountRef.current = count;
      if (!swapRef.current) {
        // 无进行中的上滚：旧行上滚出视口，新行自下而上进入
        prevRef.current = curRef.current;
        setPrev(curRef.current);
        swapRef.current = {
          start: now,
          dur: a.rate > FAST_RATE ? SWAP_MS_FAST : SWAP_MS_SLOW,
        };
      }
      // 上滚进行中：只更新新行内容，不重启动画（连续上滚流）
      curRef.current = line;
      setCur(line);
      a.offset = 0; // 新行从头渲染，横滚位移归零
    } else {
      lineCountRef.current = count;
      curRef.current = line;
      setCur(line);
    }
    startRef.current(); // 有新文本 → 唤醒循环（测量由 RO 更新）
  }, [text]);

  // 测量：RO 在**布局之后**回调（不会强制同步布局），文本增长 / 容器宽度变化都会命中；
  // 命中即更新目标值并唤醒停帧循环。
  useEffect(() => {
    const view = viewRef.current;
    const line = lineRef.current;
    if (!view || !line || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      metrics.current.viewW = view.clientWidth;
      metrics.current.scrollW = line.scrollWidth;
      metrics.current.lineH = line.offsetHeight || ROW_H;
    };
    measure(); // 初次：commit 后布局已就绪，仅此一次同步读
    const ro = new ResizeObserver(() => {
      measure();
      startRef.current();
    });
    ro.observe(view);
    ro.observe(line);
    return () => ro.disconnect();
  }, []);

  // rAF 循环：只写 transform、只读缓存测量值；上滚期间不做横滚（互斥）；
  // 横滚到位且长时间无新文本 ⇒ 停帧，等 startRef 唤醒。
  useEffect(() => {
    const a = anim.current;
    const easeOut = (p: number) => 1 - Math.pow(1 - p, 3);
    const step = (now: number) => {
      a.raf = 0;
      // 几何运动期（拖窗口/拖面板/面板过渡）只排队、不做任何读写：
      // 结束后的下一帧自然追上——上滚按 start/dur 推进（超时即完成），横滚按 dt 补偿。
      if (isBusy()) {
        a.raf = requestAnimationFrame(step);
        return;
      }
      const view = viewRef.current;
      const line = lineRef.current;
      const strip = stripRef.current;
      const dt = a.frameTs ? Math.min(0.1, (now - a.frameTs) / 1000) : 0.016;
      a.frameTs = now;
      if (view && line && strip) {
        let swapY = 0;
        if (swapRef.current) {
          // 「上滚」模式：横向冻结（offset 已归零），仅推进纵向动画
          const p = (now - swapRef.current.start) / swapRef.current.dur;
          if (p >= 1) {
            swapRef.current = null;
            if (prevRef.current != null) {
              prevRef.current = null;
              setPrev(null);
            }
          } else {
            swapY = -metrics.current.lineH * easeOut(Math.max(0, p));
          }
        } else {
          // 「横滚」模式：追逐行尾，速度随产字速率自适应（目标值来自 RO 测量缓存）
          const target = Math.max(0, metrics.current.scrollW - metrics.current.viewW);
          const speed = Math.min(2000, Math.max(60, a.rate * 10));
          if (a.offset < target) a.offset = Math.min(target, a.offset + speed * dt);
          else if (a.offset > target + 1) a.offset = Math.max(target, a.offset - speed * 2 * dt);
          // 静止停帧：横滚已到位且一段时间无新文本 ⇒ 不再排队（无读写）；
          // 文本变化与尺寸变化都会重新唤醒循环。
          if (a.offset === target && now - a.lastGrowTs > IDLE_STOP_MS) return;
        }
        line.style.transform = `translate3d(${-a.offset}px,0,0)`;
        strip.style.transform = `translate3d(0,${swapY}px,0)`;
      }
      a.raf = requestAnimationFrame(step);
    };
    startRef.current = () => {
      if (a.raf) return;
      a.frameTs = 0; // 唤醒后首帧用默认 dt（避免用停帧前的旧时间戳算出大跨步）
      a.raf = requestAnimationFrame(step);
    };
    startRef.current();
    return () => {
      if (a.raf) cancelAnimationFrame(a.raf);
      a.raf = 0;
      startRef.current = () => {};
    };
  }, []);

  return (
    <span className="thinking-ticker" ref={viewRef}>
      <span className="thinking-ticker-strip" ref={stripRef}>
        {prev != null && <span className="thinking-ticker-line is-prev">{prev}</span>}
        <span className="thinking-ticker-line" ref={lineRef}>{cur || placeholder}</span>
      </span>
    </span>
  );
});
