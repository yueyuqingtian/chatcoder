/** 设置中心：诊断（v2.2 对齐 zcode 3.18）。系统健康检查 + checkpoint 占用与清理。
 * 代码符号索引的状态/开关/重建已统一收敛到「索引库」页面（本页不再展示，避免两处入口数据口径不一）。 */
import { notify } from "../../store/toast";
import { useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconAlertCircle, IconCheckCircle, IconDatabase, IconRefresh, IconWrench } from "../icons";
import { Empty } from "../ui";

interface CheckpointStat { workspace: string; file_count: number; size_mb: number; orphan_count: number; retention_days?: number }

export function DiagnosticsPanel() {
  const [result, setResult] = useState<Array<{ name: string; ok: boolean; detail?: string }> | null>(null);
  const [checkpoints, setCheckpoints] = useState<CheckpointStat[] | null>(null);
  const [running, setRunning] = useState(false);
  const [cleaning, setCleaning] = useState(false);

  const run = async () => {
    setRunning(true); setResult(null);
    try {
      const d = await api.runDiagnostics();
      /* plan-282-1434（B5）：后端 `checks` 返回的是**对象**（`{"git": {ok, detail}, ...}`，
         见 server/app/gateway/routers/diagnostics.py:20/27/35/40-50），
         此前前端直接按数组 `.map(...)` 渲染 → 运行时 `e.map is not a function`，
         整页被 ErrorBoundary 接住。这里做归一化：数组直接使用，对象则按 name 转成列表。 */
      const raw: unknown = d.checks;
      const list = Array.isArray(raw)
        ? (raw as Array<{ name: string; ok: boolean; detail?: string }>)
        : Object.entries((raw ?? {}) as Record<string, { ok?: boolean; detail?: string } | undefined>)
            .map(([name, v]) => ({
              name,
              ok: Boolean(v?.ok),
              detail: typeof v?.detail === "string" ? v.detail : undefined,
            }));
      setResult(list);
      setCheckpoints(Array.isArray(d.checkpoints) ? d.checkpoints : null);
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setRunning(false); }
  };
  const clean = async () => {
    setCleaning(true);
    try {
      const r = await api.cleanupCheckpoints();
      /* plan-20261115：GC 只删「孤儿 / 超过保留期 / 超量」三类文件。
         旧文案只说「已清理 N 个」——N=0 时用户以为清理失败（反馈「永远清理不完」），
         实际是保留期内的活跃快照按设计不删。这里把保留期与保底原因说清楚。 */
      const sum = (k: "deleted" | "kept") =>
        (r.results ?? []).reduce((a, b) => a + (Number(b[k]) || 0), 0);
      const deleted = sum("deleted");
      const kept = sum("kept");
      const days = r.retention_days ?? 0;
      if (deleted > 0) {
        notify.success(`已清理 ${deleted} 个 checkpoint 文件`);
      } else if (kept > 0) {
        notify.info(`暂无可清理项：${kept} 个文件仍在 ${days} 天保留期内，到期后或被回滚/删除会话时自动回收`);
      } else {
        notify.info("没有需要清理的 checkpoint 文件");
      }
      await run();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setCleaning(false); }
  };
  const okCount = result?.filter((c) => c.ok).length ?? 0;
  const failCount = (result?.length ?? 0) - okCount;
  return (
    <div className="sc-page">
      {/* plan-91-393：顶部摘要 + 操作右对齐；检查项分组为带状态图标的行 */}
      <div className="sc-toolbar">
        <div className="sc-toolbar-summary">
          {result == null ? "系统健康检查" : failCount === 0 ? `全部 ${okCount} 项正常` : `${failCount} 项异常 · ${okCount} 项正常`}
        </div>
        <span className="sc-flex-spacer" />
        {checkpoints != null && checkpoints.length > 0 && (
          <button className="btn btn-ghost btn-sm" onClick={clean} disabled={cleaning}><IconWrench size={13} /> {cleaning ? "清理中…" : "清理 checkpoint"}</button>
        )}
        <button className="btn btn-primary btn-sm" onClick={run} disabled={running}>
          <span className={`sc-icon-slot ${running ? "spin" : ""}`}><IconRefresh size={13} /></span> {running ? "运行中…" : "运行诊断"}
        </button>
      </div>
      {result == null && !running && (
        <Empty title="尚未运行诊断" desc="嗒嗒会检查 Git、运行时与存储状态"
          action={<button className="btn btn-secondary btn-sm" onClick={run}>开始检查</button>} />
      )}
      {result != null && (
        <>
          <div className="sc-group-label">检查项</div>
          <div className="sc-list">
            {result.map((c) => (
              <div key={c.name} className={`sc-row sc-check ${c.ok ? "ok" : "fail"}`}>
                <span className="sc-status-icon">{c.ok ? <IconCheckCircle size={16} /> : <IconAlertCircle size={16} />}</span>
                <div className="sc-row-main">
                  <div className="sc-row-title">{c.name}</div>
                  {c.detail && <div className="sc-row-desc">{c.detail}</div>}
                </div>
                <span className={`sc-badge ${c.ok ? "ok" : "fail"}`}>{c.ok ? "正常" : "异常"}</span>
              </div>
            ))}
          </div>
        </>
      )}
      {checkpoints != null && checkpoints.length > 0 && (
        <>
          <div className="sc-group-label">checkpoint 存储（.chatcoder/checkpoints）</div>
          <div className="sc-list">
            {checkpoints.map((c) => (
              <div key={c.workspace} className="sc-row">
                <span className="sc-tile"><IconDatabase size={15} /></span>
                <div className="sc-row-main">
                  <div className="sc-row-title sc-mono">{c.workspace}</div>
                  <div className="sc-row-desc">{c.file_count} 个文件 · {c.size_mb} MB · 孤儿 {c.orphan_count} 个</div>
                </div>
                {c.orphan_count > 0 && <span className="sc-badge warn">有孤儿</span>}
                <div className="sc-row-actions">
                  <button className="btn btn-ghost btn-sm" disabled={cleaning}
                    onClick={async () => {
                      setCleaning(true);
                      try {
                        const r = await api.cleanupCheckpoints(c.workspace);
                        const d = r.results?.[0]?.deleted ?? 0;
                        const keptN = r.results?.[0]?.kept ?? 0;
                        const days = r.retention_days ?? c.retention_days ?? 0;
                        if (d > 0) notify.success(`已清理 ${d} 个 checkpoint 文件`);
                        else if (keptN > 0) notify.info(`暂无可清理项：${keptN} 个文件仍在 ${days} 天保留期内`);
                        else notify.info("没有需要清理的 checkpoint 文件");
                        await run();
                      } catch (e) { useChatStore.setState({ error: String(e) }); } finally { setCleaning(false); }
                    }}>
                    <IconWrench size={12} /> 清理
                  </button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
