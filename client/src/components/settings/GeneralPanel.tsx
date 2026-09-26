/** 设置中心：常规（v2.2 对齐 zcode 3.18）。
 * 界面语言、HTTP 代理、终端 Shell/字体、增强搜索、消息流显示开关。
 * 所有设置项走 /settings/global 持久化（config.json），重启不丢。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { useUiStore } from "../../store/ui";
import { useChatStore } from "../../store/chat";
import { useI18n } from "../../store/i18n";
import { Input, Select, Slider } from "../ui";
import { Row, Sw } from "./shared";

const TERMINAL_SHELLS = [
  { value: "auto", label: "自动（按平台默认）" },
  { value: "pwsh", label: "PowerShell 7 (pwsh)" },
  { value: "powershell", label: "Windows PowerShell" },
  { value: "cmd", label: "命令提示符 (cmd)" },
  { value: "git-bash", label: "Git Bash" },
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
    // plan-75-332：原「自动批准工具调用 / 始终需要审批的工具 / 沙箱模式 /
    // 工作目录外读取自动审批 / 计划模式外部访问」五项已移除——是否需审批统一由
    // 权限模式（询问审批 / 自动审批 / 完全访问）裁决，入口在输入框右侧。
    memory_enabled: true,
    agent_max_steps: 1000,
    agent_retry_count: 3,
    agent_retry_intervals: "10,20,30",
    browser_enabled: false,
    browser_headless: true,
    // plan-278-1391: 上下文压缩触发阈值（占模型窗口比例，默认 0.90）
    auto_compact_threshold_ratio: 0.90,
  });
  // S4（plan-41-197）：改为「修改即保存」——移除底部保存按钮，所有项在改动后
  // 300ms 内自动落盘（config.json），与外观/模型等页操作逻辑统一（用户反馈
  // “常规下面有个保存按钮，和其余标签页的操作逻辑不一致”）。
  const saveTimerRef = useRef<number | null>(null);
  const flushSave = useCallback(async (payload: typeof cfg) => {
    try {
      await api.setGlobalSettings({
        ...payload,
        // plan-278-1391: 压缩触发阈值（保留两位小数，避免浮点噪声）
        auto_compact_threshold_ratio: Math.round(payload.auto_compact_threshold_ratio * 100) / 100,
      });
      // v1.1: 保存即生效——刷新 todos/reasoning 显示开关
      await useUiStore.getState().refreshGlobalFlags();
    } catch (e) { useChatStore.setState({ error: "保存失败: " + String(e) }); }
  }, []);
  const scheduleSave = useCallback((next: typeof cfg) => {
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => { void flushSave(next); }, 300);
  }, [flushSave]);
  useEffect(() => () => { if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current); }, []);
  // 问题7/8: 终端 Shell/字体选项（后端按当前设备存在性探测）
  const [shells, setShells] = useState(TERMINAL_SHELLS);
  const [fonts, setFonts] = useState<{ value: string; label: string }[]>([]);

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
  }, []);

  /** plan-282-1416：下拉选项改为 Select 组件的 options 数组——
   *  「已选值不在候选列表中时保留显示」的逻辑在此保留（追加为第一项）。 */
  const shellOptions = useMemo(() => {
    const base = shells.map((s) => ({ value: s.value, label: s.label }));
    if (!shells.some((s) => s.value === cfg.terminal_shell)) {
      base.unshift({ value: cfg.terminal_shell, label: cfg.terminal_shell });
    }
    return base;
  }, [shells, cfg.terminal_shell]);

  const fontOptions = useMemo(() => {
    const list = fonts.length ? fonts : [{ value: "", label: "继承系统终端字体" }];
    const base = list.map((f) => ({ value: f.value, label: f.label }));
    if (!list.some((f) => f.value === cfg.terminal_font)) {
      base.unshift({ value: cfg.terminal_font, label: cfg.terminal_font || "继承系统终端字体" });
    }
    return base;
  }, [fonts, cfg.terminal_font]);

  const load = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setCfg({
        terminal_shell: g.terminal_shell || "auto",
        terminal_font: g.terminal_font || "",
        http_proxy: g.http_proxy || "",
        enhanced_search: g.enhanced_search,
        show_reasoning: g.show_reasoning,
        memory_enabled: g.memory_enabled !== false,
        agent_max_steps: typeof g.agent_max_steps === "number" ? g.agent_max_steps : 1000,
        agent_retry_count: typeof g.agent_retry_count === "number" ? g.agent_retry_count : 3,
        agent_retry_intervals: typeof g.agent_retry_intervals === "string" ? g.agent_retry_intervals : "10,20,30",
        browser_enabled: g.browser_enabled === true,
        browser_headless: g.browser_headless !== false,
        // plan-278-1391: 压缩触发阈值（后端可能未返回，回退默认 0.90）
        auto_compact_threshold_ratio: typeof g.auto_compact_threshold_ratio === "number"
          ? g.auto_compact_threshold_ratio : 0.90,
      });
    } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);

  /** S4：改动即保存——本地 state 立即更新，300ms 防抖后写后端（连续拖动只落一次） */
  const patch = (p: Partial<typeof cfg>) => setCfg((prev) => {
    const next = { ...prev, ...p };
    scheduleSave(next);
    return next;
  });

  return (
    <div className="settings-card-stack">
      <div className="settings-card">
        <Row title={t("gp.language")} desc={t("gp.language_desc")}>
          <Select
            value={ui.language}
            onChange={(v) => ui.setLanguage(v as "zh" | "en")}
            options={[{ value: "zh", label: "中文" }, { value: "en", label: "English" }]}
            aria-label={t("gp.language")}
          />
        </Row>
        <Row title={t("gp.http_proxy")} desc={t("gp.http_proxy_desc")}>
          <Input placeholder="如 http://127.0.0.1:7890" value={cfg.http_proxy} onChange={(e) => patch({ http_proxy: e.target.value })} style={{ minWidth: 220 }} />
        </Row>
        <Row title={t("gp.terminal_shell")} desc={t("gp.terminal_shell_desc")}>
          {/* 问题7: 仅列出当前设备实际存在的 Shell；已选值不在列表中时保留显示（见 shellOptions） */}
          <Select value={cfg.terminal_shell} onChange={(v) => patch({ terminal_shell: v })} options={shellOptions} style={{ minWidth: 200 }} aria-label={t("gp.terminal_shell")} />
        </Row>
        <Row title={t("gp.terminal_font")} desc={t("gp.terminal_font_desc")}>
          {/* 问题8: 字体改为下拉候选；已选自定义值不在列表中时保留显示（见 fontOptions） */}
          <Select value={cfg.terminal_font} onChange={(v) => patch({ terminal_font: v })} options={fontOptions} style={{ minWidth: 200 }} aria-label={t("gp.terminal_font")} />
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
        <Row title={t("gp.max_steps")} desc={t("gp.max_steps_desc")}>
          <div style={{ display: "flex", gap: "var(--sp-3)", alignItems: "center" }}>
            <Select
              style={{ minWidth: 160 }}
              value={String([200, 500, 1000, 0].includes(cfg.agent_max_steps) ? cfg.agent_max_steps : "custom")}
              onChange={(val) => {
                if (val === "custom") return;
                patch({ agent_max_steps: Number(val) });
              }}
              options={[
                ...MAX_STEPS_OPTIONS.map((o) => ({ value: String(o.value), label: o.label })),
                ...(![200, 500, 1000, 0].includes(cfg.agent_max_steps)
                  ? [{ value: "custom", label: `自定义 (${cfg.agent_max_steps} 步)` }]
                  : []),
              ]}
              aria-label={t("gp.max_steps")}
            />
            {![200, 500, 1000, 0].includes(cfg.agent_max_steps) && (
              <Input
                type="number"
                style={{ width: 90 }}
                min={0}
                value={cfg.agent_max_steps}
                onChange={(e) => patch({ agent_max_steps: Math.max(0, parseInt(e.target.value) || 0) })}
                aria-label="自定义最大步数"
              />
            )}
          </div>
        </Row>
        {/* plan-278-1391: 上下文压缩触发阈值——占模型上下文窗口比例，达阈值即自动压缩 */}
        <Row title={t("gp.compact_threshold")} desc={t("gp.compact_threshold_desc")}>
          <Slider
            min={50}
            max={95}
            step={5}
            value={Math.round(cfg.auto_compact_threshold_ratio * 100)}
            onChange={(v) => patch({ auto_compact_threshold_ratio: v / 100 })}
            format={(v) => `${v}%`}
            aria-label={t("gp.compact_threshold")}
          />
        </Row>
        {/* v45: 异常自动重试策略——任何报错按间隔依次重试，穷尽后才停止并显示报错 */}
        <Row title={t("gp.retry_count")} desc={t("gp.retry_count_desc")}>
          <Input
            type="number"
            style={{ width: 90 }}
            min={0}
            max={10}
            value={cfg.agent_retry_count}
            onChange={(e) => patch({ agent_retry_count: Math.max(0, parseInt(e.target.value) || 0) })}
            aria-label={t("gp.retry_count")}
          />
        </Row>
        <Row title={t("gp.retry_intervals")} desc={t("gp.retry_intervals_desc")}>
          <Input
            type="text"
            style={{ width: 160 }}
            placeholder="10,20,30"
            value={cfg.agent_retry_intervals}
            onChange={(e) => patch({ agent_retry_intervals: e.target.value })}
            aria-label={t("gp.retry_intervals")}
          />
        </Row>
        <Row title={t("gp.browser")} desc={t("gp.browser_desc")}>
          <Sw checked={cfg.browser_enabled} onChange={(v) => patch({ browser_enabled: v })} />
        </Row>
        <Row
          className={cfg.browser_enabled ? undefined : "settings-row-disabled"}
          title={t("gp.browser_headless")}
          desc={t("gp.browser_headless_desc")}
        >
          <Sw
            checked={cfg.browser_headless}
            onChange={(v) => { if (cfg.browser_enabled) patch({ browser_headless: v }); }}
          />
        </Row>
        <Row title={t("gp.density")} desc={t("gp.density_desc")}>
          <Select
            value={ui.msgDensity}
            onChange={(v) => ui.setPrefs({ msgDensity: v as "comfortable" | "compact" })}
            options={[{ value: "comfortable", label: "舒适" }, { value: "compact", label: "紧凑" }]}
            aria-label={t("gp.density")}
          />
        </Row>
      </div>

    </div>
  );
}
