/** 设置中心：记忆（v2.2 对齐 zcode 3.18）。 */
import { api, type MemoryEntryOut } from "../../api/client";
import { GenericPanel } from "./shared";

export function MemoryPanel() {
  return (
    <GenericPanel<MemoryEntryOut>
      loader={() => api.listMemories()}
      getName={(m) => m.text}
      getDesc={(m) => m.kind + " - 使用 " + m.usage_count + " 次"}
      onDelete={async (m) => api.deleteMemory(m.id)}
      getActive={() => false}
    />
  );
}
