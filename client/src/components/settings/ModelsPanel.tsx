/** 设置中心：模型管理（v2.2 对齐 zcode 3.18）。
 * 按供应商配置模型：填 URL/Key 后扫描，勾选启用并设置上下文 / 多模态。 */
import { useCallback, useEffect, useState } from "react";
import {
  api, type ModelOut, type ProviderOut,
} from "../../api/client";
import { IconRefresh, IconPlus, IconX } from "../icons";
import { Modal } from "../Modal";
import { Sw } from "./shared";

const REASONING_OPTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
const PROVIDER_OPTS = ["openai", "anthropic", "openai_compatible", "azure_openai", "google", "deepseek", "qwen", "zhipu", "moonshot", "yi", "baichuan", "minimax", "custom"];

function ModelFormModal({ open, editing, onClose, onSaved }: { open: boolean; editing: ModelOut | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ name: "", provider: "openai_compatible", base_url: "", api_key: "", context_window: "200000", reasoning_efforts: [] as string[], is_active: true, is_multimodal: false });
  useEffect(() => {
    if (editing) {
      setForm({ name: editing.name, provider: editing.provider || "openai_compatible", base_url: editing.base_url || "", api_key: "", context_window: String(editing.context_window || 200000), reasoning_efforts: editing.reasoning_efforts || [], is_active: editing.is_active, is_multimodal: editing.is_multimodal });
    } else {
      setForm({ name: "", provider: "openai_compatible", base_url: "", api_key: "", context_window: "200000", reasoning_efforts: [], is_active: true, is_multimodal: false });
    }
  }, [editing, open]);
  const handleSave = async () => {
    if (!form.name.trim()) return;
    try {
      const data: Record<string, unknown> = { name: form.name.trim(), provider: form.provider, base_url: form.base_url || undefined, context_window: Number(form.context_window) || undefined, is_active: form.is_active, is_multimodal: form.is_multimodal };
      if (form.api_key) data.api_key = form.api_key;
      if (form.reasoning_efforts.length > 0) data.reasoning_efforts = form.reasoning_efforts;
      if (editing) await api.updateModel(editing.id, data);
      else await api.createModel(data as never);
      onSaved();
      onClose();
    } catch (e) { alert(String(e)); }
  };
  const underProvider = !!editing?.provider_id;
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑模型" : "新建模型"} width={520} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        {underProvider && <div className="settings-modal-form-row"><label>所属供应商</label><span style={{ fontSize: 12, color: "var(--text-2)" }}>{editing!.provider_name || `#${editing!.provider_id}`}（连接配置继承供应商）</span></div>}
        <div className="settings-modal-form-row"><label>模型名称</label><input className="ui-input" placeholder="如 glm-5.2" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
        {!underProvider && <div className="settings-modal-form-row"><label>协议 / Provider</label><select className="ui-select" value={form.provider} onChange={(e) => setForm((p) => ({ ...p, provider: e.target.value }))}>{PROVIDER_OPTS.map((p) => <option key={p} value={p}>{p}</option>)}</select></div>}
        {!underProvider && <div className="settings-modal-form-row"><label>Base URL</label><input className="ui-input" placeholder="https://..." value={form.base_url} onChange={(e) => setForm((p) => ({ ...p, base_url: e.target.value }))} /></div>}
        {!underProvider && <div className="settings-modal-form-row"><label>API Key {editing && "(留空不修改)"}</label><input className="ui-input" placeholder="sk-..." type="password" value={form.api_key} onChange={(e) => setForm((p) => ({ ...p, api_key: e.target.value }))} /></div>}
        <div className="settings-modal-form-row"><label>上下文窗口 (tokens)</label><input className="ui-input" value={form.context_window} onChange={(e) => setForm((p) => ({ ...p, context_window: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>多模态（支持图片输入）</label><Sw checked={form.is_multimodal} onChange={(v) => setForm((p) => ({ ...p, is_multimodal: v }))} /></div>
        <div className="settings-modal-form-row"><label>思考深度档位</label><div className="settings-chips">{REASONING_OPTS.map((eff) => { const on = form.reasoning_efforts.includes(eff); return <button key={eff} type="button" className={"settings-chip" + (on ? " on" : "")} onClick={() => setForm((p) => ({ ...p, reasoning_efforts: on ? p.reasoning_efforts.filter((x) => x !== eff) : [...p.reasoning_efforts, eff] }))}>{eff}</button>; })}</div></div>
        <div className="settings-modal-form-row"><label>启用状态</label><Sw checked={form.is_active} onChange={(v) => setForm((p) => ({ ...p, is_active: v }))} /></div>
        <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!form.name.trim()}>{editing ? "保存" : "创建"}</button></div>
      </div>
    </Modal>
  );
}

/** v16: 供应商编辑弹窗 */
function ProviderFormModal({ open, editing, onClose, onSaved }: { open: boolean; editing: ProviderOut | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ name: "", base_url: "", api_key: "", api_format: "openai", is_active: true });
  useEffect(() => {
    if (editing) setForm({ name: editing.name, base_url: editing.base_url || "", api_key: "", api_format: editing.api_format || "openai", is_active: editing.is_active });
    else setForm({ name: "", base_url: "", api_key: "", api_format: "openai", is_active: true });
  }, [editing, open]);
  const handleSave = async () => {
    if (!form.name.trim() || !form.base_url.trim()) return;
    try {
      const data: Record<string, unknown> = { name: form.name.trim(), base_url: form.base_url.trim(), api_format: form.api_format, is_active: form.is_active };
      if (form.api_key) data.api_key = form.api_key;
      if (editing) await api.updateProvider(editing.id, data);
      else await api.createProvider(data as never);
      onSaved();
      onClose();
    } catch (e) { alert(String(e)); }
  };
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑供应商" : "添加供应商"} width={520} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row"><label>供应商名称</label><input className="ui-input" placeholder="如 BigModel / 9router" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>Base URL</label><input className="ui-input" placeholder="https://.../v1" value={form.base_url} onChange={(e) => setForm((p) => ({ ...p, base_url: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>API Key {editing && "(留空不修改)"}</label><input className="ui-input" placeholder="sk-..." type="password" value={form.api_key} onChange={(e) => setForm((p) => ({ ...p, api_key: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>API 格式</label><select className="ui-select" value={form.api_format} onChange={(e) => setForm((p) => ({ ...p, api_format: e.target.value }))}><option value="openai">openai（兼容接口）</option><option value="anthropic">anthropic</option></select></div>
        <div className="settings-modal-form-row"><label>启用状态</label><Sw checked={form.is_active} onChange={(v) => setForm((p) => ({ ...p, is_active: v }))} /></div>
        <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!form.name.trim() || !form.base_url.trim()}>{editing ? "保存" : "创建"}</button></div>
      </div>
    </Modal>
  );
}

interface ScanItem { name: string; enabled: boolean; context_window: string; is_multimodal: boolean; existing: boolean }

const MULTIMODAL_HINT = /vision|vl-|4v|omni|image|audio|realtime/i;

/** v16: 扫描供应商模型 → 勾选启用 + 逐模型配置 */
function ScanModelsModal({ open, provider, onClose, onSaved }: { open: boolean; provider: ProviderOut | null; onClose: () => void; onSaved: () => void }) {
  const [items, setItems] = useState<ScanItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open || !provider) return;
    setLoading(true); setError(null); setItems([]);
    (async () => {
      try {
        const [scanned, existing] = await Promise.all([
          api.scanProviderModels(provider.id),
          api.listProviderModels(provider.id),
        ]);
        const byName = new Map(existing.map((m) => [m.name, m]));
        setItems(scanned.models.map((s) => {
          const ex = byName.get(s.id);
          return {
            name: s.id,
            enabled: ex ? ex.is_active : false,
            context_window: String(ex?.context_window || s.context_window || 200000),
            is_multimodal: ex ? ex.is_multimodal : MULTIMODAL_HINT.test(s.id),
            existing: !!ex,
          };
        }));
      } catch (e) {
        setError(String(e));
      } finally { setLoading(false); }
    })();
  }, [open, provider]);

  const patch = (name: string, p: Partial<ScanItem>) => setItems((prev) => prev.map((it) => it.name === name ? { ...it, ...p } : it));

  const handleSave = async () => {
    if (!provider) return;
    setSaving(true);
    try {
      const payload = items
        .filter((it) => it.enabled || it.existing)
        .map((it) => ({ name: it.name, is_active: it.enabled, context_window: Number(it.context_window) || undefined, is_multimodal: it.is_multimodal }));
      await api.bulkSaveProviderModels(provider.id, payload);
      onSaved();
      onClose();
    } catch (e) { alert(String(e)); }
    finally { setSaving(false); }
  };

  const enabledCount = items.filter((i) => i.enabled).length;
  return (
    <Modal open={open} onClose={onClose} title={`扫描模型 — ${provider?.name ?? ""}`} width={640} height="auto">
      <div style={{ padding: 18 }}>
        {loading && <div className="navpage-empty">正在向供应商请求模型列表…</div>}
        {error && <div className="navpage-empty" style={{ color: "var(--error)" }}>扫描失败：{error}</div>}
        {!loading && !error && (
          <>
            <div style={{ fontSize: 12, color: "var(--text-3)", marginBottom: 8 }}>共发现 {items.length} 个模型，勾选要启用的模型并配置上下文 / 多模态</div>
            <div className="scan-list">
              {items.map((it) => (
                <div key={it.name} className={"scan-item" + (it.enabled ? "" : " off")}>
                  <input type="checkbox" checked={it.enabled} onChange={(e) => patch(it.name, { enabled: e.target.checked })} />
                  <span className="scan-name" title={it.name}>{it.name}</span>
                  <label>上下文 <input className="ui-input scan-ctx" value={it.context_window} onChange={(e) => patch(it.name, { context_window: e.target.value })} /></label>
                  <label><input type="checkbox" checked={it.is_multimodal} onChange={(e) => patch(it.name, { is_multimodal: e.target.checked })} /> 多模态</label>
                </div>
              ))}
              {items.length === 0 && <div className="navpage-empty">供应商未返回任何模型</div>}
            </div>
          </>
        )}
        <div className="settings-create-actions" style={{ marginTop: 12 }}>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={loading || saving || items.length === 0}>{saving ? "保存中…" : `保存（启用 ${enabledCount} 个）`}</button>
        </div>
      </div>
    </Modal>
  );
}

export function ModelsPanel() {
  const [providers, setProviders] = useState<ProviderOut[]>([]);
  const [independentModels, setIndependentModels] = useState<ModelOut[]>([]);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [expandedModels, setExpandedModels] = useState<ModelOut[]>([]);
  const [showProviderForm, setShowProviderForm] = useState(false);
  const [editingProvider, setEditingProvider] = useState<ProviderOut | null>(null);
  const [scanningProvider, setScanningProvider] = useState<ProviderOut | null>(null);
  const [editingModel, setEditingModel] = useState<ModelOut | null>(null);
  const [showModelForm, setShowModelForm] = useState(false);

  const load = useCallback(async () => {
    try { setProviders(await api.listProviders()); } catch {}
    try { setIndependentModels((await api.listModels()).filter((m) => !m.provider_id)); } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);

  const toggleExpand = async (p: ProviderOut) => {
    if (expandedId === p.id) { setExpandedId(null); return; }
    setExpandedId(p.id);
    try { setExpandedModels(await api.listProviderModels(p.id)); } catch { setExpandedModels([]); }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-ghost btn-sm" onClick={load}><IconRefresh size={13} /> 刷新</button>
        <button className="btn btn-primary btn-sm" onClick={() => { setEditingProvider(null); setShowProviderForm(true); }}><IconPlus size={13} /> 添加供应商</button>
      </div>

      <div className="settings-resource-list">
        {providers.map((p) => (
          <div key={p.id}>
            <div className="settings-resource-item">
              <div className="settings-resource-info">
                <div className="settings-resource-name">{p.name}</div>
                <div className="settings-resource-desc">
                  <span className="settings-resource-tag">{p.api_format}</span>
                  <span>{p.base_url || "-"}</span>
                  <span className="settings-resource-tag">{p.has_api_key ? "已配置 Key" : "无 Key"}</span>
                  <span className="settings-resource-tag">{p.model_count} 个模型</span>
                </div>
              </div>
              <div className="settings-resource-actions">
                <Sw checked={p.is_active} onChange={async (v) => { try { await api.updateProvider(p.id, { is_active: v }); load(); } catch {} }} />
                <button className="btn btn-ghost btn-xs" onClick={() => setScanningProvider(p)}>扫描模型</button>
                <button className="btn btn-ghost btn-xs" onClick={() => toggleExpand(p)}>{expandedId === p.id ? "收起" : "模型"}</button>
                <button className="btn btn-ghost btn-xs" onClick={() => { setEditingProvider(p); setShowProviderForm(true); }}>编辑</button>
                <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm(`删除供应商「${p.name}」及其下所有模型？`)) { try { await api.deleteProvider(p.id); load(); } catch {} } }}><IconX size={12} /></button>
              </div>
            </div>
            {expandedId === p.id && (
              <div className="provider-models-sub">
                {expandedModels.length === 0 && <div className="navpage-empty">暂无模型，点击「扫描模型」获取</div>}
                {expandedModels.map((m) => (
                  <div key={m.id} className="settings-resource-item">
                    <div className="settings-resource-info">
                      <div className="settings-resource-name">{m.name}</div>
                      <div className="settings-resource-desc">
                        <span>{m.context_window ? m.context_window + " tokens" : ""}</span>
                        {m.is_multimodal && <span className="settings-resource-tag">多模态</span>}
                        {(m.reasoning_efforts?.length ?? 0) > 0 && <span className="settings-resource-tag">思考: {m.reasoning_efforts.join("/")}</span>}
                      </div>
                    </div>
                    <div className="settings-resource-actions">
                      <Sw checked={m.is_active} onChange={async (v) => { try { await api.updateModel(m.id, { is_active: v }); setExpandedModels(await api.listProviderModels(p.id)); load(); } catch {} }} />
                      <button className="btn btn-ghost btn-xs" onClick={() => { setEditingModel(m); setShowModelForm(true); }}>编辑</button>
                      <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除模型？")) { try { await api.deleteModel(m.id); setExpandedModels(await api.listProviderModels(p.id)); load(); } catch {} } }}><IconX size={12} /></button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        {providers.length === 0 && <div className="navpage-empty">暂无供应商，点击「添加供应商」配置 URL 和 Key 后扫描模型</div>}
      </div>

      {independentModels.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="settings-card-title">独立模型（未归属供应商，自带 URL/Key）</div>
          <div className="settings-resource-list">
            {independentModels.map((m) => (
              <div key={m.id} className="settings-resource-item">
                <div className="settings-resource-info"><div className="settings-resource-name">{m.name}</div><div className="settings-resource-desc"><span className="settings-resource-tag">{m.provider || "-"}</span><span>{m.context_window ? m.context_window + " tokens" : ""}</span>{m.is_multimodal && <span className="settings-resource-tag">多模态</span>}</div></div>
                <div className="settings-resource-actions"><Sw checked={m.is_active} onChange={async (v) => { try { await api.updateModel(m.id, { is_active: v }); load(); } catch {} }} /><button className="btn btn-ghost btn-xs" onClick={() => { setEditingModel(m); setShowModelForm(true); }}>编辑</button><button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除模型？")) { try { await api.deleteModel(m.id); load(); } catch {} } }}><IconX size={12} /></button></div>
              </div>
            ))}
          </div>
        </div>
      )}

      <ProviderFormModal open={showProviderForm} editing={editingProvider} onClose={() => setShowProviderForm(false)} onSaved={load} />
      <ScanModelsModal open={!!scanningProvider} provider={scanningProvider} onClose={() => setScanningProvider(null)} onSaved={load} />
      <ModelFormModal open={showModelForm} editing={editingModel} onClose={() => setShowModelForm(false)} onSaved={async () => { load(); if (expandedId != null) { try { setExpandedModels(await api.listProviderModels(expandedId)); } catch {} } }} />
    </div>
  );
}
