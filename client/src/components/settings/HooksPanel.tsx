/** 设置中心：钩子（v2.2 对齐 zcode 3.18）。 */
import { api, type HookConfigOut } from "../../api/client";
import { GenericPanel } from "./shared";

export function HooksPanel() {
  return (
    <GenericPanel<HookConfigOut>
      loader={() => api.listHooks()}
      getName={(h) => h.event}
      getDesc={(h) => h.command}
      onToggle={async (h, v) => api.updateHook(h.id, { enabled: v })}
      onDelete={async (h) => api.deleteHook(h.id)}
      getActive={(h) => h.enabled}
    />
  );
}
