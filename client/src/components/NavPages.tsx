/** 左侧导航页（v7 对齐 ZCode；plan-248-1258 M4 统一为「自动化」并复用共享创建弹窗）。
 *  plan-282-1441（#6/#9）：原 SkillsPage / McpPage 已移除——技能与连接器并入「拓展」；
 *  plan-41-198：左面板「拓展」= 市场视图（MarketPanel），设置页「拓展」= 已安装管理（InstalledPanel）。 */
import { useCallback, useEffect, useState } from "react";
import { api, type ScheduledTaskOut } from "../api/client";
import { useChatStore } from "../store/chat";
import {
  IconCheckSquare, IconClipboard, IconFileText, IconInfo,
  IconTarget, IconX, IconZap,
} from "./icons";
import { ScheduledTaskFormModal, cronToForm, type TaskForm } from "./settings/ScheduledTaskFormModal";
import { Switch, IconButton } from "./ui";

/** plan-282-1416：开关统一走组件库 Switch（此前这里是第三套实现 .sp-switch） */
function SwitchRow({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return <Switch checked={checked} onChange={onChange} />;
}

/** 任务模板（对齐 zcode 自动化页） */
const IDLE_TEMPLATES = [
  { icon: <IconClipboard size={15} />, name: "Git 站会摘要", desc: "每周五总结这一周发生的事情。", schedule: "最早可用时段", cron: "0 13 * * 5", prompt: "总结本周 git 提交、模块变化与待跟进事项，生成站会摘要。" },
  { icon: <IconZap size={15} />, name: "CI 失败与不稳定测试报告", desc: "汇总近期 CI 失败和不稳定测试，并分析可能原因。", schedule: "最早可用时段", cron: "0 13 * * *", prompt: "汇总近期 CI 失败和不稳定测试，并分析可能原因。" },
  { icon: <IconCheckSquare size={15} />, name: "文档同步检查", desc: "检查 README、docs、配置说明和使用示例是否与当前代码一致。", schedule: "最早可用时段", cron: "0 13 * * 3", prompt: "检查 README、docs、配置说明和使用示例是否与当前代码一致，列出不一致处。" },
];
const CRON_TEMPLATES = [
  { icon: <IconTarget size={15} />, name: "晨会动态", desc: "汇总上一个工作日以来的提交、模块变化与待跟进事项。", schedule: "工作日 09:00", cron: "0 9 * * 1-5", prompt: "汇总上一个工作日以来的 git 提交、模块变化与待跟进事项。" },
  { icon: <IconZap size={15} />, name: "风险扫描", desc: "检查最近 24 小时的代码变更，报告有直接证据的高置信风险。", schedule: "每天 10:00", cron: "0 10 * * *", prompt: "检查最近 24 小时的代码变更，报告有直接证据的高置信风险。" },
  { icon: <IconFileText size={15} />, name: "发布简报", desc: "整理本周已合并变更，生成团队版与用户版发布摘要。", schedule: "每周五 16:00", cron: "0 16 * * 5", prompt: "整理本周已合并变更，生成团队版与用户版发布摘要。" },
  { icon: <IconCheckSquare size={15} />, name: "文档同步检查", desc: "对照近期实现变更，找出可能遗漏的文档更新。", schedule: "每周三 15:00", cron: "0 15 * * 3", prompt: "对照近期实现变更，找出可能遗漏的文档更新。" },
];

/** cron → 人类可读（对齐 zcode 调度文案） */
function cronLabel(cron: string): string {
  const p = cron.trim().split(/\s+/);
  if (p.length !== 5) return cron;
  const [min, hour, , , dow] = p;
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
  if (dow === "*") return `每天 ${time}`;
  if (dow === "1-5") return `工作日 ${time}`;
  const dowMap: Record<string, string> = { "0": "日", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六" };
  if (dowMap[dow]) return `每周${dowMap[dow]} ${time}`;
  return cron;
}

export function ScheduledPage() {
  const [tasks, setTasks] = useState<ScheduledTaskOut[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<ScheduledTaskOut | null>(null);
  const [initial, setInitial] = useState<Partial<TaskForm> | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [keepAwake, setKeepAwake] = useState(() => localStorage.getItem("chatcoder.keepAwake") === "1");

  const load = useCallback(async () => {
    try { setTasks(await api.listScheduledTasks()); } catch { /* ignore */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  // S3（plan-41-197）：开关语义 = “运行会话时保持电脑唤醒”——偏好只在运行期生效：
  //   有任何会话在跑（含后台/自动化任务）时才向主进程申请防休眠，全部跑完自动释放；
  //   主进程侧已改用 prevent-display-sleep（阻止显示器休眠，见 electron/main.cjs）。
  const anyRunning = useChatStore((s) => s.isRunning || s.sessions.some((x) => x.has_running));
  useEffect(() => {
    void window.chatcoderAPI?.setKeepAwake?.(keepAwake && anyRunning);
    localStorage.setItem("chatcoder.keepAwake", keepAwake ? "1" : "0");
  }, [keepAwake, anyRunning]);

  /** 模板 → 打开共享弹窗并预填（plan-248-1258 M4：与设置页同一表单） */
  const applyTemplate = (tpl: { name: string; cron: string; prompt: string }) => {
    const parsed = cronToForm(tpl.cron);
    setInitial({ name: tpl.name, prompt: tpl.prompt, ...parsed });
    setEditing(null);
    setShowForm(true);
  };

  const runNow = async (t: ScheduledTaskOut) => {
    setBusyId(t.id);
    try { await api.runScheduledTask(t.id); await load(); } catch { /* ignore */ }
    finally { setBusyId(null); }
  };

  return (
    <div className="automation-page">
      <h1 className="automation-title">自动化</h1>
      <p className="automation-sub">创建自动化任务，或排队在闲时算力空闲时后台执行。</p>

      <div className="automation-card">
        {tasks.length === 0 && <div className="automation-empty">还没有自动化任务</div>}
        {tasks.length > 0 && (
          <div className="automation-list">
            {tasks.map((t) => (
              <div key={t.id} className="automation-item">
                <div className="automation-item-main">
                  <div className="automation-item-name">{t.name}</div>
                  <div className="automation-item-desc">{cronLabel(t.cron)}</div>
                </div>
                <div className="automation-item-actions">
                  <IconButton icon={<IconZap size={13} />} title="立即试跑" disabled={busyId === t.id} onClick={() => void runNow(t)} />
                  <button className="btn btn-ghost btn-xs" onClick={() => { setEditing(t); setInitial(null); setShowForm(true); }}>编辑</button>
                  <SwitchRow checked={t.enabled} onChange={async (v) => { try { await api.updateScheduledTask(t.id, { enabled: v }); load(); } catch { /* ignore */ } }} />
                  <IconButton size="xs" icon={<IconX size={13} />} title="删除" tone="danger" onClick={async () => { try { await api.deleteScheduledTask(t.id); load(); } catch { /* ignore */ } }} />
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="automation-actions">
          <button className="automation-btn-primary" onClick={() => { setEditing(null); setInitial(null); setShowForm(true); }}>
            新建自动化
          </button>
        </div>
      </div>

      <div className="automation-wake">
        <IconInfo size={14} />
        <span className="automation-wake-text">chatcoder 运行会话时保持电脑唤醒。</span>
        <SwitchRow checked={keepAwake} onChange={setKeepAwake} />
      </div>

      <div className="automation-section">闲时任务模板</div>
      <div className="automation-grid">
        {IDLE_TEMPLATES.map((t) => (
          <button key={t.name} className="automation-tpl" onClick={() => applyTemplate(t)}>
            <span className="automation-tpl-head">{t.icon} {t.name}</span>
            <span className="automation-tpl-desc">{t.desc}</span>
            <span className="automation-tpl-sched">{t.schedule}</span>
          </button>
        ))}
      </div>

      <div className="automation-section">自动化模板</div>
      <div className="automation-grid">
        {CRON_TEMPLATES.map((t, i) => (
          <button key={`${t.name}-${i}`} className="automation-tpl" onClick={() => applyTemplate(t)}>
            <span className="automation-tpl-head">{t.icon} {t.name}</span>
            <span className="automation-tpl-desc">{t.desc}</span>
            <span className="automation-tpl-sched">{t.schedule}</span>
          </button>
        ))}
      </div>

      <ScheduledTaskFormModal
        open={showForm}
        editing={editing}
        initial={initial}
        onClose={() => { setShowForm(false); setEditing(null); setInitial(null); }}
        onSaved={() => void load()}
      />
    </div>
  );
}
