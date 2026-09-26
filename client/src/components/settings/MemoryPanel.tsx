/** 设置中心：记忆（v2.2 对齐 zcode 3.18；plan-230-1144 M4.1 三层视图；S8 重构 plan-41-197）。
 *
 * 三层化后记忆分 global（跨项目规范）/ project（项目约定）/ session（会话事实），
 * 本面板按作用域分组展示，支持：
 * - scope 页签过滤（全部/全局/项目/会话）；
 * - 候选区标记（低置信、未注入 prompt，可被 memory_search 检索）；
 * - 提升/降级（把会话记忆升级为项目/全局，或反向）；
 * - 过期时间与使用次数展示。
 *
 * S8（plan-41-197）重构：
 * - 列表每项只展示**一行摘要**（此前全文直出，满版文字难以浏览）；
 * - 顶部工具栏（作用域筛选/候选开关/批量操作）**吸顶固定**，列表可独立滑动；
 * - 点击行打开详情弹窗：查看全文、编辑内容（编辑即人工确认）、提升/降级、删除；
 * - 批量管理：多选 + 全选 + 批量删除（破坏性操作走确认弹窗）。
 */
import { useCallback, useEffect, useState } from "react";
import { api, type MemoryEntryOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconX, IconArrowUp, IconRefresh, IconTrash } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { Modal } from "../Modal";
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
  // S8：批量管理
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirmBatch, setConfirmBatch] = useState(false);
  // S8：详情弹窗（查看 + 编辑）
  const [detail, setDetail] = useState<MemoryEntryOut | null>(null);
  const [detailText, setDetailText] = useState("");
  const [detailSaving, setDetailSaving] = useState(false);

  const loadSettings = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setMemoryEnabled(g.memory_enabled !== false);
    } catch {}
  }, []);

  const load = useCallback(async () => {
    try {
      const list = await api.listMemories({
        scope: scopeFilter === "all" ? undefined : scopeFilter,
        includeCandidate,
      });
      setItems(list);
      // 清理已不存在项的选中态（换作用域/刷新后避免"幽灵选中"）
      setSelected((prev) => {
        if (prev.size === 0) return prev;
        const alive = new Set(list.map((m) => m.id));
        const next = new Set([...prev].filter((id) => alive.has(id)));
        return next.size === prev.size ? prev : next;
      });
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

  /** S8：批量删除（逐条调用后端，保持与单项删除同一语义与审计口径） */
  const doBatchDelete = async () => {
    setConfirmBatch(false);
    const ids = Array.from(selected);
    if (ids.length === 0) return;
    try {
      for (const id of ids) await api.deleteMemory(id);
      setSelected(new Set());
      await load();
    } catch (e) { setErr(String(e)); }
  };

  const toggleSelect = (id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const allSelected = items.length > 0 && items.every((m) => selected.has(m.id));
  const toggleSelectAll = () => setSelected(allSelected ? new Set() : new Set(items.map((m) => m.id)));

  /** S8：打开详情（回填可编辑文本） */
  const openDetail = (m: MemoryEntryOut) => {
    setDetail(m);
    setDetailText(m.text || "");
  };

  /** S8：保存详情编辑——内容有变化才请求；后端将清除候选标记 */
  const saveDetail = async () => {
    if (!detail) return;
    const text = detailText.trim();
    if (!text) { setErr("记忆内容不能为空"); return; }
    setDetailSaving(true);
    try {
      if (text !== detail.text) await api.updateMemory(detail.id, { text });
      setDetail(null);
      await load();
    } catch (e) { setErr(String(e)); }
    finally { setDetailSaving(false); }
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

  /** S8：单行摘要行——点击信息区打开详情，复选框用于批量管理 */
  const renderItem = (m: MemoryEntryOut) => {
    const meta = SCOPE_META[(m.scope as string) || "session"] || SCOPE_META.session;
    const checked = selected.has(m.id);
    return (
      <div key={m.id} className={"settings-resource-item mem-item mem-row" + (checked ? " selected" : "")}>
        <Checkbox checked={checked} onChange={() => toggleSelect(m.id)} />
        <div
          className="settings-resource-info mem-click"
          role="button"
          tabIndex={0}
          onClick={() => openDetail(m)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(m); } }}
        >
          <div className="settings-resource-name mem-text mem-one-line" title={m.text}>
            {m.text}
            {m.candidate && <span className="mem-badge cand" title="低置信记忆：不注入 prompt，但可被检索；编辑或提升后转为正式记忆">候选</span>}
          </div>
          <div className="settings-resource-desc">
            <span className="mem-badge" style={{ color: meta.color, borderColor: meta.color }}>{meta.label}</span>
            <span>{m.kind}</span>
            <span>· 使用 {m.usage_count} 次</span>
            {m.expires_at && <span title={m.expires_at}>· 过期 {new Date(Date.parse(m.expires_at)).toLocaleDateString()}</span>}
          </div>
        </div>
        <div className="settings-resource-actions">
          <button className="btn btn-ghost btn-xs" onClick={() => openDetail(m)}>详情</button>
          <button className="btn btn-ghost btn-xs" aria-label="删除" onClick={() => setConfirmTarget(m)}><IconX size={12} /></button>
        </div>
      </div>
    );
  };

  return (
    <div className="mem-page">
      {/* S19（plan-41-197c）：顶部固定区——AI 开关卡片 + 工具栏 + 作用域说明不随列表滚动，
          仅下方 .mem-scroll 列表区独立上下滑动（用户要求：圈起的部分固定、下方可滑）。 */}
      <div className="mem-fixed">
      <div className="settings-card">
        <Row title="AI 主动生成记忆" desc="开启后每轮对话结束时，AI 会自主提取关键事实/偏好写入记忆库；关闭则不再自动生成记忆">
          <Sw checked={memoryEnabled} onChange={handleToggle} />
        </Row>
      </div>

      {/* S8/S19：工具栏——随头部固定（不再用 sticky，避免与列表内容重叠错位） */}
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
        {selected.size > 0 && (
          <button className="btn btn-danger-ghost btn-sm" onClick={() => setConfirmBatch(true)}>
            <IconTrash size={12} /> 删除所选（{selected.size}）
          </button>
        )}
        {items.length > 0 && (
          <button className="btn btn-ghost btn-sm" onClick={toggleSelectAll}>
            {allSelected ? "取消全选" : "全选"}
          </button>
        )}
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
      </div>

      {/* 列表区：独立滚动（头部固定不动） */}
      <div className="mem-scroll">
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
      </div>

      {/* S8：详情弹窗（查看全文 + 编辑 + 提升/降级 + 删除） */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title="记忆详情"
        subtitle={detail
          ? `${SCOPE_META[(detail.scope as string) || "session"]?.label || "会话"}记忆 · ${detail.kind}`
          : undefined}
        width={620}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setDetail(null)}>取消</button>
            <button
              className="btn btn-primary btn-sm"
              disabled={detailSaving || !detailText.trim()}
              aria-busy={detailSaving}
              onClick={() => void saveDetail()}
            >
              {detailSaving ? "保存中…" : "保存"}
            </button>
          </>
        }
      >
        {detail && (
          <div className="mem-detail">
            <textarea
              className="ui-textarea mem-detail-text"
              value={detailText}
              onChange={(e) => setDetailText(e.target.value)}
              rows={6}
              aria-label="记忆内容"
              placeholder="记忆内容"
            />
            <div className="mem-detail-meta">
              <span>使用 {detail.usage_count} 次</span>
              {detail.candidate && <span className="mem-badge cand">候选</span>}
              {detail.expires_at && (
                <span>过期 {new Date(Date.parse(detail.expires_at)).toLocaleDateString()}</span>
              )}
            </div>
            <div className="mem-detail-actions">
              {(detail.scope as string) !== "global" && (
                <button className="btn btn-ghost btn-sm" disabled={busyId === detail.id}
                  onClick={async () => { await promote(detail, detail.scope === "session" ? "project" : "global"); setDetail(null); }}>
                  <IconArrowUp size={12} /> {detail.scope === "session" ? "提升为项目记忆" : "提升为全局记忆"}
                </button>
              )}
              {(detail.scope as string) !== "session" && (
                <button className="btn btn-ghost btn-sm" disabled={busyId === detail.id}
                  onClick={async () => { await promote(detail, "session"); setDetail(null); }}>
                  降级为会话记忆
                </button>
              )}
              <button className="btn btn-danger-ghost btn-sm" onClick={() => { setConfirmTarget(detail); setDetail(null); }}>
                <IconTrash size={12} /> 删除
              </button>
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除记忆"
        message={`删除该记忆？\n${confirmTarget?.text ?? ""}`}
        danger
        onCancel={() => setConfirmTarget(null)}
        onConfirm={doDelete}
      />

      <ConfirmDialog
        open={confirmBatch}
        title="批量删除记忆"
        message={`将删除选中的 ${selected.size} 条记忆，此操作不可恢复。`}
        danger
        onCancel={() => setConfirmBatch(false)}
        onConfirm={() => void doBatchDelete()}
      />
    </div>
  );
}
