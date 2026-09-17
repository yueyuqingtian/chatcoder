/** 设置中心：模型设置（plan-248-1258 M2.5 重写，参照 zcode 双栏布局）。
 *
 * 布局：左侧供应商列表（内置/自定义分组 + 启用状态点 + 凭据数），
 *       右侧详情面板：名称/启用状态、Base URL、API 格式、凭据管理（多 Key/多账号）、
 *       代理配置（跟随全局/自定义/直连）、模型列表。
 *
 * 相对旧版（手风琴列表）的改进：
 * - 一屏内完成「选供应商 → 改配置 → 管凭据/模型」，不再层层展开；
 * - 多凭据（多 API Key / 多登录账号）自动轮询，一个失败自动切下一个；
 * - workbuddy 账号可查看积分余额、刷新、一键「Buddy 加油站」签到；
 * - 每供应商可选独立代理（自定义地址）或跟随全局 / 直连。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api, type ModelOut, type ProviderCredentialOut, type ProviderOut,
} from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconPlus, IconX, IconCpu } from "../icons";
import { Modal } from "../Modal";
import { ConfirmDialog } from "../ConfirmDialog";
import { Sw } from "./shared";
import { Ta3QuotaSection } from "./Ta3QuotaSection";

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点
 * （返回会话后输入框无法聚焦），统一改用全局 Toast。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

const REASONING_OPTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
const PROVIDER_OPTS = ["openai", "anthropic", "commandcode", "openai_compatible", "azure_openai", "google", "deepseek", "qwen", "zhipu", "moonshot", "yi", "baichuan", "minimax", "custom"];

/** 账号登录类供应商（无 Base URL/Key，走 OAuth） */
const OAUTH_FORMATS = new Set(["ta3", "workbuddy", "trae"]);
const OAUTH_DEFAULT_BASE: Record<string, string> = {
  ta3: "https://lc.yinhaiyun.com/newcoder",
  workbuddy: "https://copilot.tencent.com",
  trae: "https://trae-api-cn.mchost.guru",
  commandcode: "https://api.commandcode.ai",
};

const API_FORMAT_LABEL: Record<string, string> = {
  openai: "openai（兼容接口）",
  anthropic: "anthropic",
  commandcode: "commandcode",
  ta3: "ta3（Ta+3 牛码）",
  workbuddy: "workbuddy（腾讯 CodeBuddy）",
  trae: "trae（TRAE SOLO）",
};

/** 代理模式选项（plan-248-1258 M2.3） */
const PROXY_MODES: Array<{ value: string; label: string; desc: string }> = [
  { value: "inherit", label: "跟随全局", desc: "使用软件全局代理设置" },
  { value: "global", label: "强制全局", desc: "忽略供应商自定义，固定走全局代理" },
  { value: "custom", label: "自定义", desc: "为这个供应商单独指定代理地址" },
  { value: "direct", label: "直连", desc: "不使用任何代理（含忽略环境变量）" },
];

/** 状态徽标颜色类 */
function statusDotClass(p: ProviderOut) {
  if (!p.is_active) return "models-dot off";
  if (OAUTH_FORMATS.has(p.api_format) && p.auth_status !== "logged_in") return "models-dot warn";
  return "models-dot on";
}

// ─────────────────────────────────────────────
// 弹窗：供应商编辑
// ─────────────────────────────────────────────
function ProviderFormModal({ open, editing, onClose, onSaved }: { open: boolean; editing: ProviderOut | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ name: "", base_url: "", api_key: "", api_format: "openai", is_active: true, proxy_mode: "inherit", proxy_url: "" });
  const isOAuthFormat = OAUTH_FORMATS.has(form.api_format);
  useEffect(() => {
    if (editing) {
      setForm({
        name: editing.name, base_url: editing.base_url || "", api_key: "", api_format: editing.api_format || "openai",
        is_active: editing.is_active, proxy_mode: editing.proxy_mode || "inherit", proxy_url: editing.proxy_url || "",
      });
    } else {
      setForm({ name: "", base_url: "", api_key: "", api_format: "openai", is_active: true, proxy_mode: "inherit", proxy_url: "" });
    }
  }, [editing, open]);
  const handleSave = async () => {
    if (!form.name.trim()) return;
    if (!isOAuthFormat && !form.base_url.trim()) return;
    try {
      const finalBase = form.api_format === "commandcode" && !form.base_url.trim()
        ? OAUTH_DEFAULT_BASE.commandcode
        : (isOAuthFormat ? OAUTH_DEFAULT_BASE[form.api_format] : form.base_url.trim());
      const data: Record<string, unknown> = {
        name: form.name.trim(),
        base_url: finalBase,
        api_format: form.api_format,
        is_active: form.is_active,
        proxy_mode: form.proxy_mode,
        proxy_url: form.proxy_mode === "custom" ? (form.proxy_url.trim() || null) : null,
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
        <div className="settings-modal-form-row"><label>API 格式</label><select className="ui-select" value={form.api_format} onChange={(e) => setForm((p) => ({ ...p, api_format: e.target.value, base_url: e.target.value === "commandcode" && !p.base_url ? OAUTH_DEFAULT_BASE.commandcode : p.base_url }))}>{Object.entries(API_FORMAT_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></div>
        {!isOAuthFormat && <div className="settings-modal-form-row"><label>Base URL</label><input className="ui-input" placeholder={form.api_format === "commandcode" ? "https://api.commandcode.ai" : "https://.../v1"} value={form.base_url} onChange={(e) => setForm((p) => ({ ...p, base_url: e.target.value }))} /></div>}
        {!isOAuthFormat && <div className="settings-modal-form-row"><label>API Key {editing && "(留空不修改)"}</label><input className="ui-input" placeholder={form.api_format === "commandcode" ? "user_..." : "sk-..."} type="password" value={form.api_key} onChange={(e) => setForm((p) => ({ ...p, api_key: e.target.value }))} /></div>}
        {form.api_format === "commandcode" && <div className="models-hint">CommandCode 直连模式，API Key 为以 user_ 开头的密钥（可从 ~/.commandcode/auth.json 或 commandcode.ai/studio 获取）。</div>}
        {isOAuthFormat && <div className="models-hint">{form.api_format === "ta3"
          ? "ta3 类型使用账号登录获取模型，服务端地址已内置（lc.yinhaiyun.com/newcoder）；保存后在详情面板点击「登录账号」。"
          : form.api_format === "workbuddy"
            ? "workbuddy 类型使用账号登录获取模型，服务端地址已内置（copilot.tencent.com）；保存后在详情面板点击「登录账号」，支持登录多个账号。"
            : "trae 类型使用账号登录获取模型，服务端地址已内置（trae-api-cn.mchost.guru）；保存后在详情面板点击「登录账号」。"}</div>}
        <div className="settings-modal-form-row"><label>启用状态</label><Sw checked={form.is_active} onChange={(v) => setForm((p) => ({ ...p, is_active: v }))} /></div>
        <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button><button className="btn btn-primary btn-sm" onClick={handleSave} disabled={!form.name.trim() || (!isOAuthFormat && !form.base_url.trim())}>{editing ? "保存" : "创建"}</button></div>
      </div>
    </Modal>
  );
}

// ─────────────────────────────────────────────
// 弹窗：扫描模型
// ─────────────────────────────────────────────
interface ScanItem { name: string; enabled: boolean; context_window: string; is_multimodal: boolean; existing: boolean }

const MULTIMODAL_HINT = /vision|vl-|4v|omni|image|audio|realtime/i;

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
            <div className="models-hint">共发现 {items.length} 个模型，勾选要启用的模型并配置上下文 / 多模态</div>
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

// ─────────────────────────────────────────────
// 弹窗：模型编辑
// ─────────────────────────────────────────────
function ModelFormModal({ open, editing, targetProvider, onClose, onSaved }: { open: boolean; editing: ModelOut | null; targetProvider?: ProviderOut | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ name: "", provider: "openai_compatible", base_url: "", api_key: "", context_window: "200000", reasoning_efforts: [] as string[], is_active: true, is_multimodal: false });
  useEffect(() => {
    if (editing) {
      setForm({ name: editing.name, provider: editing.provider || "openai_compatible", base_url: editing.base_url || "", api_key: "", context_window: String(editing.context_window || 200000), reasoning_efforts: editing.reasoning_efforts || [], is_active: editing.is_active, is_multimodal: editing.is_multimodal });
    } else if (targetProvider) {
      setForm({ name: "", provider: targetProvider.api_format || "openai_compatible", base_url: targetProvider.base_url || "", api_key: "", context_window: "200000", reasoning_efforts: ["low", "medium", "high"], is_active: true, is_multimodal: false });
    } else {
      setForm({ name: "", provider: "openai_compatible", base_url: "", api_key: "", context_window: "200000", reasoning_efforts: [], is_active: true, is_multimodal: false });
    }
  }, [editing, targetProvider, open]);
  const handleSave = async () => {
    if (!form.name.trim()) return;
    try {
      const data: Record<string, unknown> = {
        name: form.name.trim(),
        provider: targetProvider ? (targetProvider.api_format || "openai_compatible") : form.provider,
        base_url: targetProvider ? targetProvider.base_url : (form.base_url || undefined),
        context_window: Number(form.context_window) || undefined,
        is_active: form.is_active,
        is_multimodal: form.is_multimodal,
      };
      if (targetProvider) {
        data.provider_id = targetProvider.id;
        data.api_format = targetProvider.api_format;
      }
      if (form.api_key) data.api_key = form.api_key;
      if (form.reasoning_efforts.length > 0) data.reasoning_efforts = form.reasoning_efforts;
      if (editing) await api.updateModel(editing.id, data);
      else await api.createModel(data as never);
      onSaved();
      onClose();
    } catch (e) { notify(String(e)); }
  };
  const underProvider = !!editing?.provider_id || !!targetProvider;
  const currentProviderName = editing?.provider_name || targetProvider?.name || (editing?.provider_id ? `#${editing?.provider_id}` : "");
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑模型" : (targetProvider ? `添加模型（${targetProvider.name}）` : "新建模型")} width={520} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        {underProvider && <div className="settings-modal-form-row"><label>所属供应商</label><span style={{ fontSize: 12, color: "var(--text-2)" }}>{currentProviderName}（继承供应商连接与认证）</span></div>}
        <div className="settings-modal-form-row"><label>模型名称 / ID</label><input className="ui-input" placeholder={targetProvider?.api_format === "commandcode" ? "如 zai-org/GLM-5.1 或 deepseek/deepseek-v4-pro" : "如 glm-5.2"} value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} /></div>
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

// ─────────────────────────────────────────────
// 弹窗：凭据（API Key / 账号）编辑
// ─────────────────────────────────────────────
function CredentialFormModal({ open, providerId, editing, onClose, onSaved }: {
  open: boolean; providerId: number | null; editing: ProviderCredentialOut | null;
  onClose: () => void; onSaved: () => void;
}) {
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [priority, setPriority] = useState("0");
  const [isActive, setIsActive] = useState(true);
  useEffect(() => {
    if (editing) {
      setLabel(editing.label || "");
      setApiKey("");
      setPriority(String(editing.priority ?? 0));
      setIsActive(editing.is_active);
    } else {
      setLabel(""); setApiKey(""); setPriority("0"); setIsActive(true);
    }
  }, [editing, open]);
  const handleSave = async () => {
    if (providerId == null) return;
    try {
      const data: Record<string, unknown> = {
        label: label.trim() || null,
        priority: Number(priority) || 0,
        is_active: isActive,
      };
      if (apiKey) data.api_key = apiKey;
      if (editing) await api.updateProviderCredential(editing.id, data);
      else await api.createProviderCredential(providerId, data as never);
      onSaved();
      onClose();
    } catch (e) { notify(String(e)); }
  };
  return (
    <Modal open={open} onClose={onClose} title={editing ? "编辑凭据" : "添加 API Key"} width={460} height="auto">
      <div className="settings-modal-form" style={{ padding: 18 }}>
        <div className="settings-modal-form-row"><label>备注名</label><input className="ui-input" placeholder="如 主 Key / 备用 Key" value={label} onChange={(e) => setLabel(e.target.value)} /></div>
        <div className="settings-modal-form-row"><label>API Key {editing && "(留空不修改)"}</label><input className="ui-input" type="password" placeholder="sk-..." value={apiKey} onChange={(e) => setApiKey(e.target.value)} /></div>
        <div className="settings-modal-form-row"><label>优先级（小的先用）</label><input className="ui-input" value={priority} onChange={(e) => setPriority(e.target.value)} /></div>
        <div className="settings-modal-form-row"><label>启用</label><Sw checked={isActive} onChange={setIsActive} /></div>
        <div className="models-hint">同一供应商可配置多个 Key：某个 Key 报错（401/429/5xx）时会自动切换到下一个可用 Key，失败的 Key 进入冷却后自动恢复。</div>
        <div className="settings-create-actions"><button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button><button className="btn btn-primary btn-sm" onClick={handleSave}>{editing ? "保存" : "添加"}</button></div>
      </div>
    </Modal>
  );
}

// ─────────────────────────────────────────────
// 主面板
// ─────────────────────────────────────────────
export function ModelsPanel() {
  const [providers, setProviders] = useState<ProviderOut[]>([]);
  const [independentModels, setIndependentModels] = useState<ModelOut[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [providerModels, setProviderModels] = useState<ModelOut[]>([]);
  const [credentials, setCredentials] = useState<ProviderCredentialOut[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailProviderId, setDetailProviderId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const detailRequestId = useRef(0);
  const detailCache = useRef(new Map<number, { models: ModelOut[]; credentials: ProviderCredentialOut[] }>());

  const [showProviderForm, setShowProviderForm] = useState(false);
  const [editingProvider, setEditingProvider] = useState<ProviderOut | null>(null);
  const [scanningProvider, setScanningProvider] = useState<ProviderOut | null>(null);
  const [editingModel, setEditingModel] = useState<ModelOut | null>(null);
  const [targetProviderForModel, setTargetProviderForModel] = useState<ProviderOut | null>(null);
  const [showModelForm, setShowModelForm] = useState(false);
  const [showCredForm, setShowCredForm] = useState(false);
  const [editingCred, setEditingCred] = useState<ProviderCredentialOut | null>(null);

  // 登录/同步状态（ta3/workbuddy/trae 共用：providerId → phase）
  const [authStatus, setAuthStatus] = useState<Record<number, { phase: "idle" | "pending" | "done" | "failed"; label?: string; error?: string }>>({});
  const [syncBusy, setSyncBusy] = useState<number | null>(null);
  const [proxyTest, setProxyTest] = useState<string | null>(null);

  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean; title: string; message: string; danger?: boolean; onConfirm: () => Promise<void> | void;
  }>({ open: false, title: "", message: "", onConfirm: () => {} });

  const closeConfirm = () => setConfirmDialog((d) => ({ ...d, open: false }));

  const load = useCallback(async () => {
    try {
      const list = await api.listProviders();
      setProviders(list);
      setSelectedId((cur) => (cur != null && list.some((p) => p.id === cur) ? cur : (list[0]?.id ?? null)));
    } catch { /* ignore */ }
    try { setIndependentModels((await api.listModels()).filter((m) => !m.provider_id)); } catch { /* ignore */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const selected = providers.find((p) => p.id === selectedId) || null;
  const isOAuth = !!selected && OAUTH_FORMATS.has(selected.api_format);

  // workbuddy 供应商级积分合计（多账号时汇总展示在详情头部；全部未知时显示 --）
  const creditValues = credentials.map((c) => c.credits).filter((v): v is number => v != null);
  const totalCredits = creditValues.length > 0 ? creditValues.reduce((a, b) => a + Number(b), 0) : null;

  // 选中供应商 → 拉取其模型与凭据。切换时保留旧详情直到新详情准备好，
  // 避免双栏右侧空白/高度跳变；requestId 防止慢响应回写到错误供应商。
  const loadDetail = useCallback(async (p: ProviderOut | null, force = false) => {
    const requestId = ++detailRequestId.current;
    if (!p) {
      setDetailLoading(false);
      setProviderModels([]);
      setCredentials([]);
      return;
    }
    const cached = detailCache.current.get(p.id);
    if (cached && !force) {
      setProviderModels(cached.models);
      setCredentials(cached.credentials);
      setDetailProviderId(p.id);
      setDetailLoading(false);
      return;
    }
    setDetailLoading(true);
    setDetailProviderId(p.id);
    try {
      const [modelsResult, credentialsResult] = await Promise.allSettled([
        api.listProviderModels(p.id),
        api.listProviderCredentials(p.id),
      ]);
      if (requestId !== detailRequestId.current) return;
      const models = modelsResult.status === "fulfilled" ? modelsResult.value : [];
      const nextCredentials = credentialsResult.status === "fulfilled" ? credentialsResult.value : [];
      detailCache.current.set(p.id, { models, credentials: nextCredentials });
      setProviderModels(models);
      setCredentials(nextCredentials);
    } finally {
      if (requestId === detailRequestId.current) setDetailLoading(false);
    }
  }, []);
  useEffect(() => { void loadDetail(selected); }, [selectedId, loadDetail]);

  const patchProvider = async (id: number, data: Record<string, unknown>) => {
    try {
      const updated = await api.updateProvider(id, data);
      setProviders((prev) => prev.map((p) => (p.id === id ? { ...p, ...updated } : p)));
      if (selectedId === id) await loadDetail(updated, true);
    } catch (e) { notify(String(e)); }
  };

  // ── workbuddy 积分：余额查询结果直接回写 UI，避免写引擎异步落库造成刷新后仍为空 ──
  const loadCredits = useCallback(async (p: ProviderOut, refresh = false) => {
    if (p.api_format !== "workbuddy") return;
    const providerId = p.id;
    try {
      const response = await api.workbuddyCredits(providerId, { refresh });
      if (selectedId !== providerId) return;
      setCredentials((prev) => {
        const byId = new Map(prev.map((c) => [c.id, c]));
        for (const item of response.credentials) {
          if (item.credential_id == null) continue;
          const old = byId.get(item.credential_id);
          if (old) byId.set(item.credential_id, { ...old, credits: item.credits });
        }
        return [...byId.values()];
      });
      // 旧版本 provider 级登录态没有 credential_id：将余额显示为一个明确的账号行。
      const legacy = response.credentials.find((item) => item.credential_id == null && item.logged_in);
      if (legacy && selectedId === providerId) {
        setCredentials((prev) => {
          const syntheticId = -providerId;
          const synthetic = {
            id: syntheticId,
            provider_id: providerId,
            label: legacy.label || "WorkBuddy 账号",
            has_api_key: false,
            api_key_preview: null,
            token_ref: "legacy-workbuddy-auth",
            priority: 0,
            is_active: true,
            status: "ok",
            last_error: null,
            cooldown_until: null,
            last_ok_at: null,
            credits: legacy.credits,
            extra: legacy.account,
          } as ProviderCredentialOut;
          const exists = prev.some((c) => c.id === syntheticId);
          return exists ? prev.map((c) => c.id === syntheticId ? { ...c, ...synthetic } : c) : [...prev, synthetic];
        });
      }
    } catch (e) { notify(String(e)); }
  }, [selectedId]);
  useEffect(() => {
    if (selected?.api_format === "workbuddy" && !detailLoading) void loadCredits(selected, false);
  }, [selectedId, selected?.api_format, detailLoading, loadCredits]);

  const handleCheckin = async (p: ProviderOut) => {
    setBusy(true);
    try {
      const r = await api.workbuddyCheckin(p.id);
      const okCount = r.results.filter((x) => x.status === "claimed").length;
      const already = r.results.filter((x) => x.status === "already_claimed").length;
      const failed = r.results.filter((x) => !["claimed", "already_claimed"].includes(x.status));
      notify(`签到完成：成功 ${okCount} 个${already ? `，今日已签 ${already} 个` : ""}${failed.length ? `，失败 ${failed.length} 个` : ""}`);
      await loadCredits(p, true);
      await loadDetail(p, true);
    } catch (e) { notify(String(e)); }
    finally { setBusy(false); }
  };

  const handleProxyTest = async (p: ProviderOut) => {
    setProxyTest("测试中…");
    try {
      const r = await api.testProviderProxy(p.id);
      setProxyTest(r.ok ? `连通（${r.latency_ms}ms）${r.proxy ? ` · 代理 ${r.proxy}` : " · 直连"}` : `失败：${r.error || "未知错误"}`);
    } catch (e) { setProxyTest(String(e)); }
  };

  // ── OAuth 登录（ta3 / workbuddy / trae）──
  const handleLogin = async (p: ProviderOut) => {
    setAuthStatus((s) => ({ ...s, [p.id]: { phase: "pending" } }));
    const fmt = p.api_format;
    try {
      const start = fmt === "workbuddy" ? await api.workbuddyLoginStart(p.id)
        : fmt === "ta3" ? await api.ta3LoginStart(p.id)
          : await api.traeLoginStart(p.id);
      const url = (start as { auth_url?: string; authorize_url?: string }).auth_url || (start as { authorize_url?: string }).authorize_url;
      const statusOf = async () => fmt === "workbuddy" ? api.workbuddyLoginStatus(p.id)
        : fmt === "ta3" ? api.ta3LoginStatus(p.id) : api.traeLoginStatus(p.id);
      if (start.status === "logged_in") {
        setAuthStatus((s) => ({ ...s, [p.id]: { phase: "done", label: (start.account as Record<string, string>)?.label || "已登录" } }));
        await handleSync(p, false);
        return;
      }
      if (!url) throw new Error(start.status === "failed" ? "登录失败" : "未获取到授权地址");
      const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
      if (w.chatcoderAPI?.openExternal) await w.chatcoderAPI.openExternal(url);
      else window.open(url, "_blank");
      const deadline = Date.now() + ((start.expires_in ?? 300) + 10) * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000));
        const st = await statusOf();
        if (st.status === "logged_in") {
          setAuthStatus((s) => ({ ...s, [p.id]: { phase: "done", label: (st.account as Record<string, string>)?.label || "已登录" } }));
          await handleSync(p, false);
          return;
        }
        if (st.status === "failed") {
          setAuthStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: st.error || "登录失败" } }));
          return;
        }
      }
      setAuthStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: "登录超时，请重试" } }));
    } catch (e) {
      setAuthStatus((s) => ({ ...s, [p.id]: { phase: "failed", error: String(e) } }));
    }
  };

  const handleSync = async (p: ProviderOut, showMsg = true) => {
    setSyncBusy(p.id);
    try {
      const r = p.api_format === "workbuddy" ? await api.workbuddySync(p.id)
        : p.api_format === "ta3" ? await api.ta3Sync(p.id) : await api.traeSync(p.id);
      await load();
      await loadDetail(p, true);
      if (showMsg) notify(`同步完成：${r.synced} 个模型`);
    } catch (e) { notify(String(e)); }
    finally { setSyncBusy(null); }
  };

  const handleLogout = (p: ProviderOut) => {
    setConfirmDialog({
      open: true,
      title: "退出账号",
      message: `退出账号「${p.account_label || p.name}」？其下模型将不可用。`,
      danger: true,
      onConfirm: async () => {
        closeConfirm();
        try {
          if (p.api_format === "workbuddy") await api.workbuddyLogout(p.id);
          else if (p.api_format === "ta3") await api.ta3Logout(p.id);
          else await api.traeLogout(p.id);
          await load();
          await loadDetail(p, true);
        } catch (e) { notify(String(e)); }
      },
    });
  };

  return (
    <div className="models-page">
      <div className="models-head">
        <div>
          <div className="models-head-title">模型设置</div>
          <div className="models-head-sub">管理自定义模型供应商，配置后可随时切换使用。</div>
        </div>
        <div className="models-head-actions">
          <button className="btn btn-ghost btn-sm" title="刷新" onClick={load}><IconRefresh size={13} /></button>
          <button className="btn btn-ghost btn-sm" onClick={() => { setEditingModel(null); setTargetProviderForModel(null); setShowModelForm(true); }}><IconPlus size={13} /> 独立模型</button>
          <button className="btn btn-primary btn-sm" onClick={() => { setEditingProvider(null); setShowProviderForm(true); }}><IconPlus size={13} /> 添加供应商</button>
        </div>
      </div>

      <div className="models-body">
        {/* 左：供应商列表 */}
        <aside className="models-side">
          <div className="models-side-group">供应商</div>
          {providers.map((p) => (
            <button
              key={p.id}
              className={"models-side-item" + (p.id === selectedId ? " active" : "")}
              onClick={() => setSelectedId(p.id)}
            >
              <span className={statusDotClass(p)} />
              <span className="models-side-name" title={p.name}>{p.name}</span>
              {(p.credential_count ?? 0) > 1 && <span className="models-side-badge">{p.credential_count} Key</span>}
            </button>
          ))}
          {providers.length === 0 && <div className="models-side-empty">暂无供应商</div>}
          {independentModels.length > 0 && (
            <>
              <div className="models-side-group">独立模型</div>
              {independentModels.map((m) => (
                <div key={m.id} className="models-side-item static">
                  <span className={"models-dot " + (m.is_active ? "on" : "off")} />
                  <span className="models-side-name" title={m.name}>{m.name}</span>
                </div>
              ))}
            </>
          )}
        </aside>

        {/* 右：详情 */}
        <section className="models-detail">
          {!selected ? (
            <div className="models-detail-empty">
              <IconCpu size={28} />
              <div>选择左侧供应商以查看配置，或点击右上「添加供应商」。</div>
            </div>
          ) : (
            <>
              {detailLoading && detailProviderId === selected.id && (
                <div className="models-detail-loading" aria-live="polite">正在加载供应商配置…</div>
              )}
              <div className="models-detail-head">
                <input
                  className="models-name-input"
                  value={selected.name}
                  onChange={(e) => patchProviderLocal(setProviders, selected.id, e.target.value, "name")}
                  onBlur={(e) => patchProvider(selected.id, { name: e.target.value.trim() || selected.name })}
                />
                <span className={"models-badge " + (selected.is_active ? "ok" : "muted")}>{selected.is_active ? "已启用" : "已禁用"}</span>
                <button className="models-link" onClick={() => patchProvider(selected.id, { is_active: !selected.is_active })}>
                  {selected.is_active ? "禁用" : "启用"}
                </button>
                <div style={{ marginLeft: "auto" }}>
                  <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDialog({
                    open: true,
                    title: "删除供应商",
                    message: `删除供应商「${selected.name}」及其下所有模型与凭据？`,
                    danger: true,
                    onConfirm: async () => {
                      closeConfirm();
                      try { await api.deleteProvider(selected.id); await load(); } catch (e) { notify(String(e)); }
                    },
                  })}><IconX size={13} /></button>
                </div>
              </div>

              {/* 基本信息 */}
              <div className="models-field">
                <label>Base URL</label>
                {/* key=provider.id：defaultValue 不随 props 更新，切换供应商时必须重挂载，
                    否则残留上一个供应商的 URL（OAuth 类禁用态下尤其误导） */}
                <input
                  key={`base-${selected.id}`}
                  className="ui-input"
                  defaultValue={selected.base_url || ""}
                  disabled={OAUTH_FORMATS.has(selected.api_format)}
                  onBlur={(e) => { if (e.target.value.trim() !== (selected.base_url || "")) patchProvider(selected.id, { base_url: e.target.value.trim() }); }}
                />
              </div>
              <div className="models-field">
                <label>API 格式</label>
                <select
                  className="ui-select"
                  value={selected.api_format}
                  onChange={(e) => patchProvider(selected.id, { api_format: e.target.value })}
                >
                  {Object.entries(API_FORMAT_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </div>

              {/* 账号登录型供应商：登录 / 同步 / 退出 */}
              {isOAuth && (
                <div className="models-section">
                  <div className="models-section-title">账号</div>
                  <div className="models-inline-row">
                    <span className="settings-resource-tag">{selected.auth_status === "logged_in" ? `已登录${selected.account_label ? `：${selected.account_label}` : ""}` : "未登录"}</span>
                    <button className="btn btn-ghost btn-xs" onClick={() => handleLogin(selected)} disabled={authStatus[selected.id]?.phase === "pending"}>
                      {authStatus[selected.id]?.phase === "pending" ? "登录中…" : selected.auth_status === "logged_in" ? "重新登录" : "登录账号"}
                    </button>
                    {selected.auth_status === "logged_in" && (
                      <>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleSync(selected)} disabled={syncBusy === selected.id}>{syncBusy === selected.id ? "同步中…" : "同步模型"}</button>
                        <button className="btn btn-ghost btn-xs" onClick={() => handleLogout(selected)}>退出</button>
                      </>
                    )}
                    {authStatus[selected.id]?.phase === "failed" && <span className="models-err">{authStatus[selected.id]?.error}</span>}
                  </div>
                </div>
              )}

              {/* ta3 额度与用量（plan-270-1358：对齐 Ta+3 v0.4.6 额度查看） */}
              {selected.api_format === "ta3" && <Ta3QuotaSection provider={selected} />}

              {/* 凭据管理（多 Key / 多账号） */}
              <div className="models-section">
                <div className="models-section-title">
                  凭据（多 Key / 多账号）
                  <button className="btn btn-ghost btn-xs" onClick={() => { setEditingCred(null); setShowCredForm(true); }}><IconPlus size={12} /> 添加 Key</button>
                </div>
                <div className="models-cred-list">
                  {credentials.map((c) => (
                    <div key={c.id} className="models-cred-item">
                      <div className="models-cred-main">
                        <div className="models-cred-title">
                          {c.label || `凭据 #${c.id}`}
                          <span className={"models-badge sm " + credStatusClass(c.status)}>{credStatusLabel(c.status)}</span>
                        </div>
                        <div className="models-cred-desc">
                          {c.api_key_preview || (c.token_ref ? "账号登录" : "无 Key")}
                          {selected.api_format === "workbuddy" && c.credits != null && (
                            <span className="models-credits">积分 {formatCredits(c.credits)}</span>
                          )}
                          {c.last_error && <span className="models-err">{c.last_error}</span>}
                        </div>
                      </div>
                      <div className="models-cred-actions">
                        {selected.api_format === "workbuddy" && (
                          <button className="btn btn-ghost btn-xs" title="刷新积分余额" onClick={() => loadCredits(selected, true)}>
                            <IconRefresh size={12} />
                          </button>
                        )}
                        <Sw checked={c.is_active} onChange={async (v) => { try { await api.updateProviderCredential(c.id, { is_active: v }); await loadDetail(selected, true); } catch (e) { notify(String(e)); } }} />
                        {!OAUTH_FORMATS.has(selected.api_format) && (
                          <button className="btn btn-ghost btn-xs" onClick={() => { setEditingCred(c); setShowCredForm(true); }}>编辑</button>
                        )}
                        <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDialog({
                          open: true,
                          title: "删除凭据",
                          message: `删除凭据「${c.label || c.id}」？`,
                          danger: true,
                          onConfirm: async () => {
                            closeConfirm();
                            try { await api.deleteProviderCredential(c.id); await loadDetail(selected, true); } catch (e) { notify(String(e)); }
                          },
                        })}><IconX size={12} /></button>
                      </div>
                    </div>
                  ))}
                  {credentials.length === 0 && <div className="navpage-empty">暂无凭据，点击「添加 Key」新增（或使用账号登录）</div>}
                </div>
                {selected.api_format === "workbuddy" && (
                  <div className="models-inline-row" style={{ marginTop: 8 }}>
                    <button className="btn btn-primary btn-xs" disabled={busy} onClick={() => handleCheckin(selected)}>
                      {busy ? "签到中…" : "Buddy 加油站 · 签到领取积分"}
                    </button>
                    {/* 积分余额（多账号合计）+ 实时刷新 */}
                    <span className="models-credits-head" title="workbuddy 积分余额（多账号时为合计）">
                      积分 {totalCredits != null ? formatCredits(totalCredits) : "--"}
                      <button className="btn btn-ghost btn-xs" title="刷新积分余额" onClick={() => loadCredits(selected, true)}>
                        <IconRefresh size={12} />
                      </button>
                    </span>
                    <span className="models-hint" style={{ padding: 0 }}>打开软件后会自动后台签到，此处可手动补签。</span>
                  </div>
                )}
              </div>

              {/* 代理配置 */}
              <div className="models-section">
                <div className="models-section-title">代理</div>
                <div className="models-proxy-modes">
                  {PROXY_MODES.map((m) => (
                    <button
                      key={m.value}
                      className={"settings-pill" + ((selected.proxy_mode || "inherit") === m.value ? " active" : "")}
                      title={m.desc}
                      onClick={() => patchProvider(selected.id, { proxy_mode: m.value })}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
                {selected.proxy_mode === "custom" && (
                  <div className="models-field" style={{ marginTop: 8 }}>
                    <label>代理地址</label>
                    <input
                      key={`proxy-${selected.id}`}
                      className="ui-input"
                      placeholder="http://127.0.0.1:7897"
                      defaultValue={selected.proxy_url || ""}
                      onBlur={(e) => patchProvider(selected.id, { proxy_url: e.target.value.trim() || null })}
                    />
                  </div>
                )}
                <div className="models-inline-row" style={{ marginTop: 8 }}>
                  <button className="btn btn-ghost btn-xs" onClick={() => handleProxyTest(selected)}>测试该配置</button>
                  {proxyTest && <span className="models-hint" style={{ padding: 0 }}>{proxyTest}</span>}
                </div>
              </div>

              {/* 模型列表 */}
              <div className="models-section">
                <div className="models-section-title">
                  模型（{providerModels.length}）
                  <div style={{ display: "flex", gap: 6 }}>
                    {!isOAuth && <button className="btn btn-ghost btn-xs" onClick={() => setScanningProvider(selected)}>扫描模型</button>}
                    <button className="btn btn-ghost btn-xs" onClick={() => { setTargetProviderForModel(selected); setEditingModel(null); setShowModelForm(true); }}><IconPlus size={12} /> 添加模型</button>
                  </div>
                </div>
                <div className="models-model-list">
                  {providerModels.map((m) => (
                    <div key={m.id} className="models-model-item">
                      <div className="models-model-main">
                        <div className="models-model-name" title={m.name}>{m.name}</div>
                        <div className="models-model-desc">
                          {m.context_window ? <span>{m.context_window} tokens</span> : null}
                          {m.trae_max_context ? <span className="settings-resource-tag">max {m.trae_max_context}</span> : null}
                          {m.trae_consumption_rate ? <span className="settings-resource-tag">消耗×{m.trae_consumption_rate}</span> : null}
                          {m.api_format === "trae" && m.trae_available === false && <span className="settings-resource-tag">不可用</span>}
                          {m.is_multimodal && <span className="settings-resource-tag">多模态</span>}
                          {(m.reasoning_efforts?.length ?? 0) > 0 && <span className="settings-resource-tag">思考: {m.reasoning_efforts.join("/")}</span>}
                        </div>
                      </div>
                      <div className="models-model-actions">
                        <Sw checked={m.is_active} onChange={async (v) => { try { await api.updateModel(m.id, { is_active: v }); await loadDetail(selected, true); } catch (e) { notify(String(e)); } }} />
                        <button className="btn btn-ghost btn-xs" onClick={() => { setEditingModel(m); setTargetProviderForModel(selected); setShowModelForm(true); }}>编辑</button>
                        <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDialog({
                          open: true,
                          title: "删除模型",
                          message: `删除模型「${m.name}」？`,
                          danger: true,
                          onConfirm: async () => {
                            closeConfirm();
                            try { await api.deleteModel(m.id); await loadDetail(selected, true); } catch (e) { notify(String(e)); }
                          },
                        })}><IconX size={12} /></button>
                      </div>
                    </div>
                  ))}
                  {providerModels.length === 0 && (
                    <div className="navpage-empty">{isOAuth ? "暂无模型，请先登录账号并点击「同步模型」" : "暂无模型，点击「扫描模型」或「添加模型」获取"}</div>
                  )}
                </div>
              </div>
            </>
          )}
        </section>
      </div>

      <ProviderFormModal open={showProviderForm} editing={editingProvider} onClose={() => setShowProviderForm(false)} onSaved={load} />
      <ScanModelsModal open={!!scanningProvider} provider={scanningProvider} onClose={() => setScanningProvider(null)} onSaved={async () => { await load(); await loadDetail(selected, true); }} />
      <ModelFormModal open={showModelForm} editing={editingModel} targetProvider={targetProviderForModel} onClose={() => { setShowModelForm(false); setTargetProviderForModel(null); }} onSaved={async () => { await load(); await loadDetail(selected, true); }} />
      <CredentialFormModal open={showCredForm} providerId={selected?.id ?? null} editing={editingCred} onClose={() => { setShowCredForm(false); setEditingCred(null); }} onSaved={async () => { await loadDetail(selected, true); await load(); }} />
      <ConfirmDialog
        open={confirmDialog.open}
        title={confirmDialog.title}
        message={confirmDialog.message}
        danger={confirmDialog.danger}
        onCancel={closeConfirm}
        onConfirm={confirmDialog.onConfirm}
      />
    </div>
  );
}

/** 名称就地编辑：先改本地状态（避免受控 input 抖动），失焦时提交。 */
function patchProviderLocal(
  setProviders: React.Dispatch<React.SetStateAction<ProviderOut[]>>,
  id: number, value: string, key: keyof ProviderOut,
) {
  setProviders((prev) => prev.map((p) => (p.id === id ? { ...p, [key]: value } : p)));
}

function credStatusClass(status: string): string {
  if (status === "ok") return "ok";
  if (status === "cooldown") return "warn";
  if (status === "error") return "err";
  return "muted";
}

function credStatusLabel(status: string): string {
  if (status === "ok") return "可用";
  if (status === "cooldown") return "冷却中";
  if (status === "error") return "异常";
  return "未启用";
}

function formatCredits(v: number): string {
  return v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
