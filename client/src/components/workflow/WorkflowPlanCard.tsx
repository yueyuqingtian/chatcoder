/** 草稿计划的正文（设计 §7.7）：子工作流与角色链、写入范围、依赖、预计并发，
 * 以及在草稿中把某个子工作流的模板换成另一个模板（A → B）。决定按钮由 WorkflowActions 提供。
 */
import { useEffect } from "react";
import type { WorkflowProgramOut } from "../../api/client";
import { useWorkflowStore } from "../../store/workflow";
import { Select, type SelectOption } from "../ui";
import {
  dependencyLabel,
  estimatedConcurrency,
  roleChain,
  scopeSummary,
} from "./workflowModel";

/** 模板选项的值：模板键与子工作流标识，用一个不会出现在名称里的分隔符连接 */
const SEP = "\u001f";
const INTEGRATION = "integration";

export function WorkflowPlanBody({ snap }: { snap: WorkflowProgramOut }) {
  const templates = useWorkflowStore((s) => s.templates);
  const loadTemplates = useWorkflowStore((s) => s.loadTemplates);
  const swapTemplate = useWorkflowStore((s) => s.swapTemplate);
  const planErrors = useWorkflowStore((s) => s.planErrors[snap.run.id] ?? []);
  const busy = useWorkflowStore((s) => !!s.busy[snap.run.id]);

  useEffect(() => {
    if (templates === null) void loadTemplates();
  }, [templates, loadTemplates]);

  const options: SelectOption[] = [];
  for (const template of templates ?? []) {
    if (!template.enabled) continue;
    const flows = (template.plan?.flows ?? []) as Array<{ key: string; title?: string }>;
    for (const flow of flows) {
      options.push({
        value: `${template.key}${SEP}${flow.key}`,
        label: `${template.name} · ${flow.title || flow.key}`,
      });
    }
  }

  const concurrency = estimatedConcurrency(snap);

  return (
    <div className="wf-plan">
      <p className="wf-plan-explain">将按顺序执行，每步由子代理完成，可随时暂停。</p>
      <ol className="wf-plan-flows" aria-label="子工作流">
        {snap.flows.map((flow) => {
          const dep = dependencyLabel(snap, flow.run.package_key);
          const isIntegration = flow.run.package_key === INTEGRATION;
          return (
            <li key={flow.run.id} className="wf-plan-flow">
              <div className="wf-plan-flow-head">
                <span className="wf-plan-flow-name">{flow.run.title || flow.run.package_key}</span>
                {flow.run.isolation === "worktree" && <span className="wf-card-flow-tag">独立工作树</span>}
                {dep && <span className="wf-card-flow-dep">{dep}</span>}
              </div>
              <div className="wf-plan-flow-chain">{roleChain(flow.nodes)}</div>
              <div className="wf-plan-flow-scope">{scopeSummary(flow.nodes)}</div>
              {isIntegration ? (
                <div className="wf-plan-flow-note">集成验收由程序级验收生成，不能替换模板</div>
              ) : (
                <div className="wf-plan-flow-swap">
                  <Select
                    value=""
                    placeholder="换用其它模板…"
                    options={options}
                    disabled={busy || options.length === 0}
                    aria-label={`替换「${flow.run.title || flow.run.package_key}」的模板`}
                    onChange={(value) => {
                      const [template, flowKey] = value.split(SEP);
                      if (template && flowKey) {
                        void swapTemplate(snap.run.id, flow.run.package_key ?? "", template, flowKey);
                      }
                    }}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ol>
      <div className="wf-plan-meta">
        预计并发：最多 {concurrency} 个子工作流同时执行
        <span className="wf-plan-meta-sep" aria-hidden>·</span>
        共 {snap.flows.length} 个子工作流
      </div>
      {planErrors.length > 0 && (
        <ul className="wf-plan-errors" role="alert" aria-label="换模板未通过校验">
          {planErrors.map((item, i) => (
            <li key={i} className="wf-plan-error">
              <span className="wf-plan-error-text">{item.message}</span>
              {item.suggestion && <span className="wf-plan-error-fix">建议：{item.suggestion}</span>}
            </li>
          ))}
        </ul>
      )}
      {planErrors.length === 0 && busy && (
        <span className="wf-plan-hint" aria-live="polite">正在更新计划…</span>
      )}
    </div>
  );
}
