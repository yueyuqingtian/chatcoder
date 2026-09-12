/** 设置中心：技能管理（v2.2 对齐 zcode 3.18）。
 * 本地技能启停 + 云端 Git 技能仓库（添加/同步/导入）+ 本地目录/md 文件导入（v1.1）。 */
import { useCallback, useEffect, useState } from "react";
import { api, type SkillOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { IconRefresh, IconPlus, IconX, IconFolder } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { FormDialog } from "../ui/FormDialog";
import { Sw } from "./shared";

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点，统一改用全局提示条。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

export function SkillsPanel() {
  const [items, setItems] = useState<SkillOut[]>([]);
  const [repos, setRepos] = useState<Array<{ id: string; name: string; url: string; synced?: boolean; skill_count?: number }>>([]);
  const [showAddRepo, setShowAddRepo] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const [repoName, setRepoName] = useState("");
  const [syncingRepo, setSyncingRepo] = useState<string | null>(null);
  const [repoSkills, setRepoSkills] = useState<{ repoId: string; name: string; skills: Array<{ name: string; display_name: string; description: string; path: string }> } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ kind: "repo"; id: string; name: string } | { kind: "skill"; id: number; name: string } | null>(null);
  // v1.1: 本地导入
  const [importing, setImporting] = useState(false);
  // plan-230-1144 M1.2: 技能详情展开 + trigger 草稿（失焦即保存）
  const [expandedSkill, setExpandedSkill] = useState<number | null>(null);
  const [triggerDrafts, setTriggerDrafts] = useState<Record<number, string>>({});

  const saveTrigger = async (s: SkillOut) => {
    const draft = triggerDrafts[s.id];
    if (draft === undefined || draft === (s.trigger ?? "")) return;
    try { await api.updateSkill(s.id, { trigger: draft }); await load(); }
    catch (e) { notify("保存触发条件失败: " + String(e)); }
  };

  /** 复刻后端 _load_skills_and_mcp 的注入格式，让用户在 UI 上看到 AI 实际"看到"的那行字 */
  const renderSkillPromptLine = (s: SkillOut) => {
    const desc = (s.description || "").slice(0, 200);
    const trig = (triggerDrafts[s.id] ?? s.trigger ?? "").slice(0, 120);
    return `- ${s.name}: ${desc}${trig ? ` [触发条件: ${trig}]` : ""}`;
  };

  const load = useCallback(async () => {
    try { setItems(await api.listSkills()); } catch {}
    try { setRepos(await api.listSkillRepos()); } catch {}
  }, []);
  useEffect(() => { load(); }, [load]);

  const doImport = useCallback(async (paths: string[]) => {
    if (paths.length === 0) return;
    setImporting(true);
    const okNames: string[] = [];
    try {
      for (const p of paths) {
        const res = await api.importLocalSkill({ path: p, mode: "copy" });
        okNames.push(...res.imported);
        if (res.skipped.length > 0) notify(`以下技能已存在，已跳过：${res.skipped.join("、")}`);
      }
      await load();
      if (okNames.length > 0) notify(`成功导入 ${okNames.length} 个技能：${okNames.join("、")}`);
    } catch (e) { notify("导入失败: " + String(e)); }
    finally { setImporting(false); }
  }, [load]);

  // v19: 点击「导入本地技能」直接弹出系统选择器（目录或 .md 文件混合选择），不再出现下拉小菜单
  const handleImportLocal = async () => {
    const api_ = window.chatcoderAPI;
    if (api_?.selectFiles) {
      const paths = await api_.selectFiles([{ name: "Markdown", extensions: ["md"] }], { allowDirectories: true });
      if (paths && paths.length > 0) doImport(paths);
      return;
    }
    if (api_?.selectDirectory) {
      const dir = await api_.selectDirectory();
      if (dir) doImport([dir]);
      return;
    }
    // web 降级：输入绝对路径
    const p = prompt("请输入技能目录或 .md 文件的绝对路径（多个用逗号分隔）：");
    if (p && p.trim()) doImport(p.split(/[,，]/).map((x) => x.trim()).filter(Boolean));
  };

  const handleAddRepo = async () => {
    if (!repoUrl.trim()) return;
    try {
      await api.createSkillRepo({ url: repoUrl.trim(), name: repoName.trim() || undefined });
      setShowAddRepo(false); setRepoUrl(""); setRepoName(""); load();
    } catch (e) { notify(String(e)); }
  };

  const handleSync = async (repoId: string, name: string) => {
    setSyncingRepo(repoId);
    try {
      const res = await api.syncSkillRepo(repoId);
      setRepoSkills({ repoId, name, skills: res.skills });
      load();
    } catch (e) { notify("同步失败: " + String(e)); }
    finally { setSyncingRepo(null); }
  };

  const handleImport = async (skillName: string) => {
    if (!repoSkills) return;
    try {
      await api.importRepoSkill(repoSkills.repoId, skillName);
      load();
    } catch (e) { notify(String(e)); }
  };

  const doDelete = async () => {
    const target = confirmDelete;
    setConfirmDelete(null);
    if (!target) return;
    try {
      if (target.kind === "repo") { await api.deleteSkillRepo(target.id); if (repoSkills?.repoId === target.id) setRepoSkills(null); }
      else await api.deleteSkill(target.id);
      load();
    } catch { /* ignore */ }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginBottom: 12 }}>
        <button className="btn btn-ghost btn-sm" onClick={load}><IconRefresh size={13} /> 刷新扫描</button>
        <button className="btn btn-ghost btn-sm" onClick={() => void handleImportLocal()} disabled={importing}>
          <IconFolder size={13} /> {importing ? "导入中…" : "导入本地技能"}
        </button>
        <button className="btn btn-primary btn-sm" onClick={() => setShowAddRepo(true)}><IconPlus size={13} /> 添加技能仓库</button>
      </div>

      <FormDialog
        open={showAddRepo}
        onClose={() => { setShowAddRepo(false); setRepoUrl(""); setRepoName(""); }}
        title="添加技能仓库"
        onSubmit={() => void handleAddRepo()}
        submitLabel="添加"
        submitDisabled={!repoUrl.trim()}
      >
        <input className="ui-input" placeholder="Git 仓库地址 (https://…/repo.git)" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
        <input className="ui-input" placeholder="仓库名称（可选）" value={repoName} onChange={(e) => setRepoName(e.target.value)} />
      </FormDialog>

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
                  <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDelete({ kind: "repo", id: r.id, name: r.name })}><IconX size={12} /></button>
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
          <div key={s.id} className="settings-resource-item skill-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name">
                {s.display_name || s.name}
                <button className="btn btn-ghost btn-xs skill-expand" onClick={() => setExpandedSkill(expandedSkill === s.id ? null : s.id)}>
                  {expandedSkill === s.id ? "收起" : "详情"}
                </button>
              </div>
              <div className="settings-resource-desc"><span>{s.description || "无描述"}</span><span className="settings-resource-tag" title={s.path || undefined}>{s.source}</span></div>
              {expandedSkill === s.id && (
                <div className="skill-detail">
                  <div className="skill-detail-field">
                    <label className="settings-field-label">触发条件（AI 何时该用此技能）</label>
                    <textarea
                      className="ui-textarea" rows={2} placeholder="如：当任务涉及浏览器自动化操作时"
                      value={triggerDrafts[s.id] ?? s.trigger ?? ""}
                      onChange={(e) => setTriggerDrafts((p) => ({ ...p, [s.id]: e.target.value }))}
                      onBlur={() => void saveTrigger(s)}
                    />
                  </div>
                  <div className="skill-detail-field">
                    <label className="settings-field-label">AI 可见性预览（实际注入系统提示词的内容）</label>
                    <pre className="skill-prompt-preview">{renderSkillPromptLine(s)}</pre>
                    <div className="skill-preview-hint">AI 通过 skill_view 工具按需加载正文（{s.content ? `正文 ${s.content.length} 字符` : "正文为空，AI 加载不到指令"}）</div>
                  </div>
                </div>
              )}
            </div>
            <div className="settings-resource-actions">
              <Sw checked={s.is_active} onChange={async (v) => { try { await api.updateSkill(s.id, { is_active: v }); load(); } catch {} }} />
              <button className="btn btn-ghost btn-xs" onClick={() => setConfirmDelete({ kind: "skill", id: s.id, name: s.display_name || s.name })}><IconX size={12} /></button>
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="navpage-empty">暂无技能</div>}
      </div>
      <ConfirmDialog
        open={confirmDelete !== null}
        title={confirmDelete?.kind === "repo" ? "删除技能仓库" : "删除技能"}
        message={confirmDelete ? `删除「${confirmDelete.name}」？` : ""}
        danger
        onCancel={() => setConfirmDelete(null)}
        onConfirm={doDelete}
      />
    </div>
  );
}
