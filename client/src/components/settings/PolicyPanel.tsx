/** 设置中心：执行策略（v2.2 对齐 zcode 3.18；v3.0 (plan-88) 支持工具级规则 UI）。
 * 命令规则：前缀匹配（allow 放行 / deny 拒绝 / ask 需审批），附常用前缀快捷选择；
 * 工具规则：下拉选择工具 + 三态决策 chips（作用于工具本身，executor 按 tool_name 匹配）。
 * plan-230-1144 M2: 新增「权限模式」页签——内置 4 模式白名单/提示词可覆盖，
 * 支持新建自定义模式（从全量工具勾选白名单），模式列表供输入框模式菜单动态消费。 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ExecPolicyRuleOut, type ExecPolicyToolInfo, type ModelOut, type PermissionProfileOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconPlus, IconX, IconWrench, IconTerminal, IconPencil, IconTrash } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { Badge, Button, CardRow, Checkbox, Empty, FormDialog, IconButton, Input, List, ListRow, Menu, PageActions, Section, Segmented, Select, Tag, Textarea, Tooltip } from "../ui";
import { ModelPicker } from "../chat/ModelPicker";

const DECISION_OPTS = [
  { value: "allow", label: "放行", tone: "success" },
  { value: "deny", label: "拒绝", tone: "danger" },
  { value: "ask", label: "需审批", tone: "warning" },
] as const;
/** v3.0 (plan-88): 常用命令前缀快捷选择 */
const COMMAND_PRESETS = ["git *", "npm *", "pnpm *", "python *", "pip *", "docker *"];

type RuleType = "command" | "tool";

interface PolicyForm {
  ruleType: RuleType;
  command_pattern: string;
  tool_name: string;
  decision: string;
  justification: string;
}

const EMPTY_FORM: PolicyForm = { ruleType: "command", command_pattern: "", tool_name: "", decision: "ask", justification: "" };

function RulesSection() {
  const [rules, setRules] = useState<ExecPolicyRuleOut[]>([]);
  const [tools, setTools] = useState<ExecPolicyToolInfo[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<PolicyForm>(EMPTY_FORM);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState<{ mode: "one"; id: number } | { mode: "batch" } | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const next = await api.listExecPolicyRules();
      setRules(next);
      const ids = new Set(next.map((rule) => rule.id));
      setSelected((previous) => new Set([...previous].filter((id) => ids.has(id))));
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);
  const loadTools = useCallback(async () => { try { setTools(await api.listExecPolicyTools()); } catch {} }, []);
  useEffect(() => { load(); loadTools(); }, [load, loadTools]);
  const resetForm = () => { setForm(EMPTY_FORM); setEditingId(null); setShowCreate(false); };

  const formValid = () => {
    if (form.ruleType === "tool") return Boolean(form.tool_name);
    return Boolean(form.command_pattern.trim());
  };

  const handleSave = async () => {
    if (!formValid()) return;
    // 先新建、成功后再删旧规则：新建失败时原规则保持不变，不会出现“删了却没建上”的丢失。
    // 后端 command_pattern 无唯一约束，同模式的新旧规则可以并存，不会因此冲突。
    try {
      if (form.ruleType === "tool") {
        // 工具级规则：command_pattern 存 "(tool)xxx"（与 ws.py 审批卡"始终允许"生成格式一致）
        await api.createExecPolicyRule({
          command_pattern: `(tool)${form.tool_name}`,
          decision: form.decision,
          justification: form.justification.trim() || undefined,
          tool_name: form.tool_name,
        });
      } else {
        await api.createExecPolicyRule({
          command_pattern: form.command_pattern.trim(),
          decision: form.decision,
          justification: form.justification.trim() || undefined,
        });
      }
      if (editingId != null) await api.deleteExecPolicyRule(editingId);
      resetForm(); load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
  };

  const startEdit = (r: ExecPolicyRuleOut) => {
    const isTool = Boolean(r.tool_name);
    setEditingId(r.id);
    setForm({
      ruleType: isTool ? "tool" : "command",
      command_pattern: isTool ? "" : r.command_pattern,
      tool_name: r.tool_name || "",
      decision: r.decision,
      justification: r.justification || "",
    });
    setShowCreate(true);
  };

  const toggleSelect = (id: number) => setSelected((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleSelectAll = () => setSelected((prev) => prev.size === rules.length ? new Set() : new Set(rules.map((r) => r.id)));

  const doDelete = async () => {
    if (!confirmDelete) return;
    if (confirmDelete.mode === "batch") {
      for (const id of selected) { try { await api.deleteExecPolicyRule(id); } catch { /* ignore */ } }
      setSelected(new Set());
    } else {
      try { await api.deleteExecPolicyRule(confirmDelete.id); } catch { /* ignore */ }
    }
    setConfirmDelete(null);
    load();
  };

  const batchSetDecision = async (decision: string) => {
    if (selected.size === 0) return;
    for (const id of selected) {
      const r = rules.find((x) => x.id === id);
      if (r && r.decision !== decision) {
        try {
          await api.deleteExecPolicyRule(id);
          await api.createExecPolicyRule({
            command_pattern: r.command_pattern,
            decision,
            justification: r.justification || undefined,
            tool_name: r.tool_name || undefined,
          });
        } catch { /* ignore */ }
      }
    }
    setSelected(new Set()); load();
  };

  const decisionMeta = (d: string) => DECISION_OPTS.find((o) => o.value === d) ?? DECISION_OPTS[2];
  const setRuleType = (t: RuleType) => setForm((p) => ({ ...p, ruleType: t }));

  return (
    <>
      <PageActions>
        <Button variant="secondary" size="sm" icon={<IconRefresh size={13} />} loading={loading} onClick={() => void load()}>刷新</Button>
        <Button variant="primary" size="sm" icon={<IconPlus size={13} />} onClick={() => { setForm(EMPTY_FORM); setEditingId(null); setShowCreate(true); }}>新建规则</Button>
      </PageActions>
      <FormDialog
        open={showCreate}
        onClose={resetForm}
        title={editingId != null ? "编辑执行规则" : "新建执行规则"}
        onSubmit={() => void handleSave()}
        submitLabel={editingId != null ? "保存修改" : "创建"}
        submitDisabled={!formValid()}
      >
        <div className="settings-form-field">
          <label className="settings-field-label">规则类型</label>
          <Segmented<RuleType>
            size="sm"
            value={form.ruleType}
            onChange={setRuleType}
            aria-label="规则类型"
            options={[
              { value: "command", label: "命令规则" },
              { value: "tool", label: "工具规则" },
            ]}
          />
        </div>
        {form.ruleType === "tool" ? (
          <div className="settings-form-field">
            <label className="settings-field-label">选择工具（规则作用于工具本身）</label>
            <Select
              value={form.tool_name}
              onChange={(v) => setForm((p) => ({ ...p, tool_name: v }))}
              placeholder="— 请选择工具 —"
              options={[
                { value: "", label: "— 请选择工具 —" },
                ...tools.map((t) => ({
                  value: t.name,
                  label: `${t.name}（${t.risk_level} 风险）${t.description ? ` — ${t.description}` : ""}`,
                })),
              ]}
              aria-label="选择工具"
            />
          </div>
        ) : (
          <div className="settings-form-field">
            <label className="settings-field-label">命令前缀（支持通配符）</label>
            <Input placeholder="如 git push、npm install *" value={form.command_pattern} onChange={(e) => setForm((p) => ({ ...p, command_pattern: e.target.value }))} aria-label="命令前缀" />
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
              {COMMAND_PRESETS.map((preset) => (
                <Button key={preset} type="button" variant="secondary" size="xs" onClick={() => setForm((p) => ({ ...p, command_pattern: preset }))}>{preset}</Button>
              ))}
            </div>
          </div>
        )}
        <div className="settings-form-field">
          <label className="settings-field-label">审批决策</label>
          <Segmented<string>
            size="sm"
            value={form.decision}
            onChange={(v) => setForm((p) => ({ ...p, decision: v }))}
            aria-label="审批决策"
            options={DECISION_OPTS.map((opt) => ({ value: opt.value, label: opt.label }))}
          />
        </div>
        <div className="settings-form-field"><label className="settings-field-label">理由说明（可选）</label><Input value={form.justification} onChange={(e) => setForm((p) => ({ ...p, justification: e.target.value }))} /></div>
      </FormDialog>
      <Section bare className="pol-rules">
        <List toolbar={rules.length > 0 ? (
          <div className="policy-batch-bar">
            <div className="policy-selection">
              <Checkbox checked={selected.size === rules.length} indeterminate={selected.size > 0 && selected.size < rules.length}
                onChange={toggleSelectAll} label="全选" aria-label="全选规则" />
              <span>已选 {selected.size} / {rules.length} 条</span>
            </div>
            <Menu trigger={<Button type="button" variant="ghost" size="sm" disabled={selected.size === 0}>批量操作</Button>}
              entries={[
                ...DECISION_OPTS.map((decision) => ({ label: `设为${decision.label}`, onSelect: () => void batchSetDecision(decision.value) })),
                { label: "删除所选规则", danger: true, onSelect: () => setConfirmDelete({ mode: "batch" }) },
              ]} />
          </div>
        ) : undefined}>
          {loadError && <div className="pol-rules-message" role="alert">读取执行规则失败。可点击页头的「刷新」重试。</div>}
          {rules.map((rule) => {
            const decision = decisionMeta(rule.decision);
            const isTool = Boolean(rule.tool_name);
            const name = isTool ? rule.tool_name : rule.command_pattern;
            return <ListRow key={rule.id} className="pol-rule"
              leading={<Checkbox checked={selected.has(rule.id)} onChange={() => toggleSelect(rule.id)} aria-label={`选择规则 ${name}`} />}
              icon={isTool ? <IconWrench size={16} /> : <IconTerminal size={16} />}
              name={<div className="pol-rule-name">
                <Tooltip content={name}><code>{name}</code></Tooltip>
                <Tag>{isTool ? "工具" : "命令"}</Tag>
                <Badge size="xs" tone={decision.tone}>{decision.label}</Badge>
              </div>}
              desc={rule.justification && <span className="pol-rule-why">{rule.justification}</span>}
              actions={<>
                <IconButton size="xs" title="编辑规则" aria-label={`编辑规则 ${name}`} icon={<IconPencil size={14} />} onClick={() => startEdit(rule)} />
                <IconButton size="xs" tone="danger" title="删除规则" aria-label={`删除规则 ${name}`} icon={<IconTrash size={14} />} onClick={() => setConfirmDelete({ mode: "one", id: rule.id })} />
              </>} />;
          })}
          {rules.length === 0 && (loading ? <div className="pol-rules-message" role="status">正在读取执行规则…</div> : !loadError && (
            <Empty
              title="暂无执行策略规则"
              desc="可按命令前缀或工具名称配置放行、拒绝或审批规则"
              action={<Button variant="secondary" size="sm" icon={<IconPlus size={13} />} onClick={() => { setForm(EMPTY_FORM); setEditingId(null); setShowCreate(true); }}>新建规则</Button>}
            />
          ))}
        </List>
      </Section>
      <ConfirmDialog
        open={confirmDelete !== null}
        title="删除执行策略规则"
        message={confirmDelete?.mode === "batch" ? `删除选中的 ${selected.size} 条规则？` : "删除该规则？"}
        danger
        onCancel={() => setConfirmDelete(null)}
        onConfirm={doDelete}
      />
    </>
  );
}

/* ══ plan-230-1144 M2: 权限模式编辑器 ══
 * 左侧模式列表（内置 4 项 + 自定义），右侧编辑区：
 * 名称/显示名/类型/描述/行为提示词 + 工具白名单勾选矩阵（全量工具来自
 * /exec-policy/tools）。保存即写 config.json 的 permission_profiles，
 * 输入框模式菜单与 engine 白名单即时消费，无需重启。 */
/** S13（plan-41-197）：新建模式的组合模板——一键预填类型 / 工具白名单 / 行为提示词，
 *  降低“从零配一个 Agent 模式”的门槛（用户要求提供组合工具模版与说明）。 */
const MODE_TEMPLATES: Array<{
  key: string; label: string; desc: string;
  kind: "full" | "readonly" | "plan"; tools: string[]; hint: string;
}> = [
  {
    key: "readonly-analysis",
    label: "只读分析",
    desc: "只读探索与结论输出，不写盘、不执行修改类命令",
    kind: "readonly",
    tools: ["fs_read", "fs_list", "fs_grep", "codebase_search", "memory_search", "git_diff"],
    hint: "当前处于只读分析模式：只做阅读、检索与结论输出；禁止写入文件或执行修改类命令。",
  },
  {
    key: "frontend-polish",
    label: "前端改造",
    desc: "读写前端代码并跑构建 / 类型检查验证",
    kind: "full",
    tools: ["fs_read", "fs_list", "fs_grep", "fs_write", "editor_apply_diff", "multi_file_edit", "terminal_exec", "git_diff", "codebase_search", "todo_write"],
    hint: "当前处于前端改造模式：改动遵循项目既有设计语言与目录结构；改完必须跑类型检查与构建验证。",
  },
  {
    key: "release-ops",
    label: "发布运维",
    desc: "发布链路命令与脚本操作为主，少改代码",
    kind: "full",
    tools: ["fs_read", "fs_list", "fs_grep", "terminal_exec", "git_diff"],
    hint: "当前处于发布运维模式：优先排查与执行发布链路命令；任何破坏性操作前先确认当前状态与回滚方案。",
  },
];

/** plan-20261019-policy-search：工具白名单按类别分组——几十个工具平铺时，
 *  新加的工具（如 suggest_next_task）总在末尾被 MCP / 子代理工具压起来，用户找不到。 */
const TOOL_GROUP_LABEL: Record<string, string> = {
  core: "基础能力",
  subagent: "子代理",
  mcp: "MCP 工具",
};
const TOOL_GROUP_ORDER = ["core", "subagent", "mcp"] as const;
const TOOL_GROUP_FILTERS = [
  { key: "all", label: "全部" },
  { key: "core", label: "基础" },
  { key: "subagent", label: "子代理" },
  { key: "mcp", label: "MCP" },
] as const;

function ModeProfilesSection() {
  const [profiles, setProfiles] = useState<PermissionProfileOut[]>([]);
  const [tools, setTools] = useState<ExecPolicyToolInfo[]>([]);
  const [editing, setEditing] = useState<PermissionProfileOut | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // plan-20261019-policy-search：白名单矩阵的搜索 / 类别筛选 / 只看已选
  const [toolQuery, setToolQuery] = useState("");
  const [toolGroup, setToolGroup] = useState<"all" | "core" | "subagent" | "mcp">("all");
  const [pickedOnly, setPickedOnly] = useState(false);

  const load = useCallback(async () => {
    try {
      const list = await api.listPermissionProfiles();
      setProfiles(list);
      // plan-234-1171 R8: 自动选中首个模式——此前 editing 初值为 null 且 load 不选中，
      // 右侧编辑区（条件渲染）不出现，首屏整片空白。
      setEditing((prev) => prev ?? (list.length > 0 ? { ...list[0] } : null));
    } catch (e) { setErr(String(e)); }
    try { setTools(await api.listExecPolicyTools()); } catch { /* ignore */ }
  }, []);
  useEffect(() => { void load(); }, [load]);

  /** S13：支持从模板创建（预填类型 / 工具白名单 / 行为提示词） */
  const openCreate = (tpl?: (typeof MODE_TEMPLATES)[number]) => {
    setIsNew(true);
    setEditing({
      name: tpl ? tpl.key : "",
      display_name: tpl ? tpl.label : "",
      kind: tpl ? tpl.kind : "full",
      builtin: false,
      description: tpl ? tpl.desc : "",
      tools: tpl ? [...tpl.tools] : [],
      hint: tpl ? tpl.hint : "",
    });
  };
  const startToolToggle = (name: string) => {
    if (!editing) return;
    setEditing((prev) => prev && {
      ...prev,
      tools: prev.tools.includes(name) ? prev.tools.filter((t) => t !== name) : [...prev.tools, name],
    });
  };

  /** plan-20261019-policy-search：白名单矩阵的可见项。
   *  搜索时平铺匹配结果（避免分组标题把命中项拆散），无搜索时按类别分组。 */
  const toolView = useMemo(() => {
    const q = toolQuery.trim().toLowerCase();
    const picked = new Set(editing?.tools ?? []);
    const list = tools.filter((t) => {
      if (pickedOnly && !picked.has(t.name)) return false;
      if (toolGroup !== "all" && (t.group || "core") !== toolGroup) return false;
      if (!q) return true;
      return t.name.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q);
    });
    if (q) return [{ key: "match", label: "匹配结果", items: list }];
    return TOOL_GROUP_ORDER
      .map((key) => ({
        key,
        label: TOOL_GROUP_LABEL[key],
        items: list.filter((t) => (t.group || "core") === key),
      }))
      .filter((g) => g.items.length > 0);
  }, [tools, toolQuery, toolGroup, pickedOnly, editing?.tools]);
  const handleSave = async () => {
    if (!editing) return;
    setErr(null); setSaving(true);
    try {
      await api.upsertPermissionProfile({
        name: editing.name.trim(),
        display_name: editing.display_name.trim() || undefined,
        kind: editing.kind,
        description: editing.description.trim() || undefined,
        tools: editing.tools,
        hint: editing.hint.trim() || undefined,
      });
      setEditing(null); setIsNew(false);
      await load();
    } catch (e) { setErr(String(e)); }
    finally { setSaving(false); }
  };
  const handleDelete = async (name: string) => {
    setErr(null);
    try { await api.deletePermissionProfile(name); if (editing?.name === name) { setEditing(null); setIsNew(false); } await load(); }
    catch (e) { setErr(String(e)); }
  };

  const KIND_LABEL: Record<string, string> = { full: "完全型", readonly: "只读型", plan: "规划型" };

  return (
    <>
      <PageActions>
        <Button variant="secondary" size="sm" icon={<IconRefresh size={13} />} onClick={() => void load()}>刷新</Button>
        <Button variant="primary" size="sm" icon={<IconPlus size={13} />} onClick={() => openCreate()}>新建模式</Button>
      </PageActions>
      {err && <div className="sched-error">{err}</div>}
      {/* S13（plan-41-197）：快速模板——一键预填工具组合与行为提示词 */}
      <div className="mode-tpl-row">
        <span className="mode-tpl-label">快速模板</span>
        {MODE_TEMPLATES.map((tpl) => (
          <Tooltip key={tpl.key} content={`${tpl.desc}｜预填 ${tpl.tools.length} 个工具与行为提示词`}>
            <Button variant="secondary" size="xs" onClick={() => openCreate(tpl)}>
              {tpl.label}
            </Button>
          </Tooltip>
        ))}
      </div>
      <div className="ui-field-hint mode-intro-hint">
        模式 = 工具白名单 + 行为提示词；白名单留空表示不限工具。内置模式（智能体 / 只读 / 计划）不可删除，但可覆盖其白名单与提示词。
      </div>
      <div className="perm-modes-layout">
        <div className="perm-modes-list settings-resource-list">
          {profiles.map((p) => (
            <div key={p.name} className={"settings-resource-item" + (editing?.name === p.name ? " active" : "")}>
              <div className="settings-resource-info" onClick={() => { setIsNew(false); setEditing({ ...p }); }} style={{ cursor: "pointer" }}>
                <div className="settings-resource-name">
                  {p.display_name || p.name}
                  <span style={{ marginLeft: 8 }}><Tag>{p.builtin ? "内置" : "自定义"}</Tag></span>
                  <span style={{ marginLeft: 4 }}><Tag>{KIND_LABEL[p.kind] || p.kind}</Tag></span>
                </div>
                <div className="settings-resource-desc">
                  <span>{p.description || "—"}</span>
                  <Tag>{p.tools.length === 0 ? "全量工具" : `${p.tools.length} 个工具`}</Tag>
                </div>
              </div>
              {!p.builtin && (
                <div className="settings-resource-actions">
                  <IconButton size="xs" tone="danger" title="删除模式" aria-label={`删除模式 ${p.display_name || p.name}`} icon={<IconX size={12} />} onClick={() => void handleDelete(p.name)} />
                </div>
              )}
            </div>
          ))}
          {profiles.length === 0 && <Empty size="sm" title="暂无模式" />}
        </div>
        {editing ? (
          <div className="perm-mode-editor settings-create-form">
            <div className="settings-form-field">
              <label className="settings-field-label">模式名（小写字母/数字/下划线）</label>
              <Input value={editing.name} disabled={!isNew}
                style={{ fontFamily: "var(--font-mono)" }}
                aria-label="模式名"
                onChange={(e) => setEditing((p) => p && { ...p, name: e.target.value })} />
            </div>
            <div className="settings-form-field">
              <label className="settings-field-label">显示名</label>
              <Input value={editing.display_name}
                aria-label="显示名"
                onChange={(e) => setEditing((p) => p && { ...p, display_name: e.target.value })} />
            </div>
            <div className="settings-form-field">
              <label className="settings-field-label">模式类型（决定写盘/命令审批语义）</label>
              <Segmented<"full" | "readonly" | "plan">
                size="sm"
                value={editing.kind as "full" | "readonly" | "plan"}
                onChange={(k) => setEditing((p) => p && { ...p, kind: k })}
                aria-label="模式类型"
                options={(["full", "readonly", "plan"] as const).map((k) => ({ value: k, label: KIND_LABEL[k] }))}
              />
            </div>
            <div className="settings-form-field">
              <label className="settings-field-label">描述（显示在模式菜单 tooltip）</label>
              <Input value={editing.description}
                aria-label="描述"
                onChange={(e) => setEditing((p) => p && { ...p, description: e.target.value })} />
            </div>
            <div className="settings-form-field">
              <div className="perm-tool-head">
                <label className="settings-field-label">工具白名单（不勾选 = 全量工具）</label>
                <span className="perm-tool-count">
                  {editing.tools.length === 0 ? "未限制（全量工具）" : `已选 ${editing.tools.length} 个`}
                </span>
              </div>
              <div className="perm-tool-filter">
                <Input value={toolQuery} placeholder="搜索工具名或说明，如 suggest、建议"
                  aria-label="搜索工具"
                  onChange={(e) => setToolQuery(e.target.value)} />
                <div className="pol-mode-row-actions">
                  <Segmented<"all" | "core" | "subagent" | "mcp">
                    size="sm"
                    value={toolGroup}
                    onChange={setToolGroup}
                    aria-label="工具类别"
                    options={TOOL_GROUP_FILTERS.map((g) => ({ value: g.key, label: g.label }))}
                  />
                  <label className="perm-tool-picked">
                    <Checkbox checked={pickedOnly} onChange={() => setPickedOnly((v) => !v)}
                      aria-label="只看已选工具" />
                    <span>只看已选</span>
                  </label>
                </div>
              </div>
              <div className="perm-tool-grid">
                {toolView.map((g) => (
                  <Fragment key={g.key}>
                    <div className="perm-tool-group">{g.label}（{g.items.length}）</div>
                    {g.items.map((t) => (
                      <Tooltip key={t.name} content={t.description || undefined}>
                        <label className="perm-tool-check">
                          <Checkbox checked={editing.tools.includes(t.name)} onChange={() => startToolToggle(t.name)} aria-label={`${t.name} 白名单`} />
                          <code>{t.name}</code>
                          <span className="perm-tool-risk" data-risk={t.risk_level}>{t.risk_level}</span>
                        </label>
                      </Tooltip>
                    ))}
                  </Fragment>
                ))}
                {toolView.every((g) => g.items.length === 0) && (
                  <div className="perm-tool-empty">没有匹配的工具</div>
                )}
              </div>
            </div>
            <div className="settings-form-field">
              <label className="settings-field-label">行为提示词（注入系统指令，可选）</label>
              <Textarea rows={4} value={editing.hint}
                placeholder="如：当前处于严格审阅模式，任何写操作前必须先输出分析结论等待用户确认。"
                aria-label="行为提示词"
                onChange={(e) => setEditing((p) => p && { ...p, hint: e.target.value })} />
            </div>
            <div className="settings-create-actions">
              <Button variant="secondary" size="sm" onClick={() => { setEditing(null); setIsNew(false); }}>取消</Button>
              <Button variant="primary" size="sm" loading={saving} disabled={saving || !editing.name.trim()} onClick={() => void handleSave()}>
                {saving ? "保存中…" : "保存模式"}
              </Button>
            </div>
          </div>
        ) : (
          /* plan-234-1171 R8: 无选中时给出明确空态，而非整片空白 */
          <div className="perm-mode-empty">从左侧选择一个模式进行编辑，或点击「新建模式」</div>
        )}
      </div>
    </>
  );
}

/** plan-75-332: 审批「解释」——点击审批卡左下角「解释」时，用哪个模型与思考深度
 *  分析该命令的用途与风险。两项默认都跟随当前会话（会话模型 / 会话思考深度）；
 *  也可在此固定为专用组合（例如用轻量模型快速解释、或用强模型深度审风险）。
 *  遵循设置页「修改即保存」：改动后 300ms 防抖落盘，无保存按钮。 */
function ApprovalExplainSection() {
  const [modelId, setModelId] = useState<number | null>(null);
  const [effort, setEffort] = useState<string | null>(null);
  const [models, setModels] = useState<ModelOut[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    api.getGlobalSettings()
      .then((g) => {
        // 0 / 缺省 = 跟随会话（后端约定），统一归为 null——否则「跟随会话」项不会显示勾选
        setModelId(
          typeof g.approval_explain_model_id === "number" && g.approval_explain_model_id > 0
            ? g.approval_explain_model_id
            : null,
        );
        setEffort(g.approval_explain_reasoning_effort || null);
      })
      .catch(() => { /* 读取失败保持“跟随会话”默认 */ });
    // 可用性口径与输入框模型选择器一致：模型启用 + 供应商启用
    api.listModels()
      .then((list) => setModels(list.filter((m) => m.is_active && m.provider_active !== false)))
      .catch(() => { /* 失败则只能选“跟随会话模型” */ });
  }, []);

  const patch = useCallback((nextModelId: number | null, nextEffort: string | null) => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      void api.setGlobalSettings({
        approval_explain_model_id: nextModelId,
        approval_explain_reasoning_effort: nextEffort,
      }).catch((e) => setErr(String(e)));
    }, 300);
  }, []);
  useEffect(() => () => { if (timerRef.current) window.clearTimeout(timerRef.current); }, []);

  const selectedModel = models.find((m) => m.id === modelId) ?? null;
  // 专用深度必须先选定专用模型且该模型声明了思考档位（否则无法确定可选值）
  const supportsEffort = (selectedModel?.reasoning_efforts?.length ?? 0) > 0;
  const effortOptions = [
    { value: "", label: "跟随会话思考深度（默认）" },
    ...(selectedModel?.reasoning_efforts ?? []).map((e) => ({ value: e, label: e })),
  ];

  return (
    <Section
      title="审批解释模型"
      desc="审批卡左下角「解释」会把当前命令交给 AI 分析用途与风险。默认沿用当前会话的模型与思考深度；如需固定为更轻快或更强的组合，可在此单独指定。"
    >
      <CardRow title="解释专用模型" desc="留空则跟随当前会话所用模型">
        {/* plan-91-393：改用全局模型选择器，与输入框模型弹窗一致（分组/标签/余额） */}
        <div className="sb-model-picker">
          <ModelPicker
            models={models}
            value={modelId}
            open={pickerOpen}
            onToggle={() => setPickerOpen((o) => !o)}
            side="bottom"
            inheritLabel="跟随会话模型（默认）"
            onInherit={() => { setModelId(null); setEffort(null); patch(null, null); setPickerOpen(false); }}
            onChange={(id) => {
              // 换模型后旧深度可能不被支持 → 一并重置为「跟随会话深度」
              setModelId(id); setEffort(null); patch(id, null); setPickerOpen(false);
            }}
          />
        </div>
      </CardRow>
      <CardRow
        title="解释专用思考深度"
        desc={supportsEffort ? "留空则跟随当前会话所用思考深度" : "选定支持思考档位的专用模型后可选"}
      >
        <Select
          value={effort ?? ""}
          onChange={(v) => { const next = v || null; setEffort(next); patch(modelId, next); }}
          options={effortOptions}
          disabled={!supportsEffort}
          style={{ minWidth: 220 }}
          aria-label="解释专用思考深度"
        />
      </CardRow>
      {err && <div className="sched-error">{err}</div>}
    </Section>
  );
}

export function PolicyPanel() {
  const [tab, setTab] = useState<"rules" | "modes" | "explain">("rules");
  return (
    <div>
      {/* plan-114-450：页内标签统一为分段控件，移除遗留的 .perm-tab（AGENTS.md 点名要迁移的私有类） */}
      <div className="ui-toolbar perm-tabs-bar">
        <Segmented value={tab} onChange={setTab} aria-label="执行策略"
          options={[{ value: "rules", label: "执行规则" }, { value: "modes", label: "权限模式" }, { value: "explain", label: "审批解释" }]} />
      </div>
      {tab === "rules" && <RulesSection />}
      {tab === "modes" && <ModeProfilesSection />}
      {tab === "explain" && <ApprovalExplainSection />}
    </div>
  );
}
