/** 输入框左下工作区（plan-20261009-ui U2，参考图3/图7）。
 *
 * 首页：可点击 → 浮窗（搜索名称或路径 / 项目列表带图标与路径 / 新建项目）；
 * 会话中：只读展示当前会话所在项目（锁形图标 + 提示「会话开始后不可更换」），工作树会话额外显示分支。 */
import { useMemo, useRef, useState, useEffect } from "react";
import type { ProjectOut } from "../../api/client";
import { Popover, Tooltip } from "../ui";
import { IconCheck, IconChevronDown, IconGitBranch, IconLock, IconPlus, IconSearch } from "../icons";
import { ProjectIcon } from "../project/ProjectIcon";
import { baseName } from "../iconkit/FileIcon";
import { openProjectDialog } from "../../store/projectDialog";

function projectName(p: ProjectOut): string {
  return p.name || baseName(p.path);
}

export function WorkspaceChip({ projects, currentId, locked, branch, onSelect }: {
  projects: ProjectOut[];
  currentId: number | null;
  /** 会话已开始：只读 */
  locked: boolean;
  /** 工作树会话的分支名 */
  branch?: string | null;
  onSelect: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hl, setHl] = useState(0);
  const btnRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const current = projects.find((p) => p.id === currentId) ?? null;
  const q = query.trim().toLowerCase();
  const list = useMemo(
    () => projects.filter((p) => !q || projectName(p).toLowerCase().includes(q) || p.path.toLowerCase().includes(q)),
    [projects, q],
  );

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setHl(Math.max(0, projects.findIndex((p) => p.id === currentId)));
    const t = window.setTimeout(() => inputRef.current?.focus({ preventScroll: true }), 30);
    return () => window.clearTimeout(t);
  }, [open, projects, currentId]);
  useEffect(() => { setHl(0); }, [q]);

  if (locked) {
    return (
      <Tooltip content={current ? `${current.path}\n会话已开始，工作区不可更换` : "会话已开始，工作区不可更换"} side="top">
        <span className="cc-ws is-locked" tabIndex={0}>
          {current ? <ProjectIcon icon={current.icon} color={current.color} size={14} /> : null}
          <span className="cc-ws-name">{current ? projectName(current) : "未知项目"}</span>
          {branch && (
            <span className="cc-ws-branch"><IconGitBranch size={12} />{branch}</span>
          )}
          <IconLock size={12} className="cc-ws-lock" />
        </span>
      </Tooltip>
    );
  }

  const pick = (p: ProjectOut) => { onSelect(p.id); setOpen(false); };

  return (
    <div className="cc-anchor">
      <button
        ref={btnRef}
        type="button"
        className={"cc-ws" + (open ? " is-open" : "") + (current ? "" : " is-empty")}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {current ? <ProjectIcon icon={current.icon} color={current.color} size={14} /> : <IconPlus size={13} />}
        <span className="cc-ws-name">{current ? projectName(current) : "选择项目"}</span>
        <IconChevronDown size={12} className="cc-chip-caret" />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} placement="top-start" className="cc-ws-pop" role="dialog" ignoreRefs={[btnRef]} aria-label="选择项目">
        <div className="pop-search">
          <IconSearch size={14} />
          <input
            ref={inputRef}
            value={query}
            placeholder="搜索名称或路径"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setHl((i) => Math.min(list.length - 1, i + 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setHl((i) => Math.max(0, i - 1)); }
              else if (e.key === "Enter") { e.preventDefault(); const p = list[hl]; if (p) pick(p); }
            }}
          />
        </div>
        <div className="pop-scroll" role="listbox" aria-label="项目">
          {list.map((p, i) => (
            <button
              key={p.id}
              type="button"
              role="option"
              aria-selected={p.id === currentId}
              className={"pop-item" + (i === hl ? " is-hl" : "") + (p.id === currentId ? " is-selected" : "")}
              onMouseEnter={() => setHl(i)}
              onClick={() => pick(p)}
            >
              <span className="pop-item-icon"><ProjectIcon icon={p.icon} color={p.color} size={16} /></span>
              <span className="pop-item-main">
                <span className="pop-item-title">{projectName(p)}</span>
                <span className="pop-item-desc">{p.path}</span>
              </span>
              <span className="pop-item-end">{p.id === currentId && <span className="pop-item-check"><IconCheck size={14} /></span>}</span>
            </button>
          ))}
          {list.length === 0 && <div className="pop-empty">{projects.length === 0 ? "还没有项目，先新建一个吧" : `没有匹配「${query}」的项目`}</div>}
        </div>
        <div className="pop-sep" />
        <button type="button" className="pop-item" onClick={() => { setOpen(false); openProjectDialog({ mode: "create" }); }}>
          <span className="pop-item-icon"><IconPlus size={15} /></span>
          <span className="pop-item-title">新建项目</span>
        </button>
      </Popover>
    </div>
  );
}
