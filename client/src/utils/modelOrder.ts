/** 模型排序工具（plan-89-386）：全局选择模型的地方统一按「设置-模型管理」的
 *  供应商顺序展示——即用户在设置页左列拖拽调整的顺序（`provider.sort_order`，
 *  后端已随模型列表下发），此前部分入口按供应商名称字母序排列，两处不一致。
 *
 * 排序键与后端 `list_providers`（sort_order asc, id asc）同口径：
 *  供应商排序位 → 供应商 id → 模型名；独立模型（无供应商）恒排最后。
 * 老库 `sort_order` 全为 0 时退化为 id 序，与设置页默认顺序一致（升级零感知）。
 */
import type { ModelOut } from "../api/client";

/** 独立模型（无供应商）的排序位：恒排最后 */
const INDEPENDENT_RANK = Number.MAX_SAFE_INTEGER;

/** 供应商排序位（ModelPicker 的分组排序与平铺列表排序共用） */
export function providerRank(m: ModelOut): number {
  return m.provider_id == null ? INDEPENDENT_RANK : (m.provider_sort_order ?? 0);
}

/** 比较：供应商顺序 → 供应商 id → 模型名（与设置页左列同口径） */
export function compareModelsByProvider(a: ModelOut, b: ModelOut): number {
  const ra = providerRank(a);
  const rb = providerRank(b);
  if (ra !== rb) return ra - rb;
  const ia = a.provider_id ?? 0;
  const ib = b.provider_id ?? 0;
  if (ia !== ib) return ia - ib;
  return a.name.localeCompare(b.name);
}

/** 返回按供应商顺序排好的副本（不改动入参） */
export function sortModelsByProvider(models: ModelOut[]): ModelOut[] {
  return [...models].sort(compareModelsByProvider);
}
