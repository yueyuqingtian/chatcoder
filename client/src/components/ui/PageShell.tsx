/** PageShell —— 页面骨架（plan-282-1416 问题1 根治件）
 *
 * 设计目标：让「切 tab / 切页面」不再产生跳变、割裂与异常换行。手段：
 *  1. **唯一滚动容器**：滚动只发生在本组件的 .ui-page 上，限宽在其内层
 *     （旧实现把滚动容器与限宽容器分成两层，滚动条出现/消失会改变内容盒宽度）；
 *  2. **scrollbar-gutter: stable**：恒定预留滚动条宽度，有/无滚动条时内容宽度一致；
 *  3. **固定标题区**：标题行高恒定，副标题缺省时不渲染也不占位，
 *     因此「有/无副标题」不会让下方卡片整体位移；
 *  4. **宽度只有两档**：standard(720) / wide(880，仅图表类页面)，杜绝 680↔960 突变；
 *  5. 卡区间距统一 --sp-5(20px)，取代旧 14px gap + 16px stack 混用。
 *
 * plan-114-450：页头右上角「主操作槽」——参照 Qoder，页面的主操作（新建 / 添加 / 市场）放在标题右侧，
 * 而不是悬空在卡片之间（子代理页「新建」按钮悬空的根因）。内容组件用 <PageActions> 把按钮投送到槽里，
 * 外层装饰（settings/index.tsx 的 Panel）无需知道每页有什么按钮。
 */
import { createContext, useContext, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

const ActionsSlot = createContext<HTMLElement | null>(null);

/** 在当前 PageShell 的页头右上角渲染操作按钮；不在 PageShell 内时原地渲染 */
export function PageActions({ children }: { children: ReactNode }) {
  const slot = useContext(ActionsSlot);
  return slot ? createPortal(children, slot) : <>{children}</>;
}

interface PageShellProps {
  /** 页面标题；与 subtitle/actions 同时缺省时不渲染标题区（供自带标题区的内容组件使用） */
  title?: ReactNode;
  /** 仅在承载真实信息时传入——无实义时不渲染，避免"无意义文案" */
  subtitle?: ReactNode;
  /** standard=常规表单页(720)；wide=图表/表格类(880)；xwide=列很多的数据表页(1080，仅使用统计)。
   *  宽度按页面固定，不随页内交互变化（页内切视图不会跳宽）。 */
  width?: "standard" | "wide" | "xwide";
  /** 标题行右侧操作区（如刷新/新建）；内容组件也可通过 <PageActions> 投送 */
  actions?: ReactNode;
  /** S19（plan-41-197c）：填充式页面——页面自身不滚动，由内容组件内部滚动区承担
   *  （记忆页：标题与控制区固定、列表独立上下滑动）。 */
  fill?: boolean;
  children: ReactNode;
}

export function PageShell({ title, subtitle, width = "standard", actions, fill = false, children }: PageShellProps) {
  // S18（plan-41-197b）：标题区按需渲染——模型页自带标题区，外层若再渲染一次标题会出现双标题叠加。
  const hasHead = Boolean(title || subtitle || actions);
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  return (
    <div className={`ui-page${fill ? " is-fill" : ""}`}>
      <div className={`ui-page-inner${width === "wide" ? " is-wide" : width === "xwide" ? " is-xwide" : ""}${fill ? " is-fill" : ""}`}>
        {hasHead && (
          <div className="ui-page-head">
            <div style={{ minWidth: 0 }}>
              {title ? <h1 className="ui-page-title">{title}</h1> : null}
              {subtitle ? <div className="ui-page-subtitle">{subtitle}</div> : null}
            </div>
            {/* 槽位常驻（为空时不占宽），避免内容组件投送按钮时页头重排 */}
            <div className="ui-page-head-actions" ref={setSlot}>{actions}</div>
          </div>
        )}
        <ActionsSlot.Provider value={hasHead ? slot : null}>
          <div className="ui-page-cards">{children}</div>
        </ActionsSlot.Provider>
      </div>
    </div>
  );
}
