/** DadaLoader —— 嗒嗒加载动画（plan-114-450）。
 * 嗒嗒原地小跳：起跳前挤扁、空中拉长、落地再挤扁，地面投影随高度缩放。
 * 纯 CSS（mascot.css），不依赖 GSAP，可在任意列表/面板的加载槽位里使用；
 * reduced-motion 下静止，只保留文字。槽位尺寸固定，切换加载/内容不引起布局位移。 */
import { Dada } from "../mascot/Dada";

export function DadaLoader({ label = "加载中…", size = 40, inline = false }: {
  label?: string;
  size?: number;
  /** inline：横向排布（嗒嗒在左、文字在右），用于行内小槽位 */
  inline?: boolean;
}) {
  return (
    <div className={"dada-loader" + (inline ? " is-inline" : "")} role="status" aria-live="polite" style={{ ["--dl-size" as string]: `${size}px` }}>
      <span className="dada-loader-figure" aria-hidden>
        <span className="dada-loader-shadow" />
        <span className="dada-loader-hop"><Dada variant="icon" size={size} blink={false} /></span>
      </span>
      {label ? <span className="dada-loader-label">{label}</span> : null}
    </div>
  );
}
