/** StepSlider —— 分档滑块（plan-20261009-ui U2/U5）。
 *
 * 与 ui/Slider（连续数值）的区别：值是「第几档」。关键体验：
 *  · 可以**连续拖动**：拇指牢牢跟手，不被档位吸附；越过每个档位时实时回报，调用方立刻生效，浮窗保持打开；
 *  · 松手后拇指以弹簧吸附到最近的档位；点轨道 / 点档位名 / 键盘（←→↑↓、Home、End、PageUp/Down）同样是弹簧过渡；
 *  · 反馈：拖动时拇指微微放大，每越过一档拇指轻颤、对应档位点弹一下，已走过的轨道随拇指实时填充；
 *  · 动效时长全部来自令牌（--dur-spring），随「动画效果」档位缩放；拖动期间不加过渡，保证 1:1 跟手。
 * 几何：两端各让出半个拇指宽度（--th），档位点 / 档位名都按同一公式与拇指中心对齐。 */
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { PixelFlow } from "./PixelFlow";

export interface StepSliderStep {
  key: string;
  label: ReactNode;
  /** 档位名下方的小标注（如「推荐」） */
  badge?: ReactNode;
}

/** 半个拇指宽度（px）——与 CSS 的 --th 同值，指针位置据此换算成档位 */
const TH = 11;

export function StepSlider({ steps, index, onIndexChange, disabled = false, className = "", showLabels = true, flowAtMax = false, ...rest }: {
  steps: StepSliderStep[];
  /** 当前档位下标 */
  index: number;
  /** 越过档位时回报（拖动中实时调用；调用方应立即生效） */
  onIndexChange: (i: number) => void;
  disabled?: boolean;
  className?: string;
  /** plan-128-518：是否在轨道下方显示每档名称（默认显示） */
  showLabels?: boolean;
  /** plan-128-518：拉到最高档时轨道内出现“从右往左”的粒子流动（仅 CSS 动画） */
  flowAtMax?: boolean;
  "aria-label"?: string;
}) {
  const n = steps.length;
  const max = Math.max(0, n - 1);
  const rootRef = useRef<HTMLDivElement>(null);
  /** 拖动中的连续位置（0..max 的小数）；null = 不在拖动，拇指停在 index 上 */
  const [drag, setDrag] = useState<number | null>(null);
  /** 刚按下的前 160ms：拇指带一小段过渡「滑」到按下点，而不是瞬移 */
  const [jump, setJump] = useState(false);
  /** 每越过一档 +1：拇指内核据此重挂载以重放「轻颤」 */
  const [tick, setTick] = useState(0);
  const indexRef = useRef(index);
  indexRef.current = index;
  const dragRef = useRef<number | null>(null);
  const jumpTimer = useRef(0);
  const clamp = (v: number) => Math.max(0, Math.min(max, v));

  useEffect(() => () => window.clearTimeout(jumpTimer.current), []);

  const posFromX = (clientX: number): number => {
    const r = rootRef.current?.getBoundingClientRect();
    if (!r || n <= 1) return 0;
    const frac = (clientX - r.left - TH) / Math.max(1, r.width - 2 * TH);
    return clamp(frac * max);
  };

  /** 拖动 / 键盘 / 点击都经这里：最近档位变了就回报，并触发一次「轻颤」 */
  const report = (pos: number) => {
    const i = Math.round(clamp(pos));
    if (i !== indexRef.current) {
      indexRef.current = i;
      setTick((t) => t + 1);
      onIndexChange(i);
    }
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (disabled || e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const pos = posFromX(e.clientX);
    dragRef.current = pos;
    setDrag(pos);
    setJump(true);
    window.clearTimeout(jumpTimer.current);
    jumpTimer.current = window.setTimeout(() => setJump(false), 160);
    report(pos);
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current == null) return;
    const pos = posFromX(e.clientX);
    dragRef.current = pos;
    setDrag(pos);
    report(pos);
  };
  const endDrag = () => {
    if (dragRef.current == null) return;
    const i = Math.round(clamp(dragRef.current));
    dragRef.current = null;
    setDrag(null); // 松手：拇指由 index 接管，CSS 过渡把它弹到档位上
    report(i);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (disabled) return;
    const cur = indexRef.current;
    let next = cur;
    if (e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "PageUp") next = cur + 1;
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown" || e.key === "PageDown") next = cur - 1;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = max;
    else return;
    e.preventDefault();
    report(clamp(next));
  };

  const shown = drag ?? clamp(index);
  const near = Math.round(shown);
  const frac = (i: number) => (n <= 1 ? 0.5 : i / max);

  return (
    <div
      className={`ui-step${drag != null ? " is-dragging" : ""}${jump ? " is-jump" : ""}${disabled ? " is-disabled" : ""}${flowAtMax && n > 1 && near === max ? " is-max" : ""} ${className}`.trim()}
      style={{ ["--th" as string]: `${TH}px`, ["--pos" as string]: shown, ["--n1" as string]: Math.max(1, max) }}
    >
      <div
        ref={rootRef}
        className="ui-step-slider"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <div className="ui-step-track">
          {/* plan-128-518：最高档的像素点阵流动（参考 Claude 的努力程度滑块）：仅在最高档挂载 canvas，离开即卸载，没有常驻开销 */}
          {flowAtMax && n > 1 && near === max && <PixelFlow className="ui-step-flow" />}
          <span className="ui-step-range" />
          {steps.map((s, i) => (
            <span
              key={s.key}
              className={"ui-step-stop" + (i <= shown + 0.001 ? " is-on" : "") + (i === near ? " is-near" : "")}
              style={{ ["--p" as string]: frac(i) }}
              aria-hidden
            />
          ))}
        </div>
        <span
          className="ui-step-thumb"
          role="slider"
          tabIndex={disabled ? -1 : 0}
          aria-label={rest["aria-label"]}
          aria-orientation="horizontal"
          aria-valuemin={0}
          aria-valuemax={max}
          aria-valuenow={clamp(index)}
          aria-valuetext={typeof steps[near]?.label === "string" ? (steps[near]?.label as string) : undefined}
          aria-disabled={disabled || undefined}
          onKeyDown={onKeyDown}
        >
          <span className="ui-step-core" key={tick} data-hit={tick > 0 ? 1 : 0} />
        </span>
      </div>
      {showLabels && <div className="ui-step-labels">
        {steps.map((s, i) => (
          <button
            key={s.key}
            type="button"
            tabIndex={-1}
            disabled={disabled}
            className={"ui-step-tick" + (i === near ? " is-active" : "")}
            style={{ ["--p" as string]: frac(i) }}
            onClick={() => report(i)}
          >
            {s.label}
            {s.badge != null && <span className="ui-step-badge">{s.badge}</span>}
          </button>
        ))}
      </div>}
    </div>
  );
}
