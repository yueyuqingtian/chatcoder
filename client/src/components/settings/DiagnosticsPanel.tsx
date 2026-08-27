/** 设置中心：诊断（v2.2 对齐 zcode 3.18）。系统健康检查 + checkpoint 占用与清理。 */
import { useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconWrench } from "../icons";

interface CheckpointStat { workspace: string; file_count: number; size_mb: number; orphan_count: number }

export function DiagnosticsPanel() {
  const [result, setResult] = useState<Array<{ name: string; ok: boolean; detail?: string }> | null>(null);
  const [checkpoints, setCheckpoints] = useState<CheckpointStat[] | null>(null);
  const [running, setRunning] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const run = async () => {
    setRunning(true); setResult(null);
    try {
      const d = await api.runDiagnostics();
      setResult(d.checks);
      setCheckpoints(Array.isArray(d.checkpoints) ? d.checkpoints : null);
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setRunning(false); }
  };
  const clean = async () => {
    setCleaning(true);
    try {
      const r = await api.cleanupCheckpoints();
      const deleted = (r.results as Array<{ deleted?: number }>).reduce((a, b) => a + (b.deleted ?? 0), 0);
      useChatStore.setState({ error: `已清理 ${deleted} 个 checkpoint 文件` });
      await run();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setCleaning(false); }
  };
  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-primary btn-sm" onClick={run} disabled={running}><IconRefresh size={13} /> {running ? "运行中…" : "运行诊断"}</button>
        {checkpoints != null && checkpoints.length > 0 && (
          <button className="btn btn-ghost btn-sm" onClick={clean} disabled={cleaning}><IconWrench size={13} /> {cleaning ? "清理中…" : "清理 checkpoint"}</button>
        )}
      </div>
      {result == null && !running && <div className="navpage-empty">点击上方按钮运行系统健康检查</div>}
      {result != null && (
        <div className="settings-resource-list">
          {result.map((c) => (
            <div key={c.name} className="settings-resource-item">
              <div className="settings-resource-info">
                <div className="settings-resource-name">{c.name}</div>
                {c.detail && <div className="settings-resource-desc"><span>{c.detail}</span></div>}
              </div>
              <span className="settings-resource-tag" style={{ color: c.ok ? "var(--success)" : "var(--error)", borderColor: c.ok ? "var(--success)" : "var(--error)" }}>
                {c.ok ? "正常" : "异常"}
              </span>
            </div>
          ))}
        </div>
      )}
      {checkpoints != null && checkpoints.length > 0 && (
        <>
          <div className="sb-section-label" style={{ margin: "14px 0 6px" }}>checkpoint 存储（.chatcoder/checkpoints）</div>
          <div className="settings-resource-list">
            {checkpoints.map((c) => (
              <div key={c.workspace} className="settings-resource-item">
                <div className="settings-resource-info">
                  <div className="settings-resource-name">{c.workspace}</div>
                  <div className="settings-resource-desc">{c.file_count} 个文件 · {c.size_mb} MB · 孤儿 {c.orphan_count} 个</div>
                </div>
                <button className="btn btn-ghost btn-xs" disabled={cleaning}
                  onClick={async () => { setCleaning(true); try { await api.cleanupCheckpoints(c.workspace); await run(); } catch (e) { useChatStore.setState({ error: String(e) }); } finally { setCleaning(false); } }}>
                  <IconWrench size={12} /> 清理
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
