/** Kiro 供应商详情页「剩余积分」区块（plan-116-456）。
 *
 * 数据来自 CodeWhisperer RTS GetUsageLimits（与 Kiro IDE 的积分计同源）：
 * 展示「剩余 / 总量」、用量进度与下一次重置时刻；未登录时提示先导入本机登录态。
 * 交互：手动刷新（跳过服务端 10s 缓存）+ 登录后 60s 轮询。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type KiroQuotaOut, type ProviderOut } from "../../api/client";
import { IconGauge, IconRefresh } from "../icons";

const POLL_MS = 60_000;
const WARN_PERCENT = 80;

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function fmtCredits(v: number | null | undefined): string {
  if (v === null || v === undefined) return "--";
  const n = Number(v);
  if (!Number.isFinite(n)) return "--";
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function fmtResetTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  return `${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())} ${pad2(t.getHours())}:${pad2(t.getMinutes())}`;
}

export function KiroQuotaSection({ provider }: { provider: ProviderOut }) {
  const [data, setData] = useState<KiroQuotaOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const loggedIn = provider.auth_status === "logged_in";
  const aliveRef = useRef(true);

  const load = useCallback(async (force = false) => {
    if (!loggedIn) {
      setData(null);
      setError("");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const out = await api.kiroQuota(provider.id, { force });
      if (aliveRef.current) setData(out);
    } catch (e) {
      if (aliveRef.current) setError(String(e));
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, [loggedIn, provider.id]);

  useEffect(() => {
    aliveRef.current = true;
    void load();
    return () => { aliveRef.current = false; };
  }, [load]);

  // 登录后定时轮询，退出时自动停止
  useEffect(() => {
    if (!loggedIn) return;
    const timer = window.setInterval(() => { void load(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [loggedIn, load]);

  const limit = Number(data?.limit ?? 0);
  const used = Number(data?.used ?? 0);
  const remaining = Number(data?.remaining ?? 0);
  const percentUsed = limit > 0 ? Math.min(100, Math.round((used / limit) * 1000) / 10) : null;
  const danger = percentUsed !== null && percentUsed >= WARN_PERCENT;
  const resetText = fmtResetTime(data?.reset_at);

  return (
    <div className="models-section">
      <div className="models-section-title">
        <span>剩余积分</span>
        <button
          className="btn btn-ghost btn-xs"
          onClick={() => void load(true)}
          disabled={!loggedIn || loading}
          title="强制刷新（跳过服务端缓存）"
        >
          <IconRefresh size={13} /> 刷新
        </button>
      </div>

      {!loggedIn ? (
        <div className="ta3-quota-empty">
          <span className="ta3-quota-empty-ic" aria-hidden><IconGauge size={16} /></span>
          登录账号后可查看剩余积分。
        </div>
      ) : (
        <>
          {error && <div className="models-err">{error}</div>}
          {loading && !data ? (
            <div className="ta3-quota-skeleton" aria-label="正在加载积分信息" role="status">
              <span className="mkt-sk" style={{ width: "45%", height: 14 }} />
              <span className="mkt-sk" style={{ width: "100%", height: 42 }} />
            </div>
          ) : data && data.available ? (
            <div className={`ta3-quota-window ${danger ? "tone-warn" : "tone-normal"}`}>
              <div className="ta3-quota-window-head">
                <span className="ta3-quota-window-label">{data.account ? `账号 ${data.account}` : "当前账号"}</span>
                <span className="ta3-quota-window-percent">{percentUsed !== null ? `${percentUsed.toFixed(1)}%` : "--"}</span>
              </div>
              <div className="ta3-quota-bar">
                <div className="ta3-quota-bar-fill" style={{ width: `${percentUsed ?? 0}%` }} />
              </div>
              <div className="ta3-quota-window-foot">
                剩余 <b>{fmtCredits(remaining)}</b> / 总量 {fmtCredits(limit)}（已用 {fmtCredits(used)}）
              </div>
              {resetText && <div className="ta3-quota-hint">下次重置：{resetText}</div>}
            </div>
          ) : (
            <div className="ta3-quota-empty">
              <span className="ta3-quota-empty-ic" aria-hidden><IconGauge size={16} /></span>
              未获取到积分数据，请稍后刷新。
            </div>
          )}
        </>
      )}
    </div>
  );
}
