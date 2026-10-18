/** Git 面板（plan-20261017-goal12 项 5）：右侧面板里的常规 Git 操作。
 *
 * 两个视图：「变更」——冲突（合并进行中时置顶）→ 已暂存 → 未暂存 → 未跟踪 → 提交框；
 *          「历史」——提交列表（分支 / 标签装饰）→ 单个提交的信息、变更文件与差异。
 * 点击任一文件在下方查看差异；丢弃改动、删除分支等不可逆操作先确认。
 * 状态在打开面板、完成操作、AI 结束一轮改动后刷新。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type GitBranchesOut, type GitCommitDetailOut, type GitDiffOut, type GitLogEntry, type GitStatusOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { ConfirmDialog } from "../ConfirmDialog";
import { Button, Checkbox, Dialog, Empty, FormDialog, IconButton, Input, Menu, Popover, Segmented, Tooltip } from "../ui";
import { IconAlertTriangle, IconArrowDown, IconArrowUp, IconCheck, IconChevronDown, IconChevronRight, IconDownload, IconGitBranch, IconGlobe, IconMoreHorizontal, IconPlus, IconRefresh, IconTrash, IconX } from "../icons";
import { FileIcon } from "../iconkit/FileIcon";
import { highlightLines } from "../chat/CodeView";

type Section = "conflicts" | "staged" | "unstaged" | "untracked";

/** 变更列表的一行：staged / unstaged / untracked 按路径合并后的唯一记录。
 *  勾选只改变这一行的暂存状态，不会让行在分组之间移动。 */
type ChangeRow = {
  path: string;
  code: string;
  staged: boolean;
  oldPath?: string | null;
  /** 冲突文件：先选择保留哪一边，暂不可勾选 */
  conflict?: boolean;
};
type Pending =
  | { kind: "discard"; paths: string[]; untracked: string[]; label: string }
  | { kind: "delete-branch"; name: string }
  | null;

/** 行尾状态字母：M 修改 / A 新增 / D 删除 / R 重命名 / U 未跟踪或冲突。
 *  界面只显示单个字母（等宽、同宽列），中文含义放在 Tooltip，避免彩色文字块糊成一片。 */
const stateLetter = (code: string) => (code === "?" ? "U" : code);
const STATE_LABEL: Record<string, string> = {
  M: "已修改", A: "新增", D: "已删除", R: "重命名", C: "复制", T: "类型变更",
  U: "冲突", "?": "未跟踪",
};

function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 相对时间（近期）：刚刚 / N 分钟前 / N 小时前 / N 天前；超过 30 天回落到日期。 */
function relTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const min = Math.round((Date.now() - t) / 60_000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.round(h / 24);
  return d < 30 ? `${d} 天前` : fmtDate(iso);
}

/** 提交信息正文：首行之后的部分（首行即标题）。不用 subject.length 截取——标题被 trim 过，截取位置会错位。 */
function commitBodyOf(message: string): string {
  return message.split("\n").slice(1).join("\n").trim();
}

function splitPath(p: string): { dir: string; name: string } {
  const i = p.lastIndexOf("/");
  return i < 0 ? { dir: "", name: p } : { dir: p.slice(0, i + 1), name: p.slice(i + 1) };
}

/** 统一 diff 渲染：+ / - 行着色，hunk 头单独成行；代码部分与代码视图共用同一套语法高亮。
 *  按连续同类型行整块送进 hljs 再按行切回：跨行的块注释、模板字符串不会着色错。
 *  行首的 +/-/空格前缀单独成一列，不参与高亮。 */
function DiffView({ diff, path }: { diff: GitDiffOut; path: string }) {
  const htmlLines = useMemo(() => {
    if (diff.binary || !diff.text) return null;
    const raw = diff.text.split("\n");
    const out: (string | null)[] = new Array(raw.length).fill(null);
    const kindOf = (l: string) =>
      l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") || l.startsWith("index ")
        ? "meta"
        : l.startsWith("@@") ? "hunk"
        : l.startsWith("+") ? "add"
        : l.startsWith("-") ? "del" : "ctx";
    let i = 0;
    while (i < raw.length) {
      const k = kindOf(raw[i]);
      if (k === "meta" || k === "hunk") { i += 1; continue; }
      let j = i;
      while (j < raw.length && kindOf(raw[j]) === k) j += 1;
      // 去掉行首的 +/-/空格前缀再高亮，否则语法分析会把符号当成运算符
      const seg = raw.slice(i, j).map((l) => l.slice(1));
      const hl = highlightLines(path, seg);
      if (hl && hl.length === seg.length) {
        for (let x = 0; x < seg.length; x += 1) out[i + x] = hl[x];
      }
      i = j;
    }
    return out;
  }, [diff, path]);

  if (diff.binary) return <div className="gp-diff-note">二进制文件，无法显示差异</div>;
  if (!diff.text) return <div className="gp-diff-note">没有可显示的差异</div>;
  const lines = diff.text.split("\n");
  return (
    <div className="gp-diff" role="region" aria-label="差异">
      {lines.map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")
          ? "is-meta"
          : line.startsWith("@@") ? "is-hunk"
          : line.startsWith("+") ? "is-add"
          : line.startsWith("-") ? "is-del" : "";
        // meta / hunk 不是代码内容，不着色、也不剥前缀
        if (cls === "is-meta" || cls === "is-hunk") {
          return <div key={i} className={`gp-diff-line ${cls}`}>{line || " "}</div>;
        }
        const html = htmlLines?.[i] ?? null;
        const sign = line.charAt(0);
        const code = line.slice(1);
        return (
          <div key={i} className={`gp-diff-line ${cls}`}>
            <span className="gp-diff-sign" aria-hidden>{sign === " " ? "" : sign}</span>
            {html
              ? <code className="gp-diff-code hljs" dangerouslySetInnerHTML={{ __html: html || " " }} />
              : <code className="gp-diff-code">{code || " "}</code>}
          </div>
        );
      })}
      {diff.truncated && <div className="gp-diff-note">差异过大，已截断显示</div>}
    </div>
  );
}

export function GitPanel({ visible = true }: { visible?: boolean }) {
  const projectId = useChatStore((s) => s.currentProjectId);
  const isRunning = useChatStore((s) => s.isRunning);

  const [status, setStatus] = useState<GitStatusOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ path: string; staged: boolean } | null>(null);
  const [diff, setDiff] = useState<GitDiffOut | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [amend, setAmend] = useState(false);
  const [branches, setBranches] = useState<GitBranchesOut | null>(null);
  const [branchOpen, setBranchOpen] = useState(false);
  const [newBranchOpen, setNewBranchOpen] = useState(false);
  const [newBranch, setNewBranch] = useState("");
  const [collapsed, setCollapsed] = useState<Record<Section, boolean>>({ conflicts: false, staged: false, unstaged: false, untracked: false });
  const [pending, setPending] = useState<Pending>(null);
  /** 切换分支被本地改动阻止时，列出会被覆盖的文件，交由用户选择处理方式 */
  const [dirtySwitch, setDirtySwitch] = useState<{ name: string; files: string[] } | null>(null);
  const [noticeTone, setNoticeTone] = useState<"info" | "warn">("info");
  // 视图：变更（工作区 + 提交框）/ 历史（提交列表 → 单个提交的文件与差异）
  const [view, setView] = useState<"changes" | "history">("changes");
  const [history, setHistory] = useState<GitLogEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyMore, setHistoryMore] = useState(false);
  const [openSha, setOpenSha] = useState<string | null>(null);
  const [commit, setCommit] = useState<GitCommitDetailOut | null>(null);
  const [commitLoading, setCommitLoading] = useState(false);
  const [commitFile, setCommitFile] = useState<string | null>(null);
  const [commitDiff, setCommitDiff] = useState<GitDiffOut | null>(null);
  const [commitDiffLoading, setCommitDiffLoading] = useState(false);
  const branchBtnRef = useRef<HTMLButtonElement>(null);
  const seq = useRef(0);
  const histSeq = useRef(0);
  const commitSeq = useRef(0);
  const diffSeq = useRef(0);
  const workDiffSeq = useRef(0);
  const HISTORY_PAGE = 40;

  /** 读取状态并返回最新结果（写操作后据此判断当前选中的文件是否仍有改动） */
  const refresh = useCallback(async (): Promise<GitStatusOut | null> => {
    if (projectId == null) return null;
    const mine = ++seq.current;
    setLoading(true);
    try {
      const st = await api.gitStatus(projectId);
      if (mine !== seq.current) return null;
      setStatus(st);
      setError(null);
      return st;
    } catch (e) {
      if (mine !== seq.current) return null;
      setStatus(null);
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
      return null;
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [projectId]);

  /** 读取历史第一页（reset）或追加下一页。以 skip 翻页，不依赖页内游标。 */
  const loadHistory = useCallback(async (reset: boolean) => {
    if (projectId == null) return;
    const mine = ++histSeq.current;
    setHistoryLoading(true);
    try {
      const skip = reset ? 0 : history.length;
      const page = await api.gitLog(projectId, HISTORY_PAGE, skip);
      if (mine !== histSeq.current) return;
      setHistory((prev) => (reset ? page : [...prev, ...page]));
      setHistoryMore(page.length === HISTORY_PAGE);
    } catch (e) {
      if (mine !== histSeq.current) return;
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      setHistoryLoading(false);
    }
  }, [projectId, history.length]);

  const closeCommit = () => {
    commitSeq.current++;
    diffSeq.current++;
    setOpenSha(null);
    setCommit(null);
    setCommitLoading(false);
    setCommitFile(null);
    setCommitDiff(null);
    setCommitDiffLoading(false);
  };

  const openCommit = async (sha: string) => {
    if (projectId == null) return;
    closeCommit();
    const mine = ++commitSeq.current;
    setOpenSha(sha);
    setCommitLoading(true);
    try {
      const detail = await api.gitCommitDetail(projectId, sha);
      if (mine === commitSeq.current) setCommit(detail);
    } catch (e) {
      if (mine === commitSeq.current) setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      if (mine === commitSeq.current) setCommitLoading(false);
    }
  };

  const openCommitFile = async (path: string) => {
    if (projectId == null || openSha == null) return;
    if (commitFile === path) { setCommitFile(null); setCommitDiff(null); return; }
    const mine = ++diffSeq.current;
    setCommitFile(path);
    setCommitDiff(null);
    setCommitDiffLoading(true);
    try {
      const d = await api.gitCommitDiff(projectId, openSha, path);
      if (mine === diffSeq.current) setCommitDiff(d);
    } catch (e) {
      if (mine === diffSeq.current) setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      if (mine === diffSeq.current) setCommitDiffLoading(false);
    }
  };

  const refreshBranches = useCallback(async () => {
    if (projectId == null) return;
    try { setBranches(await api.gitBranches(projectId)); } catch { setBranches(null); }
  }, [projectId]);

  // 打开 / 切换项目时刷新；面板隐藏时不请求。历史只在历史视图下才读取
  useEffect(() => {
    if (!visible) return;
    setSelected(null);
    setDiff(null);
    setHistory([]);
    closeCommit();
    void refresh();
  }, [projectId, visible, refresh]);

  useEffect(() => {
    if (!visible || view !== "history") return;
    void loadHistory(true);
    // 仅在项目 / 视图切换时整页重读；翻页由「加载更多」负责
  }, [projectId, visible, view]);

  // AI 一轮结束（运行态回落）后，工作区很可能已变化：刷新一次
  const wasRunning = useRef(isRunning);
  useEffect(() => {
    if (wasRunning.current && !isRunning && visible) void refresh();
    wasRunning.current = isRunning;
  }, [isRunning, visible, refresh]);

  useEffect(() => {
    if (branchOpen) void refreshBranches();
  }, [branchOpen, refreshBranches]);

  /** 执行一个写操作：无论成败都刷新状态（部分成功时，如提交成功、推送失败，列表必须反映已落盘的结果）；
   *  失败以提示条呈现，不打断浏览。refresh 成功会清空 error，因此失败文案在刷新之后写入。 */
  const run = async (label: string, fn: () => Promise<string | void>) => {
    if (projectId == null) return;
    setBusy(label);
    setNotice(null);
    setError(null);
    let failure: string | null = null;
    try {
      const out = await fn();
      if (out) setNotice(out);
    } catch (e) {
      failure = e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e);
    }
    const st = await refresh();
    if (failure !== null) setError(failure);
    if (selected && st) {
      const still = st.unstaged.some((e) => e.path === selected.path) || st.staged.some((e) => e.path === selected.path);
      if (!still) { workDiffSeq.current++; setSelected(null); setDiff(null); setDiffLoading(false); }
      else {
        // 暂存状态可能已变：差异要跟着切到新的一侧（已暂存 / 未暂存），否则展示的是过期内容
        const staged = st.staged.some((e) => e.path === selected.path);
        if (staged !== selected.staged) setSelected({ path: selected.path, staged });
        void loadDiff(selected.path, staged);
      }
    }
    setBusy(null);
  };

  /** 读取某个文件的差异。只保留最后一次请求的结果：快速切换文件时晚到的旧结果不会覆盖当前选中。 */
  const loadDiff = async (path: string, staged: boolean) => {
    if (projectId == null) return;
    const mine = ++workDiffSeq.current;
    setDiffLoading(true);
    try {
      const d = await api.gitDiff(projectId, path, staged);
      if (mine === workDiffSeq.current) setDiff(d);
    } catch (e) {
      if (mine !== workDiffSeq.current) return;
      setDiff(null);
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      if (mine === workDiffSeq.current) setDiffLoading(false);
    }
  };

  const openDiff = async (path: string, staged: boolean) => {
    if (projectId == null) return;
    // 再次点击同一行 = 收起：差异就地展开在该行下方，不需要滚到列表末尾
    if (selected?.path === path) {
      workDiffSeq.current++;  // 作废仍在途的读取，避免晚到的结果把已收起的差异又展开
      setSelected(null);
      setDiff(null);
      setDiffLoading(false);
      return;
    }
    setSelected({ path, staged });
    await loadDiff(path, staged);
  };

  const toggleSection = (s: Section) => setCollapsed((c) => ({ ...c, [s]: !c[s] }));

  /** 文件首次出现时所属的分组。“未跟踪文件”被勾选暂存后会出现在 staged 列表里，
   *  若按当前 git 状态重新分组，它就会在同一瞬间「跳」进「更改」组；
   *  这里记录归属，勾选只改选中态，不改变它留在哪一组。 */
  const groupOf = useRef<Map<string, "tracked" | "untracked">>(new Map());
  // 切换项目（或工作树）时分组归属重新学习
  useEffect(() => { groupOf.current.clear(); }, [projectId]);

  const syncDisabled = busy != null || !status;

  /** 两组变更各自合并，组内勾选只改状态、不改变行位置：
   *  - trackedRows：git 已跟踪文件的改动（已暂存 + 未暂存按同一路径合并成一行）；
   *  - untrackedRows：从未被 git 跟踪的新文件，单独一组，不和已跟踪的改动混在一起。
   *  冲突文件不入组，单独置顶处理。 */
  const { trackedRows, untrackedRows, conflictRows } = useMemo(() => {
    if (!status) return { trackedRows: [] as ChangeRow[], untrackedRows: [] as ChangeRow[], conflictRows: [] as ChangeRow[] };
    const merged = new Map<string, ChangeRow>();
    for (const e of status.staged) {
      merged.set(e.path, { path: e.path, code: e.code, staged: true, oldPath: e.old_path });
    }
    for (const e of status.unstaged) {
      const hit = merged.get(e.path);
      // 部分暂存：仍算「已加入本次提交」，状态字母按工作区最新改动显示
      if (hit) hit.code = e.code;
      else merged.set(e.path, { path: e.path, code: e.code, staged: false, oldPath: e.old_path });
    }

    const tracked: ChangeRow[] = [];
    const untracked: ChangeRow[] = [];
    // 首次见到的路径按来源登记归属；已登记的（例如刚被暂存的未跟踪文件）沿用原归属
    for (const row of merged.values()) {
      const known = groupOf.current.get(row.path);
      if (known === "untracked") untracked.push(row);
      else {
        if (!known) groupOf.current.set(row.path, "tracked");
        tracked.push(row);
      }
    }
    for (const e of status.untracked) {
      if (merged.has(e.path)) continue;
      // 以 / 结尾的是目录（--untracked-files=all 下不会出现，旧后端可能仍有）：不当文件列出
      if (e.path.endsWith("/")) continue;
      const known = groupOf.current.get(e.path);
      const row: ChangeRow = { path: e.path, code: "?", staged: false };
      if (known === "tracked") tracked.push(row);
      else {
        if (!known) groupOf.current.set(e.path, "untracked");
        untracked.push(row);
      }
    }

    // 已不存在的路径不再占位，避免同名新文件沿用旧归属
    const alive = new Set([...merged.keys(), ...status.untracked.map((e) => e.path)]);
    for (const p of Array.from(groupOf.current.keys())) {
      if (!alive.has(p)) groupOf.current.delete(p);
    }

    // 按文件名排序（同名再按完整路径）：按路径排会把同一目录的文件堆在一起，
    // 找某个文件名时要逐段读目录，几百条时几乎无法扫读。
    const byName = (a: ChangeRow, b: ChangeRow) =>
      splitPath(a.path).name.localeCompare(splitPath(b.path).name, "zh") ||
      a.path.localeCompare(b.path);
    const conflicts = status.conflicts.map((e): ChangeRow => ({
      path: e.path, code: e.code, staged: false, oldPath: e.old_path, conflict: true,
    }));
    return {
      trackedRows: tracked.sort(byName),
      untrackedRows: untracked.sort(byName),
      conflictRows: conflicts,
    };
  }, [status]);

  const stagedCount =
    trackedRows.filter((r) => r.staged).length + untrackedRows.filter((r) => r.staged).length;

  /** 单行勾选：只动这一个文件，且不改变它所在的分组 */
  const toggleRow = (r: ChangeRow) => void run(r.staged ? "unstage" : "stage", async () => {
    if (projectId == null) return;
    if (r.staged) await api.gitUnstage(projectId, [r.path]);
    else await api.gitStage(projectId, [r.path]);
  });

  /** 一组内的全选 / 全不选 */
  const toggleAllRows = (rows: ChangeRow[], stage: boolean) =>
    void run(stage ? "stage-all" : "unstage-all", async () => {
      if (projectId == null) return;
      const targets = rows.filter((r) => (stage ? !r.staged : r.staged)).map((r) => r.path);
      if (targets.length === 0) return;
      if (stage) await api.gitStage(projectId, targets);
      else await api.gitUnstage(projectId, targets);
    });

  const canCommit = message.trim().length > 0 && (stagedCount > 0 || amend) && !busy;

  const doCommit = () => run("commit", async () => {
    if (projectId == null) return;
    const res = await api.gitCommit(projectId, message.trim(), amend);
    setMessage("");
    setAmend(false);
    void loadHistory(true);
    await refresh();
    return `已提交 ${res.sha}`;
  });

  /** 提交并推送：一次完成，不需要再去顶栏找推送入口 */
  const doCommitAndPush = () => run("commit-push", async () => {
    if (projectId == null) return;
    await api.gitCommit(projectId, message.trim(), amend);
    setMessage("");
    setAmend(false);
    void loadHistory(true);
    const out = await api.gitSync(projectId, "push");
    await refresh();
    return out.output || "已提交并推送";
  });

  const doDiscard = (p: Extract<Pending, { kind: "discard" }>) => run("discard", async () => {
    if (projectId == null) return;
    await api.gitDiscard(projectId, p.paths, p.untracked);
    setPending(null);
    return "已丢弃改动";
  });

  const doDeleteBranch = (name: string) => run("branch", async () => {
    if (projectId == null) return;
    const out = await api.gitBranch(projectId, "delete", name);
    setPending(null);
    void refreshBranches();
    return out.output;
  });

  const conflictCount = status?.conflicts.length ?? 0;
  /** 变更总数：分段控件的角标与提交区提示共用同一口径 */
  const dirtyCount = trackedRows.length + untrackedRows.length + conflictRows.length;
  const merging = status?.state === "merging";

  /** 单行：勾选框表示是否加入本次提交；状态字母紧跟文件名。
   *  差异就在被点击的这一行下方展开（不再跑到列表末尾），文件再多也不用滑到底。
   *  行内不再放「暂存 / 取消暂存」按钮——勾选框本身就是唯一入口。 */
  const changeRow = (r: ChangeRow) => {
    const { dir, name } = splitPath(r.path);
    const active = selected?.path === r.path;
    const letter = stateLetter(r.code);
    const label = STATE_LABEL[r.code] ?? r.code;
    return (
      <div key={r.path} className="gp-row-wrap">
        <div className={`gp-row${active ? " is-active" : ""}${r.conflict ? " is-conflict" : ""}`}>
        <Checkbox
          className="gp-row-check"
          checked={r.staged}
          disabled={busy != null || r.conflict}
          aria-label={r.staged ? `从本次提交移除 ${name}` : `加入本次提交 ${name}`}
          onChange={() => toggleRow(r)}
        />
        <button type="button" className="gp-row-main" onClick={() => void openDiff(r.path, r.staged)}>
          <span className="gp-file-ic"><FileIcon name={name} size={15} /></span>
          <span className="gp-name">{name}</span>
          {dir && <span className="gp-dir">{dir}</span>}
          {r.oldPath && <span className="gp-dir">← {r.oldPath}</span>}
          {/* 状态字母紧跟文件名/目录之后，不再被推到行尾 */}
          <Tooltip content={label} side="top">
            <span className={`gp-state is-${letter.toLowerCase()}`} aria-label={label}>{letter}</span>
          </Tooltip>
        </button>
        <span className="gp-row-ops">
          {!r.conflict && (
            <Tooltip content="丢弃改动" side="top">
              <IconButton icon={<IconTrash size={13} />} aria-label="丢弃改动" tone="danger" disabled={busy != null}
                onClick={() => setPending({
                  kind: "discard", paths: [r.path], untracked: r.code === "?" ? [r.path] : [], label: r.path,
                })} />
            </Tooltip>
          )}
        </span>
        </div>
        {active && (
          <div className="gp-diff-inline">
            {diffLoading
              ? <div className="gp-diff-note">读取差异…</div>
              : diff && diff.path === r.path
                ? <DiffView diff={diff} path={r.path} />
                : <div className="gp-diff-note">没有可显示的差异</div>}
          </div>
        )}
      </div>
    );
  };

  /** 一组变更：组头 = 全选框 + 标题与计数 + 批量菜单，组内逐行列出。
   *  「更改」与「未跟踪文件」是两类不同对象（已跟踪文件的改动 / 从未纳入 git 的新文件），
   *  分组固定；组内勾选只改是否加入本次提交，不会把行搬到别的组。 */
  const changeGroup = (opts: {
    key: "staged" | "untracked";
    title: string;
    rows: ChangeRow[];
    /** 整组都是未跟踪文件：丢弃时走「删除新文件」而不是检出还原 */
    untracked: boolean;
  }) => {
    const { key, title, rows } = opts;
    if (rows.length === 0) return null;
    const picked = rows.filter((r) => r.staged).length;
    const all = picked === rows.length;
    const some = picked > 0 && !all;
    const open = !collapsed[key];
    return (
      <div className="gp-section" key={key}>
        <div className="gp-section-head">
          <Checkbox
            className="gp-section-check"
            checked={all}
            indeterminate={some}
            disabled={busy != null}
            aria-label={all ? `全部移出本次提交：${title}` : `全部加入本次提交：${title}`}
            onChange={() => toggleAllRows(rows, !all)}
          />
          <button type="button" className="gp-section-title" aria-expanded={open} onClick={() => toggleSection(key)}>
            {open ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
            <span>{title}</span>
            <span className="gp-count">{rows.length}</span>
          </button>
          <Menu
            align="end"
            trigger={
              <IconButton icon={<IconMoreHorizontal size={14} />} aria-label={`${title}的批量操作`}
                disabled={busy != null} />
            }
            entries={[
              { key: "stage-all", label: "全部加入本次提交", disabled: all, onSelect: () => toggleAllRows(rows, true) },
              // 分隔线放在危险项之前：本项之后分组
              { key: "unstage-all", label: "全部移出本次提交", disabled: picked === 0, divider: true, onSelect: () => toggleAllRows(rows, false) },
              {
                key: "discard-all", label: "丢弃全部改动…", danger: true,
                onSelect: () => setPending({
                  kind: "discard",
                  paths: opts.untracked ? [] : rows.map((r) => r.path),
                  untracked: opts.untracked ? rows.map((r) => r.path) : [],
                  label: `${title}（${rows.length} 个文件）`,
                }),
              },
            ]}
          />
        </div>
        {open && <div className="gp-section-body">{rows.map(changeRow)}</div>}
      </div>
    );
  };

  /** 拉取：冲突不当作失败——刷新状态并切到变更视图，冲突区会置顶 */
  const doPull = () => run("pull", async () => {
    if (projectId == null) return;
    const res = await api.gitSync(projectId, "pull");
    if (res.conflict) {
      setView("changes");
      setNoticeTone("warn");
      return `拉取产生 ${res.conflicts?.length ?? 0} 个冲突文件，请在「冲突」区逐个选择保留版本后再提交。`;
    }
    setNoticeTone("info");
    return res.output;
  });

  /** 切换分支：被本地改动阻止时交给弹窗选择「暂存后切换」或「放弃改动后切换」。
   *  暂存后恢复若有冲突，同样留在变更视图里处理。 */
  const doCheckout = async (name: string, onDirty?: "stash" | "discard") => {
    if (projectId == null) return;
    setBusy("checkout");
    setError(null);
    setNotice(null);
    try {
      const res = await api.gitBranch(projectId, "checkout", name, { onDirty });
      if (res.switched === false && res.reason === "dirty") {
        setDirtySwitch({ name, files: res.files ?? [] });
        return;
      }
      setDirtySwitch(null);
      if (res.conflict) {
        setView("changes");
        setNoticeTone("warn");
        setNotice(`已切换到 ${name}，但暂存的改动恢复时产生 ${res.conflicts?.length ?? 0} 个冲突，请在「冲突」区处理。`);
      } else if (res.output) {
        setNoticeTone("info");
        setNotice(res.output);
      }
      closeCommit();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      setBusy(null);
    }
  };

  const branchName = status?.detached ? `游离 ${status.oid ?? ""}` : status?.branch ?? "—";

  /** 同步操作收进一枚菜单：抓取 / 拉取 / 推送不再各占一个文字按钮，顶栏留给分支与刷新。 */
  /** 顶栏四个同步动作：全部以图标按钮平铺，不折叠。
   *  依据：这四个动作同属「与远端交互」，使用频次与重要度相近；只把其中一个平铺、
   *  其余收进「⋯」会让用户无法判断哪些操作存在，也不会去点开菜单探索。 */
  const doFetch = () => void run("fetch", async () => {
    if (projectId == null) return;
    return (await api.gitSync(projectId, "fetch")).output;
  });
  const doPush = () => void run("push", async () => {
    if (projectId == null) return;
    return (await api.gitSync(projectId, "push")).output;
  });
  const aheadCount = status?.ahead ?? 0;

  const branchPopover = (
    <Popover open={branchOpen} onClose={() => setBranchOpen(false)} placement="bottom-start" className="gp-branch-pop" ignoreRefs={[branchBtnRef]} role="menu" aria-label="分支">
      <div className="gp-branch-scroll">
        {(branches?.local ?? []).map((b) => (
          <div key={b.name} className={`gp-branch-item${b.current ? " is-current" : ""}`}>
            <button type="button" className="gp-branch-pick" role="menuitem" disabled={b.current || busy != null}
              onClick={() => { setBranchOpen(false); void doCheckout(b.name); }}>
              <span className="gp-branch-name">{b.name}</span>
              {b.current && <IconCheck size={13} />}
            </button>
            {!b.current && (
              <Tooltip content="删除分支" side="left">
                <IconButton icon={<IconTrash size={12} />} aria-label={`删除分支 ${b.name}`} tone="danger"
                  onClick={() => { setBranchOpen(false); setPending({ kind: "delete-branch", name: b.name }); }} />
              </Tooltip>
            )}
          </div>
        ))}
        {branches && branches.remote.length > 0 && (
          <>
            <div className="gp-branch-sep">远程分支</div>
            {branches.remote.slice(0, 40).map((b) => (
              <button key={b.name} type="button" className="gp-branch-pick is-remote" role="menuitem" disabled={busy != null}
                onClick={() => { setBranchOpen(false); void doCheckout(b.name); }}>
                <span className="gp-branch-name">{b.name}</span>
              </button>
            ))}
          </>
        )}
      </div>
      <div className="gp-branch-foot">
        <button type="button" className="gp-branch-new" onClick={() => { setBranchOpen(false); setNewBranch(""); setNewBranchOpen(true); }}>
          <IconPlus size={13} /> 新建分支
        </button>
      </div>
    </Popover>
  );

  /** 历史视图：提交列表（带分支 / 标签装饰）→ 点开看单个提交的信息、变更文件与差异。 */
  const historyView = (
    <div className="gp-history">
      {openSha ? (
        <div className="gp-cv">
          <div className="gp-cv-nav">
            <Button variant="ghost" size="sm" onClick={closeCommit}>返回历史</Button>
          </div>
          {commitLoading && <div className="gp-diff-note">读取提交…</div>}
          {commit && (
            <>
              <div className="gp-cv-subject">{commit.subject}</div>
              <div className="gp-cv-meta">
                <span className="gp-hist-sha">{commit.short}</span>
                <span>{commit.author}</span>
                <time dateTime={commit.date}>{fmtDate(commit.date)}</time>
                <Button variant="ghost" size="sm" onClick={() => void navigator.clipboard?.writeText(commit.sha)}>复制完整 ID</Button>
              </div>
              {commitBodyOf(commit.message) && (
                <pre className="gp-cv-body">{commitBodyOf(commit.message)}</pre>
              )}
              <div className="gp-cv-stat">
                {commit.merge ? (
                  "合并提交：不展示文件变更（与 git log 默认行为一致）"
                ) : (
                  <>
                    {commit.files.length} 个文件 · <span className="gp-up">+{commit.additions}</span>{" "}
                    <span className="gp-down">−{commit.deletions}</span>
                  </>
                )}
              </div>
              {commit.files.length > 0 && (
                <ul className="gp-cv-files">
                  {commit.files.map((f) => {
                    const { dir, name } = splitPath(f.path);
                    const active = commitFile === f.path;
                    return (
                      <li key={f.path}>
                        <button type="button" className={`gp-row-main gp-cv-file${active ? " is-active" : ""}`}
                          onClick={() => void openCommitFile(f.path)}>
                          <span className="gp-file-ic"><FileIcon name={name} size={15} /></span>
                          <span className="gp-name">{name}</span>
                          {dir && <span className="gp-dir">{dir}</span>}
                          <span className={`gp-state is-${stateLetter(f.code).toLowerCase()}`} aria-label={STATE_LABEL[f.code] ?? f.code}>{stateLetter(f.code)}</span>
                          <span className="gp-cv-count">
                            {f.additions == null ? "二进制" : (
                              <>
                                <span className="gp-up">+{f.additions}</span>{" "}
                                <span className="gp-down">−{f.deletions}</span>
                              </>
                            )}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
              {commitFile && (commitDiffLoading
                ? <div className="gp-diff-note">读取差异…</div>
                : commitDiff && <DiffView diff={commitDiff} path={commitFile ?? ""} />)}
            </>
          )}
        </div>
      ) : (
        <>
          {history.length === 0 && !historyLoading && (
            <div className="gp-empty-wrap"><Empty size="sm" mood="calm" title="还没有提交记录" /></div>
          )}
          <ol className="gp-hist-list">
            {history.map((c) => (
              <li key={c.sha}>
                <button type="button" className="gp-hist-row" onClick={() => void openCommit(c.sha)}>
                  <span className={`gp-hist-dot${c.refs.some((r) => r.kind === "head") ? " is-head" : ""}`} aria-hidden />
                  <span className="gp-hist-main">
                    <span className="gp-hist-subject">{c.subject}</span>
                    <span className="gp-hist-meta">
                      {c.refs.map((r) => (
                        <span key={`${r.kind}-${r.name}`} className={`gp-ref is-${r.kind}`}>{r.name}</span>
                      ))}
                      <span className="gp-hist-sha">{c.short}</span>
                      <span className="gp-hist-who">{c.author}</span>
                      <span className="gp-hist-when">{relTime(c.date)}</span>
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
          {historyLoading && <div className="gp-diff-note">读取历史…</div>}
          {historyMore && !historyLoading && (
            <Button variant="ghost" size="sm" block onClick={() => void loadHistory(false)}>加载更多</Button>
          )}
        </>
      )}
    </div>
  );

  if (projectId == null) {
    return <div className="gp-panel"><Empty size="sm" mood="calm" title="先选择一个项目" /></div>;
  }

  return (
    <div className="gp-panel">
      {/* 顶栏：分支 + 同步 */}
      <div className="gp-head">
        <div className="gp-branch-wrap">
          <button ref={branchBtnRef} type="button" className={`gp-branch-btn${branchOpen ? " is-open" : ""}`}
            aria-haspopup="menu" aria-expanded={branchOpen} onClick={() => setBranchOpen((v) => !v)}>
            <IconGitBranch size={13} />
            <span className="gp-branch-label">{branchName}</span>
            <IconChevronDown size={11} />
          </button>
          {status && (status.ahead > 0 || status.behind > 0) && (
            <Tooltip title={`领先 ${status.ahead} · 落后 ${status.behind}`} side="bottom">
              <span className="gp-sync-chip">
                {status.ahead > 0 && <span className="gp-up"><IconArrowUp size={10} />{status.ahead}</span>}
                {status.behind > 0 && <span className="gp-down"><IconArrowDown size={10} />{status.behind}</span>}
              </span>
            </Tooltip>
          )}
          {branchPopover}
        </div>
        <div className="gp-head-ops">
          <Tooltip content="抓取远端更新（不合并到本地）" side="bottom">
            <IconButton icon={<IconGlobe size={14} />} aria-label="抓取远端更新"
              disabled={syncDisabled} onClick={doFetch} />
          </Tooltip>
          <Tooltip content={(status?.behind ?? 0) > 0 ? `拉取并合并 ${status?.behind} 个远端提交` : "拉取并合并远端更新"} side="bottom">
            <IconButton icon={<IconDownload size={14} />} aria-label="拉取并合并远端更新"
              disabled={syncDisabled} onClick={() => void doPull()} />
          </Tooltip>
          <Tooltip content={aheadCount > 0 ? `推送 ${aheadCount} 个提交到远端` : "没有待推送的提交"} side="bottom">
            <span className="gp-push-wrap">
              <IconButton icon={<IconArrowUp size={14} />} aria-label="推送当前分支"
                disabled={syncDisabled || aheadCount === 0} onClick={doPush} />
              {aheadCount > 0 && <span className="gp-push-badge" aria-hidden>{aheadCount}</span>}
            </span>
          </Tooltip>
          <Tooltip content="重新读取仓库状态" side="bottom">
            <IconButton icon={<IconRefresh size={14} />} aria-label="刷新状态" disabled={loading} onClick={() => void refresh()} />
          </Tooltip>
        </div>
      </div>

      <div className="gp-views">
        <Segmented
          value={view}
          onChange={setView}
          size="sm"
          aria-label="Git 视图"
          options={[
            { value: "changes", label: "变更", count: dirtyCount > 0 ? dirtyCount : undefined },
            { value: "history", label: "历史" },
          ]}
        />
      </div>

      {(notice || error) && (
        <div className={`gp-notice${error ? " is-error" : noticeTone === "warn" ? " is-warn" : ""}`} role={error ? "alert" : "status"}>
          <span className="gp-notice-text">{error ?? notice}</span>
          <button type="button" className="gp-notice-x" aria-label="关闭提示" onClick={() => { setNotice(null); setError(null); setNoticeTone("info"); }}>
            <IconX size={11} />
          </button>
        </div>
      )}

      {view === "history" && historyView}

      {view === "changes" && !status && !error && <div className="gp-loading">正在读取仓库状态…</div>}
      {view === "changes" && !status && error && (
        <div className="gp-empty-wrap">
          <Empty size="sm" mood="think" title="无法使用 Git" desc={error} />
        </div>
      )}

      {view === "changes" && status && (
        <div className="gp-body">
          {merging && (
            <div className="gp-merge-banner">
              <IconAlertTriangle size={14} />
              <span className="gp-merge-text">
                {conflictCount > 0 ? `合并进行中，${conflictCount} 个文件有冲突` : "合并进行中，冲突已全部解决，可提交完成合并"}
              </span>
              <Button variant="ghost" size="sm" disabled={busy != null}
                onClick={() => void run("abort", async () => { await api.gitMergeAbort(projectId!); return "已中止合并"; })}>
                中止合并
              </Button>
            </div>
          )}

          {conflictCount > 0 && (
            <>
              <div className="gp-conflict-help">冲突文件需先选择保留哪一边，处理完再一起提交。</div>
              <div className="gp-conflict-actions">
                {status.conflicts.map((c) => (
                  <div key={`cf-${c.path}`} className="gp-conflict-row">
                    <Tooltip title={c.path} side="top">
                      <span className="gp-conflict-name">{c.path}</span>
                    </Tooltip>
                    <Button variant="ghost" size="sm" disabled={busy != null}
                      onClick={() => void run("resolve", async () => { await api.gitResolve(projectId!, c.path, "ours"); })}>采用我方</Button>
                    <Button variant="ghost" size="sm" disabled={busy != null}
                      onClick={() => void run("resolve", async () => { await api.gitResolve(projectId!, c.path, "theirs"); })}>采用对方</Button>
                  </div>
                ))}
              </div>
            </>
          )}

          {/* 两类对象分开列：已跟踪文件的改动 / 从未纳入 git 的新文件。
              组内勾选只改是否加入本次提交，行不会跨组移动。 */}
          {changeGroup({ key: "staged", title: "更改", rows: trackedRows, untracked: false })}
          {changeGroup({ key: "untracked", title: "未跟踪文件", rows: untrackedRows, untracked: true })}

          {status.clean && !merging && (
            <div className="gp-clean">
              <IconCheck size={16} />
              <span>工作区干净，没有未提交的改动</span>
            </div>
          )}

        </div>
      )}

      {view === "changes" && status && (
        <div className="gp-commit">
          <textarea
            className="gp-commit-input"
            rows={2}
            value={message}
            placeholder={stagedCount > 0 ? "填写提交信息（Ctrl+Enter 提交）" : "先勾选要提交的文件"}
            aria-label="提交信息"
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && canCommit) { e.preventDefault(); void doCommit(); }
            }}
          />
          {/* amend 放在动作行左侧、与按钮同一水平线：位置稳定，也不再单独占一行 */}
          <div className="gp-commit-foot">
            <Tooltip content="把这次改动并入上一次提交，不新增提交记录" side="top">
              <span className="gp-amend-wrap">
                <Checkbox className="gp-amend" label="修改上次提交" checked={amend} onChange={setAmend}
                  disabled={busy != null} />
              </span>
            </Tooltip>
            <div className="gp-commit-actions">
              <Button variant="secondary" size="sm" disabled={!canCommit} loading={busy === "commit"}
                onClick={() => void doCommit()}>提交{stagedCount > 0 ? `（${stagedCount}）` : ""}</Button>
              <Button variant="primary" size="sm" disabled={!canCommit} loading={busy === "commit-push"}
                onClick={() => void doCommitAndPush()}>提交并推送</Button>
            </div>
          </div>
        </div>
      )}

      {/* 切换分支被本地改动阻止：暂存后切换可保留改动，放弃则永久丢弃 */}
      <Dialog
        open={dirtySwitch != null}
        onClose={() => setDirtySwitch(null)}
        title="本地改动阻止切换分支"
        subtitle={dirtySwitch ? `切换到「${dirtySwitch.name}」前，需要先处理下列未提交的改动` : undefined}
        width={460}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDirtySwitch(null)}>取消</Button>
            <Button variant="danger-ghost" disabled={busy != null}
              onClick={() => dirtySwitch && void doCheckout(dirtySwitch.name, "discard")}>
              放弃改动并切换
            </Button>
            <Button variant="primary" loading={busy === "checkout"} disabled={busy != null}
              onClick={() => dirtySwitch && void doCheckout(dirtySwitch.name, "stash")}>
              暂存改动并切换
            </Button>
          </>
        }
      >
        <div className="gp-dirty">
          <p className="gp-dirty-hint">
            「暂存改动并切换」会在切换后自动恢复这些改动；「放弃改动」会永久丢弃它们，无法恢复。
          </p>
          <ul className="gp-dirty-list">
            {(dirtySwitch?.files ?? []).slice(0, 8).map((f) => (
              <li key={f}><code>{f}</code></li>
            ))}
          </ul>
          {(dirtySwitch?.files.length ?? 0) > 8 && (
            <p className="gp-dirty-more">还有 {dirtySwitch!.files.length - 8} 个文件未列出</p>
          )}
        </div>
      </Dialog>

      <FormDialog
        open={newBranchOpen}
        onClose={() => setNewBranchOpen(false)}
        title="新建分支"
        subtitle="从当前提交创建并切换到新分支"
        submitLabel="创建并切换"
        submitDisabled={!newBranch.trim() || busy != null}
        onSubmit={() => {
          const name = newBranch.trim();
          setNewBranchOpen(false);
          void run("branch", async () => (await api.gitBranch(projectId!, "create", name)).output);
        }}
        width={420}
      >
        <Input value={newBranch} placeholder="如：feat/login" aria-label="新分支名称"
          onChange={(e) => setNewBranch(e.target.value)} autoFocus />
      </FormDialog>

      <ConfirmDialog
        open={pending?.kind === "discard"}
        title="丢弃改动"
        message={pending?.kind === "discard" ? `将丢弃「${pending.label}」的未提交改动，且无法恢复。确定继续吗？` : ""}
        confirmLabel="丢弃"
        danger
        onCancel={() => setPending(null)}
        onConfirm={() => { if (pending?.kind === "discard") void doDiscard(pending); }}
      />
      <ConfirmDialog
        open={pending?.kind === "delete-branch"}
        title="删除分支"
        message={pending?.kind === "delete-branch" ? `将删除本地分支「${pending.name}」。只有已合并的分支会被删除，否则 git 会拒绝。` : ""}
        confirmLabel="删除"
        danger
        onCancel={() => setPending(null)}
        onConfirm={() => { if (pending?.kind === "delete-branch") void doDeleteBranch(pending.name); }}
      />
    </div>
  );
}
