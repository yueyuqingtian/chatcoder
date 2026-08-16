/** 设置中心：子代理类型（v2.2 对齐 zcode 3.13）。
 * 管理 SubagentProfile：工具白名单、模型覆盖、系统提示词、启停。 */
import { useCallback, useEffect, useState } from "react";
import { api, type ModelOut, type SubagentProfileOut } from "../../api/client";
import { IconPlus, IconX } from "../icons";
import { Modal } from "../Modal";
import { Sw } from "./shared";

function SubagentFormModal({ open, editing, models, onClose, onSaved }: {
  open: boolean; editing: SubagentProfileOut | null; models: ModelOut[];
  onClose: () => void; onSaved: () => void;
}) {
  const [form, setForm] = useState({
    name: "", description: "", tools_whitelist: "", model_id: "", system_prompt: "", is_active: true,
  });
  useEffect(() => {
    if (editing) {
      setForm({
        name: editing.name, description: editing.description || "",
        tools_whitelist: (editing.tools_whitelist || []).join(","),
        model_id: editing.model_id != null ? String(editing.model_id) : "",
        system_prompt: editing.system_prompt || "",
        is_active: editing.is_active,
      });
    } else {
      setForm({ name: "", description: "", tools_whitelist: "", model_id: "", system_prompt: "", is_active: true });
    }
  }, [editing, open]);
  const handleSave = async () => {
    if (!form.name.trim()) return;
    try {
      const data = {
        name: form.name.trim(),
        description: form.description.trim() || undefined,
        tools_whitelist: form.tools_whitelist.trim()
          ? form.tools_whitelist.split(",").map((s) => s.trim()).filter(Boolean)
          : undefined,
        model_id: form.model_id ? Number(form.model_id) : undefined,
        system_prompt: form.system_prompt || undefined,
        is_active: form.is_active,
      };
      if (editing) await api.updateSubagent(editing.id, data);
      else await api.createSubagent(data);
      onSaved();
      onClose();
    } catch (e) { alert(String(e)); }
  };
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑子代理类型" : "新建子代理类型"} width={560} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row"><label>类型名称</label><input className="ui-input" placeholder="如 explore / code-reviewer" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>描述</label><input className="ui-input" placeholder="该子代理的职责说明" value={form.description} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>工具白名单（逗号分隔，留空 = 全量工具）</label><input className="ui-input" placeholder="如 fs_read, search_content" value={form.tools_whitelist} onChange={(e) => setForm((p) => ({ ...p, tools_whitelist: e.target.value }))} /></div>
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
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<SubagentProfileOut | null>(null);
  const load = useCallback(async () => {
    try { setItems(await api.listSubagents()); } catch {}
    try { setModels(await api.listModels()); } catch {}
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
                {(s.tools_whitelist?.length ?? 0) > 0 && <span className="settings-resource-tag">白名单 {s.tools_whitelist!.length} 工具</span>}
                {s.model_id != null && <span className="settings-resource-tag">固定模型 #{s.model_id}</span>}
                {s.system_prompt && <span className="settings-resource-tag">自定义提示词</span>}
              </div>
            </div>
            <div className="settings-resource-actions">
              <Sw checked={s.is_active} onChange={async (v) => { try { await api.updateSubagent(s.id, { is_active: v }); load(); } catch {} }} />
              <button className="btn btn-ghost btn-xs" onClick={() => { setEditing(s); setShowForm(true); }}>编辑</button>
              <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm(`删除子代理类型「${s.name}」？`)) { try { await api.deleteSubagent(s.id); load(); } catch {} } }}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无子代理类型（内置 explore / general 由系统注册）</div>}
      </div>
      <SubagentFormModal open={showForm} editing={editing} models={models} onClose={() => setShowForm(false)} onSaved={load} />
    </div>
  );
}
