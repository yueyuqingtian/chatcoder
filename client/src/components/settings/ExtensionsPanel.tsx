/** ExtensionsPanel —— 拓展中心（plan-282-1441 #6）。
 *
 * 把原先「插件 / 技能 / MCP 服务器」三个独立设置页收拢为一个「拓展」页，
 * 用子标签分区展示（用户确认：全部功能保留，按插件/技能/连接器分区）。
 * 左侧面板的「拓展」入口复用本组件，保证两处内容完全一致。
 *
 * 插件子标签是**插件市场**形态（对齐参考项目）：分类条 + 搜索 + 排序 +
 * 卡片网格 + 详情抽屉；支持从本地目录 / Git 安装，插件贡献的 skills 会
 * 出现在「技能」子标签、mcp.json 会出现在「连接器」子标签。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type PluginMarketItem } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { SkillsPanel } from "./SkillsPanel";
import { McpPanel } from "./McpPanel";
import { FormDialog } from "../ui/FormDialog";
import { Dialog, Input } from "../ui";
import { ConfirmDialog } from "../ConfirmDialog";
import {
  IconBox, IconDownload, IconPlug, IconRefresh, IconSearch, IconTrash, IconZap,
} from "../icons";

type SubTab = "plugins" | "skills" | "connectors";

const SUBTABS: Array<{ key: SubTab; label: string; icon: React.ReactNode }> = [
  { key: "plugins", label: "插件", icon: <IconBox size={13} /> },
  { key: "skills", label: "技能", icon: <IconZap size={13} /> },
  { key: "connectors", label: "连接器", icon: <IconPlug size={13} /> },
];

/** S9（plan-41-197）：英文说明的中文化兜底——多数本机插件 manifest 只有英文 description，
 *  直接铺给用户看不懂。此处按常见能力词条给出中文能力标签；
 *  未命中时提示“详见详情”，不强行机翻产生歧义。 */
const EN_ZH_TAGS: Array<[RegExp, string]> = [
  [/code review|review/i, "代码评审"],
  [/\btest(s|ing)?\b|unit test/i, "测试"],
  [/lint|format/i, "代码检查"],
  [/database|sql|query/i, "数据库"],
  [/deploy|ci\/cd|devops|release/i, "部署运维"],
  [/doc(s|umentation)?\b|readme/i, "文档"],
  [/security|audit|scan/i, "安全"],
  [/design|frontend|\bui\b/i, "前端设计"],
  [/git/i, "版本控制"],
  [/workflow|automat/i, "工作流自动化"],
];
const hasZh = (s: string) => /[\u4e00-\u9fff]/.test(s);
function zhTagsFor(text: string): string[] {
  const out: string[] = [];
  for (const [re, label] of EN_ZH_TAGS) {
    if (re.test(text) && !out.includes(label)) out.push(label);
  }
  return out.slice(0, 4);
}

/** 市场分类（"全部"由代码补入；其余从条目聚合，保证分类条始终反映真实数据） */
const DEFAULT_CATEGORIES = [
  "全部", "精选", "办公效率", "内容创作", "代码开发", "代码评审",
  "安全与测试", "数据库与分析", "运维部署", "开发者工具", "设计",
  "产品管理", "知识研究", "工作流",
];

export function ExtensionsPanel({ defaultTab = "plugins" }: { defaultTab?: SubTab }) {
  const [tab, setTab] = useState<SubTab>(defaultTab);

  return (
    <div className="extensions-panel">
      <div className="ext-subtabs" role="tablist">
        {SUBTABS.map((s) => (
          <button
            key={s.key}
            role="tab"
            aria-selected={tab === s.key}
            className={`ext-subtab${tab === s.key ? " active" : ""}`}
            onClick={() => setTab(s.key)}
          >
            {s.icon}
            <span>{s.label}</span>
          </button>
        ))}
      </div>

      {tab === "plugins" && <PluginsCatalog />}
      {tab === "skills" && <SkillsPanel />}
      {tab === "connectors" && <McpPanel />}
    </div>
  );
}

/* ── 插件市场 ── */

function PluginsCatalog() {
  const [items, setItems] = useState<PluginMarketItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("全部");
  const [sort, setSort] = useState<"hot" | "new">("hot");
  /** 详情抽屉 */
  const [detail, setDetail] = useState<PluginMarketItem | null>(null);
  /** 安装入口（本地目录 / Git） */
  const [installMode, setInstallMode] = useState<null | "dir" | "git">(null);
  const [installValue, setInstallValue] = useState("");
  const [dropTarget, setDropTarget] = useState<PluginMarketItem | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.pluginMarketplace();
      setItems(res.items ?? []);
    } catch (e) {
      useChatStore.setState({ error: `加载插件市场失败：${String(e)}` });
    } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const categories = useMemo(() => {
    const fromData = new Set(items.map((i) => i.category).filter(Boolean));
    const merged = DEFAULT_CATEGORIES.filter((c) => c === "全部" || c === "精选" || fromData.has(c));
    // 数据里有但预置列表未覆盖的分类，追加到末尾
    for (const c of fromData) if (!merged.includes(c)) merged.push(c);
    return merged;
  }, [items]);

  const filtered = useMemo(() => {
    let list = items;
    const q = query.trim().toLowerCase();
    if (q) {
      list = list.filter((i) =>
        `${i.name} ${i.displayName} ${i.description} ${i.descriptionZh} ${i.tags.join(" ")}`
          .toLowerCase().includes(q));
    }
    if (category === "精选") list = list.filter((i) => i.featured);
    else if (category !== "全部") list = list.filter((i) => i.category === category);
    // 排序：热门=已安装优先（贴近"用得多"），最新=名称序（本地数据无发布时间）
    const sorted = [...list];
    if (sort === "hot") sorted.sort((a, b) => Number(b.installed ?? false) - Number(a.installed ?? false));
    else sorted.sort((a, b) => a.displayName.localeCompare(b.displayName));
    return sorted;
  }, [items, query, category, sort]);

  const doInstall = async () => {
    const v = installValue.trim();
    if (!v) return;
    setBusy("install");
    try {
      if (installMode === "dir") await api.pluginInstallDir(v);
      else await api.pluginInstallGit(v);
      setInstallMode(null);
      setInstallValue("");
      await load();
    } catch (e) {
      useChatStore.setState({ error: `安装失败：${String(e)}` });
    } finally { setBusy(null); }
  };

  /** plan-282-1441：安装"本机扫描到的真实插件"——直接用它自己的目录，无需用户填路径。
   *  这是市场的主路径：卡片上的「安装」即从 `item.path`（真实插件目录）安装。 */
  const installScanned = async (item: PluginMarketItem) => {
    if (!item.path) return;
    setBusy(item.name);
    try {
      const res = await api.pluginInstallDir(item.path);
      await load();
      await useChatStore.getState().loadBootstrap();
      useChatStore.setState({
        error: `已安装「${item.displayName || item.name}」${res.skills ? `（并入 ${res.skills} 个技能）` : ""}`,
      });
    } catch (e) {
      useChatStore.setState({ error: `安装失败：${String(e)}` });
    } finally { setBusy(null); }
  };

  const toggle = async (item: PluginMarketItem) => {
    setBusy(item.name);
    try {
      await api.pluginSetEnabled(item.name, !item.enabled);
      await load();
      await useChatStore.getState().loadBootstrap();
    } catch (e) {
      useChatStore.setState({ error: String(e) });
    } finally { setBusy(null); }
  };

  const uninstall = async () => {
    if (!dropTarget) return;
    setBusy(dropTarget.name);
    try {
      await api.pluginUninstall(dropTarget.name);
      setDropTarget(null);
      setDetail(null);
      await load();
      await useChatStore.getState().loadBootstrap();
    } catch (e) {
      useChatStore.setState({ error: String(e) });
    } finally { setBusy(null); }
  };

  const installedCount = items.filter((i) => i.installed).length;

  return (
    <div className="plugins-catalog">
      {/* 页头：两段式——上行「标题 + 主操作」，下行「搜索 + 已安装计数」。
          此前标题、长说明、搜索框、两个安装按钮与刷新按钮全挤在一行右侧，
          窄窗口下互相抢宽度并疯狂换行。 */}
      <div className="catalog-head">
        <div className="catalog-head-text">
          <h2 className="catalog-title">
            发现 <span className="catalog-title-dim">Plugins</span>
            {items.length > 0 && <span className="catalog-count">{items.length}</span>}
          </h2>
          <p className="catalog-sub">
            已扫描本机 AI 工具的插件目录（Qoder / Claude / Cursor 等）。安装后，插件贡献的技能会并入
            <b>「技能」</b>、其连接器会并入<b>「连接器」</b>。
          </p>
        </div>
        <div className="catalog-head-actions">
          <button className="btn btn-ghost btn-sm" onClick={() => { setInstallMode("dir"); setInstallValue(""); }}>
            <IconDownload size={13} /> 从目录安装
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => { setInstallMode("git"); setInstallValue(""); }}>
            <IconDownload size={13} /> 从 Git 安装
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={loading} title="重新扫描">
            <IconRefresh size={13} />
          </button>
        </div>
      </div>

      {/* S9（plan-41-197）：搜索 + 分类条整体吸顶（子标签下方 40px），仅卡片区滚动 */}
      <div className="catalog-sticky">
      <div className="catalog-toolbar">
        <div className="catalog-search">
          <IconSearch size={13} />
          <input
            placeholder="搜索名称、说明或标签"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="搜索插件"
          />
        </div>
        <span className="catalog-installed">已安装 {installedCount}</span>
      </div>

      {/* 分类条 + 排序 */}
      <div className="catalog-filters">
        <div className="catalog-categories">
          {categories.map((c) => (
            <button
              key={c}
              className={`catalog-cat${category === c ? " active" : ""}`}
              onClick={() => setCategory(c)}
            >
              {c}
            </button>
          ))}
        </div>
        <div className="catalog-sort">
          <button className={sort === "hot" ? "active" : ""} onClick={() => setSort("hot")}>热门</button>
          <button className={sort === "new" ? "active" : ""} onClick={() => setSort("new")}>最新</button>
        </div>
      </div>
      </div>

      {/* 卡片网格 */}
      {loading && <div className="navpage-empty">正在扫描本机插件…</div>}
      {!loading && filtered.length === 0 && (
        <div className="catalog-empty">
          <span className="catalog-empty-icon"><IconBox size={20} /></span>
          <div className="catalog-empty-title">
            {items.length === 0 ? "本机未扫描到插件" : `没有匹配「${query}」的插件`}
          </div>
          {items.length === 0 && (
            <div className="catalog-empty-desc">
              已查找 Qoder / Claude / Cursor 等工具的插件目录。你也可以用右上角
              「从目录安装」或「从 Git 安装」手动添加插件。
            </div>
          )}
        </div>
      )}
      <div className="catalog-grid">
        {filtered.map((item) => (
          <div className={`catalog-card${item.installed ? " is-installed" : ""}`} key={item.name}>
            <div className="catalog-card-top">
              <span className="catalog-card-icon"><IconBox size={16} /></span>
              <div className="catalog-card-name">{item.displayName || item.name}</div>
              {item.installed && (
                <span className={`catalog-badge${item.enabled ? " on" : ""}`}>
                  {item.enabled ? "已启用" : "已安装"}
                </span>
              )}
            </div>
            <div className="catalog-card-desc" title={item.descriptionZh || item.description}>
              {item.descriptionZh || item.description || "无说明"}
            </div>
            {/* S9：英文说明的插件补一行中文能力标签（本机 manifest 多数只有英文） */}
            {!item.descriptionZh && !hasZh(item.description || "") && (
              <div className="catalog-card-zhtags">
                <b>能力</b>{" "}
                {zhTagsFor(`${item.name} ${item.displayName} ${item.description || ""} ${(item.tags || []).join(" ")}`).join(" · ") || "详见详情"}
              </div>
            )}
            <div className="catalog-card-foot">
              {/* 信息行：分类 · 来源 · 技能数（次级色，独立成行，不再与操作按钮争宽度） */}
              <span className="catalog-card-cat">
                {item.category}
                {/* plan-282-1441：展示真实来源（本机哪个工具）+ 技能数 */}
                {item.source && item.source !== "installed" && (
                  <span className="catalog-card-src">来自 {item.source}</span>
                )}
                {(item.skillCount ?? 0) > 0 && (
                  <span className="catalog-card-src">{item.skillCount} 个技能</span>
                )}
              </span>
              {/* 操作行：靠右、不换行，主次按钮宽度不再摇摆 */}
              <span className="catalog-card-ops">
                {item.installed ? (
                  <>
                    <button className="btn btn-ghost btn-xs" disabled={busy === item.name}
                      onClick={() => void toggle(item)}>
                      {item.enabled ? "停用" : "启用"}
                    </button>
                    <button className="btn btn-ghost btn-xs" onClick={() => setDetail(item)}>详情</button>
                  </>
                ) : (
                  <>
                    <button className="btn btn-ghost btn-xs" onClick={() => setDetail(item)}>详情</button>
                    <button className="btn btn-primary btn-xs"
                      disabled={busy === item.name || !item.path}
                      onClick={() => void installScanned(item)}>
                      {busy === item.name ? "安装中…" : "安装"}
                    </button>
                  </>
                )}
              </span>
            </div>
          </div>
        ))}
      </div>

      {/* 插件详情：走统一浮层基座（Dialog / Radix）。
          此前这里是内联在列表 DOM 末尾的 .catalog-detail 块，长列表下落在首屏之外，
          点击「详情」视口内毫无变化，被判定为"点不进去"。Dialog 是 position:fixed，
          从机制上保证点击即可见，同时获得焦点陷阱、Esc 与关闭后焦点还原。 */}
      <Dialog
        open={detail != null}
        onClose={() => setDetail(null)}
        width={720}
        title={
          <span className="catalog-dialog-title">
            <span className="catalog-card-icon"><IconBox size={16} /></span>
            <span>{detail?.displayName || detail?.name}</span>
            {detail?.version && <span className="catalog-detail-ver">v{detail.version}</span>}
          </span>
        }
        subtitle={
          detail
            ? [
                detail.category,
                detail.author,
                detail.source && detail.source !== "installed" ? `来自 ${detail.source}` : null,
              ].filter(Boolean).join(" · ")
            : undefined
        }
        footer={
          detail ? (
            detail.installed ? (
              <>
                <button className="btn btn-ghost btn-sm" disabled={busy === detail.name}
                  onClick={() => void toggle(detail)}>
                  {detail.enabled ? "停用" : "启用"}
                </button>
                <button className="btn btn-danger btn-sm" onClick={() => setDropTarget(detail)}>
                  <IconTrash size={12} /> 卸载
                </button>
              </>
            ) : (
              <button className="btn btn-primary btn-sm" disabled={busy === detail.name || !detail.path}
                onClick={() => void installScanned(detail)}>
                {busy === detail.name ? "安装中…" : "安装此插件"}
              </button>
            )
          ) : null
        }
      >
        {detail && (
          <div className="catalog-detail">
            <p className="catalog-detail-desc">{detail.descriptionZh || detail.description || "无说明"}</p>
            {/* S9：英文说明的插件补中文能力标签（本机 manifest 多数只有英文） */}
            {!detail.descriptionZh && !hasZh(detail.description || "") && (
              <div className="catalog-card-zhtags">
                <b>能力</b>{" "}
                {zhTagsFor(`${detail.name} ${detail.displayName} ${detail.description || ""} ${(detail.tags || []).join(" ")}`).join(" · ") || "详见下方信息"}
              </div>
            )}
            {(detail.tags ?? []).length > 0 && (
              <div className="catalog-detail-tags">
                {(detail.tags ?? []).map((t) => <span key={t} className="catalog-tag">{t}</span>)}
              </div>
            )}
            {/* 真实插件信息：贡献了哪些技能 / 是否带连接器 / 安装来源目录 */}
            {((detail.skills ?? []).length > 0 || detail.hasMcp || detail.path) && (
              <div className="catalog-detail-facts">
                {(detail.skills ?? []).length > 0 && (
                  <div className="catalog-fact">
                    <span className="catalog-fact-k">贡献技能</span>
                    <span className="catalog-fact-v">{(detail.skills ?? []).join("、")}</span>
                  </div>
                )}
                {detail.hasMcp && (
                  <div className="catalog-fact">
                    <span className="catalog-fact-k">连接器</span>
                    <span className="catalog-fact-v">含 MCP 连接器配置</span>
                  </div>
                )}
                {detail.path && (
                  <div className="catalog-fact">
                    <span className="catalog-fact-k">插件目录</span>
                    <span className="catalog-fact-v mono" title={detail.path}>{detail.path}</span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </Dialog>

      {/* 安装表单 */}
      <FormDialog
        open={installMode != null}
        onClose={() => setInstallMode(null)}
        title={installMode === "git" ? "从 Git 仓库安装插件" : "从本地目录安装插件"}
        submitLabel="安装"
        onSubmit={() => void doInstall()}
        submitDisabled={!installValue.trim() || busy === "install"}
      >
        <Input
          placeholder={installMode === "git"
            ? "仓库地址，例如 https://github.com/user/plugin.git"
            : "插件目录的绝对路径（需包含 plugin.json）"}
          value={installValue}
          onChange={(e) => setInstallValue(e.target.value)}
          aria-label={installMode === "git" ? "仓库地址" : "插件目录"}
        />
      </FormDialog>

      <ConfirmDialog
        open={dropTarget != null}
        title="卸载插件"
        message={
          `将移除插件「${dropTarget?.displayName || dropTarget?.name}」的本地文件，` +
          `并注销它贡献的技能。\n\n此操作不可恢复（可重新安装）。`
        }
        confirmLabel="卸载"
        danger
        onCancel={() => setDropTarget(null)}
        onConfirm={() => void uninstall()}
      />
    </div>
  );
}
