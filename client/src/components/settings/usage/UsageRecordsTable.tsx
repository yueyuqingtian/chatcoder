/** 使用统计 · 调用记录表（plan-20261021-usage-update）。
 *
 * 一行 = 一次模型调用：时间 / 供应商 / 账户·Key / 模型 /
 * 输入 · 缓存输入 · 输出 / 首字 · 耗时 · 速度。后端分页（每页 20 条），本组件只负责展示。
 *
 * plan-20261022-release-ci：按日期分组（组头写「今天 / 昨天 / 10月20日 周一」，行内只留时分秒），
 * 不再每行重复年月日；缓存输入旁给出命中率；口径说明收成一行，细节放在表头悬停提示里。
 *
 * 口径（与脚注一致）：
 *  - 首字 = 请求发出 → 第一个字出现；耗时 = 首字出现 → 输出结束；速度 = 输出 token ÷ 耗时；
 *  - 实测速度只统计输出 ≥ 100 token 的请求；
 *  - 带 ≈ 的是历史记录按会话日志时间戳估算：没有首字计时，等首字的时间也算在内，所以偏低，
 *    且只估输出 ≥ 200 token 的请求。
 */
import { Fragment, memo, useRef } from "react";
import type { UsageRecordItem, UsageRecordsOut } from "../../../api/client";
import { Button, Empty, Pagination, Tooltip } from "../../ui";
import { IconAlertTriangle, IconClock, IconListOrdered, IconRefresh, IconZap } from "../../icons";
import { dayLabel, fmtInt, fmtSec, fmtSecRough, fmtSpeed, splitLocalTime } from "./format";

const SKELETON_ROWS = 10;
/** 用户在 OAuth / 单 Key 供应商里没改过名的默认备注，展示时不占位（直接显示 Key） */
const GENERIC_ACCOUNT_LABELS = new Set(["默认凭据", "默认账号", "默认"]);

const Dash = () => <span className="up-dash">—</span>;

function speedTip(r: UsageRecordItem): string {
  const measured = r.first_token_ms != null && r.gen_ms != null;
  if (r.speed != null && measured) {
    return `输出 ${fmtInt(r.completion)} token ÷ 首字之后的耗时 ${fmtSec(r.gen_ms)}`;
  }
  if (r.speed != null && r.estimated) {
    return `按会话日志时间戳估算：整次请求约 ${fmtSecRough(r.est_ms)}（含等待首字的时间），所以偏低`;
  }
  if (measured) return "输出不足 100 token，不统计速度";
  return r.completion < 200 ? "历史记录没有首字计时，输出不足 200 token，不估算" : "缺少会话时间戳，无法估算";
}

const RecordRow = memo(function RecordRow({ r }: { r: UsageRecordItem }) {
  const t = splitLocalTime(r.time);
  const measured = r.first_token_ms != null && r.gen_ms != null;
  const first = measured ? fmtSec(r.first_token_ms) : null;
  const dur = measured ? fmtSec(r.gen_ms) : r.estimated ? fmtSecRough(r.est_ms) : null;
  const speed = fmtSpeed(r.speed);

  const accountText = [r.account, r.account_hint].filter(Boolean).join("　");
  const showLabel = !!r.account && !(GENERIC_ACCOUNT_LABELS.has(r.account) && r.account_hint);

  const cacheRate = r.prompt > 0 && r.cached > 0 ? Math.round((r.cached / r.prompt) * 100) : null;

  return (
    <div className="up-rec-row" role="row">
      <span className="up-rec-time" role="cell">{t ? t.clock : <Dash />}</span>
      <span role="cell">
        {r.provider ? <Tooltip content={r.provider}><span className="up-ell">{r.provider}</span></Tooltip> : <Dash />}
      </span>
      <span role="cell">
        {accountText ? (
          <Tooltip content={accountText}>
            <span className="up-acc">
              {showLabel && <span className="up-acc-name">{r.account}</span>}
              {r.account_hint && <span className="up-acc-key">{r.account_hint}</span>}
            </span>
          </Tooltip>
        ) : <Dash />}
      </span>
      <span role="cell">
        <Tooltip content={r.model}><span className="up-ell up-rec-model">{r.model}</span></Tooltip>
      </span>
      <span className="num" role="cell">{fmtInt(r.prompt)}</span>
      <span className={"num up-rec-cache" + (r.cached ? "" : " is-zero")} role="cell">
        {fmtInt(r.cached)}
        <small className="up-rec-rate">{cacheRate != null ? `${cacheRate}%` : ""}</small>
      </span>
      <span className="num" role="cell">
        {r.reasoning > 0 ? (
          <Tooltip content={`其中思考 ${fmtInt(r.reasoning)} token`}>
            <span className="up-has-tip">{fmtInt(r.completion)}</span>
          </Tooltip>
        ) : fmtInt(r.completion)}
      </span>
      <span className="num" role="cell">{first ?? <Dash />}</span>
      <span className="num" role="cell">
        {dur ? (<>{!measured && <i className="up-approx">≈</i>}{dur}</>) : <Dash />}
      </span>
      <span className="num up-rec-speed" role="cell">
        <Tooltip content={speedTip(r)}>
          <span className="up-has-tip">
            {speed ? (<>{r.estimated && <i className="up-approx">≈</i>}{speed}</>) : <Dash />}
          </span>
        </Tooltip>
      </span>
    </div>
  );
});

const SkeletonRow = () => (
  <div className="up-rec-row is-skeleton" role="presentation" aria-hidden>
    {Array.from({ length: 10 }, (_, k) => <span key={k}><i /></span>)}
  </div>
);

/** 当前页按本地日期分段：组头只在日期变化处出现（跨页时新一页的第一组照常带组头） */
function groupByDay(items: UsageRecordItem[]): Array<{ day: string; rows: UsageRecordItem[] }> {
  const groups: Array<{ day: string; rows: UsageRecordItem[] }> = [];
  for (const r of items) {
    const day = splitLocalTime(r.time)?.date ?? "";
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.rows.push(r);
    else groups.push({ day, rows: [r] });
  }
  return groups;
}

export interface UsageRecordsTableProps {
  data: UsageRecordsOut | null;
  loading: boolean;
  error: string | null;
  page: number;
  pageSize: number;
  onPage: (page: number) => void;
  onRetry: () => void;
}

export function UsageRecordsTable({ data, loading, error, page, pageSize, onPage, onRetry }: UsageRecordsTableProps) {
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const summary = data?.summary;
  const firstLoad = data === null && loading;
  const empty = !!data && !error && total === 0;
  const rootRef = useRef<HTMLElement>(null);

  // 分页条在表格底部：翻页后把卡片顶部带回视野，否则用户停在页底、看到的是新一页的最后几行
  const goPage = (p: number) => {
    onPage(p);
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    rootRef.current?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
  };

  return (
    <section ref={rootRef} className="up-card up-rec" aria-label="调用记录">
      <header className="up-card-head">
        <IconListOrdered size={16} />
        <div className="up-card-titles">
          <div className="up-card-title">调用记录</div>
          <div className="up-card-sub">按时间倒序 · 每页 {pageSize} 条</div>
        </div>
        <div className="up-rec-stats">
          <Tooltip content="所有有首字计时的调用的平均值">
            <span className="up-stat">
              <IconClock size={13} />
              <span className="k">平均首字</span>
              <span className="v">{summary?.avg_first_token_ms != null ? fmtSec(summary.avg_first_token_ms) : "—"}</span>
            </span>
          </Tooltip>
          <Tooltip content={summary?.speed_calls ? `基于 ${fmtInt(summary.speed_calls)} 次输出不少于 100 token 的调用，按 token 加权` : "还没有可统计的调用"}>
            <span className="up-stat">
              <IconZap size={13} />
              <span className="k">平均速度</span>
              <span className="v">{summary?.avg_speed != null ? <>{fmtSpeed(summary.avg_speed)}<small> token/s</small></> : "—"}</span>
            </span>
          </Tooltip>
        </div>
      </header>

      {error && (
        <div className="up-rec-error" role="alert">
          <IconAlertTriangle size={15} />
          <span>加载失败：{error}</span>
          <Button variant="ghost" size="xs" icon={<IconRefresh size={12} />} onClick={onRetry}>重试</Button>
        </div>
      )}

      {empty ? (
        <Empty size="md" title="这个范围内还没有调用记录" desc="换一个时间范围或供应商试试" />
      ) : (
        <div className="up-rec-scroll">
          <div className="up-rec-table" role="table" aria-busy={loading} aria-rowcount={total}>
            <div className="up-rec-groups" role="presentation">
              <span className="g-token">Token</span>
              <span className="g-perf">性能</span>
            </div>
            <div className="up-rec-row up-rec-head" role="row">
              <span role="columnheader">时间</span>
              <span role="columnheader">供应商</span>
              <span role="columnheader">账户 / Key</span>
              <span role="columnheader">模型</span>
              <span className="num" role="columnheader">输入</span>
              <span className="num" role="columnheader">
                <Tooltip content="输入里命中缓存的部分；右侧百分比为命中率，越高越省"><span className="up-has-tip">缓存输入</span></Tooltip>
              </span>
              <span className="num" role="columnheader">
                <Tooltip content="含思考 token；有思考的行悬停数字可看思考占多少"><span className="up-has-tip">输出</span></Tooltip>
              </span>
              <span className="num" role="columnheader">
                <Tooltip content="请求发出后，第一个字出现用了多久"><span className="up-has-tip">首字</span></Tooltip>
              </span>
              <span className="num" role="columnheader">
                <Tooltip content="从首字出现到输出结束用了多久"><span className="up-has-tip">耗时</span></Tooltip>
              </span>
              <span className="num" role="columnheader">
                <Tooltip content="每秒输出多少 token，从首字算到结束；只统计输出不少于 100 token 的调用"><span className="up-has-tip">速度 token/s</span></Tooltip>
              </span>
            </div>
            <div className={"up-rec-body" + (loading && data ? " is-loading" : "")} role="rowgroup">
              {firstLoad
                ? Array.from({ length: SKELETON_ROWS }, (_, i) => <SkeletonRow key={i} />)
                : groupByDay(items).map((g) => (
                  <Fragment key={g.day || "unknown"}>
                    <div className="up-rec-day" role="row">
                      <span role="cell">{g.day ? dayLabel(g.day) : "时间未知"}</span>
                    </div>
                    {g.rows.map((r) => <RecordRow key={r.id} r={r} />)}
                  </Fragment>
                ))}
            </div>
          </div>
        </div>
      )}

      {!empty && (
        <Pagination
          className="up-rec-pager"
          page={page}
          pageCount={data?.pages ?? 1}
          total={total}
          pageSize={pageSize}
          onChange={goPage}
          disabled={firstLoad}
          busy={loading && !!data}
        />
      )}

      <p className="up-rec-note">
        带 <i className="up-approx">≈</i> 的是早期记录按会话时间戳估算的值（含等待首字的时间，偏低）；各列口径悬停表头查看。
      </p>
    </section>
  );
}
