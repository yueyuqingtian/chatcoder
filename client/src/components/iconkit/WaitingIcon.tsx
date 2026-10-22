/** 等待态图标（plan-522-2209 四章 4.1）。
 *
 * 语义：会话 / 审批卡处于「等待用户处理」——需要人来点一下，它自己不会继续往前走。
 *  与「进行中」的转圈刻意区分：转圈是**循环动画 + --accent**，本图标是**静态形状 + --waiting 浅黄**，
 *  一眼就能分清「机器在跑」和「在等我」——所以这里不加任何旋转/呼吸动画。
 *
 * 视觉：透明底 + 纯描边（一个圆环包着一个问号），不填色、不加色块底，
 *  与 iconkit 其它线性图标（StatusIcon 等）同一语言；几何也与 StatusIcon 完全同构
 *  （viewBox 24、环 r=8 线宽 2、外缘半径 9），放进状态槽替换转圈时不会有尺寸 / 位置跳动。
 */
import { memo } from "react";

export const WaitingIcon = memo(function WaitingIcon({ size = 15, className, label }: {
  size?: number;
  className?: string;
  /** 无障碍名（缺省不朗读） */
  label?: string;
}) {
  return (
    <span
      className={"st-icon st-waiting" + (className ? ` ${className}` : "")}
      style={{ width: size, height: size }}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
        {/* plan-522-2209：与 StatusIcon 同径同线宽（r=8、线宽 2，外缘半径 9），状态槽里切换不跳动 */}
        <circle cx="12" cy="12" r="8" strokeWidth={2} />
        {/* plan-522-2209：问号钩手绘 path —— 不用 <text>，它依赖系统字体，字形与基线在不同机器上会漂移，
            也与线性图标风格不符。几何按标准问号字形（Lucide circle-help 的比例）缩放进 r=8 的环：
            钩弧 r=2.4、圆心约 (11.93, 10.41)，自左上方起笔顺时针绕过顶部（钩顶 y≈8.0）到右侧，
            再接一段贝塞尔收回中轴 x=12、竖直收尾到 y=12.8；钩顶与环描边内缘（y=5）留约 3 的空白，不相切 */}
        <path d="M9.67 9.6a2.4 2.4 0 0 1 4.66.8c0 1.65-2.33 2.4-2.33 2.4" strokeWidth={2} />
        {/* plan-522-2209：问号下方一点 = 极短线段 + 圆头线帽（保持纯描边，不用填充 circle）。
            点中心 (12, 16)：钩顶到圆点上下对称于环心 y=12，问号在环内视觉居中 */}
        <path d="M12 16v.01" strokeWidth={2} />
      </svg>
    </span>
  );
});
