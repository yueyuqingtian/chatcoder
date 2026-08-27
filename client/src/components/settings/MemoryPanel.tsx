/** 设置中心：记忆（v2.2 对齐 zcode 3.18）。 */
import { useCallback, useEffect, useState } from "react";
import { api, type MemoryEntryOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { GenericPanel, Row, Sw } from "./shared";

export function MemoryPanel() {
  const [memoryEnabled, setMemoryEnabled] = useState(true);

  const loadSettings = useCallback(async () => {
    try {
      const g = await api.getGlobalSettings();
      setMemoryEnabled(g.memory_enabled !== false);
    } catch {}
  }, []);

  useEffect(() => { loadSettings(); }, [loadSettings]);

  const handleToggle = async (val: boolean) => {
    setMemoryEnabled(val);
    try {
      await api.setGlobalSettings({ memory_enabled: val });
    } catch (e) {
      useChatStore.setState({ error: "保存失败: " + String(e) });
      setMemoryEnabled(!val);
    }
  };

  return (
    <div>
      <div className="settings-card" style={{ marginBottom: 12 }}>
        <Row title="AI 主动生成记忆" desc="开启后每轮对话结束时，AI 会自主提取关键事实/偏好写入记忆库；关闭则不再自动生成记忆">
          <Sw checked={memoryEnabled} onChange={handleToggle} />
        </Row>
      </div>
      <GenericPanel<MemoryEntryOut>
        loader={() => api.listMemories()}
        getName={(m) => m.text}
        getDesc={(m) => m.kind + " - 使用 " + m.usage_count + " 次"}
        onDelete={async (m) => api.deleteMemory(m.id)}
        getActive={() => false}
      />
    </div>
  );
}
