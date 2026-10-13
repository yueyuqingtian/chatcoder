/** 设置 → 工作流：概览、运行与执行、并发上限（全局 / 单会话，均可设为「不限」）、单子工作流并行步骤、
 * 返工与 token 预算、并行写入的热点文件、模板管理。
 *
 * 修改即保存（AGENTS §2）：开关与分段即时写入；滑块与输入防抖 300ms 后写入；
 * 卸载时把尚未发出的修改补发出去，避免输入后马上离开页面而丢失。
 * 上限由服务端每拍读取，保存后下一拍即生效。
 */
import { useEffect, useRef, useState } from "react";
import { api, type GlobalSettingsOut, type WorkflowTemplateOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { useWorkflowStore } from "../../store/workflow";
import { ConfirmDialog } from "../ConfirmDialog";
import { Button, CardRow, Input, List, ListRow, Section, Segmented, StepSlider, Switch, Tag, Textarea } from "../ui";
import { WorkflowTemplateEditor } from "./WorkflowTemplateEditor";

/** 0 = 不限 */
const GLOBAL_STEPS = [1, 2, 3, 4, 6, 8, 12, 16, 0];
const SESSION_STEPS = [1, 2, 3, 4, 6, 8, 0];
const FLOW_STEPS = [1, 2, 3, 4];
const REWORK_STEPS = [0, 1, 2, 3];

type Composition = "runtime" | "presets_only";
type ConflictPolicy = "queue" | "ask";
type Isolation = "shared" | "worktree";

interface WorkflowConfig {
  workflow_enabled: boolean;
  workflow_max_concurrent_global: number;
  workflow_max_concurrent_per_session: number;
  workflow_max_parallel_nodes_per_flow: number;
  workflow_default_max_rework: number;
  workflow_token_budget_per_run: number;
  workflow_confirm_before_start: boolean;
  workflow_auto_resume_on_network: boolean;
  workflow_composition_mode: Composition;
  workflow_hot_files: string;
  workflow_require_checks: boolean;
  workflow_auto_merge: boolean;
  workflow_conflict_policy: ConflictPolicy;
  workflow_isolation_default: Isolation;
}

/** 与服务端 config 默认值一致（保守取值） */
const DEFAULTS: WorkflowConfig = {
  workflow_enabled: true,
  workflow_max_concurrent_global: 3,
  workflow_max_concurrent_per_session: 2,
  workflow_max_parallel_nodes_per_flow: 2,
  workflow_default_max_rework: 2,
  workflow_token_budget_per_run: 0,
  workflow_confirm_before_start: true,
  workflow_auto_resume_on_network: false,
  workflow_composition_mode: "runtime",
  workflow_hot_files: "",
  workflow_require_checks: true,
  workflow_auto_merge: true,
  workflow_conflict_policy: "queue",
  workflow_isolation_default: "shared",
};

function pickConfig(g: GlobalSettingsOut): WorkflowConfig {
  return {
    workflow_enabled: g.workflow_enabled ?? DEFAULTS.workflow_enabled,
    workflow_max_concurrent_global: g.workflow_max_concurrent_global ?? DEFAULTS.workflow_max_concurrent_global,
    workflow_max_concurrent_per_session: g.workflow_max_concurrent_per_session ?? DEFAULTS.workflow_max_concurrent_per_session,
    workflow_max_parallel_nodes_per_flow: g.workflow_max_parallel_nodes_per_flow ?? DEFAULTS.workflow_max_parallel_nodes_per_flow,
    workflow_default_max_rework: g.workflow_default_max_rework ?? DEFAULTS.workflow_default_max_rework,
    workflow_token_budget_per_run: g.workflow_token_budget_per_run ?? DEFAULTS.workflow_token_budget_per_run,
    workflow_confirm_before_start: g.workflow_confirm_before_start ?? DEFAULTS.workflow_confirm_before_start,
    workflow_auto_resume_on_network: g.workflow_auto_resume_on_network ?? DEFAULTS.workflow_auto_resume_on_network,
    workflow_composition_mode: g.workflow_composition_mode ?? DEFAULTS.workflow_composition_mode,
    workflow_hot_files: g.workflow_hot_files ?? DEFAULTS.workflow_hot_files,
    workflow_require_checks: g.workflow_require_checks ?? DEFAULTS.workflow_require_checks,
    workflow_auto_merge: g.workflow_auto_merge ?? DEFAULTS.workflow_auto_merge,
    workflow_conflict_policy: g.workflow_conflict_policy ?? DEFAULTS.workflow_conflict_policy,
    workflow_isolation_default: g.workflow_isolation_default ?? DEFAULTS.workflow_isolation_default,
  };
}

const stepLabel = (v: number) => (v === 0 ? "不限" : String(v));

/** 滑块档位：精确命中取该档；否则取最接近的有限档（API 直接写入的非档位值） */
function stepIndex(steps: number[], value: number): number {
  const exact = steps.indexOf(value);
  if (exact >= 0) return exact;
  let best = 0;
  let bestDist = Infinity;
  steps.forEach((s, i) => {
    if (s === 0) return;
    const dist = Math.abs(s - value);
    if (dist < bestDist) {
      best = i;
      bestDist = dist;
    }
  });
  return best;
}

const toStepOptions = (steps: number[]) => steps.map((v) => ({ key: String(v), label: stepLabel(v) }));

function stepCount(plan: Record<string, unknown>): number {
  const flows = (plan.flows ?? []) as Array<{ nodes?: unknown[] }>;
  return flows.reduce((n, flow) => n + (flow.nodes?.length ?? 0), 0);
}

function Overview() {
  const pool = useWorkflowStore((s) => s.pool);
  const loadPool = useWorkflowStore((s) => s.loadPool);
  useEffect(() => {
    void loadPool();
    const timer = window.setInterval(() => void loadPool(), 5000);
    return () => window.clearInterval(timer);
  }, [loadPool]);
  if (!pool) return null;
  const inUse = pool.global.in_use;
  const limit = pool.global.limit;
  const programs = pool.programs ?? { running: 0, paused: 0 };
  return (
    <div className="wf-overview" aria-label="工作流概览">
      <div className="wf-stat">
        <span className="wf-stat-value">{programs.running}</span>
        <span className="wf-stat-label">运行中的工作流</span>
      </div>
      <div className="wf-stat">
        <span className="wf-stat-value">
          {inUse}
          <span className="wf-stat-unit">{limit > 0 ? ` / ${limit}` : " / 不限"}</span>
        </span>
        <span className="wf-stat-label">占用并发</span>
      </div>
      <div className="wf-stat">
        <span className="wf-stat-value">{pool.queued}</span>
        <span className="wf-stat-label">排队节点</span>
      </div>
      <div className="wf-stat">
        <span className="wf-stat-value">{programs.paused}</span>
        <span className="wf-stat-label">已暂停的工作流</span>
      </div>
    </div>
  );
}

const TEMPLATE_DESC = "内置预设只读，可复制后修改。运行结束后可在工作流面板中把计划存为我的模板，之后新的工作流可直接复用。";

function TemplateList() {
  const templates = useWorkflowStore((s) => s.templates);
  const loadTemplates = useWorkflowStore((s) => s.loadTemplates);
  const copyPreset = useWorkflowStore((s) => s.copyPreset);
  const patchTemplate = useWorkflowStore((s) => s.patchTemplate);
  const deleteTemplate = useWorkflowStore((s) => s.deleteTemplate);
  const createTemplate = useWorkflowStore((s) => s.createTemplate);
  const [pendingDelete, setPendingDelete] = useState<WorkflowTemplateOut | null>(null);
  const [editing, setEditing] = useState<WorkflowTemplateOut | null>(null);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    void loadTemplates();
  }, [loadTemplates]);

  if (templates === null) {
    return (
      <Section title="模板" desc={TEMPLATE_DESC}>
        <div className="wf-settings-hint">正在读取模板…</div>
      </Section>
    );
  }
  return (
    <Section
      title="模板"
      desc={TEMPLATE_DESC}
      actions={(
        <Button
          variant="secondary"
          size="xs"
          disabled={creating}
          onClick={async () => {
            setCreating(true);
            const created = await createTemplate(`我的模板 ${templates.filter((t) => t.source === "user").length + 1}`);
            setCreating(false);
            if (created) setEditing(created);
          }}
        >
          新建模板
        </Button>
      )}
    >
      <List>
        {templates.map((tpl) => (
          <ListRow
            key={tpl.key}
            muted={!tpl.enabled}
            name={tpl.name}
            desc={tpl.description || "无说明"}
            meta={
              <>
                <Tag>{tpl.source === "preset" ? "内置预设" : "我的模板"}</Tag>
                <Tag>{stepCount(tpl.plan)} 个步骤</Tag>
                {!tpl.enabled && <Tag>已停用</Tag>}
              </>
            }
            actions={
              tpl.source === "preset" ? (
                <Button variant="secondary" size="xs" onClick={() => void copyPreset(tpl.key)}>
                  复制为我的模板
                </Button>
              ) : (
                <>
                  <Button variant="secondary" size="xs" onClick={() => setEditing(tpl)}>
                    编辑
                  </Button>
                  <Switch checked={tpl.enabled} onChange={(v) => void patchTemplate(tpl.key, { enabled: v })} />
                  <Button variant="danger-ghost" size="xs" onClick={() => setPendingDelete(tpl)}>
                    删除
                  </Button>
                </>
              )
            }
          />
        ))}
      </List>
      {editing && <WorkflowTemplateEditor template={editing} onClose={() => setEditing(null)} />}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除模板"
        message={pendingDelete ? `删除模板「${pendingDelete.name}」？已用它规划过的工作流不受影响。` : ""}
        confirmLabel="删除"
        danger
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          const target = pendingDelete;
          setPendingDelete(null);
          if (target) void deleteTemplate(target.key);
        }}
      />
    </Section>
  );
}

export function WorkflowSettingsPanel() {
  const [cfg, setCfg] = useState<WorkflowConfig>(DEFAULTS);
  const [budgetText, setBudgetText] = useState("0");
  // 读取未完成或失败时不展示控件：避免把默认值误当成用户的真实配置
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [attempt, setAttempt] = useState(0);
  const pendingRef = useRef<Record<string, { timer: number; patch: Partial<WorkflowConfig> }>>({});

  useEffect(() => {
    let alive = true;
    setStatus("loading");
    void api.getGlobalSettings()
      .then((g) => {
        if (!alive) return;
        const next = pickConfig(g);
        setCfg(next);
        setBudgetText(String(next.workflow_token_budget_per_run));
        setStatus("ready");
      })
      .catch(() => {
        if (alive) setStatus("error");
      });
    return () => {
      alive = false;
    };
  }, [attempt]);

  useEffect(() => () => {
    // 卸载时补发尚未发出的防抖修改
    Object.values(pendingRef.current).forEach(({ timer, patch }) => {
      window.clearTimeout(timer);
      void api.setGlobalSettings(patch).catch(() => {
        /* 卸载后无法提示，下次进入页面时以服务端读取的值为准 */
      });
    });
    pendingRef.current = {};
  }, []);

  const send = (patch: Partial<WorkflowConfig>) => {
    void api.setGlobalSettings(patch).catch((e) => {
      useChatStore.setState({ error: `保存工作流设置失败：${String(e)}` });
    });
  };

  const update = (patch: Partial<WorkflowConfig>, debounceKey?: string) => {
    setCfg((prev) => ({ ...prev, ...patch }));
    if (!debounceKey) {
      send(patch);
      return;
    }
    const prev = pendingRef.current[debounceKey];
    if (prev) window.clearTimeout(prev.timer);
    const timer = window.setTimeout(() => {
      delete pendingRef.current[debounceKey];
      send(patch);
    }, 300);
    pendingRef.current[debounceKey] = { timer, patch };
  };

  if (status === "loading") return <div className="wf-settings-hint">正在读取设置…</div>;
  if (status === "error") {
    return (
      <div className="wf-settings-hint is-error" role="alert">
        <span>无法读取工作流设置，请确认后端服务已启动。</span>
        <Button variant="secondary" size="xs" onClick={() => setAttempt((n) => n + 1)}>重试</Button>
      </div>
    );
  }

  const unlimited = cfg.workflow_max_concurrent_global === 0 || cfg.workflow_max_concurrent_per_session === 0;

  return (
    <>
      <Overview />

      <Section title="运行">
        <CardRow
          title="启用工作流"
          desc="关闭后主代理不再规划新的工作流；已有运行仍可查看、继续或放弃。"
        >
          <Switch checked={cfg.workflow_enabled} onChange={(v) => update({ workflow_enabled: v })} />
        </CardRow>
        <CardRow
          title="开始前确认计划"
          desc="开启时，主代理给出的计划需要你确认后才开始；关闭则规划完成后直接执行。"
        >
          <Switch
            checked={cfg.workflow_confirm_before_start}
            onChange={(v) => update({ workflow_confirm_before_start: v })}
          />
        </CardRow>
        <CardRow
          title="网络恢复后自动续跑"
          desc="网络中断暂停后，约一分钟后自动续跑一次；若工作区有外部改动，仍会先请你确认。"
        >
          <Switch
            checked={cfg.workflow_auto_resume_on_network}
            onChange={(v) => update({ workflow_auto_resume_on_network: v })}
          />
        </CardRow>
        <CardRow
          title="网络瞬时故障的重试"
          desc="与子智能体共用同一设置：失败后按间隔自动重试，耗尽后暂停、保留进度，可继续。"
        >
          <Button
            variant="secondary"
            size="xs"
            onClick={() => window.dispatchEvent(new CustomEvent("chatcoder:open-settings-tab", { detail: { tab: "subagents" } }))}
          >
            前往子智能体设置
          </Button>
        </CardRow>
        <CardRow
          title="组装方式"
          desc="自主组装：主代理按任务设计步骤；仅预设：只能基于内置预设或我的模板规划。"
        >
          <Segmented<Composition>
            size="sm"
            aria-label="组装方式"
            value={cfg.workflow_composition_mode}
            options={[
              { value: "runtime", label: "自主组装" },
              { value: "presets_only", label: "仅预设" },
            ]}
            onChange={(v) => update({ workflow_composition_mode: v })}
          />
        </CardRow>
      </Section>

      <Section
        title="并发上限"
        desc="所有运行中的工作流共享这些上限。超出的步骤排队等待，不会丢弃；与普通子代理的上限分开计算。"
      >
        <CardRow title="全局并发" desc="所有会话的工作流子代理合计">
          <StepSlider
            className="wf-step-slider"
            aria-label="全局并发"
            steps={toStepOptions(GLOBAL_STEPS)}
            index={stepIndex(GLOBAL_STEPS, cfg.workflow_max_concurrent_global)}
            onIndexChange={(i) => update({ workflow_max_concurrent_global: GLOBAL_STEPS[i] }, "global")}
          />
        </CardRow>
        <CardRow title="单会话并发" desc="同一会话内的工作流子代理合计，防止一个会话占满全局">
          <StepSlider
            className="wf-step-slider"
            aria-label="单会话并发"
            steps={toStepOptions(SESSION_STEPS)}
            index={stepIndex(SESSION_STEPS, cfg.workflow_max_concurrent_per_session)}
            onIndexChange={(i) => update({ workflow_max_concurrent_per_session: SESSION_STEPS[i] }, "session")}
          />
        </CardRow>
        <CardRow title="单个子工作流的并行步骤" desc="同一子工作流内可同时运行的步骤数">
          <Segmented<string>
            size="sm"
            aria-label="单个子工作流的并行步骤"
            value={String(cfg.workflow_max_parallel_nodes_per_flow)}
            options={FLOW_STEPS.map((v) => ({ value: String(v), label: String(v) }))}
            onChange={(v) => update({ workflow_max_parallel_nodes_per_flow: Number(v) })}
          />
        </CardRow>
        {unlimited && (
          <p className="wf-settings-warn">
            选择「不限」后同时运行的子代理数量没有上限，token 与本机资源占用可能明显升高。
          </p>
        )}
      </Section>

      <Section title="质量与预算">
        <CardRow title="默认返工上限" desc="评估未通过时，实现步骤最多被打回的次数；超出后交给你决定">
          <Segmented<string>
            size="sm"
            aria-label="默认返工上限"
            value={String(cfg.workflow_default_max_rework)}
            options={REWORK_STEPS.map((v) => ({ value: String(v), label: String(v) }))}
            onChange={(v) => update({ workflow_default_max_rework: Number(v) })}
          />
        </CardRow>
        <CardRow
          title="检查必须通过"
          desc="开启时，测试、类型检查等确定性检查失败不能判定为通过，会打回修改；关闭则只记录检查结果。"
        >
          <Switch
            checked={cfg.workflow_require_checks}
            onChange={(v) => update({ workflow_require_checks: v })}
          />
        </CardRow>
        <CardRow title="单次运行 token 预算" desc="超出后运行暂停，调高预算即可继续；0 表示不限">
          <Input
            type="number"
            min={0}
            step={10000}
            aria-label="单次运行 token 预算"
            value={budgetText}
            onChange={(e) => {
              const raw = e.target.value;
              setBudgetText(raw);
              const n = Math.max(0, Math.floor(Number(raw) || 0));
              update({ workflow_token_budget_per_run: n }, "budget");
            }}
          />
        </CardRow>
      </Section>

      <Section title="并行写入" desc="多个步骤可能同时改动同一个文件。先尽量让它们错开，错不开时再尝试合并，合并不了才请你决定。">
        <CardRow
          title="自动合并不重叠的改动"
          desc="两边改的是同一文件的不同部分时，直接合并，不打扰你。"
        >
          <Switch
            checked={cfg.workflow_auto_merge}
            onChange={(v) => update({ workflow_auto_merge: v })}
          />
        </CardRow>
        <CardRow
          title="同一处的真冲突"
          desc="先让步骤基于最新内容重做，多次仍失败再交给你；选择「每次都问我」则每次都由你决定。"
        >
          <Segmented<ConflictPolicy>
            size="sm"
            aria-label="同一处的真冲突"
            value={cfg.workflow_conflict_policy}
            options={[
              { value: "queue", label: "先自动重做" },
              { value: "ask", label: "每次都问我" },
            ]}
            onChange={(v) => update({ workflow_conflict_policy: v })}
          />
        </CardRow>
        <CardRow
          title="默认隔离方式"
          desc="新的子工作流默认在共享工作区执行。选择「独立工作树」后，子工作流在自己的 git 分支中执行，结束时并入主工作区（需要 git 仓库）。"
        >
          <Segmented<Isolation>
            size="sm"
            aria-label="默认隔离方式"
            value={cfg.workflow_isolation_default}
            options={[
              { value: "shared", label: "共享工作区" },
              { value: "worktree", label: "独立工作树" },
            ]}
            onChange={(v) => update({ workflow_isolation_default: v })}
          />
        </CardRow>
        <div className="wf-hot-files">
          <Textarea
            rows={3}
            aria-label="热点文件"
            placeholder="每行或逗号分隔一个路径，例如 package.json、CHANGELOG.md"
            value={cfg.workflow_hot_files}
            onChange={(e) => update({ workflow_hot_files: e.target.value }, "hot")}
          />
        </div>
      </Section>

      <TemplateList />
    </>
  );
}
