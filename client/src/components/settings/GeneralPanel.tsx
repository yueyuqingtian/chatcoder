/** 设置中心：常规（v2.2 对齐 zcode 3.18）。
 * 界面语言、HTTP 代理、终端 Shell/字体、增强搜索、消息流显示开关。
 * 所有设置项走 /settings/global 持久化（config.json），重启不丢。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { useUiStore } from "../../store/ui";
import { useChatStore } from "../../store/chat";
import { useI18n } from "../../store/i18n";
import { Button, Input, Select, Slider } from "../ui";
import { Row, Sw } from "./shared";

const TERMINAL_SHELLS = [
  { value: "auto", label: "自动（按平台默认）" },
  { value: "pwsh", label: "PowerShell 7 (pwsh)" },
  { value: "powershell", label: "Windows PowerShell" },
  { value: "cmd", label: "命令提示符 (cmd)" },
  { value: "git-bash", label: "Git Bash" },
];

const MAX_OUTPUT_OPTIONS = [
  { value: 0, label: "模型默认" },
  { value: 8192, label: "8K" },
  { value: 16384, label: "16K" },
  { value: 32768, label: "32K" },
  { value: 64000, label: "64K" },
  { value: 128000, label: "128K" },
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
    browser_enabled: true,
    browser_headless: true,
    // plan-278-1391: 上下文压缩触发阈值（占模型窗口比例，默认 0.90）
    auto_compact_threshold_ratio: 0.90,
    // plan-156-619 S2：消息区搜索入口按钮（默认关闭）
    show_message_search: false,
    // plan-156-621：优先用子会话并行处理的提示词注入（默认开启）
    parallel_subsession_hint: true,
    // 下一步建议卡（默认关闭；开启后智能体 / 工作流模式才注入建议卡工具与提示词）
    suggestion_enabled: false,
    // 采样温度（工具轮 / 文本轮）与单次最大输出；0 输出上限 = 由模型或官方兜底决定
    agent_tool_temperature: 0.3,
    agent_text_temperature: 0.7,
    agent_max_output_tokens: 0,
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

  // 加载失败时禁止写回：此时界面只是默认值，任何改动若落盘都会覆盖已保存的配置（如 HTTP 代理）
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const load = useCallback(async () => {
    setLoadError("");
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
        // plan-20261015-browser-agent：服务端默认开启（AI 操作的是用户看得见的面板浏览器）
        browser_enabled: g.browser_enabled !== false,
        browser_headless: g.browser_headless !== false,
        // plan-278-1391: 压缩触发阈值（后端可能未返回，回退默认 0.90）
        auto_compact_threshold_ratio: typeof g.auto_compact_threshold_ratio === "number"
          ? g.auto_compact_threshold_ratio : 0.90,
        // plan-156-619 S2：搜索入口默认关闭
        show_message_search: g.show_message_search === true,
        // plan-156-621：后端缺省视为开启
        parallel_subsession_hint: g.parallel_subsession_hint !== false,
        // 下一步建议卡：后端缺省视为关闭
        suggestion_enabled: g.suggestion_enabled === true,
        agent_tool_temperature: typeof g.agent_tool_temperature === "number" ? g.agent_tool_temperature : 0.3,
        agent_text_temperature: typeof g.agent_text_temperature === "number" ? g.agent_text_temperature : 0.7,
        agent_max_output_tokens: typeof g.agent_max_output_tokens === "number" ? g.agent_max_output_tokens : 0,
      });
      setLoaded(true);
    } catch (e) {
      setLoadError(String(e));
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  /** S4：改动即保存——本地 state 立即更新，300ms 防抖后写后端（连续拖动只落一次） */
  const patch = (p: Partial<typeof cfg>) => {
    if (!loaded) return; // 未成功加载时拒绝写回
    setCfg((prev) => {
      const next = { ...prev, ...p };
      scheduleSave(next);
      return next;
    });
  };

  // plan-20261020-retry-cred-perf R9：重试次数用草稿态——旧实现 `parseInt(...) || 0`
  // 在「清空重输」时会把 0 防抖落盘，用户看到的重试策略被静默改成不重试。
  const [retryCountDraft, setRetryCountDraft] = useState(String(cfg.agent_retry_count));
  useEffect(() => {
    setRetryCountDraft(String(cfg.agent_retry_count));
  }, [cfg.agent_retry_count]);
  /** 只提交合法整数（0–20）；非法输入回滚为当前生效值。 */
  const commitRetryCount = () => {
    const parsed = Number.parseInt(retryCountDraft, 10);
    if (!Number.isFinite(parsed)) {
      setRetryCountDraft(String(cfg.agent_retry_count));
      return;
    }
    const clamped = Math.min(20, Math.max(0, parsed));
    setRetryCountDraft(String(clamped));
    if (clamped !== cfg.agent_retry_count) patch({ agent_retry_count: clamped });
  };

  // 最大步数同样用草稿态：清空输入不再落盘 0（0 语义为“不限制”，误清空会取消步数上限）
  const [maxStepsDraft, setMaxStepsDraft] = useState(String(cfg.agent_max_steps));
  useEffect(() => {
    setMaxStepsDraft(String(cfg.agent_max_steps));
  }, [cfg.agent_max_steps]);
  const commitMaxSteps = () => {
    const parsed = Number.parseInt(maxStepsDraft, 10);
    if (!Number.isFinite(parsed) || parsed < 0) {
      setMaxStepsDraft(String(cfg.agent_max_steps));
      return;
    }
    setMaxStepsDraft(String(parsed));
    if (parsed !== cfg.agent_max_steps) patch({ agent_max_steps: parsed });
  };

  // 隐藏的性能诊断开关：写本地标记；开启时即时挂载采集器（关闭需重启生效，采集器不卸载）
  const DIAG_FLAG = "chatcoder.diagnostics";
  const [diagOn, setDiagOn] = useState(() => {
    try { return localStorage.getItem(DIAG_FLAG) === "1"; } catch { return false; }
  });
  const toggleDiagnostics = () => {
    const next = !diagOn;
    try { localStorage.setItem(DIAG_FLAG, next ? "1" : "0"); } catch { /* 存储不可用：仅本次会话生效 */ }
    setDiagOn(next);
    if (next) void import("../../perf/metrics").then((m) => m.install());
  };

  return (
    <div
      className="settings-card-stack sc-stack"
      style={loaded ? undefined : { pointerEvents: "none", opacity: loadError ? 0.5 : 1 }}
    >
      {loadError && (
        <div
          className="settings-card"
          role="alert"
          style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "var(--sp-3)", padding: "var(--sp-3) var(--sp-4)" }}
        >
          <span style={{ color: "var(--warning)" }}>
            设置加载失败，当前显示的是默认值；为避免覆盖已保存的配置，修改已暂停保存。
          </span>
          <Button size="sm" variant="secondary" onClick={() => void load()}>重试</Button>
        </div>
      )}
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
        {/* plan-156-619 S2：消息区搜索入口开关。默认关闭——入口按钮此前常驻右上角，
            与综合工具卡抢位；关闭只隐藏按钮，Ctrl+F 仍可唤起搜索条。 */}
        <Row title={t("gp.message_search")} desc={t("gp.message_search_desc")}>
          <Sw checked={cfg.show_message_search ?? false} onChange={(v) => patch({ show_message_search: v })} />
        </Row>
        {/* plan-156-621：并行分派子会话的提示词开关。关闭后不再注入；
            plan-156-623：计划模式下不下发子会话工具，但会要求把可并行步骤分组写进方案文档，
            确认执行时按分组派发（只读 / 工作流模式仍不生效）。 */}
        <Row title={t("gp.parallel_subsession")} desc={t("gp.parallel_subsession_desc")}>
          <Sw checked={cfg.parallel_subsession_hint} onChange={(v) => patch({ parallel_subsession_hint: v })} />
        </Row>
        <Row title={t("gp.suggestion")} desc={t("gp.suggestion_desc")}>
          <Sw checked={cfg.suggestion_enabled} onChange={(v) => patch({ suggestion_enabled: v })} />
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
                value={maxStepsDraft}
                onChange={(e) => setMaxStepsDraft(e.target.value)}
                onBlur={commitMaxSteps}
                onKeyDown={(e) => { if (e.key === "Enter") commitMaxSteps(); }}
                aria-label="自定义最大步数"
              />
            )}
          </div>
        </Row>
        {/* 采样温度与单次最大输出：修改即保存。部分模型（Claude 系、思考模式）由适配器固定，不受此项影响 */}
        <Row title="工具轮温度" desc="工具调用轮次的采样温度（0–2）。数值越低输出越稳定，代码修改类任务通常偏低。">
          <Slider
            min={0}
            max={2}
            step={0.05}
            value={cfg.agent_tool_temperature}
            onChange={(v) => patch({ agent_tool_temperature: v })}
            format={(v) => v.toFixed(2)}
            aria-label="工具轮温度"
          />
        </Row>
        <Row title="文本轮温度" desc="不调用工具时（回答、解释等）的采样温度（0–2）。">
          <Slider
            min={0}
            max={2}
            step={0.05}
            value={cfg.agent_text_temperature}
            onChange={(v) => patch({ agent_text_temperature: v })}
            format={(v) => v.toFixed(2)}
            aria-label="文本轮温度"
          />
        </Row>
        <Row title="单次最大输出" desc="限制单次回复的最大长度。选择“模型默认”时，官方直连按 128K 兜底。">
          <Select
            style={{ minWidth: 160 }}
            value={String(cfg.agent_max_output_tokens)}
            onChange={(val) => patch({ agent_max_output_tokens: Number(val) })}
            options={[
              ...MAX_OUTPUT_OPTIONS.map((o) => ({ value: String(o.value), label: o.label })),
              ...(!MAX_OUTPUT_OPTIONS.some((o) => o.value === cfg.agent_max_output_tokens)
                ? [{ value: String(cfg.agent_max_output_tokens), label: `自定义 (${cfg.agent_max_output_tokens})` }]
                : []),
            ]}
            aria-label="单次最大输出"
          />
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
            max={20}
            value={retryCountDraft}
            onChange={(e) => setRetryCountDraft(e.target.value)}
            onBlur={commitRetryCount}
            onKeyDown={(e) => { if (e.key === "Enter") commitRetryCount(); }}
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
        <BrowserDownloadDirRow />
        <Row title={t("gp.density")} desc={t("gp.density_desc")}>
          <Select
            value={ui.msgDensity}
            onChange={(v) => ui.setPrefs({ msgDensity: v as "comfortable" | "compact" })}
            options={[{ value: "comfortable", label: "舒适" }, { value: "compact", label: "紧凑" }]}
            aria-label={t("gp.density")}
          />
        </Row>
      </div>

      {/* 隐藏的性能诊断入口：开启后下次启动（或即时）采集卡顿指标，供反馈问题时导出；默认关闭 */}
      <div className="settings-resource-desc" style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "flex-end" }}>
        <span>{diagOn ? "性能诊断已开启（采集卡顿指标）" : ""}</span>
        <Button variant="ghost" size="xs" onClick={toggleDiagnostics}>
          {diagOn ? "关闭性能诊断" : "性能诊断"}
        </Button>
      </div>
    </div>
  );
}

/** plan-20261018-operator-boost：内置浏览器下载目录。
 * 下载不再弹系统保存框，统一保存到这里（默认安装目录下的 downloads，不存在会自动创建）；
 * 目录由 Electron 主进程持有（下载拦截发生在主进程），因此只在桌面端显示。选择目录即保存。 */
function BrowserDownloadDirRow() {
  const { t } = useI18n();
  const exec = typeof window !== "undefined" ? window.chatcoderAPI?.browserAgentExec : undefined;
  const pick = typeof window !== "undefined" ? window.chatcoderAPI?.selectDirectory : undefined;
  const [info, setInfo] = useState<{ dir: string; custom: boolean; fallback: boolean } | null>(null);

  const run = useCallback(async (args: Record<string, unknown>) => {
    if (!exec) return;
    try {
      const r = await exec(0, "downloadDir", args);
      if (!r?.ok) throw new Error(r?.error || "操作失败");
      const v = r.result as { dir: string; custom: boolean; fallback: boolean };
      setInfo({ dir: String(v.dir || ""), custom: !!v.custom, fallback: !!v.fallback });
    } catch (e) {
      useChatStore.setState({ error: String((e as Error)?.message || e) });
    }
  }, [exec]);

  useEffect(() => { void run({ action: "get" }); }, [run]);
  if (!exec) return null;

  const where = info ? info.dir + (info.custom ? "" : t("gp.download_dir_default_tag")) : "…";
  return (
    <Row
      title={t("gp.download_dir")}
      desc={`${t("gp.download_dir_desc")}${where}${info?.fallback ? t("gp.download_dir_fallback") : ""}`}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "var(--sp-2)" }}>
        {pick && (
          <Button variant="secondary" size="sm" onClick={async () => {
            const dir = await pick();
            if (dir) await run({ action: "set", dir });
          }}>{t("gp.download_dir_change")}</Button>
        )}
        <Button variant="ghost" size="sm" onClick={() => void run({ action: "open" })}>{t("gp.download_dir_open")}</Button>
        {info?.custom && (
          <Button variant="ghost" size="sm" onClick={() => void run({ action: "set", dir: "" })}>
            {t("gp.download_dir_reset")}
          </Button>
        )}
      </div>
    </Row>
  );
}
