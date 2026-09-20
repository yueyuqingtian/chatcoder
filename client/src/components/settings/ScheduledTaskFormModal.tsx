/** ScheduledTaskFormModal（plan-248-1258 M4）：定时任务创建/编辑共享弹窗。
 *
 * 从 ScheduledPanel 抽出，供「设置 → 自动化」与左侧导航「自动化」页共用——
 * 两处入口的创建方式完全一致（可视化频率选择 + 会话选择 + 错过策略 + cron 预览）。
 *
 * 表单模型与 cron 互转逻辑（buildCron/cronToForm/describeCron）也一并迁入本文件，
 * 由调用方通过 useScheduledTaskForm 复用。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { CronValidateOut, ScheduledMissedPolicy, ScheduledTaskOut } from "@chatcoder/shared";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { FormDialog, Input, Select, Textarea } from "../ui";

/* ── 可视化频率模型 ── */

export type FreqMode = "minute" | "interval" | "hourly" | "daily" | "weekly" | "monthly" | "custom";

const FREQ_OPTS: Array<{ value: FreqMode; label: string }> = [
  { value: "minute", label: "每分钟" },
  { value: "interval", label: "按间隔" },
  { value: "hourly", label: "每小时" },
  { value: "daily", label: "每天" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
  { value: "custom", label: "自定义" },
];

const INTERVAL_OPTS = [2, 5, 10, 15, 20, 30, 45];
const WEEKDAY_LABELS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const WEEKDAY_ORDER = [1, 2, 3, 4, 5, 6, 0];

const MISSED_OPTS: Array<{ value: ScheduledMissedPolicy; label: string; desc: string }> = [
  { value: "skip", label: "跳过", desc: "错过就不补，等下一个触发点" },
  { value: "run_once", label: "补跑一次", desc: "应用关闭期间错过的触发点，重启后补跑一次" },
];

export interface TaskForm {
  name: string;
  session_id: number | null;
  freq: FreqMode;
  intervalMinutes: number;
  minute: number;
  hour: number;
  weekdays: number[];
  monthDay: number;
  customCron: string;
  prompt: string;
  missed_policy: ScheduledMissedPolicy;
}

export const EMPTY_FORM: TaskForm = {
  name: "", session_id: null,
  freq: "daily", intervalMinutes: 15, minute: 0, hour: 9,
  weekdays: [1, 2, 3, 4, 5], monthDay: 1, customCron: "",
  prompt: "", missed_policy: "skip",
};

const pad2 = (n: number) => String(n).padStart(2, "0");

/** 可视化表单 → cron 表达式（与后端 cron 语义对齐：分 时 日 月 周） */
export function buildCron(f: TaskForm): string {
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

/** 展开周字段（支持单值/列表/区间，0-6） */
export function expandDow(dow: string): number[] | null {
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

/** cron 表达式 → 可视化表单字段（尽力匹配；匹配不上则进入「自定义」模式） */
export function cronToForm(cron: string): Partial<TaskForm> {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return { freq: "custom", customCron: cron };
  const [mi, ho, dom, mon, dow] = parts;
  const num = (s: string): number | null => (/^\d{1,2}$/.test(s) ? parseInt(s, 10) : null);

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

/** cron → 人话描述（列表展示用；匹配不上时回落原始表达式） */
export function describeCron(cron: string): string {
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

/** 相对时间（"3 分钟后" / "2 小时前"） */
export function relTime(iso: string | null, nowMs: number): string {
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

export function absTime(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString();
}

/**
 * 共享弹窗：新建/编辑定时任务。
 * 编辑传 editing（ScheduledTaskOut），新建传 null（自动带入默认会话）。
 */
export function ScheduledTaskFormModal({
  open, editing, initial, onClose, onSaved,
}: {
  open: boolean;
  editing: ScheduledTaskOut | null;
  /** 新建时的预填（模板入口用；editing 非空时忽略） */
  initial?: Partial<TaskForm> | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const sessions = useChatStore((s) => s.sessions);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const [form, setForm] = useState<TaskForm>(EMPTY_FORM);
  const [err, setErr] = useState<string | null>(null);
  const [cronCheck, setCronCheck] = useState<CronValidateOut | null>(null);
  const [cronChecking, setCronChecking] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const validateTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const builtCron = useMemo(() => buildCron(form), [form]);

  // 打开时初始化表单
  useEffect(() => {
    if (!open) return;
    setErr(null);
    if (editing) {
      setForm({
        ...EMPTY_FORM,
        name: editing.name, session_id: editing.session_id,
        prompt: editing.prompt, missed_policy: editing.missed_policy || "skip",
        ...cronToForm(editing.cron),
      });
    } else {
      setForm({
        ...EMPTY_FORM,
        session_id: currentSessionId ?? sessions[0]?.id ?? null,
        ...(initial ?? {}),
      });
    }
  }, [open, editing, initial, currentSessionId, sessions]);

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // cron 防抖校验
  useEffect(() => {
    if (!open) return;
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
  }, [builtCron, open]);

  const weeklyEmpty = form.freq === "weekly" && form.weekdays.length === 0;
  const formValid = Boolean(form.name.trim() && form.prompt.trim() && form.session_id != null
    && !weeklyEmpty && builtCron.trim() && cronCheck?.valid && !cronCheck.never_fires);

  const cronState = useMemo(() => {
    if (weeklyEmpty) return { cls: "err", text: "请至少选择一个星期" };
    if (!builtCron.trim()) return { cls: "", text: "请选择触发时间" };
    if (cronChecking) return { cls: "muted", text: "校验中…" };
    if (!cronCheck) return { cls: "", text: "" };
    if (!cronCheck.valid) return { cls: "err", text: `表达式无效：${cronCheck.error || "未知错误"}` };
    if (cronCheck.never_fires || !cronCheck.next_run_at) return { cls: "err", text: "该表达式永远不会触发（如 2 月 30 日）" };
    return { cls: "ok", text: `下次触发：${absTime(cronCheck.next_run_at)}（${relTime(cronCheck.next_run_at, nowMs)}）` };
  }, [builtCron, cronCheck, cronChecking, nowMs, weeklyEmpty]);

  const handleSave = async () => {
    if (!formValid || form.session_id == null) return;
    setErr(null);
    try {
      if (editing) {
        await api.updateScheduledTask(editing.id, {
          name: form.name.trim(), cron: builtCron.trim(), prompt: form.prompt,
          missed_policy: form.missed_policy,
        });
      } else {
        await api.createScheduledTask({
          session_id: form.session_id, name: form.name.trim(),
          cron: builtCron.trim(), prompt: form.prompt, missed_policy: form.missed_policy,
        });
      }
      onSaved();
      onClose();
    } catch (e) { setErr(String(e)); }
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
    <FormDialog
      open={open}
      onClose={onClose}
      title={editing ? "编辑自动化" : "新建自动化"}
      onSubmit={() => void handleSave()}
      submitLabel={editing ? "保存修改" : "创建"}
      submitDisabled={!formValid}
      width={560}
    >
      {err && <div className="sched-error">{err}</div>}
      <div className="settings-form-field">
        <label className="settings-field-label">任务名称</label>
        <Input placeholder="如 每日构建检查" value={form.name}
          onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} aria-label="任务名称" />
      </div>
      <div className="settings-form-field">
        <label className="settings-field-label">注入到会话</label>
        <Select value={form.session_id != null ? String(form.session_id) : ""}
          onChange={(v) => setForm((p) => ({ ...p, session_id: v ? Number(v) : null }))}
          options={[{ value: "", label: "— 请选择会话 —" }, ...sessions.map((s) => ({ value: String(s.id), label: s.title || `会话 #${s.id}` }))]}
          aria-label="注入到会话" />
      </div>

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
          <Select style={{ maxWidth: 160 }} value={String(form.minute)}
            onChange={(v) => setForm((p) => ({ ...p, minute: Number(v) }))}
            options={Array.from({ length: 60 }, (_, i) => ({ value: String(i), label: `第 ${pad2(i)} 分` }))}
            aria-label="触发分钟" />
        </div>
      )}

      {(form.freq === "daily" || form.freq === "weekly" || form.freq === "monthly") && (
        <div className="settings-form-field">
          <label className="settings-field-label">
            {form.freq === "daily" ? "每天触发时刻" : form.freq === "weekly" ? "触发时刻" : "每月触发时刻"}
          </label>
          <Input type="time" style={{ maxWidth: 160 }}
            value={timeValue} onChange={(e) => setTimeValue(e.target.value)} aria-label="触发时刻" />
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
          <Select style={{ maxWidth: 160 }} value={String(form.monthDay)}
            onChange={(v) => setForm((p) => ({ ...p, monthDay: Number(v) }))}
            options={Array.from({ length: 31 }, (_, i) => ({ value: String(i + 1), label: `${i + 1} 日` }))}
            aria-label="每月触发日" />
          {form.monthDay > 28 && (
            <div className="sched-field-desc">部分月份没有 {form.monthDay} 日，这些月份将自动跳过。</div>
          )}
        </div>
      )}

      {form.freq === "custom" && (
        <div className="settings-form-field">
          <label className="settings-field-label">自定义表达式（高级：分 时 日 月 周）</label>
          <Input className={"sched-cron-input" + (cronState.cls === "err" ? " invalid" : "")}
            placeholder="0 9 * * 1-5" value={form.customCron} style={{ fontFamily: "var(--font-mono)" }}
            aria-label="自定义 cron 表达式"
            onChange={(e) => setForm((p) => ({ ...p, customCron: e.target.value }))} />
        </div>
      )}

      <div className={"sched-cron-hint" + (cronState.cls ? " " + cronState.cls : "")} style={{ marginTop: 0 }}>
        {cronState.text}
      </div>

      <div className="settings-form-field">
        <label className="settings-field-label">触发时注入的指令</label>
        <Textarea rows={3} placeholder="如：运行测试并总结失败原因"
          value={form.prompt} onChange={(e) => setForm((p) => ({ ...p, prompt: e.target.value }))} aria-label="触发时注入的指令" />
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
  );
}
