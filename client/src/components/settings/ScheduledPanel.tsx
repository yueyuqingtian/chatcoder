/** 设置中心：定时任务（plan-230-1144 M1.1 重写 + 可视化时间选择）。
 *
 * 后端 scheduler_loop 落地后本面板补齐：
 * 1. 可视化时间选择（本文件核心）：频率 chips（每分钟/间隔/每小时/每天/每周/每月）
 *    + 原生时间选择器 + 星期多选 + 日期选择；cron 表达式仅保留为「自定义」高级项。
 *    此前直接暴露 5 段 cron 输入框，用户看不懂 `* * * * *`（用户反馈）。
 * 2. 实时校验 + "下次触发时刻"预览（防抖调 /meta/validate）；
 * 3. 下次运行倒计时（本地秒级 tick，不轮询后端）；
 * 4. 最近一次执行状态徽标（ok / failed / skipped / orphaned）+ 错误详情；
 * 5. 错过策略选择（skip / run_once）、"立即试跑"与"排程预览"。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import type { CronValidateOut, ScheduledMissedPolicy, ScheduledRunStatus, ScheduledTaskOut } from "@chatcoder/shared";
import { useChatStore } from "../../store/chat";
import { IconPlay, IconPlus, IconX, IconRefresh, IconChevronDown } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { FormDialog } from "../ui/FormDialog";
import { Sw } from "./shared";

/* ── 可视化频率模型 ── */

type FreqMode = "minute" | "interval" | "hourly" | "daily" | "weekly" | "monthly" | "custom";

const FREQ_OPTS: Array<{ value: FreqMode; label: string }> = [
  { value: "minute", label: "每分钟" },
  { value: "interval", label: "按间隔" },
  { value: "hourly", label: "每小时" },
  { value: "daily", label: "每天" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
  { value: "custom", label: "自定义" },
];

/** 间隔分钟可选值（常用粒度） */
const INTERVAL_OPTS = [2, 5, 10, 15, 20, 30, 45];
/** 星期标签（cron 惯例：0=周日 ... 6=周六）；渲染顺序为周一~周日 */
const WEEKDAY_LABELS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const WEEKDAY_ORDER = [1, 2, 3, 4, 5, 6, 0];

const STATUS_META: Record<ScheduledRunStatus, { label: string; color: string }> = {
  triggered: { label: "已触发", color: "var(--info)" },
  ok: { label: "成功", color: "var(--success)" },
  failed: { label: "失败", color: "var(--error)" },
  skipped: { label: "已跳过", color: "var(--text-3)" },
  cancelled: { label: "已取消", color: "var(--warning)" },
  orphaned: { label: "会话失效", color: "var(--warning)" },
};

const MISSED_OPTS: Array<{ value: ScheduledMissedPolicy; label: string; desc: string }> = [
  { value: "skip", label: "跳过", desc: "错过就不补，等下一个触发点" },
  { value: "run_once", label: "补跑一次", desc: "应用关闭期间错过的触发点，重启后补跑一次" },
];

interface TaskForm {
  name: string;
  session_id: number | null;
  freq: FreqMode;
  intervalMinutes: number;
  minute: number;      // 0-59
  hour: number;        // 0-23
  weekdays: number[];  // 0=周日 ... 6=周六
  monthDay: number;    // 1-31
  customCron: string;
  prompt: string;
  missed_policy: ScheduledMissedPolicy;
}

const EMPTY_FORM: TaskForm = {
  name: "", session_id: null,
  freq: "daily", intervalMinutes: 15, minute: 0, hour: 9,
  weekdays: [1, 2, 3, 4, 5], monthDay: 1, customCron: "",
  prompt: "", missed_policy: "skip",
};

const pad2 = (n: number) => String(n).padStart(2, "0");

/** 可视化表单 → cron 表达式（与后端 cron 语义对齐：分 时 日 月 周） */
function buildCron(f: TaskForm): string {
  switch (f.freq) {
    case "minute": return "* * * * *";
    case "interval": return `*/${f.intervalMinutes} * * * *`;
    case "hourly": return `${f.minute} * * * *`;
    case "daily": return `${f.minute} ${f.hour} * * *`;
    case "weekly": {
      const days = [...f.weekdays].sort((a, b) => a - b);
      return days.length ? `${f.minute} ${f.hour} * * ${days.join(",")}` : "";
    }
    case "monthly": return `${f.minute} ${f.hour} ${f.monthDay} * *`;
    case "custom": return f.customCron.trim();
  }
}

/** cron 表达式 → 可视化表单字段（尽力匹配；匹配不上则进入「自定义」模式） */
function cronToForm(cron: string): Partial<TaskForm> {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return { freq: "custom", customCron: cron };
  const [mi, ho, dom, mon, dow] = parts;
  const num = (s: string): number | null => (/^\d{1,2}$/.test(s) ? parseInt(s, 10) : null);

  // */N 分 * * * *
  const step = mi.match(/^\*\/(\d{1,2})$/);
  if (step && ho === "*" && dom === "*" && mon === "*" && dow === "*") {
    return { freq: "interval", intervalMinutes: parseInt(step[1], 10) };
  }
  if (mi === "*" && ho === "*" && dom === "*" && mon === "*" && dow === "*") {
    return { freq: "minute" };
  }
  const m = num(mi);
  if (m != null && ho === "*" && dom === "*" && mon === "*" && dow === "*") {
    return { freq: "hourly", minute: m };
  }
  const h = num(ho);
  if (m != null && h != null && dom === "*" && mon === "*" && dow === "*") {
    return { freq: "daily", minute: m, hour: h };
  }
  if (m != null && h != null && dom === "*" && mon === "*" && /^[\d,\-]+$/.test(dow)) {
    const days = expandDow(dow);
    if (days && days.length) return { freq: "weekly", minute: m, hour: h, weekdays: days };
  }
  const d = num(dom);
  if (m != null && h != null && d != null && mon === "*" && dow === "*") {
    return { freq: "monthly", minute: m, hour: h, monthDay: d };
  }
  return { freq: "custom", customCron: cron };
}

/** 展开周字段（支持单值/列表/区间，0-6） */
function expandDow(dow: string): number[] | null {
  const out = new Set<number>();
  for (const seg of dow.split(",")) {
    const range = seg.match(/^(\d)-(\d)$/);
    if (range) {
      const a = parseInt(range[1], 10);
      const b = parseInt(range[2], 10);
      if (a > 6 || b > 6 || a > b) return null;
      for (let i = a; i <= b; i++) out.add(i);
    } else {
      const n = parseInt(seg, 10);
      if (Number.isNaN(n) || n < 0 || n > 6) return null;
      out.add(n);
    }
  }
  return [...out].sort((x, y) => x - y);
}

/** cron → 人话描述（列表展示用；匹配不上时回落原始表达式） */
function describeCron(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [mi, ho, dom, mon, dow] = parts;
  const num = (s: string): number | null => (/^\d{1,2}$/.test(s) ? parseInt(s, 10) : null);

  const step = mi.match(/^\*\/(\d{1,2})$/);
  if (step && ho === "*" && dom === "*" && mon === "*" && dow === "*") return `每 ${step[1]} 分钟`;
  if (mi === "*" && ho === "*" && dom === "*" && mon === "*" && dow === "*") return "每分钟";
  const m = num(mi);
  if (m != null && ho === "*" && dom === "*" && mon === "*" && dow === "*") return `每小时第 ${m} 分`;
  const h = num(ho);
  if (m != null && h != null && dom === "*" && mon === "*" && dow === "*") return `每天 ${pad2(h)}:${pad2(m)}`;
  if (m != null && h != null && dom === "*" && mon === "*" && /^[\d,\-]+$/.test(dow)) {
    const days = expandDow(dow);
    if (days) return `每周 ${days.map((x) => WEEKDAY_LABELS[x]).join("、")} ${pad2(h)}:${pad2(m)}`;
  }
  const d = num(dom);
  if (m != null && h != null && d != null && mon === "*" && dow === "*") return `每月 ${d} 日 ${pad2(h)}:${pad2(m)}`;
  return cron;
}

/** 相对时间（"3 分钟后" / "2 小时前"），用于下次运行与最近执行展示 */
function relTime(iso: string | null, nowMs: number): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const diff = t - nowMs;
  const abs = Math.abs(diff);
  const min = Math.round(abs / 60000);
  let span: string;
  if (min < 1) span = `${Math.round(abs / 1000)} 秒`;
  else if (min < 60) span = `${min} 分钟`;
  else if (min < 60 * 24) span = `${Math.round(min / 60)} 小时`;
  else span = `${Math.round(min / (60 * 24))} 天`;
  return diff >= 0 ? `${span}后` : `${span}前`;
}

function absTime(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString();
}

export function ScheduledPanel() {
  const sessions = useChatStore((s) => s.sessions);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const [tasks, setTasks] = useState<ScheduledTaskOut[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<TaskForm>(EMPTY_FORM);
  const [confirmDelete, setConfirmDelete] = useState<ScheduledTaskOut | null>(null);
  const [expandedPreview, setExpandedPreview] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [cronCheck, setCronCheck] = useState<CronValidateOut | null>(null);
  const [cronChecking, setCronChecking] = useState(false);
  const [previews, setPreviews] = useState<Record<number, string[]>>({});
  const validateTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 可视化选择的当前 cron（实时派生，保存/校验共用） */
  const builtCron = useMemo(() => buildCron(form), [form]);

  const load = useCallback(async () => {
    try { setTasks(await api.listScheduledTasks()); } catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // 秒级本地 tick：只驱动倒计时文案，不请求后端
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // cron 防抖校验（350ms）：可视化模式下也能实时反馈"下次触发时刻"
  useEffect(() => {
    const cron = builtCron.trim();
    if (validateTimer.current) clearTimeout(validateTimer.current);
    if (!cron) { setCronCheck(null); return; }
    setCronChecking(true);
    validateTimer.current = setTimeout(async () => {
      try { setCronCheck(await api.validateCron(cron)); }
      catch { setCronCheck(null); }
      finally { setCronChecking(false); }
    }, 350);
    return () => { if (validateTimer.current) clearTimeout(validateTimer.current); };
  }, [builtCron]);

  const weeklyEmpty = form.freq === "weekly" && form.weekdays.length === 0;

  const cronState = useMemo(() => {
    if (weeklyEmpty) return { cls: "err", text: "请至少选择一个星期" };
    if (!builtCron.trim()) return { cls: "", text: "请选择触发时间" };
    if (cronChecking) return { cls: "muted", text: "校验中…" };
    if (!cronCheck) return { cls: "", text: "" };
    if (!cronCheck.valid) return { cls: "err", text: `表达式无效：${cronCheck.error || "未知错误"}` };
    if (cronCheck.never_fires || !cronCheck.next_run_at) return { cls: "err", text: "该表达式永远不会触发（如 2 月 30 日）" };
    return { cls: "ok", text: `下次触发：${absTime(cronCheck.next_run_at)}（${relTime(cronCheck.next_run_at, nowMs)}）` };
  }, [builtCron, cronCheck, cronChecking, nowMs, weeklyEmpty]);

  const openCreate = () => {
    setForm({ ...EMPTY_FORM, session_id: currentSessionId ?? sessions[0]?.id ?? null });
    setEditingId(null);
    setShowForm(true);
  };

  const openEdit = (t: ScheduledTaskOut) => {
    const parsed = cronToForm(t.cron);
    setForm({
      ...EMPTY_FORM,
      name: t.name, session_id: t.session_id,
      prompt: t.prompt, missed_policy: t.missed_policy || "skip",
      ...parsed,
    });
    setEditingId(t.id);
    setShowForm(true);
  };

  const formValid = Boolean(form.name.trim() && form.prompt.trim() && form.session_id != null
    && !weeklyEmpty && builtCron.trim() && cronCheck?.valid && !cronCheck.never_fires);

  const handleSave = async () => {
    if (!formValid || form.session_id == null) return;
    setErr(null);
    try {
      if (editingId != null) {
        await api.updateScheduledTask(editingId, {
          name: form.name.trim(), cron: builtCron.trim(), prompt: form.prompt,
          missed_policy: form.missed_policy,
        });
      } else {
        await api.createScheduledTask({
          session_id: form.session_id, name: form.name.trim(),
          cron: builtCron.trim(), prompt: form.prompt, missed_policy: form.missed_policy,
        });
      }
      setShowForm(false); setEditingId(null); setForm(EMPTY_FORM);
      await load();
    } catch (e) { setErr(String(e)); }
  };

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

  const toggleWeekday = (d: number) => {
    setForm((p) => ({
      ...p,
      weekdays: p.weekdays.includes(d) ? p.weekdays.filter((x) => x !== d) : [...p.weekdays, d],
    }));
  };

  const timeValue = `${pad2(form.hour)}:${pad2(form.minute)}`;
  const setTimeValue = (v: string) => {
    const [hh, mm] = v.split(":").map((x) => parseInt(x, 10));
    if (Number.isNaN(hh) || Number.isNaN(mm)) return;
    setForm((p) => ({ ...p, hour: hh, minute: mm }));
  };

  return (
    <div>
      <div className="sched-note">
        定时任务会在到点时以你的身份向指定会话注入指令并启动一轮对话。服务端未运行时不会触发，
        错过的触发点按任务的「错过策略」处理。
      </div>

      {err && <div className="sched-error">{err}</div>}

      <div className="settings-toolbar">
        <button className="btn btn-ghost btn-sm" onClick={() => void load()}><IconRefresh size={13} /> 刷新</button>
        <button className="btn btn-primary btn-sm" onClick={openCreate}>
          <IconPlus size={13} /> 新建任务
        </button>
      </div>

      <FormDialog
        open={showForm}
        onClose={() => { setShowForm(false); setEditingId(null); }}
        title={editingId != null ? "编辑定时任务" : "新建定时任务"}
        onSubmit={() => void handleSave()}
        submitLabel={editingId != null ? "保存修改" : "创建"}
        submitDisabled={!formValid}
        width={560}
      >
          <div className="settings-form-field">
            <label className="settings-field-label">任务名称</label>
            <input className="ui-input" placeholder="如 每日构建检查" value={form.name}
              onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} />
          </div>
          <div className="settings-form-field">
            <label className="settings-field-label">注入到会话</label>
            <select className="ui-select" value={form.session_id ?? ""}
              onChange={(e) => setForm((p) => ({ ...p, session_id: e.target.value ? Number(e.target.value) : null }))}>
              <option value="">— 请选择会话 —</option>
              {sessions.map((s) => <option key={s.id} value={s.id}>{s.title || `会话 #${s.id}`}</option>)}
            </select>
          </div>

          {/* ── 可视化时间选择（用户直接点选，无需理解 cron） ── */}
          <div className="settings-form-field">
            <label className="settings-field-label">触发频率</label>
            <div className="settings-chips">
              {FREQ_OPTS.map((o) => (
                <button key={o.value} type="button"
                  className={"settings-chip" + (form.freq === o.value ? " on" : "")}
                  onClick={() => setForm((p) => ({ ...p, freq: o.value }))}>
                  {o.label}
                </button>
              ))}
            </div>
          </div>

          {form.freq === "interval" && (
            <div className="settings-form-field">
              <label className="settings-field-label">每隔多少分钟</label>
              <div className="settings-chips">
                {INTERVAL_OPTS.map((n) => (
                  <button key={n} type="button"
                    className={"settings-chip" + (form.intervalMinutes === n ? " on" : "")}
                    onClick={() => setForm((p) => ({ ...p, intervalMinutes: n }))}>
                    {n} 分钟
                  </button>
                ))}
              </div>
            </div>
          )}

          {form.freq === "hourly" && (
            <div className="settings-form-field">
              <label className="settings-field-label">在每小时的哪一分钟触发</label>
              <select className="ui-select" style={{ maxWidth: 160 }} value={form.minute}
                onChange={(e) => setForm((p) => ({ ...p, minute: Number(e.target.value) }))}>
                {Array.from({ length: 60 }, (_, i) => (
                  <option key={i} value={i}>第 {pad2(i)} 分</option>
                ))}
              </select>
            </div>
          )}

          {(form.freq === "daily" || form.freq === "weekly" || form.freq === "monthly") && (
            <div className="settings-form-field">
              <label className="settings-field-label">
                {form.freq === "daily" ? "每天触发时刻" : form.freq === "weekly" ? "触发时刻" : "每月触发时刻"}
              </label>
              <input type="time" className="ui-input" style={{ maxWidth: 160 }}
                value={timeValue} onChange={(e) => setTimeValue(e.target.value)} />
            </div>
          )}

          {form.freq === "weekly" && (
            <div className="settings-form-field">
              <label className="settings-field-label">在星期几触发（可多选）</label>
              <div className="settings-chips">
                {WEEKDAY_ORDER.map((d) => (
                  <button key={d} type="button"
                    className={"settings-chip" + (form.weekdays.includes(d) ? " on" : "")}
                    onClick={() => toggleWeekday(d)}>
                    {WEEKDAY_LABELS[d]}
                  </button>
                ))}
              </div>
            </div>
          )}

          {form.freq === "monthly" && (
            <div className="settings-form-field">
              <label className="settings-field-label">每月哪一天触发</label>
              <select className="ui-select" style={{ maxWidth: 160 }} value={form.monthDay}
                onChange={(e) => setForm((p) => ({ ...p, monthDay: Number(e.target.value) }))}>
                {Array.from({ length: 31 }, (_, i) => (
                  <option key={i + 1} value={i + 1}>{i + 1} 日</option>
                ))}
              </select>
              {form.monthDay > 28 && (
                <div className="sched-field-desc">部分月份没有 {form.monthDay} 日，这些月份将自动跳过。</div>
              )}
            </div>
          )}

          {form.freq === "custom" && (
            <div className="settings-form-field">
              <label className="settings-field-label">自定义表达式（高级：分 时 日 月 周）</label>
              <input className={"ui-input sched-cron-input" + (cronState.cls === "err" ? " invalid" : "")}
                placeholder="0 9 * * 1-5" value={form.customCron} style={{ fontFamily: "var(--font-mono)" }}
                onChange={(e) => setForm((p) => ({ ...p, customCron: e.target.value }))} />
            </div>
          )}

          <div className={"sched-cron-hint" + (cronState.cls ? " " + cronState.cls : "")} style={{ marginTop: 0 }}>
            {cronState.text}
          </div>

          <div className="settings-form-field">
            <label className="settings-field-label">触发时注入的指令</label>
            <textarea className="ui-textarea" rows={3} placeholder="如：运行测试并总结失败原因"
              value={form.prompt} onChange={(e) => setForm((p) => ({ ...p, prompt: e.target.value }))} />
          </div>
          <div className="settings-form-field">
            <label className="settings-field-label">错过策略</label>
            <div className="settings-chips">
              {MISSED_OPTS.map((o) => (
                <button key={o.value} type="button"
                  className={"settings-chip" + (form.missed_policy === o.value ? " on" : "")}
                  onClick={() => setForm((p) => ({ ...p, missed_policy: o.value }))} title={o.desc}>
                  {o.label}
                </button>
              ))}
            </div>
            <div className="sched-field-desc">{MISSED_OPTS.find((o) => o.value === form.missed_policy)?.desc}</div>
          </div>
      </FormDialog>

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
                  {/* 人话描述为主，原始 cron 悬停可见 */}
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
                <button className="btn btn-ghost btn-xs" onClick={() => openEdit(t)}>编辑</button>
                <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDelete(t)}><IconX size={12} /></button>
              </div>
            </div>
          );
        })}
        {tasks.length === 0 && <div className="navpage-empty">暂无定时任务</div>}
      </div>

      <ConfirmDialog
        open={confirmDelete !== null}
        title="删除定时任务"
        message={`删除「${confirmDelete?.name ?? ""}」？`}
        danger
        onCancel={() => setConfirmDelete(null)}
        onConfirm={doDelete}
      />
    </div>
  );
}
