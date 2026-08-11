/** ToolTree（v4 全量重写）：§3.3.2 工具调用合并行/树。
 * - 折叠态：单行小字摘要，"已读取 2 个文件 · 编辑 1 个文件 ›"
 * - 展开态：树状层级，每个 leaf 可独立展开查看完整 output
 *   - 文件路径高亮可点击 -> 右侧面板预览
 *   - output 按文件扩展名包裹代码块获得高亮
 *   - 支持多层嵌套（子代理调用）
 */
import { memo, useMemo, useState } from "react";
import type { ToolLeaf, ToolNode } from "./timeline";
import { usePanelStore } from "../../store/panel";
import {
  IconFileRead, IconFileWrite, IconFolder, IconGlobe, IconSearch, IconTerminal,
  IconUsers, IconBox, IconZap, IconFlask, IconGitBranch, IconBrain,
  IconArrowToggle, IconSpinner, IconCheck, IconX,
} from "../icons";

const GROUP_PHRASES: Record<string, string> = {
  fs_read: "读取 {n} 个文件",
  fs_write: "编辑 {n} 个文件",
  fs_list: "列出 {n} 个目录",
  fs_grep: "搜索 {n} 次",
  terminal_exec: "执行 {n} 条命令",
  web_fetch: "获取 {n} 个页面",
  web_search: "搜索 {n} 次",
  git_diff: "检查 {n} 个差异",
  spawn_subagent: "创建 {n} 个子代理",
  ask_subagent: "向子代理询问 {n} 次",
  collect_results: "收集 {n} 个结果",
};

const TOOL_ICONS: Record<string, React.ReactNode> = {
  fs_read: <IconFileRead size={13} />,
  fs_write: <IconFileWrite size={13} />,
  fs_list: <IconFolder size={13} />,
  fs_grep: <IconSearch size={13} />,
  terminal_exec: <IconTerminal size={13} />,
  web_fetch: <IconGlobe size={13} />,
  web_search: <IconSearch size={13} />,
  git_diff: <IconGitBranch size={13} />,
  git_root: <IconGitBranch size={13} />,
  spawn_subagent: <IconUsers size={13} />,
  ask_subagent: <IconBrain size={13} />,
  collect_results: <IconBox size={13} />,
  ci_run: <IconFlask size={13} />,
  memory_search: <IconBrain size={13} />,
  mcp: <IconBox size={13} />,
  codebase_search: <IconSearch size={13} />,
};

function toolIcon(tool: string): React.ReactNode {
  return TOOL_ICONS[tool] ?? <IconZap size={13} />;
}

function groupPhrase(tool: string, n: number): string {
  const tpl = GROUP_PHRASES[tool];
  return tpl ? tpl.replace("{n}", String(n)) : tool + " × " + n;
}

function leafPath(leaf: ToolLeaf): string | null {
  const p = leaf.args?.path;
  if (typeof p === "string" && p) return p;
  const file = leaf.args?.file_path;
  if (typeof file === "string" && file) return file;
  const cmd = leaf.args?.command;
  if (typeof cmd === "string" && cmd) return cmd;
  const url = leaf.args?.url;
  if (typeof url === "string" && url) return url;
  return null;
}

const LeafRow = memo(function LeafRow({ leaf }: { leaf: ToolLeaf }) {
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const [expanded, setExpanded] = useState(false);
  const path = leafPath(leaf);
  const ok = leaf.ok;
  const hasOutput = leaf.output && leaf.output.length > 0;

  const openInPanel = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (path) {
      setPreviewPath(path);
      openPanel();
      openTab("files");
    }
  };

  return (
    <div className="tc-node">
      <div className="tc-row" onClick={() => hasOutput && setExpanded(!expanded)}>
        {hasOutput ? (
          <span className="tc-chevron"><IconArrowToggle open={expanded} size={11} /></span>
        ) : (
          <span className="tc-chevron tc-chevron-spacer" />
        )}
        <span className="tc-icon">{toolIcon(leaf.tool)}</span>
        <span className={"tc-name" + (ok === null ? " text-shine" : "")}>{leaf.tool}</span>
        {path && (
          <button className="tc-path" title={path} onClick={openInPanel}>
            {path.split(/[\\/]/).pop() || path}
          </button>
        )}
        <span className={"tc-status " + (ok === null ? "wait" : ok ? "ok" : "fail")} style={{ display: "inline-flex", alignItems: "center" }}>
          {ok === null ? <IconSpinner size={11} /> : ok ? <IconCheck size={11} /> : <IconX size={11} />}
        </span>
        {leaf.durationMs != null && ok !== null && (
          <span className="tc-duration">{leaf.durationMs}ms</span>
        )}
      </div>
      {expanded && hasOutput && (
        <div className="tc-output">
          {path && /\.(ts|tsx|js|jsx|py|json|md|css|html|go|rs|java|c|cpp|sh)$/i.test(path) ? (
            <pre className="tc-code"><code>{leaf.output}</code></pre>
          ) : (
            <pre className="tc-plain">{leaf.output}</pre>
          )}
        </div>
      )}
      {expanded && leaf.error && (
        <pre className="tc-error-output">{leaf.error}</pre>
      )}
    </div>
  );
});

export const ToolTree = memo(function ToolTree({ nodes }: { nodes: ToolNode[] }) {
  const [open, setOpen] = useState(false);

  const stats = useMemo(() => {
    const map = new Map<string, number>();
    const walk = (ns: ToolNode[]) => {
      for (const n of ns) {
        if (n.kind === "group") map.set(n.tool, (map.get(n.tool) ?? 0) + n.count);
        else map.set(n.leaf.tool, (map.get(n.leaf.tool) ?? 0) + 1);
      }
    };
    walk(nodes);
    return [...map.entries()];
  }, [nodes]);

  // 是否有仍在执行（进行中）的工具：用于簇标题挂流光文字
  const hasRunning = useMemo(() => {
    let found = false;
    const walk = (ns: ToolNode[]) => {
      for (const n of ns) {
        if (n.kind === "group") { if (n.leaves.some((l) => l.ok === null)) { found = true; return; } }
        else if (n.leaf.ok === null) { found = true; return; }
      }
    };
    walk(nodes);
    return found;
  }, [nodes]);

  if (nodes.length === 0) return null;

  return (
    <div className={"tool-cluster" + (open ? " open" : "")}>
      <button className="tool-cluster-head" onClick={() => setOpen(!open)}>
        <span className="tool-cluster-chev">
          <IconArrowToggle open={open} size={12} />
        </span>
        <span className="tool-cluster-summary">
          {stats.map(([tool, n], i) => (
            <span key={tool} className="tool-cluster-cat">
              {i > 0 && <span className="tool-cluster-sep">·</span>}
              {toolIcon(tool)}
              <span className={hasRunning ? "text-shine" : undefined}>{groupPhrase(tool, n)}</span>
            </span>
          ))}
        </span>
      </button>
      {open && (
        <div className="tool-cluster-body">
          {nodes.map((n, i) =>
            n.kind === "group" ? (
              <div key={"g" + i} className="tc-group">
                <div className="tc-group-head">
                  <span className="tc-icon">{toolIcon(n.tool)}</span>
                  <span className={"tc-name" + (n.leaves.some((l) => l.ok === null) ? " text-shine" : "")}>{groupPhrase(n.tool, n.count)}</span>
                  <span className="tc-status ok">{n.leaves.filter((l) => l.ok).length}/{n.count}</span>
                </div>
                <div className="tc-group-body">
                  {n.leaves.map((leaf, j) => (
                    <LeafRow key={"l" + j} leaf={leaf} />
                  ))}
                </div>
              </div>
            ) : (
              <LeafRow key={"l" + i} leaf={n.leaf} />
            ),
          )}
        </div>
      )}
    </div>
  );
});
