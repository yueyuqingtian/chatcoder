/** 设置中心：记忆（v2.2 对齐 zcode 3.18；plan-230-1144 M4.1 三层视图重写）。
 *
 * 三层化后记忆分 global（跨项目规范）/ project（项目约定）/ session（会话事实），
 * 本面板按作用域分组展示，支持：
 * - scope 页签过滤（全部/全局/项目/会话）；
 * - 候选区标记（低置信、未注入 prompt，可被 memory_search 检索）；
 * - 提升/降级（把会话记忆升级为项目/全局，或反向）；
 * - 过期时间与使用次数展示。 */
import { useCallback, useEffect, useState } from "react";
import { api, type MemoryEntryOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconX, IconArrowUp, IconRefresh } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { Checkbox } from "../ui";
import { Row, Sw } from "./shared";

type ScopeFilter = "all" | "global" | "project" | "session";

const SCOPE_META: Record<string, { label: string; color: string; desc: string }> = {
  global: { label: "全局", color: "var(--info)", desc: "跨项目通用规范/偏好，永不过期" },
  project: { label: "项目", color: "var(--success)", desc: "本项目约定/坑点，永不过期" },
  session: { label: "会话", color: "var(--text-2)", desc: "本会话事实，30 天过期" },
};

export function MemoryPanel() {
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [items, setItems] = useState<MemoryEntryOut[]>([]);
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>("all");
  const [includeCandidate, setIncludeCandidate] = useState(true);
  const [confirmTarget, setConfirmTarget] = useState<MemoryEntryOut | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setMemoryEnabled(g.memory_enabled !== false);
    } catch {}
  }, []);

  const load = useCallback(async () => {
    try {
      setItems(await api.listMemories({
        scope: scopeFilter === "all" ? undefined : scopeFilter,
        includeCandidate,
      }));
    } catch (e) { setErr(String(e)); }
  }, [scopeFilter, includeCandidate]);

  useEffect(() => { loadSettings(); }, [loadSettings]);
  useEffect(() => { void load(); }, [load]);

  const handleToggle = async (val: boolean) => {
    setMemoryEnabled(val);
    try {
      await api.setGlobalSettings({ memory_enabled: val });
    } catch (e) {
      useChatStore.setState({ error: "保存失败: " + String(e) });
      setMemoryEnabled(!val);
    }
  };

  const promote = async (m: MemoryEntryOut, target: "session" | "project" | "global") => {
    setBusyId(m.id); setErr(null);
    try {
      await api.promoteMemory(m.id, target);
      await load();
    } catch (e) { setErr(String(e)); }
    finally { setBusyId(null); }
  };

  const doDelete = async () => {
    const t = confirmTarget;
    setConfirmTarget(null);
    if (!t) return;
    try { await api.deleteMemory(t.id); await load(); }
    catch (e) { setErr(String(e)); }
  };

  // 按作用域分组（all 视图）或在过滤视图下平铺
  const grouped = (() => {
    const order: Array<"global" | "project" | "session"> = ["global", "project", "session"];
    const map: Record<string, MemoryEntryOut[]> = { global: [], project: [], session: [] };
    for (const m of items) {
      const key = (m.scope as string) || "session";
      (map[key] || map.session).push(m);
    }
    return order.map((s) => ({ scope: s, list: map[s] })).filter((g) => g.list.length > 0);
  })();

  const renderItem = (m: MemoryEntryOut) => {
    const meta = SCOPE_META[(m.scope as string) || "session"] || SCOPE_META.session;
    return (
      <div key={m.id} className="settings-resource-item mem-item">
        <div className="settings-resource-info">
          <div className="settings-resource-name mem-text">
            {m.text}
            {m.candidate && <span className="mem-badge cand" title="低置信记忆：不注入 prompt，但可被检索；点击提升可转为正式记忆">候选</span>}
          </div>
          <div className="settings-resource-desc">
            <span className="mem-badge" style={{ color: meta.color, borderColor: meta.color }}>{meta.label}</span>
            <span>{m.kind}</span>
            <span>· 使用 {m.usage_count} 次</span>
            {m.expires_at && <span title={m.expires_at}>· 过期 {new Date(Date.parse(m.expires_at)).toLocaleDateString()}</span>}
          </div>
        </div>
        <div className="settings-resource-actions">
          {(m.scope as string) !== "global" && (
            <button className="btn btn-ghost btn-xs" disabled={busyId === m.id}
              title={(m.scope === "session") ? "提升为项目记忆" : "提升为全局记忆"}
              onClick={() => void promote(m, m.scope === "session" ? "project" : "global")}>
              <IconArrowUp size={12} /> 提升
            </button>
          )}
          {(m.scope as string) !== "session" && (
            <button className="btn btn-ghost btn-xs" disabled={busyId === m.id}
              title="降级为会话记忆（30 天后过期）"
              onClick={() => void promote(m, "session")}>
              降级
            </button>
          )}
          <button className="btn btn-ghost btn-xs" onClick={() => setConfirmTarget(m)}><IconX size={12} /></button>
        </div>
      </div>
    );
  };

  return (
    <div className="settings-card-stack">
      <div className="settings-card">
        <Row title="AI 主动生成记忆" desc="开启后每轮对话结束时，AI 会自主提取关键事实/偏好写入记忆库；关闭则不再自动生成记忆">
          <Sw checked={memoryEnabled} onChange={handleToggle} />
        </Row>
      </div>

      <div className="mem-toolbar">
        <div className="settings-chips">
          {(["all", "global", "project", "session"] as ScopeFilter[]).map((s) => (
            <button key={s} type="button"
              className={"settings-chip" + (scopeFilter === s ? " on" : "")}
              onClick={() => setScopeFilter(s)}>
              {s === "all" ? "全部" : SCOPE_META[s].label}
            </button>
          ))}
        </div>
        <label className="mem-cand-toggle">
          <Checkbox checked={includeCandidate} onChange={setIncludeCandidate} />
          显示候选区
        </label>
        <button className="btn btn-ghost btn-sm" onClick={() => void load()}><IconRefresh size={13} /> 刷新</button>
      </div>

      {err && <div className="sched-error">{err}</div>}
      {scopeFilter === "all" && (
        <div className="mem-scope-hint">
          {(["global", "project", "session"] as const).map((s) => (
            <span key={s}><b style={{ color: SCOPE_META[s].color }}>{SCOPE_META[s].label}</b>：{SCOPE_META[s].desc}</span>
          ))}
        </div>
      )}

      {scopeFilter === "all" ? (
        grouped.length === 0
          ? <div className="navpage-empty">暂无记忆</div>
          : grouped.map((g) => (
              <div key={g.scope} className="mem-group">
                <div className="mem-group-title" style={{ color: SCOPE_META[g.scope].color }}>
                  {SCOPE_META[g.scope].label}记忆（{g.list.length}）
                  <span className="mem-group-desc">{SCOPE_META[g.scope].desc}</span>
                </div>
                <div className="settings-resource-list">{g.list.map(renderItem)}</div>
              </div>
            ))
      ) : (
        <div className="settings-resource-list">
          {items.map(renderItem)}
          {items.length === 0 && <div className="navpage-empty">该作用域暂无记忆</div>}
        </div>
      )}

      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除记忆"
        message={`删除该记忆？\n${confirmTarget?.text ?? ""}`}
        danger
        onCancel={() => setConfirmTarget(null)}
        onConfirm={doDelete}
      />
    </div>
  );
}