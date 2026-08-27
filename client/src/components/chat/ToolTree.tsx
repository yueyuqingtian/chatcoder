/** ToolTree（v19）：工具调用展示。
 * - 非写操作：两次思考间连续 ≥2 合并为 action-cluster 行（运行中动词滚动切换，完成后摘要可展开）；
 * - 写操作：一行一次；紧邻同文件连续写合并为 write-merged（+N -M 累加，数字滚动动效）；
 * - 写行展开 = 真实变更（拉取 /turns/{id}/changes/diff 内联行级 diff）；点击文件名走右面板 DiffEditor；
 * - hover 不变色，仅显示展开箭头。
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
import { memo, useEffect, useMemo, useState } from "react";
import type { ToolLeaf, ToolNode } from "./timeline";
import { SEARCH_TOOLS, RUN_TOOLS, isWriteLeaf } from "./timeline";
import { api } from "../../api/client";
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
  return TOOL_VERBS[tool] ?? `已调用 ${tool}`;
}

function leafPath(leaf: ToolLeaf): string | null {
  const p = leaf.args?.path;
  if (typeof p === "string" && p) return p;
  const file = leaf.args?.file_path;
  if (typeof file === "string" && file) return file;
  // v25: 工具伪装写盘（terminal_exec 等）路径来自 change_stat
  if (leaf.changeStat?.path) return leaf.changeStat.path;
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

/** v19: +N -M 数字滚动动效组件（值变化时旧数字上移出新数字） */
const ChangeStat = memo(function ChangeStat({ additions, deletions }: { additions: number; deletions: number }) {
  return (
    <span className="tc-change-stat">
      <span className="tc-stat-roll" key={`a${additions}`}>
        <span className="tc-add">+{additions}</span>
      </span>
      <span className="tc-stat-roll" key={`d${deletions}`}>
        <span className="tc-del">-{deletions}</span>
      </span>
    </span>
  );
});

/** v19: 简易行级 diff（before/after 文本 → add/del/ctx 行），超长降级为全量 -/+ */
function simpleLineDiff(before: string, after: string): Array<{ type: "add" | "del" | "ctx"; text: string }> {
  const a = before ? before.split("\n") : [];
  const b = after ? after.split("\n") : [];
  if (a.length + b.length > 1600) {
    return [
      ...a.map((t) => ({ type: "del" as const, text: t })),
      ...b.map((t) => ({ type: "add" as const, text: t })),
    ];
  }
  // LCS 行 diff（带长度上限的 DP）
  const n = a.length, m = b.length;
  const dp: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: Array<{ type: "add" | "del" | "ctx"; text: string }> = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ type: "ctx", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ type: "del", text: a[i] }); i++; }
    else { out.push({ type: "add", text: b[j] }); j++; }
  }
  while (i < n) { out.push({ type: "del", text: a[i++] }); }
  while (j < m) { out.push({ type: "add", text: b[j++] }); }
  return out;
}

/** v19: 内联 diff 块（写操作行展开内容） */
function InlineDiff({ turnId, path }: { turnId: number | null; path: string }) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error"; msg: string } | { kind: "ok"; lines: Array<{ type: "add" | "del" | "ctx"; text: string }>; truncated: boolean }>({ kind: "loading" });
  useEffect(() => {
    let cancelled = false;
    if (turnId == null) { setState({ kind: "error", msg: "无 turn 信息，无法拉取变更" }); return; }
    api.getFileDiff(turnId, path)
      .then((d) => {
        if (cancelled) return;
        setState({ kind: "ok", lines: simpleLineDiff(d.before ?? "", d.after ?? ""), truncated: d.truncated });
      })
      .catch((e) => { if (!cancelled) setState({ kind: "error", msg: String(e) }); });
    return () => { cancelled = true; };
  }, [turnId, path]);
  if (state.kind === "loading") return <pre className="tc-plain">加载变更…</pre>;
  if (state.kind === "error") return <pre className="tc-plain">变更加载失败：{state.msg}</pre>;
  return (
    <pre className="tc-diff">
      {state.lines.map((l, i) => (
        <div key={i} className={`tc-diff-line ${l.type}`}>{l.type === "add" ? "+ " : l.type === "del" ? "- " : "  "}{l.text}</div>
      ))}
      {state.truncated && <div className="tc-diff-trunc">变更过大，已截断显示</div>}
    </pre>
  );
}

const LeafRow = memo(function LeafRow({ leaf }: { leaf: ToolLeaf }) {
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const setDiffPreview = usePanelStore((s) => s.setDiffPreview);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const [expanded, setExpanded] = useState(false);
  const path = leafPath(leaf);
  const ok = leaf.ok;
  // v25: 工具伪装写盘（terminal_exec 等带 change_stat）按写操作渲染——可展开行内 diff
  const isWrite = isWriteLeaf(leaf);
  const hasOutput = (leaf.output && leaf.output.length > 0) || !!leaf.error;
  const expandable = hasOutput || isWrite;
  const grepHits = leaf.tool === "fs_grep" && leaf.output ? parseGrepLines(leaf.output) : [];
  const summary = path ? "" : leafSummary(leaf);

  const openGrepHit = (e: React.MouseEvent, hit: { path: string; line: number }) => {
    e.stopPropagation();
    setPreviewPath(hit.path, hit.line);
    openPanel();
    openTab("files");
  };

  // 点击文件名/徽标：写入工具 = 右面板 Monaco DiffEditor 查看变更；其余查看文件
  const openFilePreview = async (e: React.MouseEvent, p: string) => {
    e.stopPropagation();
    setPreviewPath(p);
    openPanel();
    openTab("files");
    if (isWrite && leaf.turnId != null) {
      try {
        const d = await api.getFileDiff(leaf.turnId, p);
        setDiffPreview({ path: d.path, before: d.before, after: d.after, truncated: d.truncated });
      } catch { /* 回退普通预览 */ }
    }
  };

  const { dir, name } = path ? splitFilePath(path) : { dir: "", name: "" };

  return (
    <div className="tc-node">
      <div className={"tc-row" + (expandable ? " has-output" : "") + (expanded ? " expanded" : "") + (isWrite ? " tc-write" : "")} onClick={() => expandable && setExpanded(!expanded)}>
        <span className="tc-icon">{toolIcon(leaf.tool)}</span>
        <span className={"tc-verb" + (ok === null ? " text-shine" : "")}>
          {ok === null ? toolVerb(leaf.tool).replace("已", "正在") : toolVerb(leaf.tool)}
        </span>
        {path && (
          <span className="tc-filelink" title={`${path}（点击查看${isWrite ? "变更" : "文件"}）`} onClick={(e) => void openFilePreview(e, path)}>
            <FileBadge path={path} size={16} />
            <span className="tc-filename">{name}</span>
          </span>
        )}
        {path && dir && <span className="tc-dir" title={path}>{dir}</span>}
        {!path && <span className="tc-tool-name" title={leaf.tool}>{leaf.tool}</span>}
        {!path && summary && <span className="tc-query" title={summary}>{summary}</span>}
        {leaf.changeStat && ok && <ChangeStat additions={leaf.changeStat.additions} deletions={leaf.changeStat.deletions} />}
        {ok === null && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {ok === false && <span className="tc-status fail"><IconX size={11} /></span>}
        {expandable ? (
          <span className={"tc-chevron" + (expanded ? " open" : "")}><IconChevronRight size={11} /></span>
        ) : null}
      </div>
      {expanded && (
        <div className="tc-output">
          {isWrite && path ? (
            <InlineDiff turnId={leaf.turnId} path={path} />
          ) : (
            <>
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
            </>
          )}
        </div>
      )}
    </div>
  );
});

/** v19: 操作合并行（两次思考间的连续非写操作）：
 *  运行中——动词区每 1.2s 轮播最新操作（translateY 滚动动效）；
 *  完成后——「已执行 N 个操作（…）」摘要行，可展开查看全部明细。 */
const ActionClusterRow = memo(function ActionClusterRow({ leaves }: { leaves: ToolLeaf[] }) {
  const [expanded, setExpanded] = useState(false);
  const running = leaves.some((l) => l.ok === null);
  const failed = leaves.some((l) => l.ok === false);
  // 轮播索引：运行中循环展示最近若干条
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setTick((v) => v + 1), 1200);
    return () => clearInterval(t);
  }, [running]);

  const summary = useMemo(() => {
    const search = leaves.filter((l) => SEARCH_TOOLS.has(l.tool)).length;
    const run = leaves.filter((l) => RUN_TOOLS.has(l.tool)).length;
    const read = leaves.filter((l) => ["fs_read", "fs_list", "view_image", "read_attachment"].includes(l.tool)).length;
    const other = leaves.length - search - run - read;
    const parts: string[] = [];
    if (search > 0) parts.push(`${search} 搜索`);
    if (read > 0) parts.push(`${read} 读取`);
    if (run > 0) parts.push(`${run} 运行`);
    if (other > 0) parts.push(`${other} 调用`);
    return parts.join(", ") || `${leaves.length} 次调用`;
  }, [leaves]);

  const rolling = leaves[Math.min(tick % Math.max(1, leaves.length), leaves.length - 1)];
  const rollingPath = rolling ? leafPath(rolling) : null;
  const rollingText = rolling
    ? `${toolVerb(rolling.tool).replace("已", "正在")} ${rollingPath ? splitFilePath(rollingPath).name : leafSummary(rolling)}`.trim()
    : "";

  return (
    <div className="tc-node tc-explore">
      <div className={"tc-row has-output tc-explore-row" + (expanded ? " expanded" : "")} onClick={() => setExpanded(!expanded)}>
        <span className="tc-icon"><IconSearch size={13} /></span>
        {running ? (
          <span className="tc-verb-roll" key={tick}>
            <span className="tc-verb text-shine">{rollingText || "正在执行"}</span>
          </span>
        ) : (
          <>
            <span className="tc-verb">已执行 {leaves.length} 个操作</span>
            <span className="tc-explore-summary">{summary}</span>
          </>
        )}
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

/** v19: 同文件连续写合并行：文件徽标 + 累加 +N -M（滚动动效），展开看每次写明细（各自内联 diff） */
const WriteMergedRow = memo(function WriteMergedRow({ leaves }: { leaves: ToolLeaf[] }) {
  const [expanded, setExpanded] = useState(false);
  const running = leaves.some((l) => l.ok === null);
  const failed = leaves.some((l) => l.ok === false);
  const path = leafPath(leaves[0]) ?? "";
  const add = leaves.reduce((s, l) => s + (l.changeStat?.additions ?? 0), 0);
  const del = leaves.reduce((s, l) => s + (l.changeStat?.deletions ?? 0), 0);
  const { dir, name } = path ? splitFilePath(path) : { dir: "", name: "" };

  return (
    <div className="tc-node">
      <div className={"tc-row has-output tc-write" + (expanded ? " expanded" : "")} onClick={() => setExpanded(!expanded)}>
        <span className="tc-icon"><IconFileWrite size={13} /></span>
        <span className={"tc-verb" + (running ? " text-shine" : "")}>{running ? "正在编辑" : `已编辑 ×${leaves.length}`}</span>
        {path && (
          <span className="tc-filelink">
            <FileBadge path={path} size={16} />
            <span className="tc-filename">{name}</span>
          </span>
        )}
        {path && dir && <span className="tc-dir" title={path}>{dir}</span>}
        {(add > 0 || del > 0) && <ChangeStat additions={add} deletions={del} />}
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
        n.kind === "action-cluster"
          ? <ActionClusterRow key={`e${i}`} leaves={n.leaves} />
          : n.kind === "write-merged"
            ? <WriteMergedRow key={`w${i}`} leaves={n.leaves} />
            : n.kind === "group"
              ? n.leaves.map((leaf, j) => <LeafRow key={`g${i}-${j}`} leaf={leaf} />)
              : <LeafRow key={`l${i}`} leaf={n.leaf} />,
      )}
    </div>
  );
});
