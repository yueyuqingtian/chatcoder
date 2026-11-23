/** 设置 → 智能体（plan-20261123-agent-panel-ui 重构版）：
 *  - PageShell 使用 wide 档（880px），主操作「新增成员」通过 PageActions 投送到页头右上角；
 *  - 顶部「团队协作策略」支持总开关联动折叠，分工切换用 Segmented，并发上限用步进控件；
 *  - 下方「团队成员与配置」收拢为一体化双栏卡片：左栏按内置/自定义分组并支持筛选，
 *    右栏头部聚合元信息 Badge、固定槽位保存状态与溢出 Menu（危险操作二次确认），
 *    下方统一 CardRow 与 Field 排版，提示词改用分段切换 + 折叠固定运行规则。 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  api, type GlobalSettingsIn, type GlobalSettingsOut, type ModelOut, type SubagentExecutionMode,
  type SubagentProfileDetail, type SubagentProfileInput, type SubagentProfileOut,
} from "../../api/client";
import { ModelPicker } from "../chat/ModelPicker";
import {
  IconAlertCircle, IconCheck, IconCheckCircle, IconChevron, IconChevronLeft,
  IconCopy, IconMinus, IconMoreHorizontal, IconPlus, IconRotateCcw, IconSearch,
  IconSpinner, IconTerminal, IconTrash,
} from "../icons";
import {
  Badge, Button, CardRow, Collapse, Dialog, Empty, Field, IconButton,
  Input, ListRow, Menu, Section, Segmented, Select, Switch, Textarea,
  type MenuEntry,
} from "../ui";
import "../../styles/agent-team.css";

type DelegationTarget = "team" | "main";
type PromptView = "edit" | "default" | "standard" | "ta3";
type PromptTab = "edit" | "default" | "preview";
type PreviewFormat = "standard" | "ta3";
type DialogKind = "create" | "reset" | "delete" | "resetPrompt" | null;

const MODE_OPTIONS: Array<{ value: SubagentExecutionMode; label: string }> = [
  { value: "readonly", label: "只读" },
  { value: "agent", label: "智能体" },
];

const CREATE_MODE_OPTIONS: Array<{ value: SubagentExecutionMode; label: string }> = [
  { value: "readonly", label: "只读模式" },
  { value: "agent", label: "智能体模式" },
];

const DELEGATION_OPTIONS: Array<{ value: DelegationTarget; label: string }> = [
  { value: "team", label: "团队成员" },
  { value: "main", label: "主代理负责" },
];

const PROMPT_TABS: Array<{ value: PromptTab; label: string }> = [
  { value: "edit", label: "编辑" },
  { value: "default", label: "默认模板" },
  { value: "preview", label: "运行预览" },
];

const PREVIEW_FORMATS: Array<{ value: PreviewFormat; label: string }> = [
  { value: "standard", label: "通用模型" },
  { value: "ta3", label: "TA3" },
];

const INHERITED_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

const EFFORT_LABELS: Record<string, string> = {
  none: "none（关闭思考）",
  minimal: "minimal（极简）",
  low: "low（轻量）",
  medium: "medium（均衡）",
  high: "high（深度）",
  xhigh: "xhigh（超深度）",
  max: "max（最大推理）",
};

type Pending = {
  saved: SubagentProfileDetail;
  draft: SubagentProfileInput;
  timer?: ReturnType<typeof setTimeout>;
  sending?: SubagentProfileInput;
  saving?: Promise<void>;
  error?: string;
};

function modeName(mode: SubagentExecutionMode) {
  return mode === "readonly" ? "只读" : "智能体";
}

function clampConcurrency(raw: number) {
  return Math.max(1, Math.min(30, Math.round(raw)));
}

export function SubagentsPanel() {
  const [members, setMembers] = useState<SubagentProfileOut[]>([]);
  const [models, setModels] = useState<ModelOut[]>([]);
  const [settings, setSettings] = useState<GlobalSettingsOut | null>(null);
  const [loadError, setLoadError] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [limitText, setLimitText] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [narrowDetail, setNarrowDetail] = useState(false);
  const [filterQuery, setFilterQuery] = useState("");
  const [, redraw] = useState(0);
  const [detailError, setDetailError] = useState("");
  const [modelOpen, setModelOpen] = useState(false);
  const [promptView, setPromptView] = useState<PromptView>("edit");
  const [previewFormat, setPreviewFormat] = useState<PreviewFormat>("standard");
  const [preview, setPreview] = useState<SubagentProfileDetail["preview"] | null>(null);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [dialogError, setDialogError] = useState("");
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [newMode, setNewMode] = useState<SubagentExecutionMode>("readonly");
  const [newDesc, setNewDesc] = useState("");

  const records = useRef(new Map<number, Pending>());
  const settingsChain = useRef(Promise.resolve());
  const limitTimer = useRef<ReturnType<typeof setTimeout>>();
  const pendingLimit = useRef<number>();
  const currentSelection = useRef<number | null>(null);
  const mounted = useRef(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout>>();

  const nameId = useId();
  const descId = useId();
  const newNameId = useId();
  const newDescId = useId();

  const load = useCallback(async () => {
    setLoadError("");
    const [team, config, availableModels] = await Promise.allSettled([
      api.listSubagents(), api.getGlobalSettings(), api.listModels(),
    ]);
    if (!mounted.current) return;
    if (team.status === "fulfilled") {
      const sorted = [...team.value].sort((a, b) =>
        Number(b.builtin) - Number(a.builtin)
        || (a.builtin && b.builtin ? Number(a.execution_mode === "agent") - Number(b.execution_mode === "agent") : a.id - b.id));
      setMembers(sorted);
      setSelected((previous) => previous ?? sorted[0]?.id ?? null);
    }
    if (config.status === "fulfilled") {
      setSettings(config.value);
      setLimitText(String(config.value.max_concurrent_subagents ?? 4));
    }
    if (availableModels.status === "fulfilled") setModels(availableModels.value.filter((model) => model.is_active));
    if ([team, config, availableModels].some((value) => value.status === "rejected")) {
      setLoadError("部分团队设置加载失败，请重试。");
    }
  }, []);

  const saveMember = useCallback(async function flush(id: number): Promise<void> {
    const entry = records.current.get(id);
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = undefined;
    if (entry.saving) {
      await entry.saving;
      if (entry.error) return;
      return flush(id);
    }
    if (!Object.keys(entry.draft).length) return;
    const sent = entry.draft;
    entry.error = undefined;
    entry.draft = {};
    entry.sending = sent;
    entry.saving = (async () => {
      try {
        const saved = await api.updateSubagent(id, { ...sent, revision: entry.saved.revision });
        entry.saved = {
          ...entry.saved, ...saved,
          system_prompt: sent.reset ? null : sent.system_prompt === undefined ? entry.saved.system_prompt : sent.system_prompt,
        };
        if (sent.reset || sent.execution_mode) {
          entry.saved = await api.getSubagent(id).catch(() => entry.saved);
        }
        if (mounted.current) setMembers((items) => items.map((row) => row.id === id ? saved : row));
      } catch (error) {
        entry.draft = { ...sent, ...entry.draft };
        entry.error = String(error);
      } finally {
        entry.sending = undefined;
        entry.saving = undefined;
        if (mounted.current) redraw((version) => version + 1);
      }
    })();
    if (mounted.current) redraw((version) => version + 1);
    await entry.saving;
  }, []);

  const saveSettings = useCallback((patch: GlobalSettingsIn) => {
    if (mounted.current) {
      setSettings((current) => current ? { ...current, ...patch } : current);
      setSettingsError("");
    }
    settingsChain.current = settingsChain.current.then(async () => {
      try {
        await api.setGlobalSettings(patch);
      } catch (error) {
        if (mounted.current) {
          setSettingsError("保存团队设置失败。" + String(error));
          const config = await api.getGlobalSettings().catch(() => null);
          if (config && mounted.current) {
            setSettings((current) => current ? {
              ...current,
              ...Object.fromEntries(Object.keys(patch).map((key) => [key, config[key as keyof GlobalSettingsOut]])),
            } : config);
            if (patch.max_concurrent_subagents !== undefined) {
              setLimitText(String(config.max_concurrent_subagents ?? 4));
            }
          }
        }
      }
    });
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      for (const id of records.current.keys()) void saveMember(id);
      clearTimeout(limitTimer.current);
      clearTimeout(copyTimer.current);
      if (pendingLimit.current !== undefined) saveSettings({ max_concurrent_subagents: pendingLimit.current });
    };
  }, [load, saveMember, saveSettings]);

  const openDetail = useCallback(async (id: number) => {
    setDetailError("");
    if (records.current.has(id)) return;
    try {
      const data = await api.getSubagent(id);
      if (!records.current.has(id)) records.current.set(id, { saved: data, draft: {} });
      if (mounted.current) redraw((version) => version + 1);
    } catch (error) {
      if (mounted.current && currentSelection.current === id) setDetailError(String(error));
    }
  }, []);

  useEffect(() => {
    currentSelection.current = selected;
    setModelOpen(false);
    setPromptView("edit");
    setPreview(null);
    setCopiedPrompt(false);
    if (selected === null) return;
    void openDetail(selected);
    return () => { void saveMember(selected); };
  }, [openDetail, saveMember, selected]);

  const edit = (patch: SubagentProfileInput, delayed = false) => {
    if (selected === null) return;
    const entry = records.current.get(selected);
    if (!entry) return;
    entry.draft = { ...entry.draft, ...patch };
    redraw((version) => version + 1);
    clearTimeout(entry.timer);
    if (delayed) entry.timer = setTimeout(() => { void saveMember(selected); }, 300);
    else void saveMember(selected);
  };

  const selectPromptView = async (value: PromptView) => {
    setPromptView(value);
    if (value === "standard" || value === "ta3") setPreviewFormat(value);
    if (selected === null || (value !== "standard" && value !== "ta3")) return;
    const id = selected;
    setPreview(null);
    await saveMember(id);
    if (records.current.get(id)?.error) {
      if (currentSelection.current === id) setPromptView("edit");
      return;
    }
    try {
      const data = await api.getSubagent(id);
      if (currentSelection.current === id) setPreview(data.preview);
    } catch (error) {
      if (currentSelection.current === id) setDetailError(String(error));
    }
  };

  const switchPromptTab = (tab: PromptTab) => {
    if (tab === "preview") void selectPromptView(previewFormat);
    else void selectPromptView(tab);
  };

  const changeLimit = (raw: string, immediate = false) => {
    setLimitText(raw);
    clearTimeout(limitTimer.current);
    pendingLimit.current = undefined;
    const parsed = Number(raw);
    if (!raw.trim() || !Number.isFinite(parsed)) return;
    const value = clampConcurrency(parsed);
    if (immediate) {
      setLimitText(String(value));
      saveSettings({ max_concurrent_subagents: value });
      return;
    }
    pendingLimit.current = value;
    limitTimer.current = setTimeout(() => {
      pendingLimit.current = undefined;
      setLimitText(String(value));
      saveSettings({ max_concurrent_subagents: value });
    }, 300);
  };

  const stepLimit = (delta: number) => {
    const base = Number(limitText);
    const current = Number.isFinite(base) && limitText.trim() ? base : (settings?.max_concurrent_subagents ?? 4);
    changeLimit(String(clampConcurrency(current + delta)), true);
  };

  const entry = selected === null ? undefined : records.current.get(selected);
  const saved = entry?.saved;
  const draft = { ...entry?.sending, ...entry?.draft };
  const member = saved && {
    ...saved, ...draft,
    display_name: draft?.name ?? saved.display_name,
  };
  const selectedModel = models.find((model) => model.id === member?.model_id);
  const effortOptions = member?.model_id ? selectedModel?.reasoning_efforts ?? [] : INHERITED_EFFORTS;
  const effortUnsupported = Boolean(member?.model_id && effortOptions.length === 0);
  const draftPrompt = draft?.system_prompt === undefined ? saved?.system_prompt : draft.system_prompt;
  const isPromptCustomized = draftPrompt !== null && draftPrompt !== undefined;
  const shownPrompt = promptView === "edit" ? draftPrompt ?? saved?.default_prompt
    : promptView === "default" ? saved?.default_prompt
    : preview?.[promptView === "ta3" ? "ta3" : "standard"];
  const promptTopTab: PromptTab = promptView === "edit" || promptView === "default" ? promptView : "preview";

  const memberGroups = useMemo(() => {
    const q = filterQuery.trim().toLowerCase();
    const visible = members.filter((row) =>
      !q || row.display_name.toLowerCase().includes(q) || (row.description ?? "").toLowerCase().includes(q));
    return [
      { key: "builtin", title: "内置角色", items: visible.filter((row) => row.builtin) },
      { key: "custom", title: "自定义成员", items: visible.filter((row) => !row.builtin) },
    ].filter((group) => group.items.length > 0);
  }, [filterQuery, members]);

  const showDialog = (kind: DialogKind) => {
    setDialogError("");
    if (kind === "create") {
      setNewName("");
      setNewMode("readonly");
      setNewDesc("");
    }
    setDialog(kind);
  };

  const confirmDialog = async () => {
    setBusy(true);
    setDialogError("");
    try {
      if (dialog === "create") {
        const payload: SubagentProfileInput & { name: string } = {
          name: newName.trim(),
          execution_mode: newMode,
          ...(newDesc.trim() ? { description: newDesc.trim() } : {}),
        };
        const data = await api.createSubagent(payload);
        records.current.set(data.id, { saved: data, draft: {} });
        setMembers((items) => [...items, data]);
        setSelected(data.id);
        setNarrowDetail(true);
      } else if (selected !== null && dialog === "delete") {
        await saveMember(selected);
        await api.deleteSubagent(selected);
        records.current.delete(selected);
        const remaining = members.filter((item) => item.id !== selected);
        setMembers(remaining);
        setSelected(remaining[0]?.id ?? null);
        setNarrowDetail(false);
      } else if (selected !== null && entry && dialog === "reset") {
        clearTimeout(entry.timer);
        entry.timer = undefined;
        entry.draft = {};
        await saveMember(selected);
        if (entry.error) throw new Error(entry.error);
        await api.updateSubagent(selected, { reset: "all", revision: entry.saved.revision });
        entry.saved = await api.getSubagent(selected);
        setMembers((items) => items.map((item) => item.id === selected ? entry.saved : item));
        setPromptView("edit");
      } else if (selected !== null && entry && dialog === "resetPrompt") {
        clearTimeout(entry.timer);
        entry.timer = undefined;
        delete entry.draft.system_prompt;
        await saveMember(selected);
        if (entry.error) throw new Error(entry.error);
        const updated = await api.updateSubagent(selected, { reset: "prompt", revision: entry.saved.revision });
        entry.saved = { ...entry.saved, ...updated, system_prompt: null };
        entry.saved = await api.getSubagent(selected).catch(() => entry.saved);
        setMembers((items) => items.map((item) => item.id === selected ? entry.saved : item));
        setPromptView("edit");
      }
      setDialog(null);
    } catch (error) {
      setDialogError(String(error));
    } finally {
      setBusy(false);
    }
  };

  const copyDefaultPrompt = () => {
    const text = saved?.default_prompt ?? "";
    if (!text) return;
    void navigator.clipboard?.writeText(text).then(() => {
      setCopiedPrompt(true);
      clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopiedPrompt(false), 1200);
    }).catch(() => {});
  };

  const limitNum = clampConcurrency(Number(limitText) || (settings?.max_concurrent_subagents ?? 4));
  const teamEnabled = settings?.subagent_enabled !== false;

  const menuEntries: MenuEntry[] = member ? [
    member.builtin
      ? {
          key: "reset",
          label: "恢复默认配置",
          icon: <IconRotateCcw size={13} />,
          onSelect: () => showDialog("reset"),
        }
      : {
          key: "delete",
          label: "删除成员",
          icon: <IconTrash size={13} />,
          danger: true,
          onSelect: () => showDialog("delete"),
        },
  ] : [];

  const saveIndicator = entry?.error
    ? { tone: "is-error", icon: <IconAlertCircle size={13} />, text: "保存失败" }
    : entry?.saving || (entry && Object.keys(entry.draft).length > 0)
    ? { tone: "is-saving", icon: <IconSpinner size={13} />, text: "正在保存…" }
    : { tone: "is-saved", icon: <IconCheckCircle size={13} />, text: "已保存" };

  return (
    <div className="agent-team-settings">
      {loadError && (
        <div className="agent-team-alert" role="alert">
          <span>{loadError}</span>
          <Button size="xs" variant="secondary" onClick={() => void load()}>重试</Button>
        </div>
      )}

      {settings && (
        <Section title="团队协作策略" desc="统一控制主代理与工作流是否将任务委派给智能体成员。" flush>
          <CardRow
            title="启用智能体团队"
            desc="关闭后所有任务均由主代理直接完成，已配置成员仅作保留。"
            className="agent-team-row-enable"
          >
            <Switch
              aria-label="启用智能体团队"
              checked={teamEnabled}
              onChange={(value) => saveSettings({ subagent_enabled: value })}
            />
          </CardRow>

          <Collapse open={teamEnabled}>
            <div className="agent-team-policy-body">
              {([
                ["subagent_explore_enabled", "代码探索", "阅读代码、定位实现与梳理模块依赖。"],
                ["subagent_execute_enabled", "执行任务", "修改代码、运行命令与验证构建结果。"],
              ] as const).map(([key, title, description]) => (
                <CardRow key={key} title={title} desc={description}>
                  <Segmented<DelegationTarget>
                    size="sm"
                    aria-label={title}
                    options={DELEGATION_OPTIONS}
                    value={settings[key] === false ? "main" : "team"}
                    onChange={(value) => saveSettings({ [key]: value === "team" })}
                  />
                </CardRow>
              ))}
              <CardRow title="单会话并发上限" desc="达到上限时排队等待已有成员完成，范围 1～30。">
                {/* plan-20261123-ux-fixes：Button iconOnly 必须通过 icon prop 传入图标，此前放进 children 导致渲染为空框 */}
                <div className="agent-team-stepper">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    iconOnly
                    icon={<IconMinus size={13} />}
                    aria-label="减少并发上限"
                    disabled={limitNum <= 1}
                    onClick={() => stepLimit(-1)}
                  />
                  <Input
                    size="sm"
                    aria-label="单会话并发上限"
                    type="number"
                    min={1}
                    max={30}
                    suffix="路"
                    wrapClassName="agent-team-stepper-input"
                    value={limitText}
                    onChange={(event) => changeLimit(event.target.value)}
                    onBlur={() => {
                      if (pendingLimit.current === undefined) setLimitText(String(settings.max_concurrent_subagents ?? 4));
                    }}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    iconOnly
                    icon={<IconPlus size={13} />}
                    aria-label="增加并发上限"
                    disabled={limitNum >= 30}
                    onClick={() => stepLimit(1)}
                  />
                </div>
              </CardRow>
              {settingsError && <p className="agent-team-feedback" role="alert">{settingsError}</p>}
            </div>
          </Collapse>
        </Section>
      )}

      {/* plan-20261123-ux-fixes：「新增成员」归位到「团队成员与配置」分组标题右侧 */}
      <Section
        title="团队成员与配置"
        desc={`共 ${members.length} 位成员`}
        actions={
          <Button variant="primary" size="sm" icon={<IconPlus size={14} />} onClick={() => showDialog("create")}>
            新增成员
          </Button>
        }
        flush
      >
        <div className={`agent-team-workspace ${narrowDetail ? "show-detail" : ""}`}>
          <aside className="agent-team-sidebar" aria-label="成员列表">
            <div className="agent-team-search">
              <Input
                size="sm"
                clearable
                prefix={<IconSearch size={13} />}
                placeholder="筛选成员…"
                aria-label="筛选团队成员"
                value={filterQuery}
                onChange={(event) => setFilterQuery(event.target.value)}
              />
            </div>

            <div className="agent-team-list" role="listbox" aria-label="团队成员列表">
              {memberGroups.length === 0 ? (
                <Empty size="sm" title="没有匹配的成员" desc="尝试修改筛选关键字或新增成员" />
              ) : (
                memberGroups.map((group) => (
                  <div key={group.key} className="agent-team-group-block">
                    <div className="agent-team-group-label">{group.title}</div>
                    {group.items.map((row) => {
                      const model = models.find((item) => item.id === row.model_id);
                      const modelDesc = model?.name || (row.model_id ? "模型不可用" : "跟随主代理");
                      const isSelected = selected === row.id;
                      const open = () => {
                        setSelected(row.id);
                        setNarrowDetail(true);
                      };
                      return (
                        <ListRow
                          key={row.id}
                          role="option"
                          tabIndex={0}
                          selected={isSelected}
                          className={`agent-team-member mode-${row.execution_mode}`}
                          muted={!row.is_active}
                          icon={row.execution_mode === "readonly" ? <IconSearch size={14} /> : <IconTerminal size={14} />}
                          name={row.display_name}
                          desc={row.reasoning_effort ? `${modelDesc} · ${row.reasoning_effort}` : modelDesc}
                          meta={
                            <>
                              <Badge size="xs" className={`agent-team-role-pill mode-${row.execution_mode}`}>
                                {modeName(row.execution_mode)}
                              </Badge>
                              {!row.is_active && <Badge size="xs" tone="warning">已停用</Badge>}
                            </>
                          }
                          onClick={open}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              open();
                            }
                          }}
                        />
                      );
                    })}
                  </div>
                ))
              )}
            </div>
          </aside>

          <div className="agent-team-detail">
            {member && entry ? (
              <>
                <div className="agent-team-detail-head">
                  <div className="agent-team-detail-title-wrap">
                    <Button
                      variant="secondary"
                      size="sm"
                      icon={<IconChevronLeft size={14} />}
                      className="agent-team-back"
                      onClick={() => {
                        if (selected !== null) void saveMember(selected);
                        setNarrowDetail(false);
                      }}
                    >
                      成员
                    </Button>
                    <span className={`agent-team-detail-avatar mode-${member.execution_mode}`} aria-hidden>
                      {member.execution_mode === "readonly" ? <IconSearch size={16} /> : <IconTerminal size={16} />}
                    </span>
                    <div className="agent-team-detail-identity">
                      <h3 className="agent-team-detail-name">{member.display_name || "未命名成员"}</h3>
                      <div className="agent-team-detail-meta">
                        <Badge size="sm" className={`agent-team-role-pill mode-${member.execution_mode}`}>
                          {modeName(member.execution_mode)}模式
                        </Badge>
                        {member.builtin && (
                          <>
                            <span className="agent-team-meta-sep" aria-hidden>·</span>
                            <span className="agent-team-meta-text">内置角色</span>
                          </>
                        )}
                        <span className="agent-team-meta-sep" aria-hidden>·</span>
                        <span className="agent-team-meta-text">审批跟随主会话</span>
                      </div>
                    </div>
                  </div>

                  <div className="agent-team-detail-actions">
                    <span className={`agent-team-save-state ${saveIndicator.tone}`} role="status">
                      <span className="agent-team-save-icon" aria-hidden>{saveIndicator.icon}</span>
                      <span>{saveIndicator.text}</span>
                    </span>
                    <Menu
                      align="end"
                      entries={menuEntries}
                      trigger={
                        <IconButton
                          icon={<IconMoreHorizontal size={15} />}
                          aria-label="成员更多操作"
                        />
                      }
                    />
                  </div>
                </div>

                {detailError && (
                  <div className="agent-team-feedback" role="alert">
                    <span>{detailError}</span>
                    <Button size="xs" variant="secondary" onClick={() => { if (selected !== null) void openDetail(selected); }}>
                      重试
                    </Button>
                  </div>
                )}

                {entry.error && (
                  <div className="agent-team-feedback" role="alert">
                    <span>{entry.error}</span>
                    <Button size="xs" variant="secondary" onClick={() => void saveMember(member.id)}>重试保存</Button>
                    <Button
                      size="xs"
                      variant="ghost"
                      onClick={() => {
                        void api.getSubagent(member.id).then((latest) => {
                          entry.saved = latest;
                          entry.error = undefined;
                          redraw((version) => version + 1);
                        }).catch((error: unknown) => setDetailError(String(error)));
                      }}
                    >
                      加载最新版本，保留草稿
                    </Button>
                  </div>
                )}

                <div className="agent-team-detail-body">
                  <div className="agent-team-section-pad agent-team-fields">
                    <Field label="成员名称" htmlFor={nameId}>
                      <Input
                        id={nameId}
                        value={member.display_name}
                        maxLength={60}
                        onChange={(event) => edit({ name: event.target.value }, true)}
                      />
                    </Field>
                    <Field label="职责说明" hint="主代理与工作流据此判断何时向该成员派发任务" htmlFor={descId}>
                      <Textarea
                        id={descId}
                        value={member.description ?? ""}
                        maxLength={300}
                        rows={2}
                        placeholder="描述该成员擅长的任务边界与输出要求…"
                        onChange={(event) => edit({ description: event.target.value }, true)}
                      />
                    </Field>
                  </div>

                  <div className="agent-team-rows">
                    <CardRow
                      title="允许普通对话选用"
                      desc={member.builtin ? "关闭后仅在普通对话隐藏，工作流仍可按节点职责使用此成员。" : "关闭后不再自动向此成员派发任务。"}
                    >
                      <Switch
                        aria-label="允许普通对话选用"
                        checked={member.is_active}
                        onChange={(value) => edit({ is_active: value })}
                      />
                    </CardRow>

                    {!member.builtin && (
                      <CardRow title="系统执行模式" desc="只读仅可检索阅读；智能体可修改文件与执行命令。">
                        <Segmented<SubagentExecutionMode>
                          size="sm"
                          aria-label="系统执行模式"
                          options={MODE_OPTIONS}
                          value={member.execution_mode}
                          onChange={(value) => edit({ execution_mode: value })}
                        />
                      </CardRow>
                    )}

                    <CardRow title="模型" desc="可单独指定模型，或默认跟随主代理。">
                      <div className="agent-team-control-slot">
                        <ModelPicker
                          models={models}
                          value={member.model_id}
                          open={modelOpen}
                          side="bottom"
                          onToggle={() => setModelOpen((value) => !value)}
                          inheritLabel="跟随主代理"
                          onInherit={() => { edit({ model_id: null, reasoning_effort: null }); setModelOpen(false); }}
                          onChange={(id) => {
                            const picked = models.find((item) => item.id === Number(id));
                            const effort = member.reasoning_effort;
                            edit({
                              model_id: Number(id),
                              reasoning_effort: effort && picked?.reasoning_efforts?.includes(effort) ? effort : null,
                            });
                            setModelOpen(false);
                          }}
                        />
                      </div>
                    </CardRow>

                    <CardRow
                      title="思考深度"
                      desc={effortUnsupported
                        ? `${selectedModel?.name ?? "当前所选模型"}不支持调整思考深度。`
                        : member.model_id
                        ? `为 ${selectedModel?.name ?? "所选模型"} 指定独立推理深度。`
                        : "跟随主代理时，默认继承当前会话的思考深度。"}
                    >
                      <div className="agent-team-control-slot">
                        <Select
                          aria-label="思考深度"
                          className="agent-team-effort-select"
                          disabled={effortUnsupported}
                          value={member.reasoning_effort ?? ""}
                          onChange={(value) => edit({ reasoning_effort: value || null })}
                          options={effortUnsupported
                            ? [{ value: "", label: "不支持调整" }]
                            : [
                                { value: "", label: "跟随会话或模型默认" },
                                ...effortOptions.map((value) => ({ value, label: EFFORT_LABELS[value] ?? value })),
                              ]}
                        />
                      </div>
                    </CardRow>
                  </div>

                  <div className="agent-team-section-pad agent-team-prompt">
                    <div className="agent-team-prompt-head">
                      <div className="agent-team-prompt-title-group">
                        <span id="agent-team-prompt-label" className="agent-team-prompt-title">角色提示词</span>
                        <Badge
                          size="sm"
                          tone="neutral"
                          className={`agent-team-prompt-badge${isPromptCustomized ? " is-custom" : ""}`}
                        >
                          {isPromptCustomized ? "已自定义" : "使用默认"}
                        </Badge>
                      </div>

                      <div className="agent-team-prompt-tools">
                        <Segmented<PromptTab>
                          size="sm"
                          aria-label="提示词视图"
                          options={PROMPT_TABS}
                          value={promptTopTab}
                          onChange={switchPromptTab}
                        />
                        <Button
                          size="sm"
                          variant="secondary"
                          icon={<IconRotateCcw size={13} />}
                          disabled={!isPromptCustomized}
                          onClick={() => showDialog("resetPrompt")}
                        >
                          还原默认
                        </Button>
                      </div>
                    </div>

                    {promptTopTab === "default" && (
                      <div className="agent-team-prompt-subbar">
                        <span className="agent-team-prompt-subbar-text">系统内置默认提示词（只读）</span>
                        <div className="agent-team-prompt-subbar-actions">
                          <Button
                            size="xs"
                            variant="secondary"
                            icon={copiedPrompt ? <IconCheck size={12} /> : <IconCopy size={12} />}
                            onClick={copyDefaultPrompt}
                          >
                            {copiedPrompt ? "已复制" : "复制"}
                          </Button>
                          <Button
                            size="xs"
                            variant="primary"
                            onClick={() => {
                              if (isPromptCustomized) {
                                edit({ system_prompt: saved?.default_prompt ?? "" }, true);
                              }
                              setPromptView("edit");
                            }}
                          >
                            基于此编辑
                          </Button>
                        </div>
                      </div>
                    )}

                    {promptTopTab === "preview" && (
                      <div className="agent-team-prompt-subbar">
                        <span className="agent-team-prompt-subbar-text">最终注入模型的完整上下文（只读预览）</span>
                        <Segmented<PreviewFormat>
                          size="sm"
                          aria-label="预览模型格式"
                          options={PREVIEW_FORMATS}
                          value={previewFormat}
                          onChange={(format) => void selectPromptView(format)}
                        />
                      </div>
                    )}

                    <Textarea
                      aria-labelledby="agent-team-prompt-label"
                      className={promptView !== "edit" ? "is-readonly" : ""}
                      rows={10}
                      spellCheck={false}
                      readOnly={promptView !== "edit"}
                      placeholder={promptTopTab === "preview" ? "正在加载预览…" : "输入自定义角色提示词…"}
                      value={shownPrompt ?? ""}
                      maxLength={100_000}
                      onChange={(event) => {
                        if (promptView === "edit") edit({ system_prompt: event.target.value }, true);
                      }}
                    />

                    <div className="agent-team-prompt-foot">
                      <span>
                        {promptView === "edit"
                          ? (isPromptCustomized ? "已使用自定义提示词，修改内容会自动保存" : "当前展示默认内容，直接修改即可创建自定义覆盖")
                          : promptView === "default"
                          ? "只读查看默认模板"
                          : "此为实际发送给模型的完整提示词拼装结果（含固定规则）"}
                      </span>
                      <span className="agent-team-prompt-count">{(shownPrompt ?? "").length} 字</span>
                    </div>

                    <div className="agent-team-rules-box">
                      <Button
                        type="button"
                        variant="ghost"
                        block
                        className="agent-team-rules-trigger"
                        aria-expanded={rulesOpen}
                        onClick={() => setRulesOpen((value) => !value)}
                      >
                        <IconChevron open={rulesOpen} size={13} />
                        <span className="agent-team-rules-title">系统固定运行规则</span>
                        <span className="agent-team-rules-hint">只读 · 每次运行自动附加在提示词末尾</span>
                        <span className="agent-team-rules-state">{rulesOpen ? "收起" : "展开"}</span>
                      </Button>
                      <Collapse open={rulesOpen}>
                        <pre className="agent-team-runtime-rules" aria-label="固定运行规则">
                          {saved?.runtime_rules}
                        </pre>
                      </Collapse>
                    </div>
                  </div>
                </div>
              </>
            ) : (
              <div className="agent-team-detail-empty">
                {detailError ? (
                  <div className="agent-team-feedback" role="alert">
                    <span>{detailError}</span>
                    <Button size="xs" variant="secondary" onClick={() => { if (selected !== null) void openDetail(selected); }}>
                      重试
                    </Button>
                  </div>
                ) : selected !== null ? (
                  <Empty size="sm" title="正在加载成员详情…" />
                ) : (
                  <Empty size="sm" title="暂无成员" desc="点击「新增成员」创建一个智能体角色" />
                )}
              </div>
            )}
          </div>
        </div>
      </Section>

      {/* 新增成员弹窗 */}
      <Dialog
        open={dialog === "create"}
        onClose={() => { if (!busy) setDialog(null); }}
        size="form"
        title="新增成员"
        footer={
          <>
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => setDialog(null)}>取消</Button>
            <Button
              variant="primary"
              size="sm"
              loading={busy}
              disabled={!newName.trim()}
              onClick={() => void confirmDialog()}
            >
              创建成员
            </Button>
          </>
        }
      >
        <div className="agent-team-dialog-form">
          <Field label="成员名称" required htmlFor={newNameId}>
            <Input
              id={newNameId}
              value={newName}
              maxLength={60}
              placeholder="例如：安全审计员、前端测试助手"
              onChange={(event) => setNewName(event.target.value)}
            />
          </Field>

          <Field
            label="系统执行模式"
            hint={newMode === "readonly"
              ? "只读模式：仅可检索与阅读代码，适合审阅、分析与排查。"
              : "智能体模式：允许编辑文件与执行命令，适合编码与验证。"}
          >
            <Segmented<SubagentExecutionMode>
              block
              size="md"
              aria-label="新成员执行模式"
              options={CREATE_MODE_OPTIONS}
              value={newMode}
              onChange={setNewMode}
            />
          </Field>

          <Field label="职责描述（可选）" hint="便于主代理与工作流判断何时调用" htmlFor={newDescId}>
            <Textarea
              id={newDescId}
              value={newDesc}
              maxLength={300}
              rows={2}
              placeholder="简述该成员负责哪类任务…"
              onChange={(event) => setNewDesc(event.target.value)}
            />
          </Field>

          {dialogError && <p className="agent-team-dialog-error" role="alert">{dialogError}</p>}
        </div>
      </Dialog>

      {/* 删除 / 恢复默认配置 / 还原默认提示词 确认弹窗 */}
      <Dialog
        open={dialog === "delete" || dialog === "reset" || dialog === "resetPrompt"}
        onClose={() => { if (!busy) setDialog(null); }}
        width={400}
        title={dialog === "delete" ? "删除成员" : dialog === "reset" ? "恢复默认配置" : "还原默认提示词"}
        footer={
          <>
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => setDialog(null)}>取消</Button>
            <Button
              variant={dialog === "delete" ? "danger" : "primary"}
              size="sm"
              loading={busy}
              onClick={() => void confirmDialog()}
            >
              {dialog === "delete" ? "删除" : "恢复"}
            </Button>
          </>
        }
      >
        <div className="ui-dialog-message">
          {dialog === "delete"
            ? `删除「${member?.display_name}」后，引用它的旧工作流会回退使用节点的默认成员。`
            : dialog === "reset"
            ? `将「${member?.display_name}」的名称、职责、模型、思考深度、启用状态和提示词全部恢复为默认值？`
            : `确认丢弃「${member?.display_name}」当前的自定义提示词，恢复为系统默认模板吗？`}
        </div>
        {dialogError && <p className="agent-team-dialog-error" role="alert">{dialogError}</p>}
      </Dialog>
    </div>
  );
}
