/** 设置中心：工作树（plan-282-1441 #5）。
 *
 * 工作树 = git worktree，用来为同一项目开多个隔离工作区并行开发。
 * 这里按**项目**分组管理：创建 / 查看 git 状态（分支、领先落后、未提交）/
 * 合并到主工作区（三栏冲突解决）/ 删除。
 *
 * 工作树本身在数据库里是一条 Project 行（is_worktree=true），因此左侧面板
 * 会把它当作独立工作区展示，可在其中直接新建会话。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ProjectOut, type RepoCandidate, type WorktreeOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { ConfirmDialog } from "../ConfirmDialog";
import { FormDialog } from "../ui/FormDialog";
import { Button, Checkbox, Empty, IconButton, Input, List, ListRow, PageActions, Section, Tag, Tooltip } from "../ui";
import { MergeDialog } from "../worktree/MergeDialog";
import { IconDownload, IconGitBranch, IconPlus, IconRefresh, IconTrash, IconFolderOpen } from "../icons";

/** 项目名：优先 name，否则取路径末段 */
function projectLabel(p: ProjectOut): string {
  if (p.name) return p.name;
  const parts = p.path.replace(/\\/g, "/").replace(/\/$/, "").split("/");
  return parts[parts.length - 1] || p.path;
}

export function WorktreesPanel() {
  const [projects, setProjects] = useState<ProjectOut[]>([]);
  const [worktrees, setWorktrees] = useState<WorktreeOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  /** plan-308-1542 需求3-C：删除/内联操作的面板内提示（不再弹全局 Toast）。 */
  const [notice, setNotice] = useState<string | null>(null);
  /** 新建工作树：目标项目 + 表单 */
  const [createFor, setCreateFor] = useState<ProjectOut | null>(null);
  const [name, setName] = useState("");
  const [branch, setBranch] = useState("");
  /** plan-282-1441：仓库候选与勾选（支持"根是空仓库、代码在子仓库"的布局） */
  const [repoCands, setRepoCands] = useState<RepoCandidate[]>([]);
  const [pickedRepos, setPickedRepos] = useState<Set<string>>(new Set());
  const [repoLoading, setRepoLoading] = useState(false);
  /** 删除确认 / 合并弹窗 */
  const [dropTarget, setDropTarget] = useState<WorktreeOut | null>(null);
  const [mergeTarget, setMergeTarget] = useState<WorktreeOut | null>(null);
  /** 合并方向：to_main=工作树→主工作区；from_main=主工作区→工作树 */
  const [mergeDirection, setMergeDirection] = useState<"to_main" | "from_main">("to_main");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const ps = await api.listProjects();
      // 主项目 = 非工作树且未归档
      const mains = ps.filter((p) => !p.is_worktree && !p.archived);
      setProjects(mains);
      const nested = await Promise.all(
        mains.map((p) => api.listWorktrees(p.id).catch(() => [] as WorktreeOut[])),
      );
      setWorktrees(nested.flat());
    } catch { /* 非阻塞 */ }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const byParent = useMemo(() => {
    const m = new Map<number, WorktreeOut[]>();
    for (const wt of worktrees) {
      const key = wt.parent_project_id ?? 0;
      const arr = m.get(key) ?? [];
      arr.push(wt);
      m.set(key, arr);
    }
    return m;
  }, [worktrees]);

  /** plan-282-1441：打开新建对话框时加载该项目的仓库候选，并预选可用项。
   *  支持"根目录是空仓库、真实代码在子仓库"的布局——预选有提交的子仓库，
   *  把无提交的（如空仓库根）禁用并说明原因，避免用户提交后才看到报错。 */
  const openCreate = useCallback(async (project: ProjectOut) => {
    setCreateFor(project);
    setName("");
    setBranch("");
    setRepoLoading(true);
    try {
      const cands = await api.listRepoCandidates(project.id);
      setRepoCands(cands);
      const usable = cands.filter((c) => c.has_commits);
      // 有子仓库时默认只选子仓库（根为空仓库的常见布局）
      const subRepos = usable.filter((c) => !c.is_root);
      setPickedRepos(new Set((subRepos.length > 0 ? subRepos : usable).map((c) => c.path)));
    } catch (e) {
      setRepoCands([]);
      setPickedRepos(new Set());
      useChatStore.setState({ error: `读取仓库信息失败：${String(e)}` });
    } finally { setRepoLoading(false); }
  }, []);

  const toggleRepo = (path: string) =>
    setPickedRepos((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });

  const handleCreate = async () => {
    if (!createFor) return;
    if (pickedRepos.size === 0) {
      useChatStore.setState({ error: "请至少选择一个仓库" });
      return;
    }
    setBusy(true);
    try {
      const res = await api.createWorktreeForProject(createFor.id, {
        name: name.trim() || undefined,
        branch: branch.trim() || undefined,
        repos: [...pickedRepos],
      });
      setCreateFor(null);
      setName("");
      setBranch("");
      await load();
      // 刷新侧栏，让新工作树立即可见
      await useChatStore.getState().loadBootstrap();
      if (res?.count > 1) {
        useChatStore.setState({ error: `已在 ${res.count} 个仓库创建工作树` });
      }
    } catch (e) {
      useChatStore.setState({ error: `创建工作树失败：${String(e)}` });
    } finally { setBusy(false); }
  };

  const handleDelete = async (force: boolean) => {
    if (!dropTarget) return;
    setBusy(true);
    setNotice(null);
    try {
      const res = await api.deleteWorktreeProject(dropTarget.id, force);
      const n = res.deleted_sessions ?? 0;
      setDropTarget(null);
      setNotice(`已删除工作树${n > 0 ? `，并级联删除 ${n} 个会话及其消息` : ""}`);
      await load();
      // plan-308-1542 需求3-C：被删工作树可能正是当前选中项目/会话所在处——
      // detached 时清理选中态，避免左侧面板指向已不存在的项目。
      if (res.detached) {
        const store = useChatStore.getState();
        if (store.currentProjectId === dropTarget.id) {
          useChatStore.setState({ currentProjectId: null, currentSessionId: null });
        }
      }
      await useChatStore.getState().loadBootstrap();
    } catch (e) {
      // 未提交变更时后端会拒绝，提示用户可强制删除。
      // 走面板内联提示（不弹全局 Toast）——删除属面板内操作，就地反馈更清晰。
      setNotice(`删除失败：${String(e)}`);
    } finally { setBusy(false); }
  };

  const cleanupStale = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const res = await api.cleanupStaleWorktrees();
      setNotice(res.cleaned > 0
        ? `已清理 ${res.cleaned} 个失效工作树：${res.stale.map((s) => s.name).join("、")}`
        : "没有发现失效的工作树");
      await load();
      // 左面板同步刷新（否则被清理的项仍显示在项目列表里）
      await useChatStore.getState().loadBootstrap();
    } catch (e) {
      setNotice(`清理失败：${String(e)}`);
    } finally { setBusy(false); }
  };

  return (
    <>
      {/* plan-114-450：页级操作收进页头右上；说明文字移到页面副标题（i18n settings.ps.worktrees） */}
      <PageActions>
        {/* plan-308-1542：一键清理失效工作树（目录/分支已被外部删除，但左面板仍残留的僵尸项） */}
        <Tooltip content="清理目录已不存在、但仍登记在库里的工作树（左面板删不掉的残留项）">
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void cleanupStale()}>清理失效项</Button>
        </Tooltip>
        <Button variant="secondary" size="sm" icon={<IconRefresh size={13} />} loading={loading} onClick={() => void load()}>刷新</Button>
      </PageActions>

      {notice && <div className="worktrees-notice" role="status">{notice}</div>}

      {projects.length === 0 && !loading && (
        <div className="ui-card"><Empty title="还没有项目" desc="先在左侧添加一个 git 项目，就能在这里给它开并行工作树" /></div>
      )}

      {projects.map((p) => {
        const list = byParent.get(p.id) ?? [];
        return (
          <Section
            key={p.id}
            bare
            title={`${projectLabel(p)} · ${list.length} 个工作树`}
            actions={<Button variant="ghost" size="sm" icon={<IconPlus size={12} />} onClick={() => void openCreate(p)}>新建工作树</Button>}
          >
            {list.length === 0 ? (
              <div className="ui-card">
                <Empty size="sm" title="该项目还没有工作树" desc="新建后会在左侧面板作为独立工作区出现" />
              </div>
            ) : (
              <List>
                {list.map((wt) => (
                  <ListRow
                    key={wt.id}
                    icon={<IconGitBranch size={15} />}
                    name={<span className="sc-ellipsis">{wt.name}</span>}
                    desc={<>分支 {wt.branch || "—"} · <span className="sc-mono">{wt.path}</span></>}
                    meta={(wt.dirty || wt.ahead > 0 || wt.behind > 0) ? (
                      <>
                        {wt.dirty && <Tag>有未提交变更</Tag>}
                        {wt.ahead > 0 && <Tag>领先 {wt.ahead}</Tag>}
                        {wt.behind > 0 && <Tag>落后 {wt.behind}</Tag>}
                      </>
                    ) : undefined}
                    actions={
                      <>
                        {/* plan-114-450：次要操作悬停显示，常驻只留「合并」主操作，避免每行四个按钮挤在一起 */}
                        <span className="ui-row-hover">
                          <IconButton size="xs" title="把主工作区的改动更新到该工作树" icon={<IconDownload size={13} />} disabled={busy}
                            onClick={() => { setMergeDirection("from_main"); setMergeTarget(wt); }} />
                          <IconButton size="xs" title="打开目录" icon={<IconFolderOpen size={13} />} disabled={busy}
                            onClick={() => { usePanelStore.getState().setPreviewPath(wt.path); }} />
                          <IconButton size="xs" tone="danger" title="删除工作树" icon={<IconTrash size={13} />} disabled={busy}
                            onClick={() => setDropTarget(wt)} />
                        </span>
                        <Tooltip content="合并该工作树的改动到主工作区">
                          <Button variant="secondary" size="sm" disabled={busy}
                            onClick={() => { setMergeDirection("to_main"); setMergeTarget(wt); }}>合并到主工作区</Button>
                        </Tooltip>
                      </>
                    }
                  />
                ))}
              </List>
            )}
          </Section>
        );
      })}

      {/* 新建：**勾选仓库** + 名称 + 起始分支（plan-282-1441：支持子仓库布局） */}
      <FormDialog
        open={createFor != null}
        onClose={() => setCreateFor(null)}
        title={`在「${createFor ? projectLabel(createFor) : ""}」下新建工作树`}
        onSubmit={() => void handleCreate()}
        submitDisabled={busy || repoLoading || pickedRepos.size === 0}
      >
        <div className="wt-repo-label">选择仓库（可多选，将各自建立一个工作树）</div>
        {repoLoading && <div className="navpage-empty">正在读取仓库信息…</div>}
        {!repoLoading && repoCands.length === 0 && (
          <div className="navpage-empty">该项目目录不是 git 仓库，无法创建工作树。</div>
        )}
        {!repoLoading && repoCands.map((c) => (
          <div className={`wt-repo-row${c.has_commits ? "" : " disabled"}`} key={c.path}>
            <Checkbox
              checked={pickedRepos.has(c.path)}
              disabled={!c.has_commits}
              onChange={() => toggleRepo(c.path)}
              aria-label={`选择仓库 ${c.name}`}
            />
            <div className="wt-repo-info">
              <div className="wt-repo-name">
                {c.name}
                {c.is_root && <span className="wt-repo-tag">项目根</span>}
                {c.has_commits
                  ? <span className="wt-repo-branch">分支 {c.branch || "—"}</span>
                  : <span className="wt-repo-nocommit">尚无提交，不可用</span>}
              </div>
              <div className="wt-repo-path" title={c.path}>{c.path}</div>
            </div>
          </div>
        ))}
        <Input
          placeholder="工作树名称（留空自动生成，如 dev-copy）"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="工作树名称"
        />
        <Input
          placeholder="分支名（留空自动生成，如 chatcoder/dev-copy）"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          aria-label="分支名"
        />
      </FormDialog>

      <ConfirmDialog
        open={dropTarget != null}
        title="删除工作树"
        message={
          `将删除工作树「${dropTarget?.name ?? ""}」的目录与登记，并连带删除本地分支 ` +
          `${dropTarget?.branch || "（工作树分支）"}。` +
          `\n\n若该工作树存在未提交变更，删除会被拒绝；此时可选择"强制删除"放弃这些改动。`
        }
        confirmLabel="删除"
        danger
        onCancel={() => setDropTarget(null)}
        onConfirm={() => void handleDelete(false)}
      />
      {/* 强制删除的二次入口：未提交变更时的兜底 */}
      {dropTarget && (
        <div className="worktree-force-row">
          <button className="btn btn-ghost btn-xs" disabled={busy}
            onClick={() => void handleDelete(true)}>
            强制删除（丢弃未提交变更）
          </button>
        </div>
      )}

      <MergeDialog
        open={mergeTarget != null}
        direction={mergeDirection}
        worktree={mergeTarget}
        onClose={() => setMergeTarget(null)}
        onMerged={() => { setMergeTarget(null); void load(); }}
      />
    </>
  );
}
