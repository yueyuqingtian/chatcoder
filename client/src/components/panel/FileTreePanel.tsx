/** 文件管理面板（v2）：项目目录树 + 文件预览（monaco）+ 在外部打开。 */
import { useEffect, useRef, useState } from "react";
import Editor from "@monaco-editor/react";
import { api, type TreeNode } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconArrowToggle, IconFileText, IconFolder, IconFolderOpen, IconRefresh } from "../icons";

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
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [openPaths, setOpenPaths] = useState<Set<string>>(new Set());
  const [content, setContent] = useState<string>("");
  const [contentLang, setContentLang] = useState<string>("plaintext");
  const [contentError, setContentError] = useState<string | null>(null);
  const treeRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  const project = projects.find((p) => p.id === currentProjectId);

  const loadTree = async () => {
    if (!currentProjectId) return;
    try {
      const data = await api.getProjectTree(currentProjectId, 3);
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
 }, [previewPath]);

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

  const openExternal = (path: string) => {
    window.chatcoderAPI?.openPath?.(path);
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
              <button className="ft-close" onClick={() => setPreviewPath(null)}>✕</button>
            </div>
            {contentError ? (
              <div className="ft-preview-err">
                <p>{contentError}</p>
                <button className="btn-ghost" onClick={() => openExternal(previewPath)}>在外部打开</button>
              </div>
            ) : (
              <Editor
                height="100%"
                language={contentLang}
                value={content}
                theme={document.documentElement.getAttribute("data-theme") === "dark" ? "vs-dark" : "light"}
                options={{ readOnly: true, minimap: { enabled: false }, fontSize: 12 }}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
