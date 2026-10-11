/** 用量统计：日期范围选择浮层（plan-91-393：预设胶囊 + 日历区间高亮，替换原生 date 输入）。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { IconCalendar, IconChevronLeft, IconChevronRight } from "../../icons";

export type RangeMode = "today" | "7" | "30" | "month" | "custom";

interface Props {
  mode: RangeMode;
  start: string;
  end: string;
  disabled?: boolean;
  /** 选择完成（预设或自定义起止都确定）时回调 */
  onApply: (mode: RangeMode, start: string, end: string) => void;
}

const PRESETS: Array<{ mode: RangeMode; label: string }> = [
  { mode: "today", label: "今天" },
  { mode: "7", label: "7 天" },
  { mode: "30", label: "30 天" },
  { mode: "month", label: "本月" },
  { mode: "custom", label: "自定义" },
];
const WEEK = ["一", "二", "三", "四", "五", "六", "日"];

const pad = (n: number) => String(n).padStart(2, "0");
export const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

/** 预设对应的起止日期（本地时区），用于触发器文案与日历高亮 */
export function presetRange(mode: RangeMode): [string, string] | null {
  const t = new Date();
  const back = (n: number) => { const d = new Date(t); d.setDate(d.getDate() - n); return ymd(d); };
  if (mode === "today") return [ymd(t), ymd(t)];
  if (mode === "7") return [back(6), ymd(t)];
  if (mode === "30") return [back(29), ymd(t)];
  if (mode === "month") return [ymd(new Date(t.getFullYear(), t.getMonth(), 1)), ymd(t)];
  return null;
}

export function UsageDateRangePicker({ mode, start, end, disabled, onApply }: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const range = presetRange(mode) ?? (start && end ? [start, end] : null);
  const [view, setView] = useState(() => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1); });
  // 自定义选择草稿：先点起点，再点终点
  const [draftStart, setDraftStart] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const today = ymd(new Date());

  // 浮层必须支持点击外部 + Esc 关闭（AGENTS 约定）
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (!rootRef.current?.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey, true); };
  }, [open]);

  useEffect(() => { if (!open) { setDraftStart(null); setHover(null); } }, [open]);

  const cells = useMemo(() => {
    const first = new Date(view.getFullYear(), view.getMonth(), 1);
    const offset = (first.getDay() + 6) % 7; // 周一为首列
    const days = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
    const res: Array<string | null> = Array(offset).fill(null);
    for (let i = 1; i <= days; i++) res.push(ymd(new Date(view.getFullYear(), view.getMonth(), i)));
    while (res.length % 7) res.push(null);
    return res;
  }, [view]);

  // 当前高亮区间：选择中按草稿+悬停预览，否则显示已生效区间
  let lo: string | null = null, hi: string | null = null;
  if (draftStart) {
    const other = hover ?? draftStart;
    [lo, hi] = draftStart <= other ? [draftStart, other] : [other, draftStart];
  } else if (range) {
    [lo, hi] = range;
  }

  const pickDay = (d: string) => {
    if (d > today) return;
    if (!draftStart) { setDraftStart(d); return; }
    const [s, e] = draftStart <= d ? [draftStart, d] : [d, draftStart];
    onApply("custom", s, e);
    setOpen(false);
  };

  const pickPreset = (m: RangeMode) => {
    if (m === "custom") { setDraftStart(null); return; } // 自定义：留在浮层内点选日历
    onApply(m, "", "");
    setOpen(false);
  };

  const label = range
    ? (range[0] === range[1] ? range[0] : `${range[0]} ~ ${range[1]}`)
    : "选择日期范围";
  const presetLabel = PRESETS.find((p) => p.mode === mode)?.label;

  return (
    <div className="usage-drp" ref={rootRef}>
      <button
        type="button"
        className="btn btn-secondary btn-sm usage-drp-trigger"
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <IconCalendar size={14} />
        {mode !== "custom" && <span className="usage-drp-preset">{presetLabel}</span>}
        <span className="usage-drp-range">{label}</span>
      </button>
      {open && (
        <div className="usage-drp-pop" role="dialog" aria-label="选择日期范围">
          <div className="usage-drp-chips">
            {PRESETS.map((p) => (
              <button
                key={p.mode}
                type="button"
                className={`usage-drp-chip ${mode === p.mode && !draftStart ? "active" : ""}`}
                onClick={() => pickPreset(p.mode)}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="usage-drp-head">
            <button type="button" className="btn btn-ghost btn-sm usage-drp-nav" aria-label="上个月"
              onClick={() => setView(new Date(view.getFullYear(), view.getMonth() - 1, 1))}>
              <IconChevronLeft size={14} />
            </button>
            <span className="usage-drp-month">{view.getFullYear()} 年 {view.getMonth() + 1} 月</span>
            <button type="button" className="btn btn-ghost btn-sm usage-drp-nav" aria-label="下个月"
              onClick={() => setView(new Date(view.getFullYear(), view.getMonth() + 1, 1))}>
              <IconChevronRight size={14} />
            </button>
          </div>
          <div className="usage-drp-grid" onMouseLeave={() => setHover(null)}>
            {WEEK.map((w) => <span key={w} className="usage-drp-wd">{w}</span>)}
            {cells.map((d, i) => {
              if (!d) return <span key={`e${i}`} />;
              const inRange = lo && hi && d >= lo && d <= hi;
              const edge = d === lo || d === hi;
              const future = d > today;
              return (
                <button
                  key={d}
                  type="button"
                  disabled={future}
                  className={`usage-drp-day${inRange ? " in" : ""}${edge ? " edge" : ""}${d === today ? " today" : ""}`}
                  onMouseEnter={() => setHover(d)}
                  onClick={() => pickDay(d)}
                >
                  {Number(d.slice(8))}
                </button>
              );
            })}
          </div>
          <div className="usage-drp-hint">
            {draftStart ? `起始 ${draftStart}，请选择结束日期` : "点选两个日期设定自定义区间"}
          </div>
        </div>
      )}
    </div>
  );
}
