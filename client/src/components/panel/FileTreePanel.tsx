/** 文件管理面板（v2）：项目目录树 + 文件预览（monaco）+ 在外部打开。
 * v11: 变更审核 diff 视图——diffPreview.path 匹配当前预览文件时，
 * 用 Monaco DiffEditor 展示 before/after，可切换「变更对比 / 当前内容」。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import Editor, { DiffEditor } from "@monaco-editor/react";
import { api, type TreeNode } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconArrowToggle, IconFileText, IconFolder, IconFolderOpen, IconRefresh } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
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

/** 根据文件相对路径计算需要展开的目录路径集合（用于定位文件）。 */
function ancestorDirs(filePath: string): string[] {
  const parts = filePath.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 1) return [];
  const dirs: string[] = [];
  for (let i = 1; i < parts.length; i++) {
    dirs.push(parts.slice(0, i).join("/"));
  }
  return dirs;
}

/** S14（plan-41-197）：文件类型徽标映射——按扩展名给出缩写与配色。
 *  此前所有文件都用同一个 IconFileText，无法区分类型（用户反馈“各类文件图标无区分度”）。 */
const FILE_KIND: Record<string, { label: string; tone: string }> = {
  ts: { label: "TS", tone: "blue" }, tsx: { label: "TSX", tone: "blue" },
  js: { label: "JS", tone: "yellow" }, jsx: { label: "JSX", tone: "yellow" },
  mjs: { label: "JS", tone: "yellow" }, cjs: { label: "JS", tone: "yellow" },
  py: { label: "PY", tone: "green" }, pyi: { label: "PYI", tone: "green" },
  json: { label: "JSON", tone: "orange" }, jsonc: { label: "JSON", tone: "orange" },
  md: { label: "MD", tone: "gray" }, mdx: { label: "MDX", tone: "gray" }, txt: { label: "TXT", tone: "gray" },
  css: { label: "CSS", tone: "purple" }, scss: { label: "SCSS", tone: "purple" }, less: { label: "LESS", tone: "purple" },
  html: { label: "HTML", tone: "orange" }, vue: { label: "VUE", tone: "green" }, svelte: { label: "SVE", tone: "red" },
  yml: { label: "YML", tone: "red" }, yaml: { label: "YML", tone: "red" },
  toml: { label: "TOML", tone: "red" }, env: { label: "ENV", tone: "red" },
  sh: { label: "SH", tone: "green" }, ps1: { label: "PS1", tone: "blue" }, bat: { label: "BAT", tone: "gray" },
  sql: { label: "SQL", tone: "blue" }, rs: { label: "RS", tone: "orange" }, go: { label: "GO", tone: "blue" },
  java: { label: "JAVA", tone: "red" }, kt: { label: "KT", tone: "purple" },
  c: { label: "C", tone: "blue" }, cpp: { label: "C++", tone: "blue" }, h: { label: "H", tone: "blue" },
  png: { label: "IMG", tone: "green" }, jpg: { label: "IMG", tone: "green" },
  jpeg: { label: "IMG", tone: "green" }, gif: { label: "IMG", tone: "green" },
  svg: { label: "SVG", tone: "green" }, webp: { label: "IMG", tone: "green" }, ico: { label: "ICO", tone: "green" },
  lock: { label: "LOCK", tone: "gray" },
};

function FileRow({ node, depth, onSelect, openPaths, setOpenPaths, selectedPath, registerRow }: {
  node: TreeNode;
  depth: number;
  onSelect: (path: string) => void;
  openPaths: Set<string>;
  setOpenPaths: (fn: (prev: Set<string>) => Set<string>) => void;
  selectedPath: string | null;
  registerRow: (path: string, el: HTMLDivElement | null) => void;
}) {
  const isDir = node.type === "dir";
  const open = openPaths.has(node.path);

  if (isDir) {
    return (
      <>
        <div
          className="ft-row"
          style={{ paddingLeft: `${8 + depth * 14}px` }}
          onClick={() => setOpenPaths((prev) => {
            const next = new Set(prev);
            if (next.has(node.path)) next.delete(node.path);
            else next.add(node.path);
            return next;
          })}
        >
          <span className="ft-chev"><IconArrowToggle open={open} size={11} /></span>
          {open ? <IconFolderOpen size={13} /> : <IconFolder size={13} />}
          <span className="ft-name">{node.name}</span>
        </div>
        {open && node.children?.map((c) => (
          <FileRow
            key={c.path}
            node={c}
            depth={depth + 1}
            onSelect={onSelect}
            openPaths={openPaths}
            setOpenPaths={setOpenPaths}
            selectedPath={selectedPath}
            registerRow={registerRow}
          />
        ))}
      </>
    );
  }

  // S14（plan-41-197）：文件类型徽标——按扩展名区分（未知类型回落通用文件图标）
  const _ext = node.name.includes(".") ? node.name.split(".").pop()!.toLowerCase() : "";
  const _kind = FILE_KIND[_ext];
  return (
    <div
      className={`ft-row ft-file${node.path === selectedPath ? " active" : ""}`}
      style={{ paddingLeft: `${8 + depth * 14}px` }}
      onClick={() => onSelect(node.path)}
      ref={(el) => registerRow(node.path, el)}
      title={node.path}
    >
      <span className="ft-chev" />
      {_kind
        ? <span className="ft-kind" data-tone={_kind.tone} aria-hidden>{_kind.label}</span>
        : <IconFileText size={13} />}
      <span className="ft-name">{node.name}</span>
    </div>
  );
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
  const [content, setContent] = useState<string>("");
  const [contentLang, setContentLang] = useState<string>("plaintext");
  const [contentError, setContentError] = useState<string | null>(null);
  // v11: 变更对比 / 当前内容 视图切换（仅当 diff 与当前文件匹配时生效）
  const [viewMode, setViewMode] = useState<"diff" | "content">("diff");
  // v17: Markdown 文件支持 预览/源码 切换（默认预览渲染）
  const [mdView, setMdView] = useState<"preview" | "source">("preview");
  const treeRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  // v2.2 (对齐 zcode 3.14.2): Monaco 编辑器实例引用（grep 行号定位）
  const editorRef = useRef<{ revealLineInCenter: (line: number) => void; setPosition: (p: { lineNumber: number; column: number }) => void; focus: () => void } | null>(null);
  /** plan-75-334 阶段2：请求序号——快速切换文件时只允许最新一次响应落地。 */
  const loadSeqRef = useRef(0);
  /** plan-75-334 阶段3：已完成的「路径#变更签名」加载键（隐藏期不请求、恢复可见时补算）。 */
  const loadedKeyRef = useRef<string>("");

  const project = projects.find((p) => p.id === currentProjectId);

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
  const showDiff = viewMode === "diff" && diffPreview != null && diffPreview.path === previewPath;

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

  // 打开文件后自动展开父级目录并滚动定位到该文件
  useEffect(() => {
    if (!previewPath) return;
    const dirs = ancestorDirs(previewPath);
    if (dirs.length === 0) return;
    setOpenPaths((prev) => {
      const next = new Set(prev);
      let changed = false;
      for (const d of dirs) { if (!next.has(d)) { next.add(d); changed = true; } }
      return changed ? next : prev;
    });
    // 等待目录展开渲染后滚动
    const t = setTimeout(() => {
      const el = rowRefs.current.get(previewPath);
      const container = treeRef.current;
      if (el && container) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 120);
    return () => clearTimeout(t);
  }, [previewPath, tree]);

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

  return (
    <div className="ft-panel">
      <div className="ft-head">
        <button className="ft-refresh" onClick={loadTree} title="刷新"><IconRefresh size={13} /></button>
        <span className="ft-root" title={project?.path}>{project?.path.split(/[\\/]/).pop() || "项目"}</span>
        {previewPath && (
          <button className="ft-open-ext" onClick={() => openExternal(previewPath)} title="在外部打开">
            在外部打开
          </button>
        )}
      </div>
      <div className="ft-body">
        <div className="ft-tree" ref={treeRef}>
          {tree.map((n) => (
            <FileRow
              key={n.path}
              node={n}
              depth={0}
              onSelect={(p) => setPreviewPath(p)}
              openPaths={openPaths}
              setOpenPaths={setOpenPaths}
              selectedPath={previewPath}
              registerRow={(path, el) => {
                if (el) rowRefs.current.set(path, el);
                else rowRefs.current.delete(path);
              }}
            />
          ))}
          {tree.length === 0 && <div className="ft-empty">无项目内容</div>}
        </div>
        {previewPath && (
          <div className="ft-preview">
            <div className="ft-preview-head">
              <span className="ft-preview-path">{previewPath.split("/").pop()}</span>
              <div className="ft-diff-toggle">
                {diffPreview != null && diffPreview.path === previewPath && (
                  <button className={viewMode === "diff" ? "active" : ""} onClick={() => setViewMode("diff")}>变更对比</button>
                )}
                <button className={viewMode === "content" ? "active" : ""} onClick={() => setViewMode("content")}>当前内容</button>
              </div>
              {/\.(md|markdown)$/i.test(previewPath) && (
                <div className="ft-diff-toggle">
                  <button className={mdView === "preview" ? "active" : ""} onClick={() => setMdView("preview")}>预览</button>
                  <button className={mdView === "source" ? "active" : ""} onClick={() => setMdView("source")}>源码</button>
                </div>
              )}
              <button className="ft-close" onClick={() => setPreviewPath(null)}>✕</button>
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
                <button className="btn-ghost" onClick={() => openExternal(previewPath)}>在外部打开</button>
              </div>
            ) : /\.(md|markdown)$/i.test(previewPath) && mdView === "preview" ? (
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
