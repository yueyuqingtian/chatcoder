/** 新建工作树（plan-20261017-goal12 项 6；plan-20261017-frontend-polish 重构）。
 *
 * 同一弹窗服务两个入口：
 *  - 侧栏项目行（项目级）：可选择在哪些仓库创建工作树（根目录 + 子仓库的多仓库布局），填写名称与分支；
 *  - 标题栏（会话级）：只填分支，本会话之后的改动只落在该工作树。
 * 仓库候选由调用方注入（loadRepos），弹窗自身不依赖项目上下文；
 * 创建动作也由调用方提供，弹窗只负责输入校验、进行中状态与错误展示。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Dialog, DialogSection, Field, InsetCard, InsetRow, Input } from "../ui";
import type { RepoCandidate } from "../../api/client";
import { IconGitBranch } from "../icons";

/** 分支名允许的字符（与 git 引用名的常见子集一致，避免提交到后端后才失败） */
const BRANCH_RE = /^[A-Za-z0-9._/-]+$/;

export interface WorktreeCreateOptions { name?: string; branch?: string; repos?: string[] }

export function WorktreeCreateDialog({ open, subtitle, withName = false, explain, loadRepos, onClose, onCreate }: {
  open: boolean;
  subtitle?: string;
  /** 项目级创建可以命名；会话级只填分支 */
  withName?: boolean;
  explain: string;
  /** 项目级：读取可用于建工作树的仓库（多仓库时让用户选择范围） */
  loadRepos?: () => Promise<RepoCandidate[]>;
  onClose: () => void;
  onCreate: (opts: WorktreeCreateOptions) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repoList, setRepoList] = useState<RepoCandidate[] | null>(null);
  const [reposLoading, setReposLoading] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());

  // loadRepos 通常是内联箭头函数：用 ref 固定最新值，避免每次父组件重渲染都重新拉取
  const loadRef = useRef(loadRepos);
  useEffect(() => { loadRef.current = loadRepos; });

  /** 每次打开都从空白开始，并重新读取仓库候选（项目可能刚新增了子仓库） */
  useEffect(() => {
    if (!open) return;
    setName("");
    setBranch("");
    setError(null);
    setBusy(false);
    setRepoList(null);
    setPicked(new Set<string>());
    const load = loadRef.current;
    if (!load) return;
    let cancelled = false;
    setReposLoading(true);
    load()
      .then((list) => {
        if (cancelled) return;
        setRepoList(list);
        // 空仓库不能作为起点：默认只选有提交的仓库
        setPicked(new Set(list.filter((r) => r.has_commits).map((r) => r.path)));
      })
      .catch(() => { if (!cancelled) setRepoList([]); })
      .finally(() => { if (!cancelled) setReposLoading(false); });
    return () => { cancelled = true; };
  }, [open]);

  const usable = useMemo(() => (repoList ?? []).filter((r) => r.has_commits), [repoList]);
  const multi = usable.length > 1;

  /** 起点说明：让用户在创建前就知道新分支从哪个仓库的哪个分支拉出 */
  const baseLabel = useMemo(() => {
    const chosen = usable.filter((r) => picked.has(r.path));
    if (chosen.length === 0) return "";
    if (chosen.length === 1) {
      const r = chosen[0];
      return `${r.name} 的 ${r.branch || "当前分支"}`;
    }
    return `所选 ${chosen.length} 个仓库的当前分支`;
  }, [usable, picked]);

  const branchInvalid = branch.trim() !== "" && !BRANCH_RE.test(branch.trim());
  const nothingPicked = multi && picked.size === 0;
  /** 单仓库时不再让用户勾选，只展示它的分支与改动状态 */
  const single = usable.length === 1 ? usable[0] : null;
  const noRepo = !reposLoading && repoList != null && usable.length === 0;
  const canSubmit = !busy && !branchInvalid && !nothingPicked && !reposLoading && !noRepo;

  const toggleRepo = (path: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  };

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      await onCreate({
        name: withName ? name.trim() || undefined : undefined,
        branch: branch.trim() || undefined,
        // 单仓库交给后端自动探测；多仓库时只创建勾选的部分
        repos: multi ? [...picked] : undefined,
      });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message.replace(/^Error:\s*/, "") : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="新建工作树"
      subtitle={subtitle}
      width={560}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>取消</Button>
          <Button variant="primary" loading={busy} disabled={!canSubmit} onClick={() => void submit()}>
            创建工作树
          </Button>
        </>
      }
    >
      <DialogSection>
        <InsetCard>
          <InsetRow icon={<IconGitBranch size={15} />} title="独立的工作目录与分支" desc={explain} />
        </InsetCard>
      </DialogSection>

      {withName && loadRepos && (multi || noRepo) && (
        <DialogSection title="仓库">
          <div className="wtc-repos">
            {reposLoading && <div className="wtc-hint">正在读取仓库…</div>}
            {!reposLoading && noRepo && (
              <div className="wtc-base is-warn">
                这个项目还没有任何提交，先在仓库中提交一次，再创建工作树。
              </div>
            )}
            {!reposLoading && multi && (
              <>
                {(repoList ?? []).map((r) => {
                  const disabled = !r.has_commits;
                  const on = picked.has(r.path);
                  return (
                    <label key={r.path}
                      className={`wtc-repo${on ? " is-picked" : ""}${disabled ? " is-disabled" : ""}`}>
                      <input
                        type="checkbox"
                        checked={on}
                        disabled={disabled}
                        onChange={() => toggleRepo(r.path)}
                      />
                      <span className="wtc-repo-main">
                        <span className="wtc-repo-name">
                          {r.name}
                          {r.is_root && <span className="wtc-repo-tag">项目根</span>}
                          {disabled && <span className="wtc-repo-tag is-warn">无提交</span>}
                        </span>
                        <span className="wtc-repo-path">{r.path}</span>
                      </span>
                      <span className="wtc-repo-side">
                        <span className="wtc-repo-branch">{r.branch || "—"}</span>
                        {r.dirty && <span className="wtc-repo-dirty">有未提交改动</span>}
                      </span>
                    </label>
                  );
                })}
                <div className="wtc-hint">
                  默认选中所有有提交的仓库；只勾选需要的仓库，可缩小这次创建的改动范围。
                </div>
              </>
            )}
          </div>
        </DialogSection>
      )}

      <DialogSection title={withName ? "名称与分支" : "分支"}>
        <div className="wtc-form">
          {withName && (
            <Field label="名称" hint="留空则自动生成，用于侧栏显示">
              <Input value={name} placeholder="如：登录页改版" onChange={(e) => setName(e.target.value)} aria-label="工作树名称" />
            </Field>
          )}
          <Field
            label="分支"
            hint={branchInvalid ? undefined : "留空则以名称自动生成新分支"}
            error={branchInvalid ? "只能包含字母、数字与 . _ - /" : undefined}
          >
            <Input value={branch} placeholder="如：feat/login-redesign" onChange={(e) => setBranch(e.target.value)} aria-label="分支名" />
          </Field>
          {(baseLabel || single) && (
            <div className="wtc-base">
              <IconGitBranch size={13} />
              <span>
                {single ? (
                  <>
                    仓库 <b>{single.name}</b> · 当前分支 <b>{single.branch || "—"}</b>
                    {single.dirty ? " · 有未提交改动" : ""}
                  </>
                ) : (
                  <>从 <b>{baseLabel}</b> 创建新分支</>
                )}
                ；工作树目录位于仓库的 <code>.chatcoder/worktrees</code> 下。
              </span>
            </div>
          )}
          {nothingPicked && (
            <div className="wtc-base is-warn">至少选择一个仓库才能创建工作树。</div>
          )}
        </div>
      </DialogSection>

      {error && <div className="wtc-error" role="alert">{error}</div>}
    </Dialog>
  );
}
