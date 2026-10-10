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
import Editor, { DiffEditor } from "@monaco-editor/react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api, type TreeNode } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconArrowLeft, IconArrowToggle, IconChevronRight, IconCompress, IconExternalLink, IconRefresh, IconSearch, IconX, IconFolderTree } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { Button, Segmented, Tooltip } from "../ui";
import { FileIcon } from "../iconkit/FileIcon";
import { ProjectIcon } from "../project/ProjectIcon";
import { projectColorKey, projectIconKey } from "../project/projectMeta";
import { recordComponentRender, recordDerivation } from "../../perf/metrics";

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

const ROW_H = 26;
/** 面板窄于此值时，目录树与预览不再并排（各 200px 上下的代码预览根本放不下）：打开预览就占满面板，用「返回」回到目录树 */
const NARROW_PX = 540;

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
        const open = isDir && openPaths.has(n.path);
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
        const open = openPaths.has(n.path);
        rows.push({ node: n, depth, open, hit });
        if (open && n.children) {
          const all = (l: TreeNode[], d: number) => {
            for (const c of l) {
              const o = c.type === "dir" && openPaths.has(c.path);
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

export function FileTreePanel({ visible = true }: { visible?: boolean }) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("filePanel");

  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const projects = useChatStore((s) => s.projects);
  const previewPath = usePanelStore((s) => s.previewPath);
  const previewLine = usePanelStore((s) => s.previewLine);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const diffPreview = usePanelStore((s) => s.diffPreview);
  const turnChanges = useChatStore((s) => s.turnChanges);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [openPaths, setOpenPaths] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  /** 键盘光标（路径）：与「当前预览文件」是两回事——可以先移动再回车打开 */
  const [cursor, setCursor] = useState<string | null>(null);
  const [content, setContent] = useState<string>("");
  const [contentLang, setContentLang] = useState<string>("plaintext");
  const [contentError, setContentError] = useState<string | null>(null);
  // v11: 变更对比 / 当前内容 视图切换（仅当 diff 与当前文件匹配时生效）
  const [viewMode, setViewMode] = useState<"diff" | "content">("diff");
  // v17: Markdown 文件支持 预览/源码 切换（默认预览渲染）
  const [mdView, setMdView] = useState<"preview" | "source">("preview");
  const panelRef = useRef<HTMLDivElement>(null);
  const crumbsRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  const treeRef = useRef<HTMLDivElement>(null);
  /** 待定位的路径：展开祖先目录后，等行列表更新再滚动到它 */
  const pendingReveal = useRef<string | null>(null);
  // v2.2 (对齐 zcode 3.14.2): Monaco 编辑器实例引用（grep 行号定位）
  const editorRef = useRef<{ revealLineInCenter: (line: number) => void; setPosition: (p: { lineNumber: number; column: number }) => void; focus: () => void } | null>(null);
  /** plan-75-334 阶段2：请求序号——快速切换文件时只允许最新一次响应落地。 */
  const loadSeqRef = useRef(0);
  /** plan-75-334 阶段3：已完成的「路径#变更签名」加载键（隐藏期不请求、恢复可见时补算）。 */
  const loadedKeyRef = useRef<string>("");

  const project = projects.find((p) => p.id === currentProjectId);
  const projectRoot = project?.path ?? "";
  /** 预览文件的相对路径 / 树节点路径 / 比较键 */
  const previewRel = previewPath ? relativeTo(projectRoot, previewPath) : "";
  const previewNode = previewPath ? toNodePath(projectRoot, previewRel) : "";
  const previewKey = previewNode ? pathKey(previewNode) : "";

  // 面板宽度分档（只在跨过阈值时更新状态，拖分隔条逐帧改宽度不会逐帧重渲染）
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width < NARROW_PX));
    ro.observe(el);
    return () => ro.disconnect();
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
  const showDiff = viewMode === "diff" && hasDiff;

  // v11: 新 diff 到达时默认回到「变更对比」视图（用户手动切换「当前内容」不受影响）
  useEffect(() => {
    if (diffPreview != null && diffPreview.path === previewPath) {
      setViewMode("diff");
    }
  }, [diffPreview, previewPath]);

  const loadTree = async () => {
    if (!currentProjectId) return;
    try {
      const data = await api.getProjectTree(currentProjectId, 8);
      setTree(data.children ?? []);
    } catch { /* ignore */ }
  };

  useEffect(() => { loadTree(); }, [currentProjectId]);

  // 预览文件
  const loadFile = async (path: string, seq: number) => {
    if (!currentProjectId) return;
    setContentError(null);
    // plan-75-334 阶段0：文件预览请求计数（仅采集期间统计）——
    // 用来核对「只修改其它文件时当前预览请求次数为 0」这一验收项。
    recordDerivation("filePreviewRequest");
    try {
      const data = await api.readProjectFile(currentProjectId, path);
      // plan-75-334 阶段2：过期响应保护——快速切换文件时旧响应不得覆盖新预览
      if (loadSeqRef.current !== seq) return;
      setContent(data.content);
      setContentLang(toMonacoLang(data.language, path));
      if (data.truncated) setContentError("文件过大，已截断预览");
    } catch (e) {
      if (loadSeqRef.current !== seq) return;
      setContentError(String(e));
      setContent("");
    }
  };

  useEffect(() => {
    if (!previewPath) { loadedKeyRef.current = ""; return; }
    // plan-75-334 阶段3：隐藏期不请求磁盘（面板不可见时读盘+编辑器刷新都是白做）；
    // 恢复可见时本 effect 因 visible 变化重跑，键未记录 ⇒ 补算一次。
    if (!visible) return;
    const key = `${previewPath}#${previewChangeSignature}`;
    if (loadedKeyRef.current === key) return;
    loadedKeyRef.current = key;
    void loadFile(previewPath, ++loadSeqRef.current);
  }, [previewPath, previewChangeSignature, visible, currentProjectId]);

  // v19.2 (plan-917): 彻底隔离右侧文件面板与主会话流式输出。
  // 文件面板永远展示磁盘真实文件内容，绝不被 streamingBuffers 覆盖。
  // 文件落盘或修改后通过 turnChanges 重新加载最新磁盘文件。
  const displayContent = content;

  // v2.2 (对齐 zcode 3.14.2): grep path:line 跳转 → Monaco 定位到行
  useEffect(() => {
    if (previewLine != null && editorRef.current) {
      const editor = editorRef.current;
      try {
        editor.revealLineInCenter(previewLine);
        editor.setPosition({ lineNumber: previewLine, column: 1 });
        editor.focus();
      } catch { /* ignore */ }
    }
  }, [previewLine, content, contentLang]);

  // 打开文件后自动展开父级目录，并在行列表更新后滚动定位到该文件
  useEffect(() => {
    if (!previewPath) return;
    const dirs = ancestorNodePaths(projectRoot, previewRel);
    pendingReveal.current = previewNode;
    setCursor(previewNode);
    if (dirs.length === 0) return;
    setOpenPaths((prev) => {
      const next = new Set(prev);
      let changed = false;
      for (const d of dirs) { if (!next.has(d)) { next.add(d); changed = true; } }
      return changed ? next : prev;
    });
  }, [previewPath, tree, projectRoot]);

  const openExternal = (relOrAbsPath: string) => {
    if (!relOrAbsPath) return;
    let full = relOrAbsPath;
    if (project?.path) {
      const pRoot = project.path.replace(/\\/g, "/").replace(/\/$/, "");
      const cleanRel = relOrAbsPath.replace(/\\/g, "/").replace(/^\.\//, "");
      if (!cleanRel.startsWith(pRoot) && !/^[A-Za-z]:\//.test(cleanRel) && !cleanRel.startsWith("/")) {
        full = `${pRoot}/${cleanRel}`;
      }
    }
    window.chatcoderAPI?.openPath?.(full);
  };

  /* ── 虚拟化的目录树 ── */
  const rows = useMemo(() => flatten(tree, openPaths, filter), [tree, openPaths, filter]);
  const indexByPath = useMemo(() => {
    const m = new Map<string, number>();
    rows.forEach((r, i) => m.set(r.node.path, i));
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
    const i = indexByPath.get(target);
    if (i == null) return;
    pendingReveal.current = null;
    virtualizer.scrollToIndex(i, { align: "auto" });
  }, [indexByPath, virtualizer]);

  const toggleDir = (path: string) => setOpenPaths((prev) => {
    const next = new Set(prev);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });

  const activate = (row: FlatRow) => {
    setCursor(row.node.path);
    if (row.node.type === "dir") toggleDir(row.node.path);
    else setPreviewPath(row.node.path);
  };

  const onTreeKeyDown = (e: React.KeyboardEvent) => {
    if (rows.length === 0) return;
    const cur = cursor != null ? indexByPath.get(cursor) ?? -1 : -1;
    const go = (i: number) => {
      const next = Math.max(0, Math.min(rows.length - 1, i));
      setCursor(rows[next].node.path);
      virtualizer.scrollToIndex(next, { align: "auto" });
    };
    switch (e.key) {
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

  /** 面包屑：点目录 → 展开并在树里定位到它 */
  const revealDir = (dirRel: string) => {
    const node = toNodePath(projectRoot, dirRel);
    setFilter("");
    if (narrow) setPreviewPath(null); // 窄面板里目录树被预览占满：先回到目录树再定位
    pendingReveal.current = node;
    setCursor(node);
    setOpenPaths((prev) => {
      const next = new Set(prev);
      for (const d of [...ancestorNodePaths(projectRoot, `${dirRel}/x`), node]) next.add(d);
      return next;
    });
  };

  const pathParts = previewRel.split("/").filter(Boolean);
  const fileName = pathParts[pathParts.length - 1] ?? "";
  const dirParts = pathParts.slice(0, -1);
  useLayoutEffect(() => {
    const el = crumbsRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [previewPath, narrow]);
  const isMd = previewPath ? /\.(md|markdown)$/i.test(previewPath) : false;

  return (
    <div className={"ft-panel" + (narrow ? " is-narrow" : "")} ref={panelRef}>
      <div className={"ft-body" + (previewPath ? " has-preview" : "")}>
        {/* 左：目录树 */}
        <div className="ft-tree-col">
          <div className="ft-tree-head">
            <span className="ft-root">
              {project ? <ProjectIcon icon={projectIconKey(project.icon)} color={projectColorKey(project.color)} size={16} /> : <IconFolderTree size={15} />}
              <span className="ft-root-name">{project?.name || project?.path.split(/[\\/]/).pop() || "项目"}</span>
            </span>
            <Tooltip content="刷新" side="bottom">
              <button type="button" className="ui-icon-btn" aria-label="刷新" onClick={loadTree}><IconRefresh size={14} /></button>
            </Tooltip>
            <Tooltip content="全部折叠" side="bottom">
              <button type="button" className="ui-icon-btn" aria-label="全部折叠" disabled={openPaths.size === 0} onClick={() => setOpenPaths(new Set())}>
                <IconCompress size={14} />
              </button>
            </Tooltip>
          </div>
          <label className="ft-filter">
            <IconSearch size={13} />
            <input
              value={filter}
              placeholder="过滤文件…"
              aria-label="过滤文件"
              spellCheck={false}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Escape" && filter) { e.stopPropagation(); setFilter(""); } }}
            />
            {filter && (
              <button type="button" className="ft-filter-clear" aria-label="清除过滤" onClick={() => setFilter("")}><IconX size={11} /></button>
            )}
          </label>
          <div className="ft-tree" ref={treeRef} role="tree" tabIndex={0} aria-label="项目文件" onKeyDown={onTreeKeyDown}>
            <div className="ft-tree-inner" style={{ height: virtualizer.getTotalSize() }}>
              {virtualizer.getVirtualItems().map((v) => {
                const r = rows[v.index];
                const n = r.node;
                const isDir = n.type === "dir";
                return (
                  <div
                    key={n.path}
                    role="treeitem"
                    aria-level={r.depth + 1}
                    aria-expanded={isDir ? r.open : undefined}
                    aria-selected={!isDir && pathKey(n.path) === previewKey}
                    className={"ft-row" + (isDir ? "" : " ft-file") + (!isDir && pathKey(n.path) === previewKey ? " active" : "") + (n.path === cursor ? " is-cursor" : "")}
                    style={{ height: ROW_H, transform: `translateY(${v.start}px)`, paddingLeft: 6 + r.depth * 14 }}
                    onClick={() => activate(r)}
                  >
                    <span className="ft-chev">{isDir && <IconArrowToggle open={r.open} size={11} />}</span>
                    <FileIcon name={n.name} folder={isDir} open={r.open} size={16} />
                    <span className="ft-name"><Hit text={n.name} range={r.hit} /></span>
                  </div>
                );
              })}
            </div>
            {rows.length === 0 && (
              <div className="ft-empty">{filter.trim() ? `没有匹配「${filter.trim()}」的文件` : tree.length === 0 ? "无项目内容" : ""}</div>
            )}
          </div>
        </div>

        {/* 右：预览 */}
        {previewPath && (
          <div className="ft-preview">
            <div className="ft-preview-head">
              <div className="ft-ph-row">
                {narrow && (
                  <Tooltip content="返回文件列表" side="bottom">
                    <button type="button" className="ui-icon-btn ft-back" aria-label="返回文件列表" onClick={() => setPreviewPath(null)}><IconArrowLeft size={14} /></button>
                  </Tooltip>
                )}
                <span className="ft-ph-file">
                  <FileIcon name={fileName} size={16} />
                  <span className="ft-ph-name">{fileName}</span>
                </span>
                {hasDiff && (
                  <Segmented
                    size="sm"
                    value={viewMode}
                    onChange={setViewMode}
                    aria-label="预览方式"
                    options={[{ value: "diff", label: "变更对比" }, { value: "content", label: "当前内容" }]}
                  />
                )}
                {isMd && (
                  <Segmented
                    size="sm"
                    value={mdView}
                    onChange={setMdView}
                    aria-label="Markdown 视图"
                    options={[{ value: "preview", label: "预览" }, { value: "source", label: "源码" }]}
                  />
                )}
                <Tooltip content="在外部打开" side="bottom">
                  <button type="button" className="ui-icon-btn" aria-label="在外部打开" onClick={() => openExternal(previewPath)}><IconExternalLink size={14} /></button>
                </Tooltip>
                {!narrow && (
                  <Tooltip content="关闭预览" side="bottom">
                    <button type="button" className="ui-icon-btn" aria-label="关闭预览" onClick={() => setPreviewPath(null)}><IconX size={14} /></button>
                  </Tooltip>
                )}
              </div>
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
            {showDiff ? (
              <div className="ft-diff-body">
                <DiffEditor
                  height="100%"
                  language={contentLang}
                  original={diffPreview!.before ?? ""}
                  modified={diffPreview!.after ?? ""}
                  theme={document.documentElement.getAttribute("data-theme") === "dark" ? "vs-dark" : "light"}
                  options={{ readOnly: true, minimap: { enabled: false }, fontSize: 12, renderSideBySide: true }}
                />
                {diffPreview!.truncated && <div className="ft-diff-truncated">变更行数过大，内容已截断显示</div>}
              </div>
            ) : contentError ? (
              <div className="ft-preview-err">
                <p>{contentError}</p>
                <Button variant="outline" size="sm" icon={<IconExternalLink size={13} />} onClick={() => openExternal(previewPath)}>在外部打开</Button>
              </div>
            ) : isMd && mdView === "preview" ? (
              <div className="ft-md-preview">
                <MarkdownContent>{displayContent}</MarkdownContent>
              </div>
            ) : (
              <Editor
                height="100%"
                language={contentLang}
                value={content}
                theme={document.documentElement.getAttribute("data-theme") === "dark" ? "vs-dark" : "light"}
                options={{ readOnly: true, minimap: { enabled: false }, fontSize: 12 }}
                onMount={(editor) => { editorRef.current = editor; }}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

