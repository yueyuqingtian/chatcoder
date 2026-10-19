/** 回滚确认弹窗（plan-20261017-goal12 项 3 / 项 11）：
 *
 * 点击回滚（高风险操作）先展示本次将撤销的文件清单 + 回滚前后内容对比，用户审核后才执行。
 * 与手动改动重叠的文件默认让整次回滚拒绝执行（不改任何文件）；用户可选择「跳过冲突文件，回滚其余」，
 * 此时冲突文件保持现状，确认按钮文案明确写出跳过的数量。
 * 外壳统一为 ui/Dialog（与全站弹窗同一退场与配色）。
 */
import { useState } from "react";
import { useChatStore } from "../../store/chat";
import { Button, Collapse, Dialog, DialogSection, InsetCard } from "../ui";
import { IconAlertTriangle } from "../icons";
import type { RollbackPreviewFile } from "../../api/client";

interface DiffRow { type: "same" | "del" | "add"; line: string }

/** 行级 LCS diff（大文件降级为等长行对比，避免 O(n*m) 内存/耗时爆炸）。 */
function diffLines(a: string, b: string): DiffRow[] {
  const al = a.split("\n");
  const bl = b.split("\n");
  const n = al.length;
  const m = bl.length;
  if (n === 0 && m === 0) return [];
  // 保护：行数乘积超过 10 万单元格（DP 表过大）时退化为逐行对比
  if (n * m > 100_000) {
    const out: DiffRow[] = [];
    const len = Math.max(n, m);
    for (let i = 0; i < len; i++) {
      const x = i < n ? al[i] : "";
      const y = i < m ? bl[i] : "";
      if (x === y) out.push({ type: "same", line: x });
      else {
        if (x !== "") out.push({ type: "del", line: x });
        if (y !== "") out.push({ type: "add", line: y });
      }
    }
    return out;
  }
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = al[i] === bl[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (al[i] === bl[j]) {
      out.push({ type: "same", line: al[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: "del", line: al[i] });
      i++;
    } else {
      out.push({ type: "add", line: bl[j] });
      j++;
    }
  }
  while (i < n) { out.push({ type: "del", line: al[i] }); i++; }
  while (j < m) { out.push({ type: "add", line: bl[j] }); j++; }
  return out;
}

function FileDiff({ file }: { file: RollbackPreviewFile }) {
  const rows = diffLines(file.before || "", file.after || "");
  return (
    <div className="rc-diff">
      {file.conflict ? (
        <pre className="rc-diff-msg">{file.reason || "与手动改动存在重叠，回滚将跳过该文件"}</pre>
      ) : (
        <div className="rc-diff-body">
          {rows.map((r, idx) => (
            <div key={idx} className={`rc-line ${r.type}`}>
              <span className="rc-line-sign">{r.type === "del" ? "-" : r.type === "add" ? "+" : " "}</span>
              <span className="rc-line-text">{r.line || " "}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function RollbackConfirmModal() {
  const pending = useChatStore((s) => s.rollbackPending);
  const confirmRollback = useChatStore((s) => s.confirmRollback);
  const cancelRollback = useChatStore((s) => s.cancelRollback);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const files = pending?.files ?? [];
  const conflictCount = pending?.conflicts.length ?? files.filter((f) => f.conflict).length;
  const safeCount = files.filter((f) => !f.conflict).length;
  const hasConflict = conflictCount > 0;
  const turnId = pending?.turnId ?? 0;
  const affected = pending?.affected ?? { tasks: 0, messages: 0 };

  const run = async (allowPartial: boolean) => {
    setConfirming(true);
    try {
      await confirmRollback({ allowPartial });
    } finally {
      setConfirming(false);
    }
  };

  const confirmLabel = files.length === 0
    ? "确认撤回"
    : hasConflict
      ? `跳过 ${conflictCount} 个冲突文件并回滚`
      : "确认回滚";

  return (
    <Dialog
      open={pending != null}
      onClose={cancelRollback}
      title="回滚确认"
      subtitle="撤销 AI 的改动，保留你的手动改动"
      width={680}
      footer={
        <>
          <Button variant="secondary" onClick={cancelRollback}>取消</Button>
          {hasConflict ? (
            <Button variant="danger" loading={confirming} disabled={safeCount === 0 && affected.messages === 0 && affected.tasks === 0} onClick={() => void run(true)}>
              {confirmLabel}
            </Button>
          ) : (
            <Button variant="danger" loading={confirming} onClick={() => void run(false)}>
              {confirmLabel}
            </Button>
          )}
        </>
      }
    >
      <DialogSection
        title={files.length === 0 ? `turn #${turnId}` : `涉及 ${files.length} 个文件`}
        aside={files.length > 0 ? `${safeCount} 可安全回滚 · ${conflictCount} 冲突` : undefined}
      >
        <p className="rc-desc">
          {files.length === 0
            ? <>将撤回 <b>turn #{turnId}</b> 及其后的对话，<b>本次不修改任何代码文件</b>。</>
            : <>将回滚 <b>turn #{turnId}</b> 及其后的更改，按时间倒序逐个撤销。</>}
        </p>
        {(affected.tasks > 0 || affected.messages > 0) && (
          <p className="rc-conflict-note">
            <IconAlertTriangle size={13} />
            <span>
              将连带撤销该 turn 之后的执行结果：
              {affected.tasks > 0 && <>取消 <b>{affected.tasks}</b> 个任务</>}
              {affected.tasks > 0 && affected.messages > 0 && "、"}
              {affected.messages > 0 && <>软删 <b>{affected.messages}</b> 条消息</>}
              。
            </span>
          </p>
        )}
        {hasConflict && (
          <p className="rc-conflict-note">
            <IconAlertTriangle size={13} />
            <span>
              有 <b>{conflictCount}</b> 个文件与你的手动改动重叠。默认整体拒绝回滚；
              选择下方按钮可跳过它们、回滚其余文件，被跳过的文件保持现状。
            </span>
          </p>
        )}
      </DialogSection>

      {files.length > 0 && (
        <DialogSection title="文件清单" aside="点击展开查看改动">
          <InsetCard>
            <div className="rc-file-list">
              {files.map((f) => (
                <div key={f.path} className={`rc-file${f.conflict ? " conflict" : ""}`}>
                  <button
                    type="button"
                    className="rc-file-head"
                    aria-expanded={expanded === f.path}
                    onClick={() => setExpanded(expanded === f.path ? null : f.path)}
                  >
                    <span className={`rc-file-action ${f.action}`}>{f.action === "delete" ? "删除" : "恢复"}</span>
                    <span className="rc-file-path">{f.path}</span>
                    <span className={`rc-file-flag${f.conflict ? " conflict" : ""}`}>{f.conflict ? "冲突跳过" : "可安全回滚"}</span>
                  </button>
                  {/* plan-20261019-motion-sweep：对比展开走 Collapse（lazy 保留「展开才计算 LCS」的开销特性） */}
                  <Collapse open={expanded === f.path} lazy>{() => <FileDiff file={f} />}</Collapse>
                </div>
              ))}
            </div>
          </InsetCard>
        </DialogSection>
      )}
    </Dialog>
  );
}
