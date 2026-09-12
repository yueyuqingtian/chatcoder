/** 设置中心：诊断（v2.2 对齐 zcode 3.18）。系统健康检查 + checkpoint 占用与清理。
 * plan-230-1144 M3: 新增「代码符号索引」区块——索引状态（文件数/符号数/更新时间）
 * 与手动重建入口，让"AI 探索慢"可自查（索引未建时 symbol_search 首次调用会代建）。 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconWrench } from "../icons";

interface CheckpointStat { workspace: string; file_count: number; size_mb: number; orphan_count: number }
interface SymbolIndexStat { workspace?: string; available?: boolean; files?: number; symbols?: number; last_updated?: number | null; error?: string }

export function DiagnosticsPanel() {
  const [result, setResult] = useState<Array<{ name: string; ok: boolean; detail?: string }> | null>(null);
  const [checkpoints, setCheckpoints] = useState<CheckpointStat[] | null>(null);
  const [running, setRunning] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  // plan-230-1144 M3: 符号索引状态
  const [symIdx, setSymIdx] = useState<SymbolIndexStat | null>(null);
  const [rebuilding, setRebuilding] = useState(false);

  const loadSymbolIndex = useCallback(async () => {
    try { setSymIdx(await api.symbolIndexStatus()); }
    catch (e) { setSymIdx({ error: String(e) }); }
  }, []);
  useEffect(() => { void loadSymbolIndex(); }, [loadSymbolIndex]);

  const rebuildIndex = async () => {
    setRebuilding(true);
    try {
      const r = await api.symbolIndexRebuild(symIdx?.workspace);
      if (r.ok) {
        useChatStore.setState({ error: `符号索引重建完成：${r.symbols ?? 0} 个符号（${r.files_updated ?? 0} 文件更新，${r.elapsed_ms ?? 0}ms）` });
      } else {
        useChatStore.setState({ error: `重建失败：${r.error || "未知错误"}` });
      }
      await loadSymbolIndex();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setRebuilding(false); }
  };

  const run = async () => {
    setRunning(true); setResult(null);
    try {
      const d = await api.runDiagnostics();
      setResult(d.checks);
      setCheckpoints(Array.isArray(d.checkpoints) ? d.checkpoints : null);
      await loadSymbolIndex();
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
      {/* plan-230-1144 M3: 代码符号索引（symbol_search / outline 的数据源） */}
      <div className="sb-section-label" style={{ margin: "14px 0 6px" }}>代码符号索引（函数级定位）</div>
      <div className="settings-resource-list">
        <div className="settings-resource-item">
          <div className="settings-resource-info">
            <div className="settings-resource-name">
              {symIdx?.workspace || "（未选择工作区）"}
            </div>
            <div className="settings-resource-desc">
              {symIdx == null
                ? "加载中…"
                : symIdx.error
                ? `读取失败：${symIdx.error}`
                : symIdx.available
                ? <>
                    <span>{symIdx.files ?? 0} 文件</span>
                    <span>·</span>
                    <span>{symIdx.symbols ?? 0} 符号</span>
                    {symIdx.last_updated ? (
                      <>
                        <span>·</span>
                        <span>最后更新 {new Date(symIdx.last_updated * 1000).toLocaleString()}</span>
                      </>
                    ) : null}
                  </>
                : "尚未建立索引（首次调用 symbol_search 会自动建立）"}
            </div>
          </div>
          <button className="btn btn-ghost btn-xs" disabled={rebuilding} onClick={() => void rebuildIndex()}>
            <IconRefresh size={12} /> {rebuilding ? "重建中…" : "重建索引"}
          </button>
        </div>
      </div>
    </div>
  );
}
