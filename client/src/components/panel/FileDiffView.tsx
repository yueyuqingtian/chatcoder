import { useEffect, useRef, useState } from "react";
import Editor, { DiffEditor } from "@monaco-editor/react";
import type { editor as MonacoEditor } from "monaco-editor";
import type { DiffPreview } from "../../store/panel";
import { attachManualLayout } from "./editorLayout";
import { Button, Select, Tooltip } from "../ui";
import { IconArrowUp, IconArrowDown } from "../icons";

/** plan-20261105-file-review：完整文件持续可见，修改处导航和展示模式各自独立。 */
export function FileDiffView({ diff, language, mode, onModeChange }: { diff: DiffPreview; language: string; mode: string; onModeChange: (mode: string) => void }) {
  const [changes, setChanges] = useState<MonacoEditor.ILineChange[]>([]);
  const [current, setCurrent] = useState(-1);
  const diffRef = useRef<MonacoEditor.IStandaloneDiffEditor | null>(null);
  const codeRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);
  const [codeReady, setCodeReady] = useState(0);
  const theme = document.documentElement.dataset.theme === "dark" ? "vs-dark" : "light";
  const jump = (index: number) => {
    const change = changes[index];
    if (!change) return;
    setCurrent(index);
    const line = Math.max(1, change.modifiedStartLineNumber);
    const original = Math.max(1, change.originalStartLineNumber);
    diffRef.current?.getModifiedEditor().revealLineInCenter(line);
    diffRef.current?.getOriginalEditor().revealLineInCenter(original);
    codeRef.current?.revealLineInCenter(line);
    codeRef.current?.setPosition({ lineNumber: line, column: 1 });
  };
  useEffect(() => {
    const editor = codeRef.current;
    if (!editor || mode !== "marked") return;
    const collection = editor.createDecorationsCollection(changes.map(c => {
      const deleted = c.modifiedEndLineNumber === 0;
      const added = c.originalEndLineNumber === 0;
      const type = deleted ? "deleted" : added ? "added" : "modified";
      const start = Math.max(1, c.modifiedStartLineNumber);
      return {
        range: { startLineNumber: start, startColumn: 1, endLineNumber: Math.max(start, c.modifiedEndLineNumber), endColumn: 1 },
        options: { isWholeLine: true, className: `file-change-${type}`, linesDecorationsClassName: `file-change-mark-${type}`,
          hoverMessage: { value: deleted ? "此处删除了原有内容；切换单栏或并排对比可查看删除内容。" : added ? "本轮新增" : "本轮修改" },
          overviewRuler: { color: deleted ? "#c55b64" : added ? "#4f9d70" : "#5b92c5", position: 7 } },
      };
    }));
    return () => collection.clear();
  }, [changes, mode, codeReady]);
  if (diff.reason && diff.before == null && diff.after == null) return <div className="resource-state"><strong>此文件无法进行文本对比</strong><p>{diff.reason}</p><p>{diff.deleted ? "文件已删除，无法读取当前内容。" : "可切换到当前内容查看文件。"}</p></div>;
  return <div className="file-diff-view">
    <div className="resource-tools file-diff-toolbar">
      <Select size="sm" value={mode} onChange={onModeChange} aria-label="变更查看模式" options={[
        { value: "side", label: "并排对比" }, { value: "inline", label: "单栏对比" }, { value: "marked", label: "整文件标记" },
      ]} />
      <span className="file-change-legend"><span className="added">新增</span><span className="deleted">删除</span><span className="modified">修改</span></span>
      <div className="resource-actions">
        <Tooltip content="上一处修改"><Button size="xs" variant="ghost" iconOnly icon={<IconArrowUp size={14}/>} aria-label="上一处修改" disabled={current <= 0} onClick={() => jump(current - 1)}/></Tooltip>
        <span className="file-diff-count" role="status">{changes.length ? `${Math.max(0, current + 1)} / ${changes.length}` : "无差异"}</span>
        <Tooltip content="下一处修改"><Button size="xs" variant="ghost" iconOnly icon={<IconArrowDown size={14}/>} aria-label="下一处修改" disabled={!changes.length || current >= changes.length - 1} onClick={() => jump(current + 1)}/></Tooltip>
      </div>
    </div>
    <div className="file-diff-editors">
      <div className={`file-diff-editor${mode === "marked" ? " is-hidden" : ""}`} aria-hidden={mode === "marked"}>
        <DiffEditor height="100%" language={language} original={diff.before ?? ""} modified={diff.after ?? ""} theme={theme}
          options={{ readOnly: true, originalEditable: false, minimap: { enabled: false }, fontSize: 12,
            renderSideBySide: mode === "side", useInlineViewWhenSpaceIsLimited: false, automaticLayout: false,
            hideUnchangedRegions: { enabled: false }, renderOverviewRuler: true, scrollBeyondLastLine: false }}
          onMount={editor => {
            attachManualLayout(editor);
            diffRef.current = editor;
            const update = () => {
              const list = editor.getLineChanges() ?? [];
              setChanges(list); setCurrent(list.length ? 0 : -1);
              if (list.length) {
                editor.getModifiedEditor().revealLineInCenter(Math.max(1, list[0].modifiedStartLineNumber));
                editor.getOriginalEditor().revealLineInCenter(Math.max(1, list[0].originalStartLineNumber));
              }
            };
            const sub = editor.onDidUpdateDiff(update);
            editor.onDidDispose(() => { sub.dispose(); diffRef.current = null; });
            update();
          }}/>
      </div>
      {mode === "marked" && <div className="file-diff-editor"><Editor height="100%" language={language} value={diff.after ?? ""} theme={theme}
        options={{ readOnly: true, minimap: { enabled: false }, fontSize: 12, automaticLayout: false, scrollBeyondLastLine: false }}
        onMount={editor => { attachManualLayout(editor); codeRef.current = editor; editor.onDidDispose(() => { if (codeRef.current === editor) codeRef.current = null; }); setCodeReady(n => n + 1); }}/></div>}
    </div>
    {diff.reason && <div className="resource-notice">{diff.reason}</div>}
    {diff.deleted && mode === "marked" && <div className="resource-notice">此文件已删除，切换单栏或并排对比查看原内容。</div>}
    {diff.truncated && <div className="resource-notice">文件超过预览上限，当前内容已截断。</div>}
  </div>;
}
