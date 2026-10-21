/** 用量统计 · 模型明细表（plan-20261021-usage-update）。
 *
 * 原先只能按合计降序看；现在点表头可按任意列排序（再点反向）。圆点颜色按「默认合计排名」固定，
 * 排序后颜色不跟着行跑，与环形图 / 趋势图里同一个模型的颜色始终一致。
 */
import { useMemo, useState } from "react";
import type { UsageStatsOut } from "../../../api/client";
import { Tooltip } from "../../ui";
import { IconChevronDown } from "../../icons";
import { fmtTokens, modelColor } from "./chartUtils";

type Row = UsageStatsOut["by_model"][number];
type SortKey = "name" | "prompt" | "completion" | "cached" | "calls" | "total";

const COLUMNS: Array<{ key: SortKey; label: string; align?: "left" }> = [
  { key: "name", label: "模型", align: "left" },
  { key: "prompt", label: "输入" },
  { key: "completion", label: "输出" },
  { key: "cached", label: "缓存" },
  { key: "calls", label: "调用" },
  { key: "total", label: "合计" },
];

export function UsageModelTable({ rows, totalTokens }: { rows: Row[]; totalTokens: number }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "total", dir: "desc" });

  // 颜色按默认（合计降序）排名固定
  const colorIndex = useMemo(() => new Map(rows.map((r, i) => [r.key, Math.min(i, 8)])), [rows]);

  const sorted = useMemo(() => {
    const sign = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      if (sort.key === "name") return sign * a.display_name.localeCompare(b.display_name, "zh-CN");
      return sign * ((a[sort.key] as number) - (b[sort.key] as number));
    });
  }, [rows, sort]);

  const toggle = (key: SortKey) => setSort((s) => (
    s.key === key ? { key, dir: s.dir === "desc" ? "asc" : "desc" } : { key, dir: key === "name" ? "asc" : "desc" }
  ));

  return (
    <div className="up-mt" role="table">
      <div className="up-mt-row up-mt-head" role="row">
        {COLUMNS.map((c) => {
          const active = sort.key === c.key;
          return (
            <span key={c.key} className={c.align === "left" ? "" : "num"} role="columnheader" aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
              <button type="button" className={"up-sort" + (active ? " is-active" : "")} onClick={() => toggle(c.key)}>
                {c.label}
                <IconChevronDown size={12} className={"up-sort-ic" + (active && sort.dir === "asc" ? " is-asc" : "")} />
              </button>
            </span>
          );
        })}
      </div>
      {sorted.map((b) => {
        const pct = totalTokens ? (b.total / totalTokens) * 100 : 0;
        return (
          <div className="up-mt-row" role="row" key={b.key}>
            <span className="up-mt-name" role="cell">
              <i className="up-dot" style={{ background: modelColor(colorIndex.get(b.key) ?? 0) }} />
              <Tooltip content={b.display_name}><span className="up-ell">{b.display_name}</span></Tooltip>
            </span>
            <span className="num" role="cell">{fmtTokens(b.prompt)}</span>
            <span className="num" role="cell">{fmtTokens(b.completion)}</span>
            <span className="num" role="cell">{fmtTokens(b.cached)}</span>
            <span className="num" role="cell">{b.calls.toLocaleString()}</span>
            <span className="num up-mt-total" role="cell">
              <span className="v">{fmtTokens(b.total)}</span>
              <span className="up-meter" aria-hidden><i style={{ width: `${Math.max(2, pct)}%` }} /></span>
              <span className="p">{pct.toFixed(1)}%</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
