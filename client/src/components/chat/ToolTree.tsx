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
import { useRunningTicker } from "./useRunningTicker";
import type { ToolLeaf, ToolNode } from "./timeline";
import { SEARCH_TOOLS, RUN_TOOLS, isWriteLeaf } from "./timeline";
import { api } from "../../api/client";
import { usePanelStore } from "../../store/panel";
import { useChatStore } from "../../store/chat";
import { useFlowExpand, useFlowGroupExpand } from "../../store/flowExpand";
import { splitFilePath } from "./FileBadge";
import { FileIcon } from "../iconkit/FileIcon";
import { CodeView } from "./CodeView";
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { ChatCollapse } from "./ChatCollapse";
import { SubsessionQuestionRow } from "./SubsessionQuestionRow";
import { SUBSESSION_NOTICE_TOOLS } from "../../utils/subsessionQuestion";
import { toolVerb, runningVerb, toolDisplayName } from "./toolLabels";
import { ClampBlock } from "./ClampBlock";
import { Tooltip } from "../ui/Tooltip";
import {
  IconFileRead, IconFileWrite, IconFolder, IconGlobe, IconTerminal,
  IconUsers, IconBox, IconZap, IconFlask, IconGitBranch, IconBrain,
  IconSpinner, IconX, IconImage, IconChevronRight,
  IconFilePatch, IconFileSearch, IconOperationCluster, IconChecklist, IconCopy, IconInfo,
} from "../icons";

/** plan-20261016-ux（问题 4）：命令类工具——完成后即使没有输出也可展开，展开区给出「没有输出」提示卡 */
const COMMAND_TOOLS = new Set([...RUN_TOOLS, "git", "terminal_bg_status", "terminal_bg_kill", "bg_command_done", "bg_command_stopped"]);
/** 读文件类工具：只要有路径，展开区一律用编辑器式代码视图（不再按扩展名白名单判断） */
const FILE_CONTENT_TOOLS = new Set(["fs_read", "read_file_range"]);

/** plan-20261015-browser-agent：浏览器工具的行内摘要——完成后取结果首行里的操作对象（如「按钮「登录」」），
 *  运行中按参数推断（网址 / 文字 / 按键 / 方向），一行就能看清 AI 在页面上做了什么。 */
function browserSummary(leaf: ToolLeaf): string {
  const a = (leaf.args ?? {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  if (leaf.ok !== null && leaf.output && leaf.tool !== "browser_snapshot" && leaf.tool !== "browser_get_text") {
    const first = leaf.output.split("\n")[0].trim();
    const stripped = first.replace(/^(已打开|截图完成)[：:]\s*/, "").replace(/^已[^\s：:]{1,6}\s+/, "");
    if (stripped) return stripped;
  }
  switch (leaf.tool) {
    case "browser_navigate":
      return str(a.url) || ({ back: "后退", forward: "前进", reload: "刷新" } as Record<string, string>)[str(a.action)] || "";
    case "browser_type": return str(a.text) ? `「${str(a.text).slice(0, 40)}」` : "";
    case "browser_press": return str(a.keys);
    case "browser_scroll":
      if (str(a.to)) return a.to === "top" ? "到顶部" : "到底部";
      return ({ up: "向上", down: "向下", left: "向左", right: "向右" } as Record<string, string>)[str(a.direction)] || "";
    case "browser_batch": return Array.isArray(a.actions) ? `${a.actions.length} 步` : "";
    case "browser_snapshot": return str(a.query) ? `查找「${str(a.query)}」` : "";
    case "browser_tabs":
      return ({ list: "查看标签", new: "新建标签", select: "切换标签", close: "关闭标签" } as Record<string, string>)[str(a.action) || "list"] || "";
    case "browser_clipboard":
      return ({ copy: "复制", cut: "剪切", paste: "粘贴", select_all: "全选" } as Record<string, string>)[str(a.action)] || "";
    default: return str(a.text) || str(a.ref) || str(a.selector) || "";
  }
}

const TOOL_ICONS: Record<string, React.ReactNode> = {
  fs_read: <IconFileRead size={16} />,
  fs_write: <IconFileWrite size={16} />,
  fs_list: <IconFolder size={16} />,
  fs_grep: <IconFileSearch size={16} />,
  terminal_exec: <IconTerminal size={16} />,
  web_fetch: <IconGlobe size={16} />,
  web_search: <IconFileSearch size={16} />,
  codebase_search: <IconFileSearch size={16} />,
  git_diff: <IconGitBranch size={16} />,
  git_root: <IconGitBranch size={16} />,
  spawn_subagent: <IconUsers size={16} />,
  ask_subagent: <IconBrain size={16} />,
  collect_results: <IconBox size={16} />,
  ci_run: <IconFlask size={16} />,
  memory_search: <IconBrain size={16} />,
  editor_apply_diff: <IconFilePatch size={16} />,
  view_image: <IconImage size={16} />,
  todo_write: <IconChecklist size={16} />,
  ask_user_question: <IconBrain size={16} />,
  mcp: <IconBox size={16} />,
};

/** plan-91-393：工具图标改为按语义设计、带运行态动画的 ToolGlyph；TOOL_ICONS 仅作兼容保留 */
function toolIcon(tool: string): React.ReactNode {
  return <ToolGlyph tool={tool} size={16} />;
}
void TOOL_ICONS; void IconZap;

/** plan-474-2105 组 C：主代理被邮箱打断时，工具结果会留下「转后台 / 让出等待」标记。
 *  这两种都不是失败——用中性标签单独表达，避免落到红色失败态里误导用户。 */
function interruptTagOf(leaf: ToolLeaf): string | null {
  if (leaf.output && leaf.output.includes("命令已转后台继续运行")) return "已转后台";
  if (leaf.error && leaf.error.trimStart().startsWith("[已让出等待]")) return "已让出等待";
  return null;
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

/** 非文件类工具的行内摘要（命令/查询词/清单步骤）——完整展示首行，仅由 CSS 截断 */
function leafSummary(leaf: ToolLeaf): string {
  if (leaf.tool === "todo_write") {
    const todos = (leaf.args?.todos as Array<{ content?: string; status?: string }> | undefined) || [];
    const activeTodo = todos.find((t) => t.status === "in_progress") || todos.find((t) => t.status === "pending") || todos[0];
    if (activeTodo?.content) return activeTodo.content.split("\n")[0];
    return `共 ${todos.length} 项清单`;
  }
  if (leaf.tool === "ask_user_question") {
    const qs = (leaf.args?.questions as Array<{ question?: string }> | undefined) || [];
    return qs.length > 0 ? `已询问 ${qs.length} 个问题` : "向用户发起询问";
  }
  if (leaf.tool.startsWith("browser_")) return browserSummary(leaf);
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
      <span className="tc-stat-roll tc-roll-up" key={`a${additions}`}>
        <span className="tc-add">+{additions}</span>
      </span>
      <span className="tc-stat-roll tc-roll-down" key={`d${deletions}`}>
        <span className="tc-del">-{deletions}</span>
      </span>
    </span>
  );
});

/** 按行切分（与服务端 splitlines 同口径）：
 *  去掉 BOM、兼容 CRLF，并丢弃末尾换行产生的空行——否则「文件末尾有无换行」
 *  会被前端当成多一行新增，出现摘要 +0 -0、展开却多一行的错位。 */
function splitLines(text: string): string[] {
  if (!text) return [];
  const normalized = text.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");
  while (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** v19: 简易行级 diff（before/after 文本 → add/del/ctx 行）。
 *
 *  plan-20261019-diff: 先裁掉公共前后缀，再只对差异段做 LCS——大文件里改动
 *  通常只占几行，裁剪后规模很小，不再一超过 1600 行就退化成「整篇删除+整篇新增」。
 *  差异段仍然过大时保留全量 -/+ 降级，避免前端卡死。 */
function simpleLineDiff(before: string, after: string): Array<{ type: "add" | "del" | "ctx"; text: string }> {
  const a = splitLines(before);
  const b = splitLines(after);
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length, endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const head = a.slice(0, start);
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  const tail = a.slice(endA);
  const out: Array<{ type: "add" | "del" | "ctx"; text: string }> = head.map((t) => ({ type: "ctx" as const, text: t }));
  const n = midA.length, m = midB.length;
  // 差异段规模上限：超过后退化为全量 -/+（对用户仍是完整内容，只是不区分公共行）
  if (n * m > 1_600_000) {
    out.push(...midA.map((t) => ({ type: "del" as const, text: t })));
    out.push(...midB.map((t) => ({ type: "add" as const, text: t })));
  } else {
    const dp: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = midA[i] === midB[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (midA[i] === midB[j]) { out.push({ type: "ctx", text: midA[i] }); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ type: "del", text: midA[i] }); i++; }
      else { out.push({ type: "add", text: midB[j] }); j++; }
    }
    while (i < n) out.push({ type: "del", text: midA[i++] });
    while (j < m) out.push({ type: "add", text: midB[j++] });
  }
  out.push(...tail.map((t) => ({ type: "ctx" as const, text: t })));
  return out;
}

/** v19: 内联 diff 块（写操作行展开内容）。
 *
 *  plan-89-387: 传 call_key 拉取「本次编辑」的 diff——同一轮内同文件多次编辑时，
 *  每次展开只展示本次变更（此前服务端按 turn+path 聚合，多次展开见同一份累积 diff）；
 *  老记录无 call_key 时服务端回退整轮累积口径，头部会标注「该轮累计」。
 *  v7(H): 运行中记录尚未落库（404）或无 turn 信息时，用调用参数兜底展示本次编辑内容。
 *
 *  plan-522-2209 R10：diff 区**不**套 ClampBlock，只把文案收敛成同一句式。依据是数据来源不同：
 *  `ClampBlock` 拿到的文本是完整的，它自己数行、自己切头尾，标注的总量可信；
 *  而这里收到的 `lines` 已经由后端裁过（`rollback_service.py` 的 `_MAX_DIFF_LINES_RENDER`，
 *  超出只下发前 4000 行并置 `truncated`），前端的「总行数」只剩已下发的那部分——
 *  再让 ClampBlock 折一遍，既折不出真正的尾部结论，还会把「已下发行数」当成总量标注，反而失真。
 *  正确做法是让后端负责截断、前端只如实说明「其余未下发」，两种截断各归其位。 */
function InlineDiff({ turnId, path, callKey, fallbackLines, onStat }: {
  turnId: number | null;
  path: string;
  callKey?: string;
  fallbackLines?: Array<{ type: "add" | "del" | "ctx"; text: string }> | null;
  /** plan-89-387: 加载完成后回报本次变更行数（行上缺 changeStat 时回填展示） */
  onStat?: (stat: { additions: number; deletions: number }) => void;
}) {
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "error"; msg: string }
    | {
        kind: "ok";
        lines: Array<{ type: "add" | "del" | "ctx"; text: string }>;
        truncated: boolean;
        additions: number;
        deletions: number;
        /** true=本次编辑口径；false=整轮累积（老记录无 call_key） */
        singleEdit: boolean;
        /** 口径说明（二进制降级、参数兜底等） */
        note: string | null;
      }
  >({ kind: "loading" });
  useEffect(() => {
    let cancelled = false;
    const fb = fallbackLines && fallbackLines.length > 0 ? fallbackLines : null;
    /** 记录缺失时用调用参数兜底渲染，避免「展开空白」；返回是否已兜底 */
    const showFallback = (note: string): boolean => {
      if (!fb) return false;
      const c = countDiffLines(fb);
      setState({
        kind: "ok", lines: fb, truncated: false,
        additions: c.additions, deletions: c.deletions, singleEdit: true, note,
      });
      onStat?.({ additions: c.additions, deletions: c.deletions });
      return true;
    };
    if (turnId == null) {
      if (!showFallback("无写盘记录，展示调用参数中的本次编辑内容")) {
        setState({ kind: "error", msg: "该调用没有写盘记录（可能未成功执行）" });
      }
      return;
    }
    setState({ kind: "loading" });
    api.getFileDiff(turnId, path, callKey)
      .then((d) => {
        if (cancelled) return;
        // 行级 diff 优先（服务端 SequenceMatcher 预计算，与徽标 +N -M 同源一致）。
        // plan-20261019-diff: 统计必须跟着「实际渲染来源」走——旧实现即使回退到参数
        // 兜底 diff，仍沿用服务端 additions/deletions（二进制/大文件时为 0），
        // 于是出现「摘要 +0 -0、展开却有一行新增」。
        const serverLines = d.lines && d.lines.length > 0
          ? d.lines.map((l) => ({ type: l.type as "add" | "del" | "ctx", text: l.text }))
          : null;
        const usingFallback = !serverLines && d.before == null && d.after == null && !!fb;
        const rendered = serverLines
          ?? (usingFallback ? fb! : simpleLineDiff(d.before ?? "", d.after ?? ""));
        const counted = countDiffLines(rendered);
        // 服务端给了非零统计且渲染的是服务端 lines：以服务端为准（截断时也是全量统计）；
        // 其余情况（参数兜底 / 旧记录 0/0）一律按实际渲染行统计，保证摘要与展开一致。
        const serverAdds = d.additions ?? 0;
        const serverDels = d.deletions ?? 0;
        const trustServer = !!serverLines && (serverAdds > 0 || serverDels > 0);
        const adds = trustServer ? serverAdds : counted.additions;
        const dels = trustServer ? serverDels : counted.deletions;
        setState({
          kind: "ok",
          lines: rendered,
          truncated: d.truncated,
          additions: adds,
          deletions: dels,
          singleEdit: d.single_edit === true,
          note: usingFallback ? "展示调用参数中的本次编辑内容" : (d.reason ?? null),
        });
        onStat?.({ additions: adds, deletions: dels });
      })
      .catch((e) => {
        if (cancelled) return;
        if (!showFallback("写盘记录尚未落库，展示调用参数中的本次编辑内容")) {
          const raw = String(e);
          setState({
            kind: "error",
            msg: raw.includes("无此文件的写盘记录")
              ? "本次调用未产生写盘记录（编辑未成功或尚未落库）"
              : raw,
          });
        }
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnId, path, callKey, fallbackLines]);
  if (state.kind === "loading") return <pre className="tc-plain">加载变更…</pre>;
  if (state.kind === "error") return <pre className="tc-plain">变更加载失败：{state.msg}</pre>;
  const hasStat = state.additions > 0 || state.deletions > 0;
  return (
    <div className="tc-diff-wrap cv">
      {/* plan-91-393：与读文件代码视图同一编辑器外观——文件头 + 符号槽 + 等宽内容 */}
      {(hasStat || state.note || path) && (
        <div className="tc-diff-head cv-head">
          <FileIcon name={path} size={14} />
          <Tooltip content={path} side="top"><span className="cv-name">{splitFilePath(path).name}</span></Tooltip>
          {hasStat && (
            <span className="tc-diff-stat">
              {state.singleEdit ? "本次变更" : "该轮累计"}
              <span className="tc-add">+{state.additions}</span>
              <span className="tc-del">-{state.deletions}</span>
            </span>
          )}
          {state.note && <Tooltip content={state.note} side="top"><span className="tc-diff-note">{state.note}</span></Tooltip>}
        </div>
      )}
      {state.lines.length > 0 ? (
        <pre className="tc-diff">
          {state.lines.map((l, i) => (
            <div key={i} className={`tc-diff-line ${l.type}`}><span className="cv-ln">{l.type === "add" ? "+" : l.type === "del" ? "−" : ""}</span><code className="cv-code">{l.text || " "}</code></div>
          ))}
        </pre>
      ) : state.note ? null : (
        // 有口径说明（二进制 / 已删除等）时说明本身已交代原因，不再重复一行「无文本变更」
        <div className="tc-diff-empty">本次无文本变更</div>
      )}
      {/* plan-522-2209 R10：文案收敛到 ClampBlock 的「显示 前 N 行」口径。
          diff 不走 ClampBlock 的理由见 InlineDiff 顶部注释：全量行数只有后端知道，
          交给 ClampBlock 会把「已下发的行数」当成总量标注，反而失真。
          这里**不写「共 M 行」也不写「其余未下发」**——后端的 truncated 有两种来源
          （变更行数 > 2000 只置标记、下发行数 > 4000 才真裁剪），前端分不清是哪一种，
          所以只陈述确定的显示行数，把风险如实说明。 */}
      {state.truncated && (
        <div className="tc-diff-trunc">显示 前 {state.lines.length} 行（变更过大，行级 diff 可能不完整）</div>
      )}
    </div>
  );
}

/** plan-282-1441（#7）：数据库工具结果的结构化呈现。
 *
 * 为什么单独做：MCP 工具输出是纯文本，用户难以从中核对"到底执行了什么"。
 * 这里把三样东西清楚分开：
 *   ① 语句（语法高亮的等宽块，可直接复制）
 *   ② 执行摘要（连接、耗时、影响行数 / 返回行数）
 *   ③ 结果表格（前 N 行，超出提示）
 * 语句从调用参数 `args.sql` 取（比从文本里正则抠更可靠）。
 */
function SqlResultView({ tool, args, output }: {
  tool: string;
  args: Record<string, unknown>;
  output: string | null | undefined;
}) {
  const sql = typeof args.sql === "string" ? args.sql : "";
  /** 从工具文本输出里解析摘要行与 markdown 结果表（内置 MCP 的输出格式是稳定的） */
  const parsed = useMemo(() => {
    const text = output ?? "";
    const elapsed = /耗时\s*(\d+)\s*ms/.exec(text)?.[1] ?? null;
    const affected = /影响\s*([\d?]+)\s*行/.exec(text)?.[1] ?? null;
    const conn = /连接\s+([^（(]+)[（(]([\w]+)[）)]/.exec(text);
    // markdown 表格 → 行列
    const lines = text.split("\n");
    const tableStart = lines.findIndex((l) => l.trim().startsWith("|"));
    let columns: string[] = [];
    const rows: string[][] = [];
    if (tableStart >= 0 && lines[tableStart + 1]?.includes("---")) {
      columns = lines[tableStart].split("|").slice(1, -1).map((s) => s.trim());
      for (let i = tableStart + 2; i < lines.length; i++) {
        const l = lines[i].trim();
        if (!l.startsWith("|")) break;
        rows.push(l.split("|").slice(1, -1).map((s) => s.trim()));
      }
    }
    return { elapsed, affected, conn: conn ? { name: conn[1].trim(), kind: conn[2] } : null, columns, rows };
  }, [output]);

  const denied = /⛔|被拒绝/.test(output ?? "");
  const opLabel = tool === "db_query" ? "查询"
    : tool === "db_execute" ? "数据变更"
    : tool === "db_ddl" ? "结构变更"
    : tool === "db_schema" ? "读取表结构"
    : "数据库操作";

  return (
    <div className={`tc-sql${denied ? " denied" : ""}`}>
      <div className="tc-sql-head">
        <span className={`tc-sql-op${tool === "db_ddl" ? " danger" : ""}`}>{opLabel}</span>
        {parsed.conn && <span className="tc-sql-conn">{parsed.conn.name}（{parsed.conn.kind}）</span>}
        {parsed.elapsed && <span className="tc-sql-meta">{parsed.elapsed}ms</span>}
        {parsed.affected != null && <span className="tc-sql-meta">影响 {parsed.affected} 行</span>}
        {parsed.rows.length > 0 && <span className="tc-sql-meta">{parsed.rows.length} 行结果</span>}
      </div>
      {sql && (
        <pre className="tc-sql-code">
          <code>{sql}</code>
          <button
            className="tc-sql-copy"
            type="button"
            title="复制语句"
            onClick={() => { void navigator.clipboard?.writeText(sql); }}
          >
            <IconCopy size={11} />
          </button>
        </pre>
      )}
      {parsed.columns.length > 0 && (
        <div className="tc-sql-table-wrap">
          <table className="tc-sql-table">
            <thead>
              <tr>{parsed.columns.map((c, i) => <th key={i}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {parsed.rows.slice(0, 30).map((r, ri) => (
                <tr key={ri}>{r.map((c, ci) => <td key={ci}>{c}</td>)}</tr>
              ))}
            </tbody>
          </table>
          {parsed.rows.length === 0 && <div className="tc-sql-empty">（无数据）</div>}
        </div>
      )}
      {/* 被权限拒绝 / 出错时，把原因原样展示（不吞掉） */}
      {denied && <div className="tc-sql-denied">{output}</div>}
    </div>
  );
}

/** plan-89-387: 行级 diff 的行数统计（服务端未给 additions/deletions 时兜底） */
function countDiffLines(lines: Array<{ type: "add" | "del" | "ctx" }>): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const l of lines) {
    if (l.type === "add") additions++;
    else if (l.type === "del") deletions++;
  }
  return { additions, deletions };
}

/** v7(H) / plan-89-387: 从工具 args 提取「本次编辑」的兜底行级 diff。
 *
 *  用途：写盘记录尚未落库（运行中）或该 turn 无记录（老数据）时，让展开区仍能看到
 *  本次变更内容；覆盖 fs_write / editor_apply_diff / multi_file_edit 三种写工具。
 *  只使用调用参数（无法拿到写前内容时退化为全量新增行），落库后由权威 diff 覆盖。 */
function _pickStr(obj: Record<string, unknown>, keys: string[]): string {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === "string" && v) return v;
  }
  return "";
}

/** edits 允许是数组，也允许模型把它作为 JSON 字符串传入（与后端容错保持一致）。 */
function _coerceEdits(raw: unknown): Array<Record<string, unknown>> {
  const keep = (v: unknown): Array<Record<string, unknown>> =>
    Array.isArray(v) ? (v.filter((e) => e && typeof e === "object") as Array<Record<string, unknown>>) : [];
  if (typeof raw !== "string") return keep(raw);
  if (!raw.trim()) return [];
  try {
    return keep(JSON.parse(raw));
  } catch {
    return [];
  }
}

function _editPath(e: Record<string, unknown>): string {
  for (const k of ["path", "file_path", "filepath"]) {
    const v = e[k];
    if (typeof v === "string" && v) return v.replace(/\\/g, "/");
  }
  return "";
}

function leafFallbackDiff(leaf: ToolLeaf, path: string | null): Array<{ type: "add" | "del" | "ctx"; text: string }> | null {
  const a = (leaf.args ?? {}) as Record<string, unknown>;
  if (leaf.tool === "fs_write") {
    const c = a.content;
    return typeof c === "string" && c ? simpleLineDiff("", c) : null;
  }
  if (leaf.tool === "editor_apply_diff") {
    const oldT = _pickStr(a, ["old_text", "old_string", "oldString"]);
    const newT = _pickStr(a, ["new_text", "new_string", "newString"]);
    if (!oldT && !newT) return null;
    return simpleLineDiff(oldT, newT);
  }
  if (leaf.tool === "multi_file_edit") {
    const edits = _coerceEdits(a.edits);
    if (!edits.length) return null;
    const rel = (path || "").replace(/\\/g, "/");
    // 优先匹配当前卡片路径；模型漏写 path 或路径写法不同时，若全部 edit 指向
    // 同一文件，直接取第一条（否则展开只能看到「变更加载失败」）。
    let hit = rel
      ? edits.find((e) => {
          const p = _editPath(e);
          return !!p && (p === rel || p.endsWith(rel) || rel.endsWith(p));
        })
      : edits[0];
    if (!hit) {
      const paths = new Set(edits.map(_editPath).filter(Boolean));
      if (paths.size === 1) hit = edits[0];
    }
    if (!hit) return null;
    const oldT = _pickStr(hit, ["old_text", "old_string", "oldString"]);
    const newT = _pickStr(hit, ["new_text", "new_string", "newString", "content"]);
    if (!oldT && !newT) return null;
    return simpleLineDiff(oldT, newT);
  }
  return null;
}

const LeafRow = memo(function LeafRow({ leaf }: { leaf: ToolLeaf }) {
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const setDiffPreview = usePanelStore((s) => s.setDiffPreview);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  // v7(B/H): 订阅运行中实时输出/结果（按 call_key 索引），运行中即可展开看到内容
  const liveOutput = useChatStore((s) => s.runningToolOutput[leaf.callKey]);
  const liveResult = useChatStore((s) => s.runningToolResults[leaf.callKey]);
  // 展开状态按调用身份记忆：新消息使它被并入聚合行、或换段重挂载后，仍保持用户刚展开的样子
  const [expanded, setExpanded] = useFlowExpand(leaf.callKey);
  const path = leafPath(leaf);
  const ok = leaf.ok;
  // plan-474-2105 组 C：先算打断标记——它必须比 ok===false 的失败态更早生效，
  // 否则「已让出等待」（ok=false）会被渲染成红色失败。
  const interruptTag: string | null = interruptTagOf(leaf);
  // v25: 工具伪装写盘（terminal_exec 等带 change_stat）按写操作渲染——可展开行内 diff
  const isWrite = isWriteLeaf(leaf);
  // plan-89-387: 展开后从服务端拿到的「本次变更」统计（行上无 changeStat 时回填展示，
  // 覆盖历史消息缺统计的情况）；同一调用的 args 落库后不再变化，依赖 call_key 即可
  const [loadedStat, setLoadedStat] = useState<{ additions: number; deletions: number } | null>(null);
  const fallbackLines = useMemo(
    () => (isWrite ? leafFallbackDiff(leaf, path) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isWrite, leaf.callKey, path],
  );
  // plan-20261019-diff: 折叠态统计兜底——历史消息缺 change_stat 时，用调用参数
  // 计算本次编辑的 +N -M。只有 editor_apply_diff / multi_file_edit 的参数能精确
  // 表达一次编辑；fs_write 是整篇覆盖，参数只有 after，无法反映删改，不能用它兜底。
  const fallbackStat = useMemo(() => {
    if (!isWrite || !fallbackLines || fallbackLines.length === 0) return null;
    if (leaf.tool !== "editor_apply_diff" && leaf.tool !== "multi_file_edit") return null;
    return countDiffLines(fallbackLines);
  }, [isWrite, leaf.tool, fallbackLines]);
  const hasOutput = (leaf.output && leaf.output.length > 0) || !!leaf.error;
  const isCommand = COMMAND_TOOLS.has(leaf.tool);
  // v7(B): 运行中（ok===null）恒可展开——即使 output 尚未落库，也能看实时输出
  // plan-20261016-ux（问题 4）：命令类工具完成后无输出也可展开，展开区显示「没有输出」提示卡
  const expandable = hasOutput || isWrite || leaf.ok === null || isCommand;
  const grepHits = leaf.tool === "fs_grep" && leaf.output ? parseGrepLines(leaf.output) : [];
  const summary = path ? "" : leafSummary(leaf);
  const showsLive = leaf.ok === null && liveOutput != null && liveOutput.length > 0;
  /** 展开区是否有任何实际内容；没有时渲染提示卡（此前只剩 .tc-output 上下边框贴成的一根细线） */
  const hasBody = showsLive || grepHits.length > 0 || leaf.tool.startsWith("db_") || leaf.tool === "ask_user_question"
    || !!leaf.output || !!leaf.error || (isWrite && path == null && !!liveResult?.change_stat);
  // plan-474-2105 组 C：打断标记优先于失败文案，空态不能说成「执行失败」
  const emptyText = interruptTag === "已转后台"
    ? "命令已转后台继续运行，可用 terminal_bg_status 查看日志"
    : interruptTag === "已让出等待"
      ? "本次等待已让出，可按需重新调用该工具"
      : leaf.ok === null
        ? (isCommand ? "命令执行中，暂时还没有输出" : "正在执行，暂时还没有返回内容")
        : leaf.ok === false
          ? "执行失败，没有返回错误信息"
          : (isCommand ? "命令已执行完成，没有输出" : "执行完成，没有返回内容");

  const openGrepHit = (e: React.MouseEvent, hit: { path: string; line: number }) => {
    e.stopPropagation();
    setPreviewPath(hit.path, hit.line);
    openPanel();
    openTab("files");
  };

  // 点击文件名/徽标：写入工具 = 右面板 Monaco DiffEditor 查看变更（plan-89-387: 带 call_key 取本次编辑）；其余查看文件
  const openFilePreview = async (e: React.MouseEvent, p: string) => {
    e.stopPropagation();
    setPreviewPath(p);
    openPanel();
    openTab("files");
    if (isWrite && leaf.turnId != null) {
      try {
        const d = await api.getFileDiff(leaf.turnId, p, leaf.callKey);
        setDiffPreview({ path: d.path, before: d.before, after: d.after, truncated: d.truncated });
      } catch { /* 回退普通预览 */ }
    }
  };

  const { dir, name } = path ? splitFilePath(path) : { dir: "", name: "" };

  return (
    <div className="tc-node">
      <div className={"tc-row" + (expandable ? " has-output" : "") + (expanded ? " expanded" : "") + (isWrite ? " tc-write" : "")}
          data-tool-state={ok === null ? "running" : interruptTag ? "noted" : ok === false ? "fail" : "done"} onClick={() => expandable && setExpanded(!expanded)}>
        <span className="tc-icon">{toolIcon(leaf.tool)}</span>
        <span className={"tc-verb" + (ok === null ? " text-shine" : "")}>
          {ok === null ? runningVerb(leaf.tool) : toolVerb(leaf.tool)}
        </span>
        {path && (
          <Tooltip content={`${path}（点击查看${isWrite ? "变更" : "文件"}）`}>
            <span className="tc-filelink" onClick={(e) => void openFilePreview(e, path)}>
              <FileIcon name={path} size={16} />
              <span className="tc-filename">{name}</span>
            </span>
          </Tooltip>
        )}
        {path && dir && <Tooltip content={path}><span className="tc-dir">{dir}</span></Tooltip>}
        {!path && toolDisplayName(leaf.tool) && <span className="tc-tool-name">{toolDisplayName(leaf.tool)}</span>}
        {!path && summary && <Tooltip content={summary}><span className="tc-query">{summary}</span></Tooltip>}
        {/* v7(H): 写操作运行中 changeStat 未落库时回退到实时结果；
            plan-89-387: 历史消息缺统计时，展开后按「本次变更」回填行数 */}
        {(() => {
          // plan-20261019-diff: 0/0 一律不展示（旧记录缺统计时继续向后找兜底），
          // 避免出现「+0 -0」这种与实际展开内容矛盾的摘要。
          const stat = [leaf.changeStat, liveResult?.change_stat, loadedStat, fallbackStat]
            .find((s) => s && (s.additions > 0 || s.deletions > 0)) ?? null;
          return stat
            ? <ChangeStat additions={stat.additions} deletions={stat.deletions} />
            : null;
        })()}
        {/* plan-474-2105 组 C：行尾状态槽只有一个——运行中仍是 spinner；打断标记用中性徽标，
            不再与红色失败图标叠加 */}
        {ok === null ? (
          <span className="tc-status wait"><IconSpinner size={11} /></span>
        ) : interruptTag ? (
          <span className="tc-state-tag">{interruptTag}</span>
        ) : ok === false ? (
          <span className="tc-status fail"><IconX size={11} /></span>
        ) : null}
        {expandable ? (
          <span className={"tc-chevron" + (expanded ? " open" : "")}><IconChevronRight size={11} /></span>
        ) : null}
      </div>
      {expandable ? (
        <ChatCollapse open={expanded}>
          <div className="tc-output">
            {isWrite && path && leaf.ok !== false ? (
            <InlineDiff
              turnId={leaf.turnId}
              path={path}
              callKey={leaf.callKey}
              fallbackLines={fallbackLines}
              onStat={leaf.changeStat || liveResult?.change_stat ? undefined : setLoadedStat}
            />
          ) : (
            <>
              {/* v7(H): 写操作但无 path（如 change_stat 路径缺失）时显示实时结果概要 */}
              {isWrite && path == null && liveResult?.change_stat && (
                <pre className="tc-plain tc-live">已变更 +{liveResult.change_stat.additions} -{liveResult.change_stat.deletions}</pre>
              )}
              {/* v7(B): 运行中命令行的实时输出。
                  plan-522-2209 R10：折叠与截断标注统一交给 ClampBlock（头 12 + 尾 4），
                  实时输出的数据源本身是全量，copyText 直接给全文。 */}
              {showsLive && (
                <ClampBlock text={liveOutput!} preClassName="tc-plain tc-live" copyText={liveOutput!} />
              )}
              {/* plan-20261016-ux（问题 4）：没有任何内容时的提示卡（运行中 / 完成 / 失败三态文案） */}
              {!hasBody && (
                <div className="tc-empty" data-state={leaf.ok === null ? "running" : interruptTag ? "noted" : leaf.ok === false ? "fail" : "done"}>
                  {leaf.ok === null ? <IconSpinner size={12} /> : <IconInfo size={14} />}
                  <span>{emptyText}</span>
                </div>
              )}
              {grepHits.length > 0 && (
                <div className="tc-grep-hits">
                  {grepHits.slice(0, 20).map((hit, i) => (
                    <Tooltip key={i} content={hit.path}>
                      <button className="tc-grep-hit" onClick={(e) => openGrepHit(e, hit)}>
                        <span className="tc-grep-path">{hit.path.split(/[\\/]/).pop()}</span>
                        <span className="tc-grep-ln">:{hit.line}</span>
                        <span className="tc-grep-text">{hit.rest.slice(0, 60)}</span>
                      </button>
                    </Tooltip>
                  ))}
                </div>
              )}
              {/* plan-282-1441（#7）：数据库工具（db_*）的结构化呈现——
                  用户必须能看清"AI 执行了什么语句、影响了什么、返回了什么"。
                  语句从 args.sql 取（调用参数），结果从工具结果的结构化数据取。 */}
              {leaf.tool.startsWith("db_") && (
                <SqlResultView tool={leaf.tool} args={leaf.args ?? {}} output={leaf.output} />
              )}
              {/* 对齐图四、图五：ask_user_question 展开为结构化问答详情。
                  plan-278-1391: 改为扁平文本（不再套独立卡片），与 fs_read/文件编辑展开同构；
                  底部原始 JSON 输出由下方通用分支对 ask_user_question 跳过。 */}
              {leaf.tool === "ask_user_question" && (
                (() => {
                  const qs = (leaf.args?.questions as Array<{ question?: string }> | undefined) || [];
                  /** 解析工具结果中的答案映射（形如 `用户回答:\n{ "0": "..." }`）。 */
                  const parseAnswers = (): Record<string, string> => {
                    if (!leaf.output) return {};
                    const jsonMatch = leaf.output.match(/\{[\s\S]*\}/);
                    if (!jsonMatch) return {};
                    try {
                      const parsed = JSON.parse(jsonMatch[0]);
                      return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
                    } catch {
                      return {};
                    }
                  };
                  const answerMap = parseAnswers();
                  const hasAnswers = Object.keys(answerMap).length > 0;
                  // 后端最多只问 _MAX_QUESTIONS 题，多出的会被截掉、从未真正提问，而参数里仍是模型写的完整列表。
                  // 有回答记录时只列出有答案的题（提问向导要求每道实际问到的题都作答），避免把没问过的题显示成「已回答」
                  const rows = qs
                    .map((q, idx) => ({ q, idx, hit: answerMap[String(idx)] ?? answerMap[String(idx + 1)] }))
                    .filter((r) => !hasAnswers || r.hit != null);
                  // 没有回答记录时按工具状态给占位：等待中 / 取消或超时 都不能显示成「已回答」
                  const emptyHint = leaf.ok === null ? "等待回答…" : leaf.ok === false ? "未回答" : "已回答";
                  return (
                    <div className="tc-question-qa">
                      {rows.map(({ q, idx, hit }) => (
                        <div key={idx} className="tc-question-qa-item">
                          <span className="tc-qa-label">问题 {idx + 1}</span>
                          <span className="tc-qa-q">{q.question || ""}</span>
                          <span className="tc-qa-label">回答</span>
                          <span className={"tc-qa-val" + (hit == null ? " is-empty" : "")}>
                            {hit == null ? emptyHint : String(hit)}
                          </span>
                        </div>
                      ))}
                    </div>
                  );
                })()
              )}
              {/* plan-278-1391: ask_user_question 的 output 是 `用户回答:\n{json}`，
                  问答已由上方结构化列表呈现，这里跳过原文，避免重复且可读性差的 JSON。 */}
              {/* plan-20261016-ux（问题 5）：读文件类工具只要有路径一律走代码视图——此前按扩展名白名单判断，
                  xml / yml / sql / properties 等（子代理调研 MyBatis 映射、配置文件时最常见）都退化成纯文本 */}
              {leaf.output && leaf.tool !== "ask_user_question" && (path && (FILE_CONTENT_TOOLS.has(leaf.tool) || /\.(ts|tsx|js|jsx|py|json|md|css|html|go|rs|java|c|cpp|sh)$/i.test(path)) ? (
                <CodeView path={path} text={leaf.output} />
              ) : (
                // plan-522-2209 R10：统一走 ClampBlock；copyText 传完整 output，
                // 兑现「折叠只影响显示、复制仍是完整输出」的承诺。
                <ClampBlock text={leaf.output} preClassName="tc-plain" copyText={leaf.output} />
              ))}
              {/* plan-474-2105 组 C：「已让出等待」不是错误，用普通文本展示说明，不套红色错误块 */}
              {leaf.error && (interruptTag
                ? <ClampBlock text={leaf.error} preClassName="tc-plain" copyText={leaf.error} />
                : <pre className="tc-error-output">{leaf.error}</pre>)}
            </>
          )}
          </div>
        </ChatCollapse>
      ) : null}
    </div>
  );
});

/** v19: 操作合并行（两次思考间的连续非写操作）：
 *  运行中——动词区每 1.2s 轮播最新操作（translateY 滚动动效）；
 *  完成后——「已执行 N 个操作（…）」摘要行，可展开查看全部明细。 */
const ActionClusterRow = memo(function ActionClusterRow({ leaves }: { leaves: ToolLeaf[] }) {
  const [expanded, setExpanded] = useFlowGroupExpand(`group:${leaves[0].callKey}`, leaves.map((l) => l.callKey));
  const running = leaves.some((l) => l.ok === null);
  // plan-474-2105 组 C：转后台 / 让出等待不是失败，聚合行不要因此亮红叉
  const failed = leaves.some((l) => l.ok === false && !interruptTagOf(l));
  // 问题2: 轮播只在未执行完成的命令（ok===null）间切换，不再切到已完成命令
  const runningLeaves = leaves.filter((l) => l.ok === null);
  const rollingPool = runningLeaves.length > 0 ? runningLeaves : leaves;
  // 轮播索引：运行中循环展示最近若干条
  // S8c：轮播节拍改用共享秒级 ticker（原为各自 1200ms setInterval）。
  const tick = useRunningTicker(running);

  const summary = useMemo(() => {
    const search = leaves.filter((l) => SEARCH_TOOLS.has(l.tool)).length;
    const run = leaves.filter((l) => RUN_TOOLS.has(l.tool)).length;
    const read = leaves.filter((l) => ["fs_read", "fs_list", "view_image", "read_attachment"].includes(l.tool)).length;
    const web = leaves.filter((l) => l.tool.startsWith("browser_")).length;
    const other = leaves.length - search - run - read - web;
    const parts: string[] = [];
    if (web > 0) parts.push(`${web} 网页操作`);
    if (search > 0) parts.push(`${search} 搜索`);
    if (read > 0) parts.push(`${read} 读取`);
    if (run > 0) parts.push(`${run} 运行`);
    if (other > 0) parts.push(`${other} 调用`);
    return parts.join(", ") || `${leaves.length} 次调用`;
  }, [leaves]);

  const rolling = rollingPool[Math.min(tick % Math.max(1, rollingPool.length), rollingPool.length - 1)];
  const rollingPath = rolling ? leafPath(rolling) : null;
  const rollingText = rolling
    ? `${runningVerb(rolling.tool)} ${rollingPath ? splitFilePath(rollingPath).name : leafSummary(rolling)}`.trim()
    : "";

  return (
    <div className="tc-node tc-explore">
      <div className={"tc-row has-output tc-explore-row" + (expanded ? " expanded" : "")}
          data-tool-state={running ? "running" : failed ? "fail" : "done"} onClick={() => setExpanded(!expanded)}>
        <span className="tc-icon"><IconOperationCluster size={16} /></span>
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
      <ChatCollapse open={expanded}>
        <div className="tc-explore-detail">
          {leaves.map((leaf, j) => <LeafRow key={j} leaf={leaf} />)}
        </div>
      </ChatCollapse>
    </div>
  );
});

/** v19: 同文件连续写合并行：文件徽标 + 累加 +N -M（滚动动效），展开看每次写明细（各自内联 diff） */
const WriteMergedRow = memo(function WriteMergedRow({ leaves }: { leaves: ToolLeaf[] }) {
  const [expanded, setExpanded] = useFlowGroupExpand(`group:${leaves[0].callKey}`, leaves.map((l) => l.callKey));
  const running = leaves.some((l) => l.ok === null);
  // plan-474-2105 组 C：同上——打断标记不计入失败，避免合并行亮红叉
  const failed = leaves.some((l) => l.ok === false && !interruptTagOf(l));
  const path = leafPath(leaves[0]) ?? "";
  // plan-20261019-diff: 合并行按「changeStat → 调用参数兜底」取每行统计。
  // 历史消息缺 change_stat 时，折叠态整行此前不显示 +N -M。
  const statOf = (l: ToolLeaf): { additions: number; deletions: number } | null => {
    if (l.changeStat && (l.changeStat.additions > 0 || l.changeStat.deletions > 0)) return l.changeStat;
    if (l.tool === "editor_apply_diff" || l.tool === "multi_file_edit") {
      const fb = leafFallbackDiff(l, leafPath(l));
      if (fb && fb.length > 0) {
        const c = countDiffLines(fb);
        if (c.additions > 0 || c.deletions > 0) return c;
      }
    }
    return null;
  };
  const add = leaves.reduce((s, l) => s + (statOf(l)?.additions ?? 0), 0);
  const del = leaves.reduce((s, l) => s + (statOf(l)?.deletions ?? 0), 0);
  const { dir, name } = path ? splitFilePath(path) : { dir: "", name: "" };

  return (
    <div className="tc-node">
      <div className={"tc-row has-output tc-write" + (expanded ? " expanded" : "")}
          data-tool-state={running ? "running" : failed ? "fail" : "done"} onClick={() => setExpanded(!expanded)}>
        <span className="tc-icon"><IconFileWrite size={16} /></span>
        <span className={"tc-verb" + (running ? " text-shine" : "")}>{running ? "正在编辑" : `已编辑 ×${leaves.length}`}</span>
        {path && (
          <span className="tc-filelink">
            <FileIcon name={path} size={16} />
            <span className="tc-filename">{name}</span>
          </span>
        )}
        {path && dir && <Tooltip content={path}><span className="tc-dir">{dir}</span></Tooltip>}
        {(add > 0 || del > 0) && <ChangeStat additions={add} deletions={del} />}
        {running && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {!running && failed && <span className="tc-status fail"><IconX size={11} /></span>}
        <span className={"tc-chevron" + (expanded ? " open" : "")}><IconChevronRight size={11} /></span>
      </div>
      <ChatCollapse open={expanded}>
        <div className="tc-explore-detail">
          {leaves.map((leaf, j) => <LeafRow key={j} leaf={leaf} />)}
        </div>
      </ChatCollapse>
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
              // plan-20261021-subq-row：子会话提问是单叶子条目（时间线单独生成，不会并入聚合行）；
              // plan-20261022-release-ci：子会话汇报 / 结论同一行版式
              : SUBSESSION_NOTICE_TOOLS.has(n.leaf.tool)
                ? <SubsessionQuestionRow key={`l${i}`} leaf={n.leaf} />
                : <LeafRow key={`l${i}`} leaf={n.leaf} />,
      )}
    </div>
  );
});
