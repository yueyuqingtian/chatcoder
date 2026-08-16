/** 设置中心：用量统计（v1.1 重写：全软件 token 总量 + 分模型 + 会话上下文占用）。
 * 三段式：
 * 1. 总览卡：总输入/总输出/推理/缓存命中/调用次数
 * 2. 按模型分组列表：模型名 + 调用次数 + 输入/输出 + 占比条
 * 3. 会话上下文占用（折叠区）：各会话当前窗口占用估算
 */
import { useCallback, useEffect, useState } from "react";
import { api, type SessionOut, type UsageSummaryOut } from "../../api/client";
import { IconRefresh, IconChevronDown } from "../icons";

interface UsageRow {
  sessionId: number;
  title: string;
  total: number;
  context_window: number;
  message_count: number;
}

const RANGE_OPTIONS = [
  { label: "全部", days: 0 },
  { label: "近 7 天", days: 7 },
  { label: "近 30 天", days: 30 },
];

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

export function UsagePanel() {
  const [summary, setSummary] = useState<UsageSummaryOut | null>(null);
  const [days, setDays] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 会话上下文占用（保留原逻辑，折叠展示）
  const [rows, setRows] = useState<UsageRow[]>([]);
  const [showSessions, setShowSessions] = useState(false);

  const load = useCallback(async (d: number) => {
    setLoading(true); setError(null);
    try {
      const sum = await api.getUsageSummary(d);
      setSummary(sum);
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }, []);

  const loadSessions = useCallback(async () => {
    try {
      const sessions: SessionOut[] = await api.listSessions();
      const entries = await Promise.all(
        sessions.slice(0, 30).map(async (s) => {
          try {
            const u = await api.getSessionUsage(s.id);
            return { sessionId: s.id, title: s.title || `#${s.id}`, total: u.total, context_window: u.context_window, message_count: u.message_count };
          } catch { return null; }
        })
      );
      const valid = entries.filter((x): x is UsageRow => x != null);
      valid.sort((a, b) => b.total - a.total);
      setRows(valid);
    } catch { /* 会话占用加载失败不阻塞主统计 */ }
  }, []);

  useEffect(() => { load(0); }, [load]);
  useEffect(() => { if (showSessions) loadSessions(); }, [showSessions, loadSessions]);

  const total = summary?.total;
  const byModel = summary?.by_model ?? [];
  const grandTotal = Math.max(1, total?.total ?? 1);
  const maxTotal = Math.max(1, ...rows.map((r) => r.total));

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 4 }}>
          {RANGE_OPTIONS.map((opt) => (
            <button
              key={opt.days}
              className={`btn btn-sm ${days === opt.days ? "btn-primary" : "btn-ghost"}`}
              onClick={() => { setDays(opt.days); load(opt.days); }}
              disabled={loading}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => load(days)} disabled={loading}>
          <IconRefresh size={13} /> {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {error && <div className="navpage-empty" style={{ color: "var(--error)" }}>加载失败：{error}</div>}

      {/* 1. 总览卡 */}
      {total && (
        <div className="usage-overview-grid">
          <div className="usage-overview-card">
            <div className="usage-overview-label">总输入</div>
            <div className="usage-overview-value">{fmtTokens(total.prompt)}</div>
          </div>
          <div className="usage-overview-card">
            <div className="usage-overview-label">总输出</div>
            <div className="usage-overview-value">{fmtTokens(total.completion)}</div>
          </div>
          <div className="usage-overview-card">
            <div className="usage-overview-label">推理</div>
            <div className="usage-overview-value">{fmtTokens(total.reasoning)}</div>
          </div>
          <div className="usage-overview-card">
            <div className="usage-overview-label">缓存命中</div>
            <div className="usage-overview-value">{fmtTokens(total.cached)}</div>
          </div>
          <div className="usage-overview-card">
            <div className="usage-overview-label">调用次数</div>
            <div className="usage-overview-value">{total.calls.toLocaleString()}</div>
          </div>
        </div>
      )}

      {/* 2. 按模型分组 */}
      {byModel.length > 0 && (
        <div className="usage-section">
          <div className="usage-section-title">按模型分布</div>
          <div className="settings-resource-list">
            {byModel.map((m) => {
              const pct = Math.min(100, Math.round((m.total / grandTotal) * 100));
              return (
                <div key={m.model} className="settings-resource-item">
                  <div className="settings-resource-info">
                    <div className="settings-resource-name">{m.model}</div>
                    <div className="settings-resource-desc">
                      <span>{m.calls} 次调用</span>
                      <span>输入 {fmtTokens(m.prompt)} / 输出 {fmtTokens(m.completion)}</span>
                      {m.cached > 0 && <span>缓存 {fmtTokens(m.cached)}</span>}
                    </div>
                    <div className="usage-bar-wrap" style={{ marginTop: 6, width: "100%" }}>
                      <div className="usage-bar" style={{ width: `${pct}%`, background: "var(--accent)" }} />
                      <span className="usage-bar-label">{fmtTokens(m.total)} tokens（{pct}%）</span>
                    </div>
                  </div>
                  <div style={{ flexShrink: 0, fontSize: 11, color: "var(--text-2)", fontFamily: "var(--font-mono)" }}>
                    {pct}%
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {!loading && !error && !total && byModel.length === 0 && (
        <div className="navpage-empty">暂无用量数据，发送消息后统计</div>
      )}

      {/* 3. 会话上下文占用（折叠） */}
      <div className="usage-section">
        <button className="usage-collapse-btn" onClick={() => setShowSessions((v) => !v)}>
          <IconChevronDown size={13} style={{ transform: showSessions ? "rotate(180deg)" : "none", transition: "transform 0.2s" }} />
          会话上下文占用
        </button>
        {showSessions && (
          <div className="settings-resource-list" style={{ marginTop: 8 }}>
            {rows.length === 0 && <div className="navpage-empty">暂无会话数据</div>}
            {rows.map((r) => {
              const pct = Math.min(100, Math.round((r.total / r.context_window) * 100));
              return (
                <div key={r.sessionId} className="settings-resource-item">
                  <div className="settings-resource-info">
                    <div className="settings-resource-name">{r.title}</div>
                    <div className="settings-resource-desc">
                      <span className="settings-resource-tag">#{r.sessionId}</span>
                      <span>{r.message_count} 条消息</span>
                    </div>
                    <div className="usage-bar-wrap" style={{ marginTop: 6, width: "100%" }}>
                      <div className="usage-bar" style={{ width: `${pct}%`, background: pct > 80 ? "var(--warning)" : "var(--accent)" }} />
                      <span className="usage-bar-label">{Math.round(r.total).toLocaleString()} / {Math.round(r.context_window).toLocaleString()} tokens（{pct}%）</span>
                    </div>
                  </div>
                  <div style={{ flexShrink: 0, fontSize: 11, color: pct > 80 ? "var(--warning)" : "var(--text-2)", fontFamily: "var(--font-mono)" }}>
                    {Math.round((r.total / maxTotal) * 100)}%
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
