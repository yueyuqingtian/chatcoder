/** 设置中心：执行策略（v2.2 对齐 zcode 3.18；v3.0 (plan-88) 支持工具级规则 UI）。
 * 命令规则：前缀匹配（allow 放行 / deny 拒绝 / ask 需审批），附常用前缀快捷选择；
 * 工具规则：下拉选择工具 + 三态决策 chips（作用于工具本身，executor 按 tool_name 匹配）。 */
import { useCallback, useEffect, useState } from "react";
import { api, type ExecPolicyRuleOut, type ExecPolicyToolInfo } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconPlus, IconX } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";

const DECISION_OPTS = [
  { value: "allow", label: "放行", color: "var(--success)" },
  { value: "deny", label: "拒绝", color: "var(--error)" },
  { value: "ask", label: "需审批", color: "var(--warning)" },
];
/** v3.0 (plan-88): 常用命令前缀快捷选择 */
const COMMAND_PRESETS = ["git *", "npm *", "pnpm *", "python *", "pip *", "docker *"];

type RuleType = "command" | "tool";

interface PolicyForm {
  ruleType: RuleType;
  command_pattern: string;
  tool_name: string;
  decision: string;
  justification: string;
}

const EMPTY_FORM: PolicyForm = { ruleType: "command", command_pattern: "", tool_name: "", decision: "ask", justification: "" };

export function PolicyPanel() {
  const [rules, setRules] = useState<ExecPolicyRuleOut[]>([]);
  const [tools, setTools] = useState<ExecPolicyToolInfo[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<PolicyForm>(EMPTY_FORM);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState<{ mode: "one"; id: number } | { mode: "batch" } | null>(null);
  const load = useCallback(async () => { try { setRules(await api.listExecPolicyRules()); } catch {} }, []);
  const loadTools = useCallback(async () => { try { setTools(await api.listExecPolicyTools()); } catch {} }, []);
  useEffect(() => { load(); loadTools(); }, [load, loadTools]);
  const resetForm = () => { setForm(EMPTY_FORM); setEditingId(null); setShowCreate(false); };

  const formValid = () => {
    if (form.ruleType === "tool") return Boolean(form.tool_name);
    return Boolean(form.command_pattern.trim());
  };

  const handleSave = async () => {
    if (!formValid()) return;
    try {
      if (editingId != null) await api.deleteExecPolicyRule(editingId);
      if (form.ruleType === "tool") {
        // 工具级规则：command_pattern 存 "(tool)xxx"（与 ws.py 审批卡"始终允许"生成格式一致）
        await api.createExecPolicyRule({
          command_pattern: `(tool)${form.tool_name}`,
          decision: form.decision,
          justification: form.justification.trim() || undefined,
          tool_name: form.tool_name,
        });
      } else {
        await api.createExecPolicyRule({
          command_pattern: form.command_pattern.trim(),
          decision: form.decision,
          justification: form.justification.trim() || undefined,
        });
      }
      resetForm(); load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
  };

  const startEdit = (r: ExecPolicyRuleOut) => {
    const isTool = Boolean(r.tool_name);
    setEditingId(r.id);
    setForm({
      ruleType: isTool ? "tool" : "command",
      command_pattern: isTool ? "" : r.command_pattern,
      tool_name: r.tool_name || "",
      decision: r.decision,
      justification: r.justification || "",
    });
    setShowCreate(true);
  };

  const toggleSelect = (id: number) => setSelected((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleSelectAll = () => setSelected((prev) => prev.size === rules.length ? new Set() : new Set(rules.map((r) => r.id)));

  const doDelete = async () => {
    if (!confirmDelete) return;
    if (confirmDelete.mode === "batch") {
      for (const id of selected) { try { await api.deleteExecPolicyRule(id); } catch { /* ignore */ } }
      setSelected(new Set());
    } else {
      try { await api.deleteExecPolicyRule(confirmDelete.id); } catch { /* ignore */ }
    }
    setConfirmDelete(null);
    load();
  };

  const batchSetDecision = async (decision: string) => {
    if (selected.size === 0) return;
    for (const id of selected) {
      const r = rules.find((x) => x.id === id);
      if (r && r.decision !== decision) {
        try {
          await api.deleteExecPolicyRule(id);
          await api.createExecPolicyRule({
            command_pattern: r.command_pattern,
            decision,
            justification: r.justification || undefined,
            tool_name: r.tool_name || undefined,
          });
        } catch { /* ignore */ }
      }
    }
    setSelected(new Set()); load();
  };

  const decisionMeta = (d: string) => DECISION_OPTS.find((o) => o.value === d) ?? DECISION_OPTS[2];
  const setRuleType = (t: RuleType) => setForm((p) => ({ ...p, ruleType: t }));

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
          {selected.size > 0 && (<div className="policy-batch-actions"><button className="btn btn-ghost btn-xs" onClick={() => batchSetDecision("allow")} style={{ color: "var(--success)" }}>批量放行</button><button className="btn btn-ghost btn-xs" onClick={() => batchSetDecision("deny")} style={{ color: "var(--error)" }}>批量拒绝</button><button className="btn btn-ghost btn-xs" onClick={() => batchSetDecision("ask")} style={{ color: "var(--warning)" }}>批量审批</button><button className="btn btn-danger btn-xs" onClick={() => setConfirmDelete({ mode: "batch" })}>批量删除</button></div>)}
        </div>
      )}
      {showCreate && (
        <div className="settings-create-form">
          <div className="settings-form-field">
            <label className="settings-field-label">规则类型</label>
            <div className="settings-chips">
              <button type="button" className={"settings-chip" + (form.ruleType === "command" ? " on" : "")} onClick={() => setRuleType("command")}>命令规则</button>
              <button type="button" className={"settings-chip" + (form.ruleType === "tool" ? " on" : "")} onClick={() => setRuleType("tool")}>工具规则</button>
            </div>
          </div>
          {form.ruleType === "tool" ? (
            <div className="settings-form-field">
              <label className="settings-field-label">选择工具（规则作用于工具本身）</label>
              <select className="ui-select" value={form.tool_name} onChange={(e) => setForm((p) => ({ ...p, tool_name: e.target.value }))}>
                <option value="">— 请选择工具 —</option>
                {tools.map((t) => (
                  <option key={t.name} value={t.name}>{t.name}（{t.risk_level} 风险）{t.description ? ` — ${t.description}` : ""}</option>
                ))}
              </select>
            </div>
          ) : (
            <div className="settings-form-field">
              <label className="settings-field-label">命令前缀（支持通配符）</label>
              <input className="ui-input" placeholder="如 git push、npm install *" value={form.command_pattern} onChange={(e) => setForm((p) => ({ ...p, command_pattern: e.target.value }))} />
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                {COMMAND_PRESETS.map((preset) => (
                  <button key={preset} type="button" className="settings-chip" onClick={() => setForm((p) => ({ ...p, command_pattern: preset }))}>{preset}</button>
                ))}
              </div>
            </div>
          )}
          <div className="settings-form-field"><label className="settings-field-label">审批决策</label><div className="settings-chips">{DECISION_OPTS.map((opt) => (<button key={opt.value} type="button" className={"settings-chip" + (form.decision === opt.value ? " on" : "")} onClick={() => setForm((p) => ({ ...p, decision: opt.value }))} style={form.decision === opt.value ? { borderColor: opt.color, color: opt.color } : {}}>{opt.label}</button>))}</div></div>
          <div className="settings-form-field"><label className="settings-field-label">理由说明（可选）</label><input className="ui-input" value={form.justification} onChange={(e) => setForm((p) => ({ ...p, justification: e.target.value }))} /></div>
          <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={resetForm}>{editingId != null ? "取消编辑" : "取消"}</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!formValid()}>{editingId != null ? "保存修改" : "创建"}</button></div>
        </div>
      )}
      <div className="settings-resource-list">
        {rules.map((r) => { const dm = decisionMeta(r.decision); const isSel = selected.has(r.id); const isTool = Boolean(r.tool_name); return (
          <div key={r.id} className="settings-resource-item" style={isSel ? { background: "var(--accent-soft)" } : {}}>
            <input type="checkbox" className="policy-checkbox" checked={isSel} onChange={() => toggleSelect(r.id)} style={{ flexShrink: 0 }} />
            <div className="settings-resource-info">
              <div className="settings-resource-name" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>
                <span className="settings-resource-tag" style={{ color: "var(--text-secondary)", borderColor: "var(--border)" }}>{isTool ? "工具" : "命令"}</span>
                {isTool ? r.tool_name : r.command_pattern}
              </div>
              <div className="settings-resource-desc"><span className="settings-resource-tag" style={{ color: dm.color, borderColor: dm.color }}>{r.decision}</span>{r.justification && <span>{r.justification}</span>}</div>
            </div>
            <div className="settings-resource-actions"><button className="btn btn-ghost btn-xs" onClick={() => startEdit(r)}>编辑</button><button className="btn btn-ghost btn-xs" onClick={() => setConfirmDelete({ mode: "one", id: r.id })}><IconX size={12} /></button></div>
          </div>); })}
        {rules.length === 0 && !showCreate && <div className="navpage-empty">暂无执行策略规则</div>}
      </div>
      <ConfirmDialog
        open={confirmDelete !== null}
        title="删除执行策略规则"
        message={confirmDelete?.mode === "batch" ? `删除选中的 ${selected.size} 条规则？` : "删除该规则？"}
        danger
        onCancel={() => setConfirmDelete(null)}
        onConfirm={doDelete}
      />
    </div>
  );
}
