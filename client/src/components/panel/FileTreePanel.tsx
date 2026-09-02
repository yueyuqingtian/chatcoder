/** 文件管理面板（v2）：项目目录树 + 文件预览（monaco）+ 在外部打开。
 * v11: 变更审核 diff 视图——diffPreview.path 匹配当前预览文件时，
 * 用 Monaco DiffEditor 展示 before/after，可切换「变更对比 / 当前内容」。
 */
import { useEffect, useRef, useState } from "react";
import Editor, { DiffEditor } from "@monaco-editor/react";
import { api, type TreeNode } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconArrowToggle, IconFileText, IconFolder, IconFolderOpen, IconRefresh } from "../icons";
import { MarkdownContent } from "../MarkdownContent";

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

  return (
    <div
      className={`ft-row ft-file${node.path === selectedPath ? " active" : ""}`}
      style={{ paddingLeft: `${8 + depth * 14}px` }}
      onClick={() => onSelect(node.path)}
      ref={(el) => registerRow(node.path, el)}
      title={node.path}
    >
      <span className="ft-chev" />
      <IconFileText size={13} />
      <span className="ft-name">{node.name}</span>
    </div>
  );
}

export function FileTreePanel() {
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const projects = useChatStore((s) => s.projects);
  const previewPath = usePanelStore((s) => s.previewPath);
  const previewLine = usePanelStore((s) => s.previewLine);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const diffPreview = usePanelStore((s) => s.diffPreview);
  const streamingBuffers = useChatStore((s) => s.streamingBuffers);
  const isRunning = useChatStore((s) => s.isRunning);
  const turnChanges = useChatStore((s) => s.turnChanges);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const plansByTurn = useChatStore((s) => s.plansByTurn);
  const pendingPlan = useChatStore((s) => s.pendingPlan);
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

  const project = projects.find((p) => p.id === currentProjectId);

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
  const loadFile = async (path: string) => {
    if (!currentProjectId) return;
    setContentError(null);
    try {
      const data = await api.readProjectFile(currentProjectId, path);
      setContent(data.content);
      setContentLang(toMonacoLang(data.language, path));
      if (data.truncated) setContentError("文件过大，已截断预览");
    } catch (e) {
      setContentError(String(e));
      setContent("");
    }
  };

  useEffect(() => {
    if (previewPath) loadFile(previewPath);
  }, [previewPath, turnChanges]);

  // 计划文档流式刷新仅限「规划阶段」：当前 running turn 的计划尚未确认/取消时，
  // 流式缓冲即规划文本（实时刷新计划文档）；确认后进入执行阶段，流式缓冲变成
  // 消息流的执行文本，不得覆盖计划文档（与 PlanCard plan-633 同口径）。
  // 执行阶段计划文档的更新由下方 turnChanges 依赖重读磁盘内容兜底。
  const isPlanDoc = Boolean(previewPath && /ai\/chatcoder-plan-.*\.md$/i.test(previewPath));
  const planForTurn =
    runningTurnId != null
      ? (plansByTurn[runningTurnId] ??
        (pendingPlan && pendingPlan.turnId === runningTurnId ? pendingPlan : null))
      : null;
  const planSettled =
    planForTurn != null &&
    "status" in planForTurn &&
    (planForTurn.status === "confirmed" || planForTurn.status === "cancelled");
  const activeStream = isRunning && isPlanDoc && !planSettled
    ? Object.values(streamingBuffers).join("")
    : "";
  const displayContent = (activeStream && mdView === "preview") ? activeStream : content;

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
