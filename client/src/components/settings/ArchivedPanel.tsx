/** 设置中心：归档恢复（plan-88 任务 C；plan-282-1441 #4 增补搜索与批量操作）。
 *
 * 列出已归档的项目与会话，支持：
 *  - 按名称搜索（项目名/路径、会话标题）；
 *  - 多选（含全选/反选）与批量恢复；
 *  - 批量**永久删除**（物理删除，不可恢复，二次确认）。
 *
 * 数据源：GET /projects?include_archived=true + GET /sessions?include_archived=true。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ProjectOut, type SessionOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { formatRelativeTime } from "../../utils/time";
import { ConfirmDialog } from "../ConfirmDialog";
import { Checkbox, Empty, Tooltip } from "../ui";
import { IconFolder, IconMessageSquare, IconRotateCcw, IconSearch, IconTrash } from "../icons";

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
  const [query, setQuery] = useState("");
  /** 选中集合：会话与项目分开，避免 id 空间冲突 */
  const [pickedSessions, setPickedSessions] = useState<Set<number>>(new Set());
  const [pickedProjects, setPickedProjects] = useState<Set<number>>(new Set());
  /** 永久删除确认（列出将删除的对象，红色不可恢复提示） */
  const [confirmDelete, setConfirmDelete] = useState(false);

  const load = useCallback(async () => {
    try {
      const [ps, ss] = await Promise.all([
        api.listProjects({ include_archived: true }),
        api.listSessions(undefined, true),
      ]);
      const archivedProjects = ps.filter((p) => p.archived);
      const archivedSessions = ss.filter((s) => s.status === "archived");
      setProjects(archivedProjects);
      setSessions(archivedSessions);
      // 数据刷新后剔除已不存在的选中项
      const pids = new Set(archivedProjects.map((p) => p.id));
      const sids = new Set(archivedSessions.map((s) => s.id));
      setPickedProjects((prev) => new Set([...prev].filter((id) => pids.has(id))));
      setPickedSessions((prev) => new Set([...prev].filter((id) => sids.has(id))));
    } catch { /* 非阻塞 */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  /** 搜索过滤：项目按 名称/路径，会话按 标题/id */
  const filteredProjects = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return projects;
    return projects.filter((p) =>
      `${p.name || ""} ${shortPath(p.path)} ${p.path}`.toLowerCase().includes(q),
    );
  }, [projects, query]);

  const filteredSessions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) =>
      `${s.title || ""} ${s.id}`.toLowerCase().includes(q),
    );
  }, [sessions, query]);

  const projectById = useMemo(() => new Map(projects.map((p) => [p.id, p])), [projects]);

  const toggleSession = (id: number) =>
    setPickedSessions((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  const toggleProject = (id: number) =>
    setPickedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });

  const allShownSessions = filteredSessions.length > 0 && filteredSessions.every((s) => pickedSessions.has(s.id));
  const allShownProjects = filteredProjects.length > 0 && filteredProjects.every((p) => pickedProjects.has(p.id));
  const pickedCount = pickedSessions.size + pickedProjects.size;

  const toggleAllShown = () => {
    if (allShownSessions && allShownProjects) {
      setPickedSessions(new Set());
      setPickedProjects(new Set());
      return;
    }
    setPickedSessions(new Set(filteredSessions.map((s) => s.id)));
    setPickedProjects(new Set(filteredProjects.map((p) => p.id)));
  };

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
      if (open) {
        await switchSession(s.id);
        // S14（plan-41-197）：此前只切换了会话但停留在设置页（且离开设置页会按入场快照
        // 重置会话），用户感知为“点击后没有进入对应恢复的会话”。现显式请求离开设置页。
        window.dispatchEvent(new CustomEvent("chatcoder:leave-settings"));
      } else {
        await load();
      }
    } catch { /* 非阻塞 */ }
    finally { setBusy(false); }
  };

  /** 批量恢复（项目恢复其下会话由后端语义保证） */
  const restorePicked = async () => {
    if (pickedCount === 0) return;
    setBusy(true);
    try {
      await Promise.all([
        ...[...pickedProjects].map((id) => api.updateProject(id, { archived: false })),
        ...[...pickedSessions].map((id) => api.updateSession(id, { status: "active" })),
      ]);
      setPickedProjects(new Set());
      setPickedSessions(new Set());
      await loadBootstrap();
      await load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setBusy(false); }
  };

  /** 批量永久删除 */
  const deletePicked = async () => {
    setBusy(true);
    try {
      await Promise.all([
        ...[...pickedProjects].map((id) => api.deleteProject(id)),
        ...[...pickedSessions].map((id) => api.deleteSession(id, true)),
      ]);
      setPickedProjects(new Set());
      setPickedSessions(new Set());
      setConfirmDelete(false);
      await loadBootstrap();
      await load();
    } catch (e) { useChatStore.setState({ error: String(e) }); }
    finally { setBusy(false); }
  };

  const empty = projects.length === 0 && sessions.length === 0;
  const noMatch = !empty && filteredProjects.length === 0 && filteredSessions.length === 0;

  return (
    <div className="archive-panel">
      {/* 工具条：搜索 + 全选 + 批量操作 */}
      <div className="archive-toolbar sc-toolbar">
        <div className="archive-search">
          <IconSearch size={13} />
          <input
            placeholder="按名称搜索归档…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="搜索归档"
          />
        </div>
        <span className="archive-picked">已选 {pickedCount} 项</span>
        <button className="btn btn-ghost btn-sm" onClick={toggleAllShown} disabled={busy || (filteredProjects.length === 0 && filteredSessions.length === 0)}>
          {allShownSessions && allShownProjects ? "取消全选" : "全选当前"}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={() => void restorePicked()} disabled={busy || pickedCount === 0}>
          <IconRotateCcw size={12} /> 批量恢复
        </button>
        <button className="btn btn-danger btn-sm" onClick={() => setConfirmDelete(true)} disabled={busy || pickedCount === 0}>
          <IconTrash size={12} /> 批量删除
        </button>
      </div>

      {/* plan-91-393：行 = 勾选 + 图标块 + 标题/路径 + 右侧操作；分组标题与空态统一 */}
      {filteredProjects.length > 0 && <div className="sc-group-label">已归档项目</div>}
      {filteredProjects.length > 0 && (
        <div className="sc-list">
          {filteredProjects.map((p) => (
            <div key={p.id} className="sc-row">
              <Checkbox
                checked={pickedProjects.has(p.id)}
                onChange={() => toggleProject(p.id)}
                aria-label={`选择项目 ${p.name || shortPath(p.path)}`}
              />
              <span className="sc-tile"><IconFolder size={15} /></span>
              <div className="sc-row-main">
                <div className="sc-row-title">{p.name || shortPath(p.path)}</div>
                <div className="sc-row-desc sc-mono">{p.path}</div>
              </div>
              <div className="sc-row-actions">
                <Tooltip content="恢复项目及其下的会话">
                  <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void restoreProject(p.id)}>
                    <IconRotateCcw size={12} /> 恢复
                  </button>
                </Tooltip>
              </div>
            </div>
          ))}
        </div>
      )}

      {filteredSessions.length > 0 && <div className="sc-group-label">已归档会话</div>}
      {filteredSessions.length > 0 && (
        <div className="sc-list">
          {filteredSessions.map((s) => {
            const proj = s.project_id != null ? projectById.get(s.project_id) : undefined;
            return (
              <div key={s.id} className="sc-row">
                <Checkbox
                  checked={pickedSessions.has(s.id)}
                  onChange={() => toggleSession(s.id)}
                  aria-label={`选择会话 ${s.title || s.id}`}
                />
                <span className="sc-tile"><IconMessageSquare size={15} /></span>
                <div className="sc-row-main">
                  <div className="sc-row-title">{s.title || `会话 ${s.id}`}</div>
                  <div className="sc-row-desc">{proj ? `${proj.name} · ` : ""}归档于 {formatRelativeTime(s.last_activity_at)}</div>
                </div>
                <div className="sc-row-actions">
                  <Tooltip content="仅恢复到侧栏">
                    <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void restoreSession(s)}>
                      <IconRotateCcw size={12} /> 恢复
                    </button>
                  </Tooltip>
                  <Tooltip content="恢复并打开该会话">
                    <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void restoreSession(s, true)}>
                      恢复并打开
                    </button>
                  </Tooltip>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {empty && (
        <Empty title="暂无归档的项目或会话" desc="归档后的内容会收在这里，随时可以恢复" />
      )}
      {noMatch && (
        <Empty mood="think" title={`没有匹配「${query}」的归档项`}
          action={<button className="btn btn-ghost btn-sm" onClick={() => setQuery("")}>清除搜索</button>} />
      )}

      <ConfirmDialog
        open={confirmDelete}
        title="永久删除归档项"
        message={
          `将永久删除 ${pickedProjects.size} 个项目与 ${pickedSessions.size} 个会话，` +
          `及其全部消息、轮次、任务与审核记录。\n\n此操作不可恢复，无法通过归档页找回。`
        }
        confirmLabel="永久删除"
        danger
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() => void deletePicked()}
      />
    </div>
  );
}
