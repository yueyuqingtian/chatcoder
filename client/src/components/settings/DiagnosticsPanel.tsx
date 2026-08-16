/** 设置中心：诊断（v2.2 对齐 zcode 3.18）。系统健康检查。 */
import { useState } from "react";
import { api } from "../../api/client";
import { IconRefresh } from "../icons";

export function DiagnosticsPanel() {
  const [result, setResult] = useState<Array<{ name: string; ok: boolean; detail?: string }> | null>(null);
  const [running, setRunning] = useState(false);
  const run = async () => {
    setRunning(true); setResult(null);
    try {
      const d = await api.runDiagnostics();
      setResult(d.checks);
    } catch (e) { alert(String(e)); }
    finally { setRunning(false); }
  };
  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-primary btn-sm" onClick={run} disabled={running}><IconRefresh size={13} /> {running ? "运行中…" : "运行诊断"}</button>
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
    </div>
  );
}
