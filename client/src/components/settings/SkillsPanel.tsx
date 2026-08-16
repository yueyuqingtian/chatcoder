/** 设置中心：技能管理（v2.2 对齐 zcode 3.18）。
 * 本地技能启停 + 云端 Git 技能仓库（添加/同步/导入）+ 本地目录/md 文件导入（v1.1）。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type SkillOut } from "../../api/client";
import { IconRefresh, IconPlus, IconX, IconFolder } from "../icons";
import { Sw } from "./shared";

export function SkillsPanel() {
  const [items, setItems] = useState<SkillOut[]>([]);
  const [repos, setRepos] = useState<Array<{ id: string; name: string; url: string; synced?: boolean; skill_count?: number }>>([]);
  const [showAddRepo, setShowAddRepo] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const [repoName, setRepoName] = useState("");
  const [syncingRepo, setSyncingRepo] = useState<string | null>(null);
  const [repoSkills, setRepoSkills] = useState<{ repoId: string; name: string; skills: Array<{ name: string; display_name: string; description: string; path: string }> } | null>(null);
  // v1.1: 本地导入
  const [showImportMenu, setShowImportMenu] = useState(false);
  const [importing, setImporting] = useState(false);
  const importMenuRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try { setItems(await api.listSkills()); } catch {}
    try { setRepos(await api.listSkillRepos()); } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!showImportMenu) return;
    const handler = (e: MouseEvent) => {
      if (importMenuRef.current && !importMenuRef.current.contains(e.target as Node)) setShowImportMenu(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showImportMenu]);

  const doImport = useCallback(async (paths: string[]) => {
    if (paths.length === 0) return;
    setImporting(true);
    const okNames: string[] = [];
    try {
      for (const p of paths) {
        const res = await api.importLocalSkill({ path: p, mode: "copy" });
        okNames.push(...res.imported);
        if (res.skipped.length > 0) alert(`以下技能已存在，已跳过：${res.skipped.join("、")}`);
      }
      await load();
      if (okNames.length > 0) alert(`成功导入 ${okNames.length} 个技能：${okNames.join("、")}`);
    } catch (e) { alert("导入失败: " + String(e)); }
    finally { setImporting(false); }
  }, [load]);

  const handleImportLocal = async () => {
    setShowImportMenu(false);
    const api_ = window.chatcoderAPI;
    if (api_?.selectDirectory) {
      const dir = await api_.selectDirectory();
      if (dir) doImport([dir]);
      return;
    }
    // web 降级：输入绝对路径
    const p = prompt("请输入技能目录或 .md 文件的绝对路径：");
    if (p && p.trim()) doImport([p.trim()]);
  };

  const handleImportLocalFiles = async () => {
    setShowImportMenu(false);
    const api_ = window.chatcoderAPI;
    if (api_?.selectFiles) {
      const files = await api_.selectFiles([{ name: "Markdown", extensions: ["md"] }]);
      if (files && files.length > 0) doImport(files);
      return;
    }
    // web 降级：浏览器拿不到本地绝对路径，改为输入路径
    const p = prompt("请输入 .md 文件的绝对路径（多个用逗号分隔）：");
    if (p && p.trim()) doImport(p.split(/[,，]/).map((x) => x.trim()).filter(Boolean));
  };

  const handleAddRepo = async () => {
    if (!repoUrl.trim()) return;
    try {
      await api.createSkillRepo({ url: repoUrl.trim(), name: repoName.trim() || undefined });
      setShowAddRepo(false); setRepoUrl(""); setRepoName(""); load();
    } catch (e) { alert(String(e)); }
  };

  const handleSync = async (repoId: string, name: string) => {
    setSyncingRepo(repoId);
    try {
      const res = await api.syncSkillRepo(repoId);
      setRepoSkills({ repoId, name, skills: res.skills });
      load();
    } catch (e) { alert("同步失败: " + String(e)); }
    finally { setSyncingRepo(null); }
  };

  const handleImport = async (skillName: string) => {
    if (!repoSkills) return;
    try {
      await api.importRepoSkill(repoSkills.repoId, skillName);
      load();
    } catch (e) { alert(String(e)); }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-ghost btn-sm" onClick={load}><IconRefresh size={13} /> 刷新扫描</button>
        <div style={{ position: "relative" }} ref={importMenuRef}>
          <button className="btn btn-ghost btn-sm" onClick={() => setShowImportMenu((v) => !v)} disabled={importing}>
            <IconFolder size={13} /> {importing ? "导入中…" : "导入本地技能"}
          </button>
          {showImportMenu && (
            <div className="composer-menu" style={{ position: "absolute", right: 0, top: "100%", marginTop: 4, zIndex: 50, minWidth: 160 }}>
              <button onClick={handleImportLocal} style={{ display: "block", width: "100%", padding: "6px 10px", border: "none", background: "transparent", color: "var(--text-1)", textAlign: "left", cursor: "pointer", fontSize: 12 }}>选择目录…</button>
              <button onClick={handleImportLocalFiles} style={{ display: "block", width: "100%", padding: "6px 10px", border: "none", background: "transparent", color: "var(--text-1)", textAlign: "left", cursor: "pointer", fontSize: 12 }}>选择 .md 文件…</button>
            </div>
          )}
        </div>
        <button className="btn btn-primary btn-sm" onClick={() => setShowAddRepo(!showAddRepo)}><IconPlus size={13} /> 添加技能仓库</button>
      </div>

      {showAddRepo && (
        <div className="settings-create-form">
          <input className="ui-input" placeholder="Git 仓库地址 (https://…/repo.git)" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
          <input className="ui-input" placeholder="仓库名称（可选）" value={repoName} onChange={(e) => setRepoName(e.target.value)} />
          <div className="settings-create-actions">
            <button className="btn btn-ghost btn-sm" onClick={() => setShowAddRepo(false)}>取消</button>
            <button className="btn btn-primary btn-sm" onClick={handleAddRepo} disabled={!repoUrl.trim()}>添加</button>
          </div>
        </div>
      )}

      {repos.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div className="settings-card-title">技能仓库（云端 Git）</div>
          <div className="settings-resource-list">
            {repos.map((r) => (
              <div key={r.id} className="settings-resource-item">
                <div className="settings-resource-info">
                  <div className="settings-resource-name">{r.name}</div>
                  <div className="settings-resource-desc">
                    <span className="settings-resource-tag">{r.synced ? `${r.skill_count ?? 0} 个技能` : "未同步"}</span>
                    <span style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.url}</span>
                  </div>
                </div>
                <div className="settings-resource-actions">
                  <button className="btn btn-ghost btn-xs" onClick={() => handleSync(r.id, r.name)} disabled={syncingRepo === r.id}>
                    {syncingRepo === r.id ? "同步中…" : "同步/查看"}
                  </button>
                  <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除仓库？")) { try { await api.deleteSkillRepo(r.id); load(); } catch {} } }}><IconX size={12} /></button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {repoSkills && (
        <div style={{ marginBottom: 16, padding: 10, border: "1px solid var(--border)", borderRadius: "var(--r-sm)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-2)", marginBottom: 8 }}>仓库技能：{repoSkills.name}</div>
          {repoSkills.skills.length === 0 && <div className="navpage-empty">仓库中未发现技能（请确保仓库含 skills/*.md）</div>}
          {repoSkills.skills.map((sk) => (
            <div key={sk.name} className="settings-resource-item" style={{ marginBottom: 4 }}>
              <div className="settings-resource-info">
                <div className="settings-resource-name">{sk.display_name || sk.name}</div>
                <div className="settings-resource-desc"><span>{sk.description || "无描述"}</span><span className="settings-resource-tag">{sk.path}</span></div>
              </div>
              <button className="btn btn-primary btn-xs" onClick={() => handleImport(sk.name)}>导入并启用</button>
            </div>
          ))}
        </div>
      )}

      <div className="settings-resource-list">
        {items.map((s) => (
          <div key={s.id} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">{s.display_name || s.name}</div>
              <div className="settings-resource-desc"><span>{s.description || "无描述"}</span><span className="settings-resource-tag" title={s.path || undefined}>{s.source}</span></div>
            </div>
            <div className="settings-resource-actions">
              <Sw checked={s.is_active} onChange={async (v) => { try { await api.updateSkill(s.id, { is_active: v }); load(); } catch {} }} />
              <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除？")) { try { await api.deleteSkill(s.id); load(); } catch {} } }}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无技能</div>}
      </div>
    </div>
  );
}
