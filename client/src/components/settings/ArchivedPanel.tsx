/** 设置中心：归档恢复（plan-88 任务 C）。
 *
 * 列出已归档的项目与会话，支持一键恢复（解归档）；归档会话可「恢复并打开」。
 * 数据源：GET /projects?include_archived=true + GET /sessions?include_archived=true。
 */
import { useCallback, useEffect, useState } from "react";
import { api, type ProjectOut, type SessionOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { formatRelativeTime } from "../../utils/time";
import { IconFolder, IconHash, IconRotateCcw } from "../icons";

function shortPath(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/\/$/, "");
  const parts = clean.split("/");
  return parts[parts.length - 1] || clean;
}

export function ArchivedPanel() {
  const loadBootstrap = useChatStore((s) => s.loadBootstrap);
  const switchSession = useChatStore((s) => s.switchSession);
  const [projects, setProjects] = useState<ProjectOut[]>([]);
  const [sessions, setSessions] = useState<SessionOut[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [ps, ss] = await Promise.all([
        api.listProjects({ include_archived: true }),
        api.listSessions(undefined, true),
      ]);
      setProjects(ps.filter((p) => p.archived));
      setSessions(ss.filter((s) => s.status === "archived"));
    } catch { /* 非阻塞 */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const restoreProject = async (id: number) => {
    setBusy(true);
    try {
      await api.updateProject(id, { archived: false });
      await loadBootstrap();
      await load();
    } catch { /* 非阻塞 */ }
    finally { setBusy(false); }
  };

  const restoreSession = async (s: SessionOut, open = false) => {
    setBusy(true);
    try {
      await api.updateSession(s.id, { status: "active" });
      await loadBootstrap();
      if (open) await switchSession(s.id);
      else await load();
    } catch { /* 非阻塞 */ }
    finally { setBusy(false); }
  };

  const projectById = new Map(projects.map((p) => [p.id, p]));
  const empty = projects.length === 0 && sessions.length === 0;

  return (
    <div className="settings-resource-list">
      {projects.length > 0 && (
        <div className="sb-section-label" style={{ margin: "14px 0 6px" }}>已归档项目</div>
      )}
      {projects.map((p) => (
        <div key={p.id} className="settings-resource-item">
          <div className="settings-resource-info">
            <div className="settings-resource-name"><IconFolder size={13} /> {p.name || shortPath(p.path)}</div>
            <div className="settings-resource-desc">{p.path}</div>
          </div>
          <div className="settings-resource-actions">
            <button className="btn btn-ghost btn-xs" disabled={busy}
              onClick={() => void restoreProject(p.id)} title="恢复项目及其下的会话">
              <IconRotateCcw size={12} /> 恢复
            </button>
          </div>
        </div>
      ))}

      {sessions.length > 0 && (
        <div className="sb-section-label" style={{ margin: "14px 0 6px" }}>已归档会话</div>
      )}
      {sessions.map((s) => {
        const proj = s.project_id != null ? projectById.get(s.project_id) : undefined;
        return (
          <div key={s.id} className="settings-resource-item">
            <div className="settings-resource-info">
              <div className="settings-resource-name"><IconHash size={13} /> {s.title || `会话 ${s.id}`}</div>
              <div className="settings-resource-desc">
                {proj ? `${proj.name} · ` : ""}归档于 {formatRelativeTime(s.last_activity_at)}
              </div>
            </div>
            <div className="settings-resource-actions">
              <button className="btn btn-ghost btn-xs" disabled={busy}
                onClick={() => void restoreSession(s, true)} title="恢复并打开该会话">
                <IconRotateCcw size={12} /> 恢复并打开
              </button>
              <button className="btn btn-ghost btn-xs" disabled={busy}
                onClick={() => void restoreSession(s)} title="仅恢复到侧栏">
                <IconRotateCcw size={12} /> 恢复
              </button>
            </div>
          </div>
        );
      })}

      {empty && <div className="navpage-empty">暂无归档的项目或会话</div>}
    </div>
  );
}
