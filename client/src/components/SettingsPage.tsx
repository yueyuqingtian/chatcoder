/** 设置页（v6）：Modal + 左导航 + 右内容。模型弹窗编辑、执行策略批量操作。 */
import { useCallback, useEffect, useState } from "react";
import { useThemeStore } from "../store/theme";
import type { Theme } from "../store/theme";
import { useUiStore } from "../store/ui";
import type { UiPrefs } from "../store/ui";
import {
  api, type ModelOut, type SkillOut, type McpServerOut,
  type ScheduledTaskOut, type ExecPolicyRuleOut, type HookConfigOut,
  type MemoryEntryOut,
} from "../api/client";
import { IconRefresh, IconPlus, IconX } from "./icons";
import { Modal } from "./Modal";

type Tab = "general" | "models" | "skills" | "mcp" | "rules" | "scheduled" | "policy" | "hooks" | "memory" | "diagnostics" | "about";
const THEMES: Record<Theme, string> = { light: "浅色", dark: "深色" };
const REASONING_OPTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
const PROVIDER_OPTS = ["openai", "anthropic", "openai_compatible", "azure_openai", "google", "deepseek", "qwen", "zhipu", "moonshot", "yi", "baichuan", "minimax", "custom"];

function Sw({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return <label className="ui-switch"><input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} /><span className="ui-switch-track" /></label>;
}
function Row({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return <div className="settings-row"><div className="settings-row-info"><div className="settings-row-title">{title}</div><div className="settings-row-desc">{desc}</div></div><div className="settings-row-control">{children}</div></div>;
}

function ModelFormModal({ open, editing, onClose, onSaved }: { open: boolean; editing: ModelOut | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ name: "", provider: "openai_compatible", base_url: "", api_key: "", context_window: "200000", reasoning_efforts: [] as string[], is_active: true });
  useEffect(() => {
    if (editing) {
      setForm({ name: editing.name, provider: editing.provider || "openai_compatible", base_url: editing.base_url || "", api_key: "", context_window: String(editing.context_window || 200000), reasoning_efforts: editing.reasoning_efforts || [], is_active: editing.is_active });
    } else {
      setForm({ name: "", provider: "openai_compatible", base_url: "", api_key: "", context_window: "200000", reasoning_efforts: [], is_active: true });
    }
  }, [editing, open]);
  const handleSave = async () => {
    if (!form.name.trim()) return;
    try {
      const data: Record<string, unknown> = { name: form.name.trim(), provider: form.provider, base_url: form.base_url || undefined, context_window: Number(form.context_window) || undefined, is_active: form.is_active };
      if (form.api_key) data.api_key = form.api_key;
      if (form.reasoning_efforts.length > 0) data.reasoning_efforts = form.reasoning_efforts;
      if (editing) await api.updateModel(editing.id, data);
      else await api.createModel(data as any);
      onSaved();
      onClose();
    } catch (e) { alert(String(e)); }
  };
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑模型" : "新建模型"} width={520} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row"><label>模型名称</label><input className="ui-input" placeholder="如 glm-5.2" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>协议 / Provider</label><select className="ui-select" value={form.provider} onChange={(e) => setForm((p) => ({ ...p, provider: e.target.value }))}>{PROVIDER_OPTS.map((p) => <option key={p} value={p}>{p}</option>)}</select></div>
        <div className="settings-modal-form-row"><label>Base URL</label><input className="ui-input" placeholder="https://..." value={form.base_url} onChange={(e) => setForm((p) => ({ ...p, base_url: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>API Key {editing && "(留空不修改)"}</label><input className="ui-input" placeholder="sk-..." type="password" value={form.api_key} onChange={(e) => setForm((p) => ({ ...p, api_key: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>上下文窗口 (tokens)</label><input className="ui-input" value={form.context_window} onChange={(e) => setForm((p) => ({ ...p, context_window: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>思考深度档位</label><div className="settings-chips">{REASONING_OPTS.map((eff) => { const on = form.reasoning_efforts.includes(eff); return <button key={eff} type="button" className={"settings-chip" + (on ? " on" : "")} onClick={() => setForm((p) => ({ ...p, reasoning_efforts: on ? p.reasoning_efforts.filter((x) => x !== eff) : [...p.reasoning_efforts, eff] }))}>{eff}</button>; })}</div></div>
        <div className="settings-modal-form-row"><label>启用状态</label><Sw checked={form.is_active} onChange={(v) => setForm((p) => ({ ...p, is_active: v }))} /></div>
        <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!form.name.trim()}>{editing ? "保存" : "创建"}</button></div>
      </div>
    </Modal>
  );
}

function ModelsPanel() {
  const [models, setModels] = useState<ModelOut[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<ModelOut | null>(null);
  const load = useCallback(async () => { try { setModels(await api.listModels()); } catch {} }, []);
  useEffect(() => { load(); }, [load]);
  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-ghost btn-sm" onClick={load}><IconRefresh size={13} /> 刷新</button>
        <button className="btn btn-primary btn-sm" onClick={() => { setEditing(null); setShowForm(true); }}><IconPlus size={13} /> 新建</button>
      </div>
      <div className="settings-resource-list">
        {models.map((m) => (
          <div key={m.id} className="settings-resource-item">
            <div className="settings-resource-info"><div className="settings-resource-name">{m.name}</div><div className="settings-resource-desc"><span className="settings-resource-tag">{m.provider || "-"}</span><span>{m.context_window ? m.context_window + " tokens" : ""}</span>{(m.reasoning_efforts?.length ?? 0) > 0 && <span className="settings-resource-tag">思考: {m.reasoning_efforts.join("/")}</span>}</div></div>
            <div className="settings-resource-actions"><Sw checked={m.is_active} onChange={async (v) => { try { await api.updateModel(m.id, { is_active: v }); load(); } catch {} }} /><button className="btn btn-ghost btn-xs" onClick={() => { setEditing(m); setShowForm(true); }}>编辑</button><button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除模型？")) { try { await api.deleteModel(m.id); load(); } catch {} } }}><IconX size={12} /></button></div>
          </div>
        ))}
        {models.length === 0 && <div className="navpage-empty">暂无模型</div>}
      </div>
      <ModelFormModal open={showForm} editing={editing} onClose={() => setShowForm(false)} onSaved={load} />
    </div>
  );
}

function SkillsPanel() {
  const [items, setItems] = useState<SkillOut[]>([]);
  const [repos, setRepos] = useState<Array<{ id: string; name: string; url: string; synced?: boolean; skill_count?: number }>>([]);
  const [showAddRepo, setShowAddRepo] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const [repoName, setRepoName] = useState("");
  const [syncingRepo, setSyncingRepo] = useState<string | null>(null);
  const [repoSkills, setRepoSkills] = useState<{ repoId: string; name: string; skills: Array<{ name: string; display_name: string; description: string; path: string }> } | null>(null);
  const load = useCallback(async () => {
    try { setItems(await api.listSkills()); } catch {}
    try { setRepos(await api.listSkillRepos()); } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);

  const handleAddRepo = async () => {
    if (!repoUrl.trim()) return;
    try {
      await api.createSkillRepo({ url: repoUrl.trim(), name: repoName.trim() || undefined });
      setShowAddRepo(false); setRepoUrl(""); setRepoName(""); load();
    } catch (e) { alert(String(e)); }
  };

  const handleSync = async (repoId: string, name: string) => {
    setSyncingRepo(repoId);
    try {
      const res = await api.syncSkillRepo(repoId);
      setRepoSkills({ repoId, name, skills: res.skills });
      load();
    } catch (e) { alert("同步失败: " + String(e)); }
    finally { setSyncingRepo(null); }
  };

  const handleImport = async (skillName: string) => {
    if (!repoSkills) return;
    try {
      await api.importRepoSkill(repoSkills.repoId, skillName);
      load();
    } catch (e) { alert(String(e)); }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-ghost btn-sm" onClick={load}><IconRefresh size={13} /> 刷新扫描</button>
        <button className="btn btn-primary btn-sm" onClick={() => setShowAddRepo(!showAddRepo)}><IconPlus size={13} /> 添加技能仓库</button>
      </div>

      {showAddRepo && (
        <div className="settings-create-form">
          <input className="ui-input" placeholder="Git 仓库地址 (https://…/repo.git)" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
          <input className="ui-input" placeholder="仓库名称（可选）" value={repoName} onChange={(e) => setRepoName(e.target.value)} />
          <div className="settings-create-actions">
            <button className="btn btn-ghost btn-sm" onClick={() => setShowAddRepo(false)}>取消</button>
            <button className="btn btn-primary btn-sm" onClick={handleAddRepo} disabled={!repoUrl.trim()}>添加</button>
          </div>
        </div>
      )}

      {repos.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div className="settings-card-title">技能仓库（云端 Git）</div>
          <div className="settings-resource-list">
            {repos.map((r) => (
              <div key={r.id} className="settings-resource-item">
                <div className="settings-resource-info">
                  <div className="settings-resource-name">{r.name}</div>
                  <div className="settings-resource-desc">
                    <span className="settings-resource-tag">{r.synced ? `${r.skill_count ?? 0} 个技能` : "未同步"}</span>
                    <span style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.url}</span>
                  </div>
                </div>
                <div className="settings-resource-actions">
                  <button className="btn btn-ghost btn-xs" onClick={() => handleSync(r.id, r.name)} disabled={syncingRepo === r.id}>
                    {syncingRepo === r.id ? "同步中…" : "同步/查看"}
                  </button>
                  <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除仓库？")) { try { await api.deleteSkillRepo(r.id); load(); } catch {} } }}><IconX size={12} /></button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {repoSkills && (
        <div style={{ marginBottom: 16, padding: 10, border: "1px solid var(--border)", borderRadius: "var(--r-sm)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-2)", marginBottom: 8 }}>仓库技能：{repoSkills.name}</div>
          {repoSkills.skills.length === 0 && <div className="navpage-empty">仓库中未发现技能（请确保仓库含 skills/*.md）</div>}
          {repoSkills.skills.map((sk) => (
            <div key={sk.name} className="settings-resource-item" style={{ marginBottom: 4 }}>
              <div className="settings-resource-info">
                <div className="settings-resource-name">{sk.display_name || sk.name}</div>
                <div className="settings-resource-desc"><span>{sk.description || "无描述"}</span><span className="settings-resource-tag">{sk.path}</span></div>
              </div>
              <button className="btn btn-primary btn-xs" onClick={() => handleImport(sk.name)}>导入并启用</button>
            </div>
          ))}
        </div>
      )}

      <div className="settings-resource-list">
        {items.map((s) => (
          <div key={s.id} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">{s.display_name || s.name}</div>
              <div className="settings-resource-desc"><span>{s.description || "无描述"}</span><span className="settings-resource-tag">{s.source}</span></div>
            </div>
            <div className="settings-resource-actions">
              <Sw checked={s.is_active} onChange={async (v) => { try { await api.updateSkill(s.id, { is_active: v }); load(); } catch {} }} />
              <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除？")) { try { await api.deleteSkill(s.id); load(); } catch {} } }}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无技能</div>}
      </div>
    </div>
  );
}

function McpPanel() {
  const [items, setItems] = useState<McpServerOut[]>([]);
  const [candidates, setCandidates] = useState<any[]>([]);
  const [scanning, setScanning] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name: "", transport: "stdio", command: "", url: "" });
  const load = useCallback(async () => { try { setItems(await api.listMcpServers()); } catch {} }, []);
  useEffect(() => { load(); }, [load]);
  const handleScan = async () => { setScanning(true); try { const result = await api.scanMcpServers(); const existing = new Set(items.map((m) => m.name)); setCandidates(result.filter((c: any) => !existing.has(c.name))); } catch (e) { alert("扫描失败: " + String(e)); } finally { setScanning(false); } };
  const handleImport = async (c: any) => { try { await api.createMcpServer({ name: c.name, transport: c.transport, command: c.command || undefined, args: c.args, env: c.env || undefined, url: c.url || undefined, is_active: false }); setCandidates((prev) => prev.filter((x) => x !== c)); load(); } catch (e) { alert("导入失败: " + String(e)); } };
  const handleCreate = async () => { if (!form.name.trim()) return; try { await api.createMcpServer({ name: form.name.trim(), transport: form.transport, command: form.command || undefined, url: form.url || undefined, is_active: true }); setShowCreate(false); setForm({ name: "", transport: "stdio", command: "", url: "" }); load(); } catch (e) { alert(String(e)); } };
  return (<div><div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}><button className="btn btn-ghost btn-sm" onClick={handleScan} disabled={scanning}><IconRefresh size={13} /> {scanning ? "扫描中…" : "自动扫描本机"}</button><button className="btn btn-primary btn-sm" onClick={() => setShowCreate(!showCreate)}><IconPlus size={13} /> 手动创建</button></div>{showCreate && (<div className="settings-create-form"><input className="ui-input" placeholder="名称" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /><select className="ui-select" value={form.transport} onChange={(e) => setForm((p) => ({ ...p, transport: e.target.value }))}><option value="stdio">stdio</option><option value="sse">sse</option></select>{form.transport === "stdio" ? <input className="ui-input" placeholder="命令" value={form.command} onChange={(e) => setForm((p) => ({ ...p, command: e.target.value }))} /> : <input className="ui-input" placeholder="URL" value={form.url} onChange={(e) => setForm((p) => ({ ...p, url: e.target.value }))} />}<div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={() => setShowCreate(false)}>取消</button><button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={!form.name.trim()}>创建</button></div></div>)}<div className="settings-resource-list">{items.map((m) => (<div key={m.id} className="settings-resource-item"><div className="settings-resource-info"><div className="settings-resource-name">{m.display_name || m.name}</div><div className="settings-resource-desc"><span className="settings-resource-tag">{m.transport}</span><span>{m.transport === "stdio" ? m.command || "stdio" : m.url || "sse"}</span></div></div><div className="settings-resource-actions"><Sw checked={m.is_active} onChange={async (v) => { try { await api.updateMcpServer(m.id, { is_active: v }); load(); } catch {} }} /><button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除？")) { try { await api.deleteMcpServer(m.id); load(); } catch {} } }}><IconX size={12} /></button></div></div>))}{items.length === 0 && !showCreate && <div className="navpage-empty">暂无 MCP 服务器</div>}</div>{candidates.length > 0 && (<div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--border)" }}><div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-2)", marginBottom: 8 }}>扫描候选</div>{candidates.map((c, i) => (<div key={i} className="settings-resource-item" style={{ marginBottom: 4 }}><div className="settings-resource-info"><div className="settings-resource-name">{c.name}</div><div className="settings-resource-desc"><span className="settings-resource-tag">{c.transport}</span></div></div><button className="btn btn-primary btn-xs" onClick={() => handleImport(c)}>导入</button></div>))}</div>)}</div>);
}

function AiRulesPanel() {
  const [sources, setSources] = useState<Array<{ source: string; label: string; enabled: boolean }>>([]);
  const [globalRules, setGlobalRules] = useState("");
  const [workdirRules, setWorkdirRules] = useState("");
  const [scanned, setScanned] = useState<Array<{ source: string; label: string; path: string; exists: boolean; kind: string }>>([]);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const cfg = await api.getAiRules();
      setSources(cfg.sources);
      setGlobalRules(cfg.global_rules || "");
      setWorkdirRules(cfg.workdir_rules || "");
    } catch {}
    try { setScanned(await api.scanAiRules()); } catch {}
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleScan = async () => {
    try { setScanned(await api.scanAiRules()); } catch (e) { alert("扫描失败: " + String(e)); }
  };

  const toggleSource = (src: string) => {
    setSources((prev) => prev.map((s) => (s.source === src ? { ...s, enabled: !s.enabled } : s)));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.setAiRules({
        enabled_sources: sources.filter((s) => s.enabled).map((s) => s.source),
        global_rules: globalRules,
        workdir_rules: workdirRules,
      });
      load();
    } catch (e) { alert("保存失败: " + String(e)); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <span style={{ fontSize: 12, color: "var(--text-3)" }}>扫描项目根目录下常见 AI 软件的规则文档，并按来源启用 / 停用</span>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-ghost btn-sm" onClick={handleScan}><IconRefresh size={13} /> 重新扫描</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>{saving ? "保存中…" : "保存配置"}</button>
        </div>
      </div>

      <div className="settings-card-title">规则来源（启用后该来源的规则文档会注入 Agent 上下文）</div>
      <div className="settings-resource-list">
        {sources.map((s) => (
          <div key={s.source} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">{s.label}</div>
              <div className="settings-resource-desc">
                {scanned.filter((x) => x.source === s.source).map((x) => (
                  <span key={x.path} className="settings-resource-tag" style={{ color: "var(--accent)" }}>{x.path}</span>
                ))}
                {scanned.filter((x) => x.source === s.source).length === 0 && <span className="settings-resource-tag">未发现规则文档</span>}
              </div>
            </div>
            <Sw checked={s.enabled} onChange={() => toggleSource(s.source)} />
          </div>
        ))}
        {sources.length === 0 && <div className="navpage-empty">暂无规则来源</div>}
      </div>

      <div style={{ marginTop: 16 }} className="settings-card-title">全局规则</div>
      <textarea
        className="ui-textarea"
        rows={4}
        placeholder="全局规则（对所有项目生效）…"
        value={globalRules}
        onChange={(e) => setGlobalRules(e.target.value)}
      />

      <div style={{ marginTop: 16 }} className="settings-card-title">项目规则</div>
      <textarea
        className="ui-textarea"
        rows={4}
        placeholder="当前项目规则…"
        value={workdirRules}
        onChange={(e) => setWorkdirRules(e.target.value)}
      />
    </div>
  );
}

function GenericPanel<T extends { id: number }>({ loader, getName, getDesc, onToggle, onDelete, getActive }: { loader: () => Promise<T[]>; getName: (it: T) => string; getDesc: (it: T) => string; onToggle?: (it: T, v: boolean) => Promise<any>; onDelete: (it: T) => Promise<any>; getActive: (it: T) => boolean; }) {
  const [items, setItems] = useState<T[]>([]);
  const load = useCallback(async () => { try { setItems(await loader()); } catch {} }, [loader]);
  useEffect(() => { load(); }, [load]);
  return (<div className="settings-resource-list">{items.map((it) => (<div key={it.id} className="settings-resource-item"><div className="settings-resource-info"><div className="settings-resource-name">{getName(it)}</div><div className="settings-resource-desc">{getDesc(it)}</div></div><div className="settings-resource-actions">{onToggle && <Sw checked={getActive(it)} onChange={async (v) => { try { await onToggle(it, v); load(); } catch {} }} />}<button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除？")) { try { await onDelete(it); load(); } catch {} } }}><IconX size={12} /></button></div></div>))}{items.length === 0 && <div className="navpage-empty">暂无数据</div>}</div>);
}

const DECISION_OPTS = [
  { value: "allow", label: "放行", color: "var(--success)" },
  { value: "deny", label: "拒绝", color: "var(--error)" },
  { value: "ask", label: "需审批", color: "var(--warning)" },
];

function ExecPolicyPanel() {
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

const TABS: { k: Tab; l: string }[] = [
  { k: "general", l: "通用" }, { k: "models", l: "模型" }, { k: "skills", l: "技能" },
  { k: "mcp", l: "MCP" }, { k: "rules", l: "AI 规则" }, { k: "scheduled", l: "定时任务" }, { k: "policy", l: "执行策略" },
  { k: "hooks", l: "钩子" }, { k: "memory", l: "记忆" }, { k: "diagnostics", l: "诊断" }, { k: "about", l: "关于" },
];

export function SettingsPage({ onBack }: { onBack: () => void; initialTab?: string }) {
  const { theme, setTheme } = useThemeStore();
  const ui = useUiStore();
  const [tab, setTab] = useState<Tab>("general");
  return (
    <Modal open onClose={onBack} title="设置" width={960} height={600}>
      <div className="settings-modal-body">
        <nav className="settings-nav">{TABS.map((t) => <div key={t.k} className={"settings-nav-item" + (tab === t.k ? " active" : "")} onClick={() => setTab(t.k)}>{t.l}</div>)}</nav>
        <div className="settings-content">
          {tab === "general" && (<div className="settings-content-inner"><div className="settings-page-title">偏好设置</div><div className="settings-page-subtitle">主题、语言、字体与个性化外观</div><div className="settings-card"><Row title="主题模式" desc="浅色 / 深色"><div style={{ display: "flex", gap: 6 }}>{(Object.keys(THEMES) as Theme[]).map((t) => <button key={t} className={"settings-pill" + (theme === t ? " active" : "")} onClick={() => setTheme(t)}>{THEMES[t]}</button>)}</div></Row><Row title="界面语言" desc="中文 / English"><select className="ui-select" value={ui.language} onChange={(e) => ui.setLanguage(e.target.value as "zh" | "en")}><option value="zh">中文</option><option value="en">English</option></select></Row><Row title="毛玻璃效果" desc="启用半透明背景模糊"><Sw checked={ui.glassmorphism} onChange={(v) => ui.setPrefs({ glassmorphism: v })} /></Row></div><div className="settings-card"><Row title="左侧面板宽度" desc="可在主界面直接拖拽分隔条调整"><div className="settings-slider-wrap"><input type="range" className="settings-slider" min={200} max={480} step={4} value={ui.leftPanelWidth} onChange={(e) => ui.setPrefs({ leftPanelWidth: Number(e.target.value) })} /><span className="settings-slider-value">{ui.leftPanelWidth}px</span></div></Row><Row title="右侧面板宽度" desc="可在主界面直接拖拽分隔条调整"><div className="settings-slider-wrap"><input type="range" className="settings-slider" min={200} max={1200} step={10} value={ui.rightPanelWidth} onChange={(e) => ui.setPrefs({ rightPanelWidth: Number(e.target.value) })} /><span className="settings-slider-value">{ui.rightPanelWidth}px</span></div></Row><Row title="对话字号" desc="控制对话消息的文字大小"><div className="settings-slider-wrap"><input type="range" className="settings-slider" min={11} max={18} step={1} value={ui.chatFontSize} onChange={(e) => ui.setPrefs({ chatFontSize: Number(e.target.value) })} /><span className="settings-slider-value">{ui.chatFontSize}px</span></div></Row><Row title="消息行距" desc="控制对话消息的行间间距（倍率）"><div className="settings-slider-wrap"><input type="range" className="settings-slider" min={1.2} max={2.2} step={0.05} value={ui.chatLineHeight} onChange={(e) => ui.setPrefs({ chatLineHeight: Number(e.target.value) })} /><span className="settings-slider-value">{ui.chatLineHeight.toFixed(2)}</span></div></Row><Row title="内容展示宽度" desc="0 表示不限制"><div className="settings-slider-wrap"><input type="range" className="settings-slider" min={0} max={1200} step={50} value={ui.contentMaxWidth} onChange={(e) => ui.setPrefs({ contentMaxWidth: Number(e.target.value) })} /><span className="settings-slider-value">{ui.contentMaxWidth === 0 ? "不限" : ui.contentMaxWidth + "px"}</span></div></Row></div><div className="settings-card"><div className="settings-card-title">特殊文字颜色</div><div className="settings-color-grid">{([["chatCodeColor", "代码块", ui.chatCodeColor], ["chatHeadingColor", "标题", ui.chatHeadingColor], ["chatLinkColor", "链接", ui.chatLinkColor], ["chatQuoteColor", "引用", ui.chatQuoteColor]] as const).map(([key, label, val]) => (<div key={key} className="settings-color-item"><span>{label}</span><input type="color" className="settings-color-input" value={val} onChange={(e) => ui.setPrefs({ [key]: e.target.value } as Partial<UiPrefs>)} /></div>))}</div></div><div className="settings-card"><div className="settings-card-title">左侧面板外观</div><Row title="文字大小" desc="左侧面板会话与导航文字"><div className="settings-slider-wrap"><input type="range" className="settings-slider" min={10} max={16} step={1} value={ui.sidebarFontSize} onChange={(e) => ui.setPrefs({ sidebarFontSize: Number(e.target.value) })} /><span className="settings-slider-value">{ui.sidebarFontSize}px</span></div></Row><Row title="图标大小" desc="左侧面板图标尺寸"><div className="settings-slider-wrap"><input type="range" className="settings-slider" min={10} max={20} step={1} value={ui.sidebarIconSize} onChange={(e) => ui.setPrefs({ sidebarIconSize: Number(e.target.value) })} /><span className="settings-slider-value">{ui.sidebarIconSize}px</span></div></Row><Row title="聚焦颜色" desc="选中会话/导航的强调色（留空使用默认）"><div style={{ display: "flex", alignItems: "center", gap: 8 }}><input type="color" className="settings-color-input" value={ui.sidebarFocusColor || "#1A1A1E"} onChange={(e) => ui.setPrefs({ sidebarFocusColor: e.target.value })} /><button className="btn btn-ghost btn-xs" onClick={() => ui.setPrefs({ sidebarFocusColor: "" })}>重置</button></div></Row></div></div>)}
          {tab === "models" && <div className="settings-content-inner"><div className="settings-page-title">模型管理</div><div className="settings-page-subtitle">配置多 Provider 模型，含思考深度档位</div><div className="settings-card"><ModelsPanel /></div></div>}
          {tab === "skills" && <div className="settings-content-inner"><div className="settings-page-title">技能管理</div><div className="settings-page-subtitle">可被 Agent 加载的 Skill 资源</div><div className="settings-card"><SkillsPanel /></div></div>}
          {tab === "mcp" && <div className="settings-content-inner"><div className="settings-page-title">MCP 服务器</div><div className="settings-page-subtitle">连接外部工具与数据源</div><div className="settings-card"><McpPanel /></div></div>}
          {tab === "rules" && <div className="settings-content-inner"><div className="settings-page-title">AI 规则</div><div className="settings-page-subtitle">全局 / 项目规则，以及多 AI 软件规则文档的扫描与启用</div><div className="settings-card"><AiRulesPanel /></div></div>}
          {tab === "scheduled" && <div className="settings-content-inner"><div className="settings-page-title">定时任务</div><div className="settings-card"><GenericPanel<ScheduledTaskOut> loader={() => api.listScheduledTasks()} getName={(t) => t.name} getDesc={(t) => t.cron + (t.next_run_at ? " - " + t.next_run_at : "")} onToggle={async (t, v) => api.updateScheduledTask(t.id, { enabled: v })} onDelete={async (t) => api.deleteScheduledTask(t.id)} getActive={(t) => t.enabled} /></div></div>}
          {tab === "policy" && <div className="settings-content-inner"><div className="settings-page-title">执行策略</div><div className="settings-page-subtitle">控制命令执行审批规则，allow 放行 / deny 拒绝 / ask 需审批</div><div className="settings-card"><ExecPolicyPanel /></div></div>}
          {tab === "hooks" && <div className="settings-content-inner"><div className="settings-page-title">钩子</div><div className="settings-card"><GenericPanel<HookConfigOut> loader={() => api.listHooks()} getName={(h) => h.event} getDesc={(h) => h.command} onToggle={async (h, v) => api.updateHook(h.id, { enabled: v })} onDelete={async (h) => api.deleteHook(h.id)} getActive={(h) => h.enabled} /></div></div>}
          {tab === "memory" && <div className="settings-content-inner"><div className="settings-page-title">记忆</div><div className="settings-card"><GenericPanel<MemoryEntryOut> loader={() => api.listMemories()} getName={(m) => m.text} getDesc={(m) => m.kind + " - 使用 " + m.usage_count + " 次"} onDelete={async (m) => api.deleteMemory(m.id)} getActive={() => false} /></div></div>}
          {tab === "diagnostics" && <div className="settings-content-inner"><div className="settings-page-title">诊断</div><div className="settings-page-subtitle">系统健康检查</div><div className="settings-card"><div className="navpage-empty">点击下方按钮运行诊断</div><button className="btn btn-ghost btn-sm" onClick={() => api.runDiagnostics().then((d) => alert(d.checks.map((c) => c.name + ": " + (c.ok ? "正常" : "异常")).join("\n"))).catch(() => {})}><IconRefresh size={13} /> 运行诊断</button></div></div>}
          {tab === "about" && <div className="settings-content-inner"><div className="settings-page-title">关于</div><div className="settings-card"><Row title="ChatCoder" desc="项目任务驱动的 AI 编码工作台"><span /></Row></div></div>}
        </div>
      </div>
    </Modal>
  );
}
