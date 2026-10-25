/** 模型按供应商分组（plan-20261025-composer）：设置页 ModelPicker 与输入框「模型 · 思考深度」浮层共用。
 *
 * 抽成独立模块是为了两处的可用性过滤与排序口径永远一致——此前只有 ModelPicker 一处实现，
 * 输入框改成「供应商 → 模型」两段式浮层后，若各写一份，停用 / 禁用 / trae 目录过滤迟早会漂移。
 * workbuddy 积分余额的查询与徽标同理。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ModelOut } from "../../api/client";
import { providerRank } from "../../utils/modelOrder";
import { IconRefresh } from "../icons";
import { Badge, Tooltip } from "../ui";

/** 登录授权类供应商（凭据是账号而非 API Key）：设置页凭据徽标与输入框供应商标签共用 */
export const OAUTH_FORMATS = new Set(["ta3", "workbuddy", "trae", "kiro"]);

export interface ModelGroup {
  /** 分组键：供应商名（无供应商的为「独立模型」） */
  name: string;
  models: ModelOut[];
  /** 供应商 id（独立模型为 null） */
  providerId: number | null;
  /** 供应商接入格式（独立模型为 null）：区分「API」与「账号」 */
  apiFormat: string | null;
  /** workbuddy 供应商：展示积分余额 + 刷新按钮 */
  workbuddy: boolean;
  /** plan-89-386: 组排序位（本组供应商在设置页的顺序位；独立模型排最后） */
  sortOrder: number;
}

export function openModelSettings() {
  window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "models" } }));
}

/** 按供应商分组。value = 当前选中模型：即使已停用 / 所属供应商被禁用也保留显示，
 *  避免「当前模型突然消失」造成困惑。 */
export function buildModelGroups(models: ModelOut[], value: number | null): ModelGroup[] {
  const map = new Map<string, ModelGroup>();
  for (const m of models) {
    if (!m.is_active && m.id !== value) continue;
    // plan-248-1258 M2.4: 供应商被禁用时其模型不进入选择器
    if (m.provider_active === false && m.id !== value) continue;
    // trae 供应商：TRAE 目录含大量工具/占位模型，客户端实际可用的才展示
    if (m.api_format === "trae" && !m.trae_available) continue;
    const g = m.provider_name || "独立模型";
    if (!map.has(g)) {
      map.set(g, {
        name: g, models: [], providerId: m.provider_id ?? null,
        apiFormat: m.provider_id != null ? m.api_format : null,
        workbuddy: m.api_format === "workbuddy", sortOrder: providerRank(m),
      });
    }
    map.get(g)!.models.push(m);
  }
  // 组内按名称排序；组按「设置-模型管理」的供应商顺序排序（plan-89-386），
  // 排序位相同（或老库全为 0）时按供应商 id 兜底（与后端 list_providers 同口径）；
  // 独立模型的排序位为哨兵值，恒排最后。
  const arr = [...map.values()].map((g) => ({
    ...g,
    models: g.models.sort((a, b) => a.name.localeCompare(b.name)),
  }));
  arr.sort((a, b) =>
    a.sortOrder - b.sortOrder
    || (a.providerId ?? 0) - (b.providerId ?? 0)
    || a.name.localeCompare(b.name));
  return arr;
}

/** 供应商计费方式短标签：账号登录类为「账号」，其余为「API」；独立模型不标 */
export function providerKindLabel(g: ModelGroup): string | null {
  if (g.apiFormat == null) return null;
  return OAUTH_FORMATS.has(g.apiFormat) ? "账号" : "API";
}

/** workbuddy 积分余额：浮层每次打开实时刷新；点击图标手动刷新 */
export function useWorkbuddyCredits(groups: ModelGroup[], open: boolean) {
  const [credits, setCredits] = useState<Record<number, number | null>>({});
  const [busy, setBusy] = useState<Record<number, boolean>>({});
  /** plan-562-2318 B9：组件卸载 / 分组变化后，迟到的响应不得再写状态。
   *  否则浮层关掉又重开、或供应商被删掉之后，旧 provider 的余额会写回到界面上。 */
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const refresh = useCallback(async (providerId: number) => {
    setBusy((s) => ({ ...s, [providerId]: true }));
    try {
      const r = await api.workbuddyCredits(providerId, { refresh: true });
      // 多账号时按合计展示（仅统计已登录且余额已知的账号）
      const vals = r.credentials.filter((c) => c.logged_in && c.credits != null).map((c) => Number(c.credits));
      if (!aliveRef.current) return;
      setCredits((s) => ({ ...s, [providerId]: vals.length > 0 ? vals.reduce((a, b) => a + b, 0) : null }));
    } catch { /* 查询失败保留旧值 */ } finally {
      if (aliveRef.current) setBusy((s) => ({ ...s, [providerId]: false }));
    }
  }, []);
  // 以 id 串作依赖：分组随选中值重算时不重复拉取
  const idsKey = useMemo(
    () => groups.filter((g) => g.workbuddy && g.providerId != null).map((g) => g.providerId!).join(","),
    [groups],
  );
  useEffect(() => {
    if (!open || !idsKey) return;
    for (const id of idsKey.split(",")) void refresh(Number(id));
  }, [open, idsKey, refresh]);
  return { credits, busy, refresh };
}

const fmtCredits = (v: number) => v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });

/** 积分徽标 + 刷新按钮（设置页供应商行 / 子菜单头、输入框供应商行共用） */
export function CreditsBadge({ value, busy, onRefresh }: {
  value: number | null | undefined;
  busy: boolean;
  onRefresh: () => void;
}) {
  return (
    <Tooltip content="workbuddy 积分余额">
      <span className="mp-credits">
        {value != null ? `积分 ${fmtCredits(value)}` : busy ? "刷新中…" : "积分 --"}
        <Tooltip content="刷新积分余额">
          <span
            role="button"
            className={"mp-credits-refresh" + (busy ? " busy" : "")}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); if (!busy) onRefresh(); }}
          >
            <IconRefresh size={11} />
          </span>
        </Tooltip>
      </span>
    </Tooltip>
  );
}

/** 模型能力短标签（视觉 / 长上下文 / 消耗倍率）：文字徽标，比图标更直观（参考图2「视觉」） */
export function ModelTagBadges({ m }: { m: ModelOut }) {
  if (!m.is_multimodal && !m.trae_max_context && !m.trae_consumption_rate) return null;
  return (
    <span className="cc-model-tags">
      {m.is_multimodal ? (
        <Tooltip content="支持图片输入（多模态）"><Badge size="xs">视觉</Badge></Tooltip>
      ) : null}
      {m.trae_max_context ? <Badge size="xs">长上下文</Badge> : null}
      {m.trae_consumption_rate ? <Badge size="xs">消耗×{m.trae_consumption_rate}</Badge> : null}
    </span>
  );
}
