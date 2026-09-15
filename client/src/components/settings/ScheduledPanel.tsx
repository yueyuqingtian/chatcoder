/** 设置中心：自动化（plan-230-1144 M1.1 重写；plan-248-1258 M4 统一命名与表单）。
 *
 * 后端 scheduler_loop 驱动实际触发。本面板：
 * 1. 任务列表：人话时间描述 + 下次运行倒计时 + 最近执行状态 + 试跑/排程预览/编辑/删除；
 * 2. 创建与编辑统一走共享弹窗 ScheduledTaskFormModal（与左侧导航「自动化」页同款）。
 *
 * 命名说明：产品内统一称「自动化」（原「定时任务」），任务即"到点自动执行一轮对话"。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { ScheduledRunStatus, ScheduledTaskOut } from "@chatcoder/shared";
import { IconPlay, IconPlus, IconX, IconRefresh, IconChevronDown } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { Sw } from "./shared";
import {
  absTime, describeCron, relTime, ScheduledTaskFormModal,
} from "./ScheduledTaskFormModal";

const STATUS_META: Record<ScheduledRunStatus, { label: string; color: string }> = {
  triggered: { label: "已触发", color: "var(--info)" },
  ok: { label: "成功", color: "var(--success)" },
  failed: { label: "失败", color: "var(--error)" },
  skipped: { label: "已跳过", color: "var(--text-3)" },
  cancelled: { label: "已取消", color: "var(--warning)" },
  orphaned: { label: "会话失效", color: "var(--warning)" },
};

export function ScheduledPanel() {
  const [tasks, setTasks] = useState<ScheduledTaskOut[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<ScheduledTaskOut | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<ScheduledTaskOut | null>(null);
  const [expandedPreview, setExpandedPreview] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [previews, setPreviews] = useState<Record<number, string[]>>({});

  const load = useCallback(async () => {
    try { setTasks(await api.listScheduledTasks()); } catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // 秒级本地 tick：只驱动倒计时文案，不请求后端
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const handleToggle = async (t: ScheduledTaskOut, v: boolean) => {
    setErr(null);
    try { await api.updateScheduledTask(t.id, { enabled: v }); await load(); }
    catch (e) { setErr(String(e)); }
  };

  const handleRun = async (t: ScheduledTaskOut) => {
    setBusyId(t.id); setErr(null);
    try { await api.runScheduledTask(t.id); await load(); }
    catch (e) { setErr(String(e)); }
    finally { setBusyId(null); }
  };

  const togglePreview = async (t: ScheduledTaskOut) => {
    if (expandedPreview === t.id) { setExpandedPreview(null); return; }
    setBusyId(t.id);
    try {
      const r = await api.previewScheduledTask(t.id, 5);
      setPreviews((p) => ({ ...p, [t.id]: r.next_runs }));
      setExpandedPreview(t.id);
    } catch (e) { setErr(String(e)); }
    finally { setBusyId(null); }
  };

  const doDelete = async () => {
    if (!confirmDelete) return;
    try { await api.deleteScheduledTask(confirmDelete.id); await load(); }
    catch (e) { setErr(String(e)); }
    setConfirmDelete(null);
  };

  return (
    <div>
      <div className="sched-note">
        自动化任务会在到点时以你的身份向指定会话注入指令并启动一轮对话。服务端未运行时不会触发，
        错过的触发点按任务的「错过策略」处理。
      </div>

      {err && <div className="sched-error">{err}</div>}

      <div className="settings-toolbar">
        <button className="btn btn-ghost btn-sm" onClick={() => void load()}><IconRefresh size={13} /> 刷新</button>
        <button className="btn btn-primary btn-sm" onClick={() => { setEditing(null); setShowForm(true); }}>
          <IconPlus size={13} /> 新建自动化
        </button>
      </div>

      <ScheduledTaskFormModal
        open={showForm}
        editing={editing}
        onClose={() => setShowForm(false)}
        onSaved={() => void load()}
      />

      <div className="settings-resource-list">
        {tasks.map((t) => {
          const st = t.last_status ? STATUS_META[t.last_status] : null;
          const preview = previews[t.id];
          return (
            <div key={t.id} className="settings-resource-item sched-item">
              <div className="settings-resource-info">
                <div className="settings-resource-name">
                  {t.name}
                  {st && <span className="sched-status" style={{ color: st.color, borderColor: st.color }}>{st.label}</span>}
                </div>
                <div className="settings-resource-desc">
                  <span className="sched-human" title={t.cron}>{describeCron(t.cron)}</span>
                  <span>会话 #{t.session_id}</span>
                  {t.missed_policy === "run_once" && <span className="sched-tag">错过补跑</span>}
                </div>
                <div className="sched-times">
                  <span title={absTime(t.next_run_at)}>
                    下次运行：<b>{t.enabled ? relTime(t.next_run_at, nowMs) : "已停用"}</b>
                  </span>
                  <span title={absTime(t.last_run_at)}>上次：{relTime(t.last_run_at, nowMs)}</span>
                </div>
                {t.last_error && <div className="sched-last-error">{t.last_error}</div>}
                {expandedPreview === t.id && preview && (
                  <div className="sched-preview">
                    {preview.length === 0
                      ? <span className="sched-preview-empty">该表达式不会触发</span>
                      : preview.map((p, i) => <div key={i} title={p}>{i + 1}. {absTime(p)}</div>)}
                  </div>
                )}
              </div>
              <div className="settings-resource-actions">
                <Sw checked={t.enabled} onChange={(v) => void handleToggle(t, v)} />
                <button className="btn btn-ghost btn-xs" title="立即试跑一次"
                  disabled={busyId === t.id} onClick={() => void handleRun(t)}>
                  <IconPlay size={12} /> 试跑
                </button>
                <button className="btn btn-ghost btn-xs" title="预览接下来 5 次触发时刻"
                  disabled={busyId === t.id} onClick={() => void togglePreview(t)}>
                  <IconChevronDown size={12} /> 排程
                </button>
                <button className="btn btn-ghost btn-xs" onClick={() => { setEditing(t); setShowForm(true); }}>编辑</button>
                <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDelete(t)}><IconX size={12} /></button>
              </div>
            </div>
          );
        })}
        {tasks.length === 0 && <div className="navpage-empty">暂无自动化任务</div>}
      </div>

      <ConfirmDialog
        open={confirmDelete !== null}
        title="删除自动化"
        message={`删除「${confirmDelete?.name ?? ""}」？`}
        danger
        onCancel={() => setConfirmDelete(null)}
        onConfirm={doDelete}
      />
    </div>
  );
}
