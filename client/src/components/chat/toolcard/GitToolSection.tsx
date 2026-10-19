/** 工具卡 Git 栏（plan-20261019-toolcard-v2）：「更改 +N −M」「分支 ⌄」「提交或推送」三行。
 *
 * 不再列文件——文件清单在右侧 Git 面板。三行各有去处：
 *  · 更改   → 右侧 Git 面板（变更视图）；行尾是当前所有未提交改动的增删行数；
 *  · 分支   → 弹出分支浮窗：搜索、切换、「创建并检出新分支…」（弹窗输入名称）、「Git 图谱」（弹窗看历史）；
 *  · 提交或推送 → 右侧 Git 面板。
 *
 * 分支操作沿用右侧面板同一套后端（`/git/branch`）与同一套安全约定：
 *  · 切换会覆盖本地改动时，后端返回 `reason=dirty` 与文件清单，这里弹出与面板一致的选择弹窗
 *    （暂存后切换 / 放弃改动后切换 / 取消）——可恢复的情况不当作失败；
 *  · 多仓库项目：浮窗顶部可切换作用的仓库（记在 localStorage，与右侧面板同键）。
 * 操作完成后立即强制刷新 Git 摘要，并广播 `chatcoder:git-changed` 让右侧面板同步。
 */
import { memo, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  IconCheck, IconChevronDown, IconFileDiff, IconGitBranch, IconGitCommit, IconGitGraph, IconPlus, IconSearch,
} from "../../icons";
import { Button, Dialog, FormDialog, Floating, Input, Tooltip } from "../../ui";
import { api, type GitBranchesOut, type GitLogEntry } from "../../../api/client";
import { notify } from "../../../store/toast";
import { Row, ToolCardEdgeContext, useOverflow } from "./ToolCardSections";
import { layoutGraph, type GraphRow } from "./gitGraph";
import { openGitPanel, type GitSectionData, type ToolCardActions } from "./useToolCardData";

/** 分支弹窗里远程分支最多列出的条数（输入搜索词时放宽） */
const REMOTE_LIMIT = 20;
const REMOTE_SEARCH_LIMIT = 50;
/** 分支名合法字符（与后端 branch_action 的校验一致） */
const BRANCH_NAME_RE = /^[A-Za-z0-9._/-]+$/;
/** 图谱分页大小与最大绘制泳道数（再多的泳道挤在一列里也看不清） */
const GRAPH_PAGE = 80;
const GRAPH_MAX_LANES = 8;
const GRAPH_LANE_W = 14;
const GRAPH_ROW_H = 34;

/** 广播：Git 状态被工具卡改动过，右侧 Git 面板据此重新读取。 */
function announceGitChanged(): void {
  try { window.dispatchEvent(new CustomEvent("chatcoder:git-changed")); } catch { /* 非浏览器环境忽略 */ }
}

const errText = (e: unknown): string => (e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));

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
  if (d < 30) return `${d} 天前`;
  const dt = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
}

/* ═══════════════ 主体：三行 + 弹层 ═══════════════ */

export const GitToolSection = memo(function GitToolSection({ data, actions }: {
  data: GitSectionData;
  actions: ToolCardActions;
}) {
  const edgeRef = useContext(ToolCardEdgeContext);
  const branchRowRef = useRef<HTMLButtonElement>(null);
  const branchTextRef = useRef<HTMLSpanElement>(null);
  const [branchOpen, setBranchOpen] = useState(false);
  const [newBranchOpen, setNewBranchOpen] = useState(false);
  const [graphOpen, setGraphOpen] = useState(false);
  const [dirty, setDirty] = useState<{ name: string; files: string[] } | null>(null);

  const { repo } = data;
  const branchLabel = repo.detached ? `游离 ${repo.oid ?? ""}`.trim() : (repo.branch ?? "—");
  const branchOver = useOverflow(branchTextRef, branchLabel);
  const clean = data.files === 0;

  return (
    <>
      <Row
        icon={<IconFileDiff size={16} />}
        text="更改"
        tip={clean ? "没有未提交的改动" : `${data.files} 个文件有未提交的改动 · 点击在 Git 面板查看`}
        trailing={clean ? (
          <span className="tcard-row-hint">无更改</span>
        ) : data.hasLineStats ? (
          <span className="tcard-diffstat" aria-label={`新增 ${data.additions} 行，删除 ${data.deletions} 行`}>
            <span className="is-add">+{data.additions}</span>
            <span className="is-del">-{data.deletions}</span>
          </span>
        ) : (
          // 后端版本较旧、没有行数统计：退而显示文件数
          <span className="tcard-row-hint">{data.files} 个文件</span>
        )}
        onClick={openGitPanel}
      />

      {/* 分支行：文字之后紧跟折叠箭头（参考图），多仓库时行尾标出当前仓库名 */}
      <Tooltip content={branchLabel} disabled={!branchOver || branchOpen} side="left">
        <button
          ref={branchRowRef}
          type="button"
          className={"tcard-row tcard-row-btn tcard-branch-row" + (branchOpen ? " is-open" : "")}
          aria-haspopup="listbox"
          aria-expanded={branchOpen}
          aria-label={`分支 ${branchLabel}，点击切换`}
          onClick={() => setBranchOpen((v) => !v)}
        >
          <span className="tcard-ic"><IconGitBranch size={16} /></span>
          <span ref={branchTextRef} className="tcard-row-text tcard-row-fit tcard-row-strong">{branchLabel}</span>
          <IconChevronDown size={13} className={"tcard-branch-caret" + (branchOpen ? " is-open" : "")} />
          <span className="tcard-row-fill" />
          {data.repos.length > 1 && <span className="tcard-row-hint">{repo.name}</span>}
        </button>
      </Tooltip>

      <Row
        icon={<IconGitCommit size={16} />}
        text="提交或推送"
        tip={repo.ahead > 0 ? `有 ${repo.ahead} 个提交待推送 · 点击在 Git 面板提交或推送` : "在 Git 面板提交或推送"}
        trailing={(repo.ahead > 0 || repo.behind > 0) ? (
          <span className="tcard-row-hint">
            {repo.ahead > 0 ? `↑${repo.ahead}` : ""}{repo.ahead > 0 && repo.behind > 0 ? " " : ""}{repo.behind > 0 ? `↓${repo.behind}` : ""}
          </span>
        ) : undefined}
        onClick={openGitPanel}
      />

      <BranchPopup
        open={branchOpen}
        anchorRef={branchRowRef}
        edgeRef={edgeRef}
        data={data}
        actions={actions}
        onClose={() => setBranchOpen(false)}
        onNewBranch={() => { setBranchOpen(false); setNewBranchOpen(true); }}
        onGraph={() => { setBranchOpen(false); setGraphOpen(true); }}
        onDirty={(info) => { setBranchOpen(false); setDirty(info); }}
      />
      <NewBranchDialog
        open={newBranchOpen}
        data={data}
        actions={actions}
        onClose={() => setNewBranchOpen(false)}
      />
      <DirtySwitchDialog
        info={dirty}
        data={data}
        actions={actions}
        onClose={() => setDirty(null)}
      />
      <GitGraphDialog open={graphOpen} data={data} onClose={() => setGraphOpen(false)} />
    </>
  );
});

/* ═══════════════ 分支浮窗 ═══════════════ */

interface BranchItem { name: string; current: boolean; remote: boolean }

function BranchPopup({ open, anchorRef, edgeRef, data, actions, onClose, onNewBranch, onGraph, onDirty }: {
  open: boolean;
  anchorRef: React.RefObject<HTMLElement | null>;
  edgeRef?: React.RefObject<HTMLElement | null>;
  data: GitSectionData;
  actions: ToolCardActions;
  onClose: () => void;
  onNewBranch: () => void;
  onGraph: () => void;
  onDirty: (info: { name: string; files: string[] }) => void;
}) {
  return (
    <Floating
      open={open}
      anchorRef={anchorRef}
      edgeRef={edgeRef}
      side="left"
      align="start"
      gap={10}
      onClose={onClose}
      className="tcard-branch-pop"
      role="dialog"
      aria-label="切换分支"
    >
      {/* 浮窗每次打开才挂载内容：搜索词、错误提示随关闭重置 */}
      <BranchPopupBody
        data={data}
        actions={actions}
        onClose={onClose}
        onNewBranch={onNewBranch}
        onGraph={onGraph}
        onDirty={onDirty}
      />
    </Floating>
  );
}

function BranchPopupBody({ data, actions, onClose, onNewBranch, onGraph, onDirty }: {
  data: GitSectionData;
  actions: ToolCardActions;
  onClose: () => void;
  onNewBranch: () => void;
  onGraph: () => void;
  onDirty: (info: { name: string; files: string[] }) => void;
}) {
  const { projectId, repo } = data;
  const [query, setQuery] = useState("");
  const [branches, setBranches] = useState<GitBranchesOut | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  // 打开 / 切换仓库时读取分支（始终取最新：别处可能刚建过分支）
  useEffect(() => {
    let cancelled = false;
    setBranches(null);
    setLoadError(null);
    api.gitBranches(projectId, { repo: repo.rel })
      .then((b) => { if (!cancelled) setBranches(b); })
      .catch((e) => { if (!cancelled) setLoadError(errText(e)); });
    return () => { cancelled = true; };
  }, [projectId, repo.rel]);

  const q = query.trim().toLowerCase();
  const { locals, remotes } = useMemo(() => {
    const hit = (name: string) => !q || name.toLowerCase().includes(q);
    const localList: BranchItem[] = (branches?.local ?? [])
      .filter((b) => hit(b.name))
      .map((b) => ({ name: b.name, current: b.current, remote: false }));
    const localNames = new Set((branches?.local ?? []).map((b) => b.name));
    // 已有同名本地分支的远程分支不再重复列出（切换会落到本地分支上）
    const remoteList: BranchItem[] = (branches?.remote ?? [])
      .filter((b) => !localNames.has(b.name.split("/").slice(1).join("/")) && hit(b.name))
      .slice(0, q ? REMOTE_SEARCH_LIMIT : REMOTE_LIMIT)
      .map((b) => ({ name: b.name, current: false, remote: true }));
    return { locals: localList, remotes: remoteList };
  }, [branches, q]);
  const flat = useMemo(() => [...locals, ...remotes], [locals, remotes]);

  // 搜索词变化：高亮回到第一项可切换的分支
  useEffect(() => { setActive(Math.max(0, flat.findIndex((b) => !b.current))); }, [q]); // eslint-disable-line react-hooks/exhaustive-deps
  // 键盘移动高亮时保证它在可视区内
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const checkout = useCallback(async (name: string, onDirtyChoice?: "stash" | "discard") => {
    setBusy(name);
    setError(null);
    try {
      const res = await api.gitBranch(projectId, "checkout", name, { onDirty: onDirtyChoice }, { repo: repo.rel });
      if (res.switched === false && res.reason === "dirty") {
        onDirty({ name, files: res.files ?? [] });
        return;
      }
      actions.refreshGit(true);
      announceGitChanged();
      onClose();
      if (res.conflict) {
        notify.warning(`已切换到 ${name}，但暂存的改动恢复时产生 ${res.conflicts?.length ?? 0} 个冲突，请在 Git 面板的「冲突」区处理`);
        openGitPanel();
      } else {
        notify.success(`已切换到 ${name}`);
      }
    } catch (e) {
      setError(`切换失败：${errText(e)}`);
    } finally {
      setBusy(null);
    }
  }, [projectId, repo.rel, actions, onClose, onDirty]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (flat.length === 0) return;
      setActive((i) => (e.key === "ArrowDown" ? (i + 1) % flat.length : (i - 1 + flat.length) % flat.length));
    } else if (e.key === "Enter") {
      const it = flat[active];
      if (it && !it.current && busy == null) { e.preventDefault(); void checkout(it.name); }
    }
  };

  const renderItem = (b: BranchItem, idx: number) => (
    <button
      key={`${b.remote ? "r" : "l"}:${b.name}`}
      type="button"
      role="option"
      aria-selected={b.current}
      data-idx={idx}
      className={"tcard-br-item" + (b.current ? " is-current" : "") + (idx === active ? " is-active" : "")}
      disabled={b.current || busy != null}
      onMouseMove={() => setActive(idx)}
      onClick={() => void checkout(b.name)}
    >
      <span className="tcard-br-ic"><IconGitBranch size={16} /></span>
      <span className="tcard-br-main">
        <span className="tcard-br-name">{b.name}</span>
        {/* 当前分支带一行说明：未提交改动的文件数（参考图） */}
        {b.current && (
          <span className="tcard-br-sub">
            {data.files > 0 ? `未提交的更改：${data.files} 个文件` : "没有未提交的更改"}
          </span>
        )}
      </span>
      {b.current && <span className="tcard-br-check"><IconCheck size={14} /></span>}
      {busy === b.name && <span className="tcard-br-busy">切换中…</span>}
    </button>
  );

  return (
    <div className="tcard-br" onKeyDown={onKeyDown}>
      {/* 多仓库：先选仓库，列表与操作都作用于它 */}
      {data.repos.length > 1 && (
        <div className="tcard-br-repos" role="tablist" aria-label="选择仓库">
          {data.repos.map((r) => (
            <button
              key={r.rel || "root"}
              type="button"
              role="tab"
              aria-selected={r.rel === repo.rel}
              className={"tcard-br-repo" + (r.rel === repo.rel ? " is-on" : "")}
              onClick={() => { setQuery(""); setError(null); actions.setGitRepo(r.rel); }}
            >
              {r.name}
            </button>
          ))}
        </div>
      )}

      <div className="tcard-br-search">
        <Input
          size="sm"
          value={query}
          prefix={<IconSearch size={14} />}
          placeholder="搜索分支"
          aria-label="搜索分支"
          autoFocus
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      <div className="tcard-br-list" ref={listRef} role="listbox" aria-label="分支">
        {loadError ? (
          <div className="tcard-br-note is-error">{loadError}</div>
        ) : !branches ? (
          <div className="tcard-br-note">正在读取分支…</div>
        ) : flat.length === 0 ? (
          <div className="tcard-br-note">没有匹配「{query.trim()}」的分支</div>
        ) : (
          <>
            <div className="tcard-br-cap">分支</div>
            {locals.map((b, i) => renderItem(b, i))}
            {remotes.length > 0 && <div className="tcard-br-cap">远程分支</div>}
            {remotes.map((b, i) => renderItem(b, locals.length + i))}
          </>
        )}
      </div>

      {error && <div className="tcard-br-note is-error" role="alert">{error}</div>}

      <div className="tcard-br-foot">
        <button type="button" className="tcard-br-act" onClick={onNewBranch}>
          <span className="tcard-br-ic"><IconPlus size={16} /></span>
          <span>创建并检出新分支…</span>
        </button>
        <button type="button" className="tcard-br-act" onClick={onGraph}>
          <span className="tcard-br-ic"><IconGitGraph size={16} /></span>
          <span>Git 图谱</span>
        </button>
      </div>
    </div>
  );
}

/* ═══════════════ 创建并检出新分支 ═══════════════ */

function NewBranchDialog({ open, data, actions, onClose }: {
  open: boolean;
  data: GitSectionData;
  actions: ToolCardActions;
  onClose: () => void;
}) {
  const { projectId, repo } = data;
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 每次打开都从空白开始
  useEffect(() => {
    if (open) { setName(""); setError(null); setBusy(false); }
  }, [open]);

  const trimmed = name.trim();
  const format = !trimmed ? null
    : !BRANCH_NAME_RE.test(trimmed) ? "分支名只能包含字母、数字、. _ - /"
      : (trimmed.startsWith("-") || trimmed.startsWith("/") || trimmed.endsWith("/") || trimmed.includes("..")) ? "分支名格式不正确"
        : null;
  const exists = trimmed !== "" && repo.branch === trimmed;
  const valid = trimmed !== "" && format == null && !exists;

  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.gitBranch(projectId, "create", trimmed, {}, { repo: repo.rel });
      actions.refreshGit(true);
      announceGitChanged();
      notify.success(`已创建并切换到 ${trimmed}`);
      onClose();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const base = repo.detached ? "当前提交" : `当前分支「${repo.branch ?? "—"}」`;
  return (
    <FormDialog
      open={open}
      onClose={onClose}
      title="创建并检出新分支"
      subtitle={`基于${base}创建，并立即切换到新分支`}
      submitLabel={busy ? "创建中…" : "创建并检出"}
      submitDisabled={!valid || busy}
      onSubmit={() => void submit()}
      width={440}
    >
      <div className="tcard-nb">
        <Input
          value={name}
          placeholder="如：feat/login"
          aria-label="新分支名称"
          invalid={format != null || exists}
          autoFocus
          onChange={(e) => { setName(e.target.value); setError(null); }}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void submit(); } }}
        />
        {(format || exists) && <p className="tcard-nb-hint is-error">{format ?? "已经在这个分支上了"}</p>}
        {error && <p className="tcard-nb-hint is-error" role="alert">{error}</p>}
        {!format && !exists && !error && data.files > 0 && (
          <p className="tcard-nb-hint">当前有 {data.files} 个文件未提交，它们会一起带到新分支上。</p>
        )}
      </div>
    </FormDialog>
  );
}

/* ═══════════════ 切换被本地改动阻止 ═══════════════ */

function DirtySwitchDialog({ info, data, actions, onClose }: {
  info: { name: string; files: string[] } | null;
  data: GitSectionData;
  actions: ToolCardActions;
  onClose: () => void;
}) {
  const { projectId, repo } = data;
  const [busy, setBusy] = useState<"stash" | "discard" | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (info) { setBusy(null); setError(null); } }, [info]);

  const run = async (choice: "stash" | "discard") => {
    if (!info) return;
    setBusy(choice);
    setError(null);
    try {
      const res = await api.gitBranch(projectId, "checkout", info.name, { onDirty: choice }, { repo: repo.rel });
      actions.refreshGit(true);
      announceGitChanged();
      onClose();
      if (res.conflict) {
        notify.warning(`已切换到 ${info.name}，但暂存的改动恢复时产生 ${res.conflicts?.length ?? 0} 个冲突，请在 Git 面板的「冲突」区处理`);
        openGitPanel();
      } else {
        notify.success(`已切换到 ${info.name}`);
      }
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open={info != null}
      onClose={onClose}
      title="本地改动阻止切换分支"
      subtitle={info ? `切换到「${info.name}」前，需要先处理下列未提交的改动` : undefined}
      width={460}
      footer={(
        <>
          <Button variant="secondary" onClick={onClose}>取消</Button>
          <Button variant="danger-ghost" disabled={busy != null} onClick={() => void run("discard")}>放弃改动并切换</Button>
          <Button variant="primary" loading={busy === "stash"} disabled={busy != null} onClick={() => void run("stash")}>暂存改动并切换</Button>
        </>
      )}
    >
      <div className="gp-dirty">
        <p className="gp-dirty-hint">
          「暂存改动并切换」会在切换后自动恢复这些改动；「放弃改动」会永久丢弃它们，无法恢复。
        </p>
        <ul className="gp-dirty-list">
          {(info?.files ?? []).slice(0, 8).map((f) => <li key={f}><code>{f}</code></li>)}
        </ul>
        {(info?.files.length ?? 0) > 8 && <p className="gp-dirty-more">还有 {info!.files.length - 8} 个文件未列出</p>}
        {error && <p className="tcard-nb-hint is-error" role="alert">{error}</p>}
      </div>
    </Dialog>
  );
}

/* ═══════════════ Git 图谱 ═══════════════ */

/** 一行的图谱：主干 / 旁支 / 合并线 + 提交节点（SVG，泳道布局见 gitGraph.ts）。 */
const GraphCell = memo(function GraphCell({ row, head }: { row: GraphRow; head: boolean }) {
  const lanes = Math.min(Math.max(row.width, 1), GRAPH_MAX_LANES);
  const w = lanes * GRAPH_LANE_W + 6;
  const h = GRAPH_ROW_H;
  const mid = h / 2;
  const cx = (l: number) => 3 + GRAPH_LANE_W / 2 + Math.min(l, GRAPH_MAX_LANES - 1) * GRAPH_LANE_W;
  const color = (l: number) => `var(--gg-${l % 6})`;
  const curve = (x1: number, y1: number, x2: number, y2: number) =>
    `M${x1} ${y1}C${x1} ${(y1 + y2) / 2} ${x2} ${(y1 + y2) / 2} ${x2} ${y2}`;
  const lines: React.ReactNode[] = [];
  row.top.forEach((sha, l) => {
    if (sha == null || l >= GRAPH_MAX_LANES) return;
    if (l === row.lane || row.merges.includes(l)) return;
    if (row.bottom[l] === sha) lines.push(<line key={`p${l}`} x1={cx(l)} y1={0} x2={cx(l)} y2={h} stroke={color(l)} />);
  });
  row.merges.forEach((l) => {
    if (l < GRAPH_MAX_LANES) lines.push(<path key={`m${l}`} d={curve(cx(l), 0, cx(row.lane), mid)} stroke={color(l)} />);
  });
  if (row.top[row.lane] != null) lines.push(<line key="in" x1={cx(row.lane)} y1={0} x2={cx(row.lane)} y2={mid} stroke={color(row.lane)} />);
  row.links.forEach((t) => {
    if (t === row.lane) lines.push(<line key="out" x1={cx(t)} y1={mid} x2={cx(t)} y2={h} stroke={color(t)} />);
    else if (t < GRAPH_MAX_LANES) lines.push(<path key={`b${t}`} d={curve(cx(row.lane), mid, cx(t), h)} stroke={color(t)} />);
  });
  return (
    <svg className="tcard-gg" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      <g fill="none" strokeWidth={2} strokeLinecap="round">{lines}</g>
      <circle
        className={"tcard-gg-dot" + (head ? " is-head" : "")}
        cx={cx(row.lane)}
        cy={mid}
        r={4.5}
        stroke={color(row.lane)}
        strokeWidth={2}
      />
    </svg>
  );
});

function GitGraphDialog({ open, data, onClose }: { open: boolean; data: GitSectionData; onClose: () => void }) {
  const { projectId, repo } = data;
  const [commits, setCommits] = useState<GitLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [more, setMore] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async (skip: number) => {
    const mine = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const page = await api.gitLog(projectId, GRAPH_PAGE, skip, { repo: repo.rel });
      if (mine !== seqRef.current) return;
      setCommits((prev) => (skip === 0 ? page : [...prev, ...page]));
      setMore(page.length === GRAPH_PAGE);
    } catch (e) {
      if (mine === seqRef.current) setError(errText(e));
    } finally {
      if (mine === seqRef.current) setLoading(false);
    }
  }, [projectId, repo.rel]);

  // 每次打开都重新读取第一页；关闭后作废在途请求
  useEffect(() => {
    if (!open) { seqRef.current += 1; return; }
    setCommits([]);
    setMore(true);
    void load(0);
  }, [open, load]);

  const layout = useMemo(
    () => layoutGraph(commits.map((c) => ({ sha: c.sha, parents: c.parents }))),
    [commits],
  );

  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (more && !loading && el.scrollTop + el.clientHeight > el.scrollHeight - 240) void load(commits.length);
  };

  const copy = (c: GitLogEntry) => {
    void navigator.clipboard?.writeText(c.sha).then(
      () => notify.success(`已复制提交号 ${c.short}`),
      () => notify.warning("复制失败：剪贴板不可用"),
    );
  };

  const branchLabel = repo.detached ? `游离 ${repo.oid ?? ""}`.trim() : (repo.branch ?? "—");
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Git 图谱"
      subtitle={`${data.repos.length > 1 ? `${repo.name} · ` : ""}${branchLabel} 的全部历史提交`}
      width={780}
      bodyClassName="tcard-gg-body"
    >
      <div className="tcard-gg-list" onScroll={onScroll}>
        {commits.length === 0 && !loading && !error && <div className="tcard-gg-note">还没有提交记录</div>}
        {layout.rows.map((row, i) => {
          const c = commits[i];
          if (!c) return null;
          const refs = c.refs.filter((r) => r.kind !== "head");
          return (
            <Tooltip key={c.sha} side="top" content={`${c.subject}\n${c.author} · ${relTime(c.date)} · ${c.short}（点击复制提交号）`}>
              <button type="button" className="tcard-gg-row" onClick={() => copy(c)}>
                <GraphCell row={row} head={c.refs.some((r) => r.kind === "head")} />
                <span className="tcard-gg-main">
                  {refs.slice(0, 3).map((r) => (
                    <span key={`${r.kind}-${r.name}`} className={`gp-ref is-${r.kind}`}>{r.name}</span>
                  ))}
                  {refs.length > 3 && <span className="gp-ref">+{refs.length - 3}</span>}
                  <span className="tcard-gg-subject">{c.subject}</span>
                </span>
                <span className="tcard-gg-who">{c.author}</span>
                <span className="tcard-gg-when">{relTime(c.date)}</span>
                <span className="tcard-gg-sha">{c.short}</span>
              </button>
            </Tooltip>
          );
        })}
        {loading && <div className="tcard-gg-note">读取历史…</div>}
        {error && <div className="tcard-gg-note is-error" role="alert">{error}</div>}
        {more && !loading && commits.length > 0 && (
          <div className="tcard-gg-more">
            <Button variant="ghost" size="sm" onClick={() => void load(commits.length)}>加载更多</Button>
          </div>
        )}
        {!more && commits.length > 0 && <div className="tcard-gg-note">已显示全部 {commits.length} 个提交</div>}
      </div>
    </Dialog>
  );
}
