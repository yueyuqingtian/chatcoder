/** Ta+3 供应商详情页「额度与用量」区块（plan-270-1358）。
 *
 * 数据与展示口径对齐 Ta+3 v0.4.6 quotaView 投影：
 * - percentUsed === null = 该窗口不限额度（不画进度条）；>=100 视为已用尽而非异常；
 * - 倒计时以 serverTimeEpochSeconds 为基准（防本机时钟偏差）；
 * - 模型负载等级只认服务端 grade / hint 枚举，未知码忽略；
 * - 「打开网页查看额度」由服务端生成 SSO 免登链接后用系统浏览器打开。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type ProviderOut,
  type Ta3ModelStatusModelOut,
  type Ta3ModelStatusOut,
  type Ta3QuotaOut,
  type Ta3QuotaTrendOut,
  type Ta3QuotaWindowOut,
} from "../../api/client";
import { IconExternalLink, IconRefresh } from "../icons";
import { fmtTokens } from "./usage/chartUtils";

const QUOTA_WARN_PERCENT = 80;
const QUOTA_EXHAUSTED_PERCENT = 100;

const LOAD_GRADE_TEXT: Record<string, string> = {
  GRAY: "不可用",
  RED: "负载高",
  YELLOW: "负载一般",
  GREEN: "可用",
};

/** §7.3 枚举码本地化（枚举会新增，未知码一律忽略）。 */
const LOAD_REASON_TEXT: Record<string, string> = {
  no_available_upstream: "上游无可用通道",
  provider_quota_exhausted: "上游额度耗尽",
  high_error_rate: "错误率偏高",
  concurrency_saturated: "并发已满",
  provider_quota_high: "上游额度紧张",
  elevated_error_rate: "错误率升高",
  concurrency_busy: "并发繁忙",
};

type Tone = "unlimited" | "normal" | "warn" | "danger";

/** 数值解析：null / undefined / 空串保留 null（契约信号，不能静默转 0）。 */
function toNumberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toPositiveEpoch(value: unknown): number {
  const parsed = toNumberOrNull(value);
  return parsed !== null && parsed > 0 ? parsed : 0;
}

function resolveTone(percentUsed: number | null): Tone {
  if (percentUsed === null) return "unlimited";
  if (percentUsed >= QUOTA_EXHAUSTED_PERCENT) return "danger";
  if (percentUsed >= QUOTA_WARN_PERCENT) return "warn";
  return "normal";
}

/** 百分比文字：一位小数；>100 保留真实值，让用户看到超额程度。 */
function formatPercentText(percentUsed: number | null): string {
  return percentUsed === null ? "不限" : `${percentUsed.toFixed(1)}%`;
}

function formatDurationText(remainMs: number | null): string {
  if (remainMs === null) return "";
  if (remainMs <= 0) return "即将重置";
  const totalMinutes = Math.ceil(remainMs / 60_000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function formatClockText(epochMs: number, nowMs: number): string {
  const target = new Date(epochMs);
  const now = new Date(nowMs);
  const sameDay =
    target.getFullYear() === now.getFullYear() &&
    target.getMonth() === now.getMonth() &&
    target.getDate() === now.getDate();
  const time = `${String(target.getHours()).padStart(2, "0")}:${String(target.getMinutes()).padStart(2, "0")}`;
  if (sameDay) return time;
  return `${String(target.getMonth() + 1).padStart(2, "0")}-${String(target.getDate()).padStart(2, "0")} ${time}`;
}

/** 自助重置文案（§3.3：selfResetMax 为 null / 负数 = 不限次数，不是 0）。 */
function describeSelfReset(w: Ta3QuotaWindowOut): string {
  if (w.selfResetEnabled !== true) return "";
  if (w.resetPending === true) return "已有重置申请待管理员审批";
  const max = toNumberOrNull(w.selfResetMax);
  const remain = toNumberOrNull(w.selfResetRemain);
  const unlimited = max === null || max < 0 || (remain !== null && remain < 0);
  if (unlimited) return String(w.selfResetHint || "自助重置次数不限");
  const resolved = remain !== null
    ? Math.max(0, remain)
    : Math.max(0, max - (toNumberOrNull(w.selfResetUsed) ?? 0));
  return `还可自助重置 ${resolved} 次`;
}

function describeModelHint(hint: string, m: Ta3ModelStatusModelOut): string {
  switch (hint) {
    case "off_peak_cheaper": {
      const clock = /(\d{2}:\d{2})/.exec(String(m.rate?.nextChangeAt || ""))?.[1] || "";
      const nextMultiplier = toNumberOrNull(m.rate?.nextMultiplier);
      const multText = nextMultiplier !== null ? ` ${nextMultiplier.toFixed(1)}×` : "";
      return clock
        ? `${clock} 后进入低峰${multText}，错峰使用更省额度`
        : "稍后进入更低倍率时段，错峰使用更省额度";
    }
    case "busy_defer":
      return "当前负载较高，建议稍后再试或更换模型";
    case "unavailable":
      return "当前不可用，请选择其他模型";
    default:
      return "";
  }
}

function gradeClass(grade: string): string {
  switch (grade) {
    case "GREEN": return "ok";
    case "YELLOW": return "warn";
    case "RED": return "warn";
    case "GRAY": return "err";
    default: return "muted";
  }
}

export function Ta3QuotaSection({ provider }: { provider: ProviderOut }) {
  const loggedIn = provider.auth_status === "logged_in";
  const [quota, setQuota] = useState<Ta3QuotaOut | null>(null);
  const [modelStatus, setModelStatus] = useState<Ta3ModelStatusOut | null>(null);
  const [trend, setTrend] = useState<Ta3QuotaTrendOut | null>(null);
  const [trendPeriod, setTrendPeriod] = useState<"DAILY" | "MONTHLY">("DAILY");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [webBusy, setWebBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const requestSeq = useRef(0);

  const loadCore = useCallback(async (force = false) => {
    if (!loggedIn) return;
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    const [q, ms] = await Promise.allSettled([
      api.ta3Quota(provider.id, { force }),
      api.ta3ModelStatus(provider.id, { force }),
    ]);
    if (seq !== requestSeq.current) return;
    if (q.status === "fulfilled") {
      setQuota(q.value);
    } else {
      setError(String(q.reason));
    }
    if (ms.status === "fulfilled") setModelStatus(ms.value);
    setLoading(false);
  }, [provider.id, loggedIn]);

  const loadTrend = useCallback(async (period: "DAILY" | "MONTHLY") => {
    if (!loggedIn) return;
    try {
      setTrend(await api.ta3QuotaTrend(provider.id, { period }));
    } catch { /* 趋势为非关键路径：失败保留旧数据，不打断面板 */ }
  }, [provider.id, loggedIn]);

  // 切换供应商 / 登录态变化：重置并拉取一次
  useEffect(() => {
    setQuota(null);
    setModelStatus(null);
    setTrend(null);
    setTrendPeriod("DAILY");
    if (loggedIn) {
      void loadCore(false);
      void loadTrend("DAILY");
    }
  }, [provider.id, loggedIn, loadCore, loadTrend]);

  // 倒计时（基于服务端时间基准）每 30s 触发一次重渲染
  useEffect(() => {
    if (!loggedIn) return;
    const timer = window.setInterval(() => setTick((t) => t + 1), 30_000);
    return () => window.clearInterval(timer);
  }, [loggedIn]);

  const windows = useMemo(() => {
    const nowMs = Date.now();
    const serverTime = toPositiveEpoch(quota?.serverTimeEpochSeconds);
    const serverOffsetMs = serverTime > 0 ? serverTime * 1000 - nowMs : 0;
    return (quota?.windows || []).map((raw, index) => {
      const percentUsed = toNumberOrNull(raw.percentUsed);
      const resetEpochSeconds = toPositiveEpoch(raw.resetEpochSeconds);
      const remainMs = resetEpochSeconds ? resetEpochSeconds * 1000 - (nowMs + serverOffsetMs) : null;
      return {
        key: String(raw.key || raw.window || index),
        label: String(raw.label || raw.key || raw.window || `窗口 ${index + 1}`),
        window: String(raw.window || "").toUpperCase(),
        unlimited: percentUsed === null,
        exhausted: percentUsed !== null && percentUsed >= QUOTA_EXHAUSTED_PERCENT,
        tone: resolveTone(percentUsed),
        percentText: formatPercentText(percentUsed),
        progressWidth: percentUsed === null ? 0 : Math.min(100, Math.max(0, percentUsed)),
        resetMomentText: resetEpochSeconds ? formatClockText(resetEpochSeconds * 1000, nowMs + serverOffsetMs) : "",
        countdownText: formatDurationText(remainMs),
        selfResetText: describeSelfReset(raw),
      };
    });
    // tick 用于驱动倒计时按时重算（依赖并非直接参与计算）
  }, [quota, tick]);

  const trendView = useMemo(() => {
    const points = (trend?.points || [])
      .map((p) => ({
        date: String(p.date || ""),
        label: String(p.date || "").slice(5),
        requestCount: toNumberOrNull(p.requestCount) ?? 0,
        tokenCount: toNumberOrNull(p.tokenCount) ?? 0,
      }))
      .filter((p) => p.date);
    const maxToken = points.reduce((m, p) => Math.max(m, p.tokenCount), 0);
    const totalRequests = points.reduce((s, p) => s + p.requestCount, 0);
    const totalTokens = points.reduce((s, p) => s + p.tokenCount, 0);
    return { points, maxToken, totalRequests, totalTokens };
  }, [trend]);

  const models = useMemo(() => (modelStatus?.models || []).map((m) => {
    const grade = String(m.load?.grade || "").toUpperCase();
    const multiplier = toNumberOrNull(m.rate?.currentMultiplier);
    const latencyMs = toNumberOrNull(m.load?.latencyP50Ms);
    const reasons = (m.load?.reasons || [])
      .map((r) => LOAD_REASON_TEXT[String(r)])
      .filter(Boolean);
    return {
      key: String(m.model || m.displayName || ""),
      name: String(m.displayName || m.model || ""),
      showMultiplier: multiplier !== null && multiplier !== 1,
      multiplierText: multiplier !== null ? `${multiplier.toFixed(1)}×` : "",
      grade,
      gradeText: LOAD_GRADE_TEXT[grade] || "",
      latencyText: latencyMs !== null ? `近 1 小时响应 ${(latencyMs / 1000).toFixed(1)}s` : "近 1 小时无请求",
      reasonTexts: reasons,
      hintText: describeModelHint(String(m.hint || ""), m),
    };
  }), [modelStatus]);

  const concurrencyLimit = toNumberOrNull(quota?.concurrency?.limit);
  const planName = String(quota?.plan?.planName || "");
  const planEndEpoch = toNumberOrNull(quota?.plan?.endEpochSeconds);

  const refresh = useCallback(async () => {
    await Promise.allSettled([loadCore(true), loadTrend(trendPeriod)]);
  }, [loadCore, loadTrend, trendPeriod]);

  const openAdminWeb = useCallback(async () => {
    setWebBusy(true);
    setError(null);
    try {
      const r = await api.ta3AdminWeb(provider.id);
      if (!r?.url) throw new Error("服务未返回网页地址");
      const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
      if (w.chatcoderAPI?.openExternal) await w.chatcoderAPI.openExternal(r.url);
      else window.open(r.url, "_blank");
    } catch (e) {
      setError(String(e));
    } finally {
      setWebBusy(false);
    }
  }, [provider.id]);

  const switchPeriod = (p: "DAILY" | "MONTHLY") => {
    if (p === trendPeriod) return;
    setTrendPeriod(p);
    void loadTrend(p);
  };

  return (
    <div className="models-section">
      <div className="models-section-title">
        额度与用量
        <div style={{ display: "flex", gap: 6 }}>
          <button className="btn btn-ghost btn-xs" onClick={() => void refresh()} disabled={!loggedIn || loading} title="强制刷新（跳过服务端缓存）">
            <IconRefresh size={12} /> 刷新
          </button>
          <button className="btn btn-ghost btn-xs" onClick={() => void openAdminWeb()} disabled={!loggedIn || webBusy} title="在浏览器中打开后台网页查看剩余额度">
            <IconExternalLink size={12} /> 打开网页查看额度
          </button>
        </div>
      </div>

      {!loggedIn ? (
        <div className="navpage-empty">登录账号后可查看剩余额度、用量趋势与模型状态。</div>
      ) : (
        <>
          {error && <div className="models-err">{error}</div>}
          {!error && loading && !quota && <div className="ta3-quota-loading">正在加载额度信息…</div>}

          {quota && (
            <>
              <div className="ta3-quota-plan">
                <span>当前套餐：{planName ? `${planName}` : "未分配套餐"}</span>
                {concurrencyLimit !== null && concurrencyLimit > 0 && <span>并发上限：{concurrencyLimit}</span>}
                {planEndEpoch !== null && planEndEpoch > 0 && (
                  <span>{new Date(planEndEpoch * 1000).toLocaleDateString()} 到期</span>
                )}
              </div>
              <div className="ta3-quota-windows">
                {windows.map((w) => (
                  <div key={w.key} className={`ta3-quota-window tone-${w.tone}`}>
                    <div className="ta3-quota-window-head">
                      <span className="ta3-quota-window-label">{w.label}</span>
                      <span className="ta3-quota-window-percent">{w.percentText}</span>
                    </div>
                    {!w.unlimited && (
                      <div className="ta3-quota-bar">
                        <div className="ta3-quota-bar-fill" style={{ width: `${w.progressWidth}%` }} />
                      </div>
                    )}
                    <div className="ta3-quota-window-foot">
                      {w.resetMomentText ? `下次重置 ${w.resetMomentText}` : ""}
                      {w.countdownText ? ` · 剩 ${w.countdownText}` : ""}
                    </div>
                    {w.selfResetText && <div className="ta3-quota-hint">{w.selfResetText}</div>}
                    {w.window === "DAILY" && w.exhausted && !w.unlimited && (
                      <div className="ta3-quota-hint warn">
                        {`今日额度已用尽，请等待明日自动重置或使用自助重置。`}
                      </div>
                    )}
                  </div>
                ))}
                {windows.length === 0 && <div className="ta3-quota-loading">暂无额度窗口数据</div>}
              </div>
            </>
          )}

          <div className="ta3-quota-trend">
            <div className="ta3-quota-trend-head">
              <span>用量趋势</span>
              <div className="ta3-quota-seg">
                <button className={trendPeriod === "DAILY" ? "active" : ""} onClick={() => switchPeriod("DAILY")}>按天</button>
                <button className={trendPeriod === "MONTHLY" ? "active" : ""} onClick={() => switchPeriod("MONTHLY")}>按月</button>
              </div>
            </div>
            {trendView.points.length > 0 ? (
              <>
                <div className="ta3-quota-bars">
                  {trendView.points.map((p) => (
                    <div
                      key={p.date}
                      className="ta3-quota-bar-col"
                      title={`${p.label}：请求 ${p.requestCount} 次 · ${fmtTokens(p.tokenCount)} token`}
                    >
                      <div
                        className="ta3-quota-bar-v"
                        style={{ height: `${trendView.maxToken > 0 ? Math.max(2, (p.tokenCount / trendView.maxToken) * 100) : 0}%` }}
                      />
                    </div>
                  ))}
                </div>
                <div className="ta3-quota-trend-foot">
                  合计：请求 {trendView.totalRequests} 次 · {fmtTokens(trendView.totalTokens)} token
                </div>
              </>
            ) : (
              <div className="ta3-quota-loading">暂无趋势数据</div>
            )}
          </div>

          <div className="ta3-quota-models">
            <div className="ta3-quota-models-head">
              模型状态
              {modelStatus?.generatedAt ? <span className="ta3-quota-muted">（快照 {modelStatus.generatedAt}）</span> : null}
            </div>
            {models.map((m) => (
              <div key={m.key} className="ta3-quota-model">
                <div className="ta3-quota-model-head">
                  <span className="ta3-quota-model-name">{m.name}</span>
                  {m.showMultiplier && <span className="settings-resource-tag">{m.multiplierText}</span>}
                  {m.gradeText && <span className={"models-badge sm " + gradeClass(m.grade)}>{m.gradeText}</span>}
                </div>
                <div className="ta3-quota-model-desc">
                  {m.latencyText}
                  {m.reasonTexts.length > 0 ? ` · ${m.reasonTexts.join("、")}` : ""}
                </div>
                {m.hintText && <div className="ta3-quota-hint">{m.hintText}</div>}
              </div>
            ))}
            {models.length === 0 && <div className="ta3-quota-loading">暂无模型状态数据</div>}
          </div>
        </>
      )}
    </div>
  );
}
