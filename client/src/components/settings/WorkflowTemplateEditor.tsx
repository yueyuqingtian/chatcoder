/** 用户模板编辑器：增删、调整步骤，逐个修改步骤的标题、执行角色、输入来源、验收标准、写入范围、
 * 检查命令与返工设置；顶部给出迷你流程图。保存前由服务端按计划的同一套规则校验：
 * 错误标注到对应字段上，无法定位到字段的错误列在底部。
 * 子工作流的结构（名称、依赖、隔离方式）决定依赖与权限，依赖在子工作流的依赖区中设置，其余结构改动需要重新规划。
 */
import { useEffect, useState } from "react";
import { ApiError, api, type SubagentProfileOut, type WorkflowTemplateOut } from "../../api/client";
import { useWorkflowStore } from "../../store/workflow";
import { Button, FormDialog, Input, Segmented, Select, Textarea, Tooltip, type SelectOption } from "../ui";
import { ConfirmDialog } from "../ConfirmDialog";
import { KIND_LABEL, KIND_SHORT_LABEL } from "../workflow/workflowModel";

interface PlanNode {
  key: string;
  title?: string;
  kind: string;
  role?: string | null;
  task?: string;
  inputs?: string[];
  acceptance?: string[];
  write_scope?: string[];
  commands?: string[];
  rework_to?: string | null;
  max_rework?: number | null;
  question?: string | null;
}

interface PlanFlow {
  key: string;
  title?: string;
  depends_on?: string[];
  isolation?: string;
  nodes: PlanNode[];
}

interface TemplatePlan {
  title?: string;
  goal?: string;
  flows: PlanFlow[];
  [extra: string]: unknown;
}

interface Diagnostic {
  code: string;
  path: string;
  message: string;
  suggestion?: string | null;
}

interface Position {
  f: number;
  n: number;
}

/** 可新增的步骤类型（迁移到模板时不包含运行时专用的并入与合并步骤） */
const NEW_KINDS = ["explore", "execute", "review", "check"] as const;
const DEFAULT_TITLE: Record<string, string> = { explore: "探索", execute: "实现", review: "评估", check: "检查" };
const DEFAULT_ROLE: Record<string, string> = { explore: "explorer", execute: "implementer", review: "reviewer" };
const REWORK_OPTIONS = ["0", "1", "2", "3"];
/** 校验错误里能标注到字段上的字段名；其余（如整个子工作流的结构问题）列在底部 */
const INLINE_FIELDS = new Set(["role", "inputs", "write_scope", "acceptance", "commands", "question", "rework_to", "max_rework"]);
const BUILTIN_ROLES: SelectOption[] = [
  { value: "explorer", label: "探索代理（只读）" },
  { value: "implementer", label: "实现代理" },
  { value: "reviewer", label: "评估代理（只读）" },
  { value: "general", label: "通用代理" },
];

/** 错误路径 flows[i].nodes[j] → 步骤位置；无法定位到步骤的错误返回 null */
function locate(path: string): Position | null {
  const match = /flows\[(\d+)\]\.nodes\[(\d+)\]/.exec(path);
  return match ? { f: Number(match[1]), n: Number(match[2]) } : null;
}

/** 错误路径里步骤的字段名，如 flows[0].nodes[1].acceptance → acceptance */
function fieldOf(path: string): string | null {
  const match = /nodes\[\d+\]\.(\w+)/.exec(path);
  return match ? match[1] : null;
}

/** 文本框内容按行拆分（保留末尾空行，便于连续输入）；保存时再去掉空行 */
const toLines = (text: string): string[] => text.split("\n");
const toText = (items?: string[]): string => (items ?? []).join("\n");

function cleanPlan(plan: TemplatePlan): TemplatePlan {
  const clean = (items?: string[]) => (items ?? []).map((s) => s.trim()).filter(Boolean);
  return {
    ...plan,
    flows: plan.flows.map((flow) => ({
      ...flow,
      nodes: flow.nodes.map((node) => ({
        ...node,
        acceptance: clean(node.acceptance),
        write_scope: clean(node.write_scope),
        commands: clean(node.commands),
        inputs: clean(node.inputs),
      })),
    })),
  };
}

/** 新步骤的键：同一子工作流内不重复的 step-N */
function freshKey(nodes: PlanNode[]): string {
  const taken = new Set(nodes.map((n) => n.key));
  let i = 1;
  while (taken.has(`step-${i}`)) i += 1;
  return `step-${i}`;
}

/** 新子工作流的键：不与已有子工作流重复 */
function freshFlowKey(flows: PlanFlow[]): string {
  const taken = new Set(flows.map((item) => item.key));
  let n = flows.length + 1;
  while (taken.has(`flow-${n}`)) n += 1;
  return `flow-${n}`;
}

/** 新子工作流的标题：编号顺延，避免与已有标题重复 */
function freshFlowTitle(flows: PlanFlow[]): string {
  const taken = new Set(flows.map((item) => item.title || item.key));
  let n = flows.length + 1;
  while (taken.has(`子工作流 ${n}`)) n += 1;
  return `子工作流 ${n}`;
}

/** 字段下方的校验错误（只在字段旁边显示，不重复出现在底部） */
function FieldErrors({ items }: { items: Diagnostic[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="wf-tpl-field-errors" role="alert">
      {items.map((d, i) => (
        <li key={`${d.code}-${i}`}>
          {d.message}
          {d.suggestion && <small> 建议：{d.suggestion}</small>}
        </li>
      ))}
    </ul>
  );
}

/** 顶部迷你流程图：当前子工作流的步骤顺序、输入关系与返工回路 */
function MiniFlow({ nodes, selected, onSelect }: { nodes: PlanNode[]; selected: number; onSelect: (n: number) => void }) {
  const titleOf = (key: string) => nodes.find((n) => n.key === key)?.title || key;
  return (
    <ol className="wf-tpl-mini" aria-label="流程预览">
      {nodes.map((node, i) => (
        <li key={`${node.key}-${i}`} className="wf-tpl-mini-item">
          {i > 0 && <span className="wf-tpl-mini-arrow" aria-hidden>→</span>}
          <button
            type="button"
            className="wf-tpl-mini-step"
            data-kind={node.kind}
            aria-current={i === selected ? "true" : undefined}
            onClick={() => onSelect(i)}
          >
            <span className="wf-tpl-mini-kind">{KIND_SHORT_LABEL[node.kind] ?? node.kind}</span>
            <span className="wf-tpl-mini-name">{node.title || node.key}</span>
          </button>
          {node.rework_to && (
            <Tooltip content={`返工回到「${titleOf(node.rework_to)}」`} side="top" delay={300}>
              <span className="wf-tpl-mini-rework">↺ {titleOf(node.rework_to)}</span>
            </Tooltip>
          )}
        </li>
      ))}
    </ol>
  );
}

export function WorkflowTemplateEditor({
  template,
  onClose,
}: {
  template: WorkflowTemplateOut;
  onClose: () => void;
}) {
  const loadTemplates = useWorkflowStore((s) => s.loadTemplates);
  const [plan, setPlan] = useState<TemplatePlan>(
    () => JSON.parse(JSON.stringify(template.plan ?? { flows: [] })) as TemplatePlan,
  );
  const [pos, setPos] = useState<Position>({ f: 0, n: 0 });
  const [flowToRemove, setFlowToRemove] = useState<number | null>(null);
  const [diags, setDiags] = useState<Diagnostic[]>([]);
  const [saving, setSaving] = useState(false);
  const [newKind, setNewKind] = useState<string>("execute");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [profiles, setProfiles] = useState<SubagentProfileOut[]>([]);

  useEffect(() => {
    let alive = true;
    void api.listSubagents()
      .then((rows) => { if (alive) setProfiles(rows); })
      .catch(() => { /* 角色列表读取失败时只保留内置角色 */ });
    return () => { alive = false; };
  }, []);

  const flows = plan.flows ?? [];
  const flow = flows[pos.f] ?? null;
  const node = flow?.nodes[pos.n] ?? null;
  const nodes = flow?.nodes ?? [];

  const errorCount = (f: number, n: number) =>
    diags.filter((d) => {
      const at = locate(d.path);
      return at?.f === f && at.n === n;
    }).length;
  /** 当前步骤某个字段上的错误 */
  const fieldDiags = (field: string) =>
    diags.filter((d) => {
      const at = locate(d.path);
      return at?.f === pos.f && at.n === pos.n && fieldOf(d.path) === field;
    });
  /** 当前步骤的错误中，没有标注到字段上的部分（如步骤整体的问题） */
  const nodeLevelDiags = diags.filter((d) => {
    const at = locate(d.path);
    return at?.f === pos.f && at.n === pos.n && !INLINE_FIELDS.has(fieldOf(d.path) ?? "");
  });
  /** 底部列表：无法定位到步骤的错误（字段级错误已在字段旁边显示） */
  const unlocated = diags.filter((d) => locate(d.path) === null);

  const roleOptions: SelectOption[] = [
    ...BUILTIN_ROLES,
    ...profiles
      .filter((p) => (p as { is_active?: boolean }).is_active !== false)
      .map((p) => ({ value: p.name, label: p.name })),
  ];

  /** 对计划做一次修改（深拷贝后交给 mutate，避免直接改动旧状态） */
  const edit = (mutate: (next: TemplatePlan) => void) => {
    setPlan((prev) => {
      const next = JSON.parse(JSON.stringify(prev)) as TemplatePlan;
      mutate(next);
      return next;
    });
  };

  const patchNode = (fields: Partial<PlanNode>) =>
    edit((next) => {
      const target = next.flows[pos.f]?.nodes[pos.n];
      if (target) Object.assign(target, fields);
    });

  const toggleInput = (key: string) =>
    edit((next) => {
      const target = next.flows[pos.f]?.nodes[pos.n];
      if (!target) return;
      const current = target.inputs ?? [];
      target.inputs = current.includes(key) ? current.filter((item) => item !== key) : [...current, key];
    });

  const addStep = () => {
    const at = pos.n + 1;
    edit((next) => {
      const list = next.flows[pos.f]?.nodes;
      if (!list) return;
      const previous = list[pos.n];
      const kind = newKind;
      // 同一子工作流内标题不重名：已有「实现」时新步骤顺延为「实现 2」，避免两个步骤无法区分
      const base = DEFAULT_TITLE[kind] ?? kind;
      const taken = new Set(list.map((item) => item.title));
      let title = base;
      for (let n = 2; taken.has(title); n += 1) title = `${base} ${n}`;
      const created: PlanNode = {
        key: freshKey(list),
        title,
        kind,
        role: null,
        task: "",
        inputs: previous ? [previous.key] : [],
        acceptance: [],
        write_scope: [],
        commands: [],
        rework_to: null,
        max_rework: kind === "review" ? 2 : null,
        question: null,
      };
      if (kind === "review") {
        const execute = [...list].reverse().find((item) => item.kind === "execute");
        created.rework_to = execute?.key ?? null;
      }
      list.splice(at, 0, created);
    });
    setPos({ f: pos.f, n: at });
  };

  const removeStep = () => {
    const key = node?.key;
    setConfirmRemove(false);
    if (!key) return;
    edit((next) => {
      const list = next.flows[pos.f]?.nodes;
      if (!list) return;
      list.splice(pos.n, 1);
      // 其它步骤对被删步骤的引用一并清理，避免留下悬空的输入与返工目标
      for (const item of list) {
        item.inputs = (item.inputs ?? []).filter((input) => input !== key);
        if (item.rework_to === key) item.rework_to = null;
      }
    });
    setPos((prev) => ({ f: prev.f, n: Math.max(0, Math.min(prev.n, (flows[prev.f]?.nodes.length ?? 1) - 2)) }));
  };

  const moveStep = (delta: -1 | 1) => {
    const target = pos.n + delta;
    edit((next) => {
      const list = next.flows[pos.f]?.nodes;
      if (!list || target < 0 || target >= list.length) return;
      [list[pos.n], list[target]] = [list[target], list[pos.n]];
    });
    setPos({ f: pos.f, n: target });
  };

  /** 新增子工作流：以一个探索步骤作为起点，并定位到它；保存前按与计划相同的规则校验 */
  const addFlow = () => {
    const index = flows.length;
    edit((next) => {
      next.flows.push({
        key: freshFlowKey(next.flows),
        title: freshFlowTitle(next.flows),
        depends_on: [],
        nodes: [
          {
            key: "explore",
            title: DEFAULT_TITLE.explore,
            kind: "explore",
            role: null,
            task: "",
            inputs: [],
            acceptance: [],
            write_scope: [],
            commands: [],
            rework_to: null,
            max_rework: null,
            question: null,
          },
        ],
      });
    });
    setPos({ f: index, n: 0 });
  };

  /** 删除子工作流：依赖它的其它子工作流同时去掉这项依赖；至少保留一个子工作流 */
  const removeFlow = (index: number) => {
    const removed = flows[index];
    if (!removed || flows.length <= 1) return;
    edit((next) => {
      next.flows.splice(index, 1);
      for (const other of next.flows) {
        if (other.depends_on?.includes(removed.key)) {
          other.depends_on = other.depends_on.filter((key) => key !== removed.key);
        }
      }
    });
    setPos({ f: Math.min(index, flows.length - 2), n: 0 });
  };

  /** 切换本子工作流对另一个子工作流的依赖；成环等问题由保存时的计划校验给出 */
  const toggleDependency = (key: string) => {
    edit((next) => {
      const target = next.flows[pos.f];
      if (!target) return;
      const current = target.depends_on ?? [];
      target.depends_on = current.includes(key)
        ? current.filter((item) => item !== key)
        : [...current, key];
    });
  };

  const save = async () => {
    setSaving(true);
    try {
      await api.patchWorkflowTemplate(template.key, { plan: cleanPlan(plan) as unknown as Record<string, unknown> });
      await loadTemplates();
      onClose();
    } catch (err) {
      const errors = err instanceof ApiError ? (err.detail as { errors?: Diagnostic[] } | undefined)?.errors : undefined;
      setDiags(
        errors && errors.length > 0
          ? errors
          : [{ code: "save_failed", path: "", message: err instanceof Error ? err.message : "保存失败" }],
      );
    } finally {
      setSaving(false);
    }
  };

  const reworkTargets = (flowIndex: number) =>
    (flows[flowIndex]?.nodes ?? [])
      .filter((item) => item.kind === "execute")
      .map((item) => ({ value: item.key, label: item.title || item.key }));

  const upstream = nodes.slice(0, pos.n);

  return (
    <FormDialog
      open
      onClose={onClose}
      title={`编辑模板：${template.name}`}
      subtitle="增删与调整步骤，修改标题、执行角色、输入来源、验收标准、写入范围、检查命令与返工设置。保存前按与计划相同的规则校验。"
      onSubmit={() => void save()}
      submitLabel={saving ? "保存中…" : "保存"}
      submitDisabled={saving || flows.length === 0}
      width={880}
    >
      {flows.length === 0 || !node || !flow ? (
        <div className="wf-settings-hint">这个模板没有子工作流，无法编辑。</div>
      ) : (
        <div className="wf-tpl">
          <nav className="wf-tpl-nav" aria-label="模板的步骤">
            {flows.map((item, fi) => (
              <div key={`${item.key}-${fi}`} className="wf-tpl-nav-group">
                <div className="wf-tpl-nav-flowrow">
                  <span className="wf-tpl-nav-flow">{item.title || item.key}</span>
                  {flows.length > 1 && (
                    <Button
                      variant="ghost"
                      size="xs"
                      aria-label={`删除子工作流 ${item.title || item.key}`}
                      onClick={() => setFlowToRemove(fi)}
                    >
                      删除
                    </Button>
                  )}
                </div>
                {item.nodes.map((step, ni) => {
                  const current = fi === pos.f && ni === pos.n;
                  const errors = errorCount(fi, ni);
                  return (
                    <div
                      key={`${step.key}-${ni}`}
                      role="button"
                      tabIndex={0}
                      aria-current={current ? "true" : undefined}
                      className="wf-tpl-nav-node"
                      onClick={() => setPos({ f: fi, n: ni })}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setPos({ f: fi, n: ni });
                        }
                      }}
                    >
                      <span className="wf-tpl-nav-title">{step.title || step.key}</span>
                      <span className="wf-tpl-nav-kind">{KIND_LABEL[step.kind] ?? step.kind}</span>
                      {errors > 0 && <span className="wf-tpl-nav-err">{errors}</span>}
                    </div>
                  );
                })}
              </div>
            ))}
            <Button variant="ghost" size="xs" className="wf-tpl-flow-add" onClick={addFlow}>
              新增子工作流
            </Button>
            <div className="wf-tpl-add" role="group" aria-label="在当前子工作流中添加步骤">
              <Segmented<string>
                size="sm"
                aria-label="新步骤类型"
                value={newKind}
                onChange={setNewKind}
                options={NEW_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] ?? k }))}
              />
              <Button variant="secondary" size="xs" onClick={addStep}>
                在此步骤后添加
              </Button>
            </div>
          </nav>

          <div className="wf-tpl-form">
            <div className="wf-tpl-field wf-tpl-flow-preview">
              <span className="wf-gate-field-label">「{flow.title || flow.key}」的流程</span>
              <MiniFlow nodes={nodes} selected={pos.n} onSelect={(n) => setPos({ f: pos.f, n })} />
            </div>

            <div className="wf-tpl-field wf-tpl-flowdeps">
              <span className="wf-gate-field-label">
                子工作流「{flow.title || flow.key}」依赖
              </span>
              {flows.length <= 1 ? (
                <span className="wf-tpl-hint">只有一个子工作流；新增后可以设置依赖关系</span>
              ) : (
                <div className="wf-tpl-dep-list" role="group" aria-label="依赖的子工作流">
                  {flows.map((other, oi) =>
                    oi === pos.f ? null : (
                      <button
                        key={`${other.key}-${oi}`}
                        type="button"
                        className="wf-tpl-dep"
                        aria-pressed={(flow.depends_on ?? []).includes(other.key)}
                        onClick={() => toggleDependency(other.key)}
                      >
                        {other.title || other.key}
                      </button>
                    ),
                  )}
                </div>
              )}
            </div>

            {nodeLevelDiags.length > 0 && <FieldErrors items={nodeLevelDiags} />}

            <label className="wf-tpl-field">
              <span className="wf-gate-field-label">步骤标题</span>
              <Input
                aria-label="步骤标题"
                placeholder={node.key}
                value={node.title ?? ""}
                onChange={(e) => patchNode({ title: e.target.value })}
              />
            </label>
            <div className="wf-tpl-field">
              <span className="wf-gate-field-label">步骤类型</span>
              <span className="wf-tpl-hint">
                {KIND_LABEL[node.kind] ?? node.kind}（类型决定权限与依赖，不能在模板中修改）
              </span>
            </div>

            {node.kind !== "check" && (
              <div className="wf-tpl-field">
                <span className="wf-gate-field-label">执行角色</span>
                <Select
                  aria-label="执行角色"
                  value={node.role ?? ""}
                  placeholder={`默认：${BUILTIN_ROLES.find((r) => r.value === DEFAULT_ROLE[node.kind])?.label ?? ""}`}
                  options={[
                    { value: "", label: `默认（${BUILTIN_ROLES.find((r) => r.value === DEFAULT_ROLE[node.kind])?.label ?? ""}）` },
                    ...roleOptions,
                  ]}
                  onChange={(value) => patchNode({ role: value || null })}
                />
                <FieldErrors items={fieldDiags("role")} />
              </div>
            )}

            {upstream.length > 0 && (
              <div className="wf-tpl-field">
                <span className="wf-gate-field-label">输入来源（读取哪些上游步骤的产出）</span>
                <div className="wf-tpl-dep-list" role="group" aria-label="输入来源">
                  {upstream.map((up) => (
                    <button
                      key={up.key}
                      type="button"
                      className="wf-tpl-dep"
                      aria-pressed={(node.inputs ?? []).includes(up.key)}
                      onClick={() => toggleInput(up.key)}
                    >
                      {up.title || up.key}
                    </button>
                  ))}
                </div>
                <FieldErrors items={fieldDiags("inputs")} />
              </div>
            )}

            {node.kind === "execute" && (
              <label className="wf-tpl-field">
                <span className="wf-gate-field-label">验收标准（每行一条）</span>
                <Textarea
                  aria-label="验收标准"
                  rows={3}
                  value={toText(node.acceptance)}
                  onChange={(e) => patchNode({ acceptance: toLines(e.target.value) })}
                />
                <FieldErrors items={fieldDiags("acceptance")} />
              </label>
            )}

            {node.kind === "execute" && (
              <label className="wf-tpl-field">
                <span className="wf-gate-field-label">写入范围（每行一个路径或通配符）</span>
                <Textarea
                  aria-label="写入范围"
                  rows={3}
                  value={toText(node.write_scope)}
                  onChange={(e) => patchNode({ write_scope: toLines(e.target.value) })}
                />
                <span className="wf-tpl-hint">步骤只能修改这里列出的文件；需要更多时在运行中申请扩展。</span>
                <FieldErrors items={fieldDiags("write_scope")} />
              </label>
            )}

            {(node.kind === "review" || node.kind === "check") && (
              <label className="wf-tpl-field">
                <span className="wf-gate-field-label">确定性检查命令（每行一条）</span>
                <Textarea
                  aria-label="检查命令"
                  rows={3}
                  value={toText(node.commands)}
                  onChange={(e) => patchNode({ commands: toLines(e.target.value) })}
                />
                <span className="wf-tpl-hint">检查失败时，评估不能判定为通过。</span>
                <FieldErrors items={fieldDiags("commands")} />
              </label>
            )}

            {node.kind === "review" && (
              <>
                <label className="wf-tpl-field">
                  <span className="wf-gate-field-label">验收标准（每行一条）</span>
                  <Textarea
                    aria-label="评估的验收标准"
                    rows={2}
                    value={toText(node.acceptance)}
                    onChange={(e) => patchNode({ acceptance: toLines(e.target.value) })}
                  />
                  <FieldErrors items={fieldDiags("acceptance")} />
                </label>
                <div className="wf-tpl-field">
                  <span className="wf-gate-field-label">未通过时返工回到</span>
                  <Select
                    aria-label="返工回到"
                    value={node.rework_to ?? ""}
                    placeholder="不返工，直接交给我决定"
                    options={reworkTargets(pos.f)}
                    onChange={(value) => patchNode({ rework_to: value || null })}
                  />
                  <FieldErrors items={fieldDiags("rework_to")} />
                </div>
                <div className="wf-tpl-field">
                  <span className="wf-gate-field-label">最多返工几轮</span>
                  <Segmented<string>
                    size="sm"
                    aria-label="最多返工几轮"
                    value={String(node.max_rework ?? 2)}
                    options={REWORK_OPTIONS.map((v) => ({ value: v, label: v }))}
                    onChange={(v) => patchNode({ max_rework: Number(v) })}
                  />
                  <FieldErrors items={fieldDiags("max_rework")} />
                </div>
              </>
            )}

            <div className="wf-tpl-step-actions" role="group" aria-label="步骤操作">
              <Button variant="ghost" size="xs" disabled={pos.n === 0} onClick={() => moveStep(-1)}>
                上移
              </Button>
              <Button variant="ghost" size="xs" disabled={pos.n >= nodes.length - 1} onClick={() => moveStep(1)}>
                下移
              </Button>
              <Button variant="danger-ghost" size="xs" disabled={nodes.length <= 1} onClick={() => setConfirmRemove(true)}>
                删除这个步骤
              </Button>
            </div>

            {unlocated.length > 0 && (
              <ul className="wf-tpl-errors" role="alert" aria-label="校验错误">
                {unlocated.map((d, i) => (
                  <li key={`${d.code}-${i}`}>
                    {d.message}
                    {d.suggestion && <small> 建议：{d.suggestion}</small>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
      <ConfirmDialog
        open={confirmRemove}
        title="删除这个步骤？"
        message={`删除「${node?.title || node?.key || ""}」后，其它步骤对它的输入与返工引用会一并清除。`}
        confirmLabel="删除"
        danger
        onCancel={() => setConfirmRemove(false)}
        onConfirm={removeStep}
      />
      <ConfirmDialog
        open={flowToRemove !== null}
        title="删除这个子工作流？"
        message={
          flowToRemove !== null && flows[flowToRemove]
            ? `删除「${flows[flowToRemove].title || flows[flowToRemove].key}」及其中的 ${flows[flowToRemove].nodes.length} 个步骤后，依赖它的子工作流会去掉这项依赖。`
            : ""
        }
        confirmLabel="删除"
        danger
        onCancel={() => setFlowToRemove(null)}
        onConfirm={() => {
          if (flowToRemove !== null) removeFlow(flowToRemove);
          setFlowToRemove(null);
        }}
      />
    </FormDialog>
  );
}
