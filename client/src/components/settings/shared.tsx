/** 设置中心共享组件（plan-282-1416：改为组件库薄封装）。
 *
 * Row → CardRow（卡片行）、Sw → Switch（Radix 开关），
 * 消除此前三套开关实现（ui/Switch、.ui-switch、.sp-switch）与内联行样式。
 */
import { useCallback, useEffect, useState } from "react";
import { IconX } from "../icons";
import { ConfirmDialog } from "../ConfirmDialog";
import { CardRow, IconButton, List, ListRow, Switch } from "../ui";

/** 开关（兼容旧签名 checked/onChange） */
export function Sw({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return <Switch checked={checked} onChange={onChange} />;
}

/** 卡片行：标题 + 描述 + 右侧控件槽 */
export function Row({ title, desc, children, className }: { title: string; desc: string; children: React.ReactNode; className?: string }) {
  return <CardRow title={title} desc={desc} className={className}>{children}</CardRow>;
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
  const [confirmTarget, setConfirmTarget] = useState<T | null>(null);
  const load = useCallback(async () => { try { setItems(await loader()); } catch {} }, [loader]);
  useEffect(() => { load(); }, [load]);
  return (
    <div className="settings-resource-list">
      <List>
        {items.map((it) => (
          <ListRow
            key={it.id}
            name={getName(it)}
            desc={getDesc(it)}
            actions={
              <>
                {onToggle && <Sw checked={getActive(it)} onChange={async (v) => { try { await onToggle(it, v); load(); } catch {} }} />}
                <IconButton size="xs" icon={<IconX size={12} />} title="删除" onClick={() => setConfirmTarget(it)} />
              </>
            }
          />
        ))}
      </List>
      {items.length === 0 && <div className="navpage-empty">暂无数据</div>}
      <ConfirmDialog
        open={confirmTarget !== null}
        title="删除"
        message={`删除「${confirmTarget ? getName(confirmTarget) : ""}」？`}
        danger
        onCancel={() => setConfirmTarget(null)}
        onConfirm={async () => {
          const it = confirmTarget;
          setConfirmTarget(null);
          if (!it) return;
          try { await onDelete(it); load(); } catch { /* ignore */ }
        }}
      />
    </div>
  );
}
