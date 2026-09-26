/** 浮窗实时行的滚动视口（plan-73-341）。
 *
 * 与主窗 `components/chat/ThinkingTicker` 同构：单行视口内横向追逐行尾，
 * 遇到换行则旧行上滚、新行自下而上进入；两轴互斥（避免双轴叠加成"鬼畜"）。
 *
 * 三处必要简化（宠物页是独立 chunk，不拖主窗依赖）：
 *  ① 不引入主窗 `perf/bus` —— 它用于面板拖拽/窗口几何运动期跳过读写，
 *    宠物窗口没有该场景（拖拽时浮窗本就收起）；
 *  ② 文本**不截断** —— 用户明确要求"刷新内容时不要省略"：完整文本存在 ref 里，
 *    渲染只取当前行，既不丢内容也不产生大数组分配；
 *  ③ 样式类名独立（`.live-ticker*`），仍复用同一套设计令牌。
 *
 * 关键工程点（沿用主窗已踩过的坑）：
 *  · **测量与动画分离**：几何值只由 ResizeObserver 在布局之后写入缓存，
 *    rAF 只写 transform —— 否则每帧读布局属性会强制同步整棵树，拖垮流式渲染；
 *  · **静止停帧**：横滚到位且一段时间无新文本即停止排队，文本/尺寸变化再唤醒（省电）；
 *  · **行数手动计数**：思考文本可达数十 KB，避免每帧 `split` 分配大数组。
 */
import { memo, useEffect, useRef, useState } from "react";

/** 上滚时长：慢速/快刷两档（产字越快，切行越干脆） */
const SWAP_MS_SLOW = 160;
const SWAP_MS_FAST = 90;
/** 产字速率阈值（chars/s），超过视为快刷 */
const FAST_RATE = 80;
/** 静止停帧阈值：横滚到位且该时长内无新文本 ⇒ 停止排队 */
const IDLE_STOP_MS = 400;
/** 行高兜底值（与 .live-ticker-line 的 line-height 一致；正常由 RO 实测覆盖） */
const ROW_H = 18;

export const LiveTicker = memo(function LiveTicker({ text, placeholder = "" }: {
  /** 完整文本（可含换行）——本组件只渲染最后一行，历史行通过上滚动画离开视野 */
  text: string;
  /** 文本为空时的占位（不参与滚动） */
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
  /** 几何测量缓存：仅由 RO 回调（布局后）写入；rAF 循环只读，不触发强制同步布局 */
  const metrics = useRef({ scrollW: 0, viewW: 0, lineH: ROW_H });
  /** rAF 循环启动器：文本/尺寸变化时唤醒（可能是停帧态） */
  const startRef = useRef<() => void>(() => {});

  // 文本变化：EMA 测产字速率 + 提取当前行 + 换行检测（互斥状态机）
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

    // 行数统计：手动计数避免对大文本 split 分配大数组
    let count = text ? 1 : 0;
    for (let i = 0; i < text.length; i++) {
      if (text.charCodeAt(i) === 10) count++;
    }
    const line = text.slice(text.lastIndexOf("\n") + 1);

    if (count < lineCountRef.current) {
      // 内容重置（新一轮思考/消息或落库清缓冲）：直接跳变，不做动画
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
      // 上滚进行中：只更新新行内容，不重启动画（视觉为连续上滚流）
      curRef.current = line;
      setCur(line);
      a.offset = 0; // 新行从头渲染，横滚位移归零
    } else {
      lineCountRef.current = count;
      curRef.current = line;
      setCur(line);
    }
    startRef.current();
  }, [text]);

  // 测量：RO 在布局之后回调（不强制同步布局）；文本增长与容器尺寸变化都会命中
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

  // rAF 循环：只写 transform、只读缓存测量值；两轴互斥；静止停帧
  useEffect(() => {
    const a = anim.current;
    const easeOut = (p: number) => 1 - Math.pow(1 - p, 3);
    const step = (now: number) => {
      a.raf = 0;
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
          // 「横滚」模式：追逐行尾，速度随产字速率自适应
          const target = Math.max(0, metrics.current.scrollW - metrics.current.viewW);
          const speed = Math.min(2000, Math.max(60, a.rate * 10));
          if (a.offset < target) a.offset = Math.min(target, a.offset + speed * dt);
          else if (a.offset > target + 1) a.offset = Math.max(target, a.offset - speed * 2 * dt);
          // 静止停帧：横滚已到位且一段时间无新文本 ⇒ 不再排队（无任何读写）
          if (a.offset === target && now - a.lastGrowTs > IDLE_STOP_MS) return;
        }
        line.style.transform = `translate3d(${-a.offset}px,0,0)`;
        strip.style.transform = `translate3d(0,${swapY}px,0)`;
      }
      a.raf = requestAnimationFrame(step);
    };
    startRef.current = () => {
      if (a.raf) return;
      a.frameTs = 0; // 唤醒后首帧用默认 dt，避免用停帧前的旧时间戳算出大跨步
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
    <span className="live-ticker" ref={viewRef}>
      <span className="live-ticker-strip" ref={stripRef}>
        {prev != null && <span className="live-ticker-line is-prev">{prev}</span>}
        <span className="live-ticker-line" ref={lineRef}>{cur || placeholder}</span>
      </span>
    </span>
  );
});
