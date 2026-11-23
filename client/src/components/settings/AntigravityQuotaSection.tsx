/** antigravity（Google Antigravity）供应商详情页「配额」区块。
 *
 * 采用「状态总览带 + 环形仪表盘网格 + 多账号切换器 + 按池联动模型明细」布局：
 * - 数据来自后端 /providers/{id}/antigravity/quota（可选 credential_id，服务端缓存 30s）；
 * - 优先渲染服务端重建的 pools + model_windows（支持任意数量独立池，如 Gemini / Claude & GPT / Argon），
 *   缺省时回退兼容 summary.groups；
 * - 多账号时顶部提供账号切换器，默认展示「当前使用」账号，切账号命中本地短缓存可瞬切；
 * - 剩余百分比为「还剩多少」（0-100，null = 网关没给），<20% 警示、<5% 危急。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AntigravityQuotaModelOut,
  type AntigravityQuotaModelWindowOut,
  type AntigravityQuotaOut,
  type ProviderCredentialOut,
} from "../../api/client";
import { IconChevronDown, IconChevronRight, IconGauge, IconInfo, IconRefresh, IconSearch } from "../icons";
import { Badge, Button, Collapse, Input, Select, Tooltip } from "../ui";

/** 剩余低于该百分比视为紧张（剩余制：越低越危险） */
const WARN_REMAIN_PERCENT = 20;
/** 剩余低于该百分比视为危急，转错误色 */
const DANGER_REMAIN_PERCENT = 5;
/** 与服务端 30s 缓存同周期，切回已加载账号时在此窗口内复用内存快照实现瞬切 */
const CLIENT_CACHE_TTL_MS = 30_000;

type QuotaTone = "normal" | "warn" | "danger" | "unknown";

interface NormalizedWindow {
  id: string;
  label: string;
  fullLabel: string;
  percent: number | null;
  resetTime: string | null;
  spanSeconds: number;
}

interface NormalizedPool {
  key: string;
  name: string;
  isStandalone: boolean;
  windows: NormalizedWindow[];
  summaryHint: string;
  minPercent: number | null;
  modelCount: number;
}

interface MergedModelItem {
  key: string;
  pool: string;
  displayName: string;
  effortBadge: string;
  tokenType: string;
  percent: number | null;
  resetTime: string | null;
  rawIds: string[];
}

interface CacheEntry {
  data: AntigravityQuotaOut;
  fetchedAt: number;
}

/** 与后端 catalog.py 对齐的思考档位词（最长优先匹配，防止 extra-low 被 low 抢先截取） */
const EFFORT_WORDS: ReadonlyArray<readonly [string, string]> = [
  ["extra-high", "xhigh"],
  ["extra-low", "minimal"],
  ["xhigh", "xhigh"],
  ["minimal", "minimal"],
  ["low", "low"],
  ["medium", "medium"],
  ["high", "high"],
];

const EFFORT_LABEL: Record<string, string> = {
  minimal: "Minimal",
  low: "Low",
  medium: "Med",
  high: "High",
  xhigh: "XHigh",
};

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function fmtPercent(v: number | null | undefined): string {
  if (v === null || v === undefined) return "--";
  const n = Number(v);
  if (!Number.isFinite(n)) return "--";
  const clamped = Math.min(100, Math.max(0, n));
  return Number.isInteger(clamped) ? `${clamped}%` : `${clamped.toFixed(1)}%`;
}

function clampPercent(v: number | null | undefined): number {
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

function remainTone(v: number | null | undefined): QuotaTone {
  if (v === null || v === undefined) return "unknown";
  const n = Number(v);
  if (!Number.isFinite(n)) return "unknown";
  if (n < DANGER_REMAIN_PERCENT) return "danger";
  return n < WARN_REMAIN_PERCENT ? "warn" : "normal";
}

function fmtResetTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  return `${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())} ${pad2(t.getHours())}:${pad2(t.getMinutes())}`;
}

function fmtResetCountdown(iso: string | null | undefined, nowMs: number): string {
  if (!iso) return "";
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return "";
  const diff = target - nowMs;
  if (diff <= 0) return "即将重置";
  const totalMinutes = Math.ceil(diff / 60_000);
  if (totalMinutes < 1) return "不到 1 分钟后";
  if (totalMinutes < 60) return `${totalMinutes} 分钟后`;
  const totalHours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  if (totalHours < 24) return mins > 0 ? `${totalHours}小时${mins}分后` : `${totalHours}小时后`;
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  return hours > 0 ? `${days}天${hours}小时后` : `${days}天后`;
}

function normalizeWindowNames(rawName: string | undefined, windowKind?: string): {
  label: string;
  fullLabel: string;
  spanOrder: number;
} {
  const kind = (windowKind || "").toLowerCase();
  const raw = (rawName || "").trim();
  const low = raw.toLowerCase();

  if (kind === "5h" || low.includes("5 小时") || low.includes("five hour") || low.includes("5-hour") || low === "5h") {
    return { label: "5 小时", fullLabel: "5 小时限额", spanOrder: 5 * 3600 };
  }
  if (kind === "weekly" || low.includes("7 天") || low.includes("weekly")) {
    return { label: "7 天", fullLabel: "7 天周限额", spanOrder: 7 * 24 * 3600 };
  }
  const cleaned = raw.replace(/\s+Limit(\s+Remaining)?$/i, "").trim();
  return {
    label: cleaned || "额度窗口",
    fullLabel: raw || "额度窗口",
    spanOrder: Number.MAX_SAFE_INTEGER,
  };
}

/** plan-20261123-ux-fixes：主视图只展示 Gemini / Claude & GPT / Gemini 4 Argon 三个核心额度池，
 *  避免逐模型读数偏差或内部专用模型（如 orchestrator）把面板撑成数十张卡片。 */
const PRIMARY_POOL_RE = /^(gemini|claude\b.*|gemini\s*4\s*argon)$/i;

function isPrimaryPoolName(name: string): boolean {
  return PRIMARY_POOL_RE.test(String(name || "").trim());
}

function normalizeGroupPoolName(displayName: string | undefined): string {
  let name = String(displayName || "").trim();
  if (!name) return "配额分组";
  if (name.toLowerCase().endsWith(" models")) name = name.slice(0, -7).trim();
  return name.replace(/ and /g, " & ");
}

function cleanGroupDescription(desc: string | null | undefined): string {
  if (!desc) return "";
  return desc
    .replace(/^Models within this group:\s*/i, "")
    .split(/,\s*/)
    .map((s) => s.trim())
    .filter(Boolean)
    .join(" · ");
}

function splitFamily(modelId: string): { familyId: string; effort: string } {
  const text = String(modelId || "");
  for (const [word, level] of EFFORT_WORDS) {
    const suffix = `-${word}`;
    if (text.endsWith(suffix) && text.length > suffix.length) {
      return { familyId: text.slice(0, -suffix.length), effort: level };
    }
  }
  return { familyId: text, effort: "" };
}

function stripEffortFromDisplay(name: string): string {
  return name
    .replace(/\s*\((Minimal|Low|Medium|High|Extra High|Extra Low|XHigh)\)$/i, "")
    .replace(/\s+(Minimal|Low|Medium|High|Extra High|Extra Low|XHigh)$/i, "")
    .trim();
}

function fallbackDisplayName(modelId: string): string {
  return modelId
    .split("-")
    .map((seg) => {
      const low = seg.toLowerCase();
      if (low === "gpt") return "GPT";
      if (low === "oss") return "OSS";
      if (/^\d+(\.\d+)?$/.test(seg)) return seg;
      return seg.charAt(0).toUpperCase() + seg.slice(1);
    })
    .join(" ");
}

function isAntigravityAccountCred(c: ProviderCredentialOut): boolean {
  const extra = c.extra && typeof c.extra === "object" ? (c.extra as Record<string, unknown>) : null;
  if (extra?.kind === "antigravity_account") return true;
  if (!c.has_api_key && (extra?.account_email || extra?.account_sub)) return true;
  return false;
}

function accountDisplayName(c: ProviderCredentialOut): string {
  const extra = c.extra && typeof c.extra === "object" ? (c.extra as Record<string, unknown>) : null;
  const email = typeof extra?.account_email === "string" ? extra.account_email.trim() : "";
  return email || c.label || `账号 #${c.id}`;
}

function ringStrokeColor(tone: QuotaTone): string {
  if (tone === "danger") return "var(--error)";
  if (tone === "warn") return "var(--warning)";
  if (tone === "unknown") return "var(--border-strong)";
  return "var(--info)";
}

/** 单个额度环形仪表：基于精确圆弧周长计算 strokeDashoffset，动画沿用全局 --dur-3 令牌 */
function QuotaRingGauge({
  percent,
  label,
  fullLabel,
  resetTime,
  nowMs,
}: {
  percent: number | null;
  label: string;
  fullLabel: string;
  resetTime: string | null;
  nowMs: number;
}) {
  const tone = remainTone(percent);
  const size = 64;
  const strokeWidth = 5.5;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const ratio = percent === null ? 0 : clampPercent(percent) / 100;
  const offset = circumference * (1 - ratio);
  const countdown = fmtResetCountdown(resetTime, nowMs);
  const exactReset = fmtResetTime(resetTime);
  const color = ringStrokeColor(tone);

  const ringNode = (
    <div className={`ag-quota-ring-wrap tone-${tone}`}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="color-mix(in oklab, var(--text-1) 8%, transparent)"
          strokeWidth={strokeWidth}
        />
        {percent !== null && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={color}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            className="ag-quota-ring-arc"
          />
        )}
      </svg>
      <span className="ag-quota-ring-num">{fmtPercent(percent)}</span>
    </div>
  );

  return (
    <div className={`ag-quota-ring-item tone-${tone}`}>
      <Tooltip content={`${fullLabel}：剩余 ${fmtPercent(percent)}${exactReset ? ` · 重置时刻 ${exactReset}` : ""}`}>
        {ringNode}
      </Tooltip>
      <span className="ag-quota-ring-label">{label}</span>
      {exactReset ? (
        <Tooltip content={`下次重置：${exactReset}`}>
          <span className="ag-quota-ring-reset">{countdown}重置</span>
        </Tooltip>
      ) : (
        <span className="ag-quota-ring-reset">{"\u00a0"}</span>
      )}
    </div>
  );
}

/** 合并同家族不同思考档位（如 Low/Med/High），读数一致时折叠为一张芯片 */
function mergeModelFamilies(
  models: AntigravityQuotaModelOut[],
  modelWindows: AntigravityQuotaModelWindowOut[],
): MergedModelItem[] {
  const winById = new Map<string, AntigravityQuotaModelWindowOut>();
  for (const w of modelWindows) {
    if (w.model_id) winById.set(w.model_id, w);
  }
  const tokenById = new Map<string, string>();
  for (const m of models) {
    if (m.model_id && m.token_type) tokenById.set(m.model_id, m.token_type);
  }

  // 汇总出统一输入清单（model_windows 优先，缺少时补全 models）
  const rawEntries: Array<{
    modelId: string;
    name: string;
    pool: string;
    percent: number | null;
    resetTime: string | null;
    tokenType: string;
  }> = [];
  const seenIds = new Set<string>();

  for (const w of modelWindows) {
    const id = w.model_id || "";
    if (!id || seenIds.has(id)) continue;
    seenIds.add(id);
    rawEntries.push({
      modelId: id,
      name: w.name || fallbackDisplayName(id),
      pool: w.pool || "未分组",
      percent: w.remaining_percent ?? null,
      resetTime: w.reset_time ?? null,
      tokenType: tokenById.get(id) || "",
    });
  }

  for (const m of models) {
    const id = m.model_id || "";
    if (!id || seenIds.has(id)) continue;
    seenIds.add(id);
    const mw = winById.get(id);
    rawEntries.push({
      modelId: id,
      name: mw?.name || fallbackDisplayName(id),
      pool: mw?.pool || "未分组",
      percent: m.remaining_percent ?? null,
      resetTime: m.reset_time ?? null,
      tokenType: m.token_type || "",
    });
  }

  // 按 (pool + familyId) 分桶
  const grouped = new Map<string, typeof rawEntries>();
  for (const item of rawEntries) {
    const { familyId } = splitFamily(item.modelId);
    const key = `${item.pool}::${familyId}`;
    const bucket = grouped.get(key);
    if (bucket) bucket.push(item);
    else grouped.set(key, [item]);
  }

  const out: MergedModelItem[] = [];
  for (const [key, items] of grouped.entries()) {
    // 判断家族内所有变体的剩余读数是否基本一致（±0.5% 内）
    const firstPct = items[0].percent;
    const allConsistent = items.every((x) => {
      if (x.percent === null && firstPct === null) return true;
      if (x.percent === null || firstPct === null) return false;
      return Math.abs(x.percent - firstPct) <= 0.5;
    });

    if (items.length > 1 && allConsistent) {
      const efforts = items
        .map((x) => splitFamily(x.modelId).effort)
        .filter(Boolean);
      const effortText = efforts.length > 1
        ? `${efforts.length} 档`
        : efforts.length === 1
          ? (EFFORT_LABEL[efforts[0]] || efforts[0])
          : `${items.length} 变体`;
      const baseName = stripEffortFromDisplay(items[0].name) || items[0].name;
      out.push({
        key,
        pool: items[0].pool,
        displayName: baseName,
        effortBadge: effortText,
        tokenType: items.find((x) => x.tokenType)?.tokenType || "",
        percent: firstPct,
        resetTime: items.find((x) => x.resetTime)?.resetTime ?? null,
        rawIds: items.map((x) => x.modelId),
      });
    } else {
      for (const item of items) {
        out.push({
          key: `${key}::${item.modelId}`,
          pool: item.pool,
          displayName: item.name,
          effortBadge: "",
          tokenType: item.tokenType,
          percent: item.percent,
          resetTime: item.resetTime,
          rawIds: [item.modelId],
        });
      }
    }
  }
  return out;
}

export function AntigravityQuotaSection({
  providerId,
  credentials = [],
}: {
  providerId: number;
  credentials?: ProviderCredentialOut[];
}) {
  const accountCreds = useMemo(
    () => credentials.filter(isAntigravityAccountCred),
    [credentials],
  );
  const preferredDefaultId = useMemo(
    () =>
      accountCreds.find((c) => c.preferred && c.is_active)?.id
      ?? accountCreds.find((c) => c.is_active)?.id
      ?? accountCreds[0]?.id
      ?? null,
    [accountCreds],
  );

  const [selectedCredId, setSelectedCredId] = useState<number | null>(preferredDefaultId);
  const resolvedCredId = useMemo(() => {
    if (accountCreds.length === 0) return null;
    if (selectedCredId !== null && accountCreds.some((c) => c.id === selectedCredId)) {
      return selectedCredId;
    }
    return preferredDefaultId;
  }, [accountCreds, preferredDefaultId, selectedCredId]);

  const activeCred = useMemo(
    () => accountCreds.find((c) => c.id === resolvedCredId) ?? null,
    [accountCreds, resolvedCredId],
  );

  const [data, setData] = useState<AntigravityQuotaOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [requestError, setRequestError] = useState("");
  const [modelsOpen, setModelsOpen] = useState(false);
  const [poolFilter, setPoolFilter] = useState<string>("all");
  const [modelQuery, setModelQuery] = useState("");
  const [nowMs, setNowMs] = useState(() => Date.now());

  const cacheRef = useRef<Map<string, CacheEntry>>(new Map());
  const requestSeq = useRef(0);
  const aliveRef = useRef(true);

  // 切换供应商时重置缓存、选中账号与筛选态
  useEffect(() => {
    cacheRef.current.clear();
    setSelectedCredId(null);
    setData(null);
    setRequestError("");
    setModelsOpen(false);
    setPoolFilter("all");
    setModelQuery("");
  }, [providerId]);

  const credKey = resolvedCredId === null ? "default" : String(resolvedCredId);

  const load = useCallback(async (force = false) => {
    if (!force) {
      const hit = cacheRef.current.get(credKey);
      if (hit && Date.now() - hit.fetchedAt < CLIENT_CACHE_TTL_MS) {
        setData(hit.data);
        setRequestError("");
        return;
      }
      // 过期缓存先立即上屏，避免切账号时闪烁骨架
      if (hit) {
        setData(hit.data);
      } else {
        setData(null);
      }
    }

    const seq = ++requestSeq.current;
    setLoading(true);
    setRequestError("");
    try {
      const out = await api.antigravityQuota(providerId, force, resolvedCredId);
      if (!aliveRef.current || seq !== requestSeq.current) return;
      setData(out);
      if (out.ok !== false) {
        cacheRef.current.set(credKey, { data: out, fetchedAt: Date.now() });
      }
    } catch (e) {
      if (!aliveRef.current || seq !== requestSeq.current) return;
      setRequestError(String(e));
    } finally {
      if (aliveRef.current && seq === requestSeq.current) setLoading(false);
    }
  }, [providerId, resolvedCredId, credKey]);

  useEffect(() => {
    aliveRef.current = true;
    void load(false);
    return () => { aliveRef.current = false; };
  }, [load]);

  // 每 60s 刷新一次本地倒计时显示
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const failText = requestError || (data && data.ok === false ? (data.error || "查询配额失败") : "");
  const summary = data?.summary ?? null;
  const models = data?.models ?? [];
  const modelWindows = data?.model_windows ?? [];

  const mergedModels = useMemo(
    () => mergeModelFamilies(models, modelWindows),
    [models, modelWindows],
  );

  // 构建标准化池卡片列表（pools 优先，summary.groups 兜底）
  const normalizedPools = useMemo<NormalizedPool[]>(() => {
    const modelByPool = new Map<string, MergedModelItem[]>();
    for (const item of mergedModels) {
      const list = modelByPool.get(item.pool);
      if (list) list.push(item);
      else modelByPool.set(item.pool, [item]);
    }

    const rawPools = data?.pools ?? [];
    if (rawPools.length > 0) {
      const byPool = new Map<string, typeof rawPools>();
      for (const win of rawPools) {
        const key = win.pool || "配额";
        const list = byPool.get(key);
        if (list) list.push(win);
        else byPool.set(key, [win]);
      }

      const result: NormalizedPool[] = [];
      for (const [poolName, winList] of byPool.entries()) {
        const windows: NormalizedWindow[] = winList.map((w, idx) => {
          const norm = normalizeWindowNames(w.name);
          const spanSeconds = w.span_seconds ?? norm.spanOrder;
          return {
            id: `${poolName}-${idx}`,
            label: norm.label,
            fullLabel: norm.fullLabel,
            percent: w.remaining_percent ?? null,
            resetTime: w.reset_time ?? null,
            spanSeconds,
          };
        });
        windows.sort((a, b) => a.spanSeconds - b.spanSeconds);

        const poolModels = modelByPool.get(poolName) ?? [];
        const rawCount = poolModels.reduce((acc, m) => acc + m.rawIds.length, 0);
        const hintNames = poolModels.slice(0, 3).map((m) => m.displayName);
        const summaryHint = hintNames.length > 0
          ? `${hintNames.join(" · ")}${poolModels.length > 3 ? " 等" : ""}`
          : (windows.length === 1 ? "独立额度桶" : "共享池额度窗口");

        const validPercents = windows
          .map((w) => w.percent)
          .filter((p): p is number => p !== null && Number.isFinite(p));
        const minPercent = validPercents.length > 0 ? Math.min(...validPercents) : null;

        result.push({
          key: poolName,
          name: poolName,
          isStandalone: windows.length === 1 && rawCount <= 3,
          windows,
          summaryHint,
          minPercent,
          modelCount: rawCount,
        });
      }
      return result;
    }

    // 回退路径：无 pools 字段时兼容 summary.groups
    const groups = (summary?.groups ?? []).filter((g) => g && ((g.buckets?.length ?? 0) > 0 || g.display_name));
    return groups.map((group, gi) => {
      const poolName = normalizeGroupPoolName(group.display_name);
      const windows: NormalizedWindow[] = (group.buckets ?? []).map((b, bi) => {
        const norm = normalizeWindowNames(b.display_name || b.bucket_id, b.window);
        return {
          id: b.bucket_id || `${gi}-${bi}`,
          label: norm.label,
          fullLabel: norm.fullLabel,
          percent: b.remaining_percent ?? null,
          resetTime: b.reset_time ?? null,
          spanSeconds: norm.spanOrder,
        };
      });
      windows.sort((a, b) => a.spanSeconds - b.spanSeconds);
      const validPercents = windows
        .map((w) => w.percent)
        .filter((p): p is number => p !== null && Number.isFinite(p));
      return {
        key: poolName,
        name: poolName,
        isStandalone: windows.length === 1,
        windows,
        summaryHint: cleanGroupDescription(group.description) || "共享池额度窗口",
        minPercent: validPercents.length > 0 ? Math.min(...validPercents) : null,
        modelCount: (modelByPool.get(poolName) ?? []).reduce((acc, m) => acc + m.rawIds.length, 0),
      };
    });
  }, [data?.pools, mergedModels, summary?.groups]);

  // 主卡片区仅展示 Gemini / Claude & GPT / Gemini 4 Argon 三个核心额度池；
  // 若远端命名变更导致三者均未命中，则回退展示全部池避免面板空白。
  const displayPools = useMemo<NormalizedPool[]>(() => {
    const primary = normalizedPools.filter((p) => isPrimaryPoolName(p.name));
    if (primary.length === 0) return normalizedPools;
    const rank = (name: string) => {
      const low = name.trim().toLowerCase();
      if (low === "gemini") return 0;
      if (low.startsWith("claude")) return 1;
      if (low.includes("argon")) return 2;
      return 3;
    };
    return [...primary].sort((a, b) => rank(a.name) - rank(b.name));
  }, [normalizedPools]);

  // 顶部状态总览带：取展示池所有窗口最低剩余水位作为核心健康提示
  const summaryStatus = useMemo(() => {
    let lowestPool = "";
    let lowestWin = "";
    let lowestPct: number | null = null;
    let lowestReset: string | null = null;

    for (const p of displayPools) {
      for (const w of p.windows) {
        if (w.percent === null || !Number.isFinite(w.percent)) continue;
        if (lowestPct === null || w.percent < lowestPct) {
          lowestPct = w.percent;
          lowestPool = p.name;
          lowestWin = w.label;
          lowestReset = w.resetTime;
        }
      }
    }

    const tone = remainTone(lowestPct);
    const countdown = fmtResetCountdown(lowestReset, nowMs);
    let message = "已连接网关，暂未返回额度读数";
    if (lowestPct !== null) {
      if (tone === "danger") {
        message = `${lowestPool} ${lowestWin} 即将耗尽（${fmtPercent(lowestPct)}）${countdown ? `，${countdown}恢复` : ""}`;
      } else if (tone === "warn") {
        message = `${lowestPool} ${lowestWin} 偏紧（${fmtPercent(lowestPct)}），建议错峰或切换其他池`;
      } else {
        message = `${displayPools.length} 个额度池余量充沛，最低水位 ${lowestPool} ${lowestWin} ${fmtPercent(lowestPct)}`;
      }
    }
    return { tone, message };
  }, [displayPools, nowMs]);

  const poolFilterOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of mergedModels) {
      if (!m.pool) continue;
      counts.set(m.pool, (counts.get(m.pool) ?? 0) + 1);
    }
    const names = Array.from(counts.keys());
    if (names.length <= 1) return [];
    return [
      { value: "all", label: "全部", count: mergedModels.length },
      ...names.map((name) => ({ value: name, label: name, count: counts.get(name) ?? 0 })),
    ];
  }, [mergedModels]);

  const effectivePoolFilter = useMemo(() => {
    if (poolFilter === "all") return "all";
    return poolFilterOptions.some((o) => o.value === poolFilter) ? poolFilter : "all";
  }, [poolFilter, poolFilterOptions]);

  const filteredModels = useMemo(() => {
    const q = modelQuery.trim().toLowerCase();
    return mergedModels.filter((m) => {
      if (effectivePoolFilter !== "all" && m.pool !== effectivePoolFilter) return false;
      if (!q) return true;
      return (
        m.displayName.toLowerCase().includes(q)
        || m.rawIds.some((id) => id.toLowerCase().includes(q))
        || m.pool.toLowerCase().includes(q)
      );
    });
  }, [mergedModels, effectivePoolFilter, modelQuery]);

  const loaded = data !== null;
  const canShowModels = loaded && !failText && mergedModels.length > 0;
  const singleAccountLabel = data?.account || (activeCred ? accountDisplayName(activeCred) : "");

  const handlePoolCardClick = (poolName: string) => {
    if (!canShowModels) return;
    if (modelsOpen && effectivePoolFilter === poolName) {
      setPoolFilter("all");
      return;
    }
    setPoolFilter(poolName);
    if (!modelsOpen) setModelsOpen(true);
  };

  const quotaRuleTip = (
    <div style={{ maxWidth: 280, lineHeight: 1.55 }}>
      <div>同池内模型按 Token 成本等比共享 5 小时短窗（平滑瞬时并发）与 7 天周窗（绑定账号档位）。</div>
      {summary?.description ? <div style={{ marginTop: 4, opacity: 0.85 }}>{summary.description}</div> : null}
    </div>
  );

  return (
    <div className="models-section">
      <div className="models-section-title">
        <span className="ag-quota-title">
          <span>配额</span>
          {accountCreds.length > 1 ? (
            <Badge size="xs" tone="neutral">{accountCreds.length} 账号</Badge>
          ) : (
            <>
              {singleAccountLabel ? <span className="ag-quota-subacct">{singleAccountLabel}</span> : null}
              {data?.tier ? <Badge size="xs" tone="neutral">{data.tier}</Badge> : null}
            </>
          )}
        </span>
        <div className="ag-quota-head-actions">
          {canShowModels && (
            <Button
              variant="ghost"
              size="xs"
              icon={modelsOpen ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
              aria-expanded={modelsOpen}
              onClick={() => setModelsOpen((v) => !v)}
            >
              模型明细（{models.length}）
            </Button>
          )}
          <Tooltip content="强制刷新当前账号配额（跳过服务端 30 秒缓存）">
            <span>
              <Button
                variant="ghost"
                size="xs"
                icon={<IconRefresh size={12} />}
                loading={loading && loaded}
                onClick={() => void load(true)}
                disabled={loading}
              >
                刷新
              </Button>
            </span>
          </Tooltip>
        </div>
      </div>

      {/* 多账号切换器：仅登录 2 个及以上 Google 账号时显示，默认展示「当前使用」账号 */}
      {accountCreds.length > 1 && (
        <div className="ag-quota-acctbar">
          <span className="ag-quota-acctbar-label">查看账号</span>
          <Select
            size="sm"
            value={resolvedCredId !== null ? String(resolvedCredId) : ""}
            onChange={(v) => setSelectedCredId(v ? Number(v) : null)}
            options={accountCreds.map((c) => {
              const hints: string[] = [];
              if (c.preferred && c.is_active) hints.push("当前使用");
              if (!c.is_active) hints.push("已停用");
              else if (c.demoted || c.status === "cooldown") hints.push("暂缓");
              return {
                value: String(c.id),
                label: accountDisplayName(c),
                hint: hints.join(" · ") || undefined,
              };
            })}
            aria-label="选择查看配额的 Antigravity 账号"
            className="ag-quota-acct-select"
          />
          {activeCred?.preferred && activeCred.is_active && (
            <Badge size="xs" tone="success">当前使用</Badge>
          )}
          {activeCred && !activeCred.is_active && (
            <Badge size="xs" tone="neutral">已停用</Badge>
          )}
          {activeCred && activeCred.is_active && (activeCred.demoted || activeCred.status === "cooldown") && (
            <Badge size="xs" tone="warning">暂缓中</Badge>
          )}
          {data?.tier && <Badge size="xs" tone="neutral">{data.tier}</Badge>}
        </div>
      )}

      {failText ? (
        <div className="models-inline-row">
          <span className="models-err">{failText}</span>
          <Button variant="ghost" size="xs" onClick={() => void load(true)} disabled={loading}>
            {loading ? "重试中…" : "重试"}
          </Button>
        </div>
      ) : !loaded ? (
        <div className="ag-quota-skeleton" aria-label="正在加载配额信息" role="status">
          <span className="mkt-sk" style={{ width: "100%", height: 34 }} />
          <div className="ag-quota-skeleton-grid">
            <span className="mkt-sk" style={{ width: "100%", height: 158 }} />
            <span className="mkt-sk" style={{ width: "100%", height: 158 }} />
            <span className="mkt-sk" style={{ width: "100%", height: 158 }} />
          </div>
        </div>
      ) : displayPools.length === 0 ? (
        <div className="ta3-quota-empty">
          <span className="ta3-quota-empty-ic" aria-hidden><IconGauge size={16} /></span>
          网关没有返回配额分组，请稍后刷新。
        </div>
      ) : (
        <>
          {/* 状态总览带：一眼看清整体水位 + 口径提示浮层 */}
          <div className={`ag-quota-strip tone-${summaryStatus.tone}`}>
            <span className="ag-quota-strip-main">
              <span className="ag-quota-strip-dot" aria-hidden="true" />
              <span className="ag-quota-strip-text">{summaryStatus.message}</span>
            </span>
            <span className="ag-quota-strip-meta">
              <span>{data?.project ? `项目 ${data.project}` : "30s 缓存"}</span>
              <Tooltip content={quotaRuleTip}>
                <span className="ag-quota-info-ic" tabIndex={0} aria-label="查看配额计费口径">
                  <IconInfo size={13} />
                </span>
              </Tooltip>
            </span>
          </div>

          {/* 环形仪表盘池网格：仅展示核心三池（Gemini / Claude & GPT / Gemini 4 Argon） */}
          <div className="ag-quota-grid">
            {displayPools.map((pool) => {
              const poolTone = remainTone(pool.minPercent);
              const isSelected = modelsOpen && effectivePoolFilter === pool.key;
              const tagText = poolTone === "danger"
                ? "即将耗尽"
                : poolTone === "warn"
                  ? "偏紧"
                  : pool.modelCount > 0
                    ? `${pool.modelCount} 模型`
                    : pool.isStandalone
                      ? "独立池"
                      : "共享池";
              const badgeTone = poolTone === "danger" ? "danger" : poolTone === "warn" ? "warning" : "neutral";
              return (
                <div
                  key={pool.key}
                  role={canShowModels ? "button" : undefined}
                  tabIndex={canShowModels ? 0 : undefined}
                  aria-pressed={canShowModels ? isSelected : undefined}
                  className={`ag-quota-pool-card tone-${poolTone}${isSelected ? " is-selected" : ""}${canShowModels ? " is-clickable" : ""}`}
                  onClick={() => handlePoolCardClick(pool.key)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      handlePoolCardClick(pool.key);
                    }
                  }}
                >
                  <div className="ag-quota-pool-card-head">
                    <span className="ag-quota-pool-card-name">{pool.name}</span>
                    <Badge size="xs" tone={badgeTone}>{tagText}</Badge>
                  </div>
                  <div className="ag-quota-rings">
                    {pool.windows.map((win) => (
                      <QuotaRingGauge
                        key={win.id}
                        percent={win.percent}
                        label={win.label}
                        fullLabel={win.fullLabel}
                        resetTime={win.resetTime}
                        nowMs={nowMs}
                      />
                    ))}
                  </div>
                  <Tooltip content={pool.summaryHint}>
                    <div className="ag-quota-pool-card-foot">{pool.summaryHint}</div>
                  </Tooltip>
                </div>
              );
            })}
          </div>
        </>
      )}

      {/* 按模型家族合并的额度明细：走 Collapse 平滑展开/收起，支持按池筛选与搜索 */}
      {canShowModels && (
        <Collapse open={modelsOpen}>
          <div className="ag-quota-models-drawer">
            <div className="ag-quota-models-toolbar">
              {/* plan-20261123-ux-fixes：多池分组改用可换行通用 Button 组，避免单行 Segmented 溢出被 Collapse 裁切 */}
              {poolFilterOptions.length > 0 ? (
                <div className="ag-quota-pool-tabs" role="group" aria-label="按额度池筛选模型">
                  {poolFilterOptions.map((opt) => {
                    const active = effectivePoolFilter === opt.value;
                    return (
                      <Tooltip key={opt.value} content={`${opt.label}（${opt.count} 项）`}>
                        <Button
                          type="button"
                          size="xs"
                          variant={active ? "primary" : "subtle"}
                          aria-pressed={active}
                          className="ag-quota-pool-btn"
                          onClick={() => setPoolFilter(opt.value)}
                        >
                          <span className="ag-quota-pool-btn-label">{opt.label}</span>
                          <span className="ag-quota-pool-btn-count">{opt.count}</span>
                        </Button>
                      </Tooltip>
                    );
                  })}
                </div>
              ) : (
                <span className="ag-quota-models-caption">同家族思考档位已自动合并</span>
              )}
              <Input
                size="sm"
                prefix={<IconSearch size={12} />}
                clearable
                value={modelQuery}
                onChange={(e) => setModelQuery(e.target.value)}
                placeholder="筛选模型名或 ID…"
                aria-label="筛选模型"
                wrapClassName="ag-quota-models-search"
              />
            </div>

            {filteredModels.length === 0 ? (
              <div className="ag-quota-models-empty">没有匹配的模型</div>
            ) : (
              <div className="ag-quota-models-grid">
                {filteredModels.map((item) => {
                  const tone = remainTone(item.percent);
                  const exactReset = fmtResetTime(item.resetTime);
                  const tip = [
                    item.rawIds.length > 1 ? `包含变体：${item.rawIds.join(", ")}` : item.rawIds[0],
                    item.pool ? `所属池：${item.pool}` : "",
                    item.tokenType ? `计费类型：${item.tokenType}` : "",
                    exactReset ? `下次重置：${exactReset}` : "",
                  ].filter(Boolean).join(" · ");
                  return (
                    <Tooltip key={item.key} content={tip}>
                      <div className={`ag-quota-model-chip tone-${tone}`}>
                        <div className="ag-quota-model-chip-main">
                          <span className="ag-quota-model-chip-name">{item.displayName}</span>
                          <span className="ag-quota-model-chip-meta">
                            <span>{item.pool}</span>
                            {item.effortBadge ? <Badge size="xs" tone="neutral">{item.effortBadge}</Badge> : null}
                            {!item.effortBadge && item.tokenType ? <Badge size="xs" tone="neutral">{item.tokenType}</Badge> : null}
                          </span>
                        </div>
                        <span className="ag-quota-model-chip-pct">{fmtPercent(item.percent)}</span>
                      </div>
                    </Tooltip>
                  );
                })}
              </div>
            )}
          </div>
        </Collapse>
      )}
    </div>
  );
}
