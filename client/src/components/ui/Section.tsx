/** Section —— 设置页分组（plan-114-450）。
 * 统一排版：卡片外一行小号灰色组标题（右侧可放该组操作），下面是一张圆角卡片，卡片内是 CardRow / ListRow。
 * 取代各面板在卡片里塞 settings-card-title、在卡片外悬空放按钮等私有写法（子代理页「新建」按钮悬空即源于此）。
 * flush=true：卡片内容自己处理内距（如编辑器、空状态）。 */
import type { ReactNode } from "react";

export function Section({ title, desc, actions, flush = false, bare = false, children, className = "" }: {
  title?: ReactNode;
  /** 组标题下的一行说明（可选） */
  desc?: ReactNode;
  /** 组标题右侧操作（如「重新扫描」「+ 添加」） */
  actions?: ReactNode;
  flush?: boolean;
  /** 不包卡片：内容自带容器（如 List、Textarea） */
  bare?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const hasHead = Boolean(title || desc || actions);
  return (
    <section className={`ui-section ${className}`.trim()}>
      {hasHead && (
        <div className="ui-section-head">
          <div className="ui-section-head-text">
            {title ? <h2 className="ui-section-title">{title}</h2> : null}
            {desc ? <div className="ui-section-desc">{desc}</div> : null}
          </div>
          {actions ? <div className="ui-section-actions">{actions}</div> : null}
        </div>
      )}
      {bare ? children : <div className={"ui-card" + (flush ? " is-flush" : "")}>{children}</div>}
    </section>
  );
}
