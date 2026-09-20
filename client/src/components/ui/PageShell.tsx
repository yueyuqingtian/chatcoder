/** PageShell —— 页面骨架（plan-282-1416 问题1 根治件）
 *
 * 设计目标：让「切 tab / 切页面」不再产生跳变、割裂与异常换行。手段：
 *  1. **唯一滚动容器**：滚动只发生在本组件的 .ui-page 上，限宽在其内层
 *     （旧实现把滚动容器与限宽容器分成两层，滚动条出现/消失会改变内容盒宽度）；
 *  2. **scrollbar-gutter: stable**：恒定预留滚动条宽度，有/无滚动条时内容宽度一致；
 *  3. **固定标题区**：标题行高恒定（28px），副标题缺省时不渲染也不占位，
 *     因此「有/无副标题」不会让下方卡片整体位移；
 *  4. **宽度只有两档**：standard(720) / wide(880，仅图表类页面)，杜绝 680↔960 突变；
 *  5. 卡区间距统一 --sp-5(20px)，取代旧 14px gap + 16px stack 混用。
 */
import type { ReactNode } from "react";

interface PageShellProps {
  title: ReactNode;
  /** 仅在承载真实信息时传入——无实义时不渲染，避免"无意义文案" */
  subtitle?: ReactNode;
  /** standard=常规表单页(720)；wide=图表/表格类(880) */
  width?: "standard" | "wide";
  /** 标题行右侧操作区（如刷新/新建） */
  actions?: ReactNode;
  children: ReactNode;
}

export function PageShell({ title, subtitle, width = "standard", actions, children }: PageShellProps) {
  return (
    <div className="ui-page">
      <div className={`ui-page-inner${width === "wide" ? " is-wide" : ""}`}>
        <div className="ui-page-head">
          <div style={{ minWidth: 0 }}>
            <h1 className="ui-page-title">{title}</h1>
            {subtitle ? <div className="ui-page-subtitle">{subtitle}</div> : null}
          </div>
          {actions ? <div className="ui-page-head-actions">{actions}</div> : null}
        </div>
        <div className="ui-page-cards">{children}</div>
      </div>
    </div>
  );
}
