/** 回滚确认弹窗（v9）：
 * 点击回滚（高风险操作）先展示本次将撤销的文件清单 + 回滚前后内容对比，
 * 用户审核确认后才执行；存在冲突（用户手动改动与 AI 改动重叠）的文件明确标注跳过。
 */
import { useState } from "react";
import { useChatStore } from "../../store/chat";
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
  // 保护：超 200 行或 100k 单元格时退化为逐行对比
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

  if (!pending) return null;
  const { turnId, files, affected } = pending;
  const conflictCount = files.filter((f) => f.conflict).length;

  return (
    <div className="rc-overlay" onClick={cancelRollback}>
      <div className="rc-modal" onClick={(e) => e.stopPropagation()}>
        <header className="rc-header">
          <h3>回滚确认</h3>
          <button className="rc-close" onClick={cancelRollback} title="取消">×</button>
        </header>
        <div className="rc-body">
          <p className="rc-desc">
            将回滚 <b>turn #{turnId}</b> 及其后的更改（撤销 AI 的改动，保留你的手动改动）。
            共涉及 <b>{files.length}</b> 个文件：
          </p>
          {(affected.tasks > 0 || affected.messages > 0) && (
            <p className="rc-conflict-note" style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
              <IconAlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
              <span>
                将连带撤销该 turn 之后的执行结果：
                {affected.tasks > 0 && <>取消 <b>{affected.tasks}</b> 个任务</>}
                {affected.tasks > 0 && affected.messages > 0 && "、"}
                {affected.messages > 0 && <>软删 <b>{affected.messages}</b> 条消息</>}
                。
              </span>
            </p>
          )}
          {conflictCount > 0 && (
            <p className="rc-conflict-note" style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
              <IconAlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
              <span>{conflictCount} 个文件与你的手动改动存在重叠，回滚将<b>跳过</b>它们（不会覆盖你的改动）。</span>
            </p>
          )}
          <div className="rc-file-list">
            {files.length === 0 && <div className="rc-empty">该 turn 无文件写入记录，仅回滚消息。</div>}
            {files.map((f) => (
              <div key={f.path} className={`rc-file${f.conflict ? " conflict" : ""}`}>
                <div className="rc-file-head" onClick={() => setExpanded(expanded === f.path ? null : f.path)}>
                  <span className={`rc-file-action ${f.action}`}>
                    {f.action === "delete" ? "删除" : "恢复"}
                  </span>
                  <span className="rc-file-path">{f.path}</span>
                  {f.conflict ? <span className="rc-file-flag conflict">冲突跳过</span> : <span className="rc-file-flag">可安全回滚</span>}
                  <span className="rc-file-toggle">{expanded === f.path ? "▲" : "▼"}</span>
                </div>
                {expanded === f.path && <FileDiff file={f} />}
              </div>
            ))}
          </div>
        </div>
        <footer className="rc-footer">
          <button className="btn ghost" onClick={cancelRollback}>取消</button>
          <button
            className="btn danger"
            disabled={confirming}
            onClick={async () => {
              setConfirming(true);
              await confirmRollback();
              setConfirming(false);
            }}
          >
            {confirming ? "回滚中…" : "确认回滚"}
          </button>
        </footer>
      </div>
    </div>
  );
}
