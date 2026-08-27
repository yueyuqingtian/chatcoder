/** 左侧导航页（v7 对齐 ZCode）：自动化（定时任务）/ 技能 / MCP 管理页。 */
import { useCallback, useEffect, useState } from "react";
import { api, type McpServerOut, type ScheduledTaskOut, type SkillOut } from "../api/client";
import { useChatStore } from "../store/chat";
import {
  IconBox, IconCheckSquare, IconClipboard, IconFileText, IconInfo,
  IconPlus, IconRefresh, IconSearch, IconTarget, IconX, IconZap,
  IconChevronDown, IconDownload,
} from "./icons";
import { ConfirmDialog } from "./ConfirmDialog";

function SwitchRow({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="sp-switch">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="sp-slider" />
    </label>
  );
}

/** 任务模板（对齐 zcode 自动化页） */
const IDLE_TEMPLATES = [
  { icon: <IconClipboard size={15} />, name: "Git 站会摘要", desc: "每周五总结这一周发生的事情。", schedule: "最早可用时段", cron: "0 13 * * 5", prompt: "总结本周 git 提交、模块变化与待跟进事项，生成站会摘要。" },
  { icon: <IconZap size={15} />, name: "CI 失败与不稳定测试报告", desc: "汇总近期 CI 失败和不稳定测试，并分析可能原因。", schedule: "最早可用时段", cron: "0 13 * * *", prompt: "汇总近期 CI 失败和不稳定测试，并分析可能原因。" },
  { icon: <IconCheckSquare size={15} />, name: "文档同步检查", desc: "检查 README、docs、配置说明和使用示例是否与当前代码一致。", schedule: "最早可用时段", cron: "0 13 * * 3", prompt: "检查 README、docs、配置说明和使用示例是否与当前代码一致，列出不一致处。" },
];
const CRON_TEMPLATES = [
  { icon: <IconTarget size={15} />, name: "晨会动态", desc: "汇总上一个工作日以来的提交、模块变化与待跟进事项。", schedule: "工作日 09:00", cron: "0 9 * * 1-5", prompt: "汇总上一个工作日以来的 git 提交、模块变化与待跟进事项。" },
  { icon: <IconZap size={15} />, name: "风险扫描", desc: "检查最近 24 小时的代码变更，报告有直接证据的高置信风险。", schedule: "每天 10:00", cron: "0 10 * * *", prompt: "检查最近 24 小时的代码变更，报告有直接证据的高置信风险。" },
  { icon: <IconFileText size={15} />, name: "发布简报", desc: "整理本周已合并变更，生成团队版与用户版发布摘要。", schedule: "每周五 16:00", cron: "0 16 * * 5", prompt: "整理本周已合并变更，生成团队版与用户版发布摘要。" },
  { icon: <IconCheckSquare size={15} />, name: "文档同步检查", desc: "对照近期实现变更，找出可能遗漏的文档更新。", schedule: "每周三 15:00", cron: "0 15 * * 3", prompt: "对照近期实现变更，找出可能遗漏的文档更新。" },
];

/** cron → 人类可读（对齐 zcode 调度文案） */
function cronLabel(cron: string): string {
  const p = cron.trim().split(/\s+/);
  if (p.length !== 5) return cron;
  const [min, hour, , , dow] = p;
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
  if (dow === "*") return `每天 ${time}`;
  if (dow === "1-5") return `工作日 ${time}`;
  const dowMap: Record<string, string> = { "0": "日", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六" };
  if (dowMap[dow]) return `每周${dowMap[dow]} ${time}`;
  return cron;
}

export function ScheduledPage() {
  const [tasks, setTasks] = useState<ScheduledTaskOut[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [createMenu, setCreateMenu] = useState(false);
  const [keepAwake, setKeepAwake] = useState(() => localStorage.getItem("chatcoder.keepAwake") === "1");
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

  useEffect(() => {
    void window.chatcoderAPI?.setKeepAwake?.(keepAwake);
    localStorage.setItem("chatcoder.keepAwake", keepAwake ? "1" : "0");
  }, [keepAwake]);

  // 点击外部关闭创建下拉
  useEffect(() => {
    if (!createMenu) return;
    const handler = () => setCreateMenu(false);
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [createMenu]);

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
    if (!cron) return;
    try {
      await api.createScheduledTask({ session_id: currentSessionId, name: form.name.trim(), cron, prompt: form.prompt.trim() });
      setShowCreate(false);
      const d = new Date(Date.now() + 3600000);
      const pad = (n: number) => String(n).padStart(2, "0");
      setForm((p) => ({ ...p, name: "", prompt: "", runAt: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}` }));
      load();
    } catch { /* ignore */ }
  };

  /** 模板 → 预填表单 */
  const applyTemplate = (tpl: { name: string; cron: string; prompt: string }) => {
    const p = tpl.cron.split(/\s+/);
    const [min, hour, , , dow] = p;
    const d = new Date();
    d.setHours(parseInt(hour, 10) || 9, parseInt(min, 10) || 0, 0, 0);
    const pad = (n: number) => String(n).padStart(2, "0");
    setForm({
      name: tpl.name,
      prompt: tpl.prompt,
      runAt: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`,
      freq: dow === "*" ? "daily" : "weekly",
      weekday: dow === "*" || dow.includes("-") ? "1" : dow,
    });
    setShowCreate(true);
  };

  return (
    <div className="automation-page">
      <h1 className="automation-title">自动化</h1>
      <p className="automation-sub">创建定时任务，或排队在闲时算力空闲时后台执行。</p>

      <div className="automation-card">
        {tasks.length === 0 && !showCreate && <div className="automation-empty">还没有定时任务</div>}
        {tasks.length > 0 && (
          <div className="automation-list">
            {tasks.map((t) => (
              <div key={t.id} className="automation-item">
                <div className="automation-item-main">
                  <div className="automation-item-name">{t.name}</div>
                  <div className="automation-item-desc">{cronLabel(t.cron)}</div>
                </div>
                <div className="automation-item-actions">
                  <SwitchRow checked={t.enabled} onChange={async (v) => { try { await api.updateScheduledTask(t.id, { enabled: v }); load(); } catch { /* ignore */ } }} />
                  <button className="sb-icon-btn" title="删除" onClick={async () => { try { await api.deleteScheduledTask(t.id); load(); } catch { /* ignore */ } }}><IconX size={13} /></button>
                </div>
              </div>
            ))}
          </div>
        )}
        {showCreate && (
          <div className="navpage-form automation-form">
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
            {!currentSessionId && <div className="automation-hint">需先进入一个会话才能创建任务</div>}
            <div className="navpage-form-actions">
              <button className="btn-ghost" onClick={() => setShowCreate(false)}>取消</button>
              <button className="btn-primary" onClick={handleCreate} disabled={!currentSessionId || !form.name.trim() || !form.prompt.trim()}>创建</button>
            </div>
          </div>
        )}
        <div className="automation-actions">
          <div className="automation-create-wrap">
            <button className="automation-btn-primary" onClick={() => setShowCreate((v) => !v)}>
              创建定时任务
            </button>
            <button className="automation-btn-primary automation-btn-caret" onClick={(e) => { e.stopPropagation(); setCreateMenu(!createMenu); }}>
              <IconChevronDown size={13} />
            </button>
            {createMenu && (
              <div className="context-menu automation-create-menu">
                {[["once", "仅一次"], ["daily", "每天"], ["weekly", "每周"], ["monthly", "每月"]].map(([v, l]) => (
                  <div key={v} className="context-menu-item" onClick={() => { setForm((p) => ({ ...p, freq: v })); setShowCreate(true); setCreateMenu(false); }}>{l}</div>
                ))}
              </div>
            )}
          </div>
          <button className="automation-btn-ghost" onClick={() => applyTemplate(IDLE_TEMPLATES[0])}>创建闲时任务</button>
        </div>
      </div>

      <div className="automation-wake">
        <IconInfo size={14} />
        <span className="automation-wake-text">chatcoder 运行会话时保持电脑唤醒。</span>
        <SwitchRow checked={keepAwake} onChange={setKeepAwake} />
      </div>

      <div className="automation-section">闲时任务模板</div>
      <div className="automation-grid">
        {IDLE_TEMPLATES.map((t) => (
          <button key={t.name} className="automation-tpl" onClick={() => applyTemplate(t)}>
            <span className="automation-tpl-head">{t.icon} {t.name}</span>
            <span className="automation-tpl-desc">{t.desc}</span>
            <span className="automation-tpl-sched">{t.schedule}</span>
          </button>
        ))}
      </div>

      <div className="automation-section">定时任务模板</div>
      <div className="automation-grid">
        {CRON_TEMPLATES.map((t, i) => (
          <button key={`${t.name}-${i}`} className="automation-tpl" onClick={() => applyTemplate(t)}>
            <span className="automation-tpl-head">{t.icon} {t.name}</span>
            <span className="automation-tpl-desc">{t.desc}</span>
            <span className="automation-tpl-sched">{t.schedule}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export function SkillsPage() {
  const [items, setItems] = useState<SkillOut[]>([]);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<string>("all");
  const load = useCallback(async () => {
    try { setItems(await api.listSkills()); } catch { /* ignore */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const sources = Array.from(new Set(items.map((s) => s.source || "user")));
  const filtered = items.filter((s) => {
    if (filter !== "all" && (s.source || "user") !== filter) return false;
    if (query.trim()) {
      const q = query.toLowerCase();
      return (s.name + (s.display_name || "") + (s.description || "")).toLowerCase().includes(q);
    }
    return true;
  });
  const grouped = sources.map((src) => ({ src, list: filtered.filter((s) => (s.source || "user") === src) })).filter((g) => g.list.length > 0);
  const sourceLabel = (src: string) => (src === "plugin" ? "Plugin" : src === "project" ? "项目" : "用户");

  return (
    <div className="skills-page">
      <div className="skills-head">
        <h1 className="automation-title">技能</h1>
        <div className="skills-head-actions">
          <button className="sb-icon-btn" title="新建技能" onClick={() => window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "skills" } }))}><IconPlus size={15} /></button>
          <button className="sb-icon-btn" title="导入技能"><IconDownload size={15} /></button>
          <button className="sb-icon-btn" title="刷新" onClick={load}><IconRefresh size={14} /></button>
        </div>
      </div>
      <p className="automation-sub">管理项目级与用户级技能。启用后可在聊天里通过 $skill-name 使用。</p>
      <div className="skills-toolbar">
        <div className="skills-search">
          <IconSearch size={13} />
          <input placeholder="搜索技能…" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <select className="skills-filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="all">全部</option>
          {sources.map((s) => <option key={s} value={s}>{sourceLabel(s)}</option>)}
        </select>
      </div>
      {grouped.map((g) => (
        <div key={g.src} className="skills-group">
          <div className="skills-group-head">
            <span>{sourceLabel(g.src)} 技能 <span className="skills-count">{g.list.length} 项</span></span>
            {g.src === "plugin" && <span className="skills-group-note">由插件注册，修改请到对应插件中进行。</span>}
          </div>
          <div className="skills-list">
            {g.list.map((s) => (
              <div key={s.id} className="skills-item">
                <span className="skills-item-icon"><IconBox size={16} /></span>
                <div className="skills-item-main">
                  <div className="skills-item-name">{s.name}</div>
                  <div className="skills-item-desc">{s.description || "无描述"}</div>
                </div>
                <span className="skills-item-source">{s.source || "user"}</span>
                <span className="skills-item-tag">{sourceLabel(g.src)}</span>
                <SwitchRow checked={s.is_active} onChange={(v) => { void api.updateSkill(s.id, { is_active: v }).then(load); }} />
              </div>
            ))}
          </div>
        </div>
      ))}
      {filtered.length === 0 && <div className="navpage-empty">暂无技能</div>}
    </div>
  );
}

export function McpPage() {
  const [items, setItems] = useState<McpServerOut[]>([]);
  const [candidates, setCandidates] = useState<Array<{ name: string; transport: string; command: string | null; args: string[]; env: Record<string, string> | null; url: string | null; source_path: string }>>([]);
  const [scanning, setScanning] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<McpServerOut | null>(null);

  const load = useCallback(async () => {
    try { setItems(await api.listMcpServers()); } catch { /* ignore */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleScan = async () => {
    setScanning(true);
    try {
      // v6.5: 与设置页 MCP 面板一致——拉取最新列表去重，避免删除后状态不同步
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

  const handleImport = async (c: { name: string; transport: string; command: string | null; args: string[]; env: Record<string, string> | null; url: string | null; source_path?: string }) => {
    setImporting(c.name);
    try {
      await api.createMcpServer({
        name: c.name, transport: c.transport, command: c.command ?? undefined,
        args: c.args, env: c.env ?? undefined, url: c.url ?? undefined, is_active: false,
        path: c.source_path || undefined,
      });
      setCandidates((prev) => prev.filter((x) => x.name !== c.name));
      load();
    } catch (e) {
      useChatStore.setState({ error: `导入 ${c.name} 失败: ${String(e)}` });
    } finally {
      setImporting(null);
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
              <button className="icon-btn" onClick={() => setConfirmTarget(m)}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无 MCP 服务器，点击「自动扫描本机」导入</div>}
      </div>
      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除 MCP 服务器"
        message={confirmTarget ? `删除「${confirmTarget.display_name || confirmTarget.name}」？` : ""}
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
        <div className="navpage-candidates">
          <div className="navpage-candidates-title">扫描候选（勾选导入）</div>
          {candidates.map((c, i) => (
            <div key={`${c.name}-${i}`} className="navpage-candidate">
              <div className="navpage-item-main">
                <div className="navpage-item-title">{c.name}</div>
                <div className="navpage-item-desc">
                  <span className="np-tag">{c.transport}</span>
                  <span>{c.transport === "stdio" ? c.command : c.url}</span>
                  <span className="np-source">来自 {c.source_path}</span>
                </div>
              </div>
              <button className="btn-primary btn-sm" disabled={importing !== null} onClick={() => handleImport(c)}>{importing === c.name ? "导入中…" : "导入"}</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
