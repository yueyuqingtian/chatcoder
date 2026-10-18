/** 内置连接器（数据库连接 / 开发调试）配置的共用外壳（plan-20261017-goal12 项 10）。
 *
 * 两个连接器的配置都在「连接器配置」弹窗里展示，统一采用弹窗的分组语言：
 *  - 顶部一张总开关卡（图标磁贴 + 名称 + 说明 + 开关）；
 *  - 正文按分组排列（DialogSection 标题 + 白色分组卡 InsetCard），行内是「标题 + 说明 + 控件」；
 *  - 底部一条保存条：只在有未保存修改时启用保存，状态文案占固定位置，不随状态跳动。 */
import type { ReactNode } from "react";
import { Button, InsetCard, InsetRow, Switch } from "../ui";

/** 连接器图标磁贴（与弹窗图标区同一层级，圆角走令牌） */
export function ConnectorTile({ children }: { children: ReactNode }) {
  return <span className="cx-tile" aria-hidden>{children}</span>;
}

/** 顶部总开关卡：启用 / 停用整个内置连接器 */
export function ConnectorMaster({ icon, title, desc, checked, onChange }: {
  icon: ReactNode;
  title: string;
  desc: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <InsetCard className="cx-master">
      <InsetRow
        icon={<ConnectorTile>{icon}</ConnectorTile>}
        title={title}
        desc={desc}
        actions={<Switch checked={checked} onChange={onChange} aria-label={`${title} 启用`} />}
      />
    </InsetCard>
  );
}

/** 底部保存条：有未保存修改时才可点，提示文案与按钮同高、固定位置 */
export function ConnectorSaveBar({ dirty, saving, idleText, onSave }: {
  dirty: boolean;
  saving: boolean;
  idleText: string;
  onSave: () => void;
}) {
  return (
    <div className="cx-savebar">
      <span className={`cx-savebar-hint${dirty ? " is-dirty" : ""}`} role="status">
        {saving ? "保存中…" : dirty ? "有未保存的修改" : idleText}
      </span>
      <Button variant="primary" size="sm" onClick={onSave} disabled={!dirty || saving}>
        保存
      </Button>
    </div>
  );
}

/** 分组卡里的「标题 + 说明 + 控件」行，危险项用警示色标题 */
export function ConnectorSwitchRow({ title, desc, checked, onChange, danger = false }: {
  title: string;
  desc: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  danger?: boolean;
}) {
  return (
    <InsetRow
      title={<span className={danger ? "cx-danger" : undefined}>{title}</span>}
      desc={desc}
      actions={<Switch checked={checked} onChange={onChange} aria-label={title} />}
    />
  );
}
