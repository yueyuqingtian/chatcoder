/** JumpDots：消息区左侧定位横线刻度条（紧凑刻度 + 滚动聚焦自动跟随）。
 * - 紧凑排布：短横线密排（节距 ~9px）左对齐；
 * - 聚焦态由 MessageFlowCore 按消息流滚动位置计算（scrollspy）自动切换，不再固定取最后一条；
 * - 鼠标 hover 触发管风琴式**向右阶梯加宽**与摘要卡片浮层；点击快速定位到该 turn。
 *
 * plan-282-1434（B4）：修正此前的两个问题——
 *  1) 横条用 translateX 整体右移（"图4 右移现象"）→ 现在一律左对齐，只让右端变长；
 *  2) hover 时 scrollspy 的聚焦黑条仍叠加显示 → 现在 hover 进行中只高亮鼠标定位那一条。
 */
import { useState, useMemo, useRef } from "react";
import type { TimelineEntry } from "./timeline";
import { turnPreview } from "./timeline";

export function JumpDots({ entries, onJump, activeIndex }: {
  entries: TimelineEntry[];
  onJump: (entry: TimelineEntry) => void;
  /** scrollspy：消息流当前焦点 entry 在 entries 中的下标（视口上 1/3 焦点线所在 entry） */
  activeIndex?: number;
}) {
  const [hovered, setHovered] = useState<number | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  /** 各刻度中心的 clientY 缓存（按纵向升序）。
   *  plan-20261024-jumpfix：此前 pointermove 里逐个 getBoundingClientRect 取中心，
   *  每次指针移动都是 O(刻度数) 次同步布局读取——长会话上百个刻度时每帧强制重排，
   *  鼠标划过定位条会明显掉帧。改为进入轨道时测一次、移动时纯数学取最近，零布局读取。 */
  const centersRef = useRef<number[]>([]);
  /** 缓存对应的刻度数量：会话期间 turns 可能增长（新轮次追加），
   *  数量对不上说明缓存已过期，下一次 hover 前必须重新测量，否则会按旧几何取错刻度。 */
  const centersCountRef = useRef(-1);

  /** 重建刻度中心缓存（一次批量读取，不与写操作交错，只强制一次布局）。 */
  const measureCenters = () => {
    const track = trackRef.current;
    if (!track) return;
    const wraps = track.querySelectorAll<HTMLElement>(".jump-dot-wrap");
    const next: number[] = [];
    wraps.forEach((w) => {
      const r = w.getBoundingClientRect();
      next.push(r.top + r.height / 2);
    });
    centersRef.current = next;
    centersCountRef.current = next.length;
  };

  /** plan-20261024-jumpfix：hover 判定改为「按指针纵向位置取最近刻度」。
   *  此前每个刻度各自 onMouseEnter，刻度节距仅 9px、可点区 8px，指针落在两刻度之间
   *  或偏向相邻刻度时，用户以为指向 A、实际命中的是 B（现象：点倒数第二却跳到倒数第一）。
   *  现在悬停反馈与点击目标用同一几何口径：相邻刻度中心的中点即分界，无盲区。 */
  const hoverNearest = (clientY: number) => {
    // 刻度数变了（新轮次追加 / 切换会话）→ 缓存的几何已过期，先重测一次再判定
    if (centersCountRef.current !== turns.length) measureCenters();
    const centers = centersRef.current;
    if (centers.length === 0) return;
    // centers 按纵向升序（track 是纵向列）：二分找最近的刻度中心，O(log N)。
    // 用二分而非「除节距」的除法——节距是 CSS 里的常量，写进 JS 会与样式脱钩。
    let lo = 0;
    let hi = centers.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (centers[mid] < clientY) lo = mid + 1;
      else hi = mid;
    }
    let best = lo;
    if (lo > 0 && Math.abs(centers[lo - 1] - clientY) <= Math.abs(centers[lo] - clientY)) {
      best = lo - 1;
    }
    setHovered(best);
  };

  const turns = useMemo(() => {
    const out: Array<{ entry: Extract<TimelineEntry, { kind: "turn" }>; entryIdx: number }> = [];
    entries.forEach((e, entryIdx) => {
      if (e.kind !== "turn") return;
      if (!e.items.some((it) => it.kind === "user")) return;
      out.push({ entry: e, entryIdx });
    });
    return out;
  }, [entries]);

  // scrollspy：最后一个 entryIdx <= activeIndex 的 turn 即为聚焦项
  const activeDot = useMemo(() => {
    if (activeIndex == null) return -1;
    let a = -1;
    for (let i = 0; i < turns.length; i++) {
      if (turns[i].entryIdx <= activeIndex) a = i;
      else break;
    }
    return a;
  }, [turns, activeIndex]);

  if (turns.length < 2) return null;

  return (
    <div
      className="jump-dots"
      onMouseLeave={() => setHovered(null)}
    >
      <div
        ref={trackRef}
        className="jump-dots-track"
        /* plan-20261024-jumpfix：进入轨道时测一次刻度中心（一次批量布局），
           之后 pointermove 只做二分查找，不再逐帧强制重排。 */
        onPointerEnter={measureCenters}
        onPointerMove={(e) => hoverNearest(e.clientY)}
      >
        {turns.map((t, i) => {
          const preview = turnPreview(t.entry);
          const isActive = i === activeDot;
          const diff = hovered !== null ? Math.abs(hovered - i) : null;

          /** plan-282-1434（B4）：管风琴改为「向右阶梯加宽」。
           *  - 不再使用 translateX —— 此前 `transform: translateX(tx)` 让整条横线右移（图4 现象）；
           *    现在横条一律左对齐，只让**右端**变长，视觉上像向右伸展的阶梯。
           *  - hover 进行中时不套用 scrollspy 的"加粗+最深色"，只高亮鼠标定位的那一条；
           *    无 hover 时恢复 scrollspy 聚焦表现。 */
          let width = 8;
          let opacity = isActive && diff === null ? 1 : 0.3;
          let barHeight = isActive && diff === null ? "2px" : "1.5px";

          if (diff === 0) {
            width = 16;
            opacity = 1;
            barHeight = "2px";
          } else if (diff === 1) {
            width = 12;
            opacity = 0.7;
          } else if (diff === 2) {
            width = 10;
            opacity = 0.5;
          }

          return (
            <div
              // 乐观消息（turnId 为 null）与真实 turn 的 key 不得相撞：此前回退到下标 i，
              // 会与 turnId 为 1 等数字 key 重复，触发 React 重复 key 警告
              key={t.entry.turnId != null ? `turn-${t.entry.turnId}` : `entry-${t.entryIdx}`}
              className="jump-dot-wrap"
            >
              <button
                className="jump-tick-btn"
                onClick={() => onJump(t.entry)}
                /* plan-64-294：不再挂原生 title——同一 hover 已经弹出 .jump-dot-card
                   摘要卡，再接管一个浮层就是「两个浮窗叠着弹」（用户反馈图）。 */
                aria-label={`跳转到第 ${i + 1} 条`}
                type="button"
              >
                <span
                  className="jump-tick-bar"
                  style={{
                    width: `${width}px`,
                    opacity,
                    height: barHeight,
                    backgroundColor: diff === 0 || (diff === null && isActive) ? "var(--text-1)" : "var(--text-3)",
                  }}
                />
              </button>
              {diff === 0 && preview && (
                <div className="jump-dot-card">
                  <div className="jump-dot-card-index">#{i + 1}</div>
                  <div className="jump-dot-card-text">{preview}</div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
