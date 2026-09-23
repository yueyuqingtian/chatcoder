/** 设置中心：子代理类型（v2.2 对齐 zcode 3.13；v3.0 (plan-88) 工具权限改 checkbox 多选）。
 * 管理 SubagentProfile：工具权限（勾选=允许，留空=全量）、模型覆盖、系统提示词、启停。 */
import { useCallback, useEffect, useState } from "react";
import { api, type ExecPolicyToolInfo, type ModelOut, type SubagentProfileOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconPlus, IconX } from "../icons";
import { Modal } from "../Modal";
import { ConfirmDialog } from "../ConfirmDialog";
import { Input, Textarea } from "../ui";
import { Sw } from "./shared";
import { ModelPicker } from "../chat/ModelPicker";

/** plan-330-1648 M2: 思考深度档位候选（与 ModelsPanel REASONING_OPTS 同口径） */
const REASONING_OPTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点，统一改用全局提示条。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

/** v36: 模型显示名——「供应商/模型名」（与消息输入框模型选择器同口径）。
 *  模型已被删除时回落 #id，避免标签空白。 */
function modelLabel(models: ModelOut[], id: number): string {
  const m = models.find((x) => x.id === id);
  if (!m) return `#${id}`;
  return m.provider_name ? `${m.provider_name}/${m.name}` : m.name;
}

function SubagentFormModal({ open, editing, models, tools, onClose, onSaved }: {
  open: boolean; editing: SubagentProfileOut | null; models: ModelOut[]; tools: ExecPolicyToolInfo[];
  onClose: () => void; onSaved: () => void;
}) {
  const [form, setForm] = useState({
    name: "", description: "",
    tools_whitelist: new Set<string>(),
    model_id: "", system_prompt: "", is_active: true,
    /** plan-330-1648 M2: 思考深度档位（"" = 跟随会话本轮档位） */
    reasoning_effort: "",
  });
  // v36: 模型选择器（消息输入框同款）展开态——弹窗关闭/切换编辑对象时收起，避免菜单残留
  const [modelOpen, setModelOpen] = useState(false);
  useEffect(() => {
    setModelOpen(false);
    if (editing) {
      setForm({
        name: editing.name, description: editing.description || "",
        tools_whitelist: new Set(editing.tools_whitelist || []),
        model_id: editing.model_id != null ? String(editing.model_id) : "",
        system_prompt: editing.system_prompt || "",
        reasoning_effort: editing.reasoning_effort || "",
        is_active: editing.is_active,
      });
    } else {
      setForm({ name: "", description: "", tools_whitelist: new Set(), model_id: "", system_prompt: "", reasoning_effort: "", is_active: true });
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
  /** plan-330-1648 M2: 档位候选——优先所选模型支持的档位，未选模型时给全档位 */
  const _mid = form.model_id ? Number(form.model_id) : null;
  const _selModel = _mid != null ? models.find((x) => x.id === _mid) : null;
  const effortOptions = _selModel?.reasoning_efforts?.length ? _selModel.reasoning_efforts : REASONING_OPTS;
  const handleSave = async () => {
    if (!form.name.trim()) return;
    try {
      const data = {
        name: form.name.trim(),
        description: form.description.trim() || undefined,
        tools_whitelist: form.tools_whitelist.size > 0 ? [...form.tools_whitelist] : undefined,
        model_id: form.model_id ? Number(form.model_id) : undefined,
        system_prompt: form.system_prompt || undefined,
        // plan-330-1648 M2: 显式 null = 清除覆盖（回落会话档位）
        reasoning_effort: form.reasoning_effort || null,
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
        <div className="settings-modal-form-row"><label>类型名称</label><Input placeholder="如 explore / code-reviewer" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
        <div className="settings-modal-form-row"><label>描述</label><Input placeholder="该子代理的职责说明" value={form.description} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} /></div>
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
        {/* v36: 模型覆盖改用消息输入框同款选择器（「供应商/模型」+ 供应商二级分组菜单）——
            此前是扁平下拉只列模型名，看不出模型属于哪个供应商（用户反馈）。 */}
        <div className="settings-modal-form-row">
          <label>模型覆盖（留空 = 跟随主代理）</label>
          <ModelPicker
            models={models}
            value={form.model_id ? Number(form.model_id) : null}
            onChange={(id) => setForm((p) => ({ ...p, model_id: String(id) }))}
            open={modelOpen}
            onToggle={() => setModelOpen((v) => !v)}
            inheritLabel="跟随主代理"
            onInherit={() => setForm((p) => ({ ...p, model_id: "" }))}
            side="bottom"
          />
        </div>
        {/* plan-330-1648 M2: 思考深度覆盖——留空跟随会话；档位候选随所选模型能力收敛 */}
        <div className="settings-modal-form-row">
          <label>思考深度（留空 = 跟随会话）</label>
          <div className="settings-chips">
            <button type="button"
                    className={"settings-chip" + (form.reasoning_effort === "" ? " on" : "")}
                    onClick={() => setForm((p) => ({ ...p, reasoning_effort: "" }))}>跟随会话</button>
            {effortOptions.map((eff) => (
              <button key={eff} type="button"
                      className={"settings-chip" + (form.reasoning_effort === eff ? " on" : "")}
                      onClick={() => setForm((p) => ({ ...p, reasoning_effort: eff }))}>{eff}</button>
            ))}
          </div>
        </div>
        <div className="settings-modal-form-row"><label>系统提示词</label><Textarea rows={3} placeholder="可选，覆盖默认子代理系统提示词…" value={form.system_prompt} onChange={(e) => setForm((p) => ({ ...p, system_prompt: e.target.value }))} aria-label="系统提示词" /></div>
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
  // v36: 并发治理配置（全局设置）——同时运行上限 + 每轮派发总量上限
  const [limits, setLimits] = useState({ concurrent: 6, perTurn: 10 });
  const [limitsSaving, setLimitsSaving] = useState(false);
  const load = useCallback(async () => {
    try { setItems(await api.listSubagents()); } catch {}
    try { setModels(await api.listModels()); } catch {}
    try { setTools(await api.listExecPolicyTools()); } catch {}
    try {
      const g = await api.getGlobalSettings();
      setLimits({
        concurrent: typeof g.max_concurrent_subagents === "number" ? g.max_concurrent_subagents : 6,
        perTurn: typeof g.max_subagents_per_turn === "number" ? g.max_subagents_per_turn : 10,
      });
    } catch { /* 拉取失败沿用默认值 */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  /** v36: 保存并发上限（后端夹紧到合法区间并即时生效，无需重启） */
  const saveLimits = async () => {
    setLimitsSaving(true);
    try {
      await api.setGlobalSettings({
        max_concurrent_subagents: Math.max(1, Math.min(16, Math.round(limits.concurrent) || 6)),
        max_subagents_per_turn: Math.max(1, Math.min(32, Math.round(limits.perTurn) || 10)),
      });
      notify("已保存：子代理并发上限即时生效");
      load();
    } catch (e) { notify("保存失败: " + String(e)); } finally { setLimitsSaving(false); }
  };

  return (
    <div>
      {/* v36: 并发与上限——超出同时运行上限时新派发排队等待；每轮总量超限直接拒绝 */}
      <div className="subagent-limits">
        <div className="subagent-limits-head">
          <span className="subagent-limits-title">并发与上限</span>
          <button className="btn btn-primary btn-xs" onClick={saveLimits} disabled={limitsSaving}>保存</button>
        </div>
        <div className="subagent-limits-row">
          <label>同时运行的子代理上限</label>
          <Input type="number" min={1} max={16} value={String(limits.concurrent)}
                 onChange={(e) => setLimits((p) => ({ ...p, concurrent: Number(e.target.value) || 1 }))}
                 className="subagent-limits-input" aria-label="同时运行的子代理上限" />
          <span className="subagent-limits-hint">达到上限后新派发排队等待（1~16）</span>
        </div>
        <div className="subagent-limits-row">
          <label>每轮派发总量上限</label>
          <Input type="number" min={1} max={32} value={String(limits.perTurn)}
                 onChange={(e) => setLimits((p) => ({ ...p, perTurn: Number(e.target.value) || 1 }))}
                 className="subagent-limits-input" aria-label="每轮派发总量上限" />
          <span className="subagent-limits-hint">单个回合内最多派发数量，含已完成（1~32）</span>
        </div>
      </div>
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
                {s.model_id != null && <span className="settings-resource-tag">固定模型 {modelLabel(models, s.model_id)}</span>}
                {s.reasoning_effort && <span className="settings-resource-tag">思考深度 {s.reasoning_effort}</span>}
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
