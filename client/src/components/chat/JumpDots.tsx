/** JumpDots：消息区左侧定位横线刻度条（紧凑刻度 + 滚动聚焦自动跟随）。
 * - 紧凑排布：短横线密排（节距 ~9px）左对齐；
 * - 聚焦态由 MessageFlowCore 按消息流滚动位置计算（scrollspy）自动切换，不再固定取最后一条；
 * - 鼠标 hover 触发管风琴式**向右阶梯加宽**与摘要卡片浮层；点击快速定位到该 turn。
 *
 * plan-282-1434（B4）：修正此前的两个问题——
 *  1) 横条用 translateX 整体右移（"图4 右移现象"）→ 现在一律左对齐，只让右端变长；
 *  2) hover 时 scrollspy 的聚焦黑条仍叠加显示 → 现在 hover 进行中只高亮鼠标定位那一条。
 */
import { useState, useMemo } from "react";
import type { TimelineEntry } from "./timeline";
import { turnPreview } from "./timeline";

export function JumpDots({ entries, onJump, activeIndex }: {
  entries: TimelineEntry[];
  onJump: (entry: TimelineEntry) => void;
  /** scrollspy：消息流当前焦点 entry 在 entries 中的下标（视口上 1/3 焦点线所在 entry） */
  activeIndex?: number;
}) {
  const [hovered, setHovered] = useState<number | null>(null);

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
      <div className="jump-dots-track">
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
              key={t.entry.turnId ?? i}
              className="jump-dot-wrap"
              onMouseEnter={() => setHovered(i)}
            >
              <button
                className="jump-tick-btn"
                onClick={() => onJump(t.entry)}
                title={preview || `第 ${i + 1} 条`}
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
