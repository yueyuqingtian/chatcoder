/** 设置中心：MCP 服务器（v2.2 对齐 zcode 3.18）。
 * 自动扫描本机 + 手动创建（stdio/sse）+ 启停。 */
import { useCallback, useEffect, useState } from "react";
import { api, type McpServerOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconPlus, IconX } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { Modal } from "../Modal";
import { FormDialog } from "../ui/FormDialog";
import { Sw } from "./shared";

export function McpPanel() {
  const [items, setItems] = useState<McpServerOut[]>([]);
  const [candidates, setCandidates] = useState<Array<{ name: string; transport: string; command: string | null; args?: string[]; env?: Record<string, string> | null; url?: string | null; source_path?: string }>>([]);
  const [scanning, setScanning] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState<McpServerOut | null>(null);
  const [form, setForm] = useState({ name: "", transport: "stdio", command: "", url: "" });
  // plan-230-1144 M1.3: 工具清单展开与手动刷新（健康状态可见）
  const [expandedTools, setExpandedTools] = useState<number | null>(null);
  const [refreshingId, setRefreshingId] = useState<number | null>(null);
  // plan-234-1171 R1: 握手需项目工作区根（codegraph 依赖 rootUri，args 含 ${workspaceFolder}）
  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const currentProjectPath = projects.find((p) => p.id === currentProjectId)?.path;
  const handleRefreshTools = async (m: McpServerOut) => {
    setRefreshingId(m.id);
    try {
      const r = await api.refreshMcpTools(m.id, currentProjectPath);
      if (r.server) setItems((prev) => prev.map((x) => (x.id === m.id ? r.server : x)));
      setExpandedTools(m.id);
    } catch (e) {
      useChatStore.setState({ error: `刷新「${m.display_name || m.name}」工具失败: ${String(e)}` });
    } finally {
      setRefreshingId(null);
    }
  };
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
  const handleImport = async (c: { name: string; transport: string; command: string | null; args?: string[]; env?: Record<string, string> | null; url?: string | null; source_path?: string }) => {
    setImporting(c.name);
    try {
      await api.createMcpServer({ name: c.name, transport: c.transport, command: c.command || undefined, args: c.args, env: c.env || undefined, url: c.url || undefined, is_active: false, path: c.source_path || undefined });
      setCandidates((prev) => prev.filter((x) => x.name !== c.name));
      load();
    } catch (e) {
      useChatStore.setState({ error: `导入 ${c.name} 失败: ${String(e)}` });
    } finally {
      setImporting(null);
    }
  };
  const [showJsonModal, setShowJsonModal] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);

  const handleJsonImport = async () => {
    if (!jsonText.trim()) return;
    setJsonError(null);
    try {
      const parsed = JSON.parse(jsonText);
      const mcpServers = parsed.mcpServers || parsed;
      if (typeof mcpServers !== "object" || mcpServers === null) {
        throw new Error("无效的 MCP 配置，必须包含 mcpServers 键或对象定义");
      }
      let count = 0;
      for (const [name, config] of Object.entries(mcpServers)) {
        if (!config || typeof config !== "object") continue;
        const c = config as Record<string, unknown>;
        const command = typeof c.command === "string" ? c.command : undefined;
        const args = Array.isArray(c.args) ? c.args.map(String) : undefined;
        const env = typeof c.env === "object" && c.env !== null ? (c.env as Record<string, string>) : undefined;
        const url = typeof c.url === "string" ? c.url : undefined;
        const transport = url ? "sse" : "stdio";
        await api.createMcpServer({
          name,
          transport,
          command,
          args,
          env,
          url,
          is_active: true,
        });
        count++;
      }
      setShowJsonModal(false);
      setJsonText("");
      load();
    } catch (e: any) {
      setJsonError(e.message || String(e));
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
        <button className="btn btn-ghost btn-sm" onClick={() => setShowJsonModal(true)}>导入 JSON 配置</button>
        <button className="btn btn-primary btn-sm" onClick={() => setShowCreate(true)}><IconPlus size={13} /> 手动创建</button>
      </div>
      <FormDialog
        open={showCreate}
        onClose={() => { setShowCreate(false); setForm({ name: "", transport: "stdio", command: "", url: "" }); }}
        title="手动创建 MCP 服务器"
        onSubmit={() => void handleCreate()}
        submitDisabled={!form.name.trim()}
      >
        <input className="ui-input" placeholder="名称" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} />
        <select className="ui-select" value={form.transport} onChange={(e) => setForm((p) => ({ ...p, transport: e.target.value }))}><option value="stdio">stdio</option><option value="sse">sse</option></select>
        {form.transport === "stdio"
          ? <input className="ui-input" placeholder="命令" value={form.command} onChange={(e) => setForm((p) => ({ ...p, command: e.target.value }))} />
          : <input className="ui-input" placeholder="URL" value={form.url} onChange={(e) => setForm((p) => ({ ...p, url: e.target.value }))} />}
      </FormDialog>
      <div className="settings-resource-list">
        {items.map((m) => {
          const toolCount = Array.isArray(m.tools) ? m.tools.length : 0;
          return (
            <div key={m.id} className="settings-resource-item mcp-item">
              <div className="settings-resource-info">
                <div className="settings-resource-name">
                  {m.display_name || m.name}
                  {/* plan-230-1144 M1.3: 健康状态徽标——有缓存工具清单=已验证，否则=未验证 */}
                  <span className={"mcp-health" + (toolCount > 0 ? " ok" : " warn")}
                    title={toolCount > 0 ? `已缓存 ${toolCount} 个工具` : "尚未拉取到工具清单，点击「刷新工具」验证连接"}>
                    {toolCount > 0 ? `${toolCount} 个工具` : "未验证"}
                  </span>
                  <button className="btn btn-ghost btn-xs mcp-tools-toggle"
                    disabled={toolCount === 0}
                    onClick={() => setExpandedTools(expandedTools === m.id ? null : m.id)}>
                    {expandedTools === m.id ? "收起" : "工具清单"}
                  </button>
                </div>
                <div className="settings-resource-desc">
                  <span className="settings-resource-tag">{m.transport}</span>
                  <span>{m.transport === "stdio" ? m.command || "stdio" : m.url || "sse"}</span>
                  <span className="settings-resource-tag" title="引擎按风险等级注入：只读类工具四种模式全可见；写类工具仅完全访问/默认模式注入，只读/计划模式仅可见只读子集">
                    按风险分级注入
                  </span>
                </div>
                {expandedTools === m.id && toolCount > 0 && (
                  <div className="mcp-tools-list">
                    {(m.tools as Array<Record<string, unknown>>).map((t, i) => (
                      <div key={String(t.name || i)} className="mcp-tool-item">
                        <code>{String(t.name || "?")}</code>
                        <span>{String(t.description || "").slice(0, 120)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div className="settings-resource-actions">
                <Sw checked={m.is_active} onChange={async (v) => { try { await api.updateMcpServer(m.id, { is_active: v }, currentProjectPath); load(); } catch {} }} />
                <button className="btn btn-ghost btn-xs" disabled={refreshingId === m.id}
                  title="重新握手并拉取工具清单"
                  onClick={() => void handleRefreshTools(m)}>
                  <IconRefresh size={12} /> {refreshingId === m.id ? "握手中…" : "刷新工具"}
                </button>
                <button className="btn btn-ghost btn-xs" onClick={() => setConfirmTarget(m)}><IconX size={12} /></button>
              </div>
            </div>
          );
        })}
        {items.length === 0 && <div className="navpage-empty">暂无 MCP 服务器</div>}
      </div>
      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除 MCP 服务器"
        message={`删除「${confirmTarget?.display_name || confirmTarget?.name || ""}」？`}
        danger
        onCancel={() => setConfirmTarget(null)}
        onConfirm={async () => {
          const it = confirmTarget;
          setConfirmTarget(null);
          if (!it) return;
          try { await api.deleteMcpServer(it.id); load(); } catch { /* ignore */ }
        }}
      />
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

      <Modal
        open={showJsonModal}
        onClose={() => { setShowJsonModal(false); setJsonError(null); }}
        title="导入 MCP JSON 配置"
        subtitle="支持粘贴标准 Claude / Cursor 的 mcpServers JSON 格式"
        width={640}
        actions={
          <>
            <button className="btn btn-ghost" onClick={() => setShowJsonModal(false)}>取消</button>
            <button className="btn btn-primary" onClick={handleJsonImport} disabled={!jsonText.trim()}>确认导入</button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {jsonError && (
            <div style={{ color: "var(--error)", fontSize: 12, padding: "6px 10px", background: "color-mix(in srgb, var(--error) 10%, transparent)", borderRadius: 6 }}>
              {jsonError}
            </div>
          )}
          <textarea
            className="ui-textarea"
            style={{ minHeight: 200, fontFamily: "var(--font-mono)", fontSize: 12 }}
            placeholder={`{\n  "mcpServers": {\n    "memory": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-memory"]\n    }\n  }\n}`}
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
          />
        </div>
      </Modal>
    </div>
  );
}
