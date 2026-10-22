/** Git 面板（plan-20261017-goal12 项 5）：右侧面板里的常规 Git 操作。
 *
 * 两个视图：「变更」——冲突（合并进行中时置顶）→ 已暂存 → 未暂存 → 未跟踪 → 提交框；
 *          「历史」——提交列表（分支 / 标签装饰）→ 单个提交的信息、变更文件与差异。
 * 点击任一文件在下方查看差异；丢弃改动、删除分支等不可逆操作先确认。
 * 状态在打开面板、完成操作、AI 结束一轮改动后刷新。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type GitBranchesOut, type GitCommitDetailOut, type GitDiffOut, type GitLogEntry, type GitRepoOut, type GitStatusOut } from "../../api/client";
import { findSession, useChatStore } from "../../store/chat";
import { bucketSessionId, usePanelStore } from "../../store/panel";
import { ConfirmDialog } from "../ConfirmDialog";
import { Button, Checkbox, Collapse, Dialog, Empty, FormDialog, IconButton, Input, Menu, Popover, Segmented, Tooltip } from "../ui";
import { IconAlertTriangle, IconArrowDown, IconArrowUp, IconCheck, IconChevronDown, IconDownload, IconGitBranch, IconGlobe, IconLayers, IconMoreHorizontal, IconPlus, IconRefresh, IconSpinner, IconTrash, IconX } from "../icons";
import { FileIcon } from "../iconkit/FileIcon";
import { highlightLines } from "../chat/CodeView";

type Section = "conflicts" | "staged" | "unstaged" | "untracked";

/** plan-522-2209：联网同步操作的中文阶段名。顶栏进度行据此展示「正在拉取并合并 · 已等待 Ns」。 */
const SYNC_LABELS: Record<string, string> = {
  fetch: "正在抓取远端更新",
  pull: "正在拉取并合并",
  push: "正在推送",
};

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
  // plan-156-621：Git 面板绑定「当前面板桶所属会话」的项目，而不是全局当前项目——
  // 面板按会话保活，切会话后后台桶不应跟随当前项目漂移；桶里没有会话时才回退全局。
  const activeBucket = usePanelStore((s) => s.activeBucket);
  const fallbackProjectId = useChatStore((s) => s.currentProjectId);
  const bucketProjectId = useChatStore((s) => {
    const sid = bucketSessionId(activeBucket);
    return sid == null ? null : findSession(s, sid)?.project_id ?? null;
  });
  const projectId = bucketProjectId ?? fallbackProjectId;
  const isRunning = useChatStore((s) => s.isRunning);

  const [status, setStatus] = useState<GitStatusOut | null>(null);
  /** plan-20261019-git-multirepo：项目内的仓库列表；多于一个时顶栏出现仓库切换。
   *  repo 记录所选仓库与它所属项目——切换项目时不会沿用上一个项目的仓库路径。 */
  const [repos, setRepos] = useState<GitRepoOut[]>([]);
  const [repoSel, setRepoSel] = useState<{ projectId: number | null; rel: string }>({ projectId: null, rel: "" });
  const [reposReady, setReposReady] = useState(false);
  const [repoOpen, setRepoOpen] = useState(false);
  const repo = repoSel.projectId === projectId ? repoSel.rel : "";
  const repoBtnRef = useRef<HTMLButtonElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  /** plan-522-2209：同步已等待秒数（本地计时）。后端一次拉取可能要一两分钟，
   *  只把按钮置灰用户看不出是否在动，这里给出可读的等待时长。 */
  const [syncWaited, setSyncWaited] = useState(0);
  /** plan-522-2209：写操作的序号。取消等待、切换项目/仓库后自增，晚到的旧响应不得再改当前视图。 */
  const runSeq = useRef(0);
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
      const st = await api.gitStatus(projectId, { repo });
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
  }, [projectId, repo]);

  /** 读取历史第一页（reset）或追加下一页。以 skip 翻页，不依赖页内游标。 */
  const loadHistory = useCallback(async (reset: boolean) => {
    if (projectId == null) return;
    const mine = ++histSeq.current;
    setHistoryLoading(true);
    try {
      const skip = reset ? 0 : history.length;
      const page = await api.gitLog(projectId, HISTORY_PAGE, skip, { repo });
      if (mine !== histSeq.current) return;
      setHistory((prev) => (reset ? page : [...prev, ...page]));
      setHistoryMore(page.length === HISTORY_PAGE);
    } catch (e) {
      if (mine !== histSeq.current) return;
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      setHistoryLoading(false);
    }
  }, [projectId, history.length, repo]);

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
      const detail = await api.gitCommitDetail(projectId, sha, { repo });
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
      const d = await api.gitCommitDiff(projectId, openSha, path, { repo });
      if (mine === diffSeq.current) setCommitDiff(d);
    } catch (e) {
      if (mine === diffSeq.current) setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      if (mine === diffSeq.current) setCommitDiffLoading(false);
    }
  };

  const refreshBranches = useCallback(async () => {
    if (projectId == null) return;
    try { setBranches(await api.gitBranches(projectId, { repo })); } catch { setBranches(null); }
  }, [projectId, repo]);

  /** plan-20261019-git-multirepo：重新读取仓库列表（打开下拉时刷新一次改动数）。 */
  const fetchRepos = useCallback(async () => {
    if (projectId == null) return;
    try { setRepos(await api.gitRepos(projectId)); } catch { /* 读取失败保留现有列表 */ }
  }, [projectId]);

  /** 切换仓库：记住选择，清空旧仓库状态，交给刷新 effect 重新读取。 */
  const chooseRepo = (rel: string) => {
    if (projectId == null) return;
    setRepoOpen(false);
    setStatus(null);
    setBranches(null);
    setRepoSel({ projectId, rel });
    try {
      if (rel) localStorage.setItem(`chatcoder.git.repo.${projectId}`, rel);
      else localStorage.removeItem(`chatcoder.git.repo.${projectId}`);
    } catch { /* 记忆失败不影响切换 */ }
  };

  // 项目变化时重新识别仓库，并恢复该项目上次选择的仓库；识别完成前不发状态请求，
  // 避免拿上一个项目的仓库路径去请求新项目。
  useEffect(() => {
    if (!visible || projectId == null) return;
    let cancelled = false;
    setRepoOpen(false);
    setReposReady(false);
    setRepos([]);
    void api.gitRepos(projectId)
      .then((list) => {
        if (cancelled) return;
        setRepos(list);
        let saved = "";
        try { saved = localStorage.getItem(`chatcoder.git.repo.${projectId}`) ?? ""; } catch { saved = ""; }
        const pick = list.find((r) => r.rel === saved) ?? list.find((r) => r.is_root) ?? list[0];
        setRepoSel({ projectId, rel: pick?.rel ?? "" });
        setReposReady(true);
      })
      .catch(() => {
        if (cancelled) return;
        // 不是 Git 项目：回到单仓库模式，沿用原来的错误提示
        setRepos([]);
        setRepoSel({ projectId, rel: "" });
        setReposReady(true);
      });
    return () => { cancelled = true; };
  }, [projectId, visible]);

  // 打开 / 切换项目或仓库时刷新；面板隐藏时不请求。历史只在历史视图下才读取
  useEffect(() => {
    if (!visible || !reposReady) return;
    // plan-522-2209：切换后必须放弃上一个项目/仓库的在途同步——否则按钮会一直保持
    // 置灰（busy 没有对应请求来复位），且旧响应回来还会把结果写进新视图。
    runSeq.current++;
    setBusy(null);
    setSyncWaited(0);
    setSelected(null);
    setDiff(null);
    setHistory([]);
    closeCommit();
    void refresh();
  }, [projectId, repo, visible, reposReady, refresh]);

  // plan-522-2209：联网同步期间本地计时，进度行展示「已等待 Ns」。
  // 只依赖 busy 是否存在：busy 变化即重新起算，避免跨操作累加。
  useEffect(() => {
    if (busy == null) { setSyncWaited(0); return; }
    const started = Date.now();
    setSyncWaited(0);
    const timer = window.setInterval(() => {
      setSyncWaited(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    if (!visible || view !== "history" || !reposReady) return;
    void loadHistory(true);
    // 仅在项目 / 仓库 / 视图切换时整页重读；翻页由「加载更多」负责
  }, [projectId, repo, visible, view, reposReady]);

  /** plan-20261019-git-multirepo：工具卡路径属于哪个仓库——取最长匹配前缀，根仓库（空串）兜底。 */
  const repoOfPath = useCallback((path: string): string => {
    const norm = path.replace(/\\/g, "/");
    let hit = "";
    for (const r of repos) {
      if (r.rel && (norm === r.rel || norm.startsWith(`${r.rel}/`)) && r.rel.length > hit.length) hit = r.rel;
    }
    return hit;
  }, [repos]);

  /** plan-156-620：工具卡点击文件 → 状态就绪后切到变更视图并展开该文件差异（消费一次即清空）。 */
  const gitFocus = usePanelStore((s) => s.gitFocus);
  useEffect(() => {
    if (!visible || !gitFocus || !status || !reposReady) return;
    // plan-156-621：焦点不属于本面板项目时直接丢弃——跨项目保活时把旧路径拿到当前仓库请求差异，
    // 只会得到「文件不存在」或读到同名但无关的文件。
    if (gitFocus.projectId != null && projectId != null && gitFocus.projectId !== projectId) {
      usePanelStore.getState().clearGitFocus();
      return;
    }
    // plan-20261019-git-multirepo：先切到文件所属仓库，焦点保留到新仓库状态就绪后再展开，
    // 否则会用旧仓库的状态去匹配新仓库的路径。路径前缀比调用方传的 repo 更可靠（前缀最长优先）。
    const target = repoOfPath(gitFocus.path) || gitFocus.repo;
    if (target !== repo) { chooseRepo(target); return; }
    usePanelStore.getState().clearGitFocus();
    // 工具卡给的是项目内路径，单仓库请求需要去掉仓库前缀
    const path = repo && (gitFocus.path === repo || gitFocus.path.startsWith(`${repo}/`))
      ? gitFocus.path.slice(repo.length + 1)
      : gitFocus.path;
    setView("changes");
    if (selected?.path === path) return;
    setSelected({ path, staged: gitFocus.staged });
    void loadDiff(path, gitFocus.staged);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gitFocus, status, visible, projectId, repo, reposReady]);

  // AI 一轮结束（运行态回落）后，工作区很可能已变化：刷新一次
  const wasRunning = useRef(isRunning);
  useEffect(() => {
    if (wasRunning.current && !isRunning && visible) void refresh();
    wasRunning.current = isRunning;
  }, [isRunning, visible, refresh]);

  // plan-20261019-toolcard-v2：会话工具卡里切换分支 / 暂存后广播该事件，面板可见时同步刷新
  useEffect(() => {
    if (!visible) return;
    const onChanged = () => { void refresh(); };
    window.addEventListener("chatcoder:git-changed", onChanged);
    return () => window.removeEventListener("chatcoder:git-changed", onChanged);
  }, [visible, refresh]);

  useEffect(() => {
    if (branchOpen) void refreshBranches();
  }, [branchOpen, refreshBranches]);

  /** plan-522-2209：同步类写操作的取消——只结束界面等待，不中断后端 git 进程。
   *
   *  后端是 HTTP 请求-响应模型，一次 pull 已经跑起来就停不掉（强杀 git 可能留下 index.lock，
   *  反而让之后所有 git 操作失败）。因此「取消」的语义是：界面立刻退出等待态、隐藏进度行，
   *  并把这次操作标记作废——它最终成功或失败都不再改动当前视图。 */
  const cancelSync = () => {
    runSeq.current++;          // 作废在途响应
    setBusy(null);
    setSyncWaited(0);
    setNotice("已停止等待，同步请求仍在后台继续，完成后可点刷新查看结果");
    setNoticeTone("warn");
  };

  /** 执行一个写操作：无论成败都刷新状态（部分成功时，如提交成功、推送失败，列表必须反映已落盘的结果）；
   *  失败以提示条呈现，不打断浏览。refresh 成功会清空 error，因此失败文案在刷新之后写入。 */
  const run = async (label: string, fn: () => Promise<string | void>) => {
    if (projectId == null) return;
    // plan-522-2209：setBusy(null) 原先放在末尾、refresh 之后，异常路径（或刷新抛错）会让
    // 按钮永久置灰；改用 finally 保证一定复位。序号用于让「取消 / 切换项目」后的旧响应失效。
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
      // 已被取消 / 切换：这次操作的结果作废，不写回当前视图
      if (mine !== runSeq.current) return;
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
    } finally {
      // 刷新也走完后才解除忙碌：否则按钮已可点、状态还在读，用户会以为没生效
      if (mine === runSeq.current) setBusy(null);
    }
  };

  /** 读取某个文件的差异。只保留最后一次请求的结果：快速切换文件时晚到的旧结果不会覆盖当前选中。 */
  const loadDiff = async (path: string, staged: boolean) => {
    if (projectId == null) return;
    const mine = ++workDiffSeq.current;
    setDiffLoading(true);
    try {
      const d = await api.gitDiff(projectId, path, staged, { repo });
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

  // plan-522-2209：把「为什么不能点」拆成两个独立原因——此前两者共用一个布尔量，
  // 提示永远只有「正在同步」，仓库状态尚未读取时也会这么说，用户以为在同步而在等。
  const statusMissing = !status;
  const syncDisabled = statusMissing || busy != null;
  /** 同步按钮的提示文案：随禁用原因变化；可用时用动作自身的说明。 */
  const syncHint = (idle: string) => (statusMissing ? "仓库状态未就绪" : busy != null ? "正在同步…" : idle);
  /** 当前是否为联网同步（决定顶栏进度行是否出现、哪个按钮显 loading）。 */
  const syncing = busy != null && SYNC_LABELS[busy] !== undefined;

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
    if (r.staged) await api.gitUnstage(projectId, [r.path], { repo });
    else await api.gitStage(projectId, [r.path], { repo });
  });

  /** 一组内的全选 / 全不选 */
  const toggleAllRows = (rows: ChangeRow[], stage: boolean) =>
    void run(stage ? "stage-all" : "unstage-all", async () => {
      if (projectId == null) return;
      const targets = rows.filter((r) => (stage ? !r.staged : r.staged)).map((r) => r.path);
      if (targets.length === 0) return;
      if (stage) await api.gitStage(projectId, targets, { repo });
      else await api.gitUnstage(projectId, targets, { repo });
    });

  const canCommit = message.trim().length > 0 && (stagedCount > 0 || amend) && !busy;

  const doCommit = () => run("commit", async () => {
    if (projectId == null) return;
    const res = await api.gitCommit(projectId, message.trim(), amend, { repo });
    setMessage("");
    setAmend(false);
    void loadHistory(true);
    await refresh();
    return `已提交 ${res.sha}`;
  });

  /** 提交并推送：一次完成，不需要再去顶栏找推送入口 */
  const doCommitAndPush = () => run("commit-push", async () => {
    if (projectId == null) return;
    await api.gitCommit(projectId, message.trim(), amend, { repo });
    setMessage("");
    setAmend(false);
    void loadHistory(true);
    const out = await api.gitSync(projectId, "push", { repo });
    await refresh();
    return out.output || "已提交并推送";
  });

  const doDiscard = (p: Extract<Pending, { kind: "discard" }>) => run("discard", async () => {
    if (projectId == null) return;
    await api.gitDiscard(projectId, p.paths, p.untracked, { repo });
    setPending(null);
    return "已丢弃改动";
  });

  const doDeleteBranch = (name: string) => run("branch", async () => {
    if (projectId == null) return;
    const out = await api.gitBranch(projectId, "delete", name, {}, { repo });
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
        {/* plan-20261019-motion-sweep：差异就地展开 / 收起走 Collapse（高度过渡，收起结束才卸载）；
            收起时 openDiff 会同步清空 diff，Collapse 冻结最后一次展开的内容，不会在收起过程中闪成「没有可显示的差异」。 */}
        <Collapse open={active} lazy>
          {() => (
            <div className="gp-diff-inline">
              {diffLoading
                ? <div className="gp-diff-note">读取差异…</div>
                : diff && diff.path === r.path
                  ? <DiffView diff={diff} path={r.path} />
                  : <div className="gp-diff-note">没有可显示的差异</div>}
            </div>
          )}
        </Collapse>
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
            {/* 同一枚箭头旋转表达开合（此前展开 / 收起互换两个图标，是硬切） */}
            <IconChevronDown size={12} className={"gp-section-caret" + (open ? " is-open" : "")} />
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
        <Collapse open={open} lazy>{() => <div className="gp-section-body">{rows.map(changeRow)}</div>}</Collapse>
      </div>
    );
  };

  /** 拉取：冲突不当作失败——刷新状态并切到变更视图，冲突区会置顶 */
  const doPull = () => run("pull", async () => {
    if (projectId == null) return;
    const res = await api.gitSync(projectId, "pull", { repo });
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
      const res = await api.gitBranch(projectId, "checkout", name, { onDirty }, { repo });
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
    return (await api.gitSync(projectId, "fetch", { repo })).output;
  });
  const doPush = () => void run("push", async () => {
    if (projectId == null) return;
    return (await api.gitSync(projectId, "push", { repo })).output;
  });
  const aheadCount = status?.ahead ?? 0;

  /** plan-20261019-git-multirepo：仓库下拉——名称、相对路径、分支与改动数同屏，当前项打勾。 */
  const repoPopover = (
    <Popover open={repoOpen} onClose={() => setRepoOpen(false)} placement="bottom-start"
      className="gp-repo-pop" ignoreRefs={[repoBtnRef]} role="menu" aria-label="切换仓库">
      <div className="gp-repo-scroll">
        {repos.map((r) => (
          <button key={r.rel || "root"} type="button" role="menuitemradio" aria-checked={r.rel === repo}
            className={`gp-repo-item${r.rel === repo ? " is-current" : ""}`} onClick={() => chooseRepo(r.rel)}>
            <span className="gp-repo-item-main">
              <span className="gp-repo-item-name">
                {r.rel === repo && <IconCheck size={12} />}
                <span>{r.name}</span>
              </span>
              {/* 位于项目根下一层的仓库，相对路径与名称相同：重复的路径行不再显示 */}
              {(r.rel && r.rel !== r.name) && <span className="gp-repo-item-path">{r.rel}</span>}
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
  const currentRepoName = repos.find((r) => r.rel === repo)?.name ?? "仓库";

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
      {/* 顶栏：仓库（多仓库时）+ 分支 + 同步 */}
      <div className="gp-head">
        <div className="gp-head-scope">
          {repos.length > 1 && (
            <div className="gp-repo-wrap">
              <button ref={repoBtnRef} type="button" className={`gp-repo-btn${repoOpen ? " is-open" : ""}`}
                aria-haspopup="menu" aria-expanded={repoOpen} aria-label={`切换仓库，当前 ${currentRepoName}`}
                onClick={() => {
                  const next = !repoOpen;
                  setRepoOpen(next);
                  if (next) void fetchRepos();
                }}>
                <IconLayers size={13} />
                <span className="gp-repo-name">{currentRepoName}</span>
                <IconChevronDown size={11} />
              </button>
              {repoPopover}
            </div>
          )}
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
        </div>
        <div className="gp-head-ops">
          <Tooltip content={syncHint("抓取远端更新（不合并到本地）")} side="bottom">
            <IconButton icon={<IconGlobe size={14} />} aria-label="抓取远端更新"
              loading={busy === "fetch"} disabled={syncDisabled} onClick={doFetch} />
          </Tooltip>
          <Tooltip content={syncHint((status?.behind ?? 0) > 0 ? `拉取并合并 ${status?.behind} 个远端提交` : "拉取并合并远端更新")} side="bottom">
            <IconButton icon={<IconDownload size={14} />} aria-label="拉取并合并远端更新"
              loading={busy === "pull"} disabled={syncDisabled} onClick={() => void doPull()} />
          </Tooltip>
          <Tooltip content={syncHint(aheadCount > 0 ? `推送 ${aheadCount} 个提交到远端` : "没有待推送的提交")} side="bottom">
            <span className="gp-push-wrap">
              <IconButton icon={<IconArrowUp size={14} />} aria-label="推送当前分支"
                loading={busy === "push"} disabled={syncDisabled || aheadCount === 0} onClick={doPush} />
              {aheadCount > 0 && <span className="gp-push-badge" aria-hidden>{aheadCount}</span>}
            </span>
          </Tooltip>
          <Tooltip content="重新读取仓库状态" side="bottom">
            <IconButton icon={<IconRefresh size={14} />} aria-label="刷新状态" disabled={loading} onClick={() => void refresh()} />
          </Tooltip>
        </div>
      </div>

      {/* plan-522-2209：联网同步的常驻进度行。此前点完同步只有三个按钮一起变灰，
          既没有阶段也没有时长，几秒和两分钟的观感完全一样；这里给出阶段 + 已等待秒数 + 取消。
          走 Collapse（不用条件渲染），出现/收起都不抖动下方列表。 */}
      <Collapse open={syncing}>
        <div className="gp-sync-bar" role="status" aria-live="polite">
          <span className="gp-sync-spin" aria-hidden><IconSpinner size={12} /></span>
          <span className="gp-sync-text">
            {SYNC_LABELS[busy ?? ""] ?? "正在同步"} · 已等待 {syncWaited}s
          </span>
          <Button variant="ghost" size="sm" className="gp-sync-cancel" onClick={cancelSync}>取消等待</Button>
        </div>
      </Collapse>

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

      {/* plan-20261019-motion-sweep：提示条走 Collapse——出现时自上而下展开、关闭时收拢，下方列表随之滑动；
          此前是条件渲染，整块瞬间插入把列表顶下去。关闭时内容冻结，文案不会先清空再收拢。 */}
      <Collapse open={!!(notice || error)} lazy>
        {() => (
          <div className={`gp-notice${error ? " is-error" : noticeTone === "warn" ? " is-warn" : ""}`} role={error ? "alert" : "status"}>
            <span className="gp-notice-text">{error ?? notice}</span>
            <button type="button" className="gp-notice-x" aria-label="关闭提示" onClick={() => { setNotice(null); setError(null); setNoticeTone("info"); }}>
              <IconX size={11} />
            </button>
          </div>
        )}
      </Collapse>

      {view === "history" && historyView}

      {view === "changes" && !status && !error && <div className="gp-loading">正在读取仓库状态…</div>}
      {view === "changes" && !status && error && (
        <div className="gp-empty-wrap">
          <Empty size="sm" mood="think" title="无法使用 Git" desc={error} />
        </div>
      )}

      {view === "changes" && status && (
        <div className="gp-body">
          <Collapse open={merging} lazy>
            {() => (
              <div className="gp-merge-banner">
                <IconAlertTriangle size={14} />
                <span className="gp-merge-text">
                  {conflictCount > 0 ? `合并进行中，${conflictCount} 个文件有冲突` : "合并进行中，冲突已全部解决，可提交完成合并"}
                </span>
                <Button variant="ghost" size="sm" disabled={busy != null}
                  onClick={() => void run("abort", async () => { await api.gitMergeAbort(projectId!, { repo }); return "已中止合并"; })}>
                  中止合并
                </Button>
              </div>
            )}
          </Collapse>

          <Collapse open={conflictCount > 0} lazy>
            {() => (
              <>
                <div className="gp-conflict-help">冲突文件需先选择保留哪一边，处理完再一起提交。</div>
                <div className="gp-conflict-actions">
                  {status.conflicts.map((c) => (
                    <div key={`cf-${c.path}`} className="gp-conflict-row">
                      <Tooltip title={c.path} side="top">
                        <span className="gp-conflict-name">{c.path}</span>
                      </Tooltip>
                      <Button variant="ghost" size="sm" disabled={busy != null}
                        onClick={() => void run("resolve", async () => { await api.gitResolve(projectId!, c.path, "ours", { repo }); })}>采用我方</Button>
                      <Button variant="ghost" size="sm" disabled={busy != null}
                        onClick={() => void run("resolve", async () => { await api.gitResolve(projectId!, c.path, "theirs", { repo }); })}>采用对方</Button>
                    </div>
                  ))}
                </div>
              </>
            )}
          </Collapse>

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
          void run("branch", async () => (await api.gitBranch(projectId!, "create", name, {}, { repo })).output);
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
