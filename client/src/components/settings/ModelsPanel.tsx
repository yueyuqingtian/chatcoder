/** 设置中心：模型管理（v2.2 对齐 zcode 3.18）。
 * 按供应商配置模型：填 URL/Key 后扫描，勾选启用并设置上下文 / 多模态。 */
import { useCallback, useEffect, useState } from "react";
import {
  api, type ModelOut, type ProviderOut,
} from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconPlus, IconX } from "../icons";
import { Modal } from "../Modal";
import { ConfirmDialog } from "../ConfirmDialog";
import { Sw } from "./shared";

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点
 * （返回会话后输入框无法聚焦），统一改用全局 Toast。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

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
    } catch (e) { notify(String(e)); }
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
  // v24: ta3 / workbuddy / trae 为账号登录类型（无需填 URL/Key，服务端地址内置）
  const isOAuthFormat = form.api_format === "ta3" || form.api_format === "workbuddy" || form.api_format === "trae";
  useEffect(() => {
    if (editing) setForm({ name: editing.name, base_url: editing.base_url || "", api_key: "", api_format: editing.api_format || "openai", is_active: editing.is_active });
    else setForm({ name: "", base_url: "", api_key: "", api_format: "openai", is_active: true });
  }, [editing, open]);
  const handleSave = async () => {
    if (!form.name.trim()) return;
    if (!isOAuthFormat && !form.base_url.trim()) return;
    try {
      // 账号登录类型无需填 Base URL：后端内置默认服务端
      const defaultBase: Record<string, string> = {
        ta3: "https://lc.yinhaiyun.com/newcoder",
        workbuddy: "https://copilot.tencent.com",
        trae: "https://trae-api-cn.mchost.guru",
      };
      const data: Record<string, unknown> = {
        name: form.name.trim(),
        base_url: isOAuthFormat ? defaultBase[form.api_format] : form.base_url.trim(),
        api_format: form.api_format,
        is_active: form.is_active,
      };
      if (form.api_key) data.api_key = form.api_key;
      if (editing) await api.updateProvider(editing.id, data);
      else await api.createProvider(data as never);
      onSaved();
      onClose();
    } catch (e) { notify(String(e)); }
  };
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑供应商" : "添加供应商"} width={520} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row"><label>供应商名称</label><input className="ui-input" placeholder="如 Ta+3 牛码" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
        {!isOAuthFormat && <div className="settings-modal-form-row"><label>Base URL</label><input className="ui-input" placeholder="https://.../v1" value={form.base_url} onChange={(e) => setForm((p) => ({ ...p, base_url: e.target.value }))} /></div>}
        {!isOAuthFormat && <div className="settings-modal-form-row"><label>API Key {editing && "(留空不修改)"}</label><input className="ui-input" placeholder="sk-..." type="password" value={form.api_key} onChange={(e) => setForm((p) => ({ ...p, api_key: e.target.value }))} /></div>}
        <div className="settings-modal-form-row"><label>API 格式</label><select className="ui-select" value={form.api_format} onChange={(e) => setForm((p) => ({ ...p, api_format: e.target.value }))}><option value="openai">openai（兼容接口）</option><option value="anthropic">anthropic</option><option value="ta3">ta3（Ta+3 牛码）</option><option value="workbuddy">workbuddy（腾讯 CodeBuddy）</option><option value="trae">trae（TRAE SOLO）</option></select></div>
        {isOAuthFormat && <div style={{ fontSize: 12, color: "var(--text-3)", padding: "0 2px 8px" }}>{form.api_format === "ta3"
          ? "ta3 类型使用账号登录获取模型，服务端地址已内置（lc.yinhaiyun.com/newcoder），无需配置；保存后点击卡片上的「登录 Ta+3 账号」。"
          : form.api_format === "workbuddy"
            ? "workbuddy 类型使用账号登录获取模型，服务端地址已内置（copilot.tencent.com），无需配置；保存后点击卡片上的「登录 WorkBuddy 账号」。"
            : "trae 类型使用账号登录获取模型，服务端地址已内置（trae-api-cn.mchost.guru）；保存后点击卡片上的「登录 TRAE 账号」。"}</div>}
        <div className="settings-modal-form-row"><label>启用状态</label><Sw checked={form.is_active} onChange={(v) => setForm((p) => ({ ...p, is_active: v }))} /></div>
        <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!form.name.trim() || (!isOAuthFormat && !form.base_url.trim())}>{editing ? "保存" : "创建"}</button></div>
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
    } catch (e) { notify(String(e)); }
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
  // v23: ta3 登录状态（providerId → {phase, label?, error?}）
  const [ta3Status, setTa3Status] = useState<Record<number, { phase: "idle" | "pending" | "done" | "failed"; label?: string; error?: string }>>({});
  const [ta3Busy, setTa3Busy] = useState<number | null>(null);
  // v24: workbuddy 登录状态（结构与 ta3 一致）
  const [wbStatus, setWbStatus] = useState<Record<number, { phase: "idle" | "pending" | "done" | "failed"; label?: string; error?: string }>>({});
  const [wbBusy, setWbBusy] = useState<number | null>(null);
  // v25: trae 登录状态（结构与 ta3 一致）
  const [traeStatus, setTraeStatus] = useState<Record<number, { phase: "idle" | "pending" | "done" | "failed"; label?: string; error?: string }>>({});
  const [traeBusy, setTraeBusy] = useState<number | null>(null);

  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    title: string;
    message: string;
    danger?: boolean;
    onConfirm: () => Promise<void> | void;
  }>({
    open: false,
    title: "",
    message: "",
    onConfirm: () => {},
  });

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

  // ── ta3 登录：启动 PKCE 浏览器登录 → 轮询状态 → 成功自动同步模型 ──
  const handleTa3Login = async (p: ProviderOut) => {
    setTa3Status((s) => ({ ...s, [p.id]: { phase: "pending" } }));
    try {
      const start = await api.ta3LoginStart(p.id);
      // 银海通 IM 静默登录：后端直接登录成功，无需打开浏览器
      if (start.status === "logged_in") {
        setTa3Status((s) => ({ ...s, [p.id]: { phase: "done", label: (start.account as Record<string, string>)?.label || "已登录" } }));
        await handleTa3Sync(p.id, false);
        return;
      }
      if (!start.authorize_url) throw new Error(start.status === "failed" ? "登录失败" : "未获取到授权地址");
      const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
      if (w.chatcoderAPI?.openExternal) await w.chatcoderAPI.openExternal(start.authorize_url);
      else window.open(start.authorize_url, "_blank");
      const deadline = Date.now() + ((start.expires_in ?? 90) + 10) * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const st = await api.ta3LoginStatus(p.id);
        if (st.status === "logged_in") {
          setTa3Status((s) => ({ ...s, [p.id]: { phase: "done", label: (st.account as Record<string, string>)?.label || "已登录" } }));
          await handleTa3Sync(p.id, false);
          return;
        }
        if (st.status === "failed") {
          setTa3Status((s) => ({ ...s, [p.id]: { phase: "failed", error: st.error || "登录失败" } }));
          return;
        }
      }
      setTa3Status((s) => ({ ...s, [p.id]: { phase: "failed", error: "登录超时，请重试" } }));
    } catch (e) {
      setTa3Status((s) => ({ ...s, [p.id]: { phase: "failed", error: String(e) } }));
    }
  };

  const handleTa3Sync = async (pid: number, showMsg = true) => {
    setTa3Busy(pid);
    try {
      const r = await api.ta3Sync(pid);
      load();
      if (showMsg) notify(`同步完成：${r.synced} 个模型`);
    } catch (e) { notify(String(e)); }
    finally { setTa3Busy(null); }
  };

  const handleTa3Logout = (p: ProviderOut) => {
    setConfirmDialog({
      open: true,
      title: "退出账号",
      message: `退出 Ta+3 账号「${p.account_label || p.name}」？其下模型将不可用。`,
      danger: true,
      onConfirm: async () => {
        setConfirmDialog((d) => ({ ...d, open: false }));
        try { await api.ta3Logout(p.id); load(); } catch (e) { notify(String(e)); }
      },
    });
  };

  const isTa3 = (p: ProviderOut) => p.api_format === "ta3";

  // ── workbuddy 登录：启动 auth/state → 打开浏览器 → 轮询状态 → 成功自动同步模型 ──
  const handleWbLogin = async (p: ProviderOut) => {
    setWbStatus((s) => ({ ...s, [p.id]: { phase: "pending" } }));
    try {
      const start = await api.workbuddyLoginStart(p.id);
      if (start.status === "logged_in") {
        setWbStatus((s) => ({ ...s, [p.id]: { phase: "done", label: (start.account as Record<string, string>)?.label || "已登录" } }));
        await handleWbSync(p.id, false);
        return;
      }
      if (!start.auth_url) throw new Error(start.status === "failed" ? "登录失败" : "未获取到授权地址");
      const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
      if (w.chatcoderAPI?.openExternal) await w.chatcoderAPI.openExternal(start.auth_url);
      else window.open(start.auth_url, "_blank");
      const deadline = Date.now() + ((start.expires_in ?? 300) + 10) * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000));
        const st = await api.workbuddyLoginStatus(p.id);
        if (st.status === "logged_in") {
          setWbStatus((s) => ({ ...s, [p.id]: { phase: "done", label: (st.account as Record<string, string>)?.label || "已登录" } }));
          await handleWbSync(p.id, false);
          return;
        }
        if (st.status === "failed") {
          setWbStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: st.error || "登录失败" } }));
          return;
        }
      }
      setWbStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: "登录超时，请重试" } }));
    } catch (e) {
      setWbStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: String(e) } }));
    }
  };

  const handleWbSync = async (pid: number, showMsg = true) => {
    setWbBusy(pid);
    try {
      const r = await api.workbuddySync(pid);
      load();
      if (showMsg) notify(`同步完成：${r.synced} 个模型`);
    } catch (e) { notify(String(e)); }
    finally { setWbBusy(null); }
  };

  const handleWbLogout = (p: ProviderOut) => {
    setConfirmDialog({
      open: true,
      title: "退出账号",
      message: `退出 WorkBuddy 账号「${p.account_label || p.name}」？其下模型将不可用。`,
      danger: true,
      onConfirm: async () => {
        setConfirmDialog((d) => ({ ...d, open: false }));
        try { await api.workbuddyLogout(p.id); load(); } catch (e) { notify(String(e)); }
      },
    });
  };

  const isWb = (p: ProviderOut) => p.api_format === "workbuddy";

  // ── trae 登录：本地回调 + 授权页 → 轮询状态 → 成功自动同步模型（对齐 ta3 PKCE 流程）──
  const handleTraeLogin = async (p: ProviderOut) => {
    setTraeStatus((s) => ({ ...s, [p.id]: { phase: "pending" } }));
    try {
      const start = await api.traeLoginStart(p.id);
      if (!start.authorize_url) throw new Error(start.status === "failed" ? "登录失败" : "未获取到授权地址");
      const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
      if (w.chatcoderAPI?.openExternal) await w.chatcoderAPI.openExternal(start.authorize_url);
      else window.open(start.authorize_url, "_blank");
      const deadline = Date.now() + ((start.expires_in ?? 300) + 10) * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const st = await api.traeLoginStatus(p.id);
        if (st.status === "logged_in") {
          setTraeStatus((s) => ({ ...s, [p.id]: { phase: "done", label: (st.account as Record<string, string>)?.label || "已登录" } }));
          await handleTraeSync(p.id, false);
          return;
        }
        if (st.status === "failed") {
          setTraeStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: st.error || "登录失败" } }));
          return;
        }
      }
      setTraeStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: "登录超时，请重试" } }));
    } catch (e) {
      setTraeStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: String(e) } }));
    }
  };

  const handleTraeSync = async (pid: number, showMsg = true) => {
    setTraeBusy(pid);
    try {
      const r = await api.traeSync(pid);
      load();
      if (showMsg) notify(`同步完成：${r.synced} 个模型`);
    } catch (e) { notify(String(e)); }
    finally { setTraeBusy(null); }
  };

  const handleTraeLogout = (p: ProviderOut) => {
    setConfirmDialog({
      open: true,
      title: "退出账号",
      message: `退出 TRAE 账号「${p.account_label || p.name}」？其下模型将不可用。`,
      danger: true,
      onConfirm: async () => {
        setConfirmDialog((d) => ({ ...d, open: false }));
        try { await api.traeLogout(p.id); load(); } catch (e) { notify(String(e)); }
      },
    });
  };

  const isTrae = (p: ProviderOut) => p.api_format === "trae";

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
                  {isTa3(p) || isWb(p) || isTrae(p)
                    ? <>
                        <span className="settings-resource-tag">{p.auth_status === "logged_in" ? `已登录${p.account_label ? `：${p.account_label}` : ""}` : "未登录"}</span>
                        {(isTa3(p) ? ta3Status[p.id] : isWb(p) ? wbStatus[p.id] : traeStatus[p.id])?.phase === "failed" && <span className="settings-resource-tag" style={{ color: "var(--error)" }}>{(isTa3(p) ? ta3Status[p.id] : isWb(p) ? wbStatus[p.id] : traeStatus[p.id]).error}</span>}
                      </>
                    : <span className="settings-resource-tag">{p.has_api_key ? "已配置 Key" : "无 Key"}</span>}
                  <span className="settings-resource-tag">{p.model_count} 个模型</span>
                </div>
              </div>
              <div className="settings-resource-actions">
                <Sw checked={p.is_active} onChange={async (v) => { try { await api.updateProvider(p.id, { is_active: v }); load(); } catch {} }} />
                {isTa3(p) ? (
                  <>
                    <button className="btn btn-ghost btn-xs" onClick={() => handleTa3Login(p)} disabled={ta3Status[p.id]?.phase === "pending"}>
                      {ta3Status[p.id]?.phase === "pending" ? "登录中…" : p.auth_status === "logged_in" ? "重新登录" : "登录 Ta+3 账号"}
                    </button>
                    {p.auth_status === "logged_in" && (
                      <>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleTa3Sync(p.id)} disabled={ta3Busy === p.id}>{ta3Busy === p.id ? "同步中…" : "同步模型"}</button>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleTa3Logout(p)}>退出</button>
                      </>
                    )}
                  </>
                ) : isWb(p) ? (
                  <>
                    <button className="btn btn-ghost btn-xs" onClick={() => handleWbLogin(p)} disabled={wbStatus[p.id]?.phase === "pending"}>
                      {wbStatus[p.id]?.phase === "pending" ? "登录中…" : p.auth_status === "logged_in" ? "重新登录" : "登录 WorkBuddy 账号"}
                    </button>
                    {p.auth_status === "logged_in" && (
                      <>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleWbSync(p.id)} disabled={wbBusy === p.id}>{wbBusy === p.id ? "同步中…" : "同步模型"}</button>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleWbLogout(p)}>退出</button>
                      </>
                    )}
                  </>
                ) : isTrae(p) ? (
                  <>
                    <button className="btn btn-ghost btn-xs" onClick={() => handleTraeLogin(p)} disabled={traeStatus[p.id]?.phase === "pending"}>
                      {traeStatus[p.id]?.phase === "pending" ? "登录中…" : p.auth_status === "logged_in" ? "重新登录" : "登录 TRAE 账号"}
                    </button>
                    {p.auth_status === "logged_in" && (
                      <>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleTraeSync(p.id)} disabled={traeBusy === p.id}>{traeBusy === p.id ? "同步中…" : "同步模型"}</button>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleTraeLogout(p)}>退出</button>
                      </>
                    )}
                  </>
                ) : (
                  <button className="btn btn-ghost btn-xs" onClick={() => setScanningProvider(p)}>扫描模型</button>
                )}
                <button className="btn btn-ghost btn-xs" onClick={() => toggleExpand(p)}>{expandedId === p.id ? "收起" : "模型"}</button>
                <button className="btn btn-ghost btn-xs" onClick={() => { setEditingProvider(p); setShowProviderForm(true); }}>编辑</button>
                <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDialog({
                  open: true,
                  title: "删除供应商",
                  message: `删除供应商「${p.name}」及其下所有模型？`,
                  danger: true,
                  onConfirm: async () => {
                    setConfirmDialog((d) => ({ ...d, open: false }));
                    try { await api.deleteProvider(p.id); load(); } catch { /* ignore */ }
                  },
                })}><IconX size={12} /></button>
              </div>
            </div>
            {expandedId === p.id && (
              <div className="provider-models-sub">
                {expandedModels.length === 0 && <div className="navpage-empty">{(isTa3(p) || isWb(p) || isTrae(p)) ? "暂无模型，请先登录账号并点击「同步模型」" : "暂无模型，点击「扫描模型」获取"}</div>}
                {expandedModels.map((m) => (
                  <div key={m.id} className="settings-resource-item">
                    <div className="settings-resource-info">
                      <div className="settings-resource-name">{m.name}</div>
                      <div className="settings-resource-desc">
                        <span>{m.context_window ? m.context_window + " tokens" : ""}</span>
                        {m.trae_max_context ? <span className="settings-resource-tag">max {m.trae_max_context}</span> : null}
                        {m.trae_consumption_rate ? <span className="settings-resource-tag">消耗×{m.trae_consumption_rate}</span> : null}
                        {m.api_format === "trae" && m.trae_available === false && <span className="settings-resource-tag">不可用</span>}
                        {m.is_multimodal && <span className="settings-resource-tag">多模态</span>}
                        {(m.reasoning_efforts?.length ?? 0) > 0 && <span className="settings-resource-tag">思考: {m.reasoning_efforts.join("/")}</span>}
                      </div>
                    </div>
                    <div className="settings-resource-actions">
                      <Sw checked={m.is_active} onChange={async (v) => { try { await api.updateModel(m.id, { is_active: v }); setExpandedModels(await api.listProviderModels(p.id)); load(); } catch {} }} />
                      <button className="btn btn-ghost btn-xs" onClick={() => { setEditingModel(m); setShowModelForm(true); }}>编辑</button>
                      <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDialog({
                        open: true,
                        title: "删除模型",
                        message: `删除模型「${m.name}」？`,
                        danger: true,
                        onConfirm: async () => {
                          setConfirmDialog((d) => ({ ...d, open: false }));
                          try { await api.deleteModel(m.id); setExpandedModels(await api.listProviderModels(p.id)); load(); } catch { /* ignore */ }
                        },
                      })}><IconX size={12} /></button>
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
                <div className="settings-resource-actions"><Sw checked={m.is_active} onChange={async (v) => { try { await api.updateModel(m.id, { is_active: v }); load(); } catch {} }} /><button className="btn btn-ghost btn-xs" onClick={() => { setEditingModel(m); setShowModelForm(true); }}>编辑</button><button className="btn btn-ghost btn-xs" onClick={() => setConfirmDialog({
                  open: true,
                  title: "删除模型",
                  message: `删除模型「${m.name}」？`,
                  danger: true,
                  onConfirm: async () => {
                    setConfirmDialog((d) => ({ ...d, open: false }));
                    try { await api.deleteModel(m.id); load(); } catch { /* ignore */ }
                  },
                })}><IconX size={12} /></button></div>
              </div>
            ))}
          </div>
        </div>
      )}

      <ProviderFormModal open={showProviderForm} editing={editingProvider} onClose={() => setShowProviderForm(false)} onSaved={load} />
      <ScanModelsModal open={!!scanningProvider} provider={scanningProvider} onClose={() => setScanningProvider(null)} onSaved={load} />
      <ModelFormModal open={showModelForm} editing={editingModel} onClose={() => setShowModelForm(false)} onSaved={async () => { load(); if (expandedId != null) { try { setExpandedModels(await api.listProviderModels(expandedId)); } catch {} } }} />
      <ConfirmDialog
        open={confirmDialog.open}
        title={confirmDialog.title}
        message={confirmDialog.message}
        danger={confirmDialog.danger}
        onCancel={() => setConfirmDialog((d) => ({ ...d, open: false }))}
        onConfirm={confirmDialog.onConfirm}
      />
    </div>
  );
}
