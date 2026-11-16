/** plan-20261109-rollback-grouped-fix：回滚前按保留文件与撤销改动分组，明确对话影响。 */
import { useState } from "react";
import { useChatStore } from "../../store/chat";
import { Button, Dialog } from "../ui";
import { RollbackFileGroups } from "./RollbackFileGroups";
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
        <pre className="rc-diff-msg">{file.reason || "无法校验文件状态，将保留当前内容"}</pre>
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
  const [confirming, setConfirming] = useState(false);

  const files = pending?.files ?? [];
  const kept = files.filter((file) => file.conflict);
  const safeCount = files.length - kept.length;
  const affected = pending?.affected ?? { tasks: 0, messages: 0 };
  const hasConflict = kept.length > 0;
  const run = async () => {
    setConfirming(true);
    try {
      await confirmRollback({ allowPartial: hasConflict });
    } finally {
      setConfirming(false);
    }
  };
  const impact = [
    affected.messages > 0 ? `撤回本轮及之后的 ${affected.messages} 条消息` : "撤回本轮及之后的对话",
    safeCount > 0 ? `撤销 ${safeCount} 个文件的改动` : "文件保持不变",
    affected.tasks > 0 ? `取消 ${affected.tasks} 个任务` : null,
  ].filter(Boolean).join("，");

  return (
    <Dialog open={pending != null} onClose={confirming ? () => {} : cancelRollback}
      title="回滚确认" width={640}
      footer={<>
        <Button variant="secondary" disabled={confirming} onClick={cancelRollback}>取消</Button>
        <Button variant="danger" loading={confirming} onClick={() => void run()}>
          {safeCount > 0 ? "确认回滚" : "仅撤回消息"}
        </Button>
      </>}
    >
      <p className="rb-impact">{impact}。</p>
      {hasConflict && safeCount > 0 && <p className="rb-kept-note">{kept.length} 个文件将保留当前内容。</p>}
      <RollbackFileGroups key={pending?.turnId ?? "closed"} files={files} renderDetail={(file) => <FileDiff file={file} />} />
    </Dialog>
  );
}
