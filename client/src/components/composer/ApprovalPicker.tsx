/** 权限模式选择（plan-20261009-ui U2）：胶囊 + 标准浮层（图标 + 名称 + 一行说明 + 选中勾）。
 *  「完全访问」以危险色提示；前者管「能做什么」（执行模式），这里管「要不要问」。 */
import { useRef } from "react";
import { Popover, Tooltip } from "../ui";
import { IconCheck, IconChevronDown } from "../icons";
import { useI18n } from "../../store/i18n";
import type { ApprovalMode } from "../../store/drafts";
import { APPROVAL_META, APPROVAL_ORDER } from "./modeMeta";

export function ApprovalPicker({ open, onToggle, onClose, value, onChange }: {
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  value: ApprovalMode;
  onChange: (v: ApprovalMode) => void;
}) {
  const { t } = useI18n();
  const btnRef = useRef<HTMLButtonElement>(null);
  const meta = APPROVAL_META[value];
  return (
    <div className="cc-anchor">
      <Tooltip content={t("composer.approval_title") + "：" + t(meta.labelKey)} side="top" disabled={open}>
        <button
          ref={btnRef}
          type="button"
          className={"cc-chip cc-approval-chip" + (meta.danger ? " is-danger" : "") + (open ? " is-open" : "")}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={t("composer.approval_title") + "：" + t(meta.labelKey)}
          onClick={onToggle}
        >
          {meta.icon(14)}
          <span className="cc-chip-label">{t(meta.labelKey)}</span>
          <IconChevronDown size={12} className="cc-chip-caret" />
        </button>
      </Tooltip>
      <Popover open={open} onClose={onClose} placement="top-start" className="cc-approval-pop" role="menu" ignoreRefs={[btnRef]} aria-label={t("composer.approval_title")}>
        <div className="pop-label">{t("composer.approval_title")}</div>
        {APPROVAL_ORDER.map((m) => {
          const mm = APPROVAL_META[m];
          const active = m === value;
          return (
            <button
              key={m}
              type="button"
              role="menuitemradio"
              aria-checked={active}
              className={"pop-item" + (mm.danger ? " is-danger" : "") + (active ? " is-selected" : "")}
              onClick={() => { onChange(m); onClose(); }}
            >
              <span className="pop-item-icon">{mm.icon(16)}</span>
              <span className="pop-item-main">
                <span className="pop-item-title">{t(mm.labelKey)}</span>
                <span className="pop-item-desc">{t(mm.descKey)}</span>
              </span>
              <span className="pop-item-end">{active && <span className="pop-item-check"><IconCheck size={14} /></span>}</span>
            </button>
          );
        })}
      </Popover>
    </div>
  );
}
