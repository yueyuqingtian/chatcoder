/** 设置中心：常规（v2.2 对齐 zcode 3.18）。
 * 界面语言、HTTP 代理、终端 Shell / 字体、增强搜索、消息区与智能体的运行开关、异常自动重试等。
 * 所有设置项「修改即保存」（300ms 防抖），走 /settings/global 持久化（config.json），重启不丢。
 * 外观（主题、字号、行距、宽度）见「外观」页；电脑操控与内置浏览器见「电脑操控」页；子代理见「子代理」页。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type TerminalShellOption } from "../../api/client";
import { useUiStore } from "../../store/ui";
import { useChatStore } from "../../store/chat";
import { useI18n } from "../../store/i18n";
import { isFontInstalled } from "../../utils/fontProbe";
import { Button, Input, Select, Slider, TagInput, type SelectOption } from "../ui";
import { Row, Sw } from "./shared";

/** 终端 Shell 兜底列表（后端探测结果未返回时使用，此时全部视为可用） */
const TERMINAL_SHELLS: Array<{ value: string; label: string }> = [
  { value: "auto", label: "自动（按平台默认）" },
  { value: "pwsh", label: "PowerShell 7 (pwsh)" },
  { value: "powershell", label: "Windows PowerShell" },
  { value: "cmd", label: "命令提示符 (cmd)" },
  { value: "git-bash", label: "Git Bash" },
];

/** 终端字体候选（等宽）。JetBrains Mono 随应用内置，始终可用；其余字体取决于本机是否安装。 */
const DEFAULT_TERMINAL_FONT = "JetBrains Mono";
const TERMINAL_FONTS: Array<{ value: string; label: string; bundled?: boolean }> = [
  { value: DEFAULT_TERMINAL_FONT, label: "JetBrains Mono（内置）", bundled: true },
  { value: "Cascadia Code", label: "Cascadia Code" },
  { value: "Consolas", label: "Consolas" },
  { value: "Fira Code", label: "Fira Code" },
  { value: "Source Code Pro", label: "Source Code Pro" },
  { value: "Sarasa Mono SC", label: "更纱等宽 (Sarasa Mono SC)" },
];

/** 异常重试间隔：每个标签是一次重试前的等待秒数（整数 0–3600），标签个数即重试次数（最多 20） */
const RETRY_MAX_COUNT = 20;
const RETRY_MAX_SECONDS = 3600;

function validateRetrySeconds(raw: string): string | null {
  if (!/^\d+$/.test(raw)) return "请输入整数秒数（0–3600）";
  if (Number(raw) > RETRY_MAX_SECONDS) return `最长 ${RETRY_MAX_SECONDS} 秒`;
  return null;
}

/** 把后端保存的间隔串（逗号 / 中文标点分隔）拆成标签；不合法的项丢弃，与服务端口径一致 */
function parseRetryIntervals(raw: string): string[] {
  return raw
    .split(/[,，、；;\s]+/)
    .map((s) => s.trim())
    .filter((s) => /^\d+$/.test(s) && Number(s) <= RETRY_MAX_SECONDS)
    .slice(0, RETRY_MAX_COUNT);
}

export function GeneralPanel() {
  const ui = useUiStore();
  const { t } = useI18n();
  const [cfg, setCfg] = useState({
    terminal_shell: "auto", terminal_font: DEFAULT_TERMINAL_FONT, http_proxy: "",
    enhanced_search: true,
    show_message_search: false,
    suggestion_enabled: false,
    agent_retry_intervals: "10,20,30",
    // plan-278-1391: 上下文压缩触发阈值（占模型窗口比例，默认 0.90）
    auto_compact_threshold_ratio: 0.90,
    // 采样温度（工具轮 / 文本轮）与单次最大输出；0 输出上限 = 由模型或官方兜底决定
    agent_tool_temperature: 0.3,
    agent_text_temperature: 0.7,
    agent_max_output_tokens: 0,
  });
  /** 增强搜索实际使用的 rg 来源（只读，由后端探测；不随设置一起回写） */
  const [rgSource, setRgSource] = useState<"bundled" | "path" | "none" | undefined>(undefined);

  // S4（plan-41-197）：改为「修改即保存」——移除底部保存按钮，所有项在改动后
  // 300ms 内自动落盘（config.json），与外观/模型等页操作逻辑统一。
  const saveTimerRef = useRef<number | null>(null);
  /** plan-568-2333：防抖窗口内待落盘的最新配置。此前卸载只 clearTimeout，改完 300ms 内
   *  切页/关设置就丢掉最后一笔（违反「修改即保存」），这里记下来在卸载时补发。 */
  const pendingSaveRef = useRef<typeof cfg | null>(null);
  const flushSave = useCallback(async (payload: typeof cfg) => {
    try {
      await api.setGlobalSettings({
        ...payload,
        // plan-278-1391: 压缩触发阈值（保留两位小数，避免浮点噪声）
        auto_compact_threshold_ratio: Math.round(payload.auto_compact_threshold_ratio * 100) / 100,
      });
      // 保存即生效：刷新 todos / 搜索入口 / 终端字体等全局显示开关
      await useUiStore.getState().refreshGlobalFlags();
    } catch (e) { useChatStore.setState({ error: "保存失败: " + String(e) }); }
  }, []);
  const scheduleSave = useCallback((next: typeof cfg) => {
    pendingSaveRef.current = next;
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => {
      saveTimerRef.current = null;
      pendingSaveRef.current = null;
      void flushSave(next);
    }, 300);
  }, [flushSave]);
  // plan-568-2333：卸载（切页 / 关闭设置）时把窗口里的最后一笔补发出去
  useEffect(() => () => {
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = null;
    const pend = pendingSaveRef.current;
    pendingSaveRef.current = null;
    if (pend) void flushSave(pend);
  }, [flushSave]);

  // 终端 Shell：后端按本机实际安装情况给出可用性，未安装的置灰并标注
  const [shells, setShells] = useState<TerminalShellOption[] | null>(null);
  useEffect(() => {
    api.getTerminalOptions()
      .then((o) => setShells(o.shells?.length ? o.shells : null))
      .catch(() => setShells(null));
  }, []);
  const shellOptions = useMemo<SelectOption[]>(() => {
    const list: Array<{ value: string; label: string; available: boolean }> =
      shells ?? TERMINAL_SHELLS.map((s) => ({ ...s, available: true }));
    return list.map((s) => ({
      value: s.value,
      label: s.label,
      disabled: !s.available,
      hint: s.available ? undefined : "未安装",
    }));
  }, [shells]);
  const shellMissing = !!shells && shells.some((s) => s.value === cfg.terminal_shell && !s.available);

  // 终端字体：内置字体恒可用；其余按本机是否安装（渲染端 canvas 探测）置灰并标注
  const fontOptions = useMemo<SelectOption[]>(() => {
    const opts: SelectOption[] = TERMINAL_FONTS.map((f) => {
      const ok = !!f.bundled || isFontInstalled(f.value);
      return { value: f.value, label: f.label, disabled: !ok, hint: ok ? undefined : "未安装" };
    });
    if (cfg.terminal_font && !opts.some((o) => o.value === cfg.terminal_font)) {
      const ok = isFontInstalled(cfg.terminal_font);
      opts.push({ value: cfg.terminal_font, label: cfg.terminal_font, disabled: !ok, hint: ok ? undefined : "未安装" });
    }
    return opts;
  }, [cfg.terminal_font]);

  const rgDesc = rgSource === "bundled"
    ? "使用随应用内置的 ripgrep 做全库文本搜索，速度更快。"
    : rgSource === "path"
      ? "使用本机 PATH 中的 ripgrep（rg）做全库文本搜索。"
      : rgSource === "none"
        ? "未找到 ripgrep（rg），当前使用内置的 Python 逐行搜索。安装 ripgrep 并重启应用后会自动启用。"
        : "全库文本搜索优先使用 ripgrep（rg），未找到时回退为内置的 Python 逐行搜索。";

  // 加载失败时禁止写回：此时界面只是默认值，任何改动若落盘都会覆盖已保存的配置（如 HTTP 代理）
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const load = useCallback(async () => {
    setLoadError("");
    try {
      const g = await api.getGlobalSettings();
      setCfg({
        terminal_shell: g.terminal_shell || "auto",
        terminal_font: g.terminal_font || DEFAULT_TERMINAL_FONT,
        http_proxy: g.http_proxy || "",
        enhanced_search: g.enhanced_search !== false,
        show_message_search: g.show_message_search === true,
        suggestion_enabled: g.suggestion_enabled === true,
        agent_retry_intervals: typeof g.agent_retry_intervals === "string" ? g.agent_retry_intervals : "10,20,30",
        // plan-278-1391: 压缩触发阈值（后端可能未返回，回退默认 0.90）
        auto_compact_threshold_ratio: typeof g.auto_compact_threshold_ratio === "number"
          ? g.auto_compact_threshold_ratio : 0.90,
        agent_tool_temperature: typeof g.agent_tool_temperature === "number" ? g.agent_tool_temperature : 0.3,
        agent_text_temperature: typeof g.agent_text_temperature === "number" ? g.agent_text_temperature : 0.7,
        agent_max_output_tokens: typeof g.agent_max_output_tokens === "number" ? g.agent_max_output_tokens : 0,
      });
      setRgSource(g.ripgrep_source);
      setLoaded(true);
    } catch (e) {
      setLoadError(String(e));
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  /** 改动即保存——本地 state 立即更新，300ms 防抖后写后端（连续拖动只落一次） */
  const patch = (p: Partial<typeof cfg>) => {
    if (!loaded) return; // 未成功加载时拒绝写回
    setCfg((prev) => {
      const next = { ...prev, ...p };
      scheduleSave(next);
      return next;
    });
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
        <Row
          title={t("gp.terminal_shell")}
          desc={shellMissing ? `${t("gp.terminal_shell_desc")} 当前所选 Shell 未安装，已回退为平台默认。` : t("gp.terminal_shell_desc")}
        >
          <Select value={cfg.terminal_shell} onChange={(v) => patch({ terminal_shell: v })} options={shellOptions} style={{ minWidth: 220 }} aria-label={t("gp.terminal_shell")} />
        </Row>
        <Row title={t("gp.terminal_font")} desc={t("gp.terminal_font_desc")}>
          <Select value={cfg.terminal_font} onChange={(v) => patch({ terminal_font: v })} options={fontOptions} style={{ minWidth: 220 }} aria-label={t("gp.terminal_font")} />
        </Row>
      </div>

      <div className="settings-card">
        <Row title={t("gp.enhanced_search")} desc={rgDesc}>
          <Sw checked={cfg.enhanced_search} onChange={(v) => patch({ enhanced_search: v })} />
        </Row>
        {/* plan-156-619 S2：消息区搜索入口开关。默认关闭——入口按钮此前常驻右上角，
            与综合工具卡抢位；关闭只隐藏按钮，Ctrl+F 仍可唤起搜索条。 */}
        <Row title={t("gp.message_search")} desc={t("gp.message_search_desc")}>
          <Sw checked={cfg.show_message_search} onChange={(v) => patch({ show_message_search: v })} />
        </Row>
        <Row title={t("gp.suggestion")} desc={t("gp.suggestion_desc")}>
          <Sw checked={cfg.suggestion_enabled} onChange={(v) => patch({ suggestion_enabled: v })} />
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
        {/* v45: 异常自动重试——标签即一次重试，个数即次数；全部用完仍失败才停止并显示报错 */}
        <Row title={t("gp.retry_intervals")} desc={t("gp.retry_intervals_desc")}>
          <TagInput
            values={parseRetryIntervals(cfg.agent_retry_intervals)}
            onChange={(vals) => patch({ agent_retry_intervals: vals.join(",") })}
            validate={validateRetrySeconds}
            maxTags={RETRY_MAX_COUNT}
            placeholder="输入秒数，回车添加"
            formatTag={(v) => `${v} 秒`}
            width={260}
            aria-label={t("gp.retry_intervals")}
          />
        </Row>
      </div>

      <BrowserDownloadDirRow />
    </div>
  );
}

const MAX_OUTPUT_OPTIONS = [
  { value: 0, label: "模型默认" },
  { value: 8192, label: "8K" },
  { value: 16384, label: "16K" },
  { value: 32768, label: "32K" },
  { value: 64000, label: "64K" },
  { value: 128000, label: "128K" },
];

/** plan-20261018-operator-boost：内置浏览器下载目录。
 * 下载不再弹系统保存框，统一保存到这里（默认安装目录下的 downloads，不存在会自动创建）；
 * 目录由 Electron 主进程持有（下载拦截发生在主进程），因此只在桌面端显示。选择目录即保存。
 * plan-537-2246：卡片容器改由本组件自己持有——此前外层无条件渲染 <div class="settings-card">，
 * 而本组件在无 Electron API 时返回 null，于是页面底部留下一个只有 1px 描边的空卡片，
 * 看起来就是一条“阴影线”。现在返回 null 时整块（含卡片壳）都不渲染。 */
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
    <div className="settings-card">
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
    </div>
  );
}
