/** 文件管理面板（v2）：项目目录树 + 文件预览（monaco）+ 在外部打开。
 * v11: 变更审核 diff 视图——diffPreview.path 匹配当前预览文件时，
 * 用 Monaco DiffEditor 展示 before/after，可切换「变更对比 / 当前内容」。
 *
 * plan-20261009-ui U8：
 *  · 目录树改为**扁平化 + 虚拟滚动**（@tanstack/react-virtual，项目已有依赖）：上万个节点也只渲染可见行；
 *  · 文件 / 文件夹统一用 Material 文件类型图标（iconkit/FileIcon），取代文字徽标（TS / JS …）；
 *  · 树头：项目图标 + 名称 + 刷新 / 全部折叠；下方是过滤框（匹配的文件与其祖先目录自动展开并高亮）；
 *  · 键盘：↑↓ 移动、→ 展开 / 进入、← 折叠 / 回父级、Enter 打开、Home / End；
 *  · 预览头：路径面包屑（点目录 → 在树里定位）+ 分段切换（变更对比 / 当前内容，预览 / 源码）+ 外部打开 + 关闭；
 *  · 没有预览时树占满整个面板，打开预览后树收成侧栏（宽度带弹簧过渡）。 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { FileDiffView } from "./FileDiffView";
import { ResourceLoading } from "./ResourceLoading";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api, previewApi, type PreviewSource, type TreeNode } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { bucketKeyOf, usePanelStore } from "../../store/panel";
import { IconArrowLeft, IconArrowToggle, IconAt, IconChevronLeft, IconChevronRight, IconCopy, IconExternalLink, IconFileSearch, IconFolderPlus, IconMoreHorizontal, IconPanelLeft, IconPlus, IconSearch, IconX, IconFolderTree } from "../icons";
import ResourcePreview from "./ResourcePreview";
import { Button, Collapse, Empty, FormDialog, IconButton, Input, Menu, Segmented, Tooltip } from "../ui";
import { PointContextMenu } from "../ui/ContextMenu";
import { ConfirmDialog } from "../ConfirmDialog";
import { FileIcon } from "../iconkit/FileIcon";
import { ProjectIcon } from "../project/ProjectIcon";
import { projectColorKey, projectIconKey } from "../project/projectMeta";
import { recordComponentRender } from "../../perf/metrics";
import { isBusy, subscribe } from "../../perf/bus";

/** 文件扩展名 -> Monaco 语言 ID 映射（后端只返回扩展名，这里兜底转换）。 */
const EXT_LANG_MAP: Record<string, string> = {
  py: "python", pyw: "python",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  java: "java", kt: "kotlin", kts: "kotlin",
  css: "css", scss: "scss", less: "less",
  html: "html", htm: "html", xml: "xml", svg: "xml",
  json: "json", jsonc: "json",
  md: "markdown", markdown: "markdown",
  sh: "shell", bash: "shell", zsh: "shell",
  yml: "yaml", yaml: "yaml",
  sql: "sql", go: "go", rs: "rust", rb: "ruby", php: "php",
  c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp",
  cs: "csharp", swift: "swift", dart: "dart",
  vue: "html", svelte: "html",
  dockerfile: "dockerfile",
};

/** 将后端返回的 language（扩展名或语言 id）归一化为 Monaco 语言 id。 */
function toMonacoLang(lang: string | null, filename: string): string {
  if (!lang) {
    const ext = filename.split(".").pop()?.toLowerCase() ?? "";
    return EXT_LANG_MAP[ext] ?? "plaintext";
  }
  const l = lang.toLowerCase();
  if (EXT_LANG_MAP[l]) return EXT_LANG_MAP[l];
  return l;
}

/** 路径比较键：统一斜杠与大小写（Windows 路径不区分大小写；预览路径可能来自智能体，大小写未必与磁盘一致） */
const pathKey = (p: string): string => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();

/** plan-20261121-file-tree-settings：浅层响应只更新所读目录，未预读的后代沿用已加载内容。 */
function replaceChildren(nodes: TreeNode[], path: string, children: TreeNode[]): TreeNode[] {
  const key = pathKey(path);
  return nodes.map((node) => {
    if (pathKey(node.path) === key) {
      const cached = new Map(node.children?.map((child) => [pathKey(child.path), child.children]));
      return {
        ...node,
        children: children.map((child) => child.type === "dir" && child.children == null
          ? { ...child, children: cached.get(pathKey(child.path)) }
          : child),
      };
    }
    if (!node.children?.length) return node;
    const updated = replaceChildren(node.children, path, children);
    return updated.some((child, index) => child !== node.children?.[index]) ? { ...node, children: updated } : node;
  });
}

/** 刷新提交前补齐已展开但未预读的分支，避免短暂用残缺的树替换当前视图。 */
async function fillExpandedDirectories(
  root: TreeNode,
  expanded: ReadonlySet<string>,
  read: (path: string) => Promise<TreeNode[]>,
  isCurrent: () => boolean,
  skipped: Set<string>,
) {
  const queue = [root];
  for (let next = 0; next < queue.length && isCurrent();) {
    const batch = queue.slice(next, next + 6);
    next += batch.length;
    await Promise.all(batch.map(async (node) => {
      const key = pathKey(node.path);
      if (node.type !== "dir" || !expanded.has(key)) return;
      if (node.children == null && !skipped.has(key)) {
        try { node.children = await read(node.path); }
        catch { skipped.add(key); }
      }
      for (const child of node.children ?? []) {
        if (child.type === "dir" && expanded.has(pathKey(child.path))) queue.push(child);
      }
    }));
  }
}

/** 项目内相对路径：预览路径可能是绝对路径（目录树点开）、也可能是相对路径（任务摘要 / 引用点开），统一成相对项目根的形式 */
function relativeTo(root: string, p: string): string {
  const norm = p.replace(/\\/g, "/").replace(/^\.\//, "");
  const r = root.replace(/\\/g, "/").replace(/\/+$/, "");
  if (r && norm.toLowerCase().startsWith(r.toLowerCase() + "/")) return norm.slice(r.length + 1);
  return norm;
}

/** 相对路径 → 目录树节点路径（树节点路径是后端给的绝对路径） */
function toNodePath(root: string, rel: string): string {
  if (/^([A-Za-z]:\/|\/)/.test(rel)) return rel;
  const r = root.replace(/\\/g, "/").replace(/\/+$/, "");
  return r ? `${r}/${rel}` : rel;
}

/** 文件的所有祖先目录（节点路径），用于展开定位 */
function ancestorNodePaths(root: string, rel: string): string[] {
  const parts = rel.split("/").filter(Boolean);
  const out: string[] = [];
  for (let i = 1; i < parts.length; i++) out.push(toNodePath(root, parts.slice(0, i).join("/")));
  return out;
}

/** 扁平化后的一行（虚拟滚动的最小单位） */
interface FlatRow {
  node: TreeNode;
  depth: number;
  open: boolean;
  /** 过滤命中的字符区间（用于高亮） */
  hit: [number, number] | null;
}

const ROW_H = 28;

/** 面板窄于此值时，目录树与预览不再并排（各 200px 上下的代码预览根本放不下）：打开预览就占满面板，用「返回」回到目录树 */
const NARROW_PX = 540;
/** 面板宽于此值（典型为全屏）时，目录树即使没有预览也只占侧栏宽度，不让文件名横跨整个屏幕 */
const WIDE_PX = 960;
/** 打开预览时目录树是否收起（plan-20261016-ux 问题 10：按需腾出预览空间；记住用户选择） */
const TREE_COLLAPSED_KEY = "chatcoder.ft.treeCollapsed";
function loadTreeCollapsed(): boolean {
  try { return localStorage.getItem(TREE_COLLAPSED_KEY) === "1"; } catch { return false; }
}

/** 把目录树按展开状态（及过滤词）压平成行列表。
 *  过滤：文件名命中的文件、含命中后代的目录保留；有命中后代的目录强制展开；
 *  仅目录名自己命中时按用户的展开状态决定是否展示其子项。 */
function flatten(nodes: TreeNode[], openPaths: Set<string>, query: string): FlatRow[] {
  const out: FlatRow[] = [];
  const q = query.trim().toLowerCase();
  if (!q) {
    const walk = (list: TreeNode[], depth: number) => {
      for (const n of list) {
        const isDir = n.type === "dir";
        const open = isDir && openPaths.has(pathKey(n.path));
        out.push({ node: n, depth, open, hit: null });
        if (open && n.children) walk(n.children, depth + 1);
      }
    };
    walk(nodes, 0);
    return out;
  }
  const rangeOf = (name: string): [number, number] | null => {
    const i = name.toLowerCase().indexOf(q);
    return i >= 0 ? [i, i + q.length] : null;
  };
  const walkFiltered = (list: TreeNode[], depth: number): FlatRow[] => {
    const rows: FlatRow[] = [];
    for (const n of list) {
      const hit = rangeOf(n.name);
      if (n.type !== "dir") {
        if (hit) rows.push({ node: n, depth, open: false, hit });
        continue;
      }
      const child = walkFiltered(n.children ?? [], depth + 1);
      if (child.length > 0) {
        rows.push({ node: n, depth, open: true, hit }, ...child);
      } else if (hit) {
        const open = openPaths.has(pathKey(n.path));
        rows.push({ node: n, depth, open, hit });
        if (open && n.children) {
          const all = (l: TreeNode[], d: number) => {
            for (const c of l) {
              const o = c.type === "dir" && openPaths.has(pathKey(c.path));
              rows.push({ node: c, depth: d, open: o, hit: null });
              if (o && c.children) all(c.children, d + 1);
            }
          };
          all(n.children, depth + 1);
        }
      }
    }
    return rows;
  };
  return walkFiltered(nodes, 0);
}

function Hit({ text, range }: { text: string; range: [number, number] | null }) {
  if (!range) return <>{text}</>;
  return <>{text.slice(0, range[0])}<mark className="ft-mark">{text.slice(range[0], range[1])}</mark>{text.slice(range[1])}</>;
}

export function FileTreePanel({ visible = true, sessionId = null }: { visible?: boolean; sessionId?: number | null }) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("filePanel");

  const session = useChatStore((s) => s.sessions.find(x => x.id === sessionId));
  const currentProjectId = session?.project_id ?? null;
  const projects = useChatStore((s) => s.projects);
  const previewRequestId = usePanelStore((s) => s.buckets[bucketKeyOf(sessionId)]?.previewRequestId ?? 0);
  const previewTarget = usePanelStore((s) => s.buckets[bucketKeyOf(sessionId)]?.previewTarget ?? null);
  const previewPath = usePanelStore((s) => s.buckets[bucketKeyOf(sessionId)]?.previewPath ?? null);
  const previewLine = usePanelStore((s) => s.buckets[bucketKeyOf(sessionId)]?.previewLine ?? null);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const turnFileReview = usePanelStore(s => s.buckets[bucketKeyOf(sessionId)]?.turnFileReview ?? null);
  const diffPreview = usePanelStore((s) => s.buckets[bucketKeyOf(sessionId)]?.diffPreview ?? null);
  // 按本实例所属会话桶读取「计划查看后收起目录树」请求（各会话的文件面板常驻挂载，不能共用投影值）
  const planCollapsePending = usePanelStore((s) => s.buckets[bucketKeyOf(sessionId)]?.planCollapsePending ?? false);
  const turnChanges = useChatStore((s) => s.turnChanges);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [openPaths, updateOpenPaths] = useState<Set<string>>(new Set());
  const openPathsRef = useRef(openPaths);
  const setOpenPaths = (value: Set<string> | ((paths: Set<string>) => Set<string>)) => {
    const next = typeof value === "function" ? value(openPathsRef.current) : value;
    openPathsRef.current = next;
    updateOpenPaths(next);
  };
  const [filter, setFilter] = useState("");
  const [showHidden, setShowHidden] = useState(false);
  const showHiddenRef = useRef(showHidden);
  showHiddenRef.current = showHidden;
  const [loadingTree, setLoadingTree] = useState(true);
  const [searchMode, setSearchMode] = useState<"tree" | "path" | "content">("tree");
  const [extension, setExtension] = useState("");
  const [searchResults, setSearchResults] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [gitMarks, setGitMarks] = useState<Map<string, string>>(new Map());
  const [context, setContext] = useState<{ x: number; y: number; node: TreeNode } | null>(null);
  const [create, setCreate] = useState<{ parent: string; type: "file" | "dir" } | null>(null);
  const [rename, setRename] = useState<TreeNode | null>(null);
  const [deleting, setDeleting] = useState<TreeNode | null>(null);
  const [editValue, setEditValue] = useState("");
  const [editError, setEditError] = useState("");
  const [mutating, setMutating] = useState(false);
  const mutationLock = useRef(false);
  /** 键盘光标（路径）：与「当前预览文件」是两回事——可以先移动再回车打开 */
  const [cursor, setCursor] = useState<string | null>(null);
  const contentLang = toMonacoLang(null, previewPath ?? "");
  const [contentError, setContentError] = useState<string | null>(null);
  useEffect(()=>setContentError(null),[previewPath,previewTarget]);
  // v11: 变更对比 / 当前内容 视图切换（仅当 diff 与当前文件匹配时生效）
  const [diffLayout, setDiffLayout] = useState("side");
  const [viewMode, setViewMode] = useState<"diff" | "content">("diff");
  // v17: Markdown 文件支持 预览/源码 切换（默认预览渲染）
  const panelRef = useRef<HTMLDivElement>(null);
  const crumbsRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  const [treeCollapsed, setTreeCollapsed] = useState(loadTreeCollapsed);
  // 计划模式查看完整计划：目录树自动收起，给计划正文腾出宽度。只改本次状态，不覆盖用户记住的偏好；
  // 用户仍可用按钮手动展开。请求只在面板可见时消费（打开计划时文件标签通常是随后才激活的）。
  useEffect(() => {
    if (!visible || !planCollapsePending) return;
    setTreeCollapsed(true);
    usePanelStore.getState().ackPlanCollapse();
  }, [visible, planCollapsePending]);
  const toggleTree = () => setTreeCollapsed((v) => {
    const next = !v;
    try { localStorage.setItem(TREE_COLLAPSED_KEY, next ? "1" : "0"); } catch { /* 存储不可用时只在本次生效 */ }
    return next;
  });
  const treeRef = useRef<HTMLDivElement>(null);
  /** 待定位的路径：展开祖先目录后，等行列表更新再滚动到它 */
  const pendingReveal = useRef<string | null>(null);

  const project = projects.find((p) => p.id === currentProjectId);
  const projectRoot = session?.worktree_path || project?.path || "";
  const workspaceKey = JSON.stringify([currentProjectId, projectRoot, sessionId]);
  const workspaceRef = useRef(workspaceKey);
  workspaceRef.current = workspaceKey;
  const projectSource = (path: string): Extract<PreviewSource,{source:"project"}> | null => sessionId != null && currentProjectId != null ? {source:"project", session_id:sessionId, project_id:currentProjectId, path} : null;
  const resourceSource = previewTarget ?? (previewPath ? projectSource(previewPath) : null);
  /** 预览文件的相对路径 / 树节点路径 / 比较键 */
  const previewRel = previewPath ? relativeTo(projectRoot, previewPath) : "";
  const previewNode = previewPath ? toNodePath(projectRoot, previewRel) : "";
  const previewKey = previewNode ? pathKey(previewNode) : "";

  // 面板宽度分档（只在跨过阈值时更新状态，拖分隔条逐帧改宽度不会逐帧重渲染）
  // plan-20261016-ux（问题 10）：宽面板（全屏）档——没有预览时目录树也收成侧栏，右侧给空态说明
  const [wide, setWide] = useState(false);
  // plan-20261017-goal12 项 8：拖拽 / 窗口运动期间不切换布局档（跨阈值时目录树与空态会整块跳变），
  // 记下最新宽度，松手后由 PerfBus 通知统一落档，让 CSS 过渡一次性平滑完成。
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    let pendingWidth: number | null = null;
    const apply = (w: number) => {
      setNarrow(w < NARROW_PX);
      setWide(w >= WIDE_PX);
    };
    const ro = new ResizeObserver(([entry]) => {
      const w = entry.contentRect.width;
      if (isBusy()) { pendingWidth = w; return; }
      pendingWidth = null;
      apply(w);
    });
    ro.observe(el);
    const off = subscribe((busy) => {
      if (busy || pendingWidth == null) return;
      apply(pendingWidth);
      pendingWidth = null;
    });
    return () => { ro.disconnect(); off(); };
  }, []);

  /** plan-75-334 阶段2：文件加载依赖从**整个 turnChanges** 收窄为「当前预览路径的变更签名」。
   *  此前任意 turn 的任意文件变化都会重读当前预览文件并刷新编辑器内容；
   *  现在只有「当前预览路径确实发生变更」时才触发加载。
   *
   *  签名取命中文件的**变更元数据**（action/additions/deletions）而非仅位置：
   *  同一文件在同一个 turn 中被二次修改（行数变化）也会换签名 → 重新读盘，
   *  保持「文件落盘后重新读取真实磁盘内容」的语义；其它文件变化则签名不变、不读盘。 */
  const previewChangeSignature = useMemo(() => {
    if (!previewPath) return "";
    const norm = previewPath.replace(/\\/g, "/");
    const parts: string[] = [];
    for (const key of Object.keys(turnChanges)) {
      for (const c of turnChanges[Number(key)] ?? []) {
        if (String(c?.path ?? "").replace(/\\/g, "/") !== norm) continue;
        parts.push(`${key}:${c.action}:${c.additions}:${c.deletions}`);
      }
    }
    return parts.join("|");
  }, [turnChanges, previewPath]);

  // v11: 当前是否处于 diff 视图（diffPreview 与预览文件匹配）
  const hasDiff = diffPreview != null && diffPreview.path === previewPath;
  /** 本轮被删除的文件：磁盘上已没有，只能看变更对比 */
  const isDeletedDiff = hasDiff && diffPreview!.deleted === true;
  const showDiff = (turnFileReview ? turnFileReview.mode === "diff" : viewMode === "diff" || isDeletedDiff) && hasDiff;

  /** plan-20261018-turn-changes: diff 的 model 唯一键。
   *  连续点不同轮次的「审查」时，@monaco-editor/react 会复用同一 model URI；
   *  旧 model 在卸载时被 dispose，新实例又用同一 URI，于是抛
   *  "TextModel got disposed before DiffEditorWidget model got reset"。
   *  用「文件 + 两侧内容规模」生成唯一键，让每次不同的 diff 各自持有独立 model。 */
  const diffModelKey = diffPreview
    ? `${diffPreview.path}#${(diffPreview.before ?? "").length}-${(diffPreview.after ?? "").length}`
    : "";

  /** plan-20261018-turn-changes: Monaco 的 dispose/create 竞态规避。
   *  @monaco-editor/react 在 React 卸载阶段 dispose 旧 TextModel，而同一批 effect
   *  里新实例又 createModel/setModel，monaco 0.55 会抛
   *  "TextModel got disposed before DiffEditorWidget model got reset"（仅控制台报错，
   *  但会污染日志）。这里先让旧实例卸载，隔一帧再挂载新实例，把两步彻底错开。 */
  const [mountedDiffKey, setMountedDiffKey] = useState("");
  useEffect(() => {
    if (!showDiff || !diffModelKey) {
      setMountedDiffKey("");
      return;
    }
    if (mountedDiffKey === diffModelKey) return;
    setMountedDiffKey("");                       // 第一步：卸载旧实例
    const timer = window.setTimeout(() => setMountedDiffKey(diffModelKey), 60);  // 第二步：延迟挂载
    return () => window.clearTimeout(timer);
    // 刻意不依赖 mountedDiffKey：否则 setMountedDiffKey("") 会再次触发本 effect，
    // 定时器被反复清理，diff 永远挂不上。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showDiff, diffModelKey]);

  // v11: 新 diff 到达时默认回到「变更对比」视图（用户手动切换「当前内容」不受影响）
  useEffect(() => {
    if (diffPreview != null && diffPreview.path === previewPath) {
      setViewMode("diff");
    }
  }, [diffPreview, previewPath]);

  const treeGeneration = useRef(0);
  const refreshRequest = useRef<AbortController | null>(null);
  const loadedScope = useRef("");
  const loadChildren = async (path: string) => {
    const source = projectSource(path);
    if (!source) return;
    const generation = treeGeneration.current;
    const isCurrent = () => generation === treeGeneration.current;
    const hidden = showHiddenRef.current;
    const read = async (dir: string) => (await previewApi.children({ ...source, path: dir }, undefined, 1, hidden)).children;
    try {
      const children = await read(path);
      if (!isCurrent()) return;
      const branch: TreeNode = { name: "", path, type: "dir", children };
      const skipped = new Set<string>();
      await fillExpandedDirectories(branch, openPathsRef.current, read, isCurrent, skipped);
      if (!isCurrent()) return;
      setTree((old) => replaceChildren(old, path, children));
      if (skipped.size) setContentError("部分子目录暂时无法读取，可折叠后重新打开。");
    } catch (error) {
      if (isCurrent()) setContentError(String(error));
    }
  };
  const loadTree = async () => {
    if (workspaceRef.current !== workspaceKey) return;
    setContentError(null);
    const generation = ++treeGeneration.current;
    refreshRequest.current?.abort();
    const source=projectSource("");
    if (!source) { setTree([]); setLoadingTree(false); return; }
    const controller = new AbortController();
    refreshRequest.current = controller;
    const hidden = showHiddenRef.current;
    const isCurrent = () => workspaceRef.current === workspaceKey &&
      generation === treeGeneration.current && !controller.signal.aborted;
    setLoadingTree(true);
    try {
      const data = await previewApi.children(source, controller.signal, 8, hidden);
      if (!isCurrent()) return;
      const root: TreeNode = { name: project?.name ?? data.path, path: data.path, type: "dir", children: data.children };
      if (tree.length === 0) setOpenPaths((old) => new Set([...old, pathKey(data.path)]));
      const read = async (dir: string) => (await previewApi.children({ ...source, path: dir }, controller.signal, 1, hidden)).children;
      const skipped = new Set<string>();
      let expanded: Set<string>;
      do {
        expanded = openPathsRef.current;
        await fillExpandedDirectories(root, expanded, read, isCurrent, skipped);
      } while (isCurrent() && expanded !== openPathsRef.current);
      if (!isCurrent()) return;
      setTree(data.children.length ? [root] : []);
      if (skipped.size) setContentError("部分目录暂时无法读取，可折叠后重新打开。");
    } catch (e) {
      if (isCurrent()) setContentError(String(e));
    } finally {
      if (isCurrent()) {
        refreshRequest.current = null;
        setLoadingTree(false);
      }
    }
  };
  useEffect(() => {
    setTree([]); setOpenPaths(new Set()); setCursor(null); setContext(null); setCreate(null); setRename(null); setDeleting(null);
    return () => { refreshRequest.current?.abort(); treeGeneration.current++; loadedScope.current = ""; };
  }, [currentProjectId, projectRoot, sessionId]);
  useEffect(() => {
    const scope = `${workspaceKey}:${showHidden}`;
    if (!visible || loadedScope.current === scope) return;
    loadedScope.current = scope;
    void loadTree();
  }, [visible, workspaceKey, showHidden]);

  useEffect(() => {
    setSearchResults([]);
    setSearchError("");
    if (!visible || currentProjectId == null || searchMode === "tree" || !filter.trim()) {
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    const timer = window.setTimeout(() => {
      void api.projectFileSearch(currentProjectId, filter, 100, showHidden, {
        sessionId, mode: searchMode, extension, signal: controller.signal,
      }).then((paths) => {
        if (!controller.signal.aborted) setSearchResults(paths);
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setSearchError(String(error));
      }).finally(() => {
        if (!controller.signal.aborted) setSearching(false);
      });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [visible, currentProjectId, sessionId, showHidden, searchMode, filter, extension]);

  const refreshMarks = async (stillCurrent: () => boolean) => {
    if (currentProjectId == null) return;
    try {
      const status = await api.gitStatusAll(currentProjectId, { sessionId });
      const marks = new Map<string, string>();
      for (const group of [status.staged, status.unstaged, status.untracked, status.conflicts]) {
        for (const file of group) {
          const rel = file.path.replace(/\\/g, "/");
          marks.set(pathKey(toNodePath(projectRoot, rel)), file.code === "??" ? "U" : file.code.slice(0, 1));
          for (const parent of ancestorNodePaths(projectRoot, rel)) marks.set(pathKey(parent), "•");
        }
      }
      if (stillCurrent()) setGitMarks(marks);
    } catch {
      if (stillCurrent()) setGitMarks(new Map());
    }
  };
  useEffect(() => {
    if (!visible) return;
    let current = true;
    const refresh = () => void refreshMarks(() => current);
    refresh();
    const timer = window.setInterval(refresh, 10000);
    return () => { current = false; window.clearInterval(timer); };
  }, [visible, currentProjectId, projectRoot, sessionId, turnChanges]);

  useEffect(() => {
    if (!previewPath) return;
    const dirs = [projectRoot, ...ancestorNodePaths(projectRoot, previewRel)].map(pathKey);
    pendingReveal.current = previewNode;
    setCursor(previewNode);
    if (dirs.length === 0) return;
    setOpenPaths((prev) => {
      const next = new Set(prev);
      let changed = false;
      for (const d of dirs) { if (!next.has(d)) { next.add(d); changed = true; } }
      return changed ? next : prev;
    });
  }, [previewPath, projectRoot]);

  const openExternal = (relOrAbsPath: string) => {
    if (!relOrAbsPath) return;
    let full = relOrAbsPath;
    if (projectRoot) {
      const pRoot = projectRoot.replace(/\\/g, "/").replace(/\/$/, "");
      const cleanRel = relOrAbsPath.replace(/\\/g, "/").replace(/^\.\//, "");
      if (!cleanRel.startsWith(pRoot) && !/^[A-Za-z]:\//.test(cleanRel) && !cleanRel.startsWith("/")) {
        full = `${pRoot}/${cleanRel}`;
      }
    }
    window.chatcoderAPI?.openPath?.(full);
  };

  /* ── 虚拟化的目录树 ── */
  const remoteSearch = searchMode !== "tree" && Boolean(filter.trim());
  const rows = useMemo<FlatRow[]>(() => remoteSearch ? searchResults.map((rel) => ({
    node: { path: toNodePath(projectRoot, rel), name: rel, type: "file" },
    depth: 0, open: false, hit: null,
  })) : flatten(tree, openPaths, filter), [tree, openPaths, filter, remoteSearch, searchResults, projectRoot]);
  const indexByPath = useMemo(() => {
    const m = new Map<string, number>();
    rows.forEach((r, i) => m.set(pathKey(r.node.path), i));
    return m;
  }, [rows]);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => treeRef.current,
    estimateSize: () => ROW_H,
    overscan: 14,
  });

  // 行列表更新后兑现待定位请求（展开祖先目录是异步的，不能在同一拍里 scrollToIndex）
  useEffect(() => {
    const target = pendingReveal.current;
    if (!target) return;
    const i = indexByPath.get(pathKey(target));
    if (i == null) return;
    pendingReveal.current = null;
    virtualizer.scrollToIndex(i, { align: "auto" });
  }, [indexByPath, virtualizer]);

  /** plan-20261019-motion-sweep：目录展开 / 收起的过渡窗口。
   *  虚拟列表的行是绝对定位 + transform，展开时下方的行整体位移、新露出的行凭空出现——此前全是瞬切。
   *  这里只在「用户点开 / 收起目录」的这一小段时间里：① 给行的位移挂过渡（.is-toggling）；
   *  ② 给新露出的后代行入场动画（revealDirPath 标出是哪个目录刚被展开）。
   *  过滤输入、滚动、窗口缩放引起的行位置变化不在窗口内，不会有拖泥带水的滑动。 */
  const [toggling, setToggling] = useState(false);
  const [revealDirPath, setRevealDirPath] = useState<string | null>(null);
  const toggleTimer = useRef<number | null>(null);
  useEffect(() => () => {
    if (toggleTimer.current != null) window.clearTimeout(toggleTimer.current);
  }, []);
  const toggleDir = (path: string) => {
    const key = pathKey(path);
    const opening = !openPathsRef.current.has(key);
    setOpenPaths((prev) => {
      const next = new Set(prev);
      if (opening) next.add(key);
      else next.delete(key);
      return next;
    });
    if (opening) void loadChildren(path);
    setToggling(true);
    setRevealDirPath(opening ? path : null);
    if (toggleTimer.current != null) window.clearTimeout(toggleTimer.current);
    // 200ms 位移 + 最多 12 行 × 16ms 的错峰，留出余量再复位
    toggleTimer.current = window.setTimeout(() => {
      toggleTimer.current = null;
      setToggling(false);
      setRevealDirPath(null);
    }, 480);
  };
  /** 刚被展开的目录的后代行在 rows 里的下标区间（含端点）；没有进行中的展开则为 null */
  const revealRange = useMemo(() => {
    if (revealDirPath == null) return null;
    const di = indexByPath.get(pathKey(revealDirPath));
    if (di == null) return null;
    const base = rows[di].depth;
    let end = di;
    while (end + 1 < rows.length && rows[end + 1].depth > base) end += 1;
    return end > di ? ([di + 1, end] as const) : null;
  }, [revealDirPath, rows, indexByPath]);

  const activate = (row: FlatRow) => {
    setCursor(row.node.path);
    if (row.node.type === "dir") toggleDir(row.node.path);
    else {
      if (remoteSearch) void revealNode(row.node.path);
      setPreviewPath(row.node.path);
    }
  };

  const onTreeKeyDown = (e: React.KeyboardEvent) => {
    if (rows.length === 0) return;
    const cur = cursor != null ? indexByPath.get(pathKey(cursor)) ?? -1 : -1;
    const go = (i: number) => {
      const next = Math.max(0, Math.min(rows.length - 1, i));
      setCursor(rows[next].node.path);
      virtualizer.scrollToIndex(next, { align: "auto" });
    };
    switch (e.key) {
      case "F2":
        if (rows[cur] && canModify(rows[cur].node)) { e.preventDefault(); startRename(rows[cur].node); }
        break;
      case "ContextMenu":
      case "F10": {
        if (e.key === "F10" && !e.shiftKey) break;
        const rect = treeRef.current?.getBoundingClientRect();
        if (rows[cur] && rect) { e.preventDefault(); setContext({ x: rect.left + 12, y: rect.top + 14, node: rows[cur].node }); }
        break;
      }
      case "ArrowDown": e.preventDefault(); go(cur + 1); break;
      case "ArrowUp": e.preventDefault(); go(cur < 0 ? 0 : cur - 1); break;
      case "Home": e.preventDefault(); go(0); break;
      case "End": e.preventDefault(); go(rows.length - 1); break;
      case "ArrowRight": {
        const r = rows[cur];
        if (!r || r.node.type !== "dir") break;
        e.preventDefault();
        if (!r.open) toggleDir(r.node.path);
        else if (rows[cur + 1] && rows[cur + 1].depth > r.depth) go(cur + 1);
        break;
      }
      case "ArrowLeft": {
        const r = rows[cur];
        if (!r) break;
        e.preventDefault();
        if (r.node.type === "dir" && r.open && !filter.trim()) { toggleDir(r.node.path); break; }
        for (let i = cur - 1; i >= 0; i--) if (rows[i].depth < r.depth) { go(i); break; }
        break;
      }
      case "Enter":
      case " ": {
        const r = rows[cur];
        if (!r) break;
        e.preventDefault();
        activate(r);
        break;
      }
      default: break;
    }
  };

  /** plan-771-3177：全库结果可能位于目录树预加载深度之外。 */
  const revealNode = async (node: string, directory = false, closePreview = false, explicit = true) => {
    const source = projectSource(node);
    if (!source || workspaceRef.current !== workspaceKey) return;
    const generation = treeGeneration.current;
    const root = projectRoot.replace(/\\/g, "/").replace(/\/+$/, "");
    const rel = relativeTo(root, node);
    const dirs = [root, ...ancestorNodePaths(root, pathKey(node) === pathKey(root) ? "" : rel)];
    if (directory && pathKey(node) !== pathKey(root)) dirs.push(node);
    try {
      const lists = await Promise.all(dirs.map((dir) => previewApi.children({ ...source, path: dir }, undefined, 1, showHiddenRef.current)));
      const panel = usePanelStore.getState();
      if (generation !== treeGeneration.current || workspaceRef.current !== workspaceKey || panel.activeBucket !== bucketKeyOf(sessionId)) return;
      if ((closePreview || !explicit) && panel.previewRequestId !== previewRequestId) return;
      setTree((old) => lists.reduce((nodes, data, index) => replaceChildren(nodes, index === 0 ? data.path : dirs[index], data.children),
        old.length ? old : [{ name: project?.name ?? root, path: lists[0]?.path ?? root, type: "dir", children: [] }]));
      if (explicit) { setFilter(""); setTreeCollapsed(false); }
      setCursor(node); pendingReveal.current = node;
      setOpenPaths((old) => new Set([...old, ...dirs.map(pathKey), ...lists.map((entry) => pathKey(entry.path))]));
      if (closePreview) setPreviewPath(null);
    } catch (error) {
      if (generation === treeGeneration.current) setContentError(String(error));
    }
  };
  useEffect(() => {
    if (!visible || !previewNode || previewTarget || refreshRequest.current || tree.length === 0 ||
      !previewKey.startsWith(pathKey(projectRoot) + "/") || indexByPath.has(previewKey) || filter.trim()) return;
    // 从 Git 或聊天打开的深层文件可能不在已预读的目录里，沿路径补齐。
    void revealNode(previewNode, false, false, false);
  }, [visible, previewRequestId, previewNode, workspaceKey]);
  const revealDirectory = (node: string) => revealNode(node, true, true);
  const revealDir = (dirRel: string) => { void revealDirectory(toNodePath(projectRoot, dirRel)); };

  const canModify = (node: TreeNode) => pathKey(node.path) !== pathKey(projectRoot)
    && !relativeTo(projectRoot, node.path).split("/").some((part) => part.toLowerCase() === ".git");
  const startCreate = (type: "file" | "dir", parent = "") => {
    setRename(null); setCreate({ parent, type }); setEditValue(""); setEditError("");
  };
  const startRename = (node: TreeNode) => {
    setCreate(null); setCursor(node.path); setRename(node);
    setEditValue(relativeTo(projectRoot, node.path).split("/").pop() ?? node.name); setEditError("");
  };
  const handleMutation = async (operation: () => Promise<void>, onError: (error: string) => void) => {
    if (mutationLock.current) return;
    mutationLock.current = true;
    setMutating(true);
    try { await operation(); }
    catch (error) { onError(error instanceof Error ? error.message : String(error)); }
    finally { mutationLock.current = false; setMutating(false); }
  };
  const parentOf = (path: string) => relativeTo(projectRoot, path).split("/").slice(0, -1).join("/");
  const selectAfterMutation = async (relative: string, directory: boolean) => {
    if (relative.split("/").some((part) => part.startsWith(".")) && !showHiddenRef.current) {
      const target = toNodePath(projectRoot, relative);
      setFilter(""); setCursor(target); pendingReveal.current = target;
      setOpenPaths((old) => new Set([...old, ...ancestorNodePaths(projectRoot, relative).map(pathKey)]));
      setShowHidden(true);
      return;
    }
    await loadTree();
    await revealNode(toNodePath(projectRoot, relative), directory);
  };
  const saveEdit = () => {
    const value = editValue.trim();
    if (currentProjectId == null || !value) return;
    setEditError("");
    void handleMutation(async () => {
      if (create) {
        const data = await api.projectFileCreate(currentProjectId, {
          path: [create.parent, value].filter(Boolean).join("/"), type: create.type, session_id: sessionId,
        });
        setCreate(null);
        await selectAfterMutation(data.relative_path, data.type === "dir");
      } else if (rename) {
        if (/[\\/]/.test(value)) throw new Error("请输入同级的新名称");
        const previous = relativeTo(projectRoot, rename.path);
        const next = [parentOf(rename.path), value].filter(Boolean).join("/");
        if (previous === next) { setRename(null); return; }
        const data = await api.projectFileRename(currentProjectId, { src: previous, dst: next, session_id: sessionId });
        const panel = usePanelStore.getState();
        if (panel.activeBucket === bucketKeyOf(sessionId) && panel.previewPath) {
          const open = relativeTo(projectRoot, panel.previewPath);
          if (open === previous || open.startsWith(previous + "/")) setPreviewPath(toNodePath(projectRoot, data.relative_path + open.slice(previous.length)));
        }
        setRename(null);
        if (rename.type === "dir") {
          const from = pathKey(rename.path);
          const to = pathKey(data.path);
          setOpenPaths((paths) => new Set(Array.from(paths, (path) =>
            path === from || path.startsWith(from + "/") ? to + path.slice(from.length) : path)));
        }
        await selectAfterMutation(data.relative_path, data.type === "dir");
      }
    }, setEditError);
  };
  const deleteEntry = () => {
    if (!deleting || currentProjectId == null) return;
    setEditError("");
    void handleMutation(async () => {
      const path = relativeTo(projectRoot, deleting.path);
      await api.projectFileDelete(currentProjectId, { path, recursive: deleting.type === "dir", session_id: sessionId });
      const panel = usePanelStore.getState();
      if (panel.activeBucket === bucketKeyOf(sessionId) && panel.previewPath) {
        const open = relativeTo(projectRoot, panel.previewPath);
        if (open === path || open.startsWith(path + "/")) setPreviewPath(null);
      }
      setDeleting(null);
      const deleted = pathKey(deleting.path);
      setOpenPaths((paths) => new Set(Array.from(paths).filter((entry) => entry !== deleted && !entry.startsWith(deleted + "/"))));
      setFilter("");
      await loadTree();
    }, setEditError);
  };
  const copyPath = (path: string) => void navigator.clipboard.writeText(path).catch((error: unknown) => setContentError(String(error)));
  const referenceFile = (path: string) => {
    window.dispatchEvent(new CustomEvent("chatcoder:composer-file-reference", {
      detail: { sessionId, path: relativeTo(projectRoot, path) },
    }));
  };
  const revealExternal = (path: string) => {
    const desktop = window.chatcoderAPI?.showItemInFolder;
    if (desktop && pathKey(path) !== pathKey(projectRoot)) {
      void desktop(path).then((result) => {
        if (result && !result.ok) setContentError(result.error || "无法在资源管理器中定位");
      }).catch((error: unknown) => setContentError(String(error)));
    } else {
      void api.openPathOnHost(path).catch((error: unknown) => setContentError(String(error)));
    }
  };
  const contextItems = context ? (() => {
    const node = context.node;
    const directory = node.type === "dir";
    const root = pathKey(node.path) === pathKey(projectRoot);
    const parent = directory ? (root ? "" : relativeTo(projectRoot, node.path)) : parentOf(node.path);
    const items = [
      { label: directory ? "展开 / 折叠" : "预览文件", onClick: () => directory ? toggleDir(node.path) : setPreviewPath(node.path) },
      { label: "复制相对路径", onClick: () => copyPath(root ? "." : relativeTo(projectRoot, node.path)) },
      { label: "复制绝对路径", onClick: () => copyPath(node.path) },
      { label: "在资源管理器中显示", onClick: () => revealExternal(node.path) },
    ];
    if (directory) items.push({ label: "在此处打开终端", onClick: () => {
      void api.openPathOnHost(node.path, "terminal").catch((error: unknown) => setContentError(String(error)));
    } });
    else items.push({ label: "引用到对话", onClick: () => referenceFile(node.path) });
    if (root || canModify(node)) items.splice(1, 0,
      { label: directory ? "新建文件…" : "新建同级文件…", onClick: () => startCreate("file", parent) },
      { label: directory ? "新建文件夹…" : "新建同级文件夹…", onClick: () => startCreate("dir", parent) },
    );
    return canModify(node) ? [...items,
      { label: "重命名", onClick: () => startRename(node) },
      { label: "创建副本", onClick: () => {
        if (currentProjectId == null) return;
        void handleMutation(async () => {
          const result = await api.projectFileDuplicate(currentProjectId, { path: relativeTo(projectRoot, node.path), session_id: sessionId });
          await selectAfterMutation(result.relative_path, directory);
        }, setContentError);
      } },
      { label: directory ? "删除目录…" : "删除文件…", danger: true, onClick: () => { setEditError(""); setDeleting(node); } },
    ] : items;
  })() : [];
  const expandFirstLevels = () => {
    const paths = new Set<string>();
    const visit = (nodes: TreeNode[], depth: number) => {
      for (const node of nodes) if (node.type === "dir") {
        if (node.children == null) continue;
        paths.add(pathKey(node.path));
        if (depth > 1 && node.children) visit(node.children, depth - 1);
      }
    };
    visit(tree, 3);
    setOpenPaths((old) => new Set([...old, ...paths]));
  };

  const pathParts = previewRel.split("/").filter(Boolean);
  const fileName = pathParts[pathParts.length - 1] ?? "";
  const dirParts = pathParts.slice(0, -1);
  useLayoutEffect(() => {
    const el = crumbsRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [previewPath, narrow]);

  if(previewTarget) return <div className="ft-panel resource-attachment-panel" ref={panelRef}>
    <ResourcePreview key={`${previewRequestId}:${JSON.stringify(previewTarget)}`} source={previewTarget} onClose={()=>setPreviewPath(null)} onDirectory={path=>void revealDirectory(path)}/>
  </div>;

  return (
    <div className={"ft-panel" + (narrow ? " is-narrow" : "")} ref={panelRef}>
      {contentError && <div className="resource-notice" role="alert">{contentError}</div>}
      <div className={"ft-body" + (previewPath ? " has-preview" : "") + (wide ? " is-wide" : "") + (previewPath && treeCollapsed && !narrow ? " tree-collapsed" : "")}>
        {/* 左：目录树（预览时可收起；收起后不可聚焦，见 panel.css .tree-collapsed） */}
        <div className="ft-tree-col" aria-hidden={previewPath && treeCollapsed && !narrow ? true : undefined}>
          <div className="ft-tree-head">
            <Tooltip content={projectRoot || "未关联项目"} side="bottom">
            <span className="ft-root">
              {project ? <ProjectIcon icon={projectIconKey(project.icon)} color={projectColorKey(project.color)} size={16} /> : <IconFolderTree size={15} />}
              <span className="ft-root-name">{project?.name || project?.path.split(/[\\/]/).pop() || "项目"}</span>
            </span>
            </Tooltip>
            <div className="ft-actions">
              <IconButton size="xs" title="新建文件" icon={<IconPlus size={14} />} disabled={!projectRoot} onClick={() => startCreate("file")} />
              <IconButton size="xs" title="新建文件夹" icon={<IconFolderPlus size={14} />} disabled={!projectRoot} onClick={() => startCreate("dir")} />
              <IconButton size="xs" title="定位当前文件" icon={<IconFileSearch size={14} />} disabled={!previewPath} onClick={() => void revealNode(previewNode)} />
              <Menu trigger={<IconButton size="xs" aria-label="更多文件操作" icon={<IconMoreHorizontal size={14} />} disabled={!projectRoot} />}
                entries={[
                  { label: "显示隐藏项和忽略目录", active: showHidden, onSelect: () => setShowHidden(!showHidden) },
                  { label: "展开前三层", onSelect: expandFirstLevels },
                  { label: "全部折叠", onSelect: () => setOpenPaths(new Set(tree.map((node) => pathKey(node.path)))) },
                  { label: "刷新", onSelect: () => { void loadTree(); void refreshMarks(() => true); } },
                  { label: "打开项目目录", onSelect: () => revealExternal(projectRoot) },
                ]} />
            </div>
          </div>
          <div className="ft-search">
            <Input
              className="ft-filter-input"
              size="sm"
              prefix={<IconSearch size={13} />}
              clearable
              value={filter}
              placeholder={searchMode === "content" ? "搜索全文…" : searchMode === "path" ? "搜索路径…" : "过滤文件…"}
              aria-label="搜索文件"
              spellCheck={false}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && filter) { e.stopPropagation(); setFilter(""); }
                if (e.key === "Enter" && filter.trim() && searchMode === "tree") setSearchMode("path");
              }}
            />
            <Menu trigger={<IconButton size="xs" active={searchMode !== "tree"} aria-label="搜索范围" icon={<IconFileSearch size={14} />} />}
              entries={[
                { label: "过滤目录树", active: searchMode === "tree", onSelect: () => setSearchMode("tree") },
                { label: "全库按路径搜索", active: searchMode === "path", onSelect: () => setSearchMode("path") },
                { label: "全库按内容搜索", active: searchMode === "content", onSelect: () => setSearchMode("content") },
              ]} />
          </div>
          <Collapse open={searchMode !== "tree"} lazy>
            <div className="ft-search"><Input size="sm" value={extension} onChange={(e) => setExtension(e.target.value)}
              aria-label="扩展名过滤" placeholder="限定扩展名（如 ts）" /></div>
          </Collapse>
          <div className={"ft-tree" + (toggling ? " is-toggling" : "")} ref={treeRef} role="tree" tabIndex={0} aria-label="项目文件" aria-busy={loadingTree || undefined} onKeyDown={onTreeKeyDown}>
            <div className="ft-tree-inner" style={{ height: virtualizer.getTotalSize() }}>
              {virtualizer.getVirtualItems().map((v) => {
                const r = rows[v.index];
                const n = r.node;
                const isDir = n.type === "dir";
                const mark = gitMarks.get(pathKey(n.path));
                const editing = rename && pathKey(rename.path) === pathKey(n.path);
                const entering = revealRange != null && v.index >= revealRange[0] && v.index <= revealRange[1];
                return (
                  <div
                    key={n.path}
                    role="treeitem"
                    aria-level={r.depth + 1}
                    aria-expanded={isDir ? r.open : undefined}
                    aria-selected={isDir ? n.path === cursor : pathKey(n.path) === previewKey}
                    className={"ft-row" + (isDir ? "" : " ft-file") + ((isDir ? n.path === cursor : pathKey(n.path) === previewKey) ? " active" : "") + (n.path === cursor ? " is-cursor" : "") + (entering ? " is-entering" : "")}
                    style={{
                      height: ROW_H,
                      transform: `translateY(${v.start}px)`,
                      paddingLeft: 6 + r.depth * 14,
                      // 入场错峰：越靠下的行越晚出现，最多错开 12 档
                      ...(entering ? { ["--i" as string]: Math.min(v.index - revealRange[0], 12) } : null),
                    }}
                    onClick={() => { if (!editing) activate(r); }}
                    onContextMenu={(e) => {
                      e.preventDefault(); e.stopPropagation(); setCursor(n.path);
                      setContext({ x: e.clientX, y: e.clientY, node: n });
                    }}
                  >
                    <span className="ft-chev">{isDir && <IconArrowToggle open={r.open} size={11} />}</span>
                    <FileIcon name={n.name} folder={isDir} open={r.open} size={16} />
                    {editing ? <Input size="sm" className="ft-rename" autoFocus value={editValue} disabled={mutating}
                      aria-label="新名称" invalid={!!editError} onFocus={(e) => e.currentTarget.select()}
                      onChange={(e) => setEditValue(e.target.value)} onKeyDown={(e) => {
                        e.stopPropagation();
                        if (e.key === "Enter") { e.preventDefault(); saveEdit(); }
                        if (e.key === "Escape") { setRename(null); setEditError(""); treeRef.current?.focus(); }
                      }} /> : <span className="ft-name"><Hit text={n.name} range={r.hit} /></span>}
                    {mark && <span className={`ft-git-mark mark-${mark === "•" ? "dir" : mark.toLowerCase()}`}
                      aria-label={mark === "•" ? "目录中有 Git 变更" : `Git 状态 ${mark}`}>{mark}</span>}
                  </div>
                );
              })}
            </div>
            {rows.length === 0 && (searchError ? <div className="resource-notice" role="alert">{searchError}</div>
              : (remoteSearch ? searching : loadingTree) ? <ResourceLoading />
              : contentError ? <Button size="sm" variant="ghost" onClick={() => void loadTree()}>重试加载</Button>
              : <Empty size="sm" mood={filter.trim() ? "think" : "calm"}
                title={filter.trim() ? `没有匹配「${filter.trim()}」的文件` : projectRoot ? "项目里还没有文件" : "此会话未关联项目"} />)}
            {!remoteSearch && filter.trim() && rows.length === 0 && !loadingTree &&
              <Button size="sm" variant="ghost" onClick={() => setSearchMode("path")}>在整个项目中搜索</Button>}
          </div>
          {rename && editError && <div className="resource-notice" role="alert">{editError}</div>}
        </div>

        {/* 宽面板且没有预览：右侧空态说明（引导点选左侧文件） */}
        {wide && !previewPath && (
          <div className="ft-wide-empty">
            <Empty size="sm" mood="calm" title="选择左侧文件进行预览" />
          </div>
        )}

        {/* 右：预览 */}
        {previewPath && (
          <div className="ft-preview">
            <div className="ft-preview-head">
              <div className="ft-ph-row">
                {narrow ? (
                  <Tooltip content="返回文件列表" side="bottom">
                    <button type="button" className="ui-icon-btn ft-back" aria-label="返回文件列表" onClick={() => setPreviewPath(null)}><IconArrowLeft size={14} /></button>
                  </Tooltip>
                ) : (
                  <Tooltip content={treeCollapsed ? "展开目录" : "收起目录"} side="bottom">
                    <button
                      type="button"
                      className="ui-icon-btn ft-tree-toggle"
                      aria-label={treeCollapsed ? "展开目录" : "收起目录"}
                      aria-pressed={!treeCollapsed}
                      onClick={toggleTree}
                    >
                      <IconPanelLeft size={16} open={!treeCollapsed} />
                    </button>
                  </Tooltip>
                )}
                <span className="ft-ph-file">
                  <FileIcon name={fileName} size={16} />
                  <span className="ft-ph-name">{fileName}</span>
                </span>
                {isDeletedDiff && <span className="ft-ph-deleted">已删除</span>}
                <Menu trigger={<IconButton size="xs" aria-label="预览文件操作" icon={<IconMoreHorizontal size={14} />} />}
                  entries={[
                    { label: "复制相对路径", icon: <IconCopy size={14} />, onSelect: () => copyPath(previewRel) },
                    { label: "引用到对话", icon: <IconAt size={14} />, onSelect: () => referenceFile(previewNode) },
                    { label: "在目录树中定位", icon: <IconFileSearch size={14} />, onSelect: () => void revealNode(previewNode, false, narrow) },
                  ]} />
                {(hasDiff || turnFileReview) && !isDeletedDiff && (
                  <Segmented
                    size="sm"
                    value={turnFileReview?.mode ?? viewMode}
                    onChange={value => { if (turnFileReview) void usePanelStore.getState().openTurnFile(turnFileReview, turnFileReview.index, value); else setViewMode(value); }}
                    aria-label="预览方式"
                    options={[{ value: "diff", label: "变更对比" }, { value: "content", label: "当前内容" }]}
                  />
                )}
                {!isDeletedDiff && (
                  <Tooltip content="在外部打开" side="bottom">
                    <button type="button" className="ui-icon-btn" aria-label="在外部打开" onClick={() => openExternal(previewPath)}><IconExternalLink size={14} /></button>
                  </Tooltip>
                )}
                {!narrow && (
                  <Tooltip content="关闭预览" side="bottom">
                    <button type="button" className="ui-icon-btn" aria-label="关闭预览" onClick={() => setPreviewPath(null)}><IconX size={14} /></button>
                  </Tooltip>
                )}
              </div>
              {turnFileReview && <div className="file-review-navigation">
                <span className="resource-origin">本轮文件</span>
                <span className="file-review-name">{turnFileReview.index + 1} / {turnFileReview.files.length}</span>
                <div className="resource-actions">
                  <Tooltip content="上一个文件"><Button variant="ghost" size="xs" iconOnly icon={<IconChevronLeft size={14}/>} aria-label="上一个变更文件" disabled={turnFileReview.index <= 0} onClick={() => void usePanelStore.getState().openTurnFile(turnFileReview, turnFileReview.index - 1, turnFileReview.mode)}/></Tooltip>
                  <Tooltip content="下一个文件"><Button variant="ghost" size="xs" iconOnly icon={<IconChevronRight size={14}/>} aria-label="下一个变更文件" disabled={turnFileReview.index >= turnFileReview.files.length - 1} onClick={() => void usePanelStore.getState().openTurnFile(turnFileReview, turnFileReview.index + 1, turnFileReview.mode)}/></Tooltip>
                </div>
              </div>}
              {dirParts.length > 0 && (
                <div className="ft-crumbs" ref={crumbsRef} aria-label="文件路径">
                  {dirParts.map((seg, i) => (
                    <span className="ft-crumb-wrap" key={`${i}:${seg}`}>
                      {i > 0 && <IconChevronRight size={10} className="ft-crumb-sep" />}
                      <button type="button" className="ft-crumb" onClick={() => revealDir(dirParts.slice(0, i + 1).join("/"))}>{seg}</button>
                    </span>
                  ))}
                </div>
              )}
            </div>
            {turnFileReview?.loading ? <ResourceLoading>正在读取本轮变更…</ResourceLoading>
            : turnFileReview?.error ? <div className="resource-state" role="alert"><p>{turnFileReview.error}</p><Button size="sm" variant="outline" onClick={() => void usePanelStore.getState().openTurnFile(turnFileReview, turnFileReview.index, "diff")}>重试</Button></div>
            : showDiff ? (
              <div className="ft-diff-body">
                {/* plan-20261018-turn-changes: 只有 mountedDiffKey 与当前 diff 一致时才挂载编辑器。
                    旧实例先卸载、隔一帧再挂载新实例，避免 monaco 0.55 在同一个 tick 里
                    dispose 旧 TextModel 又 reset 新 model，抛
                    "TextModel got disposed before DiffEditorWidget model got reset"。 */}
                {mountedDiffKey === diffModelKey && (
                  <FileDiffView key={`${previewRequestId}:${mountedDiffKey}`} diff={diffPreview!} language={contentLang} mode={diffLayout} onModeChange={setDiffLayout}/>

                )}

              </div>
            ) : resourceSource ? (
              <ResourcePreview showName={false} key={`${previewRequestId}:${JSON.stringify(resourceSource)}:${previewChangeSignature}`} source={resourceSource} line={previewLine} onDirectory={path=>void revealDirectory(path)} />
            ) : <div className="resource-state">此会话未关联项目，无法读取项目文件。</div>}
          </div>
        )}
      </div>
      <PointContextMenu point={context} items={contextItems} onClose={() => setContext(null)} />
      <FormDialog open={create != null} title={create?.type === "dir" ? "新建文件夹" : "新建文件"}
        subtitle={create?.parent || projectRoot} onClose={() => { if (!mutating) setCreate(null); }}
        onSubmit={saveEdit} submitDisabled={mutating || !editValue.trim()}>
        <Input value={editValue} onChange={(e) => setEditValue(e.target.value)} autoFocus
          placeholder="输入名称或相对路径" aria-label="名称" invalid={!!editError} disabled={mutating}
          onKeyDown={(e) => { if (e.key === "Enter") saveEdit(); }} />
        {editError && <div role="alert" className="resource-notice">{editError}</div>}
      </FormDialog>
      <ConfirmDialog open={deleting != null} title={deleting?.type === "dir" ? "删除文件夹" : "删除文件"}
        message={editError || `删除「${deleting ? relativeTo(projectRoot, deleting.path) : ""}」${deleting?.type === "dir" ? "及其所有内容" : ""}？回收站不可用时将永久删除。`}
        danger confirmLabel="删除" busy={mutating} busyLabel="正在删除…" onConfirm={deleteEntry} onCancel={() => setDeleting(null)} />
    </div>
  );
}

