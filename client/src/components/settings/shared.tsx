/** 设置中心共享组件（v2.2 对齐 zcode 3.18）：开关 / 行 / 通用资源列表。 */
import { useCallback, useEffect, useState } from "react";
import { IconX } from "../icons";

export function Sw({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return <label className="ui-switch"><input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} /><span className="ui-switch-track" /></label>;
}

export function Row({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return <div className="settings-row"><div className="settings-row-info"><div className="settings-row-title">{title}</div><div className="settings-row-desc">{desc}</div></div><div className="settings-row-control">{children}</div></div>;
}

export function CardTitle({ children }: { children: React.ReactNode }) {
  return <div className="settings-card-title">{children}</div>;
}

/** 通用资源列表（增删 + 可选启停） */
export function GenericPanel<T extends { id: number }>({ loader, getName, getDesc, onToggle, onDelete, getActive }: {
  loader: () => Promise<T[]>;
  getName: (it: T) => string;
  getDesc: (it: T) => string;
  onToggle?: (it: T, v: boolean) => Promise<unknown>;
  onDelete: (it: T) => Promise<unknown>;
  getActive: (it: T) => boolean;
}) {
  const [items, setItems] = useState<T[]>([]);
  const load = useCallback(async () => { try { setItems(await loader()); } catch {} }, [loader]);
  useEffect(() => { load(); }, [load]);
  return (
    <div className="settings-resource-list">
      {items.map((it) => (
        <div key={it.id} className="settings-resource-item">
          <div className="settings-resource-info">
            <div className="settings-resource-name">{getName(it)}</div>
            <div className="settings-resource-desc">{getDesc(it)}</div>
          </div>
          <div className="settings-resource-actions">
            {onToggle && <Sw checked={getActive(it)} onChange={async (v) => { try { await onToggle(it, v); load(); } catch {} }} />}
            <button className="btn btn-ghost btn-xs" onClick={async () => { if (confirm("删除？")) { try { await onDelete(it); load(); } catch {} } }}><IconX size={12} /></button>
          </div>
        </div>
      ))}
      {items.length === 0 && <div className="navpage-empty">暂无数据</div>}
    </div>
  );
}
