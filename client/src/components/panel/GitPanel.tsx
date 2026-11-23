/** Git 面板：右侧面板里的常规 Git 操作。
 *
 * 四个视图：
 *  - 「更改」：冲突（置顶）→ 已暂存的更改 → 更改 → 未跟踪 → 固定提交卡；
 *  - 「历史」：提交列表（分支 / 标签装饰）→ 吸顶导航与单个提交的文件差异；
 *  - 「分支」：本地 / 远程分支切换、创建与删除；
 *  - 「贮藏」：新建贮藏、展开文件统计、应用 / 弹出 / 删除贮藏。
 *
 * 严格绑定入参 `sessionId` 对应的会话桶与 `GitScope`（repo + sessionId），
 * 多会话保活切换时互不串流。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type GitBranchesOut,
  type GitCommitDetailOut,
  type GitDiffOut,
  type GitFileEntry,
  type GitLogEntry,
  type GitRepoOut,
  type GitScope,
  type GitStashEntry,
  type GitStashShowOut,
  type GitStatusOut,
} from "../../api/client";
import { findSession, useChatStore } from "../../store/chat";
import { bucketSessionId, usePanelStore } from "../../store/panel";
import { ConfirmDialog } from "../ConfirmDialog";
import {
  Badge,
  Button,
  Checkbox,
  Collapse,
  Dialog,
  Empty,
  FormDialog,
  IconButton,
  Input,
  Menu,
  Popover,
  Segmented,
  Textarea,
  Tooltip,
} from "../ui";
import {
  IconAlertTriangle,
  IconArchive,
  IconArrowDown,
  IconArrowLeft,
  IconArrowUp,
  IconCheck,
  IconChevronDown,
  IconCopy,
  IconDownload,
  IconExternalLink,
  IconGitBranch,
  IconGlobe,
  IconLayers,
  IconMinus,
  IconMoreHorizontal,
  IconPlay,
  IconPlus,
  IconRefresh,
  IconRotateCcw,
  IconSearch,
  IconSpinner,
  IconTrash,
  IconX,
} from "../icons";
import { FileIcon } from "../iconkit/FileIcon";
import { highlightLines } from "../chat/CodeView";

type SectionKey = "staged" | "unstaged" | "untracked";
type PanelView = "changes" | "history" | "branches" | "stash";

/** 联网同步操作的中文阶段名。顶栏进度行据此展示「正在拉取并合并 · 已等待 Ns」。 */
const SYNC_LABELS: Record<string, string> = {
  fetch: "正在抓取远端更新",
  pull: "正在拉取并合并",
  push: "正在推送",
};

/** 单个分区内的变更行（已暂存 / 工作区更改 / 未跟踪三段彼此独立，MM 文件在两段各自占一行）。 */
type SectionRow = {
  section: SectionKey;
  path: string;
  code: string;
  oldPath?: string | null;
};

type Pending =
  | { kind: "discard"; paths: string[]; untracked: string[]; label: string }
  | { kind: "delete-branch"; name: string }
  | { kind: "reset-soft" }
  | { kind: "stash-drop"; index: number; refName: string; message: string }
  | null;

const stateLetter = (code: string) => (code === "?" ? "U" : code);
const STATE_LABEL: Record<string, string> = {
  M: "已修改",
  A: "新增",
  D: "已删除",
  R: "重命名",
  C: "复制",
  T: "类型变更",
  U: "冲突",
  "?": "未跟踪",
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

/** 提交信息正文：首行之后的部分（首行即标题）。 */
function commitBodyOf(message: string): string {
  return message.split("\n").slice(1).join("\n").trim();
}

function splitPath(p: string): { dir: string; name: string } {
  const i = p.lastIndexOf("/");
  return i < 0 ? { dir: "", name: p } : { dir: p.slice(0, i + 1), name: p.slice(i + 1) };
}

function sortByName(entries: GitFileEntry[], section: SectionKey): SectionRow[] {
  return entries
    .filter((e) => !(section === "untracked" && e.path.endsWith("/")))
    .map((e): SectionRow => ({
      section,
      path: e.path,
      code: section === "untracked" ? "?" : e.code,
      oldPath: e.old_path,
    }))
    .sort((a, b) =>
      splitPath(a.path).name.localeCompare(splitPath(b.path).name, "zh") ||
      a.path.localeCompare(b.path),
    );
}

function createOpId(): string {
  return `git_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

const HUNK_HEAD_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

interface ParsedDiffBlock {
  headerLines: string[];
  /** 每个块对应一个 hunk（或纯 header） */
  hunks: Array<{
    startLine: number;
    header: string;
    lines: string[];
    patch: string | null;
  }>;
  /** 仅当确实是 unified diff 头（含 diff --git / --- / +++）时才允许拼装 hunk patch */
  patchable: boolean;
}

function parseUnifiedDiff(text: string, isNewFile: boolean, truncated: boolean): ParsedDiffBlock {
  if (isNewFile) {
    return { headerLines: [], hunks: [], patchable: false };
  }
  const raw = text.split("\n");
  // 若末尾是 split 出来的空尾行，保留精确行序列但避免把末尾空串当作额外代码行
  const lines = raw.length > 0 && raw[raw.length - 1] === "" ? raw.slice(0, -1) : raw;
  const firstHunkIdx = lines.findIndex((l) => HUNK_HEAD_RE.test(l));
  if (firstHunkIdx < 0) {
    return { headerLines: lines, hunks: [], patchable: false };
  }
  const headerLines = lines.slice(0, firstHunkIdx);
  const patchable = headerLines.some(
    (l) => l.startsWith("diff --git ") || l.startsWith("--- ") || l.startsWith("+++ "),
  );
  const hunks: ParsedDiffBlock["hunks"] = [];
  let i = firstHunkIdx;
  while (i < lines.length) {
    if (!HUNK_HEAD_RE.test(lines[i])) {
      i += 1;
      continue;
    }
    const start = i;
    let j = i + 1;
    while (j < lines.length && !HUNK_HEAD_RE.test(lines[j]) && !lines[j].startsWith("diff --git ")) {
      j += 1;
    }
    const body = lines.slice(start, j);
    const isLast = j >= lines.length;
    const canPatch = patchable && !(truncated && isLast);
    hunks.push({
      startLine: start,
      header: lines[start],
      lines: body,
      patch: canPatch ? [...headerLines, ...body].join("\n") + "\n" : null,
    });
    i = j;
  }
  return { headerLines, hunks, patchable };
}

/** 统一 diff 渲染：
 *  - 已跟踪文件差异：+ / - 行着色、hunk 头右侧提供「暂存块 / 取消暂存块」；
 *  - 未跟踪新文件（diff.new）：直接以代码视图整行高亮，不误剥首字符、也不解析假 @@ 头。 */
function DiffView({
  diff,
  path,
  hunkMode,
  hunkBusy,
  onHunkAction,
  wrap = false,
}: {
  diff: GitDiffOut;
  path: string;
  hunkMode?: "stage" | "unstage";
  hunkBusy?: boolean;
  onHunkAction?: (patch: string) => void;
  wrap?: boolean;
}) {
  const parsed = useMemo(
    () => parseUnifiedDiff(diff.text, diff.new, diff.truncated),
    [diff.text, diff.new, diff.truncated],
  );

  const rawLines = useMemo(() => {
    if (diff.binary || !diff.text) return [];
    const arr = diff.text.split("\n");
    return arr.length > 0 && arr[arr.length - 1] === "" ? arr.slice(0, -1) : arr;
  }, [diff.binary, diff.text]);

  const lineNumbers = useMemo(() => {
    let left = 0;
    let right = 0;
    let inHunk = false;
    return rawLines.map((line) => {
      const head = HUNK_HEAD_RE.exec(line);
      if (head) {
        left = Number(head[1]); right = Number(head[2]); inHunk = true;
        return null;
      }
      if (line.startsWith("diff --git ")) inHunk = false;
      if (!inHunk) return null;
      if (line.startsWith("+")) return [null, right++];
      if (line.startsWith("-")) return [left++, null];
      if (line.startsWith(" ")) return [left++, right++];
      return null;
    });
  }, [rawLines]);

  const htmlLines = useMemo(() => {
    if (diff.binary || !diff.text) return null;
    if (diff.new) {
      return highlightLines(path, rawLines);
    }
    const out: (string | null)[] = new Array(rawLines.length).fill(null);
    const kindOf = (index: number) => lineNumbers[index] ? rawLines[index][0] : null;
    let i = 0;
    while (i < rawLines.length) {
      const k = kindOf(i);
      if (!k) {
        i += 1;
        continue;
      }
      let j = i;
      while (j < rawLines.length && kindOf(j) === k) j += 1;
      const seg = rawLines.slice(i, j).map((l) => l.slice(1));
      const hl = highlightLines(path, seg);
      if (hl && hl.length === seg.length) {
        for (let x = 0; x < seg.length; x += 1) out[i + x] = hl[x];
      }
      i = j;
    }
    return out;
  }, [diff.binary, diff.text, diff.new, path, rawLines, lineNumbers]);

  const hunkAtLine = useMemo(() => {
    const map = new Map<number, ParsedDiffBlock["hunks"][number]>();
    for (const h of parsed.hunks) map.set(h.startLine, h);
    return map;
  }, [parsed.hunks]);

  if (diff.binary) return <div className="gp-diff-note">二进制文件，无法显示差异</div>;
  if (!diff.text) return <div className="gp-diff-note">没有可显示的差异</div>;

  if (diff.new) {
    return (
      <div className={`gp-diff${wrap ? " is-wrap" : ""}`} role="region" aria-label="新文件内容">
        {rawLines.map((line, i) => {
          const html = htmlLines?.[i] ?? null;
          return (
            <div key={i} className="gp-diff-line is-add">
              <span className="gp-diff-number" aria-hidden />
              <span className="gp-diff-number" aria-hidden>{i + 1}</span>
              <span className="gp-diff-sign" aria-hidden>+</span>
              {html ? (
                <code className="gp-diff-code hljs" dangerouslySetInnerHTML={{ __html: html || " " }} />
              ) : (
                <code className="gp-diff-code">{line || " "}</code>
              )}
            </div>
          );
        })}
        {diff.truncated && <div className="gp-diff-note">文件过大，已截断显示</div>}
      </div>
    );
  }

  return (
    <div className={`gp-diff${wrap ? " is-wrap" : ""}`} role="region" aria-label="差异">
      {rawLines.map((line, i) => {
        const numbers = lineNumbers[i];
        const isHunk = HUNK_HEAD_RE.test(line);
        if (isHunk) {
          const h = hunkAtLine.get(i);
          const patch = h?.patch ?? null;
          return (
            <div key={i} className="gp-diff-line is-hunk">
              <span className="gp-diff-hunk-text">{line}</span>
              {onHunkAction && hunkMode && patch && (
                <Button
                  variant="ghost"
                  size="xs"
                  className="gp-hunk-btn"
                  disabled={hunkBusy}
                  onClick={(e) => {
                    e.stopPropagation();
                    onHunkAction(patch);
                  }}
                >
                  {hunkMode === "unstage" ? "取消暂存块" : "暂存块"}
                </Button>
              )}
            </div>
          );
        }
        if (!numbers) {
          return <div key={i} className="gp-diff-line is-meta">{line || " "}</div>;
        }
        const cls = line.startsWith("+") ? "is-add" : line.startsWith("-") ? "is-del" : "";
        const html = htmlLines?.[i] ?? null;
        const sign = line.charAt(0);
        const code = line.slice(1);
        return (
          <div key={i} className={`gp-diff-line ${cls}`}>
            <span className="gp-diff-number" aria-label={numbers[0] == null ? undefined : `旧行号 ${numbers[0]}`}>{numbers[0]}</span>
            <span className="gp-diff-number" aria-label={numbers[1] == null ? undefined : `新行号 ${numbers[1]}`}>{numbers[1]}</span>
            <span className="gp-diff-sign" aria-hidden>{sign === " " ? "" : sign}</span>
            {html ? (
              <code className="gp-diff-code hljs" dangerouslySetInnerHTML={{ __html: html || " " }} />
            ) : (
              <code className="gp-diff-code">{code || " "}</code>
            )}
          </div>
        );
      })}
      {diff.truncated && <div className="gp-diff-note">差异过大，已截断显示（末块不支持单独暂存）</div>}
    </div>
  );
}

export function GitPanel({ sessionId = null, visible = true }: { sessionId?: number | null; visible?: boolean }) {
  // 严格按会话隔离：优先用 RightPanel 传入的 sessionId（所属会话桶）定位项目与 GitScope，
  // 避免后台保活会话在切换后错读其他会话的项目或主工作树。
  const activeBucket = usePanelStore((s) => s.activeBucket);
  const effectiveSessionId = sessionId === undefined ? bucketSessionId(activeBucket) : sessionId;
  const fallbackProjectId = useChatStore((s) => s.currentProjectId);
  const sessionProjectId = useChatStore((s) =>
    effectiveSessionId == null ? null : findSession(s, effectiveSessionId)?.project_id ?? null,
  );
  const projectId = sessionProjectId ?? fallbackProjectId;
  const isRunning = useChatStore((s) =>
    effectiveSessionId == null
      ? s.isRunning
      : (s.sessionState[effectiveSessionId]?.isRunning ?? (s.currentSessionId === effectiveSessionId && s.isRunning)),
  );

  const [status, setStatus] = useState<GitStatusOut | null>(null);
  const [repos, setRepos] = useState<GitRepoOut[]>([]);
  const [repoSel, setRepoSel] = useState<{ projectId: number | null; rel: string }>({ projectId: null, rel: "" });
  const [reposReady, setReposReady] = useState(false);
  const [repoOpen, setRepoOpen] = useState(false);
  const repo = repoSel.projectId === projectId ? repoSel.rel : "";
  const repoBtnRef = useRef<HTMLButtonElement>(null);

  const scope = useMemo<GitScope>(
    () => ({ repo, sessionId: effectiveSessionId }),
    [repo, effectiveSessionId],
  );

  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [syncWaited, setSyncWaited] = useState(0);
  const [syncCancelPending, setSyncCancelPending] = useState(false);
  const [syncProgress, setSyncProgress] = useState<{ phase: string | null; percent: number | null }>({ phase: null, percent: null });
  const runSeq = useRef(0);
  const activeSyncOpId = useRef<string | null>(null);

  const [notice, setNotice] = useState<string | null>(null);
  const [noticeTone, setNoticeTone] = useState<"info" | "warn">("info");

  // 选中差异（带 section，彻底隔离 MM 文件在 staged 与 unstaged 各自展开）
  const [selected, setSelected] = useState<{ section: SectionKey; path: string } | null>(null);
  const [diff, setDiff] = useState<GitDiffOut | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [wrapDiff, setWrapDiff] = useState(false);

  // 变更筛选
  const [filterOpen, setFilterOpen] = useState(false);
  const [filterQuery, setFilterQuery] = useState("");

  // 提交区
  const [message, setMessage] = useState("");
  const [amend, setAmend] = useState(false);

  // 分支
  const [branches, setBranches] = useState<GitBranchesOut | null>(null);
  const [branchesLoading, setBranchesLoading] = useState(false);
  const [branchFilter, setBranchFilter] = useState("");
  const [branchOpen, setBranchOpen] = useState(false);
  const [newBranchOpen, setNewBranchOpen] = useState(false);
  const [newBranch, setNewBranch] = useState("");
  const branchBtnRef = useRef<HTMLButtonElement>(null);

  // 分区折叠（默认全部展开）
  const [collapsed, setCollapsed] = useState<Record<SectionKey, boolean>>({
    staged: false,
    unstaged: false,
    untracked: false,
  });

  // 确认弹窗 / 脏切换弹窗
  const [pending, setPending] = useState<Pending>(null);
  const [dirtySwitch, setDirtySwitch] = useState<{ name: string; files: string[] } | null>(null);

  // 四段视图：更改 / 历史 / 分支 / 贮藏
  const [view, setView] = useState<PanelView>("changes");

  // 历史
  const [history, setHistory] = useState<GitLogEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyMore, setHistoryMore] = useState(false);
  const [openSha, setOpenSha] = useState<string | null>(null);
  const [commit, setCommit] = useState<GitCommitDetailOut | null>(null);
  const [commitLoading, setCommitLoading] = useState(false);
  const [commitFile, setCommitFile] = useState<string | null>(null);
  const [commitDiff, setCommitDiff] = useState<GitDiffOut | null>(null);
  const [commitDiffLoading, setCommitDiffLoading] = useState(false);

  // 贮藏（Stash）
  const [stashes, setStashes] = useState<GitStashEntry[]>([]);
  const [stashesLoading, setStashesLoading] = useState(false);
  const [expandedStash, setExpandedStash] = useState<number | null>(null);
  const [stashDetails, setStashDetails] = useState<Record<number, GitStashShowOut>>({});
  const [stashDetailLoading, setStashDetailLoading] = useState<number | null>(null);
  const [stashDialogOpen, setStashDialogOpen] = useState(false);
  const [stashMessage, setStashMessage] = useState("");
  const [stashIncludeUntracked, setStashIncludeUntracked] = useState(true);

  const seq = useRef(0);
  const histSeq = useRef(0);
  const commitSeq = useRef(0);
  const diffSeq = useRef(0);
  const workDiffSeq = useRef(0);
  const stashSeq = useRef(0);
  const HISTORY_PAGE = 40;

  const repoStorageKey = (pid: number) =>
    effectiveSessionId != null ? `chatcoder.git.repo.${pid}.s${effectiveSessionId}` : `chatcoder.git.repo.${pid}`;

  /** 在右侧「文件」管理面板中打开指定文件 */
  const openInFilesPanel = useCallback(
    (relPath: string) => {
      const fullRel = repo ? `${repo}/${relPath}` : relPath;
      const ps = usePanelStore.getState();
      ps.setPreviewPath(fullRel);
      ps.openPanel();
      ps.openTab("files");
    },
    [repo],
  );

  /** 读取仓库状态 */
  const refresh = useCallback(async (): Promise<GitStatusOut | null> => {
    if (projectId == null) return null;
    const mine = ++seq.current;
    setLoading(true);
    try {
      const st = await api.gitStatus(projectId, scope);
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
  }, [projectId, scope]);

  /** 读取贮藏列表 */
  const refreshStashes = useCallback(async () => {
    if (projectId == null) return;
    const mine = ++stashSeq.current;
    setStashesLoading(true);
    try {
      const list = await api.gitStashes(projectId, scope);
      if (mine !== stashSeq.current) return;
      setStashes(list);
      setStashDetails({});
    } catch {
      if (mine === stashSeq.current) setStashes([]);
    } finally {
      if (mine === stashSeq.current) setStashesLoading(false);
    }
  }, [projectId, scope]);

  /** 读取历史第一页（reset）或追加下一页 */
  const loadHistory = useCallback(
    async (reset: boolean) => {
      if (projectId == null) return;
      const mine = ++histSeq.current;
      setHistoryLoading(true);
      try {
        const skip = reset ? 0 : history.length;
        const page = await api.gitLog(projectId, HISTORY_PAGE, skip, scope);
        if (mine !== histSeq.current) return;
        setHistory((prev) => (reset ? page : [...prev, ...page]));
        setHistoryMore(page.length === HISTORY_PAGE);
      } catch (e) {
        if (mine !== histSeq.current) return;
        setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
      } finally {
        if (mine === histSeq.current) setHistoryLoading(false);
      }
    },
    [projectId, history.length, scope],
  );

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
      const detail = await api.gitCommitDetail(projectId, sha, scope);
      if (mine === commitSeq.current) setCommit(detail);
    } catch (e) {
      if (mine === commitSeq.current) setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      if (mine === commitSeq.current) setCommitLoading(false);
    }
  };

  const openCommitFile = async (path: string) => {
    if (projectId == null || openSha == null) return;
    if (commitFile === path) {
      setCommitFile(null);
      setCommitDiff(null);
      return;
    }
    const mine = ++diffSeq.current;
    setCommitFile(path);
    setCommitDiff(null);
    setCommitDiffLoading(true);
    try {
      const d = await api.gitCommitDiff(projectId, openSha, path, scope);
      if (mine === diffSeq.current) setCommitDiff(d);
    } catch (e) {
      if (mine === diffSeq.current) setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      if (mine === diffSeq.current) setCommitDiffLoading(false);
    }
  };

  const refreshBranches = useCallback(async () => {
    if (projectId == null) return;
    setBranchesLoading(true);
    try {
      setBranches(await api.gitBranches(projectId, scope));
    } catch {
      setBranches(null);
    } finally {
      setBranchesLoading(false);
    }
  }, [projectId, scope]);

  const fetchRepos = useCallback(async () => {
    if (projectId == null) return;
    try {
      setRepos(await api.gitRepos(projectId, { sessionId: effectiveSessionId }));
    } catch {
      /* 读取失败保留现有列表 */
    }
  }, [projectId, effectiveSessionId]);

  const chooseRepo = (rel: string) => {
    if (projectId == null) return;
    setRepoOpen(false);
    setStatus(null);
    setBranches(null);
    setStashes([]);
    setExpandedStash(null);
    setStashDetails({});
    setRepoSel({ projectId, rel });
    try {
      const key = repoStorageKey(projectId);
      if (rel) localStorage.setItem(key, rel);
      else localStorage.removeItem(key);
    } catch {
      /* 记忆失败不影响切换 */
    }
  };

  // 项目或会话变化时重新识别仓库
  useEffect(() => {
    if (!visible || projectId == null) return;
    let cancelled = false;
    setRepoOpen(false);
    setReposReady(false);
    setRepos([]);
    void api
      .gitRepos(projectId, { sessionId: effectiveSessionId })
      .then((list) => {
        if (cancelled) return;
        setRepos(list);
        let saved = "";
        try {
          saved =
            localStorage.getItem(repoStorageKey(projectId)) ??
            localStorage.getItem(`chatcoder.git.repo.${projectId}`) ??
            "";
        } catch {
          saved = "";
        }
        const pick = list.find((r) => r.rel === saved) ?? list.find((r) => r.is_root) ?? list[0];
        setRepoSel({ projectId, rel: pick?.rel ?? "" });
        setReposReady(true);
      })
      .catch(() => {
        if (cancelled) return;
        setRepos([]);
        setRepoSel({ projectId, rel: "" });
        setReposReady(true);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, effectiveSessionId, visible]);

  // 打开 / 切换项目或仓库时刷新
  useEffect(() => {
    if (!visible || !reposReady) return;
    runSeq.current++;
    activeSyncOpId.current = null;
    setBusy(null);
    setSyncWaited(0);
    setSelected(null);
    setDiff(null);
    setHistory([]);
    setExpandedStash(null);
    setStashDetails({});
    closeCommit();
    void refresh();
    void refreshStashes();
  }, [projectId, repo, visible, reposReady, refresh, refreshStashes]);

  // plan-771-3177：网络连接阶段由本地计时兜底，传输阶段显示 Git 报告的真实进度。
  useEffect(() => {
    if (busy == null || SYNC_LABELS[busy] === undefined) {
      setSyncWaited(0);
      setSyncProgress({ phase: null, percent: null });
      return;
    }
    const started = Date.now();
    setSyncWaited(0);
    setSyncProgress({ phase: null, percent: null });
    let pending: AbortController | null = null;
    const tick = () => {
      setSyncWaited(Math.floor((Date.now() - started) / 1000));
      const op = activeSyncOpId.current;
      if (!op || pending) return;
      const controller = new AbortController();
      pending = controller;
      void api.gitSyncProgress(op, controller.signal).then((progress) => {
        if (!controller.signal.aborted && activeSyncOpId.current === op && progress.phase) setSyncProgress(progress);
      }).catch(() => {}).finally(() => { if (pending === controller) pending = null; });
    };
    tick();
    const timer = window.setInterval(tick, 650);
    return () => { window.clearInterval(timer); pending?.abort(); };
  }, [busy]);

  useEffect(() => {
    if (!visible || !reposReady) return;
    if (view === "history") void loadHistory(true);
    else if (view === "branches") void refreshBranches();
    else if (view === "stash") void refreshStashes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, repo, visible, view, reposReady]);

  const repoOfPath = useCallback(
    (path: string): string => {
      const norm = path.replace(/\\/g, "/");
      let hit = "";
      for (const r of repos) {
        if (r.rel && (norm === r.rel || norm.startsWith(`${r.rel}/`)) && r.rel.length > hit.length) hit = r.rel;
      }
      return hit;
    },
    [repos],
  );

  /** 工具卡点击文件 → 切到「变更」视图并展开对应分区的差异 */
  const gitFocus = usePanelStore((s) => s.gitFocus);
  useEffect(() => {
    if (!visible || !gitFocus || !status || !reposReady) return;
    if (gitFocus.projectId != null && projectId != null && gitFocus.projectId !== projectId) {
      usePanelStore.getState().clearGitFocus();
      return;
    }
    const target = repoOfPath(gitFocus.path) || gitFocus.repo;
    if (target !== repo) {
      chooseRepo(target);
      return;
    }
    usePanelStore.getState().clearGitFocus();
    const path =
      repo && (gitFocus.path === repo || gitFocus.path.startsWith(`${repo}/`))
        ? gitFocus.path.slice(repo.length + 1)
        : gitFocus.path;
    setView("changes");
    let section: SectionKey = gitFocus.staged ? "staged" : "unstaged";
    if (gitFocus.staged && status.staged.some((e) => e.path === path)) section = "staged";
    else if (!gitFocus.staged && status.unstaged.some((e) => e.path === path)) section = "unstaged";
    else if (status.untracked.some((e) => e.path === path)) section = "untracked";
    else if (status.staged.some((e) => e.path === path)) section = "staged";
    if (selected?.section === section && selected?.path === path) return;
    setSelected({ section, path });
    void loadDiff(path, section === "staged");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gitFocus, status, visible, projectId, repo, reposReady]);

  // AI 一轮结束后自动刷新
  const wasRunning = useRef(isRunning);
  useEffect(() => {
    if (wasRunning.current && !isRunning && visible) {
      void refresh();
      void refreshStashes();
    }
    wasRunning.current = isRunning;
  }, [isRunning, visible, refresh, refreshStashes]);

  // 会话工具卡里切换分支 / 暂存后广播该事件
  useEffect(() => {
    if (!visible) return;
    const onChanged = () => {
      void refresh();
      void refreshStashes();
    };
    window.addEventListener("chatcoder:git-changed", onChanged);
    return () => window.removeEventListener("chatcoder:git-changed", onChanged);
  }, [visible, refresh, refreshStashes]);

  useEffect(() => {
    if (branchOpen) void refreshBranches();
  }, [branchOpen, refreshBranches]);

  /** plan-771-3177：收到后端取消确认后再释放同步操作槽位。 */
  const cancelSync = async () => {
    const opId = activeSyncOpId.current;
    if (!opId || syncCancelPending) return;
    setSyncCancelPending(true);
    try {
      const result = await api.gitSyncCancel(opId);
      if (activeSyncOpId.current !== opId) return;
      if (!result.cancelled) {
        setNoticeTone("warn"); setNotice("同步仍在收尾，暂时无法取消");
        return;
      }
      activeSyncOpId.current = null;
      runSeq.current++;
      setBusy(null);
      setSyncWaited(0);
      setNotice("已取消远端同步");
      setNoticeTone("warn");
      void refresh();
    } catch {
      if (activeSyncOpId.current === opId) {
        setNoticeTone("warn"); setNotice("取消请求未送达，可再次尝试");
      }
    } finally {
      setSyncCancelPending(false);
    }
  };

  /** 读取某个文件在特定分区的差异 */
  const loadDiff = async (path: string, staged: boolean) => {
    if (projectId == null) return;
    const mine = ++workDiffSeq.current;
    setDiffLoading(true);
    try {
      const d = await api.gitDiff(projectId, path, staged, scope);
      if (mine === workDiffSeq.current) setDiff(d);
    } catch (e) {
      if (mine !== workDiffSeq.current) return;
      setDiff(null);
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      if (mine === workDiffSeq.current) setDiffLoading(false);
    }
  };

  /** 统一执行写操作并在完成后刷新状态与保留/更新展开的差异 */
  const run = async (label: string, fn: () => Promise<string | void>) => {
    if (projectId == null) return;
    const mine = ++runSeq.current;
    setBusy(label);
    setSyncWaited(0);
    setNotice(null);
    setError(null);
    let failure: string | null = null;
    try {
      try {
        const out = await fn();
        if (mine === runSeq.current && out) setNotice(out);
      } catch (e) {
        failure = e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e);
      }
      if (mine !== runSeq.current) return;
      const st = await refresh();
      if (failure !== null) setError(failure);
      if (selected && st) {
        const existsInSection =
          selected.section === "staged"
            ? st.staged.some((e) => e.path === selected.path)
            : selected.section === "unstaged"
              ? st.unstaged.some((e) => e.path === selected.path)
              : st.untracked.some((e) => e.path === selected.path);
        if (!existsInSection) {
          workDiffSeq.current++;
          setSelected(null);
          setDiff(null);
          setDiffLoading(false);
        } else {
          void loadDiff(selected.path, selected.section === "staged");
        }
      }
    } finally {
      if (mine === runSeq.current) setBusy(null);
    }
  };

  const openDiff = async (section: SectionKey, path: string) => {
    if (projectId == null) return;
    if (selected?.section === section && selected?.path === path) {
      workDiffSeq.current++;
      setSelected(null);
      setDiff(null);
      setDiffLoading(false);
      return;
    }
    setSelected({ section, path });
    await loadDiff(path, section === "staged");
  };

  const toggleSection = (s: SectionKey) => setCollapsed((c) => ({ ...c, [s]: !c[s] }));

  // 三段式独立变更数据（彻底废除强行合并，MM 文件在两段各占一行）
  const { stagedRows, unstagedRows, untrackedRows, conflictRows } = useMemo(() => {
    if (!status) {
      return {
        stagedRows: [] as SectionRow[],
        unstagedRows: [] as SectionRow[],
        untrackedRows: [] as SectionRow[],
        conflictRows: [] as GitFileEntry[],
      };
    }
    return {
      stagedRows: sortByName(status.staged, "staged"),
      unstagedRows: sortByName(status.unstaged, "unstaged"),
      untrackedRows: sortByName(status.untracked, "untracked"),
      conflictRows: status.conflicts,
    };
  }, [status]);

  const stagedCount = stagedRows.length;
  const conflictCount = conflictRows.length;
  const dirtyCount = stagedRows.length + unstagedRows.length + untrackedRows.length + conflictRows.length;
  const merging = status?.state === "merging";

  // 变更筛选：总数 >= 8 或用户主动展开/有查询词时显示过滤框
  const showFilterBox = dirtyCount >= 8 || filterOpen || filterQuery.trim().length > 0;
  const normFilter = filterQuery.trim().toLowerCase();
  const filteredStaged = useMemo(
    () => (normFilter ? stagedRows.filter((r) => r.path.toLowerCase().includes(normFilter)) : stagedRows),
    [stagedRows, normFilter],
  );
  const filteredUnstaged = useMemo(
    () => (normFilter ? unstagedRows.filter((r) => r.path.toLowerCase().includes(normFilter)) : unstagedRows),
    [unstagedRows, normFilter],
  );
  const filteredUntracked = useMemo(
    () => (normFilter ? untrackedRows.filter((r) => r.path.toLowerCase().includes(normFilter)) : untrackedRows),
    [untrackedRows, normFilter],
  );

  // 单文件操作
  const stagePaths = (paths: string[]) =>
    void run("stage", async () => {
      if (projectId == null || paths.length === 0) return;
      await api.gitStage(projectId, paths, scope);
    });

  const unstagePaths = (paths: string[]) =>
    void run("unstage", async () => {
      if (projectId == null || paths.length === 0) return;
      await api.gitUnstage(projectId, paths, scope);
    });

  const handleHunkAction = (mode: "stage" | "unstage", patch: string) =>
    void run(mode === "stage" ? "stage-hunk" : "unstage-hunk", async () => {
      if (projectId == null) return;
      if (mode === "stage") await api.gitStageHunk(projectId, patch, scope);
      else await api.gitUnstageHunk(projectId, patch, scope);
    });

  const canCommit = message.trim().length > 0 && (stagedCount > 0 || amend) && !busy;

  const doCommit = () =>
    run("commit", async () => {
      if (projectId == null) return;
      const res = await api.gitCommit(projectId, message.trim(), amend, scope);
      setMessage("");
      setAmend(false);
      void loadHistory(true);
      return `已提交 ${res.sha}`;
    });

  const doCommitAndPush = () =>
    run("commit-push", async () => {
      if (projectId == null) return;
      await api.gitCommit(projectId, message.trim(), amend, scope);
      setMessage("");
      setAmend(false);
      void loadHistory(true);
      const opId = createOpId();
      activeSyncOpId.current = opId;
      try {
        const out = await api.gitSync(projectId, "push", scope, opId);
        return out.output || "已提交并推送";
      } finally {
        if (activeSyncOpId.current === opId) activeSyncOpId.current = null;
      }
    });

  const doDiscard = (p: Extract<Pending, { kind: "discard" }>) =>
    run("discard", async () => {
      if (projectId == null) return;
      await api.gitDiscard(projectId, p.paths, p.untracked, scope, true);
      setPending(null);
      return "已丢弃改动";
    });

  const doDeleteBranch = (name: string) =>
    run("branch", async () => {
      if (projectId == null) return;
      const out = await api.gitBranch(projectId, "delete", name, {}, scope);
      setPending(null);
      void refreshBranches();
      return out.output;
    });

  const doResetSoft = () =>
    run("reset-soft", async () => {
      if (projectId == null) return;
      const res = await api.gitResetSoft(projectId, scope);
      setPending(null);
      if (res.message) {
        setMessage((prev) => (prev.trim() ? `${res.message}\n\n${prev.trim()}` : res.message));
      }
      void loadHistory(true);
      return `已撤销提交 ${res.previous_sha}，改动保留在已暂存区`;
    });

  // 远端同步（带真实可取消 opId）
  const doSync = (action: "fetch" | "pull" | "push") =>
    void run(action, async () => {
      if (projectId == null) return;
      const opId = createOpId();
      activeSyncOpId.current = opId;
      try {
        const res = await api.gitSync(projectId, action, scope, opId);
        if (res.cancelled) {
          setNoticeTone("warn");
          return "已取消同步";
        }
        if (action === "pull" && res.conflict) {
          setView("changes");
          setNoticeTone("warn");
          return `拉取产生 ${res.conflicts?.length ?? 0} 个冲突文件，请在「冲突」区处理后再提交。`;
        }
        setNoticeTone("info");
        return res.output;
      } finally {
        if (activeSyncOpId.current === opId) activeSyncOpId.current = null;
      }
    });

  /** 切换分支 */
  const doCheckout = async (name: string, onDirty?: "stash" | "discard") => {
    if (projectId == null) return;
    setBusy("checkout");
    setError(null);
    setNotice(null);
    try {
      const res = await api.gitBranch(projectId, "checkout", name, { onDirty }, scope);
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
      void refreshBranches();
      void refreshStashes();
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      setBusy(null);
    }
  };

  // Stash 操作
  const openStashDialog = (includeUntracked = true) => {
    setStashMessage("");
    setStashIncludeUntracked(includeUntracked);
    setStashDialogOpen(true);
  };

  const doStashPush = () =>
    void run("stash-push", async () => {
      if (projectId == null) return;
      const res = await api.gitStashPush(
        projectId,
        {
          message: stashMessage.trim() || undefined,
          include_untracked: stashIncludeUntracked,
        },
        scope,
      );
      setStashDialogOpen(false);
      setStashMessage("");
      void refreshStashes();
      return res.output;
    });

  const toggleStashDetail = async (index: number) => {
    if (expandedStash === index) {
      setExpandedStash(null);
      return;
    }
    setExpandedStash(index);
    if (stashDetails[index] || projectId == null) return;
    setStashDetailLoading(index);
    try {
      const detail = await api.gitStashShow(projectId, index, scope);
      setStashDetails((prev) => ({ ...prev, [index]: detail }));
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      setStashDetailLoading((cur) => (cur === index ? null : cur));
    }
  };

  const doStashRestore = (op: "apply" | "pop", index: number) =>
    void run(`stash-${op}`, async () => {
      if (projectId == null) return;
      const res =
        op === "apply"
          ? await api.gitStashApply(projectId, index, scope)
          : await api.gitStashPop(projectId, index, scope);
      void refreshStashes();
      if (res.conflict) {
        setView("changes");
        setNoticeTone("warn");
        return `恢复贮藏产生 ${res.conflicts?.length ?? 0} 个冲突文件，请在「变更」视图处理。`;
      }
      setNoticeTone("info");
      return res.output || (op === "pop" ? "已弹出贮藏" : "已应用贮藏");
    });

  const doStashDrop = (index: number) =>
    void run("stash-drop", async () => {
      if (projectId == null) return;
      const res = await api.gitStashDrop(projectId, index, scope);
      setPending(null);
      if (expandedStash === index) setExpandedStash(null);
      void refreshStashes();
      return res.output || "已删除贮藏";
    });

  const statusMissing = !status;
  const syncDisabled = statusMissing || busy != null;
  const syncHint = (idle: string) => (statusMissing ? "仓库状态未就绪" : busy != null ? "正在同步…" : idle);
  const syncing = busy != null && SYNC_LABELS[busy] !== undefined;
  const aheadCount = status?.ahead ?? 0;
  const branchName = status?.detached ? `游离 ${status.oid ?? ""}` : status?.branch ?? "—";
  const currentRepoName = repos.find((r) => r.rel === repo)?.name ?? "仓库";

  /** 单行变更渲染 */
  const renderChangeRow = (r: SectionRow) => {
    const { dir, name } = splitPath(r.path);
    const active = selected?.section === r.section && selected?.path === r.path;
    const letter = stateLetter(r.code);
    const label = STATE_LABEL[r.code] ?? r.code;
    const stateCls = r.section === "untracked" ? "is-untracked" : `is-${letter.toLowerCase()}`;
    const tipText = r.oldPath ? `${r.oldPath} → ${r.path}` : r.path;
    const isDeleted = r.code === "D";

    return (
      <div key={`${r.section}:${r.path}`} className="gp-row-wrap">
        <div className={`gp-row${active ? " is-active" : ""}`}>
          <Tooltip content={tipText} side="top">
            <button
              type="button"
              className="gp-row-main"
              onClick={() => void openDiff(r.section, r.path)}
            >
              <span className="gp-file-ic"><FileIcon name={name} size={15} /></span>
              <span className="gp-name">{name}</span>
              {dir && <span className="gp-dir">{dir}</span>}
              {r.oldPath && <span className="gp-dir">← {r.oldPath}</span>}
              <Tooltip content={label} side="top">
                <span className={`gp-state ${stateCls}`} aria-label={label}>{letter}</span>
              </Tooltip>
            </button>
          </Tooltip>
          <span className="gp-row-ops">
            {!isDeleted && (
              <Tooltip content="在文件面板中打开" side="top">
                <IconButton
                  size="xs"
                  icon={<IconExternalLink size={12} />}
                  aria-label={`在文件面板中打开 ${name}`}
                  onClick={() => openInFilesPanel(r.path)}
                />
              </Tooltip>
            )}
            {r.section === "staged" && (
              <Tooltip content="取消暂存" side="top">
                <IconButton
                  size="xs"
                  icon={<IconMinus size={13} />}
                  aria-label={`取消暂存 ${name}`}
                  disabled={busy != null}
                  onClick={() => unstagePaths([r.path])}
                />
              </Tooltip>
            )}
            {r.section === "unstaged" && (
              <>
                <Tooltip content="丢弃改动" side="top">
                  <IconButton
                    size="xs"
                    icon={<IconRotateCcw size={12} />}
                    aria-label={`丢弃改动 ${name}`}
                    tone="danger"
                    disabled={busy != null}
                    onClick={() =>
                      setPending({
                        kind: "discard",
                        paths: [r.path],
                        untracked: [],
                        label: r.path,
                      })
                    }
                  />
                </Tooltip>
                <Tooltip content="暂存改动" side="top">
                  <IconButton
                    size="xs"
                    icon={<IconPlus size={13} />}
                    aria-label={`暂存 ${name}`}
                    disabled={busy != null}
                    onClick={() => stagePaths([r.path])}
                  />
                </Tooltip>
              </>
            )}
            {r.section === "untracked" && (
              <>
                <Tooltip content="删除未跟踪文件" side="top">
                  <IconButton
                    size="xs"
                    icon={<IconTrash size={12} />}
                    aria-label={`删除未跟踪文件 ${name}`}
                    tone="danger"
                    disabled={busy != null}
                    onClick={() =>
                      setPending({
                        kind: "discard",
                        paths: [],
                        untracked: [r.path],
                        label: r.path,
                      })
                    }
                  />
                </Tooltip>
                <Tooltip content="暂存文件" side="top">
                  <IconButton
                    size="xs"
                    icon={<IconPlus size={13} />}
                    aria-label={`暂存 ${name}`}
                    disabled={busy != null}
                    onClick={() => stagePaths([r.path])}
                  />
                </Tooltip>
              </>
            )}
          </span>
        </div>
        <Collapse open={active} lazy>
          {() => (
            <div className="gp-diff-inline">
              <div className="gp-diff-actions">
                <Button size="xs" variant="ghost" onClick={() => openInFilesPanel(r.path)}>打开文件</Button>
                <Button size="xs" variant="ghost" disabled={busy != null}
                  onClick={() => r.section === "staged" ? unstagePaths([r.path]) : stagePaths([r.path])}>
                  {r.section === "staged" ? "取消暂存" : "暂存"}
                </Button>
                <Button size="xs" variant="ghost" aria-pressed={wrapDiff} onClick={() => setWrapDiff(!wrapDiff)}>自动换行</Button>
              </div>
              {diffLoading ? (
                <div className="gp-diff-note">读取差异…</div>
              ) : diff && diff.path === r.path ? (
                <DiffView
                  diff={diff}
                  path={r.path}
                  wrap={wrapDiff}
                  hunkMode={r.section === "staged" ? "unstage" : r.section === "unstaged" ? "stage" : undefined}
                  hunkBusy={busy != null}
                  onHunkAction={
                    r.section === "staged" || r.section === "unstaged"
                      ? (patch) => handleHunkAction(r.section === "staged" ? "unstage" : "stage", patch)
                      : undefined
                  }
                />
              ) : (
                <div className="gp-diff-note">没有可显示的差异</div>
              )}
            </div>
          )}
        </Collapse>
      </div>
    );
  };

  /** 三段式分组渲染 */
  const renderSection = (opts: {
    key: SectionKey;
    title: string;
    rows: SectionRow[];
  }) => {
    const { key, title, rows } = opts;
    if (rows.length === 0) return null;
    const open = !collapsed[key];
    const allPaths = rows.map((r) => r.path);

    return (
      <section className="gp-section" key={key}>
        <div className="gp-section-head">
          <button
            type="button"
            className="gp-section-title"
            aria-expanded={open}
            onClick={() => toggleSection(key)}
          >
            <IconChevronDown size={12} className={"gp-section-caret" + (open ? " is-open" : "")} />
            <span className="gp-section-label">{title}</span>
            <span className="gp-count">{rows.length}</span>
          </button>
          <div className="gp-section-actions">
            {key === "staged" && (
              <Tooltip content="全部取消暂存" side="top">
                <IconButton
                  size="xs"
                  icon={<IconMinus size={13} />}
                  aria-label="全部取消暂存"
                  disabled={busy != null}
                  onClick={() => unstagePaths(allPaths)}
                />
              </Tooltip>
            )}
            {key === "unstaged" && (
              <>
                <Tooltip content="全部暂存" side="top">
                  <IconButton
                    size="xs"
                    icon={<IconPlus size={13} />}
                    aria-label="全部暂存更改"
                    disabled={busy != null}
                    onClick={() => stagePaths(allPaths)}
                  />
                </Tooltip>
                <Menu
                  align="end"
                  trigger={
                    <IconButton
                      size="xs"
                      icon={<IconMoreHorizontal size={14} />}
                      aria-label="更改批量操作"
                      disabled={busy != null}
                    />
                  }
                  entries={[
                    {
                      key: "stage-all",
                      label: "全部暂存",
                      onSelect: () => stagePaths(allPaths),
                    },
                    {
                      key: "stash-changes",
                      label: "贮藏当前改动…",
                      divider: true,
                      onSelect: () => openStashDialog(true),
                    },
                    {
                      key: "discard-all-unstaged",
                      label: "放弃全部工作区更改…",
                      danger: true,
                      onSelect: () =>
                        setPending({
                          kind: "discard",
                          paths: allPaths,
                          untracked: [],
                          label: `更改（${rows.length} 个文件）`,
                        }),
                    },
                  ]}
                />
              </>
            )}
            {key === "untracked" && (
              <>
                <Tooltip content="全部暂存" side="top">
                  <IconButton
                    size="xs"
                    icon={<IconPlus size={13} />}
                    aria-label="全部暂存未跟踪文件"
                    disabled={busy != null}
                    onClick={() => stagePaths(allPaths)}
                  />
                </Tooltip>
                <Menu
                  align="end"
                  trigger={
                    <IconButton
                      size="xs"
                      icon={<IconMoreHorizontal size={14} />}
                      aria-label="未跟踪文件批量操作"
                      disabled={busy != null}
                    />
                  }
                  entries={[
                    {
                      key: "stage-all-untracked",
                      label: "全部暂存",
                      onSelect: () => stagePaths(allPaths),
                    },
                    {
                      key: "stash-with-untracked",
                      label: "贮藏当前改动（含未跟踪）…",
                      divider: true,
                      onSelect: () => openStashDialog(true),
                    },
                    {
                      key: "delete-all-untracked",
                      label: "删除全部未跟踪文件…",
                      danger: true,
                      onSelect: () =>
                        setPending({
                          kind: "discard",
                          paths: [],
                          untracked: allPaths,
                          label: `未跟踪文件（${rows.length} 个文件）`,
                        }),
                    },
                  ]}
                />
              </>
            )}
          </div>
        </div>
        <Collapse open={open} lazy>
          {() => <div className="gp-section-body">{rows.map(renderChangeRow)}</div>}
        </Collapse>
      </section>
    );
  };

  /** 仓库下拉 */
  const repoPopover = (
    <Popover
      open={repoOpen}
      onClose={() => setRepoOpen(false)}
      placement="bottom-start"
      className="gp-repo-pop"
      ignoreRefs={[repoBtnRef]}
      role="menu"
      aria-label="切换仓库"
    >
      <div className="gp-repo-scroll">
        {repos.map((r) => (
          <button
            key={r.rel || "root"}
            type="button"
            role="menuitemradio"
            aria-checked={r.rel === repo}
            className={`gp-repo-item${r.rel === repo ? " is-current" : ""}`}
            onClick={() => chooseRepo(r.rel)}
          >
            <span className="gp-repo-item-main">
              <Tooltip content={r.rel ? `${r.name} (${r.rel})` : r.name} side="left">
                <span className="gp-repo-item-name">
                  {r.rel === repo && <IconCheck size={12} />}
                  <span>{r.name}</span>
                </span>
              </Tooltip>
              {r.rel && r.rel !== r.name && <span className="gp-repo-item-path">{r.rel}</span>}
            </span>
            <span className="gp-repo-item-side">
              <span className="gp-repo-item-branch">{r.detached ? "游离 HEAD" : r.branch ?? "—"}</span>
              <span className={`gp-repo-item-changes${r.changes > 0 ? "" : " is-clean"}`}>
                {r.changes > 0 ? `${r.changes} 个改动` : "无改动"}
              </span>
            </span>
          </button>
        ))}
      </div>
      <div className="gp-repo-sep">切换后，变更、历史、分支与提交都作用于所选仓库</div>
    </Popover>
  );

  /** 顶栏快速分支下拉 */
  const branchPopover = (
    <Popover
      open={branchOpen}
      onClose={() => setBranchOpen(false)}
      placement="bottom-start"
      className="gp-branch-pop"
      ignoreRefs={[branchBtnRef]}
      role="menu"
      aria-label="分支"
    >
      <div className="gp-branch-scroll">
        {(branches?.local ?? []).map((b) => (
          <div key={b.name} className={`gp-branch-item${b.current ? " is-current" : ""}`}>
            <Tooltip content={b.name} side="top">
              <button
                type="button"
                className="gp-branch-pick"
                role="menuitem"
                disabled={b.current || busy != null}
                onClick={() => {
                  setBranchOpen(false);
                  void doCheckout(b.name);
                }}
              >
                <span className="gp-branch-name">{b.name}</span>
                {b.current && <IconCheck size={13} />}
              </button>
            </Tooltip>
            {!b.current && (
              <Tooltip content="删除分支" side="left">
                <IconButton
                  size="xs"
                  icon={<IconTrash size={12} />}
                  aria-label={`删除分支 ${b.name}`}
                  tone="danger"
                  onClick={() => {
                    setBranchOpen(false);
                    setPending({ kind: "delete-branch", name: b.name });
                  }}
                />
              </Tooltip>
            )}
          </div>
        ))}
        {branches && branches.remote.length > 0 && (
          <>
            <div className="gp-branch-sep">远程分支</div>
            {branches.remote.slice(0, 40).map((b) => (
              <Tooltip key={b.name} content={b.name} side="top">
                <button
                  type="button"
                  className="gp-branch-pick is-remote"
                  role="menuitem"
                  disabled={busy != null}
                  onClick={() => {
                    setBranchOpen(false);
                    void doCheckout(b.name);
                  }}
                >
                  <span className="gp-branch-name">{b.name}</span>
                </button>
              </Tooltip>
            ))}
          </>
        )}
      </div>
      <div className="gp-branch-foot">
        <Button
          variant="ghost"
          size="sm"
          block
          icon={<IconPlus size={13} />}
          className="gp-branch-new-btn"
          onClick={() => {
            setBranchOpen(false);
            setNewBranch("");
            setNewBranchOpen(true);
          }}
        >
          新建分支
        </Button>
      </div>
    </Popover>
  );

  /** 历史视图 */
  const historyView = (
    <div className="gp-history">
      {openSha ? (
        <div className="gp-cv">
          <div className="gp-cv-nav">
            <Button variant="ghost" size="sm" icon={<IconArrowLeft size={13} />} onClick={closeCommit}>
              返回历史
            </Button>
          </div>
          {commitLoading && <div className="gp-diff-note">读取提交…</div>}
          {commit && (
            <div className="gp-cv-content">
              <div className="gp-cv-subject">{commit.subject}</div>
              <div className="gp-cv-meta">
                <span className="gp-hist-sha">{commit.short}</span>
                <span>{commit.author}</span>
                <time dateTime={commit.date}>{fmtDate(commit.date)}</time>
                <Button
                  variant="ghost"
                  size="xs"
                  icon={<IconCopy size={12} />}
                  onClick={() => {
                    void navigator.clipboard?.writeText(commit.sha);
                    setNoticeTone("info");
                    setNotice(`已复制提交哈希 ${commit.short}`);
                  }}
                >
                  复制完整 ID
                </Button>
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
                    const letter = stateLetter(f.code);
                    return (
                      <li key={f.path}>
                        <Tooltip content={f.path} side="top">
                          <button
                            type="button"
                            className={`gp-row-main gp-cv-file${active ? " is-active" : ""}`}
                            onClick={() => void openCommitFile(f.path)}
                          >
                            <span className="gp-file-ic"><FileIcon name={name} size={15} /></span>
                            <span className="gp-name">{name}</span>
                            {dir && <span className="gp-dir">{dir}</span>}
                            <Tooltip content={STATE_LABEL[f.code] ?? f.code} side="top">
                              <span className={`gp-state is-${letter.toLowerCase()}`} aria-label={STATE_LABEL[f.code] ?? f.code}>
                                {letter}
                              </span>
                            </Tooltip>
                            <span className="gp-cv-count">
                              {f.additions == null ? (
                                "二进制"
                              ) : (
                                <>
                                  <span className="gp-up">+{f.additions}</span>{" "}
                                  <span className="gp-down">−{f.deletions}</span>
                                </>
                              )}
                            </span>
                          </button>
                        </Tooltip>
                      </li>
                    );
                  })}
                </ul>
              )}
              {commitFile &&
                (commitDiffLoading ? (
                  <div className="gp-diff-note">读取差异…</div>
                ) : (
                  commitDiff && <DiffView diff={commitDiff} path={commitFile} />
                ))}
            </div>
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
                    <Tooltip content={c.subject} side="top">
                      <span className="gp-hist-subject">{c.subject}</span>
                    </Tooltip>
                    <span className="gp-hist-meta">
                      {c.refs.map((r) => (
                        <Tooltip key={`${r.kind}-${r.name}`} content={r.name} side="top">
                          <span className={`gp-ref is-${r.kind}`}>{r.name}</span>
                        </Tooltip>
                      ))}
                      <span className="gp-hist-sha">{c.short}</span>
                      <Tooltip content={c.author} side="top"><span className="gp-hist-who">{c.author}</span></Tooltip>
                      <Tooltip content={fmtDate(c.date)} side="top">
                        <span className="gp-hist-when">{relTime(c.date)}</span>
                      </Tooltip>
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
          {historyLoading && <div className="gp-diff-note">读取历史…</div>}
          {historyMore && !historyLoading && (
            <Button variant="ghost" size="sm" block onClick={() => void loadHistory(false)}>
              加载更多
            </Button>
          )}
        </>
      )}
    </div>
  );

  /** 分支视图 */
  const bfQuery = branchFilter.trim().toLowerCase();
  const localBranches = useMemo(
    () => (branches?.local ?? []).filter((b) => !bfQuery || b.name.toLowerCase().includes(bfQuery)),
    [branches, bfQuery],
  );
  const remoteBranches = useMemo(
    () => (branches?.remote ?? []).filter((b) => !bfQuery || b.name.toLowerCase().includes(bfQuery)),
    [branches, bfQuery],
  );

  const branchesView = (
    <div className="gp-subview">
      <div className="gp-subview-bar">
        <Input
          size="sm"
          clearable
          prefix={<IconSearch size={13} />}
          placeholder="筛选分支…"
          aria-label="筛选分支"
          value={branchFilter}
          onChange={(e) => setBranchFilter(e.target.value)}
        />
        <Button
          variant="secondary"
          size="sm"
          icon={<IconPlus size={13} />}
          disabled={busy != null}
          onClick={() => {
            setNewBranch("");
            setNewBranchOpen(true);
          }}
        >
          新建
        </Button>
      </div>
      {branchesLoading && !branches ? (
        <div className="gp-loading">读取分支列表…</div>
      ) : !branches ? (
        <div className="gp-empty-wrap"><Empty size="sm" mood="calm" title="无法读取分支列表" /></div>
      ) : (
        <div className="gp-subview-scroll">
          <div className="gp-group-head">
            <span>本地分支</span>
            <span className="gp-count">{localBranches.length}</span>
          </div>
          {localBranches.length === 0 ? (
            <div className="gp-diff-note">没有匹配的本地分支</div>
          ) : (
            <ul className="gp-list">
              {localBranches.map((b) => (
                <li key={b.name} className={`gp-list-row${b.current ? " is-current" : ""}`}>
                  <div className="gp-list-main">
                    <Tooltip content={b.name} side="top">
                      <span className="gp-list-title">
                        <IconGitBranch size={13} />
                        <span className="gp-mono-ellipsis">{b.name}</span>
                        {b.current && <Badge size="xs" tone="accent">当前</Badge>}
                      </span>
                    </Tooltip>
                    <div className="gp-list-meta">
                      {b.upstream && (
                        <Tooltip content={`上游：${b.upstream}`} side="top">
                          <span className="gp-mono-ellipsis">{b.upstream}</span>
                        </Tooltip>
                      )}
                      {b.date && <span>{relTime(b.date)}</span>}
                    </div>
                  </div>
                  <div className="gp-list-ops">
                    {!b.current && (
                      <>
                        <Button
                          variant="ghost"
                          size="xs"
                          disabled={busy != null}
                          onClick={() => void doCheckout(b.name)}
                        >
                          切换
                        </Button>
                        <Tooltip content="删除分支" side="top">
                          <IconButton
                            size="xs"
                            icon={<IconTrash size={12} />}
                            aria-label={`删除分支 ${b.name}`}
                            tone="danger"
                            disabled={busy != null}
                            onClick={() => setPending({ kind: "delete-branch", name: b.name })}
                          />
                        </Tooltip>
                      </>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {remoteBranches.length > 0 && (
            <>
              <div className="gp-group-head">
                <span>远程分支</span>
                <span className="gp-count">{remoteBranches.length}</span>
              </div>
              <ul className="gp-list">
                {remoteBranches.slice(0, 60).map((b) => (
                  <li key={b.name} className="gp-list-row">
                    <div className="gp-list-main">
                      <Tooltip content={b.name} side="top">
                        <span className="gp-list-title">
                          <IconGlobe size={13} />
                          <span className="gp-mono-ellipsis">{b.name}</span>
                        </span>
                      </Tooltip>
                      {b.date && <div className="gp-list-meta"><span>{relTime(b.date)}</span></div>}
                    </div>
                    <div className="gp-list-ops">
                      <Button
                        variant="ghost"
                        size="xs"
                        disabled={busy != null}
                        onClick={() => void doCheckout(b.name)}
                      >
                        检出
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );

  /** 贮藏（Stash）视图 */
  const stashView = (
    <div className="gp-subview">
      <div className="gp-subview-bar">
        <span className="gp-subview-hint">
          {stashes.length > 0 ? `共 ${stashes.length} 条贮藏` : "暂存工作区未提交的改动"}
        </span>
        <div className="gp-subview-bar-ops">
          <Tooltip content="刷新贮藏列表" side="top">
            <IconButton
              size="xs"
              icon={<IconRefresh size={13} />}
              aria-label="刷新贮藏列表"
              disabled={stashesLoading}
              onClick={() => void refreshStashes()}
            />
          </Tooltip>
          <Button
            variant="secondary"
            size="sm"
            icon={<IconArchive size={13} />}
            disabled={busy != null || !status || status.clean}
            onClick={() => openStashDialog(true)}
          >
            新建贮藏
          </Button>
        </div>
      </div>

      {stashesLoading && stashes.length === 0 ? (
        <div className="gp-loading">读取贮藏列表…</div>
      ) : stashes.length === 0 ? (
        <div className="gp-empty-wrap">
          <Empty
            size="sm"
            mood="calm"
            title="还没有贮藏记录"
            desc={status && !status.clean ? "可把当前未提交改动暂存到贮藏栈，稍后再恢复" : "工作区当前没有未提交改动"}
            action={
              status && !status.clean ? (
                <Button variant="secondary" size="sm" icon={<IconArchive size={13} />} onClick={() => openStashDialog(true)}>
                  贮藏当前改动
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : (
        <div className="gp-subview-scroll">
          <ul className="gp-stash-list">
            {stashes.map((s) => {
              const open = expandedStash === s.index;
              const detail = stashDetails[s.index];
              const loadingThis = stashDetailLoading === s.index;
              return (
                <li key={s.ref} className={`gp-stash-item${open ? " is-open" : ""}`}>
                  <div className="gp-stash-head">
                    <button
                      type="button"
                      className="gp-stash-toggle"
                      aria-expanded={open}
                      onClick={() => void toggleStashDetail(s.index)}
                    >
                      <IconChevronDown size={12} className={"gp-section-caret" + (open ? " is-open" : "")} />
                      <div className="gp-stash-info">
                        <div className="gp-stash-top">
                          <span className="gp-stash-ref">{s.ref}</span>
                          {s.branch && (
                            <Tooltip content={`分支：${s.branch}`} side="top">
                              <span className="gp-ref is-branch">{s.branch}</span>
                            </Tooltip>
                          )}
                          <Tooltip content={fmtDate(s.date)} side="top">
                            <span className="gp-stash-time">{relTime(s.date)}</span>
                          </Tooltip>
                        </div>
                        <Tooltip content={s.message} side="top">
                          <div className="gp-stash-msg">{s.message}</div>
                        </Tooltip>
                      </div>
                    </button>
                    <div className="gp-stash-ops">
                      <Tooltip content="应用（保留该条贮藏）" side="top">
                        <IconButton
                          size="xs"
                          icon={<IconPlay size={12} />}
                          aria-label={`应用 ${s.ref}`}
                          disabled={busy != null}
                          onClick={() => doStashRestore("apply", s.index)}
                        />
                      </Tooltip>
                      <Tooltip content="弹出（应用并从列表移除）" side="top">
                        <IconButton
                          size="xs"
                          icon={<IconArrowUp size={12} />}
                          aria-label={`弹出 ${s.ref}`}
                          disabled={busy != null}
                          onClick={() => doStashRestore("pop", s.index)}
                        />
                      </Tooltip>
                      <Tooltip content="删除该条贮藏" side="top">
                        <IconButton
                          size="xs"
                          icon={<IconTrash size={12} />}
                          aria-label={`删除 ${s.ref}`}
                          tone="danger"
                          disabled={busy != null}
                          onClick={() =>
                            setPending({
                              kind: "stash-drop",
                              index: s.index,
                              refName: s.ref,
                              message: s.message,
                            })
                          }
                        />
                      </Tooltip>
                    </div>
                  </div>
                  <Collapse open={open} lazy>
                    {() => (
                      <div className="gp-stash-detail">
                        {loadingThis ? (
                          <div className="gp-diff-note">读取文件变更…</div>
                        ) : detail ? (
                          <>
                            <div className="gp-stash-stat">
                              {detail.files.length} 个文件 · <span className="gp-up">+{detail.additions}</span>{" "}
                              <span className="gp-down">−{detail.deletions}</span>
                            </div>
                            <ul className="gp-stash-files">
                              {detail.files.map((f) => {
                                const { dir, name } = splitPath(f.path);
                                const letter = stateLetter(f.code);
                                return (
                                  <li key={f.path} className="gp-stash-file">
                                    <Tooltip content={f.path} side="top">
                                      <span className="gp-stash-file-main">
                                        <span className="gp-file-ic"><FileIcon name={name} size={14} /></span>
                                        <span className="gp-name">{name}</span>
                                        {dir && <span className="gp-dir">{dir}</span>}
                                      </span>
                                    </Tooltip>
                                    <span className={`gp-state is-${letter.toLowerCase()}`}>{letter}</span>
                                    <span className="gp-cv-count">
                                      {f.additions == null ? (
                                        "二进制"
                                      ) : (
                                        <>
                                          <span className="gp-up">+{f.additions}</span>{" "}
                                          <span className="gp-down">−{f.deletions}</span>
                                        </>
                                      )}
                                    </span>
                                  </li>
                                );
                              })}
                            </ul>
                            {detail.patch && <DiffView path="" wrap diff={{
                              path: "", text: detail.patch, truncated: !!detail.truncated, binary: false, new: false,
                            }} />}
                          </>
                        ) : (
                          <div className="gp-diff-note">暂无详情</div>
                        )}
                      </div>
                    )}
                  </Collapse>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );

  if (projectId == null) {
    return (
      <div className="gp-panel">
        <Empty size="sm" mood="calm" title="先选择一个项目" />
      </div>
    );
  }

  return (
    <div className="gp-panel">
      {/* 顶栏：仓库（多仓库时）+ 分支 + 远端同步 */}
      <div className="gp-head">
        <div className="gp-head-scope">
          {repos.length > 1 && (
            <div className="gp-repo-wrap">
              <Tooltip content={`当前仓库：${currentRepoName}`} side="bottom">
                <Button
                  ref={repoBtnRef}
                  variant="ghost"
                  size="sm"
                  className={`gp-repo-btn${repoOpen ? " is-open" : ""}`}
                  aria-haspopup="menu"
                  aria-expanded={repoOpen}
                  aria-label={`切换仓库，当前 ${currentRepoName}`}
                  onClick={() => {
                    const next = !repoOpen;
                    setRepoOpen(next);
                    if (next) void fetchRepos();
                  }}
                >
                  <IconLayers size={13} />
                  <span className="gp-repo-name">{currentRepoName}</span>
                  <IconChevronDown size={11} />
                </Button>
              </Tooltip>
              {repoPopover}
            </div>
          )}
          <div className="gp-branch-wrap">
            <Tooltip content={`当前分支：${branchName}`} side="bottom">
              <Button
                ref={branchBtnRef}
                variant="ghost"
                size="sm"
                className={`gp-branch-btn${branchOpen ? " is-open" : ""}`}
                aria-haspopup="menu"
                aria-expanded={branchOpen}
                onClick={() => setBranchOpen((v) => !v)}
              >
                <IconGitBranch size={13} />
                <span className="gp-branch-label">{branchName}</span>
                <IconChevronDown size={11} />
              </Button>
            </Tooltip>
            {status && (status.ahead > 0 || status.behind > 0) && (
              <Tooltip content={`领先 ${status.ahead} · 落后 ${status.behind}`} side="bottom">
                <span className="gp-sync-chip">
                  {status.ahead > 0 && <span className="gp-up"><IconArrowUp size={10} />{status.ahead}</span>}
                  {status.behind > 0 && <span className="gp-down"><IconArrowDown size={10} />{status.behind}</span>}
                </span>
              </Tooltip>
            )}
            {branchPopover}
          </div>
        </div>
        <div className="gp-head-ops">
          <Tooltip content={syncHint("抓取远端更新（不合并到本地）")} side="bottom">
            <IconButton
              size="sm"
              icon={<IconGlobe size={14} />}
              aria-label="抓取远端更新"
              loading={busy === "fetch"}
              disabled={syncDisabled}
              onClick={() => doSync("fetch")}
            />
          </Tooltip>
          <Tooltip content={syncHint((status?.behind ?? 0) > 0 ? `拉取并合并 ${status?.behind} 个远端提交` : "拉取并合并远端更新")} side="bottom">
            <IconButton
              size="sm"
              icon={<IconDownload size={14} />}
              aria-label="拉取并合并远端更新"
              loading={busy === "pull"}
              disabled={syncDisabled}
              onClick={() => doSync("pull")}
            />
          </Tooltip>
          <Tooltip content={syncHint(aheadCount > 0 ? `推送 ${aheadCount} 个提交到远端` : "没有待推送的提交")} side="bottom">
            <span className="gp-push-wrap">
              <IconButton
                size="sm"
                icon={<IconArrowUp size={14} />}
                aria-label="推送当前分支"
                loading={busy === "push"}
                disabled={syncDisabled || aheadCount === 0}
                onClick={() => doSync("push")}
              />
              {aheadCount > 0 && <span className="gp-push-badge" aria-hidden>{aheadCount}</span>}
            </span>
          </Tooltip>
          <Tooltip content="重新读取仓库状态" side="bottom">
            <IconButton
              size="sm"
              icon={<IconRefresh size={14} />}
              aria-label="刷新状态"
              disabled={loading}
              onClick={() => {
                void refresh();
                void refreshStashes();
                if (view === "branches") void refreshBranches();
              }}
            />
          </Tooltip>
        </div>
      </div>

      {/* 远端同步进度条（可真实取消后端子进程） */}
      <Collapse open={syncing}>
        <div className="gp-sync-bar" role="status" aria-live="polite">
          <span className="gp-sync-spin" aria-hidden><IconSpinner size={12} /></span>
          <span className="gp-sync-text">
            {SYNC_LABELS[busy ?? ""] ?? "正在同步"} · {({
              connecting: "正在连接", counting: "正在统计对象", compressing: "正在压缩对象",
              receiving: "正在接收对象", writing: "正在发送对象", resolving: "正在处理差异", cancelled: "正在取消",
            } as Record<string, string>)[syncProgress.phase ?? "connecting"] ?? "正在连接"}
            {syncProgress.percent != null && ` ${syncProgress.percent}%`} · {syncWaited}s
          </span>
          <Button variant="ghost" size="xs" className="gp-sync-cancel" loading={syncCancelPending}
            disabled={!activeSyncOpId.current || syncCancelPending} onClick={() => void cancelSync()}>
            取消
          </Button>
        </div>
      </Collapse>

      {/* 四段视图切换 + 变更筛选入口 */}
      <div className="gp-views">
        <Segmented
          value={view}
          onChange={setView}
          size="sm"
          block
          aria-label="Git 视图"
          options={[
            { value: "changes", label: "更改", count: dirtyCount > 0 ? dirtyCount : undefined },
            { value: "history", label: "历史" },
            { value: "branches", label: "分支" },
            { value: "stash", label: "贮藏", count: stashes.length > 0 ? stashes.length : undefined },
          ]}
        />
        {view === "changes" && dirtyCount > 0 && dirtyCount < 8 && (
          <Tooltip content={filterOpen ? "收起筛选" : "筛选变更文件"} side="bottom">
            <IconButton
              size="xs"
              active={filterOpen || filterQuery.trim().length > 0}
              icon={<IconSearch size={13} />}
              aria-label="筛选变更文件"
              onClick={() => {
                setFilterOpen((v) => {
                  if (v) setFilterQuery("");
                  return !v;
                });
              }}
            />
          </Tooltip>
        )}
      </div>

      {/* 全局提示条 */}
      <Collapse open={!!(notice || error)} lazy>
        {() => (
          <div className={`gp-notice${error ? " is-error" : noticeTone === "warn" ? " is-warn" : ""}`} role={error ? "alert" : "status"}>
            <span className="gp-notice-text">{error ?? notice}</span>
            <IconButton
              size="xs"
              icon={<IconX size={12} />}
              aria-label="关闭提示"
              onClick={() => {
                setNotice(null);
                setError(null);
                setNoticeTone("info");
              }}
            />
          </div>
        )}
      </Collapse>

      {view === "history" && historyView}
      {view === "branches" && branchesView}
      {view === "stash" && stashView}

      {view === "changes" && !status && !error && <div className="gp-loading">正在读取仓库状态…</div>}
      {view === "changes" && !status && error && (
        <div className="gp-empty-wrap">
          <Empty size="sm" mood="think" title="无法使用 Git" desc={error} />
        </div>
      )}

      {view === "changes" && status && (
        <>
          <Collapse open={showFilterBox} lazy>
            {() => (
              <div className="gp-filter-bar">
                <Input
                  size="sm"
                  clearable
                  prefix={<IconSearch size={13} />}
                  placeholder={`筛选变更文件（${dirtyCount}）…`}
                  aria-label="筛选变更文件"
                  value={filterQuery}
                  onChange={(e) => setFilterQuery(e.target.value)}
                />
              </div>
            )}
          </Collapse>

          <div className="gp-body">
            {/* 合并进行中 & 冲突区置顶 */}
            <Collapse open={merging} lazy>
              {() => (
                <div className="gp-merge-banner">
                  <IconAlertTriangle size={14} />
                  <span className="gp-merge-text">
                    {conflictCount > 0 ? `合并进行中，${conflictCount} 个文件有冲突` : "合并进行中，冲突已全部解决，可提交完成合并"}
                  </span>
                  <Button
                    variant="ghost"
                    size="xs"
                    disabled={busy != null}
                    onClick={() =>
                      void run("abort", async () => {
                        await api.gitMergeAbort(projectId, scope);
                        return "已中止合并";
                      })
                    }
                  >
                    中止合并
                  </Button>
                </div>
              )}
            </Collapse>

            <Collapse open={conflictCount > 0} lazy>
              {() => (
                <div className="gp-conflict-box">
                  <div className="gp-conflict-help">冲突文件需先选择保留哪一边，处理完再一起提交。</div>
                  <div className="gp-conflict-actions">
                    {conflictRows.map((c) => {
                      const { name } = splitPath(c.path);
                      return (
                        <div key={`cf-${c.path}`} className="gp-conflict-row">
                          <Tooltip content={c.path} side="top">
                            <span className="gp-conflict-name">
                              <span className="gp-file-ic"><FileIcon name={name} size={14} /></span>
                              <span className="gp-mono-ellipsis">{c.path}</span>
                            </span>
                          </Tooltip>
                          <div className="gp-conflict-ops">
                            <Tooltip content="在文件面板中打开" side="top">
                              <IconButton
                                size="xs"
                                icon={<IconExternalLink size={12} />}
                                aria-label={`在文件面板中打开 ${name}`}
                                onClick={() => openInFilesPanel(c.path)}
                              />
                            </Tooltip>
                            <Button
                              variant="ghost"
                              size="xs"
                              disabled={busy != null}
                              onClick={() =>
                                void run("resolve", async () => {
                                  await api.gitResolve(projectId, c.path, "ours", scope);
                                })
                              }
                            >
                              采用我方
                            </Button>
                            <Button
                              variant="ghost"
                              size="xs"
                              disabled={busy != null}
                              onClick={() =>
                                void run("resolve", async () => {
                                  await api.gitResolve(projectId, c.path, "theirs", scope);
                                })
                              }
                            >
                              采用对方
                            </Button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </Collapse>

            {/* 三段式分区：已暂存的更改 / 更改 / 未跟踪 */}
            {renderSection({ key: "staged", title: "已暂存的更改", rows: filteredStaged })}
            {renderSection({ key: "unstaged", title: "更改", rows: filteredUnstaged })}
            {renderSection({ key: "untracked", title: "未跟踪", rows: filteredUntracked })}

            {normFilter &&
              filteredStaged.length === 0 &&
              filteredUnstaged.length === 0 &&
              filteredUntracked.length === 0 &&
              dirtyCount > 0 && (
                <div className="gp-filter-empty">
                  <span>没有匹配「{filterQuery.trim()}」的变更文件</span>
                  <Button variant="ghost" size="xs" onClick={() => setFilterQuery("")}>清除筛选</Button>
                </div>
              )}

            {status.clean && !merging && (
              <div className="gp-clean">
                <IconCheck size={16} />
                <span>工作区干净，没有未提交的改动</span>
              </div>
            )}
          </div>

          {/* 固定在底部的提交区 */}
          <div className="gp-commit">
            <Textarea
              className="gp-commit-input"
              rows={2}
              value={message}
              placeholder={stagedCount > 0 || amend ? "填写提交信息（Ctrl+Enter 提交）" : "先暂存要提交的文件"}
              aria-label="提交信息"
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && canCommit) {
                  e.preventDefault();
                  void doCommit();
                }
              }}
            />
            <div className="gp-commit-guidance">
              <Button size="xs" variant="ghost" disabled={busy != null || !!message.trim() || stagedCount === 0}
                onClick={() => setMessage(stagedRows.length === 1 ? `更新 ${stagedRows[0].path}` : `更新 ${stagedCount} 个文件`)}>
                填入提交说明
              </Button>
              <span className={message.split("\n")[0].length > 72 ? "is-long" : undefined}>
                首行 {message.split("\n")[0].length} / 72
              </span>
            </div>
            <div className="gp-commit-foot">
              <Tooltip content="把这次改动并入上一次提交，不新增提交记录" side="top">
                <span className="gp-amend-wrap">
                  <Checkbox
                    className="gp-amend"
                    label="修改上次提交"
                    checked={amend}
                    onChange={setAmend}
                    disabled={busy != null}
                  />
                </span>
              </Tooltip>
              <div className="gp-commit-actions">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!canCommit}
                  loading={busy === "commit"}
                  onClick={() => void doCommit()}
                >
                  提交{stagedCount > 0 ? `（${stagedCount}）` : ""}
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  disabled={!canCommit}
                  loading={busy === "commit-push"}
                  onClick={() => void doCommitAndPush()}
                >
                  提交并推送
                </Button>
                <Menu
                  align="end"
                  side="top"
                  trigger={
                    <IconButton
                      size="sm"
                      icon={<IconMoreHorizontal size={14} />}
                      aria-label="更多提交操作"
                      disabled={busy != null}
                    />
                  }
                  entries={[
                    {
                      key: "reset-soft",
                      label: "撤销上次提交 (Soft Reset)…",
                      icon: <IconRotateCcw size={13} />,
                      onSelect: () => setPending({ kind: "reset-soft" }),
                    },
                    {
                      key: "stash-now",
                      label: "贮藏当前改动…",
                      icon: <IconArchive size={13} />,
                      disabled: status.clean,
                      onSelect: () => openStashDialog(true),
                    },
                  ]}
                />
              </div>
            </div>
          </div>
        </>
      )}

      {/* 切换分支被本地改动阻止 */}
      <Dialog
        open={dirtySwitch != null}
        onClose={() => setDirtySwitch(null)}
        title="本地改动阻止切换分支"
        subtitle={dirtySwitch ? `切换到「${dirtySwitch.name}」前，需要先处理下列未提交的改动` : undefined}
        width={460}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDirtySwitch(null)}>取消</Button>
            <Button
              variant="danger-ghost"
              disabled={busy != null}
              onClick={() => dirtySwitch && void doCheckout(dirtySwitch.name, "discard")}
            >
              放弃改动并切换
            </Button>
            <Button
              variant="primary"
              loading={busy === "checkout"}
              disabled={busy != null}
              onClick={() => dirtySwitch && void doCheckout(dirtySwitch.name, "stash")}
            >
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

      {/* 新建分支 */}
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
          void run("branch", async () => {
            const out = (await api.gitBranch(projectId, "create", name, {}, scope)).output;
            void refreshBranches();
            return out;
          });
        }}
        width={420}
      >
        <Input
          value={newBranch}
          placeholder="如：feat/login"
          aria-label="新分支名称"
          onChange={(e) => setNewBranch(e.target.value)}
          autoFocus
        />
      </FormDialog>

      {/* 新建贮藏 */}
      <FormDialog
        open={stashDialogOpen}
        onClose={() => setStashDialogOpen(false)}
        title="新建贮藏"
        subtitle="将当前工作区改动存入贮藏栈，使工作区恢复干净"
        submitLabel="贮藏"
        submitDisabled={busy != null || !status || status.clean}
        onSubmit={doStashPush}
        width={420}
      >
        <div className="gp-stash-form">
          <Input
            value={stashMessage}
            placeholder="可选：贮藏说明（如 WIP 登录校验）"
            aria-label="贮藏说明"
            onChange={(e) => setStashMessage(e.target.value)}
            autoFocus
          />
          <Checkbox
            label="包含未跟踪文件"
            checked={stashIncludeUntracked}
            onChange={setStashIncludeUntracked}
          />
        </div>
      </FormDialog>

      {/* 丢弃改动 / 删除未跟踪 */}
      <ConfirmDialog
        open={pending?.kind === "discard"}
        title={pending?.kind === "discard" && pending.paths.length === 0 ? "删除未跟踪文件" : "丢弃改动"}
        message={
          pending?.kind === "discard"
            ? pending.paths.length === 0
              ? `将永久删除未跟踪文件「${pending.label}」，且无法恢复。确定继续吗？`
              : `将丢弃「${pending.label}」的工作区改动，保留已暂存的版本。确定继续吗？`
            : ""
        }
        confirmLabel={pending?.kind === "discard" && pending.paths.length === 0 ? "删除" : "丢弃"}
        danger
        busy={busy === "discard"}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          if (pending?.kind === "discard") void doDiscard(pending);
        }}
      />

      {/* 删除分支 */}
      <ConfirmDialog
        open={pending?.kind === "delete-branch"}
        title="删除分支"
        message={
          pending?.kind === "delete-branch"
            ? `将删除本地分支「${pending.name}」。只有已合并的分支会被删除，否则 git 会拒绝。`
            : ""
        }
        confirmLabel="删除"
        danger
        busy={busy === "branch"}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          if (pending?.kind === "delete-branch") void doDeleteBranch(pending.name);
        }}
      />

      {/* 撤销上次提交（Soft Reset） */}
      <ConfirmDialog
        open={pending?.kind === "reset-soft"}
        title="撤销上次提交"
        message="将执行 git reset --soft HEAD~1：回退最近一次提交记录，所有改动保留在「已暂存的更改」中，旧提交说明会回填到输入框。确定继续吗？"
        confirmLabel="撤销提交"
        busy={busy === "reset-soft"}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          void doResetSoft();
        }}
      />

      {/* 删除贮藏 */}
      <ConfirmDialog
        open={pending?.kind === "stash-drop"}
        title="删除贮藏"
        message={
          pending?.kind === "stash-drop"
            ? `将永久删除贮藏「${pending.refName}：${pending.message}」，且无法恢复。确定继续吗？`
            : ""
        }
        confirmLabel="删除"
        danger
        busy={busy === "stash-drop"}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          if (pending?.kind === "stash-drop") void doStashDrop(pending.index);
        }}
      />
    </div>
  );
}
