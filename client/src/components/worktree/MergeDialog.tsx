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
import { api, type ModelOut, type WorktreeMergeDirection, type WorktreeOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { Dialog } from "../ui/Dialog";
import { ConfirmDialog } from "../ConfirmDialog";
import { IconCheck, IconChevronDown, IconChevronUp, IconFileText, IconRefresh, IconWand } from "../icons";

/* ── plan-308-1542 需求3-A：git diff3 冲突标记解析 ──
 *
 * 冲突内容现由 git 产出（`git merge-file --diff3` / 临时工作树 `git merge`），
 * 格式为：
 *   <<<<<<< 主工作区(ours)
 *   ...ours...
 *   ||||||| base
 *   ...base...
 *   =======
 *   ...theirs...
 *   >>>>>>> 工作树(theirs)
 * 解析真实标记比自研 LCS 猜冲突更准（与 git/IDEA 判定一致）。 */

const MARK_OURS = /^<{7}/;
const MARK_BASE = /^\|{7}/;
const MARK_SEP = /^={7}$/;
const MARK_THEIRS = /^>{7}/;

/** 解析 diff3 冲突块；无标记返回空数组（调用方回退到 LCS 启发式）。 */
export function parseConflictMarkers(text: string): Array<{
  startLine: number; endLine: number; oursLines: string[]; baseLines: string[]; theirsLines: string[];
}> {
  const lines = (text ?? "").split("\n");
  const out: Array<{ startLine: number; endLine: number; oursLines: string[]; baseLines: string[]; theirsLines: string[] }> = [];
  let i = 0;
  while (i < lines.length) {
    if (MARK_OURS.test(lines[i])) {
      const start = i;
      const ours: string[] = [];
      const base: string[] = [];
      const theirs: string[] = [];
      let stage: "ours" | "base" | "theirs" = "ours";
      i++;
      for (; i < lines.length; i++) {
        const ln = lines[i];
        if (MARK_BASE.test(ln) && stage === "ours") { stage = "base"; continue; }
        if (MARK_SEP.test(ln) && stage !== "theirs") { stage = "theirs"; continue; }
        if (MARK_THEIRS.test(ln)) { break; }
        (stage === "ours" ? ours : stage === "base" ? base : theirs).push(ln);
      }
      out.push({ startLine: start, endLine: i, oursLines: ours, baseLines: base, theirsLines: theirs });
    }
    i++;
  }
  return out;
}

/** 内容是否还含未解决冲突标记（提交前校验，与后端 merge_apply 双重防护）。 */
export function hasConflictMarkers(text: string): boolean {
  return /^(<{7}|\|{7}|={7}|>{7})/m.test(text ?? "");
}

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
  direction = "to_main",
  onClose,
  onMerged,
}: {
  open: boolean;
  worktree: WorktreeOut | null;
  /** 合并方向：to_main=工作树→主工作区（默认）；from_main=主工作区→工作树 */
  direction?: WorktreeMergeDirection;
  onClose: () => void;
  onMerged: () => void;
}) {
  const toMain = direction === "to_main";
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
  /** 来源侧有未提交改动时的"先提交"确认 */
  const [confirmCommit, setConfirmCommit] = useState(false);
  const [committing, setCommitting] = useState(false);
  /** AI 合并使用的模型（可自选供应商的模型）；null=服务端默认 */
  const [modelId, setModelId] = useState<number | null>(null);
  const [models, setModels] = useState<ModelOut[]>([]);

  const loadPreview = async () => {
    if (!worktree) return;
    setLoading(true);
    setError("");
    try {
      const pv = await api.worktreeMergePreview(worktree.id, direction);
      setPreview(pv);
      // 可自动合并的文件直接预置为合并结果，用户只需处理冲突文件
      const auto: Record<string, string> = {};
      for (const f of pv.files) {
        if (f.status !== "deleted" && f.has_auto_merge && f.merged != null) auto[f.path] = f.merged;
      }
      setResolved(auto);
      // 来源侧有未提交改动：提示用户先提交（确认后自动提交再继续）
      setConfirmCommit(Boolean(pv.source_dirty));
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
    setConfirmCommit(false);
    void loadPreview();
    // 模型列表：供 AI 合并自选供应商模型
    api.listModels().then(setModels).catch(() => setModels([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, worktree?.id, direction]);

  /** 用户确认后自动提交来源侧的未提交改动，再继续合并 */
  const doCommitSource = async () => {
    if (!worktree) return;
    setCommitting(true);
    setError("");
    try {
      await api.worktreeCommit(worktree.id, toMain ? "worktree" : "main");
      setConfirmCommit(false);
      await loadPreview();
    } catch (e) {
      setError(String(e));
    } finally { setCommitting(false); }
  };

  const openFile = async (path: string) => {
    if (!worktree) return;
    setLoading(true);
    setError("");
    try {
      const data = await api.worktreeMergeFile(worktree.id, path, direction);
      setBlobs({ base: data.base, ours: data.ours, theirs: data.theirs });
      // 默认合并结果：优先用后端自动三方合并的内容（含冲突标记），否则用来源侧版本
      const auto = preview?.files.find((f) => f.path === path)?.merged;
      setMerged(auto ?? data.theirs ?? data.ours ?? "");
      setActivePath(path);
      setHunkIndex(0);
    } catch (e) {
      setError(String(e));
    } finally { setLoading(false); }
  };

  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const mergeProgress = useChatStore((s) => s.mergeProgress);
  const resetMergeProgress = useChatStore((s) => s.resetMergeProgress);

  const conflicts = useMemo(() => {
    if (!blobs) return [];
    // plan-308-1542 需求3-A：优先解析 **git 产出的 diff3 标记**（与 git/IDEA 判定一致），
    // 仅当内容里没有标记时才回退到 LCS 启发式（兼容手动编辑过的结果）。
    const hunks = parseConflictMarkers(merged);
    if (hunks.length > 0) {
      const all = merged.split("\n");
      return hunks.map((h) => ({
        baseStart: h.startLine,
        baseEnd: h.endLine,
        oursLines: h.oursLines,
        theirsLines: h.theirsLines,
        _markerRange: [h.startLine, h.endLine] as [number, number],
        _allLines: all,
      }));
    }
    return computeConflicts(blobs.base, blobs.ours, blobs.theirs);
  }, [blobs, merged]);

  /** 把当前合并结果保存为该文件的最终内容 */
  const saveFile = () => {
    if (!activePath) return;
    setResolved((prev) => ({ ...prev, [activePath]: merged }));
    setActivePath(null);
    setBlobs(null);
  };

  /** 接受某侧：把当前冲突块替换为指定侧内容。
   *
   * plan-308-1542 需求3-B：内容为 git diff3 标记时，直接按**标记行区间**定位替换
   * （比按 base 行号猜测猫确——merged 行号与 base 本就不同）。
   */
  const acceptSide = (side: "ours" | "theirs" | "both", at = hunkIndex) => {
    const hunk: any = conflicts[at];
    if (!hunk) return;
    const replacement =
      side === "ours" ? hunk.oursLines
      : side === "theirs" ? hunk.theirsLines
      : [...hunk.oursLines, ...hunk.theirsLines];

    let next = merged;
    if (hunk._markerRange && hunk._allLines) {
      // git 标记路径：按标记所在行区间整体替换
      const [s, e] = hunk._markerRange as [number, number];
      const lines = [...hunk._allLines] as string[];
      lines.splice(s, e - s + 1, ...replacement);
      next = lines.join("\n");
    } else {
      const lines = merged.split("\n");
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
      if (!replaced) lines.push(...replacement);
      next = lines.join("\n");
    }
    setMerged(next);
    // 解决完当前块自动跳下一处（没有下一处则停在最后）
    setHunkIndex((i) => Math.min(i + 1, Math.max(0, conflicts.length - 1)));
  };

  /** 全部采用某一侧（当前文件全部冲突块）。 */
  const acceptAllSide = (side: "ours" | "theirs") => {
    if (!blobs) return;
    // 从最后一个冲突块往前替换，避免行号偏移
    let next = merged;
    const hunks = [...conflicts];
    for (let k = hunks.length - 1; k >= 0; k--) {
      const h: any = hunks[k];
      const replacement = side === "ours" ? h.oursLines : h.theirsLines;
      if (h._markerRange && h._allLines) {
        const [s, e] = h._markerRange as [number, number];
        const lines = next.split("\n");
        lines.splice(s, e - s + 1, ...replacement);
        next = lines.join("\n");
      }
    }
    setMerged(next);
  };

  const [aiNotice, setAiNotice] = useState("");

  /** 请求 AI 建议（针对当前冲突块）。
   *
   * plan-308-1542 修复（用户反馈"ai 建议点击了看不到"）：
   * 之前有两种"看不到"：① 未打开文件时直接 return，界面无任何反应；
   * ② 成功把建议写进中间栏后也无任何提示，用户不知道发生了什么。
   * 现在：未打开文件→提示如何操作；成功→绿色提示条（含模型名）。 */
  const askAi = async () => {
    if (!worktree) return;
    if (!activePath) {
      setError("请先在下方文件列表中点击「合并」打开一个文件，再请求 AI 建议");
      return;
    }
    setAiBusy(true);
    setError("");
    setAiNotice("");
    try {
      const hunk = conflicts[hunkIndex];
      const res = await api.worktreeMergeAi(worktree.id, activePath,
        hunk ? { ours: hunk.oursLines.join("\n"), theirs: hunk.theirsLines.join("\n") } : undefined,
        { direction, modelId });
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
        // 可见反馈：告诉用户建议已插入到哪里（否则"点了没反应"）
        setAiNotice(`AI 建议已插入${hunk ? `第 ${hunkIndex + 1} 处冲突块` : "整个文件"}`
          + `${res.model ? `（模型 ${res.model}）` : ""}，确认无误后点「保存并标记已解决」`);
      } else {
        setError(res.error || "AI 未给出建议");
      }
    } catch (e) {
      setError(String(e));
    } finally { setAiBusy(false); }
  };

  /** plan-308-1542 需求3-A：一键 AI 智能合并（后端边执行边广播 merge.progress）。
   *
   * 取代原来"前端 for 循环逐个调 AI"——那样用户看不到任何过程。
   * 现在后端一次调用完成：git 判定 → 冲突文件走模型 → 广播进度 → 返回汇总报告。
   */
  const aiMergeAll = async () => {
    if (!worktree || !preview) return;
    setAiBusy(true);
    setError("");
    resetMergeProgress();
    try {
      const res = await api.worktreeMergeAiAll(worktree.id, {
        direction, modelId, sessionId: currentSessionId,
      });
      // 后端返回的 resolved 是权威结果（含 git 自动合入 + AI 解决）
      if (res.resolved) setResolved({ ...resolved, ...res.resolved });
      if (res.preview) setPreview(res.preview);
      const rep = res.report;
      if (rep && rep.failed > 0) {
        const failedPaths = rep.files.filter((f) => f.result === "failed").map((f) => f.path);
        setError(`以下文件 AI 未给出建议，请手动处理：\n${failedPaths.join("\n")}`);
      }
    } catch (e) {
      setError(String(e));
    } finally { setAiBusy(false); }
  };

  const pendingFiles = useMemo(
    () => (preview?.files ?? []).filter((f) => f.status !== "deleted"),
    [preview],
  );

  /** plan-308-1542 需求3-B：当前冲突块在"合并结果"里的行区间（用于三栏高亮）。 */
  const currentHunkRange = useMemo<[number, number] | null>(() => {
    const h: any = conflicts[hunkIndex];
    if (!h) return null;
    if (h._markerRange) return h._markerRange as [number, number];
    return [h.baseStart, h.baseEnd];
  }, [conflicts, hunkIndex]);

  /** 全部非删除文件是否都已有最终结果且不含未解决冲突标记。 */
  const allResolved = useMemo(() => {
    if (!preview) return false;
    const files = preview.files.filter((f) => f.status !== "deleted");
    if (files.length === 0) return false;
    return files.every((f) => {
      const content = resolved[f.path] ?? f.merged ?? "";
      return !hasConflictMarkers(content);
    });
  }, [preview, resolved]);

  /** plan-308-1542 需求3-B：键盘快捷键跳转冲突点。
   *  F7 / Alt+↓ 下一处；Shift+F7 / Alt+↑ 上一处（与 IDEA 习惯对齐）。 */
  useEffect(() => {
    if (!open || !activePath) return;
    const onKey = (e: KeyboardEvent) => {
      const next = (e.key === "F7" && !e.shiftKey) || (e.altKey && e.key === "ArrowDown");
      const prev = (e.key === "F7" && e.shiftKey) || (e.altKey && e.key === "ArrowUp");
      if (!next && !prev) return;
      e.preventDefault();
      setHunkIndex((i) => (next
        ? Math.min(i + 1, Math.max(0, conflicts.length - 1))
        : Math.max(0, i - 1)));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, activePath, conflicts.length]);

  const applyMerge = async () => {
    if (!worktree || !preview) return;
    setSubmitting(true);
    setError("");
    try {
      const files = preview.files.map((f) =>
        f.status === "deleted"
          ? { path: f.path, deleted: true }
          : { path: f.path, content: resolved[f.path] ?? f.merged ?? "" },
      );
      await api.worktreeMergeApply(worktree.id, files, direction);
      setConfirmApply(false);
      if (toMain) {
        // 合并已写入主工作区：不直接关闭，改为二次确认是否删除当前工作树
        setPostError("");
        setPostMerge(true);
      } else {
        // 反向（主工作区 → 工作树）：无需删除工作树，直接收尾
        onMerged();
      }
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
  const srcName = toMain ? `工作树「${worktree?.name ?? ""}」` : `主工作区`;
  const dstName = toMain ? `主工作区` : `工作树「${worktree?.name ?? ""}」`;

  return (
    <>
      <Dialog
        open={open && !postMerge && !confirmCommit}
        onClose={onClose}
        width={1080}
        title={`合并${srcName}的改动到${dstName}`}
        subtitle={preview
          ? `分支 ${toMain ? preview.branch : preview.base_branch} → ${toMain ? preview.base_branch : preview.branch}`
          : undefined}
        actions={models.length > 0 ? (
          <select
            className="merge-model-select"
            title="AI 智能合并使用的模型（可自选供应商）"
            value={modelId ?? ""}
            onChange={(e) => setModelId(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">AI 模型：服务端默认</option>
            {models.filter((m) => m.is_active).map((m) => (
              <option key={m.id} value={m.id}>
                {m.provider_name ? `${m.provider_name}/${m.name}` : m.name}
              </option>
            ))}
          </select>
        ) : undefined}
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
        {error && <div className="merge-error merge-error-block">{error}</div>}
        {/* plan-308-1542 修复：AI 建议成功后的可见反馈（此前点了没有任何提示） */}
        {aiNotice && <div className="merge-ai-notice">{aiNotice}</div>}

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
                <button className="btn btn-ghost btn-xs" onClick={() => acceptSide("theirs")}>接受左侧（{toMain ? "工作树" : "主工作区"}）</button>
                <button className="btn btn-ghost btn-xs" onClick={() => acceptSide("ours")}>接受右侧（{toMain ? "主工作区" : "工作树"}）</button>
                <button className="btn btn-ghost btn-xs" onClick={() => acceptSide("both")}>两者都保留</button>
                <span className="merge-hunk-sep" />
                {/* plan-308-1542 需求3-B：全部采用（当前文件全部冲突块） */}
                <button className="btn btn-ghost btn-xs" onClick={() => acceptAllSide("theirs")}>全部采用左侧</button>
                <button className="btn btn-ghost btn-xs" onClick={() => acceptAllSide("ours")}>全部采用右侧</button>
                {!hasConflictMarkers(merged) && (
                  <span className="merge-file-resolved-tag">本文件冲突已全部解决</span>
                )}
              </div>
            )}

            {/* plan-308-1542 需求3-B：三栏带行号 + 冲突行高亮（与 git 标记行区间对齐） */}
            <div className="merge-columns merge-columns-numbered">
              <div className="merge-col">
                <div className="merge-col-head">{toMain ? "工作树（来源）" : "主工作区（来源）"}</div>
                <NumberedPre text={blobs.theirs} highlight={currentHunkRange} />
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
                <div className="merge-col-head">{toMain ? "主工作区（目标）" : "工作树（目标）"}</div>
                <NumberedPre text={blobs.ours} highlight={currentHunkRange} />
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
              <div className="navpage-empty">
                {srcName}与{dstName}没有差异，无需合并。
              </div>
            )}
            {!loading && preview && preview.files.length > 0 && (
              <div className="merge-filelist-hint">
                {preview.engine === "fallback" && (
                  <span className="merge-engine-warn">（当前 git 版本不支持内存合并，已降级为兼容模式）</span>
                )}
                共 {preview.files.length} 个文件，
                {preview.has_conflict
                  ? `其中 ${preview.files.filter((f) => f.conflict).length} 个存在冲突（需处理或 AI 合并）`
                  : "全部可自动合并"}
                。已自动合并的文件可直接提交，冲突文件请逐个处理。
              </div>
            )}
            {/* plan-308-1542 需求3-A/3-B：全部冲突已解决提示 */}
            {!loading && allResolved && pendingFiles.length > 0 && (
              <div className="merge-all-resolved">
                <IconCheck size={13} /> 全部冲突已解决，可提交合并
              </div>
            )}
            {!loading && preview?.files.map((f) => (
              <div className="merge-file-row" key={f.path}>
                <span className={`merge-file-status ${f.status}`}>{statusLabel[f.status] ?? f.status}</span>
                <IconFileText size={13} />
                <span className="merge-file-path" title={f.path}>{f.path}</span>
                {f.binary ? <span className="merge-file-binary" title={f.reason || "二进制文件"}>二进制</span>
                  : f.conflict ? <span className="merge-file-conflict">冲突</span>
                    : <span className="merge-file-auto" title={f.change_side === "theirs" ? "仅来源侧改动，已由 git 自动采用" : ""}>可自动合并</span>}
                {resolved[f.path] != null && <span className="merge-file-done">已解决</span>}
                {f.status !== "deleted" && f.needs_manual && (
                  <span className="merge-file-manual">需人工选侧</span>
                )}
                {f.status !== "deleted" && (
                  <button className="btn btn-ghost btn-xs" onClick={() => void openFile(f.path)}>
                    合并
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {/* plan-308-1542 需求3-A：AI 自动合并进度面板（像消息流一样实时追加）
            + 完成后的汇总报告卡。仅在有一轮进度时显示。 */}
        {mergeProgress && (mergeProgress.lines.length > 0 || mergeProgress.report) && (
          <div className="merge-progress">
            <div className="merge-progress-head">
              <span>AI 合并进度</span>
              {mergeProgress.running && <span className="merge-progress-running">进行中…</span>}
            </div>
            <div className="merge-progress-lines">
              {mergeProgress.lines.map((ln, i) => (
                <div className={`merge-progress-line${ln.ok === false ? " is-bad" : ln.ok ? " is-ok" : ""}`} key={i}>
                  <span className="merge-progress-time">
                    {new Date(ln.at).toLocaleTimeString()}
                  </span>
                  <span className="merge-progress-text">{ln.text}</span>
                </div>
              ))}
            </div>
            {mergeProgress.report && (
              <div className="merge-report">
                <div className="merge-report-title">合并结果报告</div>
                <div className="merge-report-stats">
                  <span>共 {mergeProgress.report.total} 个文件</span>
                  <span className="is-ok">git 自动合入 {mergeProgress.report.git}</span>
                  <span className="is-ok">AI 解决 {mergeProgress.report.ai}</span>
                  <span className={mergeProgress.report.conflicted ? "is-warn" : ""}>待人工 {mergeProgress.report.conflicted}</span>
                  <span className={mergeProgress.report.failed ? "is-bad" : ""}>失败 {mergeProgress.report.failed}</span>
                  <span>耗时 {(mergeProgress.report.elapsed_ms / 1000).toFixed(1)}s</span>
                </div>
                {mergeProgress.report.files.filter((r) => r.reason).length > 0 && (
                  <div className="merge-report-detail">
                    {mergeProgress.report.files.filter((r) => r.reason).map((r) => (
                      <div className="merge-report-detail-row" key={r.path}>
                        <code>{r.path}</code> — {r.result}：{r.reason}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </Dialog>

      <ConfirmDialog
        open={confirmApply}
        title="提交合并"
        message={
          `将把解决后的内容写入${dstName}并创建一次提交。\n\n` +
          `未在列表中处理过的文件将按${toMain ? "工作树" : "主工作区"}当前内容写入。是否继续？`
        }
        confirmLabel="提交"
        onCancel={() => setConfirmApply(false)}
        onConfirm={() => void applyMerge()}
      />

      {/* 来源侧有未提交改动：提示用户先提交（确认后自动提交再继续合并） */}
      <ConfirmDialog
        open={confirmCommit}
        title="先提交未提交的改动"
        message={
          `${toMain ? "工作树" : "主工作区"}存在未提交的改动。\n\n` +
          `若先提交这些改动，合并会以最新提交为准，历史更清晰。\n` +
          `点击「提交并继续」将自动提交它们；点击「跳过」则直接合并当前工作区内容。`
        }
        confirmLabel={committing ? "提交中…" : "提交并继续"}
        cancelLabel="跳过"
        onCancel={() => setConfirmCommit(false)}
        onConfirm={() => void doCommitSource()}
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

/** 带行号 + 冲突区间高亮的只读代码栏（plan-308-1542 需求3-B）。
 *  行号让"冲突在哪一行"与 git/IDEA 的口径一致；高亮当前冲突块，便于逐点解决。 */
function NumberedPre({ text, highlight }: { text: string | null; highlight: [number, number] | null }) {
  const lines = (text ?? "").split("\n");
  if (text == null) return <pre className="merge-pre">{`（无此文件）`}</pre>;
  const [hs, he] = highlight ?? [-1, -1];
  return (
    <div className="merge-pre-numbered">
      {lines.map((ln, i) => (
        <div
          className={`merge-codeline${i >= hs && i <= he ? " is-conflict" : ""}`}
          key={i}
        >
          <span className="merge-lineno">{i + 1}</span>
          <span className="merge-codetext">{ln}</span>
        </div>
      ))}
    </div>
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
