/** 拓展二级浮窗（plan-20261009-ui U2，参考图5）：搜索 + 条目列表 + 「管理 / 探索更多」。
 *  点击条目即插入对应引用 chip（与 / 菜单同一套引用语义），键盘上下选择、回车插入、Esc 返回上一级。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Popover } from "../ui";
import { IconArrowUpRight, IconPlug, IconPuzzle, IconSearch, IconSliders, IconSparkles } from "../icons";
import { openExtensionMarket, openExtensionsManager, type ExtKind } from "../../store/navIntent";

export interface ExtItem {
  key: string;
  name: string;
  desc: string;
  iconUrl?: string;
}

const KIND_TEXT: Record<ExtKind, { noun: string; empty: string }> = {
  plugin: { noun: "插件", empty: "还没有启用的插件" },
  skill: { noun: "技能", empty: "还没有启用的技能" },
  connector: { noun: "连接器", empty: "还没有启用的连接器" },
};

export function extKindIcon(kind: ExtKind, size = 16) {
  if (kind === "plugin") return <IconPuzzle size={size} />;
  if (kind === "skill") return <IconSparkles size={size} />;
  return <IconPlug size={size} />;
}

function ItemIcon({ kind, url }: { kind: ExtKind; url?: string }) {
  const [failed, setFailed] = useState(false);
  if (url && !failed) {
    return <img className="cc-ext-img" src={url} alt="" loading="lazy" onError={() => setFailed(true)} />;
  }
  return <span className="cc-ext-glyph">{extKindIcon(kind, 15)}</span>;
}

/** 轻量模糊匹配：名称 / 描述子串命中，或名称按字符顺序命中（少打几个字也能搜到） */
function hit(item: ExtItem, q: string): boolean {
  if (!q) return true;
  const hay = `${item.name} ${item.desc}`.toLowerCase();
  if (hay.includes(q)) return true;
  let i = 0;
  const name = item.name.toLowerCase();
  for (let j = 0; j < name.length && i < q.length; j++) if (name[j] === q[i]) i++;
  return i >= q.length;
}

export function ExtensionPicker({ open, kind, items, onPick, onClose, onLeaveMenu }: {
  open: boolean;
  kind: ExtKind;
  items: ExtItem[];
  onPick: (item: ExtItem) => void;
  onClose: () => void;
  /** 「管理 / 探索更多」跳转前关闭整个 + 浮窗 */
  onLeaveMenu: () => void;
}) {
  const [query, setQuery] = useState("");
  const [hl, setHl] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  /** plan-404-1892（图2）：只有键盘 ↑/↓ 引起的选中变化才把高亮项滚入可视区；
   *  鼠标悬停（onMouseEnter → setHl）不得触发滚动——此前悬停到部分可见项会把列表拽动。 */
  const kbNavRef = useRef(false);
  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => items.filter((it) => hit(it, q)), [items, q]);
  const noun = KIND_TEXT[kind].noun;

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setHl(0);
    kbNavRef.current = false;
    const t = window.setTimeout(() => inputRef.current?.focus({ preventScroll: true }), 30);
    return () => window.clearTimeout(t);
  }, [open, kind]);

  // 搜索词变化 → 列表重排，回到顶部（内容变化的自然行为，不算悬停自动滚动）
  useEffect(() => {
    setHl(0);
    kbNavRef.current = false;
    if (listRef.current) listRef.current.scrollTop = 0;
  }, [q]);

  // 键盘高亮项滚入可视区（只滚列表自身，不牵动外层消息流）。
  // plan-404-1892（图2）：仅键盘导航触发；用 rect 相对位移计算，不依赖 offsetParent。
  useEffect(() => {
    if (!kbNavRef.current) return;
    kbNavRef.current = false;
    const list = listRef.current;
    const el = list?.querySelector<HTMLElement>(".pop-item.is-hl");
    if (!list || !el) return;
    const lr = list.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    if (er.top < lr.top) list.scrollTop -= lr.top - er.top;
    else if (er.bottom > lr.bottom) list.scrollTop += er.bottom - lr.bottom;
  }, [hl]);

  return (
    <Popover open={open} onClose={onClose} placement="right-end" className="cc-ext-pop" role="dialog" aria-label={`选择${noun}`} closeOnOutside={false}>
      <div className="pop-search">
        <IconSearch size={14} />
        <input
          ref={inputRef}
          value={query}
          placeholder={`搜索${noun}`}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); kbNavRef.current = true; setHl((i) => Math.min(filtered.length - 1, i + 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); kbNavRef.current = true; setHl((i) => Math.max(0, i - 1)); }
            else if (e.key === "Enter") { e.preventDefault(); const it = filtered[hl]; if (it) onPick(it); }
            else if (e.key === "ArrowLeft" && !query) { e.preventDefault(); onClose(); }
          }}
        />
      </div>
      <div className="pop-scroll cc-ext-list" ref={listRef} role="listbox" aria-label={noun}>
        {filtered.map((it, i) => (
          <button
            key={it.key}
            type="button"
            role="option"
            aria-selected={i === hl}
            className={"pop-item cc-ext-item" + (i === hl ? " is-hl" : "")}
            onMouseEnter={() => { kbNavRef.current = false; setHl(i); }}
            onClick={() => onPick(it)}
          >
            <span className="pop-item-icon"><ItemIcon kind={kind} url={it.iconUrl} /></span>
            <span className="cc-ext-name">{it.name}</span>
            {it.desc && <span className="pop-item-inline-desc">{it.desc}</span>}
          </button>
        ))}
        {filtered.length === 0 && (
          <div className="pop-empty">{items.length === 0 ? KIND_TEXT[kind].empty : `没有匹配「${query}」的${noun}`}</div>
        )}
      </div>
      <div className="pop-sep" />
      <button type="button" className="pop-item" onClick={() => { onLeaveMenu(); openExtensionsManager(kind); }}>
        <span className="pop-item-icon"><IconSliders size={15} /></span>
        <span className="pop-item-title">管理{noun}</span>
      </button>
      <button type="button" className="pop-item" onClick={() => { onLeaveMenu(); openExtensionMarket(kind); }}>
        <span className="pop-item-icon"><IconArrowUpRight size={15} /></span>
        <span className="pop-item-title">探索更多{noun}</span>
      </button>
    </Popover>
  );
}
