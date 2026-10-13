/** 思考深度（plan-20261009-ui U2，参考图6）：胶囊 + 分档滑块浮窗。
 *
 * 档位来自所选模型在「设置 → 模型」中的配置，按高低序（关闭 → 最大）自动排序后铺在滑轨上；
 * 推荐档下方标注「推荐」。滑块用 ui/StepSlider：可以连续拖动（越过档位即时生效，浮窗保持打开、可一路拖下去），
 * 松手弹簧吸附到最近档位；点轨道、点档位名、键盘 ←→ / Home / End 均可用。
 * 说明：旧菜单里的「默认」实际会回落到模型最高档（冷启动兜底），语义含混，这里不再单列。 */
import { useRef } from "react";
import { Popover, StepSlider, Tooltip } from "../ui";
import { IconBrain, IconSliders } from "../icons";
import { openModelSettings } from "../chat/ModelPicker";
import { effortLabel, sortEfforts } from "./modeMeta";

export function EffortPicker({ open, onToggle, onClose, efforts, value, onChange }: {
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  /** 模型配置的档位（任意顺序） */
  efforts: string[];
  /** 当前生效档位 */
  value: string | null;
  onChange: (v: string) => void;
}) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const sorted = sortEfforts(efforts);
  const idx = value ? sorted.indexOf(value) : -1;
  const sliderIdx = idx >= 0 ? idx : Math.max(0, sorted.length - 1);
  const n = sorted.length;

  return (
    <div className="cc-anchor">
      <Tooltip content={`思考深度：${effortLabel(value)}`} side="top" disabled={open}>
        <button
          ref={btnRef}
          type="button"
          className={"cc-chip cc-effort-chip" + (open ? " is-open" : "")}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label={`思考深度：${effortLabel(value)}`}
          onClick={onToggle}
        >
          <IconBrain size={14} />
          <span className="cc-chip-label">{effortLabel(value)}</span>
        </button>
      </Tooltip>
      <Popover open={open} onClose={onClose} placement="top-end" className="cc-effort-pop" ignoreRefs={[btnRef]} aria-label="思考深度">
        <Tooltip content="深度越高，推理越充分、回答越周全，但耗时与用量也更多" side="top">
          <div className="cc-effort-head">
            <span className="cc-effort-title">思考深度</span>
            <span className="cc-effort-value" key={sorted[sliderIdx]}>{effortLabel(sorted[sliderIdx])}</span>
          </div>
        </Tooltip>
        <div className="cc-effort-slider">
          <span className="cc-effort-end">快</span>
          <StepSlider
            steps={sorted.map((e) => ({ key: e, label: effortLabel(e) }))}
            index={sliderIdx}
            onIndexChange={(i) => { const e = sorted[i]; if (e && e !== value) onChange(e); }}
            disabled={n <= 1}
            showLabels={false}
            flowAtMax
            aria-label="思考深度"
          />
          <span className="cc-effort-end">智能</span>
        </div>
        <Tooltip content="档位来自「设置 → 模型」中该模型的配置" side="bottom">
          <button type="button" className="cc-effort-manage" onClick={() => { onClose(); openModelSettings(); }}>
            <IconSliders size={13} />
            <span>调整可选档位</span>
          </button>
        </Tooltip>
      </Popover>
    </div>
  );
}
