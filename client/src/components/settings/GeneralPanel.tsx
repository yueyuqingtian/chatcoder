/** 设置中心：常规（v2.2 对齐 zcode 3.18）。
 * 界面语言、HTTP 代理、终端 Shell/字体、增强搜索、消息流显示开关。
 * 所有设置项走 /settings/global 持久化（config.json），重启不丢。 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import { useUiStore } from "../../store/ui";
import { useChatStore } from "../../store/chat";
import { useI18n } from "../../store/i18n";
import { Row, Sw } from "./shared";

const TERMINAL_SHELLS = [
  { value: "auto", label: "自动（按平台默认）" },
  { value: "pwsh", label: "PowerShell 7 (pwsh)" },
  { value: "powershell", label: "Windows PowerShell" },
  { value: "cmd", label: "命令提示符 (cmd)" },
  { value: "git-bash", label: "Git Bash" },
];

const SANDBOX_MODES = [
  { value: "workspace-write", label: "工作区写访问（默认）" },
  { value: "read-only", label: "只读沙箱（仅读操作）" },
  { value: "danger-full-access", label: "危险全访问（免审批）" },
];

const MAX_STEPS_OPTIONS = [
  { value: 200, label: "200 步" },
  { value: 500, label: "500 步" },
  { value: 1000, label: "1000 步（默认）" },
  { value: 0, label: "不限制步数" },
];

export function GeneralPanel() {
  const ui = useUiStore();
  const { t } = useI18n();
  const [cfg, setCfg] = useState({
    terminal_shell: "auto", terminal_font: "", http_proxy: "",
    enhanced_search: true, show_reasoning: true,
    auto_approve_tools: false, force_approval_tools: "",
    memory_enabled: true,
    plan_mode_allow_outside_access: false,
    sandbox_mode: "workspace-write",
    agent_max_steps: 1000,
    browser_enabled: false,
    browser_headless: true,
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  // 问题7/8: 终端 Shell/字体选项（后端按当前设备存在性探测）
  const [shells, setShells] = useState(TERMINAL_SHELLS);
  const [fonts, setFonts] = useState<{ value: string; label: string }[]>([]);
  // 问题9: 始终需要审批的工具——可用工具列表 + 已选标签
  const [toolNames, setToolNames] = useState<string[]>([]);
  const [toolDraft, setToolDraft] = useState("");
  const [toolOpen, setToolOpen] = useState(false);

  useEffect(() => {
    api.getTerminalOptions()
      .then((o) => {
        setFonts(o.fonts || []);
        const avail = (o.shells || [])
          .filter((s) => s.available || s.value === "auto")
          .map((s) => ({ value: s.value, label: s.label }));
        if (avail.length > 0) setShells(avail);
      })
      .catch(() => { /* 拉取失败沿用硬编码候选 */ });
    api.listExecPolicyTools()
      .then((list) => setToolNames(list.map((t) => t.name)))
      .catch(() => { /* 失败则工具下拉为空，仅支持手动输入 */ });
  }, []);

  /** 问题9: 已选审批工具列表（以逗号字符串与 cfg.force_approval_tools 同步） */
  const forceTools = useMemo(
    () => cfg.force_approval_tools.split(",").map((s) => s.trim()).filter(Boolean),
    [cfg.force_approval_tools],
  );
  const setForceTools = (next: string[]) => patch({ force_approval_tools: next.join(",") });
  const addForceTool = (name: string) => {
    const t = name.trim();
    if (!t) return;
    if (forceTools.includes(t)) { setToolDraft(""); return; }
    setForceTools([...forceTools, t]);
    setToolDraft("");
  };
  const removeForceTool = (name: string) => setForceTools(forceTools.filter((t) => t !== name));
  const filteredTools = useMemo(
    () => toolNames.filter((n) => !forceTools.includes(n) && n.toLowerCase().includes(toolDraft.trim().toLowerCase())),
    [toolNames, forceTools, toolDraft],
  );

  const load = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setCfg({
        terminal_shell: g.terminal_shell || "auto",
        terminal_font: g.terminal_font || "",
        http_proxy: g.http_proxy || "",
        enhanced_search: g.enhanced_search,
        show_reasoning: g.show_reasoning,
        auto_approve_tools: g.auto_approve_tools,
        force_approval_tools: g.force_approval_tools || "",
        memory_enabled: g.memory_enabled !== false,
        plan_mode_allow_outside_access: g.plan_mode_allow_outside_access === true,
        sandbox_mode: g.sandbox_mode || "workspace-write",
        agent_max_steps: typeof g.agent_max_steps === "number" ? g.agent_max_steps : 1000,
        browser_enabled: g.browser_enabled === true,
        browser_headless: g.browser_headless !== false,
      });
    } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);

  const patch = (p: Partial<typeof cfg>) => setCfg((prev) => ({ ...prev, ...p }));

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.setGlobalSettings({
        terminal_shell: cfg.terminal_shell,
        terminal_font: cfg.terminal_font,
        http_proxy: cfg.http_proxy,
        enhanced_search: cfg.enhanced_search,
        show_reasoning: cfg.show_reasoning,
        auto_approve_tools: cfg.auto_approve_tools,
        force_approval_tools: cfg.force_approval_tools,
        memory_enabled: cfg.memory_enabled,
        plan_mode_allow_outside_access: cfg.plan_mode_allow_outside_access,
        sandbox_mode: cfg.sandbox_mode,
        agent_max_steps: cfg.agent_max_steps,
        browser_enabled: cfg.browser_enabled,
        browser_headless: cfg.browser_headless,
      });
      // v1.1: 保存即生效——刷新 todos/reasoning 显示开关
      await useUiStore.getState().refreshGlobalFlags();
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
    } catch (e) { useChatStore.setState({ error: "保存失败: " + String(e) }); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <div className="settings-card">
        <Row title={t("gp.language")} desc={t("gp.language_desc")}>
          <select className="ui-select" value={ui.language} onChange={(e) => ui.setLanguage(e.target.value as "zh" | "en")}>
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </Row>
        <Row title={t("gp.http_proxy")} desc={t("gp.http_proxy_desc")}>
          <input className="ui-input" placeholder="如 http://127.0.0.1:7890" value={cfg.http_proxy} onChange={(e) => patch({ http_proxy: e.target.value })} />
        </Row>
        <Row title={t("gp.terminal_shell")} desc={t("gp.terminal_shell_desc")}>
          <select className="ui-select" value={cfg.terminal_shell} onChange={(e) => patch({ terminal_shell: e.target.value })}>
            {/* 问题7: 仅列出当前设备实际存在的 Shell；已选值不在列表中时保留显示 */}
            {!shells.some((s) => s.value === cfg.terminal_shell) && (
              <option value={cfg.terminal_shell}>{cfg.terminal_shell}</option>
            )}
            {shells.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </Row>
        <Row title={t("gp.terminal_font")} desc={t("gp.terminal_font_desc")}>
          <select className="ui-select" value={cfg.terminal_font} onChange={(e) => patch({ terminal_font: e.target.value })}>
            {/* 问题8: 字体改为下拉候选；已选自定义值不在列表中时保留显示 */}
            {!fonts.some((f) => f.value === cfg.terminal_font) && (
              <option value={cfg.terminal_font}>{cfg.terminal_font || "继承系统终端字体"}</option>
            )}
            {(fonts.length ? fonts : [{ value: "", label: "继承系统终端字体" }]).map((f) => (
              <option key={f.value} value={f.value}>{f.label}</option>
            ))}
          </select>
        </Row>
      </div>

      <div className="settings-card">
        <Row title={t("gp.enhanced_search")} desc={t("gp.enhanced_search_desc")}>
          <Sw checked={cfg.enhanced_search} onChange={(v) => patch({ enhanced_search: v })} />
        </Row>
        <Row title={t("gp.memory")} desc={t("gp.memory_desc")}>
          <Sw checked={cfg.memory_enabled} onChange={(v) => patch({ memory_enabled: v })} />
        </Row>
        <Row title={t("gp.reasoning")} desc={t("gp.reasoning_desc")}>
          <Sw checked={cfg.show_reasoning} onChange={(v) => patch({ show_reasoning: v })} />
        </Row>
        <Row title={t("gp.auto_approve")} desc={t("gp.auto_approve_desc")}>
          <Sw checked={cfg.auto_approve_tools} onChange={(v) => patch({ auto_approve_tools: v })} />
        </Row>
        <Row title={t("gp.force_approve")} desc={t("gp.force_approve_desc")}>
          <div className="approval-tool-selector">
            {forceTools.map((t) => (
              <span key={t} className="approval-tool-chip">
                {t}
                <button type="button" className="approval-tool-chip-remove" onClick={() => removeForceTool(t)} title="移除">×</button>
              </span>
            ))}
            <input
              className="approval-tool-input"
              value={toolDraft}
              placeholder={forceTools.length ? "" : "选择或输入工具名…"}
              onChange={(e) => { setToolDraft(e.target.value); setToolOpen(true); }}
              onFocus={() => setToolOpen(true)}
              onBlur={() => setTimeout(() => setToolOpen(false), 150)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === ",") {
                  e.preventDefault();
                  if (toolDraft.trim()) addForceTool(toolDraft);
                } else if (e.key === "Escape") {
                  setToolOpen(false);
                }
              }}
            />
            {toolOpen && (filteredTools.length > 0 || toolDraft.trim()) && (
              <div className="approval-tool-dropdown">
                {filteredTools.map((n) => (
                  <button
                    key={n}
                    type="button"
                    className="approval-tool-option"
                    onMouseDown={() => addForceTool(n)}
                  >
                    {n}
                  </button>
                ))}
                {toolDraft.trim() && !toolNames.includes(toolDraft.trim()) && (
                  <button
                    type="button"
                    className="approval-tool-option custom"
                    onMouseDown={() => addForceTool(toolDraft)}
                  >
                    添加「{toolDraft.trim()}」
                  </button>
                )}
              </div>
            )}
          </div>
        </Row>
        <Row title={t("gp.sandbox")} desc={t("gp.sandbox_desc")}>
          <select
            className="ui-select"
            value={cfg.sandbox_mode}
            onChange={(e) => patch({ sandbox_mode: e.target.value as typeof cfg.sandbox_mode })}
          >
            {SANDBOX_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </Row>
        <Row title={t("gp.max_steps")} desc={t("gp.max_steps_desc")}>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              className="ui-select"
              style={{ minWidth: 140 }}
              value={[200, 500, 1000, 0].includes(cfg.agent_max_steps) ? cfg.agent_max_steps : "custom"}
              onChange={(e) => {
                const val = e.target.value;
                if (val === "custom") return;
                patch({ agent_max_steps: Number(val) });
              }}
            >
              {MAX_STEPS_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
              {![200, 500, 1000, 0].includes(cfg.agent_max_steps) && (
                <option value="custom">自定义 ({cfg.agent_max_steps} 步)</option>
              )}
            </select>
            {![200, 500, 1000, 0].includes(cfg.agent_max_steps) && (
              <input
                type="number"
                className="ui-input"
                style={{ width: 90 }}
                min={0}
                value={cfg.agent_max_steps}
                onChange={(e) => patch({ agent_max_steps: Math.max(0, parseInt(e.target.value) || 0) })}
              />
            )}
          </div>
        </Row>
        <Row title={t("gp.browser")} desc={t("gp.browser_desc")}>
          <Sw checked={cfg.browser_enabled} onChange={(v) => patch({ browser_enabled: v })} />
        </Row>
        {cfg.browser_enabled && (
          <Row title={t("gp.browser_headless")} desc={t("gp.browser_headless_desc")}>
            <Sw checked={cfg.browser_headless} onChange={(v) => patch({ browser_headless: v })} />
          </Row>
        )}
        <Row title={t("gp.plan_outside")} desc={t("gp.plan_outside_desc")}>
          <Sw checked={cfg.plan_mode_allow_outside_access} onChange={(v) => patch({ plan_mode_allow_outside_access: v })} />
        </Row>
        <Row title={t("gp.density")} desc={t("gp.density_desc")}>
          <select
            className="ui-select"
            value={ui.msgDensity}
            onChange={(e) => ui.setPrefs({ msgDensity: e.target.value as "comfortable" | "compact" })}
          >
            <option value="comfortable">舒适</option>
            <option value="compact">紧凑</option>
          </select>
        </Row>
      </div>

      <div className="settings-create-actions" style={{ marginTop: 12 }}>
        <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>
          {saving ? t("gp.saving") : saved ? t("gp.saved") : t("gp.save")}
        </button>
      </div>
    </div>
  );
}
