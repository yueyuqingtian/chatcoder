/** 市场面板（plan-41-198 S2）：左面板「拓展」的市场视图。
 *
 * 设计参照 qoder 市场（https://qoder.com.cn/marketplace）：
 *  - 顶部：插件 / 技能 / 连接器 三标签 + 搜索 + 「已安装」筛选 + 主动作按钮（固定）；
 *  - 内容区独立滚动：`发现 <类型>` 标题区 + 分类条（含热门/最新）+ 三列卡片网格；
 *  - 卡片：图标 + 名称 + 中文描述（两行省略）；点击进入详情。
 *
 * 条目来源分级（后端 /market/catalog 标注 installKind）：
 *  - scan      本机扫描到、尚未安装 → 「安装」直接从本机目录安装；
 *  - market    仅在内置精选目录中 → 「前往市场获取」引导，不伪造仓库地址；
 *  - installed 已安装 → 启停 / 卸载（与设置页「扩展管理」同源）。
 *
 * 现有导入与扫描能力在此保留入口：插件（目录 / Git）、技能（本地导入 / 技能仓库 /
 * 扫描本机）、连接器（导入 JSON / 扫描本机 / 手动创建）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type MarketCategoryFacet, type MarketItem, type MarketKind } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { ConfirmDialog } from "../ConfirmDialog";
import { Modal } from "../Modal";
import { Input, Menu, Switch, Textarea } from "../ui";
import {
  IconSearch, IconRefresh, IconPlus, IconDownload, IconTrash, IconFolder, IconMoreHorizontal,
} from "../icons";
import { MarketDecor, KindMark, KIND_ACCENT, WaveUnderline } from "./MarketDecor";

const KINDS: Array<{
  key: MarketKind; label: string; icon: React.ReactNode; noun: string;
  hero: string;
  /** 副标题里的强调词（下方画手写标记线） */
  waveWord: string;
  /** 标记线颜色：取品类的"铅笔档"（比主色浅一档），它是标注而不是高亮 */
  waveColor: string;
}> = [
  { key: "plugin", label: "插件", icon: <KindMark kind="plugin" size={32} />, noun: "Plugins",
    hero: "聚焦 效率、数据分析、设计。", waveWord: "效率", waveColor: KIND_ACCENT.plugin.pencil },
  { key: "skill", label: "技能", icon: <KindMark kind="skill" size={32} />, noun: "Skills",
    hero: "聚焦 效率提升、内容创作、官方精选。", waveWord: "效率提升", waveColor: KIND_ACCENT.skill.pencil },
  { key: "connector", label: "连接器", icon: <KindMark kind="connector" size={32} />, noun: "Connectors",
    hero: "聚焦 内容创作、知识研究。", waveWord: "内容创作", waveColor: KIND_ACCENT.connector.pencil },
];

/** 副标题渲染：把配置的强调词包进波浪容器（参考图里关键词下有一条手绘波浪线）。 */
function renderHero(k: { hero: string; waveWord: string; waveColor: string }) {
  const idx = k.hero.indexOf(k.waveWord);
  if (idx < 0) return k.hero;
  return (
    <>
      {k.hero.slice(0, idx)}
      <span className="mkt-wave-word">
        {k.waveWord}
        {/* chars：波浪宽度按词长自适应（plan-41-227），避免固定 viewBox 被过度压缩变形 */}
        <WaveUnderline color={k.waveColor} chars={k.waveWord.length} />
      </span>
      {k.hero.slice(idx + k.waveWord.length)}
    </>
  );
}

function notify(msg: string) { useChatStore.setState({ error: msg }); }

/** 安装成功的"可操作说明"（plan-59-286）。
 *  此前只报"已安装"，用户不知道接下来怎么用；这里一次说清可操作点：
 *  技能随任务自动匹配、连接器需手动启用、两者都能在对话里用 / 引用。 */
function installNotice(o: { name: string; skills?: number; connectors?: number }): string {
  const parts: string[] = [];
  if (o.skills) parts.push(`贡献 ${o.skills} 个技能（随任务自动匹配，也可用 / 引用）`);
  if (o.connectors) parts.push(`贡献 ${o.connectors} 个连接器（需在「设置 → 扩展管理」启用后生效）`);
  if (parts.length === 0) parts.push("可直接在对话中使用（用 / 引用）");
  return `已安装「${o.name}」：${parts.join("；")}。`;
}

function openExternal(url: string) {
  const w = window as Window & { chatcoderAPI?: { openExternal?: (u: string) => Promise<unknown> } };
  if (w.chatcoderAPI?.openExternal) void w.chatcoderAPI.openExternal(url);
  else window.open(url, "_blank");
}

/** 卡片图标配色：按名称取稳定色相（同一插件每次渲染同色）；导出供设置页扩展管理复用 */
export const TONES = ["blue", "green", "orange", "purple", "red"] as const;
export function toneOf(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 997;
  return TONES[h % TONES.length];
}

/** plan-41-225：条目图标——远端有 icon_url 时用真实图片，缺失或加载失败回退首字母色块。
 *  回退是必需的：远端图标可能 404 或被网络拦截，宁可显示色块也不能出现破图。 */
function MarketIcon({ item, failed, onFail, size = 24 }: {
  item: MarketItem;
  failed: Set<string>;
  onFail: (id: string) => void;
  size?: number;
}) {
  const style = { width: size, height: size };
  if (item.iconUrl && !failed.has(item.id)) {
    return (
      <img
        className="market-icon-img"
        style={style}
        src={item.iconUrl}
        alt=""
        loading="lazy"
        onError={() => onFail(item.id)}
      />
    );
  }
  return (
    <span className="market-icon-letter" style={style} data-tone={toneOf(item.name)} aria-hidden>
      {(item.displayName || item.name).trim().slice(0, 1).toUpperCase()}
    </span>
  );
}

/** 单页条目数（后端上限 60；40 兼顾首屏速度与滚动次数） */
const PAGE_SIZE = 40;
/** 搜索防抖：远端检索，避免逐键打后端 */
const SEARCH_DEBOUNCE_MS = 400;

/** plan-41-226：分类兑底聚合。
 *  远端 facets 在条目少的类型下（插件 109 条 / 连接器 278 条）经常为空，
 *  分类条就只剩「全部/精选」——用户反馈「没有插件的筛选项」。
 *  这里从已加载条目按分类聚合，保证每个标签页都有可用的筛选；
 *  筛选时把该值当远端 category 参数透传（分类 code 与远端同源，可直接用）。 */
function aggregateCategories(list: MarketItem[]): MarketCategoryFacet[] {
  const map = new Map<string, { label: string; count: number }>();
  for (const it of list) {
    // 本机项（内置 / 已安装）不参与分类聚合：它们不属于远端分类体系
    if (it.group) continue;
    // 筛选要用远端 code（category 已本地化成中文，不能拿它去查远端）
    const code = (it.categoryCode || it.category || "").trim();
    if (!code) continue;
    const prev = map.get(code);
    map.set(code, { label: it.category || code, count: (prev?.count ?? 0) + 1 });
  }
  return [...map.entries()]
    .map(([code, v]) => ({ code, label: v.label, count: v.count }))
    .sort((a, b) => b.count - a.count);
}

/** plan-284-1451：模糊搜索。
 *  远端检索是关键词匹配，用户少打几个字（如 `chromedev`、`qoder cred`）就搜不到；
 *  这里在已加载结果上再叠一层模糊匹配：分词后每个词都要命中（子串或子序列），
 *  全部词命中才算匹配 —— 比单关键词更贴合"边想边打"的检索习惯。 */
function isSubsequence(needle: string, hay: string): boolean {
  let i = 0;
  for (let j = 0; j < hay.length && i < needle.length; j++) {
    if (hay[j] === needle[i]) i++;
  }
  return i >= needle.length;
}

function fuzzyHit(text: string, query: string): boolean {
  const hay = text.toLowerCase();
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  return terms.every((t) => hay.includes(t) || isSubsequence(t, hay));
}

export function MarketPanel() {
  const [kind, setKind] = useState<MarketKind>("plugin");
  const [items, setItems] = useState<MarketItem[]>([]);
  const [categories, setCategories] = useState<MarketCategoryFacet[]>([]);
  const [marketUrl, setMarketUrl] = useState("");
  const [loading, setLoading] = useState(false);
  /** plan-41-225：远端分页状态——全量可见靠它（本地只持有已加载的页） */
  const [page, setPage] = useState(1);
  const [lastPage, setLastPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [degraded, setDegraded] = useState(false);
  /** 图标加载失败的条目 id（回退首字母色块，避免破图） */
  const [iconFailed, setIconFailed] = useState<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  /** 已选分类 code（"" = 全部；"__featured__" = 精选，本地过滤） */
  const [category, setCategory] = useState("");
  const [sort, setSort] = useState<"hot" | "new">("hot");
  const [detail, setDetail] = useState<MarketItem | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  /** plan-284-1451：刷新按钮的旋转态（点击后至少转满一圈，给足视觉反馈） */
  const [refreshing, setRefreshing] = useState(false);
  const [dropTarget, setDropTarget] = useState<MarketItem | null>(null);
  /** 主动作对话框：插件安装 / 技能导入 / 连接器添加 */
  const [dialog, setDialog] = useState<null | "plugin" | "skill" | "connector">(null);
  /** 查询参数快照（避免异步回写时读到已变的 state） */
  const queryRef = useRef({ kind, query: "", category: "", sort: "hot" });
  /** 已加载条目镜像：加载更多时需基于「最新列表」追加，并用于分类兑底聚合 */
  const itemsRef = useRef<MarketItem[]>([]);
  useEffect(() => { itemsRef.current = items; }, [items]);

  /** plan-41-225：拉取市场数据（远端分页 + 归一化 + 已装状态；失败自动降级到缓存/本机）。
   *  append=false 用于切类型/搜索/分类/排序（重置到第 1 页）；
   *  append=true 用于滚到底部的加载更多（追加而非替换，保证滚动位置不跳）。 */
  const fetchPage = useCallback(async (k: MarketKind, pageNum: number, append: boolean) => {
    const snap = { ...queryRef.current, kind: k };
    if (append) setLoadingMore(true); else setLoading(true);
    try {
      const res = await api.marketBrowse({
        kind: snap.kind,
        page: pageNum,
        pageSize: PAGE_SIZE,
        keyword: snap.query,
        // 本地伪分类（精选）不传远端；其余 code 透传以覆盖全库筛选
        category: snap.category.startsWith("__") ? "" : snap.category,
        sort: snap.sort as "hot" | "new",
      });
      // 丢弃过期响应（筛选快速切换 / 切类型期间的回包）
      const cur = queryRef.current;
      if (cur.kind !== snap.kind || cur.query !== snap.query
        || cur.category !== snap.category || cur.sort !== snap.sort) return;
      const nextItems = append
        ? [...itemsRef.current, ...(res.items ?? [])]
        : (res.items ?? []);
      setItems(nextItems);
      setPage(res.page ?? pageNum);
      setLastPage(res.lastPage ?? 1);
      setTotal(res.total ?? 0);
      // 分类：优先用远端 facets；为空时从已加载条目聚合，保证筛选条不空着。
      // 均无数据时保留上一次的分类，避免排序/翻页瞬间分类条闪成空白（伴生页面抖动）。
      const nextCats = (res.categories ?? []).length > 0
        ? (res.categories ?? [])
        : aggregateCategories(nextItems);
      if (nextCats.length > 0) setCategories(nextCats);
      setMarketUrl(res.marketUrl || "");
      setDegraded(!!res.degraded);
    } catch (e) {
      notify("加载市场失败：" + String(e));
    } finally {
      if (append) setLoadingMore(false); else setLoading(false);
    }
  }, []);

  /** 查询条件变化 → 重置到第 1 页重新拉取。
   *  搜索词防抖 400ms 并走**远端**（全库检索）：43,710 条不可能在本地过滤。 */
  useEffect(() => {
    queryRef.current = { kind, query: query.trim(), category, sort };
    const delay = query.trim() ? SEARCH_DEBOUNCE_MS : 0;
    const timer = window.setTimeout(() => { void fetchPage(kind, 1, false); }, delay);
    return () => window.clearTimeout(timer);
  }, [kind, query, category, sort, fetchPage]);

  /** 切换类型：分类回退「全部」（分类集合随类型变化），保留搜索词（跨类型检索意图延续） */
  const switchKind = (k: MarketKind) => {
    if (k === kind) return;
    setCategory("");
    setDetail(null);
    setItems([]);
    setPage(1);
    setKind(k);
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  };

  /** 无限滚动：距底 400px 且还有下一页时追加加载 */
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el || loading || loadingMore) return;
    if (page >= lastPage) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 400) {
      void fetchPage(kind, page + 1, true);
    }
  };

  /** 图标加载失败记录（回退首字母色块，避免破图） */
  const onIconFail = useCallback((id: string) => {
    setIconFailed((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  /** 本地可见集：远端已完成分类/关键词检索，这里再叠三件事——
   *  1）「精选」本地开关；
   *  2）模糊匹配收窄（少打字、跳字母也能命中，见 fuzzyHit）；
   *  3）已安装条目置顶（装过的排在上面，方便直接启停 / 卸载）。 */
  const visible = useMemo(() => {
    let list = items;
    if (category === "__featured__") list = list.filter((i) => i.featured || i.installed);
    const q = query.trim();
    if (q) {
      const hits = list.filter((i) =>
        fuzzyHit(`${i.displayName} ${i.name} ${i.description ?? ""} ${(i.tags ?? []).join(" ")}`, q));
      // 模糊收窄只在确实有命中时生效：远端可能是语义匹配，本地过滤不该把结果清空
      if (hits.length > 0) list = hits;
    }
    // 稳定排序：已安装在前，其余保持远端给出的顺序
    return [...list].sort((a, b) => Number(b.installed ?? false) - Number(a.installed ?? false));
  }, [items, category, query]);

  /** 分组展示：**仅连接器页**分「内置 / 本机已安装 / 市场精选」三段。
   *  连接器天然有"系统内置（数据库连接 / 开发调试）"这一类，分组能让用户看懂
   *  "哪些是自带的、哪些是我装的"；技能与插件页则不需要分段——
   *  它们的已安装条目已被"置顶"排序表达（用户明确要求：技能与插件不分列）。 */
  const sections = useMemo(() => {
    if (kind !== "connector") {
      return [{ key: "all" as const, label: "", items: visible }];
    }
    const buckets = [
      { key: "builtin" as const, label: "内置", items: [] as MarketItem[] },
      { key: "local" as const, label: "本机已安装", items: [] as MarketItem[] },
      { key: "market" as const, label: "市场精选", items: [] as MarketItem[] },
    ];
    for (const it of visible) {
      const idx = it.group === "builtin" ? 0 : it.group === "local" ? 1 : 2;
      buckets[idx].items.push(it);
    }
    return buckets.filter((b) => b.items.length > 0);
  }, [visible, kind]);

  /** 组标题只在连接器页出现（且不带计数——数量由标题行的规模徽标承担） */
  const showGroupTitles = kind === "connector";

  /** plan-41-225：安装条目到本机（用户要求「点击就安装，不跳浏览器」）。
   *  - 本机扫描到的插件（installKind=scan）→ 直接用它的本地目录安装；
   *  - 远端条目 → 走后端 /market/install：技能下载技能包落盘入库、插件下载包并复用
   *    既有插件安装流程、连接器写 MCP 配置（默认不启用，由用户在扩展管理开启）。 */
  const installItem = async (item: MarketItem) => {
    setBusy(item.id);
    try {
      // 连接器默认不启用，插件/技能安装即生效——乐观更新要按此区分，否则开关会显示成已启用
      let enabledAfter = item.kind !== "connector";
      if (item.installKind === "scan" && item.path) {
        const res = await api.pluginInstallDir(item.path);
        notify(installNotice({
          name: item.displayName, skills: res.skills, connectors: res.connectorCount,
        }));
      } else {
        // 远端 id：从 `<kind>:<ident>` 拆出（ident 自身可能含冒号，故只切第一段）
        const ident = item.id.includes(":") ? item.id.slice(item.id.indexOf(":") + 1) : item.id;
        const res = await api.marketInstall({
          kind: item.kind, ident,
          displayName: item.displayName, description: item.description,
        });
        if (typeof res.enabled === "boolean") enabledAfter = res.enabled;
        if (res.kind === "connector") {
          notify(`已添加连接器「${res.name}」：需在「设置 → 扩展管理」启用后生效，对话中可用 / 引用。`);
        } else if (res.kind === "plugin") {
          notify(installNotice({
            name: res.name, skills: res.skills, connectors: res.connectorCount,
          }));
        } else {
          notify(`已安装技能「${res.name}」：随任务自动匹配，也可在对话中用 / 引用。`);
        }
      }
      // plan-284-1450：先就地把条目切成「已安装」，列表与详情按钮立即变化，
      // 不再等重新拉取（用户反馈：安装完成后列表仍显示「安装」）。
      const patch = (it: MarketItem): MarketItem =>
        it.id === item.id
          ? { ...it, installed: true, enabled: enabledAfter, installKind: "installed" }
          : it;
      setItems((prev) => prev.map(patch));
      setDetail((prev) => (prev && prev.id === item.id ? patch(prev) : prev));
      await fetchPage(kind, 1, false);
      await useChatStore.getState().loadBootstrap();
    } catch (e) {
      // 后端对失败场景返回 400 + 中文可读原因，直接展示而不再裹一层技术错
      notify(`安装失败：${String(e)}`);
    } finally { setBusy(null); }
  };

  /** 前往「设置 → 扩展管理」管理已安装项（市场页只负责发现与获取） */
  const goManage = () => {
    setDetail(null);
    window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "extensions" } }));
  };

  /** plan-284-1451：启停按品类分派（插件用名称，技能/连接器用本机记录 id），
   *  并**就地**更新行内开关，避免等重新拉取导致滑块回跳。 */
  const toggleEnabled = async (item: MarketItem) => {
    setBusy(item.id);
    const next = !item.enabled;
    try {
      if (item.kind === "plugin") await api.pluginSetEnabled(item.name, next);
      else if (item.kind === "skill" && item.localId != null) await api.updateSkill(item.localId, { is_active: next });
      else if (item.kind === "connector" && item.localId != null) await api.updateMcpServer(item.localId, { is_active: next });
      const patch = (it: MarketItem): MarketItem => (it.id === item.id ? { ...it, enabled: next } : it);
      setItems((prev) => prev.map(patch));
      setDetail((prev) => (prev && prev.id === item.id ? patch(prev) : prev));
      await useChatStore.getState().loadBootstrap();
    } catch (e) { notify(String(e)); }
    finally { setBusy(null); }
  };

  /** 卸载 / 删除已安装条目（三点菜单入口）。三类同源，均走既有删除通道。 */
  const doUninstall = async () => {
    const t = dropTarget;
    setDropTarget(null);
    if (!t) return;
    setBusy(t.id);
    try {
      if (t.kind === "plugin") await api.pluginUninstall(t.name);
      else if (t.kind === "skill" && t.localId != null) await api.deleteSkill(t.localId);
      else if (t.kind === "connector" && t.localId != null) await api.deleteMcpServer(t.localId);
      setDetail(null);
      // 就地改回未安装，随后拉取校准（远端仍是同一分类，不需要整页重排）
      const patch = (it: MarketItem): MarketItem =>
        it.id === t.id ? { ...it, installed: false, enabled: false, installKind: "remote", localId: undefined } : it;
      setItems((prev) => prev.map(patch));
      await fetchPage(kind, 1, false);
      await useChatStore.getState().loadBootstrap();
    } catch (e) { notify(String(e)); }
    finally { setBusy(null); }
  };

  /** 刷新：重拉当前查询并让图标转满一圈（点击的即时反馈） */
  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await fetchPage(kind, 1, false);
      await useChatStore.getState().loadBootstrap();
    } finally {
      // 至少转满一圈（--dur-spin 基准 0.8s）再停下，避免"点了没转完就复位"
      window.setTimeout(() => setRefreshing(false), 820);
    }
  };

  const current = KINDS.find((k) => k.key === kind)!;

  /** 单行渲染（分组列表复用）：图标 + 名称/描述 + 操作区。 */
  const renderRow = (item: MarketItem) => (
    <div
      key={item.id}
      className={"market-row" + (item.installed ? " is-installed" : "")}
      role="button"
      tabIndex={0}
      onClick={() => setDetail(item)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDetail(item); }
      }}
    >
      <MarketIcon item={item} failed={iconFailed} onFail={onIconFail} size={30} />
      <span className="market-row-text">
        <span className="market-row-name">{item.displayName || item.name}</span>
        <span className="market-row-desc">{item.description || "暂无说明"}</span>
      </span>
      {/* 操作区对齐参考图——
          未安装：一个「+」图标按钮（点一下即装，不再用文字按钮占宽）；
          已安装：「…」菜单（详情 / 卸载）+ 启停滑块。 */}
      <span className="market-row-ops" onClick={(e) => e.stopPropagation()}>
        {item.installed ? (
          <>
            <Menu
              align="end"
              trigger={
                <button type="button" className="ui-icon-btn size-xs" aria-label="更多操作">
                  <IconMoreHorizontal size={15} />
                </button>
              }
              entries={[
                { label: "查看详情", onSelect: () => setDetail(item) },
                {
                  label: item.kind === "plugin" ? "卸载" : "删除",
                  icon: <IconTrash size={12} />,
                  danger: true,
                  onSelect: () => setDropTarget(item),
                },
              ]}
            />
            {(item.kind === "plugin" || item.localId != null) && (
              <Switch
                checked={!!item.enabled}
                disabled={busy === item.id}
                onChange={() => void toggleEnabled(item)}
                aria-label={`${item.displayName} 启用状态`}
              />
            )}
          </>
        ) : (
          <button
            type="button"
            className="ui-icon-btn size-xs market-op-add"
            aria-label={`安装 ${item.displayName}`}
            disabled={busy === item.id}
            onClick={() => void installItem(item)}
          >
            <IconPlus size={15} />
          </button>
        )}
      </span>
    </div>
  );

  return (
    <div className="market-panel">
      {/* 顶部固定：标签 + 搜索 + 已安装筛选 + 主动作 */}
      <div className="market-topbar">
        <div className="market-tabs" role="tablist">
          {KINDS.map((k) => (
            <button
              key={k.key}
              role="tab"
              aria-selected={kind === k.key}
              className={"market-tab" + (kind === k.key ? " active" : "")}
              onClick={() => switchKind(k.key)}
            >
              {k.label}
            </button>
          ))}
        </div>
        <div className="market-topbar-right">
          {/* plan-284-1451：刷新入口（点击图标转动并重拉当前查询） */}
          <button
            type="button"
            className={"market-refresh" + (refreshing ? " is-spinning" : "")}
            aria-label="刷新市场内容"
            disabled={refreshing}
            onClick={() => void refresh()}
          >
            <IconRefresh size={15} />
          </button>
          <div className="market-search">
            <IconSearch size={14} />
            <input
              placeholder="搜索名称、说明或标签"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="搜索市场"
            />
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => {
              // plan-41-226：按用户要求——「已安装」跳转到设置页的扩展管理界面。
              // 市场页只负责发现与获取，管理集中在设置页（避免两处都能管理造成职责重叠）。
              // plan-284-1452：按钮不再显示个数（用户要求"右上角已安装不用展示个数"）。
              window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "extensions" } }));
            }}
          >
            已安装
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setDialog(kind)}>
            <IconPlus size={13} />
            {kind === "plugin" ? "安装插件" : kind === "skill" ? "导入技能" : "添加连接器"}
          </button>
        </div>
      </div>

      {/* 内容区独立滚动（头部固定；滚到底部自动加载下一页） */}
      <div className="market-scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="market-inner">
        <div className="market-hero">
          <div className="market-hero-text">
            <h2 className="market-hero-title">
              发现 {current.icon}
              {/* plan-284-1450：品类名用所属主色 + 字距微调，作为页面身份标识 */}
              <span className="market-hero-noun" style={{ color: KIND_ACCENT[kind].strong }}>
                {current.noun}
              </span>
              {/* plan-59-286：规模徽标挂在标题行末尾（不再混进筛选区）——
                  它是品类体量的一部分，与"发现什么、有多少"这句语义同处一行；
                  底色与数字色取品类主色，和品类名同源，不额外引入第三种颜色。 */}
              {total > 0 && (
                <span
                  className="market-hero-total"
                  style={{ background: KIND_ACCENT[kind].soft, color: KIND_ACCENT[kind].strong }}
                >
                  {total > 999 ? `${Math.round(total / 1000)}k` : total.toLocaleString()} 个
                </span>
              )}
            </h2>
            {/* key=kind：切标签时重挂载副标题，使手绘线动画随之重播 */}
            <p className="market-hero-sub" key={kind}>
              {renderHero(current)}
            </p>
            {degraded && (
              <div className="market-degraded">网络暂不可用，以下为本机已缓存/已安装内容，联网后自动更新</div>
            )}
          </div>
          <div className="market-hero-art" aria-hidden>
            {/* 装饰卡片组（plan-41-226）：三类各自一组造型卡片（花形/方块/圆形 + 品类图标），
                切标签时整组重挂载以重播「零散聚拢」入场动画。 */}
            <MarketDecor kind={kind} />
          </div>
        </div>

        <div className="market-filters">
          <div className="market-cats">
            {/* 分类来自远端 facets（code 透传远端，覆盖全库筛选）；精选为本地开关 */}
            <button
              type="button"
              className={"market-cat" + (category === "" ? " on" : "")}
              onClick={() => setCategory("")}
            >全部</button>
            <button
              type="button"
              className={"market-cat" + (category === "__featured__" ? " on" : "")}
              onClick={() => setCategory("__featured__")}
            >精选</button>
            {categories.map((c) => (
              <button
                key={c.code || c.label}
                type="button"
                className={"market-cat" + (category === c.code ? " on" : "")}
                onClick={() => setCategory(c.code)}
              >
                {c.label}
                {c.count > 0 && <span className="market-cat-count">{c.count > 999 ? `${Math.round(c.count / 1000)}k` : c.count}</span>}
              </button>
            ))}
          </div>
          {/* 筛选行只放筛选与排序——结果数不在这里（它属于"规模"，见标题行徽标） */}
          <div className="market-sort">
            <button type="button" className={sort === "hot" ? "on" : ""} onClick={() => setSort("hot")}>热门</button>
            <button type="button" className={sort === "new" ? "on" : ""} onClick={() => setSort("new")}>最新</button>
          </div>
        </div>

        {loading && <div className="market-empty">正在加载市场条目…</div>}
        {!loading && visible.length === 0 && (
          <div className="market-empty">
            {`没有匹配「${query || "当前筛选"}」的${current.label}`}
          </div>
        )}
        {/* 行式列表：连接器页按「内置 / 本机已安装 / 市场精选」分组渲染；
            技能与插件页不分段（用户要求），已安装条目靠置顶表达。 */}
        {sections.map((sec) => (
          <section className="market-section" key={sec.key}>
            {showGroupTitles && sec.label && (
              <div className="market-group-title">{sec.label}</div>
            )}
            <div className="market-rows">{sec.items.map(renderRow)}</div>
          </section>
        ))}
        {loadingMore && <div className="market-more">正在加载更多…</div>}
        {!loadingMore && !loading && page < lastPage && (
          <div className="market-more">已显示 {items.length} / {total.toLocaleString()}，继续下滑加载更多</div>
        )}
        {!loadingMore && !loading && page >= lastPage && items.length > 0 && (
          <div className="market-more">已到底部（共 {total.toLocaleString()} 条）</div>
        )}
        </div>
      </div>

      {/* 详情 */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={
          detail ? (
            <span className="market-detail-title">
              {/* plan-284-1450：详情头部用真实市场图标（缺图时自动回退首字母色块） */}
              <MarketIcon item={detail} failed={iconFailed} onFail={onIconFail} size={30} />
              <span>{detail.displayName || detail.name}</span>
              {detail.version && <span className="market-detail-ver">v{detail.version}</span>}
            </span>
          ) : (
            "详情"
          )
        }
        subtitle={detail
          ? [detail.category, detail.author, detail.downloads ? `${detail.downloads} 次获取` : null]
              .filter(Boolean).join(" · ")
          : undefined}
        width={640}
        footer={
          detail ? (
            detail.installed ? (
              <>
                {(detail.kind === "plugin" || detail.localId != null) && (
                  <button className="btn btn-ghost btn-sm" disabled={busy === detail.id}
                    onClick={() => void toggleEnabled(detail)}>
                    {detail.enabled ? "停用" : "启用"}
                  </button>
                )}
                <button className="btn btn-primary btn-sm" onClick={goManage}>前往管理 →</button>
                {(detail.kind === "plugin" || detail.localId != null) && (
                  <button className="btn btn-danger-ghost btn-sm" onClick={() => setDropTarget(detail)}>
                    <IconTrash size={12} /> {detail.kind === "plugin" ? "卸载" : "删除"}
                  </button>
                )}
              </>
            ) : (
              <button className="btn btn-primary btn-sm" disabled={busy === detail.id}
                onClick={() => void installItem(detail)}>
                <IconDownload size={12} /> {busy === detail.id ? "安装中…" : "安装到本机"}
              </button>
            )
          ) : null
        }
      >
        {detail && (
          <div className="market-detail">
            <p className="market-detail-desc">{detail.descriptionZh || detail.description || "暂无说明"}</p>
            {(detail.tags || []).length > 0 && (
              <div className="market-detail-tags">
                {(detail.tags || []).map((t) => <span key={t} className="market-tag">{t}</span>)}
              </div>
            )}
            {detail.installKind !== "installed" && (
              <div className="market-detail-note">
                安装后可在「设置 → 扩展管理」中启停或卸载。
                {detail.kind === "connector" && "连接器默认不启用，需手动开启后生效。"}
              </div>
            )}
            {/* 保留市场页面入口作为兜底（极少数条目无包可供本机安装时） */}
            {marketUrl && (
              <button className="market-detail-marketlink" onClick={() => openExternal(marketUrl)}>
                在浏览器中查看该市场
              </button>
            )}
            <div className="market-detail-facts">
              <div className="market-fact"><span className="market-fact-k">类型</span><span className="market-fact-v">{current.label}</span></div>
              <div className="market-fact"><span className="market-fact-k">分类</span><span className="market-fact-v">{detail.category}</span></div>
              {detail.source && <div className="market-fact"><span className="market-fact-k">来源</span><span className="market-fact-v">{detail.source}</span></div>}
              {(detail.skills || []).length > 0 && (
                <div className="market-fact"><span className="market-fact-k">贡献技能</span><span className="market-fact-v">{(detail.skills || []).join("、")}</span></div>
              )}
              {detail.path && (
                <div className="market-fact"><span className="market-fact-k">本机目录</span><span className="market-fact-v mono">{detail.path}</span></div>
              )}
            </div>
          </div>
        )}
      </Modal>

      {/* 主动作对话框：按类型分派到既有导入 / 安装 / 扫描通道 */}
      <ActionDialogs
        which={dialog}
        onClose={() => setDialog(null)}
        onDone={async () => { setDialog(null); await fetchPage(kind, 1, false); await useChatStore.getState().loadBootstrap(); }}
      />

      <ConfirmDialog
        open={dropTarget !== null}
        title={dropTarget?.kind === "plugin" ? "卸载插件" : dropTarget?.kind === "skill" ? "删除技能" : "删除连接器"}
        message={
          dropTarget?.kind === "plugin"
            ? `将移除插件「${dropTarget?.displayName || dropTarget?.name}」的本地文件，并注销它贡献的技能。\n\n此操作不可恢复（可重新安装）。`
            : `将从本机删除「${dropTarget?.displayName || dropTarget?.name}」。\n\n此操作不可恢复（可重新安装）。`
        }
        confirmLabel={dropTarget?.kind === "plugin" ? "卸载" : "删除"}
        danger
        onCancel={() => setDropTarget(null)}
        onConfirm={() => void doUninstall()}
      />
    </div>
  );
}

/* ── 主动作对话框（插件安装 / 技能导入 / 连接器添加）──
 * plan-41-198 S3：导出供设置页「扩展管理」复用，保证两处的导入/扫描通道完全一致。 */
export function ActionDialogs({ which, onClose, onDone }: {
  which: null | "plugin" | "skill" | "connector";
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  // 插件：目录 / Git
  const [pluginPath, setPluginPath] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  // 技能：本地导入路径与模式 + 仓库 URL + 扫描
  const [skillPath, setSkillPath] = useState("");
  const [skillMode, setSkillMode] = useState<"copy" | "link">("link");
  const [repoUrl, setRepoUrl] = useState("");
  // 连接器：JSON 文本 + 手动创建
  const [mcpJson, setMcpJson] = useState("");
  const [mcpError, setMcpError] = useState<string | null>(null);

  useEffect(() => {
    if (!which) {
      setPluginPath(""); setGitUrl(""); setSkillPath(""); setRepoUrl(""); setMcpJson(""); setMcpError(null);
    }
  }, [which]);

  if (!which) return null;

  const pickDir = async (setter: (v: string) => void) => {
    const dir = await window.chatcoderAPI?.selectDirectory?.();
    if (dir) setter(dir);
  };

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); await onDone(); }
    catch (e) { notify(String(e)); }
    finally { setBusy(false); }
  };

  const importMcpJson = async () => {
    setMcpError(null);
    const parsed = JSON.parse(mcpJson);
    const servers = parsed?.mcpServers ?? parsed;
    if (typeof servers !== "object" || servers === null) {
      throw new Error("无效的 MCP 配置：需包含 mcpServers 键或为服务器对象");
    }
    let count = 0;
    for (const [name, cfgRaw] of Object.entries(servers as Record<string, unknown>)) {
      const cfg = (cfgRaw || {}) as Record<string, unknown>;
      await api.createMcpServer({
        name: String(name),
        transport: String(cfg.type || cfg.transport || "stdio"),
        command: cfg.command ? String(cfg.command) : undefined,
        args: Array.isArray(cfg.args) ? (cfg.args as string[]) : undefined,
        env: (cfg.env && typeof cfg.env === "object") ? (cfg.env as Record<string, string>) : undefined,
        url: cfg.url ? String(cfg.url) : undefined,
        is_active: false,
      });
      count += 1;
    }
    notify(`已导入 ${count} 个连接器（默认未启用，可在扩展管理中开启）`);
  };

  if (which === "plugin") {
    return (
      <Modal
        open
        onClose={onClose}
        title="安装插件"
        subtitle="从本地目录或 Git 仓库安装；插件贡献的技能会自动并入技能列表"
        width={620}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button>
            <button className="btn btn-primary btn-sm" disabled={busy || (!pluginPath.trim() && !gitUrl.trim())}
              onClick={() => void run(async () => {
                if (gitUrl.trim()) await api.pluginInstallGit(gitUrl.trim());
                else await api.pluginInstallDir(pluginPath.trim());
                notify("插件安装完成");
              })}>
              {busy ? "安装中…" : "安装"}
            </button>
          </>
        }
      >
        <div className="market-form">
          <div className="market-form-row">
            <label>本地目录（需包含 plugin.json）</label>
            <div className="market-form-inline">
              <Input placeholder="例如 D:\\plugins\\my-plugin" value={pluginPath}
                onChange={(e) => { setPluginPath(e.target.value); setGitUrl(""); }} />
              <button className="btn btn-ghost btn-sm" onClick={() => void pickDir(setPluginPath)}>
                <IconFolder size={13} /> 选择
              </button>
            </div>
          </div>
          <div className="market-form-row">
            <label>或 Git 仓库地址</label>
            <Input placeholder="例如 https://github.com/user/plugin.git" value={gitUrl}
              onChange={(e) => { setGitUrl(e.target.value); setPluginPath(""); }} />
          </div>
        </div>
      </Modal>
    );
  }

  if (which === "skill") {
    return (
      <Modal
        open
        onClose={onClose}
        title="导入技能"
        subtitle="支持本地目录 / 单个 Markdown、云端技能仓库（Git），以及扫描本机已装技能"
        width={620}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button>
            <button className="btn btn-primary btn-sm" disabled={busy || (!skillPath.trim() && !repoUrl.trim())}
              onClick={() => void run(async () => {
                if (repoUrl.trim()) {
                  await api.createSkillRepo({ url: repoUrl.trim() });
                  notify("技能仓库已添加，可在「设置 → 扩展管理」中同步并导入其技能");
                } else {
                  const res = await api.importLocalSkill({ path: skillPath.trim(), mode: skillMode });
                  notify(`已导入 ${res.count} 个技能${res.skipped.length ? `（跳过重名 ${res.skipped.length} 个）` : ""}`);
                }
              })}>
              {busy ? "处理中…" : "导入"}
            </button>
          </>
        }
      >
        <div className="market-form">
          <div className="market-form-row">
            <label>本地路径（目录或 .md 文件）</label>
            <div className="market-form-inline">
              <Input placeholder="例如 D:\\skills 或 D:\\skills\\review.md" value={skillPath}
                onChange={(e) => { setSkillPath(e.target.value); setRepoUrl(""); }} />
              <button className="btn btn-ghost btn-sm" onClick={() => void pickDir(setSkillPath)}>
                <IconFolder size={13} /> 选择
              </button>
            </div>
          </div>
          <div className="market-form-row">
            <label>导入方式</label>
            <div className="market-chips">
              <button type="button" className={"market-chip" + (skillMode === "link" ? " on" : "")}
                onClick={() => setSkillMode("link")}>链接（不复制文件）</button>
              <button type="button" className={"market-chip" + (skillMode === "copy" ? " on" : "")}
                onClick={() => setSkillMode("copy")}>复制到应用目录</button>
            </div>
          </div>
          <div className="market-form-row">
            <label>或云端技能仓库（Git 地址）</label>
            <Input placeholder="例如 https://github.com/user/skills.git" value={repoUrl}
              onChange={(e) => { setRepoUrl(e.target.value); setSkillPath(""); }} />
          </div>
          <div className="market-form-actions">
            <button className="btn btn-ghost btn-sm" disabled={busy}
              onClick={() => void run(async () => {
                const res = await api.scanSkills();
                notify(`已扫描本机技能目录（新增 ${res.added ?? 0} 个）`);
              })}>
              <IconRefresh size={13} /> 扫描本机技能目录
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  // connector
  return (
    <Modal
      open
      onClose={onClose}
      title="添加连接器"
      subtitle="支持粘贴标准 mcpServers JSON、扫描本机已配置的 MCP 服务器"
      width={640}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button>
          <button className="btn btn-primary btn-sm" disabled={busy || !mcpJson.trim()}
            onClick={() => void run(async () => {
              try { await importMcpJson(); }
              catch (e) { setMcpError(String(e)); }
            })}>
            {busy ? "导入中…" : "确认导入"}
          </button>
        </>
      }
    >
      <div className="market-form">
        <div className="market-form-row">
          <label>MCP JSON 配置</label>
          <Textarea rows={8} className="market-json" placeholder={'{\n  "mcpServers": {\n    "server-name": { "command": "npx", "args": ["-y", "some-mcp"] }\n  }\n}'}
            value={mcpJson} onChange={(e) => setMcpJson(e.target.value)} aria-label="MCP JSON 配置" />
          {mcpError && <div className="market-form-error">{mcpError}</div>}
        </div>
        <div className="market-form-actions">
          <button className="btn btn-ghost btn-sm" disabled={busy}
            onClick={() => void run(async () => {
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
              notify(`已从本机配置导入 ${n} 个连接器（默认未启用）`);
            })}>
            <IconRefresh size={13} /> 扫描本机 MCP 配置
          </button>
        </div>
      </div>
    </Modal>
  );
}
