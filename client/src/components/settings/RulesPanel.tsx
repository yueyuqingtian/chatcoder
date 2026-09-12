/** 设置中心：AI 规则（v2.2 对齐 zcode 3.18）。
 * 全局 / 项目规则，以及多 AI 软件规则文档的扫描与启用。
 * plan-234-1171 R7: 项目规则按「具体项目」存储——
 *   修复前读写一律用服务端 settings.workspace_root，与注入侧按会话项目路径读取的
 *   键不一致（本机 workspace_root 是 F:\project\yipinCode，而当前项目是
 *   D:\myProject\chatcoder），用户写的规则永远注入不进去，面板也不显示归属项目。
 *   现在顶部可选项目并显式展示「项目名 · 路径」，切换即重新拉取该项目规则。 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconFolder } from "../icons";
import { Sw } from "./shared";

export function RulesPanel() {
  const [sources, setSources] = useState<Array<{ source: string; label: string; enabled: boolean }>>([]);
  const [globalRules, setGlobalRules] = useState("");
  const [workdirRules, setWorkdirRules] = useState("");
  const [scanned, setScanned] = useState<Array<{ source: string; label: string; path: string; exists: boolean; kind: string }>>([]);
  const [saving, setSaving] = useState(false);
  // 当前配置归属的项目路径（决定项目规则的存取键）
  const [projectPath, setProjectPath] = useState<string>("");

  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const activeProjects = projects.filter((p) => !p.archived);

  // 默认选中当前项目；用户切换项目后以其选择为准
  useEffect(() => {
    if (projectPath) return;
    const cur = activeProjects.find((p) => p.id === currentProjectId) ?? activeProjects[0];
    if (cur) setProjectPath(cur.path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProjectId, projects]);

  const load = useCallback(async (path: string) => {
    try {
      // project_path 同时用于读取项目规则与扫描该项目根下的规则文档
      const cfg = await api.getAiRules(path || undefined);
      setSources(cfg.sources);
      setGlobalRules(cfg.global_rules || "");
      setWorkdirRules(cfg.workdir_rules || "");
      // 回传的高亮归属路径可能与传入值规范化后不同（resolve 结果），以其为准更准确
      if (cfg.project_path && !path) setProjectPath(cfg.project_path);
    } catch { /* 忽略：面板保持上次内容 */ }
    try { setScanned(await api.scanAiRules(path || undefined)); } catch { /* 忽略 */ }
  }, []);

  useEffect(() => { if (projectPath) void load(projectPath); }, [projectPath, load]);

  const handleScan = async () => {
    try { setScanned(await api.scanAiRules(projectPath || undefined)); }
    catch (e) { useChatStore.setState({ error: "扫描失败: " + String(e) }); }
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
        project_path: projectPath || undefined,
      });
      await load(projectPath);
    } catch (e) { useChatStore.setState({ error: "保存失败: " + String(e) }); }
    finally { setSaving(false); }
  };

  const selectedProject = activeProjects.find((p) => p.path === projectPath);

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <span style={{ fontSize: 12, color: "var(--text-3)" }}>扫描项目根目录下常见 AI 软件的规则文档，并按来源启用 / 停用</span>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-ghost btn-sm" onClick={handleScan}><IconRefresh size={13} /> 重新扫描</button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>{saving ? "保存中…" : "保存配置"}</button>
        </div>
      </div>

      {/* plan-234-1171 R7: 明确「规则配给哪个项目」——可切换项目，并显式展示归属名与路径 */}
      <div className="settings-card-title">项目规则归属</div>
      <div className="rules-project-picker">
        <IconFolder size={13} />
        <select
          className="ui-select rules-project-select"
          value={selectedProject?.id ?? ""}
          onChange={(e) => {
            const picked = activeProjects.find((p) => String(p.id) === e.target.value);
            if (picked) setProjectPath(picked.path);
          }}
        >
          {activeProjects.length === 0 && <option value="">暂无项目</option>}
          {activeProjects.map((p) => (
            <option key={p.id} value={p.id}>{p.name || p.path}</option>
          ))}
        </select>
        <span className="rules-project-path" title={projectPath}>{projectPath || "未选择项目"}</span>
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

      <div className="settings-card-title">全局规则</div>
      <textarea
        className="ui-textarea"
        rows={8}
        style={{ minHeight: 160 }}
        placeholder="全局规则（对所有项目生效）…"
        value={globalRules}
        onChange={(e) => setGlobalRules(e.target.value)}
      />

      <div className="settings-card-title">
        项目规则{selectedProject ? `（${selectedProject.name || selectedProject.path}）` : ""}
      </div>
      <textarea
        className="ui-textarea"
        rows={8}
        style={{ minHeight: 160 }}
        placeholder="当前项目规则…"
        value={workdirRules}
        onChange={(e) => setWorkdirRules(e.target.value)}
      />
    </div>
  );
}
