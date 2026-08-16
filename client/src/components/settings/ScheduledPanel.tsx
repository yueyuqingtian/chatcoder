/** 设置中心：定时任务（v2.2 对齐 zcode 3.18）。 */
import { api, type ScheduledTaskOut } from "../../api/client";
import { GenericPanel } from "./shared";

export function ScheduledPanel() {
  return (
    <GenericPanel<ScheduledTaskOut>
      loader={() => api.listScheduledTasks()}
      getName={(t) => t.name}
      getDesc={(t) => t.cron + (t.next_run_at ? " - " + t.next_run_at : "")}
      onToggle={async (t, v) => api.updateScheduledTask(t.id, { enabled: v })}
      onDelete={async (t) => api.deleteScheduledTask(t.id)}
      getActive={(t) => t.enabled}
    />
  );
}
