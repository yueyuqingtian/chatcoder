/** JumpDots（v4 重写）：§3.3.5 消息区最左侧定位圆点。
 * 只展示有用户消息的 turn（用户发送的消息），而非每个 turn 都显示。
 * - hover 浮窗显示该 turn 首条用户消息摘要
 * - 点击 scrollToIndex
 */
import { useState } from "react";
import type { TimelineEntry } from "./timeline";
import { turnPreview } from "./timeline";

export function JumpDots({ entries, onJump }: {
  entries: TimelineEntry[];
  onJump: (entry: TimelineEntry) => void;
}) {
  const [hovered, setHovered] = useState<number | null>(null);

  const turns = (entries.filter((e) => e.kind === "turn") as Extract<TimelineEntry, { kind: "turn" }>[]).filter(
    (t) => t.items.some((it) => it.kind === "user"),
  );

  if (turns.length < 2) return null;

  return (
    <div className="jump-dots">
      {turns.map((t, i) => {
        const preview = turnPreview(t);
        return (
          <div
            key={i}
            className="jump-dot-wrap"
            onMouseEnter={() => setHovered(i)}
            onMouseLeave={() => setHovered(null)}
          >
            <button
              className="jump-dot primary"
              onClick={() => onJump(t)}
              title={preview || ("第 " + (i + 1) + " 条")}
              aria-label={"跳转到第 " + (i + 1) + " 条"}
            />
            {hovered === i && preview && (
              <div className="jump-dot-tooltip">
                <span className="jump-dot-preview">{preview}</span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
