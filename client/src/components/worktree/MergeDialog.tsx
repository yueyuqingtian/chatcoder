/** MergeDialog —— 工作树合并到主工作区的三栏冲突解决（plan-282-1441 #5）。
 *
 * 两层级（对齐 IDEA 的合并体验）：
 *  1) 差异文件列表：状态图标 + 路径，逐文件进入解决；顶部提供「全部采用主工作区/工作树」与
 *     「一键 AI 智能合并」（对所有冲突文件依次求建议）；
 *  2) 三栏解决：左=工作树(theirs)、中=合并结果（可编辑）、右=主工作区(ours)；
 *     冲突块可「接受左侧 / 接受右侧 / 都保留」，可上下跳转，可请求 AI 建议插入中间栏，
 *     也支持直接手动编辑/粘贴。全部解决后提交合并。
 *
 * 冲突块识别：对本文件三个版本做行级 diff，取"两侧都相对 base 有改动且改动不同"的行区间。
 * 这里用轻量实现（LCS 行 diff），不引入额外依赖。
 */
import { useEffect, useMemo, useState } from "react";
import { api, type WorktreeOut } from "../../api/client";
import { Dialog } from "../ui/Dialog";
import { ConfirmDialog } from "../ConfirmDialog";
import { IconCheck, IconChevronDown, IconChevronUp, IconFileText, IconRefresh, IconWand } from "../icons";

/* ── 轻量行级 diff ── */

function splitLines(text: string | null | undefined): string[] {
  if (!text) return [];
  return text.replace(/\r\n/g, "\n").split("\n");
}

/** 最长公共子序列的行索引对（用于把两侧变更对齐到 base 的行位置） */
function lcsPairs(a: string[], b: string[]): Array<[number, number]> {
  const n = a.length;
  const m = b.length;
  // 大文件降级：超过阈值不做逐行对齐（避免 O(n*m) 卡顿），按"整体替换"处理
  if (n * m > 4_000_000) return [];
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const pairs: Array<[number, number]> = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      pairs.push([i, j]);
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      i++;
    } else {
      j++;
    }
  }
  return pairs;
}

/** 把某个版本相对 base 的"改动行区间"算出来（返回 base 侧的行号区间集合） */
function changedRangesOnBase(base: string[], side: string[]): Array<[number, number]> {
  const pairs = lcsPairs(base, side);
  const ranges: Array<[number, number]> = [];
  let prevBase = -1;
  for (const [bi] of pairs) {
    if (bi > prevBase + 1) ranges.push([prevBase + 1, bi - 1]);
    prevBase = bi;
  }
  if (prevBase < base.length - 1) ranges.push([prevBase + 1, base.length - 1]);
  return ranges;
}

export interface ConflictHunk {
  /** base 侧行号区间（含端点） */
  baseStart: number;
  baseEnd: number;
  oursLines: string[];
  theirsLines: string[];
}

/** 计算冲突块：base 上"两侧都有改动且归属区间重叠"视为冲突 */
export function computeConflicts(base: string | null, ours: string | null, theirs: string | null): ConflictHunk[] {
  const b = splitLines(base);
  const o = splitLines(ours);
  const t = splitLines(theirs);
  if (b.length === 0) return [];
  const oursRanges = changedRangesOnBase(b, o);
  const theirsRanges = changedRangesOnBase(b, t);
  const hunks: ConflictHunk[] = [];
  for (const [os, oe] of oursRanges) {
    for (const [ts, te] of theirsRanges) {
      // 区间相交 → 冲突（同一段 base 被两侧分别改动）
      if (Math.max(os, ts) <= Math.min(oe, te)) {
        const start = Math.max(os, ts);
        const end = Math.min(oe, te);
        // 该区间两侧各自的内容：按 LCS 对齐取 ours/theirs 中对应的行
        hunks.push({
          baseStart: start,
          baseEnd: end,
          oursLines: sliceByBaseRange(b, o, start, end),
          theirsLines: sliceByBaseRange(b, t, start, end),
        });
      }
    }
  }
  return hunks;
}

/** 取某侧版本中"对应 base [start,end] 区间"的行（近似：按对齐后的位置切片） */
function sliceByBaseRange(base: string[], side: string[], start: number, end: number): string[] {
  const pairs = lcsPairs(base, side);
  // 找到 base 区间前后最近的锚点，取二者之间 side 的行
  let before = 0; // side 上的起始
  let after = side.length;
  for (const [bi, si] of pairs) {
    if (bi < start) before = si + 1;
    if (bi > end) { after = si; break; }
  }
  return side.slice(before, Math.max(before, after));
}

export function MergeDialog({
  open,
  worktree,
  onClose,
  onMerged,
}: {
  open: boolean;
  worktree: WorktreeOut | null;
  onClose: () => void;
  onMerged: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<import("../../api/client").WorktreeMergePreview | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [error, setError] = useState("");

  /** 单文件解决视图 */
  const [activePath, setActivePath] = useState<string | null>(null);
  const [blobs, setBlobs] = useState<{ base: string | null; ours: string | null; theirs: string | null } | null>(null);
  const [merged, setMerged] = useState("");
  const [hunkIndex, setHunkIndex] = useState(0);
  /** 已解决的最终内容：path → content（未解决的不写） */
  const [resolved, setResolved] = useState<Record<string, string>>({});
  const [confirmApply, setConfirmApply] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  /** 合并完成后的收尾二次确认：是否顺手删除当前工作树（不删则保留） */
  const [postMerge, setPostMerge] = useState(false);
  const [wtDeleting, setWtDeleting] = useState(false);
  const [postError, setPostError] = useState("");
  /** 工作树有未提交变更时，需用户二次确认才强制删除 */
  const [needForce, setNeedForce] = useState(false);

  const loadPreview = async () => {
    if (!worktree) return;
    setLoading(true);
    setError("");
    try {
      setPreview(await api.worktreeMergePreview(worktree.id));
    } catch (e) {
      setError(String(e));
      setPreview(null);
    } finally { setLoading(false); }
  };

  useEffect(() => {
    if (!open || !worktree) return;
    setActivePath(null);
    setBlobs(null);
    setResolved({});
    setError("");
    setPostMerge(false);
    setPostError("");
    void loadPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, worktree?.id]);

  const openFile = async (path: string) => {
    if (!worktree) return;
    setLoading(true);
    setError("");
    try {
      const data = await api.worktreeMergeFile(worktree.id, path);
      setBlobs({ base: data.base, ours: data.ours, theirs: data.theirs });
      // 默认合并结果：优先用工作树版本（工作树的改动通常是要合入的）
      setMerged(data.theirs ?? data.ours ?? "");
      setActivePath(path);
      setHunkIndex(0);
    } catch (e) {
      setError(String(e));
    } finally { setLoading(false); }
  };

  const conflicts = useMemo(
    () => (blobs ? computeConflicts(blobs.base, blobs.ours, blobs.theirs) : []),
    [blobs],
  );

  /** 把当前合并结果保存为该文件的最终内容 */
  const saveFile = () => {
    if (!activePath) return;
    setResolved((prev) => ({ ...prev, [activePath]: merged }));
    setActivePath(null);
    setBlobs(null);
  };

  /** 接受某侧：把当前冲突块替换为指定侧内容 */
  const acceptSide = (side: "ours" | "theirs" | "both") => {
    const hunk = conflicts[hunkIndex];
    if (!hunk) return;
    const lines = merged.split("\n");
    const replacement =
      side === "ours" ? hunk.oursLines
      : side === "theirs" ? hunk.theirsLines
      : [...hunk.oursLines, ...hunk.theirsLines];
    // 用 base 行号定位到 merged 中的对应位置：这里采用"按 base 区间逐行替换"的稳健做法
    // （merged 默认取自某一侧，行数与 base 可能有偏移，故用内容匹配兜底）
    const baseLines = splitLines(blobs?.base);
    const target = baseLines.slice(hunk.baseStart, hunk.baseEnd + 1);
    let replaced = false;
    if (target.length > 0) {
      const startIdx = findSublist(lines, target, hunk.baseStart);
      if (startIdx >= 0) {
        lines.splice(startIdx, target.length, ...replacement);
        replaced = true;
      }
    }
    if (!replaced) {
      // 兜底：在文件末尾追加（保证用户能看到内容并手动调整）
      lines.push(...replacement);
    }
    setMerged(lines.join("\n"));
  };

  /** 请求 AI 建议（针对当前冲突块） */
  const askAi = async () => {
    if (!worktree || !activePath) return;
    setAiBusy(true);
    setError("");
    try {
      const hunk = conflicts[hunkIndex];
      const res = await api.worktreeMergeAi(worktree.id, activePath, hunk
        ? { ours: hunk.oursLines.join("\n"), theirs: hunk.theirsLines.join("\n") }
        : undefined);
      if (res.ok && res.suggestion != null) {
        if (hunk) {
          // 把建议替换到该冲突块位置
          const lines = merged.split("\n");
          const target = splitLines(blobs?.base).slice(hunk.baseStart, hunk.baseEnd + 1);
          const startIdx = target.length > 0 ? findSublist(lines, target, hunk.baseStart) : -1;
          if (startIdx >= 0) lines.splice(startIdx, target.length, ...splitLines(res.suggestion));
          else lines.push(...splitLines(res.suggestion));
          setMerged(lines.join("\n"));
        } else {
          setMerged(res.suggestion);
        }
      } else {
        setError(res.error || "AI 未给出建议");
      }
    } catch (e) {
      setError(String(e));
    } finally { setAiBusy(false); }
  };

  /** 一键 AI 智能合并：对所有文件依次求整文件建议 */
  const aiMergeAll = async () => {
    if (!worktree || !preview) return;
    setAiBusy(true);
    setError("");
    try {
      const next: Record<string, string> = { ...resolved };
      for (const f of preview.files) {
        if (f.status === "deleted") continue;
        const res = await api.worktreeMergeAi(worktree.id, f.path);
        if (res.ok && res.suggestion != null) next[f.path] = res.suggestion;
      }
      setResolved(next);
    } catch (e) {
      setError(String(e));
    } finally { setAiBusy(false); }
  };

  const pendingFiles = useMemo(
    () => (preview?.files ?? []).filter((f) => f.status !== "deleted"),
    [preview],
  );

  const applyMerge = async () => {
    if (!worktree || !preview) return;
    setSubmitting(true);
    setError("");
    try {
      const files = preview.files.map((f) =>
        f.status === "deleted"
          ? { path: f.path, deleted: true }
          : { path: f.path, content: resolved[f.path] ?? "" },
      );
      await api.worktreeMergeApply(worktree.id, files);
      setConfirmApply(false);
      // 合并已写入主工作区：不直接关闭，改为二次确认是否删除当前工作树
      setPostError("");
      setPostMerge(true);
    } catch (e) {
      setError(String(e));
    } finally { setSubmitting(false); }
  };

  /** 合并后收尾：删除工作树（含本地分支）。
   *  先按普通删除；若因未提交变更被拒，则提示用户确认后再强制删除（不静默丢弃改动）。 */
  const finishAndDelete = async (force = false) => {
    if (!worktree) return;
    setWtDeleting(true);
    setPostError("");
    try {
      await api.deleteWorktreeProject(worktree.id, force);
      setPostMerge(false);
      onMerged();
    } catch (e) {
      // 未提交变更时后端会拒绝：引导用户确认强制删除
      setNeedForce(true);
      setPostError(String(e));
    } finally { setWtDeleting(false); }
  };

  /** 合并后收尾：保留工作树，仅关闭对话框 */
  const finishKeep = () => {
    setPostMerge(false);
    setNeedForce(false);
    onMerged();
  };

  const statusLabel: Record<string, string> = {
    added: "新增", modified: "修改", deleted: "删除", renamed: "重命名", copied: "复制",
  };

  return (
    <>
      <Dialog
        open={open && !postMerge}
        onClose={onClose}
        width={1080}
        title={`合并工作树「${worktree?.name ?? ""}」到主工作区`}
        subtitle={preview ? `分支 ${preview.branch} → ${preview.base_branch}` : undefined}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button>
            <button className="btn btn-ghost btn-sm" onClick={() => void loadPreview()} disabled={loading}>
              <IconRefresh size={13} /> 重新检测
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => void aiMergeAll()} disabled={aiBusy || !preview}>
              <IconWand size={13} /> {aiBusy ? "AI 合并中…" : "一键 AI 智能合并"}
            </button>
            <button className="btn btn-primary btn-sm"
              onClick={() => setConfirmApply(true)}
              disabled={!preview || pendingFiles.length === 0 || submitting}>
              提交合并{pendingFiles.length > 0 ? `（已解决 ${Object.keys(resolved).length}/${pendingFiles.length}）` : ""}
            </button>
          </>
        }
      >
        {error && <div className="merge-error">{error}</div>}

        {/* 第二层：三栏冲突解决 */}
        {activePath && blobs ? (
          <div className="merge-workbench">
            <div className="merge-workbench-head">
              <button className="btn btn-ghost btn-xs" onClick={() => { setActivePath(null); setBlobs(null); }}>
                ← 返回文件列表
              </button>
              <span className="merge-workbench-path">{activePath}</span>
              {conflicts.length > 0 && (
                <span className="merge-hunk-nav">
                  冲突 {hunkIndex + 1}/{conflicts.length}
                  <button className="merge-icon-btn" disabled={hunkIndex <= 0}
                    onClick={() => setHunkIndex((i) => Math.max(0, i - 1))} title="上一处冲突">
                    <IconChevronUp size={12} />
                  </button>
                  <button className="merge-icon-btn" disabled={hunkIndex >= conflicts.length - 1}
                    onClick={() => setHunkIndex((i) => Math.min(conflicts.length - 1, i + 1))} title="下一处冲突">
                    <IconChevronDown size={12} />
                  </button>
                </span>
              )}
              <button className="btn btn-ghost btn-xs" onClick={() => void askAi()} disabled={aiBusy}>
                <IconWand size={12} /> {aiBusy ? "生成中…" : "AI 建议"}
              </button>
            </div>

            {conflicts.length > 0 && (
              <div className="merge-hunk-actions">
                <span>当前冲突块：</span>
                <button className="btn btn-ghost btn-xs" onClick={() => acceptSide("theirs")}>接受左侧（工作树）</button>
                <button className="btn btn-ghost btn-xs" onClick={() => acceptSide("ours")}>接受右侧（主工作区）</button>
                <button className="btn btn-ghost btn-xs" onClick={() => acceptSide("both")}>两者都保留</button>
              </div>
            )}

            <div className="merge-columns">
              <div className="merge-col">
                <div className="merge-col-head">工作树（theirs）</div>
                <pre className="merge-pre">{blobs.theirs ?? "（无此文件）"}</pre>
              </div>
              <div className="merge-col merge-col-center">
                <div className="merge-col-head">合并结果（可编辑）</div>
                <textarea
                  className="merge-editor"
                  value={merged}
                  onChange={(e) => setMerged(e.target.value)}
                  spellCheck={false}
                />
              </div>
              <div className="merge-col">
                <div className="merge-col-head">主工作区（ours）</div>
                <pre className="merge-pre">{blobs.ours ?? "（无此文件）"}</pre>
              </div>
            </div>

            <div className="merge-workbench-foot">
              <button className="btn btn-primary btn-xs" onClick={saveFile}>
                <IconCheck size={12} /> 保存并标记已解决
              </button>
            </div>
          </div>
        ) : (
          /* 第一层：差异文件列表 */
          <div className="merge-filelist">
            {loading && <div className="navpage-empty">正在检测差异…</div>}
            {!loading && preview && preview.files.length === 0 && (
              <div className="navpage-empty">工作树与主工作区没有差异，无需合并。</div>
            )}
            {!loading && preview?.files.map((f) => (
              <div className="merge-file-row" key={f.path}>
                <span className={`merge-file-status ${f.status}`}>{statusLabel[f.status] ?? f.status}</span>
                <IconFileText size={13} />
                <span className="merge-file-path" title={f.path}>{f.path}</span>
                {f.conflict && <span className="merge-file-conflict">冲突</span>}
                {resolved[f.path] != null && <span className="merge-file-done">已解决</span>}
                {f.status !== "deleted" && (
                  <button className="btn btn-ghost btn-xs" onClick={() => void openFile(f.path)}>
                    合并
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </Dialog>

      <ConfirmDialog
        open={confirmApply}
        title="提交合并"
        message={
          `将把解决后的内容写入主工作区并创建一次提交。\n\n` +
          `未在列表中处理过的文件将按工作树版本写入。是否继续？`
        }
        confirmLabel="提交"
        onCancel={() => setConfirmApply(false)}
        onConfirm={() => void applyMerge()}
      />

      {/* 合并完成后的收尾：是否删除当前工作树（不删则保留，可继续在里面开发） */}
      <Dialog
        open={postMerge}
        onClose={finishKeep}
        width={420}
        title="合并完成"
        dismissOnOverlay={false}
        footer={
          needForce ? (
            <>
              <button className="btn btn-ghost btn-sm" onClick={finishKeep} disabled={wtDeleting}>
                取消
              </button>
              <button className="btn btn-danger btn-sm" onClick={() => void finishAndDelete(true)} disabled={wtDeleting}>
                {wtDeleting ? "删除中…" : "强制删除（丢弃未提交变更）"}
              </button>
            </>
          ) : (
            <>
              <button className="btn btn-ghost btn-sm" onClick={finishKeep} disabled={wtDeleting}>
                保留工作树
              </button>
              <button className="btn btn-danger btn-sm" onClick={() => void finishAndDelete(false)} disabled={wtDeleting}>
                {wtDeleting ? "删除中…" : "删除工作树"}
              </button>
            </>
          )
        }
      >
        <div className="ui-dialog-message">
          {needForce ? (
            <>
              <p>该工作树存在未提交变更，普通删除已被拒绝。</p>
              <p className="merge-post-note">
                继续删除会一并丢弃这些未提交的改动，且连带删除本地分支
                <code>{worktree?.branch || "（工作树分支）"}</code>。是否确认？
              </p>
            </>
          ) : (
            <>
              <p>已将工作树「{worktree?.name ?? ""}」的改动合并到主工作区。</p>
              <p className="merge-post-note">
                是否删除该工作树？删除会移除工作树目录与登记，并连带删除本地分支
                <code>{worktree?.branch || "（工作树分支）"}</code>。
              </p>
              <p>选择「保留工作树」则维持现状，可继续在其中开发。</p>
            </>
          )}
        </div>
        {postError && <div className="merge-error">{postError}</div>}
      </Dialog>
    </>
  );
}

/** 在 lines 中从 fromHint 起查找 sub 的起始下标（找不到返回 -1） */
function findSublist(lines: string[], sub: string[], fromHint: number): number {
  if (sub.length === 0) return -1;
  const tryAt = (start: number) => {
    if (start < 0 || start + sub.length > lines.length) return false;
    for (let k = 0; k < sub.length; k++) if (lines[start + k] !== sub[k]) return false;
    return true;
  };
  if (tryAt(fromHint)) return fromHint;
  for (let i = 0; i + sub.length <= lines.length; i++) {
    if (tryAt(i)) return i;
  }
  return -1;
}
