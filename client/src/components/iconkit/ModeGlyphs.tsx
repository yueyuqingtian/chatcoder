/** 执行模式 / 权限模式图标（plan-128-518）。
 *  统一规格：24 网格、1.6 描边、圆角端点；主形状 + 一处 .lx-fill 淡面作为层次，颜色全走 currentColor，
 *  由宿主行状态控制（默认次级色、选中主色、危险项错误色）。悬停微动复用 icons.css 的 .lx-* 约定，
 *  只动点睛部件，不位移整枚图标。 */
import type { ReactNode } from "react";
import { opticalStroke } from "../icons";

type P = { size?: number };

function Glyph({ size = 16, name, children }: P & { name: string; children: ReactNode }) {
  return (
    // plan-20261014-fix4：描边改为按尺寸换算的光学线宽，与其余界面图标同一笔触
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={opticalStroke(size)}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden className={`ic lx lx-${name}`}>
      {children}
    </svg>
  );
}

/** 智能体：中心实心节点 + 四向短线（自主执行），悬停时射线转 45° */
export function MgAgent(p: P) {
  return <Glyph {...p} name="mg-agent">
    <circle className="lx-fill" cx="12" cy="12" r="4.2" />
    <g className="lx-spin"><path d="M12 3.5v3M12 17.5v3M3.5 12h3M17.5 12h3" /></g>
  </Glyph>;
}
/** 计划：清单卡片，首行已勾选 */
export function MgPlan(p: P) {
  return <Glyph {...p} name="mg-plan">
    <rect className="lx-fill" x="4.5" y="3.5" width="15" height="17" rx="3" />
    <path className="lx-draw" d="M8.2 9.2l1.5 1.5 2.7-2.9" />
    <path d="M14.4 9.6h2M8.2 15h8" />
  </Glyph>;
}
/** 只读：眼睛 + 底部短横（锁定观看），悬停时瞳孔轻移 */
export function MgReadonly(p: P) {
  return <Glyph {...p} name="mg-readonly">
    <path className="lx-fill" d="M3 11.2s3.2-5.7 9-5.7 9 5.7 9 5.7-3.2 5.7-9 5.7-9-5.7-9-5.7z" />
    <circle className="lx-nudge" cx="12" cy="11.2" r="2.5" />
    <path d="M8.5 20.5h7" />
  </Glyph>;
}
/** 目标：同心靶环 + 命中的箭，悬停时箭尾弹一下 */
export function MgGoal(p: P) {
  return <Glyph {...p} name="mg-goal">
    <circle className="lx-fill" cx="11" cy="13" r="7.5" />
    <circle cx="11" cy="13" r="3.6" />
    <path className="lx-nudge" d="M11 13l7-7M18 6V3.2M18 6h2.8" />
  </Glyph>;
}

/** 每次询问：盾牌内问号 */
export function MgAsk(p: P) {
  return <Glyph {...p} name="mg-ask">
    <path className="lx-fill" d="M12 3.4l7 2.7v5.3c0 4.3-2.9 7.5-7 9.2-4.1-1.7-7-4.9-7-9.2V6.1z" />
    <path className="lx-wiggle" d="M9.9 9.9a2.2 2.2 0 1 1 3.3 1.9c-.8.5-1.2 1-1.2 1.8M12 16.3h.01" />
  </Glyph>;
}
/** 自动审批：盾牌内闪电 */
export function MgAuto(p: P) {
  return <Glyph {...p} name="mg-auto">
    <path className="lx-fill" d="M12 3.4l7 2.7v5.3c0 4.3-2.9 7.5-7 9.2-4.1-1.7-7-4.9-7-9.2V6.1z" />
    <path className="lx-wiggle" d="M12.8 8.2l-2.7 4h3.6l-1.5 3.6" />
  </Glyph>;
}
/** 完全访问：已打开的锁（危险色由宿主 .is-danger 提供） */
export function MgFull(p: P) {
  return <Glyph {...p} name="mg-full">
    <rect className="lx-fill" x="5" y="11" width="14" height="9" rx="2.6" />
    <path className="lx-hop" d="M8.5 11V8.4a3.5 3.5 0 0 1 6.7-1.4" />
    <circle cx="12" cy="15.5" r="1" />
  </Glyph>;
}
