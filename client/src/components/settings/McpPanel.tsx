/** 设置中心：MCP 服务器（v2.2 对齐 zcode 3.18）。
 * 自动扫描本机 + 手动创建（stdio/sse）+ 启停。 */
import { useCallback, useEffect, useState } from "react";
import { api, type McpServerOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconPlus, IconX } from "../icons";
import { Sw } from "./shared";

export function McpPanel() {
  const [items, setItems] = useState<McpServerOut[]>([]);
  const [candidates, setCandidates] = useState<Array<{ name: string; transport: string; command: string | null; args?: string[]; env?: Record<string, string> | null; url?: string | null }>>([]);
  const [scanning, setScanning] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name: "", transport: "stdio", command: "", url: "" });
  const load = useCallback(async () => { try { setItems(await api.listMcpServers()); } catch {} }, []);
  useEffect(() => { load(); }, [load]);
  const handleScan = async () => {
    setScanning(true);
    try {
      // v6.5: 同时拉取最新已存在列表，避免“刚删除的 MCP”因本地状态未刷新被错误过滤；
      // 并按 name 去重（多个客户端配置里常出现同名 server）。
      const [servers, result] = await Promise.all([api.listMcpServers(), api.scanMcpServers()]);
      setItems(servers);
      const existing = new Set(servers.map((m) => m.name));
      const seen = new Set<string>();
      const next: typeof candidates = [];
      for (const c of result) {
        if (existing.has(c.name) || seen.has(c.name)) continue;
        seen.add(c.name);
        next.push(c);
      }
      setCandidates(next);
    } catch (e) {
      useChatStore.setState({ error: "扫描失败: " + String(e) });
    } finally {
      setScanning(false);
    }
  };
  const handleImport = async (c: { name: string; transport: string; command: string | null; args?: string[]; env?: Record<string, string> | null; url?: string | null }) => {
    setImporting(c.name);
    try {
      await api.createMcpServer({ name: c.name, transport: c.transport, command: c.command || undefined, args: c.args, env: c.env || undefined, url: c.url || undefined, is_active: false });
      setCandidates((prev) => prev.filter((x) => x.name !== c.name));
      load();
    } catch (e) {
      useChatStore.setState({ error: `导入 ${c.name} 失败: ${String(e)}` });
    } finally {
      setImporting(null);
    }
  };
  const handleCreate = async () => {
    if (!form.name.trim()) return;
    try {
      await api.createMcpServer({ name: form.name.trim(), transport: form.transport, command: form.command || undefined, url: form.url || undefined, is_active: true });
      setShowCreate(false);
      setForm({ name: "", transport: "stdio", command: "", url: "" });
      load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
  };
  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-ghost btn-sm" onClick={handleScan} disabled={scanning}><IconRefresh size={13} /> {scanning ? "扫描中…" : "自动扫描本机"}</button>
        <button className="btn btn-primary btn-sm" onClick={() => setShowCreate(!showCreate)}><IconPlus size={13} /> 手动创建</button>
      </div>
      {showCreate && (
        <div className="settings-create-form">
          <input className="ui-input" placeholder="名称" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} />
          <select className="ui-select" value={form.transport} onChange={(e) => setForm((p) => ({ ...p, transport: e.target.value }))}><option value="stdio">stdio</option><option value="sse">sse</option></select>
          {form.transport === "stdio"
            ? <input className="ui-input" placeholder="命令" value={form.command} onChange={(e) => setForm((p) => ({ ...p, command: e.target.value }))} />
            : <input className="ui-input" placeholder="URL" value={form.url} onChange={(e) => setForm((p) => ({ ...p, url: e.target.value }))} />}
          <div className="settings-create-actions">
            <button className="btn btn-ghost btn-sm" onClick={() => setShowCreate(false)}>取消</button>
            <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={!form.name.trim()}>创建</button>
          </div>
        </div>
      )}
      <div className="settings-resource-list">
        {items.map((m) => (
          <div key={m.id} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">{m.display_name || m.name}</div>
              <div className="settings-resource-desc"><span className="settings-resource-tag">{m.transport}</span><span>{m.transport === "stdio" ? m.command || "stdio" : m.url || "sse"}</span></div>
            </div>
            <div className="settings-resource-actions">
              <Sw checked={m.is_active} onChange={async (v) => { try { await api.updateMcpServer(m.id, { is_active: v }); load(); } catch {} }} />
              <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除？")) { try { await api.deleteMcpServer(m.id); load(); } catch {} } }}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && !showCreate && <div className="navpage-empty">暂无 MCP 服务器</div>}
      </div>
      {candidates.length > 0 && (
        <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-2)", marginBottom: 8 }}>扫描候选</div>
          {candidates.map((c, i) => (
            <div key={`${c.name}-${i}`} className="settings-resource-item" style={{ marginBottom: 4 }}>
              <div className="settings-resource-info">
                <div className="settings-resource-name">{c.name}</div>
                <div className="settings-resource-desc"><span className="settings-resource-tag">{c.transport}</span></div>
              </div>
              <button className="btn btn-primary btn-xs" disabled={importing !== null} onClick={() => handleImport(c)}>{importing === c.name ? "导入中…" : "导入"}</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
