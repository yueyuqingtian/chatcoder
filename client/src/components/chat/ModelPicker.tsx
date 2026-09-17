/** ModelPicker（plan-238-1191：基于 Radix DropdownMenu 重写）：
 * 一级 = 供应商菜单（单行名称 + 右侧 ›，当前模型所在供应商显示 ✓）；
 * 二级 = 该供应商的模型子菜单（悬停/聚焦即滑出，选中项左侧 ✓）。
 * 底部「管理模型」打开设置页。ComposerBox / Workspace 空态输入框共用。
 *
 * 视觉沿用项目设计变量（bg-elevated / border / shadow / bg-hover / accent），
 * 弹层定位、碰撞翻转、键盘导航与 ARIA 语义由 Radix 组件库承担（替换此前
 * 手写 portal + 坐标 clamp + 两级网格的方形弹窗）。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { api, type ModelOut } from "../../api/client";
import { IconCpu, IconCheck, IconChevronRight, IconMultimodal, IconRefresh } from "../icons";

interface ModelGroup {
  name: string;
  models: ModelOut[];
  /** 供应商 id（独立模型为 null） */
  providerId: number | null;
  /** workbuddy 供应商：展示积分余额 + 刷新按钮 */
  workbuddy: boolean;
}

export function openModelSettings() {
  window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "models" } }));
}

/** 模型附加信息标签（长上下文 / 消耗倍率 / 多模态图标） */
function ModelTags({ m }: { m: ModelOut }) {
  const hasTags = Boolean(m.trae_max_context || m.trae_consumption_rate || m.is_multimodal);
  if (!hasTags) return <span className="mp-tags" />;
  return (
    <span className="mp-tags">
      {m.trae_max_context ? <span className="mp-tag">长上下文</span> : null}
      {m.trae_consumption_rate ? <span className="mp-tag">消耗×{m.trae_consumption_rate}</span> : null}
      {m.is_multimodal ? (
        <span className="mp-tag mp-tag-icon" title="支持图片输入（多模态）"><IconMultimodal size={12} /></span>
      ) : null}
    </span>
  );
}

export function ModelPicker({
  models,
  value,
  onChange,
  open,
  onToggle,
}: {
  models: ModelOut[];
  value: number | null;
  onChange: (id: number) => void;
  open: boolean;
  onToggle: () => void;
}) {
  const groups = useMemo<ModelGroup[]>(() => {
    const map = new Map<string, ModelGroup>();
    for (const m of models) {
      if (!m.is_active && m.id !== value) continue;
      // plan-248-1258 M2.4: 供应商被禁用时其模型不进入选择器
      //（当前选中项保留显示，避免"当前模型突然消失"造成困惑）
      if (m.provider_active === false && m.id !== value) continue;
      // trae 供应商：TRAE 目录含大量工具/占位模型，客户端实际可用的才展示
      if (m.api_format === "trae" && !m.trae_available) continue;
      const g = m.provider_name || "独立模型";
      if (!map.has(g)) {
        map.set(g, { name: g, models: [], providerId: m.provider_id ?? null, workbuddy: m.api_format === "workbuddy" });
      }
      map.get(g)!.models.push(m);
    }
    // 组内按名称排序，组按名称排序（"独立模型" 排最后）
    const arr = [...map.values()].map((g) => ({
      ...g,
      models: g.models.sort((a, b) => a.name.localeCompare(b.name)),
    }));
    arr.sort((a, b) => (a.name === "独立模型" ? 1 : b.name === "独立模型" ? -1 : a.name.localeCompare(b.name)));
    return arr;
  }, [models, value]);

  // ── workbuddy 积分余额：弹窗每次打开实时刷新；点击图标手动刷新 ──
  const [credits, setCredits] = useState<Record<number, number | null>>({});
  const [creditsBusy, setCreditsBusy] = useState<Record<number, boolean>>({});
  const refreshCredits = useCallback(async (providerId: number) => {
    setCreditsBusy((s) => ({ ...s, [providerId]: true }));
    try {
      const r = await api.workbuddyCredits(providerId, { refresh: true });
      // 多账号时按合计展示（仅统计已登录且余额已知的账号）
      const vals = r.credentials.filter((c) => c.logged_in && c.credits != null).map((c) => Number(c.credits));
      setCredits((s) => ({ ...s, [providerId]: vals.length > 0 ? vals.reduce((a, b) => a + b, 0) : null }));
    } catch { /* 查询失败保留旧值 */ } finally {
      setCreditsBusy((s) => ({ ...s, [providerId]: false }));
    }
  }, []);
  const wbProviderIds = useMemo(
    () => groups.filter((g) => g.workbuddy && g.providerId != null).map((g) => g.providerId!),
    [groups],
  );
  useEffect(() => {
    if (!open) return;
    for (const id of wbProviderIds) void refreshCredits(id);
  }, [open, wbProviderIds, refreshCredits]);

  const fmtCredits = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  /** 积分徽标 + 刷新按钮（一级供应商行与二级菜单头共用） */
  const renderCredits = (g: ModelGroup) => {
    if (!g.workbuddy || g.providerId == null) return null;
    const v = credits[g.providerId];
    const busy = !!creditsBusy[g.providerId];
    return (
      <span className="mp-credits" title="workbuddy 积分余额">
        {v != null ? `积分 ${fmtCredits(v)}` : busy ? "刷新中…" : "积分 --"}
        <span
          role="button"
          className={"mp-credits-refresh" + (busy ? " busy" : "")}
          title="刷新积分余额"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => { e.preventDefault(); e.stopPropagation(); if (!busy) void refreshCredits(g.providerId!); }}
        >
          <IconRefresh size={11} />
        </span>
      </span>
    );
  };

  const activeModel = models.find((m) => m.id === value);
  const label = activeModel
    ? activeModel.provider_name
      ? `${activeModel.provider_name}/${activeModel.name}`
      : activeModel.name
    : "模型";

  return (
    <div className="composer-model-wrap">
      <DropdownMenu.Root open={open} onOpenChange={(next) => { if (next !== open) onToggle(); }}>
        <DropdownMenu.Trigger asChild>
          <button className="composer-model-badge" type="button" title={label}>
            <IconCpu size={13} />
            <span className="mp-label">{label}</span>
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          {/* side=top：输入框贴近窗口底部，向上展开；align=start 与触发按钮左缘对齐，
              保证菜单文本整体左对齐（plan-238-1210）；越界翻转/平移由 avoidCollisions 处理 */}
          <DropdownMenu.Content
            className="mp-menu"
            side="top"
            align="start"
            sideOffset={6}
            collisionPadding={8}
          >
            <div className="mp-menu-head">选择模型</div>
            {groups.length === 0 ? (
              <>
                <div className="mp-models-empty">暂无可用模型，请先在设置中添加</div>
                <DropdownMenu.Separator className="mp-sep" />
                <DropdownMenu.Item className="mp-item" onSelect={() => openModelSettings()}>
                  <span className="mp-item-check" />
                  <span className="mp-item-name">管理模型</span>
                </DropdownMenu.Item>
              </>
            ) : (
              <>
                {groups.map((g) => {
                  const holdsCurrent = g.models.some((m) => m.id === value);
                  return (
                    <DropdownMenu.Sub key={g.name}>
                      <DropdownMenu.SubTrigger className="mp-group">
                        <span className="mp-group-name" title={g.name}>{g.name}</span>
                        {renderCredits(g)}
                        {holdsCurrent
                          ? <span className="mp-group-current"><IconCheck size={12} /></span>
                          : <span className="mp-group-arrow"><IconChevronRight size={13} /></span>}
                      </DropdownMenu.SubTrigger>
                      <DropdownMenu.Portal>
                        <DropdownMenu.SubContent
                          className="mp-menu mp-sub-menu"
                          sideOffset={6}
                          alignOffset={-5}
                          collisionPadding={8}
                        >
                          <div className="mp-menu-head">{g.name}{renderCredits(g)}</div>
                          {g.models.map((m) => (
                            <DropdownMenu.Item
                              key={m.id}
                              className={"mp-item" + (m.id === value ? " active" : "")}
                              title={m.name}
                              onSelect={() => onChange(m.id)}
                            >
                              <span className="mp-item-check">{m.id === value && <IconCheck size={12} />}</span>
                              <span className="mp-item-name">{m.name}</span>
                              <ModelTags m={m} />
                            </DropdownMenu.Item>
                          ))}
                        </DropdownMenu.SubContent>
                      </DropdownMenu.Portal>
                    </DropdownMenu.Sub>
                  );
                })}
                <DropdownMenu.Separator className="mp-sep" />
                <DropdownMenu.Item className="mp-item" onSelect={() => openModelSettings()}>
                  <span className="mp-item-check" />
                  <span className="mp-item-name">管理模型</span>
                </DropdownMenu.Item>
              </>
            )}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  );
}
