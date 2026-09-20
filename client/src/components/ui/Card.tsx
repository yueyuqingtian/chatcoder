/** Card / CardTitle / CardRow / ListRow —— 统一卡片与展示行（plan-282-1416）
 *
 * 取代旧 `.settings-card` + `.settings-row` 的「负 margin 撑分隔线」写法：
 *  - 卡片 border-radius 12（与用户气泡同值）、overflow:hidden 让分隔线天然贴合圆角；
 *  - CardRow 用固定上下内距（--row-pad-y）承载「标题+描述 / 控件槽」两列，
 *    行内控件换行时不会再外溢（这是旧负 margin 方案"割裂/异常换行"的根源）；
 *  - ListRow 用于资源列表（增删项），与 Card 区分：Card 是分组容器，ListRow 是列表项。
 */
import type { ReactNode } from "react";

export function Card({ className = "", children }: { className?: string; children: ReactNode }) {
  return <div className={`ui-card ${className}`.trim()}>{children}</div>;
}

export function CardTitle({ children }: { children: ReactNode }) {
  return <div className="ui-card-title">{children}</div>;
}

export function CardBody({ className = "", children }: { className?: string; children: ReactNode }) {
  return <div className={`ui-card-body ${className}`.trim()}>{children}</div>;
}

interface CardRowProps {
  title: ReactNode;
  /** 描述行——无实义时省略（不占位由 CSS 保证行高不跳） */
  desc?: ReactNode;
  /** 右侧控件槽（开关/下拉/滑块/按钮组） */
  children?: ReactNode;
  disabled?: boolean;
  className?: string;
}

export function CardRow({ title, desc, children, disabled = false, className = "" }: CardRowProps) {
  return (
    <div className={`ui-card-row${disabled ? " is-disabled" : ""} ${className}`.trim()}>
      <div className="ui-card-row-info">
        <div className="ui-card-row-title">{title}</div>
        {desc ? <div className="ui-card-row-desc">{desc}</div> : null}
      </div>
      {children != null && <div className="ui-card-row-control">{children}</div>}
    </div>
  );
}

interface ListRowProps {
  name: ReactNode;
  desc?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

export function ListRow({ name, desc, actions, className = "" }: ListRowProps) {
  return (
    <div className={`ui-list-row ${className}`.trim()}>
      <div className="ui-list-row-info">
        <div className="ui-list-row-name">{name}</div>
        {desc ? <div className="ui-list-row-desc">{desc}</div> : null}
      </div>
      {actions ? <div className="ui-list-row-actions">{actions}</div> : null}
    </div>
  );
}

export function List({ children }: { children: ReactNode }) {
  return <div className="ui-list">{children}</div>;
}
