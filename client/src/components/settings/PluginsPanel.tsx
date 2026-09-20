/** v19 插件页：系统组件插件化管理。
 * 按 slot 列出内置/外挂插件，像拼积木一样下拉选择各 slot 的生效组件；
 * 支持恢复默认；外挂插件可禁用（卸载其注册）。
 */
import { useMemo, useSyncExternalStore } from "react";
import {
  listPlugins, listSlotPlugins, getActivePlugin, replaceSlot, resetSlot,
  unregisterExternal, subscribeRegistry, registryVersion, type SlotId,
} from "../../plugins/registry";
import { Select } from "../ui";

const SLOT_LABELS: Record<SlotId, string> = {
  sidebar: "左侧会话栏",
  "settings-sidebar": "设置侧栏",
  titlebar: "顶部标题栏",
  "message-flow": "消息流",
  composer: "对话输入框",
  "empty-state": "空态首页",
  "right-panel": "右侧面板",
  "thinking-block": "思考块",
  "tool-tree": "工具调用树",
  "subagent-card": "子代理卡片",
  "plan-card": "计划卡",
  "compact-card": "压缩卡",
};

export function PluginsPanel() {
  useSyncExternalStore(subscribeRegistry, registryVersion);
  const slots = useMemo(() => Object.keys(SLOT_LABELS) as SlotId[], []);
  const all = listPlugins();
  const extCount = all.filter((p) => !p.builtin).length;

  return (
    <div>
      <div className="settings-card-title">
        系统组件（{all.length} 个插件，其中外挂 {extCount} 个）
      </div>
      <div className="settings-resource-list">
        {slots.map((slot) => {
          const plugins = listSlotPlugins(slot);
          const active = getActivePlugin(slot);
          return (
            <div className="plugin-slot-row" key={slot}>
              <span className="plugin-slot-name">{SLOT_LABELS[slot]}</span>
              <span className="plugin-slot-desc" title={active?.description}>{active?.description || "—"}</span>
              <Select
                value={active?.id ?? ""}
                onChange={(id) => replaceSlot(slot, id)}
                options={plugins.map((p) => ({
                  value: p.id,
                  label: `${p.name}${p.builtin ? "（内置）" : "（外挂）"}`,
                }))}
                className="plugin-slot-select"
                aria-label={`${SLOT_LABELS[slot]} 生效组件`}
              />
              {active && !active.builtin && (
                <button className="btn btn-ghost btn-xs" title="禁用外挂插件并恢复内置" onClick={() => unregisterExternal(active.id)}>禁用</button>
              )}
              {active && active.id !== plugins.find((p) => p.builtin)?.id && (
                <button className="btn btn-ghost btn-xs" onClick={() => resetSlot(slot)}>恢复默认</button>
              )}
            </div>
          );
        })}
      </div>
      <div className="settings-row" style={{ marginTop: 12 }}>
        <div className="settings-row-info">
          <div className="settings-row-title">外挂插件协议</div>
          <div className="settings-row-desc">
            在 ~/.chatcoder/plugins/&lt;目录&gt;/ 放置 plugin.json（id/name/slot/entry/description）与入口 js，
            入口内调用 <code>api.registerPlugin(...)</code>（传入 id/name/slot/description/builtin:false/component）即可替换对应系统组件；重启应用生效。
          </div>
        </div>
      </div>
    </div>
  );
}


