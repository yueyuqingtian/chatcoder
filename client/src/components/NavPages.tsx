/** 左侧导航页（v2）：定时任务 / 技能 / MCP 管理页。 */
import { useCallback, useEffect, useState } from "react";
import { api, type McpServerOut, type ScheduledTaskOut, type SkillOut } from "../api/client";
import { useChatStore } from "../store/chat";
import { IconPlus, IconRefresh, IconX } from "./icons";

function SwitchRow({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="sp-switch">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="sp-slider" />
    </label>
  );
}

export function ScheduledPage() {
  const [tasks, setTasks] = useState<ScheduledTaskOut[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  // v6: 用"日期时间 + 频率"替代 cron 表达式（普通用户无需了解 cron）
  const [form, setForm] = useState<{ name: string; runAt: string; freq: string; weekday: string; prompt: string }>(() => {
    const d = new Date(Date.now() + 3600000);
    const pad = (n: number) => String(n).padStart(2, "0");
    return { name: "", runAt: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`, freq: "daily", weekday: "1", prompt: "" };
  });
  const currentSessionId = useChatStore((s) => s.currentSessionId);

  const load = useCallback(async () => {
    try { setTasks(await api.listScheduledTasks()); } catch { /* ignore */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  // 友好时间 → cron 换算
  const toCron = (): string | null => {
    if (!form.runAt) return null;
    const d = new Date(form.runAt);
    if (isNaN(d.getTime())) return null;
    const min = d.getMinutes(), hour = d.getHours(), day = d.getDate(), month = d.getMonth() + 1;
    switch (form.freq) {
      case "once": return `${min} ${hour} ${day} ${month} *`;
      case "daily": return `${min} ${hour} * * *`;
      case "weekly": return `${min} ${hour} * * ${form.weekday}`;
      case "monthly": return `${min} ${hour} ${day} * *`;
      default: return `${min} ${hour} * * *`;
    }
  };

  const handleCreate = async () => {
    if (!currentSessionId || !form.name.trim() || !form.prompt.trim()) return;
    const cron = toCron();
    if (!cron) { alert("请选择有效的执行时间"); return; }
    try {
      await api.createScheduledTask({ session_id: currentSessionId, name: form.name.trim(), cron, prompt: form.prompt.trim() });
      setShowCreate(false);
      const d = new Date(Date.now() + 3600000);
      const pad = (n: number) => String(n).padStart(2, "0");
      setForm((p) => ({ ...p, name: "", prompt: "", runAt: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}` }));
      load();
    } catch (e) { alert(String(e)); }
  };

  return (
    <div className="navpage">
      <div className="navpage-head">
        <span className="navpage-title">定时任务</span>
        <button className="btn-ghost" onClick={() => setShowCreate((v) => !v)}><IconPlus size={13} /> 新建</button>
      </div>
      {showCreate && (
        <div className="navpage-form">
          <input className="sp-input" placeholder="任务名称" value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} />
          <label className="sp-label">执行时间</label>
          <input type="datetime-local" className="sp-input" value={form.runAt} onChange={(e) => setForm((p) => ({ ...p, runAt: e.target.value }))} />
          <label className="sp-label">重复频率</label>
          <div className="sp-freq-row">
            {[["once", "仅一次"], ["daily", "每天"], ["weekly", "每周"], ["monthly", "每月"]].map(([v, l]) => (
              <button key={v} className={"sp-pill" + (form.freq === v ? " active" : "")} onClick={() => setForm((p) => ({ ...p, freq: v }))}>{l}</button>
            ))}
          </div>
          {form.freq === "weekly" && (
            <select className="sp-input" value={form.weekday} onChange={(e) => setForm((p) => ({ ...p, weekday: e.target.value }))}>
              {[["1", "周一"], ["2", "周二"], ["3", "周三"], ["4", "周四"], ["5", "周五"], ["6", "周六"], ["0", "周日"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          )}
          <label className="sp-label">执行提示词</label>
          <textarea className="sp-textarea" placeholder="执行提示词…" rows={3} value={form.prompt} onChange={(e) => setForm((p) => ({ ...p, prompt: e.target.value }))} />
          <div className="navpage-form-actions">
            <button className="btn-ghost" onClick={() => setShowCreate(false)}>取消</button>
            <button className="btn-primary" onClick={handleCreate} disabled={!form.name.trim() || !form.prompt.trim()}>创建</button>
          </div>
        </div>
      )}
      <div className="navpage-list">
        {tasks.map((t) => (
          <div key={t.id} className="navpage-item">
            <div className="navpage-item-main">
              <div className="navpage-item-title">{t.name}</div>
              <div className="navpage-item-desc">
                <span className="np-tag">{t.cron}</span>
                <span>{t.next_run_at ? `下次: ${t.next_run_at}` : "未排程"}</span>
              </div>
            </div>
            <div className="navpage-item-actions">
              <SwitchRow checked={t.enabled} onChange={async (v) => { try { await api.updateScheduledTask(t.id, { enabled: v }); load(); } catch { /* ignore */ } }} />
              <button className="icon-btn" onClick={async () => { if (confirm(`删除「${t.name}」?`)) { try { await api.deleteScheduledTask(t.id); load(); } catch { /* ignore */ } } }}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {tasks.length === 0 && <div className="navpage-empty">暂无定时任务</div>}
      </div>
    </div>
  );
}

function ResourceNavPage<T extends { id: number; is_active: boolean }>({ loader, title, renderName, renderDesc, onToggle, onDelete }: {
  loader: () => Promise<T[]>;
  title: string;
  renderName: (t: T) => string;
  renderDesc: (t: T) => string;
  onToggle: (t: T, v: boolean) => Promise<unknown>;
  onDelete: (t: T) => Promise<unknown>;
}) {
  const [items, setItems] = useState<T[]>([]);
  const load = useCallback(async () => {
    try { setItems(await loader()); } catch { /* ignore */ }
  }, [loader]);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="navpage">
      <div className="navpage-head">
        <span className="navpage-title">{title}</span>
        <button className="btn-ghost" onClick={load}><IconRefresh size={13} /> 刷新</button>
      </div>
      <div className="navpage-list">
        {items.map((it) => (
          <div key={it.id} className="navpage-item">
            <div className="navpage-item-main">
              <div className="navpage-item-title">{renderName(it)}</div>
              <div className="navpage-item-desc">{renderDesc(it)}</div>
            </div>
            <div className="navpage-item-actions">
              <SwitchRow checked={it.is_active} onChange={(v) => onToggle(it, v).then(load)} />
              <button className="icon-btn" onClick={() => onDelete(it).then(load)}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无数据</div>}
      </div>
    </div>
  );
}

export function SkillsPage() {
  return (
    <ResourceNavPage<SkillOut>
      loader={() => api.listSkills()}
      title="技能"
      renderName={(s) => s.display_name || s.name}
      renderDesc={(s) => `${s.description || "无描述"} · ${s.source}`}
      onToggle={(s, v) => api.updateSkill(s.id, { is_active: v })}
      onDelete={(s) => api.deleteSkill(s.id)}
    />
  );
}

export function McpPage() {
  const [items, setItems] = useState<McpServerOut[]>([]);
  const [candidates, setCandidates] = useState<Array<{ name: string; transport: string; command: string | null; args: string[]; env: Record<string, string> | null; url: string | null; source_path: string }>>([]);
  const [scanning, setScanning] = useState(false);

  const load = useCallback(async () => {
    try { setItems(await api.listMcpServers()); } catch { /* ignore */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleScan = async () => {
    setScanning(true);
    try {
      const result = await api.scanMcpServers();
      const existing = new Set(items.map((m) => m.name));
      setCandidates(result.filter((c) => !existing.has(c.name)));
    } catch (e) {
      alert("扫描失败: " + String(e));
    } finally {
      setScanning(false);
    }
  };

  const handleImport = async (c: { name: string; transport: string; command: string | null; args: string[]; env: Record<string, string> | null; url: string | null }) => {
    try {
      await api.createMcpServer({
        name: c.name, transport: c.transport, command: c.command ?? undefined,
        args: c.args, env: c.env ?? undefined, url: c.url ?? undefined, is_active: false,
      });
      setCandidates((prev) => prev.filter((x) => x !== c));
      load();
    } catch (e) {
      alert("导入失败: " + String(e));
    }
  };

  return (
    <div className="navpage">
      <div className="navpage-head">
        <div>
          <span className="navpage-title">MCP 服务器</span>
          <span className="navpage-subtitle">连接外部工具与数据源</span>
        </div>
        <div className="navpage-head-actions">
          <button className="btn-ghost" onClick={handleScan} disabled={scanning}>
            <IconRefresh size={13} /> {scanning ? "扫描中…" : "自动扫描本机"}
          </button>
        </div>
      </div>
      <div className="navpage-list">
        {items.map((m) => (
          <div key={m.id} className="navpage-item">
            <div className="navpage-item-main">
              <div className="navpage-item-title">{m.display_name || m.name}</div>
              <div className="navpage-item-desc">
                <span className="np-tag">{m.transport}</span>
                <span>{m.transport === "stdio" ? m.command || "stdio" : m.url || "sse"}</span>
              </div>
            </div>
            <div className="navpage-item-actions">
              <SwitchRow checked={m.is_active} onChange={async (v) => { try { await api.updateMcpServer(m.id, { is_active: v }); load(); } catch { /* ignore */ } }} />
              <button className="icon-btn" onClick={async () => { if (confirm(`删除「${m.name}」?`)) { try { await api.deleteMcpServer(m.id); load(); } catch { /* ignore */ } } }}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无 MCP 服务器，点击「自动扫描本机」导入</div>}
      </div>
      {candidates.length > 0 && (
        <div className="navpage-candidates">
          <div className="navpage-candidates-title">扫描候选（勾选导入）</div>
          {candidates.map((c, i) => (
            <div key={i} className="navpage-candidate">
              <div className="navpage-item-main">
                <div className="navpage-item-title">{c.name}</div>
                <div className="navpage-item-desc">
                  <span className="np-tag">{c.transport}</span>
                  <span>{c.transport === "stdio" ? c.command : c.url}</span>
                  <span className="np-source">来自 {c.source_path}</span>
                </div>
              </div>
              <button className="btn-primary btn-sm" onClick={() => handleImport(c)}>导入</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
