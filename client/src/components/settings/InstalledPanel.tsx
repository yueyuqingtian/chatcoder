/** 扩展管理（plan-41-198 S3）：设置 → 拓展。
 *
 * 与左面板「市场」的分工（用户要求）：
 *  - 左面板「拓展」= 市场视图（MarketPanel）：浏览与**安装/导入**；
 *  - 本页 = 已安装管理：只展示**已经安装**的插件 / 技能 / 连接器（+ 智能体计数），
 *    按来源分组，支持搜索、启停、卸载，并保留现有导入与扫描通道（+ 添加）。
 *
 * 设计参照图 4：标题区（扩展管理 + 副标题 + 前往市场）+ 计数标签 + 搜索 + 「+ 添加」
 * + 分组行式列表（图标 / 名称 / 描述 / 更多 / 开关）。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, type McpServerOut, type PluginMarketItem, type SkillOut,
} from "../../api/client";
import { useChatStore } from "../../store/chat";
import { ConfirmDialog } from "../ConfirmDialog";
import { Modal } from "../Modal";
import { Input, Menu, Select, Switch } from "../ui";
import {
  IconBox, IconPlug, IconPlus, IconRefresh, IconSearch, IconTrash, IconMoreHorizontal, IconZap, IconGitBranch,
} from "../icons";
import { ActionDialogs, toneOf } from "./MarketPanel";
import { DatabaseConnectionsPanel } from "./DatabaseConnectionsPanel";
import { DebuggerPanel } from "./DebuggerPanel";

type InstKind = "plugin" | "skill" | "connector";

interface Row {
  key: string;
  kind: InstKind;
  /** 插件用名称做标识；技能/连接器同时持有数据库 id */
  name: string;
  dbId?: number;
  displayName: string;
  description: string;
  enabled: boolean;
  /** 来源键（用于分组标题） */
  source: string;
  /** plan-284-1450：安装时保存的市场图标（已安装列表复用市场视觉） */
  iconUrl?: string;
  /** 本机落盘路径 / 连接地址，详情里展示 */
  path?: string;
}

/** 从安装元数据取市场图标地址（仅接受 https，与市场页同口径） */
function marketIconOf(meta?: Record<string, unknown> | null): string {
  const url = String((meta || {}).market_icon_url || "");
  return url.startsWith("https://") ? url : "";
}

/** 已安装条目图标：优先复用市场图标，缺失或加载失败回退首字母色块（与市场页一致） */
function InstIcon({ name, displayName, iconUrl, size = 28 }: {
  name: string; displayName: string; iconUrl?: string; size?: number;
}) {
  const [failed, setFailed] = useState(false);
  const style = { width: size, height: size };
  if (iconUrl && !failed) {
    return (
      <img
        className="inst-icon-img"
        style={style}
        src={iconUrl}
        alt=""
        loading="lazy"
        onError={() => setFailed(true)}
      />
    );
  }
  return (
    <span className="inst-icon" data-tone={toneOf(name)} style={style}>
      {displayName.trim().slice(0, 1).toUpperCase()}
    </span>
  );
}

const KIND_META: Record<InstKind, { label: string; icon: React.ReactNode }> = {
  plugin: { label: "插件", icon: <IconBox size={13} /> },
  skill: { label: "技能", icon: <IconZap size={13} /> },
  connector: { label: "连接器", icon: <IconPlug size={13} /> },
};

/** 来源键 → 分组标题（plan-41-225：一张表说了算——
 *  此前插件用 marketplaceName、技能用 source，同源内容被拆到不同组，用户看不懂分组规则）。 */
const SOURCE_LABEL: Record<string, string> = {
  qoder: "Qoder 市场",
  // 各外部工具扫描来的技能统一归入「本机扫描」（用户视角：都是从本机其他工具扫到的）
  claude: "本机扫描",
  codex: "本机扫描",
  codebuddy: "本机扫描",
  cursor: "本机扫描",
  trae: "本机扫描",
  chatcoder: "本机扫描",
  builtin: "内置",
  custom: "手动添加",
  repo: "技能仓库",
  plugin: "插件贡献",
  local: "本机安装",
  installed: "本机安装",
};

function notify(msg: string) { useChatStore.setState({ error: msg }); }

export function InstalledPanel() {
  const [plugins, setPlugins] = useState<PluginMarketItem[]>([]);
  const [skills, setSkills] = useState<SkillOut[]>([]);
  const [connectors, setConnectors] = useState<McpServerOut[]>([]);
  const [counts, setCounts] = useState<Record<InstKind, number>>({ plugin: 0, skill: 0, connector: 0 });
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<"all" | InstKind>("all");
  /** plan-41-226：来源筛选（用户反馈缺筛选）——按分组来源过滤，插件/技能/连接器通用 */
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [busy, setBusy] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<Row | null>(null);
  /** plan-284-1450：已安装条目详情（此前行不可点，用户无法查看详情与落盘位置） */
  const [detail, setDetail] = useState<Row | null>(null);
  const [dialog, setDialog] = useState<null | InstKind>(null);
  /** 内置连接器（database / debugger）的专属配置面板（plan-41-198：保留原配置能力） */
  const [configServer, setConfigServer] = useState<McpServerOut | null>(null);
  /** 技能仓库管理：添加 / 同步 / 按需导入 / 移除（保留现有技能仓库导入方式） */
  const [repoOpen, setRepoOpen] = useState(false);
  const [repos, setRepos] = useState<Array<{ id: string; name: string; url: string; skill_count?: number }>>([]);
  const [repoUrl, setRepoUrl] = useState("");
  const [repoBusy, setRepoBusy] = useState<string | null>(null);
  const [repoSkills, setRepoSkills] = useState<{
    repoId: string;
    skills: Array<{ name: string; display_name?: string; description?: string }>;
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // plan-41-226：不再请求已安装聚合（那个接口含「智能体」计数，本项目无此管理能力）。
      // 三个列表长度即事实源——计数与列表天然一致，少一次请求也少一处不一致风险。
      const [pl, sk, mc] = await Promise.all([
        api.pluginInstalled().catch(() => [] as PluginMarketItem[]),
        api.listSkills().catch(() => [] as SkillOut[]),
        api.listMcpServers().catch(() => [] as McpServerOut[]),
      ]);
      setPlugins(pl);
      setSkills(sk);
      setConnectors(mc);
      setCounts({ plugin: pl.length, skill: sk.length, connector: mc.length });
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [];
    for (const p of plugins) {
      out.push({
        key: `plugin:${p.name}`, kind: "plugin", name: p.name,
        displayName: p.displayName || p.name,
        description: p.descriptionZh || p.description || "",
        enabled: !!p.enabled,
        source: p.marketplaceName || "local",
        iconUrl: p.logo || "",
        path: p.path,
      });
    }
    for (const s of skills) {
      out.push({
        key: `skill:${s.id}`, kind: "skill", name: s.name, dbId: s.id,
        displayName: s.display_name || s.name,
        description: s.description || "",
        enabled: s.is_active,
        source: s.source || "custom",
        iconUrl: marketIconOf(s.meta),
        path: s.path || undefined,
      });
    }
    for (const m of connectors) {
      out.push({
        key: `connector:${m.id}`, kind: "connector", name: m.name, dbId: m.id,
        displayName: m.display_name || m.name,
        description: m.description || `${m.transport}${m.command ? ` · ${m.command}` : ""}`,
        enabled: m.is_active,
        source: m.source || "custom",
        iconUrl: marketIconOf(m.meta),
        path: m.url || m.command || undefined,
      });
    }
    return out;
  }, [plugins, skills, connectors]);

  /** 来源选项（从已加载行聚合，保证下拉里不会出现空选项） */
  const sourceOptions = useMemo(() => {
    const set = new Set<string>();
    for (const r of rows) set.add(SOURCE_LABEL[r.source] || r.source || "其他");
    return [...set].sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  }, [rows]);

  const filtered = useMemo(() => {
    let list = rows;
    if (kindFilter !== "all") list = list.filter((r) => r.kind === kindFilter);
    // plan-41-226：来源筛选（补充用户指出的「没有插件的筛选项」）
    if (sourceFilter) list = list.filter((r) => (SOURCE_LABEL[r.source] || r.source || "其他") === sourceFilter);
    const q = query.trim().toLowerCase();
    if (q) {
      list = list.filter((r) =>
        `${r.displayName} ${r.name} ${r.description}`.toLowerCase().includes(q));
    }
    return list;
  }, [rows, kindFilter, sourceFilter, query]);

  /** 按来源分组（同组内保持 插件 → 技能 → 连接器 的稳定顺序） */
  const grouped = useMemo(() => {
    const order: InstKind[] = ["plugin", "skill", "connector"];
    const map = new Map<string, Row[]>();
    for (const r of [...filtered].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind))) {
      const label = SOURCE_LABEL[r.source] || r.source || "其他";
      const arr = map.get(label) ?? [];
      arr.push(r);
      map.set(label, arr);
    }
    return [...map.entries()];
  }, [filtered]);

  const toggleEnabled = async (row: Row) => {
    setBusy(row.key);
    try {
      if (row.kind === "plugin") await api.pluginSetEnabled(row.name, !row.enabled);
      else if (row.kind === "skill" && row.dbId != null) await api.updateSkill(row.dbId, { is_active: !row.enabled });
      else if (row.kind === "connector" && row.dbId != null) await api.updateMcpServer(row.dbId, { is_active: !row.enabled });
      await load();
      await useChatStore.getState().loadBootstrap();
    } catch (e) { notify(String(e)); }
    finally { setBusy(null); }
  };

  const doRemove = async () => {
    const t = dropTarget;
    setDropTarget(null);
    if (!t) return;
    setBusy(t.key);
    try {
      if (t.kind === "plugin") await api.pluginUninstall(t.name);
      else if (t.kind === "skill" && t.dbId != null) await api.deleteSkill(t.dbId);
      else if (t.kind === "connector" && t.dbId != null) await api.deleteMcpServer(t.dbId);
      await load();
      await useChatStore.getState().loadBootstrap();
    } catch (e) { notify(String(e)); }
    finally { setBusy(null); }
  };

  /** 前往市场：左面板「拓展」入口（nav=skills），由 App 的离开设置通道处理 */
  const gotoMarket = () => {
    window.dispatchEvent(new CustomEvent("chatcoder:leave-settings", { detail: { nav: "skills" } }));
  };

  /** 扫描本机技能目录（plan-41-225：从导入弹窗里提为一级入口——
   *  "想扫描"不该先开"导入"弹窗） */
  const scanSkills = async () => {
    try {
      const res = await api.scanSkills();
      await load();
      await useChatStore.getState().loadBootstrap();
      notify(`已扫描本机技能目录（新增 ${res.added ?? 0} 个）`);
    } catch (e) { notify("扫描失败：" + String(e)); }
  };

  /** 扫描本机已配置的 MCP 服务器并导入（默认不启用，由用户逐个开启） */
  const scanMcp = async () => {
    try {
      const candidates = await api.scanMcpServers();
      if (candidates.length === 0) { notify("本机未发现可导入的 MCP 配置"); return; }
      let n = 0;
      for (const c of candidates) {
        await api.createMcpServer({
          name: c.name, transport: c.transport, command: c.command || undefined,
          args: c.args, env: c.env || undefined, url: c.url || undefined,
          is_active: false, path: c.source_path || undefined,
        });
        n += 1;
      }
      await load();
      await useChatStore.getState().loadBootstrap();
      notify(`已从本机配置导入 ${n} 个连接器（默认未启用）`);
    } catch (e) { notify("扫描失败：" + String(e)); }
  };

  // ── 技能仓库：打开弹窗时加载列表（与 SkillsPanel 同一套 API，能力对等） ──
  const loadRepos = useCallback(async () => {
    try { setRepos(await api.listSkillRepos()); } catch { /* 非阻塞 */ }
  }, []);
  useEffect(() => { if (repoOpen) void loadRepos(); }, [repoOpen, loadRepos]);

  /** 计数标签：固定渲染四类（全部 / 插件 / 技能 / 连接器），**不按数据量过滤**。
   *
   *  plan-41-226 修正：此前按「无数据不渲染」过滤，本机插件数为 0 时「插件」标签整个消失，
   *  用户反馈「没有插件选项」。标签是**导航**（告诉用户有哪些类型可看），不是结果列表；
   *  数量为 0 本身就是有效信息（“我还没装插件”），标签消失反而让人以为该类型不被支持。
   *  点击空类型时给明确空态与前往市场引导，因此不存在“点了没反应”的问题。 */
  const filterTabs: Array<{ key: "all" | InstKind; label: string; n: number }> = [
    { key: "all", label: "全部", n: counts.plugin + counts.skill + counts.connector },
    { key: "plugin", label: "插件", n: counts.plugin },
    { key: "skill", label: "技能", n: counts.skill },
    { key: "connector", label: "连接器", n: counts.connector },
  ];

  return (
    <div className="inst-page">
      {/* 自带标题区（与市场页一致的排版；外层 PageShell 不再渲染标题） */}
      <div className="inst-head">
        <div>
          <h1 className="inst-title">扩展管理</h1>
          {/* 副标题不再包含「智能体」——项目无此管理能力，写了会让用户以为有入口 */}
          <p className="inst-sub">管理本机已安装的插件、技能和连接器。</p>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={gotoMarket}>前往市场</button>
      </div>

      <div className="inst-toolbar">
        <div className="inst-counts">
          {filterTabs.map((c) => (
            <button
              key={c.key}
              type="button"
              className={"inst-count" + (kindFilter === c.key ? " on" : "")}
              onClick={() => setKindFilter(c.key)}
            >
              {c.label} <b>{c.n}</b>
            </button>
          ))}
        </div>
        {/* 来源筛选（plan-41-226）：覆盖插件/技能/连接器的来源维度 */}
        {sourceOptions.length > 1 && (
          <Select
            value={sourceFilter}
            onChange={setSourceFilter}
            options={[{ value: "", label: "全部来源" }, ...sourceOptions.map((s) => ({ value: s, label: s }))]}
            style={{ minWidth: 116 }}
            aria-label="按来源筛选"
          />
        )}
        <div className="inst-search">
          <IconSearch size={13} />
          <input
            placeholder="搜索已安装项"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="搜索已安装项"
          />
        </div>
        <Menu
          align="end"
          trigger={
            <button className="btn btn-ghost btn-md">
              <IconPlus size={13} /> 添加
            </button>
          }
          entries={[
            { label: "安装插件（目录 / Git）", icon: <IconBox size={13} />, onSelect: () => setDialog("plugin") },
            { label: "导入技能（本地目录 / md）", icon: <IconZap size={13} />, onSelect: () => setDialog("skill") },
            { label: "技能仓库（Git 同步导入）", icon: <IconGitBranch size={13} />, onSelect: () => setRepoOpen(true) },
            { label: "添加连接器（JSON 导入）", icon: <IconPlug size={13} />, onSelect: () => setDialog("connector") },
            { divider: true, label: "" },
            // 扫描升为一级入口：此前藏在「导入技能」弹窗里，用户得先开弹窗才看得到
            { label: "扫描本机技能目录", icon: <IconRefresh size={13} />, onSelect: () => void scanSkills() },
            { label: "扫描本机 MCP 配置", icon: <IconRefresh size={13} />, onSelect: () => void scanMcp() },
            { divider: true, label: "" },
            { label: "刷新列表", icon: <IconRefresh size={13} />, onSelect: () => void load() },
          ]}
        />
      </div>

      {/* plan-59-286：每类拓展的常驻使用说明——"装完之后怎么用"必须在界面里说清，
          否则用户装完不知道下一步（这是此前最影响可用性认知的缺口）。 */}
      <div className="inst-hint">
        {kindFilter === "plugin"
          ? "插件打包技能与连接器：技能随任务自动匹配，连接器需在列表中启用后才可用。"
          : kindFilter === "skill"
            ? "技能随任务自动匹配（AI 按描述与触发条件选用），也可在对话中用 / 引用。"
            : kindFilter === "connector"
              ? "连接器需启用后才对 AI 可用；启用时会自动获取工具清单，失败可点刷新重试。"
              : "技能随任务自动匹配；连接器需启用后才可用；插件打包技能与连接器。"}
      </div>

      <div className="inst-scroll">
        {loading && rows.length === 0 && <div className="inst-empty">正在加载已安装内容…</div>}
        {!loading && filtered.length === 0 && (
          <div className="inst-empty">
            {/* 空态分三种：搜索无结果 / 该类型未安装 / 全空。
                第二类带主按钮引导——点空类型标签不会“点了没反应”。 */}
            {query ? (
              `没有匹配「${query}」的已安装项`
            ) : kindFilter !== "all" ? (
              <>
                <div>还没有安装{filterTabs.find((c) => c.key === kindFilter)?.label ?? "该类型"}，去市场看看？</div>
                <button className="btn btn-primary btn-sm" onClick={gotoMarket}>前往市场</button>
              </>
            ) : (
              <>
                <div>还没有安装任何扩展</div>
                <button className="btn btn-primary btn-sm" onClick={gotoMarket}>前往市场</button>
              </>
            )}
          </div>
        )}
        {grouped.map(([label, list]) => (
          <div key={label} className="inst-group">
            <div className="inst-group-title">{label}</div>
            <div className="inst-list">
              {list.map((row) => (
                <div
                  key={row.key}
                  className="inst-row"
                  role="button"
                  tabIndex={0}
                  onClick={() => setDetail(row)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDetail(row); }
                  }}
                >
                  <InstIcon name={row.name} displayName={row.displayName} iconUrl={row.iconUrl} />
                  <div className="inst-main">
                    <div className="inst-name">
                      {row.displayName}
                      <span className="inst-kind">{KIND_META[row.kind].label}</span>
                    </div>
                    {row.description && <div className="inst-desc">{row.description}</div>}
                  </div>
                  {/* 操作区阻止冒泡：点开关/菜单不应误开详情 */}
                  <div className="inst-actions" onClick={(e) => e.stopPropagation()}>
                    {/* plan-41-225：行内只保留「… + 开关」两件套（对齐参考图）；
                        配置类动作与移除收进菜单，开关已承担启停（菜单不再重复同一动作）。 */}
                    <Menu
                      align="end"
                      trigger={
                        <button className="ui-icon-btn size-xs" aria-label="更多操作">
                          <IconMoreHorizontal size={14} />
                        </button>
                      }
                      entries={[
                        ...(row.kind === "connector" && (row.name === "database" || row.name === "debugger")
                          ? [{
                              label: "配置",
                              onSelect: () => setConfigServer(connectors.find((c) => c.id === row.dbId) ?? null),
                            }]
                          : []),
                        { label: "移除", icon: <IconTrash size={12} />, danger: true, onSelect: () => setDropTarget(row) },
                      ]}
                    />
                    {/* 开关统一走 ui/Switch（项目规则：开关不得自绘） */}
                    <Switch
                      checked={row.enabled}
                      onChange={() => void toggleEnabled(row)}
                      disabled={busy === row.key}
                      aria-label={`${row.displayName} 启用状态`}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* 内置连接器配置（数据库连接 / 开发调试）——保留原有专属配置面板 */}
      <Modal
        open={configServer !== null}
        onClose={() => setConfigServer(null)}
        title={configServer?.name === "database" ? "数据库连接配置" : configServer?.name === "debugger" ? "开发调试配置" : "连接器配置"}
        subtitle={configServer?.description || undefined}
        width={860}
      >
        {configServer?.name === "database" && <DatabaseConnectionsPanel server={configServer} />}
        {configServer?.name === "debugger" && <DebuggerPanel server={configServer} />}
      </Modal>

      {/* 技能仓库：添加 / 同步 / 按需导入 / 移除（保留现有技能仓库导入方式） */}
      <Modal
        open={repoOpen}
        onClose={() => { setRepoOpen(false); setRepoSkills(null); }}
        title="技能仓库"
        subtitle="添加云端技能仓库（Git），同步后可按需导入其中的技能"
        width={680}
      >
        <div className="inst-repo">
          <div className="inst-repo-add">
            <Input
              placeholder="https://github.com/user/skills.git"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              aria-label="技能仓库地址"
            />
            <button
              className="btn btn-primary btn-sm"
              disabled={!repoUrl.trim() || repoBusy !== null}
              onClick={async () => {
                setRepoBusy("add");
                try { await api.createSkillRepo({ url: repoUrl.trim() }); setRepoUrl(""); await loadRepos(); }
                catch (e) { notify(String(e)); }
                finally { setRepoBusy(null); }
              }}
            >
              <IconPlus size={13} /> 添加
            </button>
          </div>
          {repos.length === 0 && <div className="inst-empty">暂无技能仓库</div>}
          {repos.map((r) => (
            <div key={r.id} className="inst-repo-row">
              <div className="inst-repo-main">
                <div className="inst-repo-name">{r.name}</div>
                <div className="inst-repo-url">{r.url}</div>
              </div>
              <button
                className="btn btn-ghost btn-xs"
                disabled={repoBusy !== null}
                onClick={async () => {
                  setRepoBusy(r.id);
                  try {
                    const res = await api.syncSkillRepo(r.id);
                    setRepoSkills({ repoId: r.id, skills: res.skills });
                    notify(`已同步「${r.name}」，可导入 ${res.skills.length} 个技能`);
                  } catch (e) { notify(String(e)); }
                  finally { setRepoBusy(null); }
                }}
              >
                <IconRefresh size={12} /> {repoBusy === r.id ? "同步中…" : "同步"}
              </button>
              <button
                className="btn btn-ghost btn-xs"
                aria-label="移除仓库"
                disabled={repoBusy !== null}
                onClick={async () => {
                  setRepoBusy(r.id);
                  try {
                    await api.deleteSkillRepo(r.id);
                    await loadRepos();
                    if (repoSkills?.repoId === r.id) setRepoSkills(null);
                  } catch (e) { notify(String(e)); }
                  finally { setRepoBusy(null); }
                }}
              >
                <IconTrash size={12} />
              </button>
            </div>
          ))}
          {repoSkills && (
            <div className="inst-repo-skills">
              <div className="inst-group-title">可导入技能（{repoSkills.skills.length}）</div>
              {repoSkills.skills.map((s) => (
                <div key={s.name} className="inst-repo-skill">
                  <div className="inst-main">
                    <div className="inst-name">{s.display_name || s.name}</div>
                    {s.description && <div className="inst-desc">{s.description}</div>}
                  </div>
                  <button
                    className="btn btn-primary btn-xs"
                    disabled={repoBusy !== null}
                    onClick={async () => {
                      setRepoBusy(s.name);
                      try {
                        await api.importRepoSkill(repoSkills.repoId, s.name);
                        await load();
                        notify(`已导入技能「${s.display_name || s.name}」`);
                      } catch (e) { notify(String(e)); }
                      finally { setRepoBusy(null); }
                    }}
                  >
                    导入
                  </button>
                </div>
              ))}
              {repoSkills.skills.length === 0 && <div className="inst-empty">该仓库未发现可导入技能</div>}
            </div>
          )}
        </div>
      </Modal>

      <ActionDialogs
        which={dialog}
        onClose={() => setDialog(null)}
        onDone={async () => { setDialog(null); await load(); await useChatStore.getState().loadBootstrap(); }}
      />

      {/* 已安装条目详情（plan-284-1450）：此前行不可点，用户看不到来源/落盘位置与开关状态 */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={
          detail ? (
            <span className="inst-detail-title">
              <InstIcon name={detail.name} displayName={detail.displayName} iconUrl={detail.iconUrl} size={30} />
              <span>{detail.displayName}</span>
              <span className="inst-kind">{KIND_META[detail.kind].label}</span>
            </span>
          ) : (
            "详情"
          )
        }
        subtitle={detail
          ? [SOURCE_LABEL[detail.source] || detail.source || "其他", detail.name].filter(Boolean).join(" · ")
          : undefined}
        width={560}
        footer={
          detail ? (
            <>
              <button
                className="btn btn-ghost btn-sm"
                disabled={busy === detail.key}
                onClick={() => void toggleEnabled(detail)}
              >
                {detail.enabled ? "停用" : "启用"}
              </button>
              <button
                className="btn btn-danger-ghost btn-sm"
                onClick={() => { setDropTarget(detail); setDetail(null); }}
              >
                <IconTrash size={12} /> {detail.kind === "plugin" ? "卸载" : "删除"}
              </button>
            </>
          ) : null
        }
      >
        {detail && (
          <div className="inst-detail">
            <p className="inst-detail-desc">{detail.description || "暂无说明"}</p>
            <div className="inst-detail-facts">
              <div className="inst-fact">
                <span className="inst-fact-k">状态</span>
                <span className="inst-fact-v">{detail.enabled ? "已启用" : "已停用"}</span>
              </div>
              <div className="inst-fact">
                <span className="inst-fact-k">来源</span>
                <span className="inst-fact-v">{SOURCE_LABEL[detail.source] || detail.source || "其他"}</span>
              </div>
              <div className="inst-fact">
                <span className="inst-fact-k">标识</span>
                <span className="inst-fact-v mono">{detail.name}</span>
              </div>
              {detail.path && (
                <div className="inst-fact">
                  <span className="inst-fact-k">{detail.kind === "connector" ? "连接" : "本机位置"}</span>
                  <span className="inst-fact-v mono">{detail.path}</span>
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={dropTarget !== null}
        title={dropTarget?.kind === "plugin" ? "卸载插件" : dropTarget?.kind === "skill" ? "删除技能" : "删除连接器"}
        message={
          dropTarget?.kind === "plugin"
            ? `将移除插件「${dropTarget?.displayName}」的本地文件，并注销它贡献的技能。\n\n此操作不可恢复（可重新安装）。`
            : `将删除「${dropTarget?.displayName}」。\n\n此操作不可恢复。`
        }
        confirmLabel={dropTarget?.kind === "plugin" ? "卸载" : "删除"}
        danger
        onCancel={() => setDropTarget(null)}
        onConfirm={() => void doRemove()}
      />
    </div>
  );
}
