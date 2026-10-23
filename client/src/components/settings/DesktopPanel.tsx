/** 设置中心：电脑操控（plan-334-1661）。
 *
 * 为什么单独一页而不是塞进「常规」：这块能力会真实操作鼠标键盘、可读写屏幕内容，
 * 风险面与配置项都明显多于常规项。集中一页便于用户看清「开了什么、放开了什么」。
 *
 * 本页也是该能力的**唯一入口**——它不作为 MCP 注册，因此不会出现在「拓展 → 连接器」页面，
 * 避免用户在两处看到同一个能力而产生困惑。
 *
 * 操作逻辑与设置中心其它页一致：所有开关「修改即保存」（300ms 防抖）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { DesktopRecipe, DesktopStatus, RecipeScope } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { useI18n } from "../../store/i18n";
import { ConfirmDialog } from "../ConfirmDialog";
import { Badge, Button, CardRow, Checkbox, FormDialog, Input, List, ListRow, Segmented, Slider, Textarea, Tooltip } from "../ui";
import { Field } from "../ui/Field";
import { Row, Sw } from "./shared";

type SubTab = "basic" | "recipes";

const SUBTABS: Array<{ key: SubTab; i18n: string }> = [
  { key: "basic", i18n: "dp.tab.basic" },
  { key: "recipes", i18n: "dp.tab.recipes" },
];

export function DesktopPanel() {
  const { t } = useI18n();
  const [tab, setTab] = useState<SubTab>("basic");

  return (
    <div className="dp-panel">
      {/* plan-114-450：页内标签统一为灰底分段控件（参照 Qoder），取代私有 .dp-subtab 下划线样式 */}
      <div className="ui-toolbar">
        <Segmented value={tab} onChange={setTab} aria-label={t("dp.tab.group")}
          options={SUBTABS.map((s) => ({ value: s.key, label: t(s.i18n) }))} />
      </div>

      {tab === "basic" && <DesktopBasicSection />}
      {tab === "recipes" && <DesktopRecipeSection />}
    </div>
  );
}

/* ── 基础配置 ── */

type CoordMode = "auto" | "pixel" | "norm1000";

type DesktopCfg = {
  desktop_enabled: boolean;
  desktop_plain_ops_enabled: boolean;
  /** 内置浏览器：AI 在右侧浏览器面板中操作网页（浏览器工具的唯一开关） */
  browser_enabled: boolean;
  /** 后台无头浏览器：仅未连接桌面端时的兜底浏览器生效 */
  browser_headless: boolean;
  desktop_require_foreground: boolean;
  desktop_screenshot_quality: number;
  desktop_screenshot_max_dim: number;
  desktop_recipe_enabled: boolean;
  desktop_coord_mode: CoordMode;
};

// plan-20261018-operator-boost：默认开启电脑操控；截图自动适配模型可见尺寸、画质 100（与后端 config.py 一致）。
// plan-20261019-desktop-precision：截图元素编号、点击前校验、破坏性操作词表随 OCR 一并移除。
// plan-20261019-desktop-targeting：新增「坐标口径」，模型总点偏时可手动换口径。
const DEFAULTS: DesktopCfg = {
  desktop_enabled: true,
  desktop_plain_ops_enabled: true,
  browser_enabled: true,
  browser_headless: true,
  desktop_require_foreground: true,
  desktop_screenshot_quality: 100,
  desktop_screenshot_max_dim: 0,
  desktop_recipe_enabled: true,
  desktop_coord_mode: "auto",
};

function toCoordMode(v: unknown): CoordMode {
  return v === "pixel" || v === "norm1000" ? v : "auto";
}

type Translate = (key: string, params?: Record<string, string | number>) => string;

/** 「检测」结果的一句话：分辨率、各显示器缩放、DPI 感知级别（不是 PerMonitorV2 时明确提示会偏移）、落点自检。
 *  plan-537-2251：改由 i18n 键拼装（原先硬编码中文，英文界面不生效），拼接格式保持不变。 */
function describeProbe(t: Translate, info: Record<string, unknown>, calib?: Record<string, unknown>): string {
  const screen = (info.screen as number[] | undefined) ?? [];
  const monitors = (info.monitors as Array<{ w?: number; h?: number; scale?: number }> | undefined) ?? [];
  const aware = typeof info.dpiAwareness === "string" ? info.dpiAwareness : "";
  const parts = [
    t("dp.probe.core", { version: String(info.version ?? "?") }),
    monitors.length > 0
      ? t("dp.probe.monitors", {
        list: monitors.map((m) => t("dp.probe.monitor_item", { w: String(m.w ?? "?"), h: String(m.h ?? "?"), scale: String(m.scale ?? 100) })).join("、"),
      })
      : t("dp.probe.screen", { w: String(screen[0] ?? "?"), h: String(screen[1] ?? "?") }),
  ];
  if (aware) {
    parts.push(aware === "PerMonitorV2"
      ? t("dp.probe.dpi_ok")
      : t("dp.probe.dpi_warn", { level: aware }));
  }
  if (calib && typeof calib.finalMax === "number") {
    parts.push(calib.finalMax === 0
      ? t("dp.probe.calib_ok")
      : t("dp.probe.calib_warn", { px: String(calib.finalMax) }));
  }
  parts.push(t("dp.probe.pid", { pid: String(info.pid ?? "?") }));
  return parts.join("，") + "。";
}

function DesktopBasicSection() {
  const { t } = useI18n();
  // i18n 的 t 每次渲染都是新函数，放进依赖会让加载副作用反复重跑；用 ref 取最新值
  const tRef = useRef(t);
  tRef.current = t;

  const [cfg, setCfg] = useState<DesktopCfg>(DEFAULTS);
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeMsg, setProbeMsg] = useState("");
  // plan-537-2251：拉取设置成功前禁止写回——一次失败的读取会用默认值把用户配置整体覆盖
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  // 长边输入框的中间态：逐字输入只改文本，失焦时才提交解析后的数值
  const [maxDimText, setMaxDimText] = useState(String(DEFAULTS.desktop_screenshot_max_dim));
  /** 中间态文本与已加载标记：卸载清理的闭包读不到最新 state */
  const maxDimTextRef = useRef(maxDimText);
  maxDimTextRef.current = maxDimText;
  const loadedRef = useRef(false);

  // 与其它设置页一致：改动即保存（300ms 防抖，连续拖动只落一次盘）
  const saveTimerRef = useRef<number | null>(null);
  /** 是否有尚未落盘的改动 */
  const dirtyRef = useRef(false);
  /** 最新配置：卸载清理的闭包只能拿到挂载时的旧值，必须走 ref */
  const latestRef = useRef(cfg);
  latestRef.current = cfg;
  /** 卸载后不再 setState（检测请求可能晚于切页返回） */
  const mountedRef = useRef(true);

  const flushSave = useCallback(async (payload: DesktopCfg) => {
    dirtyRef.current = false;
    try {
      await api.setGlobalSettings(payload);
    } catch (e) {
      useChatStore.setState({ error: t("dp.save_fail", { error: String(e) }) });
    }
  }, []);
  const scheduleSave = useCallback((next: DesktopCfg) => {
    dirtyRef.current = true;
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => {
      saveTimerRef.current = null;
      void flushSave(next);
    }, 300);
  }, [flushSave]);
  // 卸载（含切换子标签）：先取消计时，再把待落盘的改动立即补写一次
  useEffect(() => () => {
    if (saveTimerRef.current) {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    // 输入框可能因切页卸载而未触发失焦：把未提交的长边文本一并算进这次补写
    const parsed = Number.parseInt(maxDimTextRef.current, 10);
    const pendingMaxDim = Number.isFinite(parsed) ? Math.min(4096, Math.max(0, parsed)) : 0;
    if (loadedRef.current && pendingMaxDim !== latestRef.current.desktop_screenshot_max_dim) {
      void flushSave({ ...latestRef.current, desktop_screenshot_max_dim: pendingMaxDim });
    } else if (dirtyRef.current) {
      void flushSave(latestRef.current);
    }
  }, [flushSave]);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const load = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      const next: DesktopCfg = {
        desktop_enabled: g.desktop_enabled !== false,
        desktop_plain_ops_enabled: g.desktop_plain_ops_enabled !== false,
        browser_enabled: g.browser_enabled !== false,
        browser_headless: g.browser_headless !== false,
        desktop_require_foreground: g.desktop_require_foreground !== false,
        desktop_screenshot_quality: typeof g.desktop_screenshot_quality === "number"
          ? g.desktop_screenshot_quality : 100,
        desktop_screenshot_max_dim: typeof g.desktop_screenshot_max_dim === "number"
          ? g.desktop_screenshot_max_dim : 0,
        desktop_recipe_enabled: g.desktop_recipe_enabled !== false,
        desktop_coord_mode: toCoordMode(g.desktop_coord_mode),
      };
      setCfg(next);
      setMaxDimText(String(next.desktop_screenshot_max_dim));
      loadedRef.current = true;
      setLoaded(true);
      setLoadError("");
    } catch (e) {
      // 读取失败：界面沿用默认展示，但保持「未加载」状态以禁止写回
      setLoadError(String(e));
      useChatStore.setState({ error: tRef.current("dp.load_fail", { error: String(e) }) });
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  // plan-537-2251：副作用放在 update 外面——先算出下一份配置，再分别提交状态与保存
  const patch = useCallback((p: Partial<DesktopCfg>) => {
    if (!loaded || loadError) return;
    const next = { ...latestRef.current, ...p };
    latestRef.current = next;
    setCfg(next);
    scheduleSave(next);
  }, [loaded, loadError, scheduleSave]);

  // 内核状态：仅展示，不触发启动（避免打开设置页就拉起子进程）。
  // 本区块随子标签卸载重挂，因此切回「基础配置」即等于重新读取。
  useEffect(() => {
    let alive = true;
    api.getDesktopStatus()
      .then((s) => { if (alive) setStatus(s); })
      .catch(() => { if (alive) setStatus(null); });
    return () => { alive = false; };
  }, []);

  const probe = useCallback(async () => {
    setProbing(true);
    setProbeMsg("");
    try {
      const r = await api.probeDesktop();
      if (!mountedRef.current) return;
      setProbeMsg(describeProbe(tRef.current, r.info as Record<string, unknown>, r.calib));
      api.getDesktopStatus()
        .then((s) => { if (mountedRef.current) setStatus(s); })
        .catch(() => { /* 状态展示非关键 */ });
    } catch (e) {
      if (mountedRef.current) setProbeMsg(tRef.current("dp.probe.fail", { error: String(e) }));
    } finally {
      if (mountedRef.current) setProbing(false);
    }
  }, []);

  // 长边输入：逐字编辑不写库，失焦时解析为整数并夹取到后端可接受范围（清空按 0 = 自动适配）
  const commitMaxDim = () => {
    const parsed = Number.parseInt(maxDimText, 10);
    const v = Number.isFinite(parsed) ? Math.min(4096, Math.max(0, parsed)) : 0;
    setMaxDimText(String(v));
    patch({ desktop_screenshot_max_dim: v });
  };

  const dim = !cfg.desktop_enabled;
  const dimPlain = dim || !cfg.desktop_plain_ops_enabled;
  // 未找到操控内核时无法检测，按钮置灰并说明原因
  const coreMissing = status?.core_found === false;
  const statusText =
    status === null ? t("dp.status.unknown")
    : status.running ? t("dp.status.running")
    : status.core_found ? t("dp.status.idle")
    : t("dp.status.missing");

  return (
    <div className="settings-card-stack sc-stack">
      <div className="settings-card">
        <Row title={t("dp.enable")} desc={t("dp.enable_desc")}>
          {/* plan-537-2251：只改电脑操控本身，不再连带打开内置浏览器（它有独立开关） */}
          <Sw
            checked={cfg.desktop_enabled}
            onChange={(v) => patch({ desktop_enabled: v })}
          />
        </Row>
        <Row title={t("dp.plain")} desc={t("dp.plain_desc")} disabled={dim}>
          <Sw
            checked={cfg.desktop_plain_ops_enabled}
            disabled={dim}
            onChange={(v) => patch({ desktop_plain_ops_enabled: v })}
          />
        </Row>
        <Row title={t("dp.browser")} desc={t("dp.browser_desc")}>
          <Sw checked={cfg.browser_enabled} onChange={(v) => patch({ browser_enabled: v })} />
        </Row>
        <Row
          title={t("dp.browser_headless")}
          desc={t("dp.browser_headless_desc")}
          disabled={!cfg.browser_enabled}
        >
          <Sw
            checked={cfg.browser_headless}
            disabled={!cfg.browser_enabled}
            onChange={(v) => { if (cfg.browser_enabled) patch({ browser_headless: v }); }}
          />
        </Row>
      </div>

      <div className="settings-card">
        <Row title={t("dp.foreground")} desc={t("dp.foreground_desc")} disabled={dimPlain}>
          <Sw
            checked={cfg.desktop_require_foreground}
            disabled={dimPlain}
            onChange={(v) => patch({ desktop_require_foreground: v })}
          />
        </Row>
        <Row title={t("dp.max_dim")} desc={t("dp.max_dim_desc")} disabled={dimPlain}>
          <Input
            type="number"
            style={{ width: 100 }}
            min={0}
            max={4096}
            value={maxDimText}
            disabled={dimPlain}
            onChange={(e) => setMaxDimText(e.target.value)}
            onBlur={commitMaxDim}
          />
        </Row>
        <Row title={t("dp.coord_mode")} desc={t("dp.coord_mode_desc")} disabled={dimPlain}>
          <Segmented
            value={cfg.desktop_coord_mode}
            onChange={(v) => patch({ desktop_coord_mode: v })}
            disabled={dimPlain}
            aria-label={t("dp.coord_mode")}
            options={(["auto", "pixel", "norm1000"] as const).map((m) => ({
              value: m, label: t(`dp.coord_mode.${m}`),
            }))}
          />
        </Row>
        <Row title={t("dp.quality")} desc={t("dp.quality_desc")} disabled={dimPlain}>
          <Slider
            min={10}
            max={100}
            step={5}
            disabled={dimPlain}
            value={cfg.desktop_screenshot_quality}
            onChange={(v) => patch({ desktop_screenshot_quality: v })}
            format={(v) => `${v}`}
          />
        </Row>
        <Row title={t("dp.recipe")} desc={t("dp.recipe_desc")} disabled={dim}>
          <Sw
            checked={cfg.desktop_recipe_enabled}
            disabled={dim}
            onChange={(v) => patch({ desktop_recipe_enabled: v })}
          />
        </Row>
      </div>

      <div className="settings-card">
        <CardRow title={t("dp.status")} desc={t("dp.status_desc")}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--sp-3)" }}>
            <span style={{ fontSize: 12, color: "var(--text-3)" }}>{statusText}</span>
            {/* 内核缺失时按钮置灰，悬停说明原因；外层 span 只做事件宿主，不改变布局 */}
            <Tooltip
              content={t("dp.status.missing_hint")}
              disabled={!coreMissing}
            >
              <span style={{ display: "inline-flex" }}>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={probe}
                  loading={probing}
                  disabled={coreMissing}
                >
                  {t("dp.detect")}
                </Button>
              </span>
            </Tooltip>
          </div>
        </CardRow>
        {probeMsg && (
          <Row title={t("dp.detect_result")} desc={probeMsg}>
            <span />
          </Row>
        )}
      </div>
    </div>
  );
}

/* ── 操作路线管理 ── */

function DesktopRecipeSection() {
  const { t } = useI18n();
  // 同样用 ref 固定 t 引用，避免列表加载副作用被反复触发
  const tRef = useRef(t);
  tRef.current = t;
  const [items, setItems] = useState<DesktopRecipe[]>([]);
  const [keyword, setKeyword] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<DesktopRecipe | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [notice, setNotice] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);

  // plan-537-2251：结果行在卡片中部，底部批量操作后容易看不见，同步发一条全局提示
  const notify = useCallback((msg: string) => {
    setNotice(msg);
    useChatStore.setState({ error: msg });
  }, []);

  const load = useCallback(async (kw = "") => {
    try {
      const r = await api.listDesktopRecipes(kw);
      setItems(r.items);
      // 列表刷新后清理已不存在的选中项，避免「删了却还勾着」
      setSelected((prev) => new Set([...prev].filter((id) => r.items.some((it) => it.id === id))));
    } catch (e) {
      notify(tRef.current("dp.recipe.load_fail", { error: String(e) }));
    }
  }, [notify]);
  useEffect(() => { load(); }, [load]);

  const toggleSelect = (id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const allSelected = items.length > 0 && items.every((it) => selected.has(it.id));

  const removeSelected = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.deleteDesktopRecipes([...selected]);
      notify(tRef.current("dp.recipe.deleted", { n: r.deleted }));
      setSelected(new Set());
      await load(keyword);
    } catch (e) {
      notify(tRef.current("dp.recipe.delete_fail", { error: String(e) }));
    } finally {
      setBusy(false);
    }
  }, [selected, keyword, load, notify]);

  const clearAll = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.deleteAllDesktopRecipes();
      notify(tRef.current("dp.recipe.cleared", { n: r.deleted }));
      setSelected(new Set());
      await load(keyword);
    } catch (e) {
      notify(tRef.current("dp.recipe.clear_fail", { error: String(e) }));
    } finally {
      setBusy(false);
    }
  }, [keyword, load, notify]);

  const doExport = useCallback(async () => {
    try {
      const data = await api.exportDesktopRecipes();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `desktop-recipes-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      notify(tRef.current("dp.recipe.exported", { n: data.count }));
    } catch (e) {
      notify(tRef.current("dp.recipe.export_fail", { error: String(e) }));
    }
  }, [notify]);

  const doImport = useCallback(async (file: File) => {
    setBusy(true);
    try {
      const payload = JSON.parse(await file.text());
      const r = await api.importDesktopRecipes(payload);
      notify(tRef.current("dp.recipe.imported", {
        created: r.created, updated: r.updated, skipped: r.skipped,
      }));
      await load(keyword);
    } catch (e) {
      notify(tRef.current("dp.recipe.import_fail", { error: String(e) }));
    } finally {
      setBusy(false);
    }
  }, [keyword, load, notify]);

  return (
    <div className="settings-card-stack sc-stack">
      <div className="settings-card">
        <CardRow title={t("dp.recipe.section")} desc={t("dp.recipe.section_desc")}>
          <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
            {t("dp.recipe.new")}
          </Button>
        </CardRow>
        {/* 搜索行：输入框与按钮固定同行。此前复用允许换行的工具条容器，
            输入框倾向占满宽度时按钮会被挤到下一行。 */}
        <div className="dp-search-row">
          <Input
            placeholder={t("dp.recipe.search_ph")}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void load(keyword); }}
          />
          <Button variant="secondary" size="sm" onClick={() => load(keyword)}>
            {t("dp.recipe.search")}
          </Button>
        </div>

        {notice && (
          <Row title={t("dp.recipe.notice")} desc={notice}>
            <span />
          </Row>
        )}
      </div>

      <div className="settings-card">
        {/* 批量操作条：占满卡片宽度，左侧勾选状态、右侧动作组分组排布——
            此前挤在行尾控件槽里，窄窗口下按钮会零散换行。 */}
        <div className="dp-toolbar">
          <div className="dp-toolbar-group">
            <Checkbox
              checked={allSelected}
              indeterminate={selected.size > 0 && !allSelected}
              disabled={items.length === 0}
              onChange={() => setSelected(allSelected ? new Set() : new Set(items.map((it) => it.id)))}
              label={t("dp.recipe.select_all")}
            />
            <span className="dp-toolbar-hint">
              {t("dp.recipe.selected", { n: selected.size, total: items.length })}
            </span>
          </div>
          <div className="dp-toolbar-group">
            <Button variant="secondary" size="sm" onClick={doExport} disabled={items.length === 0}>
              {t("dp.recipe.export")}
            </Button>
            <Button variant="secondary" size="sm" onClick={() => fileRef.current?.click()} loading={busy}>
              {t("dp.recipe.import")}
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => setConfirmDelete(true)}
              disabled={selected.size === 0 || busy}
            >
              {t("dp.recipe.delete_selected")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setConfirmClear(true)}
              disabled={items.length === 0 || busy}
            >
              {t("dp.recipe.clear")}
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept="application/json"
              style={{ display: "none" }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void doImport(f);
                e.target.value = "";
              }}
            />
          </div>
        </div>

        {items.length === 0 ? (
          <div className="navpage-empty">{t("dp.recipe.empty")}</div>
        ) : (
          <List>
            {items.map((it) => (
              <ListRow
                key={it.id}
                onClick={() => setEditing(it)}
                name={
                  <span className="dp-recipe-title">
                    {/* 勾选框独立于行点击：点它只做选择，不打开弹窗 */}
                    <span onClick={(e) => e.stopPropagation()}>
                      <Checkbox
                        checked={selected.has(it.id)}
                        onChange={() => toggleSelect(it.id)}
                        aria-label={`选择 ${it.app_name} ${it.intent}`}
                      />
                    </span>
                    <span className="dp-recipe-title-text">{it.app_name} · {it.intent}</span>
                    {/* plan-20261018-operator-boost：网页路线（内置浏览器）与桌面应用路线共用列表，用角标区分 */}
                    {it.scope === "browser" && (
                      <Badge size="xs" tone="info">{t("dp.recipe.scope_web")}</Badge>
                    )}
                    {it.source === "user" && (
                      <span style={{ fontSize: 11, color: "var(--text-3)" }}>
                        {t("dp.recipe.edited")}
                      </span>
                    )}
                    <span className="dp-recipe-usage">使用 {it.usage_count} 次</span>
                  </span>
                }
                desc={<span className="dp-recipe-desc">{it.principle}</span>}
                actions={
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={(e) => { e.stopPropagation(); setEditing(it); }}
                  >
                    {t("dp.recipe.edit")}
                  </Button>
                }
              />
            ))}
          </List>
        )}
      </div>

      <RecipeEditor
        open={creating || editing !== null}
        recipe={editing}
        onClose={() => { setCreating(false); setEditing(null); }}
        onSaved={async (created?: boolean) => {
          setCreating(false);
          setEditing(null);
          notify(tRef.current(created === false ? "dp.recipe.saved_updated" : "dp.recipe.saved"));
          await load(keyword);
        }}
      />

      <ConfirmDialog
        open={confirmDelete}
        title={t("dp.recipe.delete_selected")}
        message={t("dp.recipe.delete_confirm", { n: selected.size })}
        danger
        onCancel={() => setConfirmDelete(false)}
        onConfirm={async () => {
          setConfirmDelete(false);
          await removeSelected();
        }}
      />
      <ConfirmDialog
        open={confirmClear}
        title={t("dp.recipe.clear")}
        message={t("dp.recipe.clear_confirm", { n: items.length })}
        danger
        onCancel={() => setConfirmClear(false)}
        onConfirm={async () => {
          setConfirmClear(false);
          await clearAll();
        }}
      />
    </div>
  );
}

/** 路线编辑器：新增或编辑一条操作路线。 */
function RecipeEditor({
  open,
  recipe,
  onClose,
  onSaved,
}: {
  open: boolean;
  recipe: DesktopRecipe | null;
  onClose: () => void;
  onSaved: (created?: boolean) => void | Promise<void>;
}) {
  const { t } = useI18n();
  const [scope, setScope] = useState<RecipeScope>("desktop");
  const [appName, setAppName] = useState("");
  const [intent, setIntent] = useState("");
  const [principle, setPrinciple] = useState("");
  const [pitfalls, setPitfalls] = useState("");
  const [saving, setSaving] = useState(false);
  // plan-537-2251：错误按字段分开，挂到对应输入项下（原先单一错误挂在「坑点」上，指向错误位置）
  const [errs, setErrs] = useState<{ appName?: string; intent?: string; principle?: string }>({});
  const principleRef = useRef<HTMLTextAreaElement | null>(null);
  const pitfallsRef = useRef<HTMLTextAreaElement | null>(null);

  // 打开时用当前记录（或空）重置表单——避免上一次的残留内容串到下一次编辑
  useEffect(() => {
    if (!open) return;
    setScope(recipe?.scope === "browser" ? "browser" : "desktop");
    setAppName(recipe?.app_name ?? "");
    setIntent(recipe?.intent ?? "");
    setPrinciple(recipe?.principle ?? "");
    setPitfalls(recipe?.pitfalls ?? "");
    setErrs({});
    // 长内容从开头看起：受控组件换值后浏览器可能保留上一次的滚动位置
    requestAnimationFrame(() => {
      if (principleRef.current) principleRef.current.scrollTop = 0;
      if (pitfallsRef.current) pitfallsRef.current.scrollTop = 0;
    });
  }, [open, recipe]);

  const save = useCallback(async () => {
    const required = t("dp.recipe.required");
    const nextErrs = {
      appName: appName.trim() ? undefined : required,
      intent: intent.trim() ? undefined : required,
      principle: principle.trim() ? undefined : required,
    };
    setErrs(nextErrs);
    if (nextErrs.appName || nextErrs.intent || nextErrs.principle) return;
    setSaving(true);
    try {
      if (recipe) {
        await api.updateDesktopRecipe(recipe.id, { app_name: appName, intent, principle, pitfalls, scope });
        await onSaved();
      } else {
        const r = await api.createDesktopRecipe({ app_name: appName, intent, principle, pitfalls, scope });
        // 同名路线后端会直接覆盖：把 created=false 传给上层，提示用户这次实际是更新
        await onSaved(r.created);
      }
    } catch (e) {
      useChatStore.setState({ error: t("dp.recipe.save_fail", { error: String(e) }) });
    } finally {
      setSaving(false);
    }
  }, [appName, intent, principle, pitfalls, scope, recipe, onSaved, t]);

  return (
    <FormDialog
      open={open}
      onClose={onClose}
      title={recipe ? t("dp.recipe.edit_title") : t("dp.recipe.new_title")}
      subtitle={t("dp.recipe.editor_desc")}
      onSubmit={save}
      submitLabel={t("dp.recipe.save")}
      submitDisabled={saving}
      width={640}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--sp-4)" }}>
        <Field label={t("dp.recipe.scope")}>
          <Segmented
            value={scope}
            onChange={setScope}
            aria-label={t("dp.recipe.scope")}
            options={[
              { value: "desktop", label: t("dp.recipe.scope_desktop") },
              { value: "browser", label: t("dp.recipe.scope_web") },
            ]}
          />
        </Field>
        <Field
          label={scope === "browser" ? t("dp.recipe.site") : t("dp.recipe.app_name")}
          hint={scope === "browser" ? t("dp.recipe.site_hint") : t("dp.recipe.app_name_hint")}
          error={errs.appName}
          required
        >
          <Input value={appName} onChange={(e) => setAppName(e.target.value)} />
        </Field>
        <Field label={t("dp.recipe.intent")} hint={t("dp.recipe.intent_hint")} error={errs.intent} required>
          <Input value={intent} onChange={(e) => setIntent(e.target.value)} />
        </Field>
        <Field label={t("dp.recipe.principle")} hint={t("dp.recipe.principle_hint")} error={errs.principle} required>
          <Textarea ref={principleRef} rows={8} value={principle} onChange={(e) => setPrinciple(e.target.value)} />
        </Field>
        <Field label={t("dp.recipe.pitfalls")} hint={t("dp.recipe.pitfalls_hint")}>
          <Textarea ref={pitfallsRef} rows={4} value={pitfalls} onChange={(e) => setPitfalls(e.target.value)} />
        </Field>
      </div>
    </FormDialog>
  );
}
