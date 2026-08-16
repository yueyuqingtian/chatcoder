/** 设置中心：执行策略（v2.2 对齐 zcode 3.18）。
 * 控制命令执行审批规则，allow 放行 / deny 拒绝 / ask 需审批，支持批量操作。 */
import { useCallback, useEffect, useState } from "react";
import { api, type ExecPolicyRuleOut } from "../../api/client";
import { IconRefresh, IconPlus, IconX } from "../icons";

const DECISION_OPTS = [
  { value: "allow", label: "放行", color: "var(--success)" },
  { value: "deny", label: "拒绝", color: "var(--error)" },
  { value: "ask", label: "需审批", color: "var(--warning)" },
];

export function PolicyPanel() {
  const [rules, setRules] = useState<ExecPolicyRuleOut[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState({ command_pattern: "", decision: "ask", justification: "" });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const load = useCallback(async () => { try { setRules(await api.listExecPolicyRules()); } catch {} }, []);
  useEffect(() => { load(); }, [load]);
  const resetForm = () => { setForm({ command_pattern: "", decision: "ask", justification: "" }); setEditingId(null); setShowCreate(false); };
  const handleSave = async () => {
    if (!form.command_pattern.trim()) return;
    try { if (editingId != null) await api.deleteExecPolicyRule(editingId); await api.createExecPolicyRule({ command_pattern: form.command_pattern.trim(), decision: form.decision, justification: form.justification.trim() || undefined }); resetForm(); load(); } catch (e) { alert(String(e)); }
  };
  const startEdit = (r: ExecPolicyRuleOut) => { setEditingId(r.id); setForm({ command_pattern: r.command_pattern, decision: r.decision, justification: r.justification || "" }); setShowCreate(true); };
  const toggleSelect = (id: number) => setSelected((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleSelectAll = () => setSelected((prev) => prev.size === rules.length ? new Set() : new Set(rules.map((r) => r.id)));
  const batchDelete = async () => { if (selected.size === 0 || !confirm("删除选中的 " + selected.size + " 条规则？")) return; for (const id of selected) { try { await api.deleteExecPolicyRule(id); } catch {} } setSelected(new Set()); load(); };
  const batchSetDecision = async (decision: string) => { if (selected.size === 0) return; for (const id of selected) { const r = rules.find((x) => x.id === id); if (r && r.decision !== decision) { try { await api.deleteExecPolicyRule(id); await api.createExecPolicyRule({ command_pattern: r.command_pattern, decision, justification: r.justification || undefined }); } catch {} } } setSelected(new Set()); load(); };
  const decisionMeta = (d: string) => DECISION_OPTS.find((o) => o.value === d) ?? DECISION_OPTS[2];
  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 8 }}>
        <button className="btn btn-ghost btn-sm" onClick={load}><IconRefresh size={13} /> 刷新</button>
        <button className="btn btn-primary btn-sm" onClick={() => { if (editingId != null) resetForm(); else setShowCreate(!showCreate); }}><IconPlus size={13} /> 新建规则</button>
      </div>
      {rules.length > 0 && (
        <div className="policy-batch-bar">
          <input type="checkbox" className="policy-checkbox" checked={selected.size === rules.length} onChange={toggleSelectAll} />
          <span>已选 {selected.size}/{rules.length}</span>
          {selected.size > 0 && (<div className="policy-batch-actions"><button className="btn btn-ghost btn-xs" onClick={() => batchSetDecision("allow")} style={{ color: "var(--success)" }}>批量放行</button><button className="btn btn-ghost btn-xs" onClick={() => batchSetDecision("deny")} style={{ color: "var(--error)" }}>批量拒绝</button><button className="btn btn-ghost btn-xs" onClick={() => batchSetDecision("ask")} style={{ color: "var(--warning)" }}>批量审批</button><button className="btn btn-danger btn-xs" onClick={batchDelete}>批量删除</button></div>)}
        </div>
      )}
      {showCreate && (
        <div className="settings-create-form">
          <div className="settings-form-field"><label className="settings-field-label">命令模式（支持通配符）</label><input className="ui-input" placeholder="如 git *、rm -rf *" value={form.command_pattern} onChange={(e) => setForm((p) => ({ ...p, command_pattern: e.target.value }))} /></div>
          <div className="settings-form-field"><label className="settings-field-label">审批决策</label><div className="settings-chips">{DECISION_OPTS.map((opt) => (<button key={opt.value} type="button" className={"settings-chip" + (form.decision === opt.value ? " on" : "")} onClick={() => setForm((p) => ({ ...p, decision: opt.value }))} style={form.decision === opt.value ? { borderColor: opt.color, color: opt.color } : {}}>{opt.label}</button>))}</div></div>
          <div className="settings-form-field"><label className="settings-field-label">理由说明（可选）</label><input className="ui-input" value={form.justification} onChange={(e) => setForm((p) => ({ ...p, justification: e.target.value }))} /></div>
          <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={resetForm}>{editingId != null ? "取消编辑" : "取消"}</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!form.command_pattern.trim()}>{editingId != null ? "保存修改" : "创建"}</button></div>
        </div>
      )}
      <div className="settings-resource-list">
        {rules.map((r) => { const dm = decisionMeta(r.decision); const isSel = selected.has(r.id); return (
          <div key={r.id} className="settings-resource-item" style={isSel ? { background: "var(--accent-soft)" } : {}}>
            <input type="checkbox" className="policy-checkbox" checked={isSel} onChange={() => toggleSelect(r.id)} style={{ flexShrink: 0 }} />
            <div className="settings-resource-info"><div className="settings-resource-name" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{r.command_pattern}</div><div className="settings-resource-desc"><span className="settings-resource-tag" style={{ color: dm.color, borderColor: dm.color }}>{r.decision}</span>{r.justification && <span>{r.justification}</span>}</div></div>
            <div className="settings-resource-actions"><button className="btn btn-ghost btn-xs" onClick={() => startEdit(r)}>编辑</button><button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除规则？")) { try { await api.deleteExecPolicyRule(r.id); load(); } catch {} } }}><IconX size={12} /></button></div>
          </div>); })}
        {rules.length === 0 && !showCreate && <div className="navpage-empty">暂无执行策略规则</div>}
      </div>
    </div>
  );
}
