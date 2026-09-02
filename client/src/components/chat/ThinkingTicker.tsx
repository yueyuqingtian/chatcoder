/** ThinkingTicker（v41）：思考流单行滚动视口——ThinkingBlock 与 StreamingText 尾部共用。
 * - 当前行随流式 delta 向右渲染；行超宽后从右往左滚动追逐行尾；
 * - v41 互斥状态机：横滚与换行上滚同一时刻只进行一种——换行触发时横向位移冻结归零，
 *   上滚动画期间完全不做横滚，结束后才恢复（避免双轴叠加造成鬼畜）；
 * - v41 换行合并：一帧内到达多个换行只做一次上滚；上滚进行中再来新行只更新新行内容，
 *   不重启动画（视觉为连续上滚流）；
 * - v41 速度自适应：产字越快横滚追逐越快（上限 2000px/s）、上滚时长越短（160ms→90ms），
 *   快刷时"一行快速渲染→快速滚到下一行"。
 */
import { memo, useEffect, useRef, useState } from "react";

const SWAP_MS_SLOW = 160;
const SWAP_MS_FAST = 90;
const FAST_RATE = 80; // chars/s，超过视为快刷

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
  const anim = useRef({ offset: 0, rate: 0, lastLen: 0, lastTs: 0, frameTs: 0, raf: 0 });

  // 文本变化：EMA 测产字速率（chars/s）+ 当前行提取 + 换行检测（互斥状态机）
  useEffect(() => {
    const a = anim.current;
    const now = performance.now();
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
  }, [text]);

  // rAF 循环：上滚期间不做横滚（互斥）；上滚结束后横滚以自适应速度追逐行尾
  useEffect(() => {
    const a = anim.current;
    const easeOut = (p: number) => 1 - Math.pow(1 - p, 3);
    const step = (now: number) => {
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
            swapY = -(line.offsetHeight || 18) * easeOut(Math.max(0, p));
          }
        } else {
          // 「横滚」模式：追逐行尾，速度随产字速率自适应
          const target = Math.max(0, line.scrollWidth - view.clientWidth);
          const speed = Math.min(2000, Math.max(60, a.rate * 10));
          if (a.offset < target) a.offset = Math.min(target, a.offset + speed * dt);
          else if (a.offset > target + 1) a.offset = Math.max(target, a.offset - speed * 2 * dt);
        }
        line.style.transform = `translate3d(${-a.offset}px,0,0)`;
        strip.style.transform = `translate3d(0,${swapY}px,0)`;
      }
      a.raf = requestAnimationFrame(step);
    };
    a.raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(a.raf);
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
