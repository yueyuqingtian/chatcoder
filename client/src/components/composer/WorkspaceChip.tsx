/** 输入框左下工作区（plan-20261009-ui U2，参考图3/图7）。
 *
 * 首页：可点击 → 浮窗（搜索名称或路径 / 项目列表带图标与路径 / 新建项目）；
 * 会话中：只读展示当前会话所在项目（锁形图标 + 提示「会话开始后不可更换」），工作树会话额外显示分支。 */
import { useMemo, useRef, useState, useEffect, useLayoutEffect } from "react";
import type { ProjectOut } from "../../api/client";
import { Empty, Popover, Tooltip, type PopoverPlacement } from "../ui";
import { IconCheck, IconChevronDown, IconGitBranch, IconLock, IconPlus, IconSearch } from "../icons";
import { ProjectIcon } from "../project/ProjectIcon";
import { baseName } from "../iconkit/FileIcon";
import { openProjectDialog } from "../../store/projectDialog";

function projectName(p: ProjectOut): string {
  return p.name || baseName(p.path);
}

/** 列表高度上下限与浮层非列表部分（搜索框 + 分隔线 + 新建行 + 内边距）的近似高度 */
const LIST_MAX_PX = 320;
const LIST_MIN_PX = 120;
const CHROME_PX = 120;

export function WorkspaceChip({ projects, currentId, locked, branch, onSelect, variant = "chip" }: {
  projects: ProjectOut[];
  currentId: number | null;
  /** 会话已开始：只读 */
  locked: boolean;
  /** 工作树会话的分支名 */
  branch?: string | null;
  onSelect: (id: number) => void;
  /** plan-114-449：inline = 首页标语里的下划线文字触发（浮窗向下弹出），与输入框下的 chip 共用同一列表 */
  variant?: "chip" | "inline";
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hl, setHl] = useState(0);
  const btnRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** 弹出方向与列表可用高度：按触发器上下两侧的剩余空间决定，保证浮层完整可见、列表内部滚动 */
  const [place, setPlace] = useState<PopoverPlacement>(variant === "inline" ? "bottom-start" : "top-start");
  const [listMax, setListMax] = useState(LIST_MAX_PX);
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

  useLayoutEffect(() => {
    if (!open) return;
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    // 首选方向：inline 向下（标语下方），chip 向上（输入框上方）；首选侧空间不足且另一侧更宽时翻转。
    // 扣掉搜索框、分隔线与「新建项目」行的占位后，剩余空间即列表的可用高度。
    const below = window.innerHeight - r.bottom;
    const above = r.top;
    const preferDown = variant === "inline";
    const down = preferDown ? below >= LIST_MIN_PX + CHROME_PX || below >= above : below > above && below >= LIST_MIN_PX + CHROME_PX;
    const room = down ? below : above;
    setPlace(down ? "bottom-start" : "top-start");
    setListMax(Math.max(LIST_MIN_PX, Math.min(LIST_MAX_PX, Math.floor(room - CHROME_PX))));
  }, [open, variant]);

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
    <div className={"cc-anchor" + (variant === "inline" ? " es-ws-anchor" : "")}>
      <button
        ref={btnRef}
        type="button"
        className={variant === "inline"
          ? "es-ws-trigger" + (open ? " is-open" : "")
          : "cc-ws" + (open ? " is-open" : "") + (current ? "" : " is-empty")}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {variant === "chip" && (current ? <ProjectIcon icon={current.icon} color={current.color} size={14} /> : <IconPlus size={13} />)}
        <span className={variant === "inline" ? "es-ws-name" : "cc-ws-name"}>{current ? projectName(current) : "选择项目"}</span>
        <IconChevronDown size={variant === "inline" ? 14 : 12} className="cc-chip-caret" />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} placement={place} className="cc-ws-pop" role="dialog" ignoreRefs={[btnRef]} aria-label="选择项目">
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
        <div className="pop-scroll" role="listbox" aria-label="项目" style={{ maxHeight: listMax }}>
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
          {list.length === 0 && <Empty size="sm" mood={projects.length === 0 ? "calm" : "think"} title={projects.length === 0 ? "还没有项目，先新建一个吧" : `没有匹配「${query}」的项目`} />}
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
