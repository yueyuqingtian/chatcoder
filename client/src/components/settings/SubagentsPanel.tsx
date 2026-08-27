/** 设置中心：子代理类型（v2.2 对齐 zcode 3.13；v3.0 (plan-88) 工具权限改 checkbox 多选）。
 * 管理 SubagentProfile：工具权限（勾选=允许，留空=全量）、模型覆盖、系统提示词、启停。 */
import { useCallback, useEffect, useState } from "react";
import { api, type ExecPolicyToolInfo, type ModelOut, type SubagentProfileOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconPlus, IconX } from "../icons";
import { Modal } from "../Modal";
import { ConfirmDialog } from "../ConfirmDialog";
import { Sw } from "./shared";

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点，统一改用全局提示条。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

function SubagentFormModal({ open, editing, models, tools, onClose, onSaved }: {
  open: boolean; editing: SubagentProfileOut | null; models: ModelOut[]; tools: ExecPolicyToolInfo[];
  onClose: () => void; onSaved: () => void;
}) {
  const [form, setForm] = useState({
    name: "", description: "",
    tools_whitelist: new Set<string>(),
    model_id: "", system_prompt: "", is_active: true,
  });
  useEffect(() => {
    if (editing) {
      setForm({
        name: editing.name, description: editing.description || "",
        tools_whitelist: new Set(editing.tools_whitelist || []),
        model_id: editing.model_id != null ? String(editing.model_id) : "",
        system_prompt: editing.system_prompt || "",
        is_active: editing.is_active,
      });
    } else {
      setForm({ name: "", description: "", tools_whitelist: new Set(), model_id: "", system_prompt: "", is_active: true });
    }
  }, [editing, open]);
  const toggleTool = (name: string) => setForm((p) => {
    const next = new Set(p.tools_whitelist);
    if (next.has(name)) next.delete(name); else next.add(name);
    return { ...p, tools_whitelist: next };
  });
  const setAllTools = (checked: boolean) => setForm((p) => ({
    ...p, tools_whitelist: new Set(checked ? tools.map((t) => t.name) : []),
  }));
  const handleSave = async () => {
    if (!form.name.trim()) return;
    try {
      const data = {
        name: form.name.trim(),
        description: form.description.trim() || undefined,
        tools_whitelist: form.tools_whitelist.size > 0 ? [...form.tools_whitelist] : undefined,
        model_id: form.model_id ? Number(form.model_id) : undefined,
        system_prompt: form.system_prompt || undefined,
        is_active: form.is_active,
      };
      if (editing) await api.updateSubagent(editing.id, data);
      else await api.createSubagent(data);
      onSaved();
      onClose();
    } catch (e) { notify(String(e)); }
  };
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑子代理类型" : "新建子代理类型"} width={640} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row"><label>类型名称</label><input className="ui-input" placeholder="如 explore / code-reviewer" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>描述</label><input className="ui-input" placeholder="该子代理的职责说明" value={form.description} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} /></div>
        <div className="settings-modal-form-row">
          <label>工具权限（勾选 = 允许该工具，留空 = 全量工具）</label>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => setAllTools(true)}>全选</button>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => setAllTools(false)}>清空</button>
            <span style={{ fontSize: 12, color: "var(--text-2)" }}>已选 {form.tools_whitelist.size}/{tools.length}</span>
          </div>
          <div className="subagent-tool-grid">
            {tools.map((t) => {
              const checked = form.tools_whitelist.has(t.name);
              return (
                <label key={t.name} className={"subagent-tool-cell" + (checked ? " on" : "")}>
                  <input type="checkbox" checked={checked} onChange={() => toggleTool(t.name)} />
                  <span className="subagent-tool-name">{t.name}</span>
                  <span className="subagent-tool-risk">{t.risk_level}</span>
                </label>
              );
            })}
          </div>
        </div>
        <div className="settings-modal-form-row"><label>模型覆盖（留空 = 跟随主代理）</label><select className="ui-select" value={form.model_id} onChange={(e) => setForm((p) => ({ ...p, model_id: e.target.value }))}><option value="">跟随主代理</option>{models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></div>
        <div className="settings-modal-form-row"><label>系统提示词</label><textarea className="ui-textarea" rows={3} placeholder="可选，覆盖默认子代理系统提示词…" value={form.system_prompt} onChange={(e) => setForm((p) => ({ ...p, system_prompt: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>启用状态</label><Sw checked={form.is_active} onChange={(v) => setForm((p) => ({ ...p, is_active: v }))} /></div>
        <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!form.name.trim()}>{editing ? "保存" : "创建"}</button></div>
      </div>
    </Modal>
  );
}

export function SubagentsPanel() {
  const [items, setItems] = useState<SubagentProfileOut[]>([]);
  const [models, setModels] = useState<ModelOut[]>([]);
  const [tools, setTools] = useState<ExecPolicyToolInfo[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<SubagentProfileOut | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<SubagentProfileOut | null>(null);
  const load = useCallback(async () => {
    try { setItems(await api.listSubagents()); } catch {}
    try { setModels(await api.listModels()); } catch {}
    try { setTools(await api.listExecPolicyTools()); } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);
  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-primary btn-sm" onClick={() => { setEditing(null); setShowForm(true); }}><IconPlus size={13} /> 新建子代理类型</button>
      </div>
      <div className="settings-resource-list">
        {items.map((s) => (
          <div key={s.id} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">{s.name}</div>
              <div className="settings-resource-desc">
                <span>{s.description || "无描述"}</span>
                {(s.tools_whitelist?.length ?? 0) > 0
                  ? <span className="settings-resource-tag">允许 {s.tools_whitelist!.length} 个工具</span>
                  : <span className="settings-resource-tag">全量工具</span>}
                {s.model_id != null && <span className="settings-resource-tag">固定模型 #{s.model_id}</span>}
                {s.system_prompt && <span className="settings-resource-tag">自定义提示词</span>}
              </div>
            </div>
            <div className="settings-resource-actions">
              <Sw checked={s.is_active} onChange={async (v) => { try { await api.updateSubagent(s.id, { is_active: v }); load(); } catch {} }} />
              <button className="btn btn-ghost btn-xs" onClick={() => { setEditing(s); setShowForm(true); }}>编辑</button>
              <button className="btn btn-ghost btn-xs" onClick={() => setConfirmTarget(s)}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无子代理类型（内置 explore / general 由系统注册）</div>}
      </div>
      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除子代理类型"
        message={confirmTarget ? `删除子代理类型「${confirmTarget.name}」？` : ""}
        danger
        onCancel={() => setConfirmTarget(null)}
        onConfirm={async () => {
          const it = confirmTarget;
          setConfirmTarget(null);
          if (!it) return;
          try { await api.deleteSubagent(it.id); load(); } catch { /* ignore */ }
        }}
      />
      <SubagentFormModal open={showForm} editing={editing} models={models} tools={tools} onClose={() => setShowForm(false)} onSaved={load} />
    </div>
  );
}
