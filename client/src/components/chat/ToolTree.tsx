/** ToolTree（v13 对齐 ZCode）：工具调用逐行平铺。
 * 行结构：动词（已编辑/已运行/已搜索…）+ 文件徽标 + 文件名(粗体) + 目录(灰) + +x -y
 * 点击行展开 output；grep 命中行可点击跳转右侧面板。
 */

/** v2.2 (对齐 zcode 3.14.2): 解析 grep 输出中的 `path:line:` 行 → [{path, line, rest}] */
function parseGrepLines(output: string): Array<{ path: string; line: number; rest: string }> {
  const out: Array<{ path: string; line: number; rest: string }> = [];
  const re = /^([^\s:][^:\n]*?):(\d+):\s?(.*)$/;
  for (const raw of output.split("\n")) {
    const m = raw.match(re);
    if (m) out.push({ path: m[1], line: Number(m[2]), rest: m[3] });
  }
  return out;
}
import { memo, useState } from "react";
import type { ToolLeaf, ToolNode } from "./timeline";
import { usePanelStore } from "../../store/panel";
import { FileBadge, splitFilePath } from "./FileBadge";
import {
  IconFileRead, IconFileWrite, IconFolder, IconGlobe, IconSearch, IconTerminal,
  IconUsers, IconBox, IconZap, IconFlask, IconGitBranch, IconBrain,
  IconSpinner, IconX, IconDiff, IconImage, IconChevronRight,
} from "../icons";

/** 工具 → 中文动作动词（对齐 zcode 行首文案） */
const TOOL_VERBS: Record<string, string> = {
  fs_read: "已读取",
  fs_write: "已编辑",
  editor_apply_diff: "已编辑",
  fs_list: "已列出",
  fs_grep: "已搜索",
  codebase_search: "已搜索",
  terminal_exec: "已运行",
  web_fetch: "已抓取",
  web_search: "已搜索",
  git_diff: "已检查",
  git_root: "已定位",
  spawn_subagent: "已创建子代理",
  ask_subagent: "已询问子代理",
  collect_results: "已收集结果",
  ci_run: "已运行 CI",
  memory_search: "已搜索记忆",
  view_image: "已查看图片",
  mcp: "已调用",
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
  editor_apply_diff: <IconDiff size={13} />,
  view_image: <IconImage size={13} />,
  mcp: <IconBox size={13} />,
};

function toolIcon(tool: string): React.ReactNode {
  return TOOL_ICONS[tool] ?? <IconZap size={13} />;
}

function toolVerb(tool: string): string {
  return TOOL_VERBS[tool] ?? "已调用";
}

function leafPath(leaf: ToolLeaf): string | null {
  const p = leaf.args?.path;
  if (typeof p === "string" && p) return p;
  const file = leaf.args?.file_path;
  if (typeof file === "string" && file) return file;
  return null;
}

/** 非文件类工具的行内摘要（命令/查询词）——完整展示首行，仅由 CSS 截断 */
function leafSummary(leaf: ToolLeaf): string {
  const cmd = leaf.args?.command ?? leaf.args?.cmd;
  if (typeof cmd === "string" && cmd) return cmd.split("\n")[0];
  const q = leaf.args?.query ?? leaf.args?.pattern ?? leaf.args?.url;
  if (typeof q === "string" && q) return q;
  return "";
}

/** 写入类工具：永不合并，行内展示文件与 +N -M，点击文件名打开右侧变更预览 */
const WRITE_TOOLS = new Set(["fs_write", "editor_apply_diff", "multi_file_edit"]);

const LeafRow = memo(function LeafRow({ leaf }: { leaf: ToolLeaf }) {
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const [expanded, setExpanded] = useState(false);
  const path = leafPath(leaf);
  const ok = leaf.ok;
  const hasOutput = (leaf.output && leaf.output.length > 0) || !!leaf.error;
  const grepHits = leaf.tool === "fs_grep" && leaf.output ? parseGrepLines(leaf.output) : [];
  const summary = path ? "" : leafSummary(leaf);

  const openGrepHit = (e: React.MouseEvent, hit: { path: string; line: number }) => {
    e.stopPropagation();
    setPreviewPath(hit.path, hit.line);
    openPanel();
    openTab("files");
  };

  // 点击文件名/徽标：右侧面板查看文件（写入工具 = 查看变更）
  const openFilePreview = (e: React.MouseEvent, p: string) => {
    e.stopPropagation();
    setPreviewPath(p);
    openPanel();
    openTab("files");
  };

  const isWrite = WRITE_TOOLS.has(leaf.tool);
  const { dir, name } = path ? splitFilePath(path) : { dir: "", name: "" };

  return (
    <div className="tc-node">
      <div className={"tc-row" + (hasOutput ? " has-output" : "") + (expanded ? " expanded" : "") + (isWrite ? " tc-write" : "")} onClick={() => hasOutput && setExpanded(!expanded)}>
        <span className="tc-icon">{toolIcon(leaf.tool)}</span>
        <span className={"tc-verb" + (ok === null ? " text-shine" : "")}>
          {ok === null ? toolVerb(leaf.tool).replace("已", "正在") : toolVerb(leaf.tool)}
        </span>
        {path && (
          <span className="tc-filelink" title={`${path}（点击查看${isWrite ? "变更" : "文件"}）`} onClick={(e) => openFilePreview(e, path)}>
            <FileBadge path={path} size={16} />
            <span className="tc-filename">{name}</span>
          </span>
        )}
        {path && dir && <span className="tc-dir" title={path}>{dir}</span>}
        {!path && summary && <span className="tc-query" title={summary}>{summary}</span>}
        {leaf.changeStat && ok && (
          <span className="tc-change-stat">
            <span className="tc-add">+{leaf.changeStat.additions}</span>
            <span className="tc-del">-{leaf.changeStat.deletions}</span>
          </span>
        )}
        {ok === null && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {ok === false && <span className="tc-status fail"><IconX size={11} /></span>}
        {hasOutput ? (
          <span className={"tc-chevron" + (expanded ? " open" : "")}><IconChevronRight size={11} /></span>
        ) : null}
      </div>
      {expanded && hasOutput && (
        <div className="tc-output">
          {grepHits.length > 0 && (
            <div className="tc-grep-hits">
              {grepHits.slice(0, 20).map((hit, i) => (
                <button key={i} className="tc-grep-hit" title={hit.path} onClick={(e) => openGrepHit(e, hit)}>
                  <span className="tc-grep-path">{hit.path.split(/[\\/]/).pop()}</span>
                  <span className="tc-grep-ln">:{hit.line}</span>
                  <span className="tc-grep-text">{hit.rest.slice(0, 60)}</span>
                </button>
              ))}
            </div>
          )}
          {leaf.output && (path && /\.(ts|tsx|js|jsx|py|json|md|css|html|go|rs|java|c|cpp|sh)$/i.test(path) ? (
            <pre className="tc-code"><code>{leaf.output}</code></pre>
          ) : (
            <pre className="tc-plain">{leaf.output}</pre>
          ))}
          {leaf.error && <pre className="tc-error-output">{leaf.error}</pre>}
        </div>
      )}
    </div>
  );
});

/** 探索合并行（对齐 zcode「🔍 探索 · N 文件」）：
 *  连续读/搜/列调用 ≥2 合并为一行；点击展开查看每次调用明细。
 *  从单行升级为合并行时以动画过渡（新挂载即播放一次）。
 */
const ExploreCluster = memo(function ExploreCluster({ leaves }: { leaves: ToolLeaf[] }) {
  const [expanded, setExpanded] = useState(false);
  const running = leaves.some((l) => l.ok === null);
  const failed = leaves.some((l) => l.ok === false);

  // 汇总：搜索类计「N 搜索」，其余按唯一文件路径计「N 文件」
  const SEARCH_TOOLS = new Set(["fs_grep", "codebase_search", "web_search", "memory_search"]);
  const searchCount = leaves.filter((l) => SEARCH_TOOLS.has(l.tool)).length;
  const fileCount = new Set(
    leaves.filter((l) => !SEARCH_TOOLS.has(l.tool)).map((l) => leafPath(l)).filter(Boolean),
  ).size;
  const parts: string[] = [];
  if (searchCount > 0) parts.push(`${searchCount} 搜索`);
  if (fileCount > 0) parts.push(`${fileCount} 文件`);
  const summary = parts.length > 0 ? parts.join(", ") : `${leaves.length} 次调用`;

  return (
    <div className="tc-node tc-explore">
      <div className={"tc-row has-output tc-explore-row" + (expanded ? " expanded" : "")} onClick={() => setExpanded(!expanded)}>
        <span className="tc-icon"><IconSearch size={13} /></span>
        <span className={"tc-verb" + (running ? " text-shine" : "")}>{running ? "正在探索" : "探索"}</span>
        <span className="tc-explore-summary">{summary}</span>
        {running && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {!running && failed && <span className="tc-status fail"><IconX size={11} /></span>}
        <span className={"tc-chevron" + (expanded ? " open" : "")}><IconChevronRight size={11} /></span>
      </div>
      {expanded && (
        <div className="tc-explore-detail">
          {leaves.map((leaf, j) => <LeafRow key={j} leaf={leaf} />)}
        </div>
      )}
    </div>
  );
});

export const ToolTree = memo(function ToolTree({ nodes }: { nodes: ToolNode[] }) {
  if (nodes.length === 0) return null;
  return (
    <div className="tool-flat">
      {nodes.map((n, i) =>
        n.kind === "explore"
          ? <ExploreCluster key={`e${i}`} leaves={n.leaves} />
          : n.kind === "group"
            ? n.leaves.map((leaf, j) => <LeafRow key={`g${i}-${j}`} leaf={leaf} />)
            : <LeafRow key={`l${i}`} leaf={n.leaf} />,
      )}
    </div>
  );
});
