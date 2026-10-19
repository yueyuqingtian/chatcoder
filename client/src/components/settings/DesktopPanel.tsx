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
import { Badge, Button, CardRow, Checkbox, FormDialog, Input, List, ListRow, Segmented, Slider, Textarea } from "../ui";
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
        <Segmented value={tab} onChange={setTab} aria-label={t("dp.tab.basic")}
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
  desktop_browser_ops_enabled: boolean;
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
  desktop_browser_ops_enabled: true,
  desktop_require_foreground: true,
  desktop_screenshot_quality: 100,
  desktop_screenshot_max_dim: 0,
  desktop_recipe_enabled: true,
  desktop_coord_mode: "auto",
};

function toCoordMode(v: unknown): CoordMode {
  return v === "pixel" || v === "norm1000" ? v : "auto";
}

/** 「检测」结果的一句话：分辨率、各显示器缩放、DPI 感知级别（不是 PerMonitorV2 时明确提示会偏移）、落点自检。 */
function describeProbe(info: Record<string, unknown>, calib?: Record<string, unknown>): string {
  const screen = (info.screen as number[] | undefined) ?? [];
  const monitors = (info.monitors as Array<{ w?: number; h?: number; scale?: number }> | undefined) ?? [];
  const aware = typeof info.dpiAwareness === "string" ? info.dpiAwareness : "";
  const parts = [
    `内核可用，版本 ${info.version ?? "?"}`,
    monitors.length > 0
      ? `显示器 ${monitors.map((m) => `${m.w ?? "?"}×${m.h ?? "?"}（缩放 ${m.scale ?? 100}%）`).join("、")}`
      : `分辨率 ${screen[0] ?? "?"}×${screen[1] ?? "?"}`,
  ];
  if (aware) {
    parts.push(aware === "PerMonitorV2"
      ? "DPI 感知正常（坐标为物理像素，不受缩放影响）"
      : `DPI 感知为 ${aware}，坐标可能被系统缩放，建议更新应用或重启`);
  }
  if (calib && typeof calib.finalMax === "number") {
    parts.push(calib.finalMax === 0
      ? "光标落点自检通过（5 个点位偏差 0 像素）"
      : `光标落点自检最大偏差 ${calib.finalMax} 像素（已自动校正）`);
  }
  parts.push(`进程 ${info.pid ?? "?"}`);
  return parts.join("，") + "。";
}

function DesktopBasicSection() {
  const { t } = useI18n();
  const [cfg, setCfg] = useState<DesktopCfg>(DEFAULTS);
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeMsg, setProbeMsg] = useState("");

  // 与其它设置页一致：改动即保存（300ms 防抖，连续拖动只落一次盘）
  const saveTimerRef = useRef<number | null>(null);
  const flushSave = useCallback(async (payload: DesktopCfg) => {
    try {
      await api.setGlobalSettings(payload);
    } catch (e) {
      useChatStore.setState({ error: "保存失败: " + String(e) });
    }
  }, []);
  const scheduleSave = useCallback((next: DesktopCfg) => {
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => { void flushSave(next); }, 300);
  }, [flushSave]);
  useEffect(() => () => { if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current); }, []);

  const load = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setCfg({
        desktop_enabled: g.desktop_enabled !== false,
        desktop_plain_ops_enabled: g.desktop_plain_ops_enabled !== false,
        desktop_browser_ops_enabled: g.desktop_browser_ops_enabled !== false,
        desktop_require_foreground: g.desktop_require_foreground !== false,
        desktop_screenshot_quality: typeof g.desktop_screenshot_quality === "number"
          ? g.desktop_screenshot_quality : 100,
        desktop_screenshot_max_dim: typeof g.desktop_screenshot_max_dim === "number"
          ? g.desktop_screenshot_max_dim : 0,
        desktop_recipe_enabled: g.desktop_recipe_enabled !== false,
        desktop_coord_mode: toCoordMode(g.desktop_coord_mode),
      });
    } catch { /* 拉取失败沿用默认值 */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const patch = (p: Partial<DesktopCfg>) => setCfg((prev) => {
    const next = { ...prev, ...p };
    scheduleSave(next);
    return next;
  });

  // 内核状态：仅展示，不触发启动（避免打开设置页就拉起子进程）
  useEffect(() => {
    api.getDesktopStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  const probe = useCallback(async () => {
    setProbing(true);
    setProbeMsg("");
    try {
      const r = await api.probeDesktop();
      setProbeMsg(describeProbe(r.info as Record<string, unknown>, r.calib));
      api.getDesktopStatus().then(setStatus).catch(() => { /* 状态展示非关键 */ });
    } catch (e) {
      setProbeMsg("检测失败：" + String(e));
    } finally {
      setProbing(false);
    }
  }, []);

  const dim = !cfg.desktop_enabled;
  const dimPlain = dim || !cfg.desktop_plain_ops_enabled;
  const statusText =
    status === null ? t("dp.status.unknown")
    : status.running ? t("dp.status.running")
    : status.core_found ? t("dp.status.idle")
    : t("dp.status.missing");

  return (
    <div className="settings-card-stack sc-stack">
      <div className="settings-card">
        <Row title={t("dp.enable")} desc={t("dp.enable_desc")}>
          <Sw checked={cfg.desktop_enabled} onChange={(v) => patch({ desktop_enabled: v })} />
        </Row>
        <Row title={t("dp.plain")} desc={t("dp.plain_desc")} disabled={dim}>
          <Sw
            checked={cfg.desktop_plain_ops_enabled}
            onChange={(v) => patch({ desktop_plain_ops_enabled: v })}
          />
        </Row>
        <Row title={t("dp.browser")} desc={t("dp.browser_desc")} disabled={dim}>
          <Sw
            checked={cfg.desktop_browser_ops_enabled}
            onChange={(v) => patch({ desktop_browser_ops_enabled: v })}
          />
        </Row>
      </div>

      <div className="settings-card">
        <Row title={t("dp.foreground")} desc={t("dp.foreground_desc")} disabled={dimPlain}>
          <Sw
            checked={cfg.desktop_require_foreground}
            onChange={(v) => patch({ desktop_require_foreground: v })}
          />
        </Row>
        <Row title={t("dp.max_dim")} desc={t("dp.max_dim_desc")} disabled={dimPlain}>
          <Input
            type="number"
            style={{ width: 100 }}
            min={0}
            max={4096}
            value={cfg.desktop_screenshot_max_dim}
            disabled={dimPlain}
            onChange={(e) => patch({ desktop_screenshot_max_dim: Math.max(0, parseInt(e.target.value) || 0) })}
          />
        </Row>
        <Row title={t("dp.coord_mode")} desc={t("dp.coord_mode_desc")} disabled={dimPlain}>
          <Segmented
            value={cfg.desktop_coord_mode}
            onChange={(v) => patch({ desktop_coord_mode: v })}
            aria-label={t("dp.coord_mode")}
            options={(["auto", "pixel", "norm1000"] as const).map((m) => ({
              value: m, label: t(`dp.coord_mode.${m}`),
            }))}
          />
        </Row>
        <Row title={t("dp.quality")} desc={t("dp.quality_desc")} disabled={dimPlain}>
          <Slider
            min={30}
            max={100}
            step={5}
            disabled={dimPlain}
            value={cfg.desktop_screenshot_quality}
            onChange={(v) => patch({ desktop_screenshot_quality: v })}
            format={(v) => `${v}`}
          />
        </Row>
        <Row title={t("dp.recipe")} desc={t("dp.recipe_desc")} disabled={dimPlain}>
          <Sw
            checked={cfg.desktop_recipe_enabled}
            onChange={(v) => patch({ desktop_recipe_enabled: v })}
          />
        </Row>
      </div>

      <div className="settings-card">
        <CardRow title={t("dp.status")} desc={t("dp.status_desc")}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--sp-3)" }}>
            <span style={{ fontSize: 12, color: "var(--text-3)" }}>{statusText}</span>
            <Button variant="secondary" size="sm" onClick={probe} loading={probing}>
              {t("dp.detect")}
            </Button>
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

  const load = useCallback(async (kw = "") => {
    try {
      const r = await api.listDesktopRecipes(kw);
      setItems(r.items);
      // 列表刷新后清理已不存在的选中项，避免「删了却还勾着」
      setSelected((prev) => new Set([...prev].filter((id) => r.items.some((it) => it.id === id))));
    } catch (e) {
      setNotice("加载失败：" + String(e));
    }
  }, []);
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
      setNotice(`已删除 ${r.deleted} 条。`);
      setSelected(new Set());
      await load(keyword);
    } catch (e) {
      setNotice("删除失败：" + String(e));
    } finally {
      setBusy(false);
    }
  }, [selected, keyword, load]);

  const clearAll = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.deleteAllDesktopRecipes();
      setNotice(`已清空 ${r.deleted} 条。`);
      setSelected(new Set());
      await load(keyword);
    } catch (e) {
      setNotice("清空失败：" + String(e));
    } finally {
      setBusy(false);
    }
  }, [keyword, load]);

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
      setNotice(`已导出 ${data.count} 条。`);
    } catch (e) {
      setNotice("导出失败：" + String(e));
    }
  }, []);

  const doImport = useCallback(async (file: File) => {
    setBusy(true);
    try {
      const payload = JSON.parse(await file.text());
      const r = await api.importDesktopRecipes(payload);
      setNotice(`导入完成：新增 ${r.created} 条，更新 ${r.updated} 条，跳过 ${r.skipped} 条。`);
      await load(keyword);
    } catch (e) {
      setNotice("导入失败：" + String(e));
    } finally {
      setBusy(false);
    }
  }, [keyword, load]);

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
              已选 {selected.size} / {items.length} 条
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
        onSaved={async () => {
          setCreating(false);
          setEditing(null);
          setNotice("已保存。");
          await load(keyword);
        }}
      />

      <ConfirmDialog
        open={confirmDelete}
        title={t("dp.recipe.delete_selected")}
        message={`确定删除选中的 ${selected.size} 条操作路线？此操作不可撤销。`}
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
        message={`确定清空全部 ${items.length} 条操作路线？此操作不可撤销。`}
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
  onSaved: () => void | Promise<void>;
}) {
  const { t } = useI18n();
  const [scope, setScope] = useState<RecipeScope>("desktop");
  const [appName, setAppName] = useState("");
  const [intent, setIntent] = useState("");
  const [principle, setPrinciple] = useState("");
  const [pitfalls, setPitfalls] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
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
    setErr("");
    // 长内容从开头看起：受控组件换值后浏览器可能保留上一次的滚动位置
    requestAnimationFrame(() => {
      if (principleRef.current) principleRef.current.scrollTop = 0;
      if (pitfallsRef.current) pitfallsRef.current.scrollTop = 0;
    });
  }, [open, recipe]);

  const save = useCallback(async () => {
    if (!appName.trim() || !intent.trim() || !principle.trim()) {
      setErr(t("dp.recipe.required"));
      return;
    }
    setSaving(true);
    setErr("");
    try {
      if (recipe) {
        await api.updateDesktopRecipe(recipe.id, { app_name: appName, intent, principle, pitfalls, scope });
      } else {
        await api.createDesktopRecipe({ app_name: appName, intent, principle, pitfalls, scope });
      }
      await onSaved();
    } catch (e) {
      setErr("保存失败：" + String(e));
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
          required
        >
          <Input value={appName} onChange={(e) => setAppName(e.target.value)} />
        </Field>
        <Field label={t("dp.recipe.intent")} hint={t("dp.recipe.intent_hint")} required>
          <Input value={intent} onChange={(e) => setIntent(e.target.value)} />
        </Field>
        <Field label={t("dp.recipe.principle")} hint={t("dp.recipe.principle_hint")} required>
          <Textarea ref={principleRef} rows={8} value={principle} onChange={(e) => setPrinciple(e.target.value)} />
        </Field>
        <Field label={t("dp.recipe.pitfalls")} hint={t("dp.recipe.pitfalls_hint")} error={err}>
          <Textarea ref={pitfallsRef} rows={4} value={pitfalls} onChange={(e) => setPitfalls(e.target.value)} />
        </Field>
      </div>
    </FormDialog>
  );
}
