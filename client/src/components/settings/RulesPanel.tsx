/** 设置中心：AI 规则（v2.2 对齐 zcode 3.18）。
 * 全局 / 项目规则，以及多 AI 软件规则文档的扫描与启用。 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh } from "../icons";
import { Sw } from "./shared";

export function RulesPanel() {
  const [sources, setSources] = useState<Array<{ source: string; label: string; enabled: boolean }>>([]);
  const [globalRules, setGlobalRules] = useState("");
  const [workdirRules, setWorkdirRules] = useState("");
  const [scanned, setScanned] = useState<Array<{ source: string; label: string; path: string; exists: boolean; kind: string }>>([]);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const cfg = await api.getAiRules();
      setSources(cfg.sources);
      setGlobalRules(cfg.global_rules || "");
      setWorkdirRules(cfg.workdir_rules || "");
    } catch {}
    try { setScanned(await api.scanAiRules()); } catch {}
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleScan = async () => {
    try { setScanned(await api.scanAiRules()); } catch (e) { useChatStore.setState({ error: "扫描失败: " + String(e) }); }
  };

  const toggleSource = (src: string) => {
    setSources((prev) => prev.map((s) => (s.source === src ? { ...s, enabled: !s.enabled } : s)));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.setAiRules({
        enabled_sources: sources.filter((s) => s.enabled).map((s) => s.source),
        global_rules: globalRules,
        workdir_rules: workdirRules,
      });
      load();
    } catch (e) { useChatStore.setState({ error: "保存失败: " + String(e) }); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <span style={{ fontSize: 12, color: "var(--text-3)" }}>扫描项目根目录下常见 AI 软件的规则文档，并按来源启用 / 停用</span>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-ghost btn-sm" onClick={handleScan}><IconRefresh size={13} /> 重新扫描</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>{saving ? "保存中…" : "保存配置"}</button>
        </div>
      </div>

      <div className="settings-card-title">规则来源（启用后该来源的规则文档会注入 Agent 上下文）</div>
      <div className="settings-resource-list">
        {sources.map((s) => (
          <div key={s.source} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">{s.label}</div>
              <div className="settings-resource-desc">
                {scanned.filter((x) => x.source === s.source).map((x) => (
                  <span key={x.path} className="settings-resource-tag" style={{ color: "var(--accent)" }}>{x.path}</span>
                ))}
                {scanned.filter((x) => x.source === s.source).length === 0 && <span className="settings-resource-tag">未发现规则文档</span>}
              </div>
            </div>
            <Sw checked={s.enabled} onChange={() => toggleSource(s.source)} />
          </div>
        ))}
        {sources.length === 0 && <div className="navpage-empty">暂无规则来源</div>}
      </div>

      <div style={{ marginTop: 16 }} className="settings-card-title">全局规则</div>
      <textarea
        className="ui-textarea"
        rows={4}
        placeholder="全局规则（对所有项目生效）…"
        value={globalRules}
        onChange={(e) => setGlobalRules(e.target.value)}
      />

      <div style={{ marginTop: 16 }} className="settings-card-title">项目规则</div>
      <textarea
        className="ui-textarea"
        rows={4}
        placeholder="当前项目规则…"
        value={workdirRules}
        onChange={(e) => setWorkdirRules(e.target.value)}
      />
    </div>
  );
}
