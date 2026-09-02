/** JumpDots：消息区左侧定位横线刻度条（紧凑刻度 + 滚动聚焦自动跟随）。
 * - 紧凑排布：短横线密排（节距 ~9px）左对齐；聚焦线加长加粗，其余刻度不位移（布局零跳变）；
 * - 聚焦态由 MessageFlowCore 按消息流滚动位置计算（scrollspy）自动切换，不再固定取最后一条；
 * - 鼠标 hover 触发平滑水波涟漪阻尼位移动效与摘要卡片浮层；点击快速定位到该 turn。
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

          // 水波涟漪位移与长度（聚焦线更长更粗，其余线不动）
          let tx = 0;
          let width = isActive ? 13 : 8;
          let opacity = isActive ? 1 : 0.3;
          let barHeight = isActive ? "2px" : "1.5px";

          if (diff === 0) {
            tx = 6;
            width = 15;
            opacity = 1;
            barHeight = "2px";
          } else if (diff === 1) {
            tx = 3.5;
            width = 11.5;
            opacity = 0.65;
          } else if (diff === 2) {
            tx = 1.5;
            width = 9.5;
            opacity = 0.45;
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
                    transform: `translateX(${tx}px)`,
                    width: `${width}px`,
                    opacity,
                    height: barHeight,
                    backgroundColor: isActive || diff === 0 ? "var(--text-1)" : "var(--text-3)",
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
