/** Pagination —— 统一分页条（plan-20261021-usage-update）。
 *
 * 左侧是「共 N 条 · 每页 M 条」的说明，右侧是上一页 / 页码 / 下一页；页数多时再附一个「跳至」输入。
 * 页码窗口恒为 7 个槽位（首页、末页常驻，当前页附近展开，其余折叠为省略号），
 * 翻页时按钮不会因为页码数量变化而左右位移。
 * 页码按钮走 ui/Button（当前页 secondary 底色，其余 ghost），不自造按钮类。
 */
import { useState } from "react";
import { Button } from "./Button";
import { Input } from "./Input";
import { IconChevronLeft, IconChevronRight } from "../icons";

type PageItem = number | "gap-l" | "gap-r";

/** 恒定 7 个槽位的页码窗口；总页数不超过 7 时全部展开 */
export function pageItems(page: number, count: number): PageItem[] {
  if (count <= 7) return Array.from({ length: count }, (_, i) => i + 1);
  if (page <= 4) return [1, 2, 3, 4, 5, "gap-r", count];
  if (page >= count - 3) return [1, "gap-l", count - 4, count - 3, count - 2, count - 1, count];
  return [1, "gap-l", page - 1, page, page + 1, "gap-r", count];
}

export function Pagination({ page, pageCount, total, pageSize, onChange, disabled = false, busy = false, className = "" }: {
  page: number;
  pageCount: number;
  total: number;
  pageSize: number;
  onChange: (page: number) => void;
  disabled?: boolean;
  /** 正在加载目标页：只在左侧说明处显示提示，不改变按钮布局 */
  busy?: boolean;
  className?: string;
}) {
  const [jump, setJump] = useState("");
  const count = Math.max(1, pageCount);
  const go = (p: number) => {
    const next = Math.min(count, Math.max(1, Math.round(p)));
    if (next !== page) onChange(next);
  };
  const submitJump = () => {
    const n = Number(jump);
    setJump("");
    if (Number.isFinite(n) && n >= 1) go(n);
  };
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);

  return (
    <nav className={`ui-pager ${className}`.trim()} aria-label="分页">
      <div className="ui-pager-info">
        <span className="ui-pager-range">{total === 0 ? "0 条" : `${from.toLocaleString()}–${to.toLocaleString()} 条`}</span>
        <span className="ui-pager-total">共 {total.toLocaleString()} 条</span>
        <span className={"ui-pager-busy" + (busy ? " is-on" : "")} aria-live="polite">{busy ? "加载中…" : ""}</span>
      </div>
      <div className="ui-pager-ctrl">
        <Button variant="ghost" size="sm" iconOnly icon={<IconChevronLeft size={14} />} aria-label="上一页" disabled={disabled || page <= 1} onClick={() => go(page - 1)} />
        {pageItems(page, count).map((it) =>
          typeof it === "number" ? (
            <Button
              key={it}
              variant={it === page ? "secondary" : "ghost"}
              size="sm"
              className="ui-pager-num"
              aria-label={`第 ${it} 页`}
              aria-current={it === page ? "page" : undefined}
              disabled={disabled}
              onClick={() => go(it)}
            >
              {it}
            </Button>
          ) : (
            <span key={it} className="ui-pager-gap" aria-hidden>…</span>
          ),
        )}
        <Button variant="ghost" size="sm" iconOnly icon={<IconChevronRight size={14} />} aria-label="下一页" disabled={disabled || page >= count} onClick={() => go(page + 1)} />
        {count > 7 && (
          <Input
            size="sm"
            prefix="跳至"
            suffix="页"
            wrapClassName="ui-pager-jump"
            inputMode="numeric"
            aria-label="跳转到指定页码"
            value={jump}
            disabled={disabled}
            onChange={(e) => setJump(e.target.value.replace(/[^\d]/g, "").slice(0, 5))}
            onKeyDown={(e) => { if (e.key === "Enter") submitJump(); }}
            onBlur={() => { if (jump) submitJump(); }}
          />
        )}
      </div>
    </nav>
  );
}
