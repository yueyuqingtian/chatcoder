/** ModelPicker（plan-238-1191：基于 Radix DropdownMenu 重写）：设置页表单里的模型选择控件。
 * 一级 = 供应商菜单（单行名称 + 右侧 ›，当前模型所在供应商显示 ✓）；
 * 二级 = 该供应商的模型子菜单（悬停/聚焦即滑出，选中项左侧 ✓）。
 * 底部「管理模型」打开设置页。
 * plan-20261025-composer：输入框改用「模型 · 思考深度」合并浮层（composer/ModelEffortPicker），
 * 这里只保留表单形态（与 ui/Select 同高、同边框、带右侧箭头）；分组与积分逻辑在 modelGroups 与其共用。
 *
 * 视觉沿用项目设计变量（bg-elevated / border / shadow / bg-hover / accent），
 * 弹层定位、碰撞翻转、键盘导航与 ARIA 语义由 Radix 组件库承担（替换此前
 * 手写 portal + 坐标 clamp + 两级网格的方形弹窗）。
 */
import { useMemo } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { ModelOut } from "../../api/client";
import { IconCpu, IconCheck, IconChevronDown, IconChevronRight, IconMultimodal } from "../icons";
import { useDialogContentHost } from "../ui/Dialog";
import { Empty, Tooltip } from "../ui";
import { buildModelGroups, CreditsBadge, openModelSettings, useWorkbuddyCredits, type ModelGroup } from "./modelGroups";

/** 模型附加信息标签（长上下文 / 消耗倍率 / 多模态图标） */
function ModelTags({ m }: { m: ModelOut }) {
  const hasTags = Boolean(m.trae_max_context || m.trae_consumption_rate || m.is_multimodal);
  if (!hasTags) return <span className="mp-tags" />;
  return (
    <span className="mp-tags">
      {m.trae_max_context ? <span className="mp-tag">长上下文</span> : null}
      {m.trae_consumption_rate ? <span className="mp-tag">消耗×{m.trae_consumption_rate}</span> : null}
      {m.is_multimodal ? (
        <Tooltip content="支持图片输入（多模态）"><span className="mp-tag mp-tag-icon"><IconMultimodal size={12} /></span></Tooltip>
      ) : null}
    </span>
  );
}

export function ModelPicker({
  models,
  value,
  onChange,
  open,
  onToggle,
  inheritLabel,
  onInherit,
  side = "top",
}: {
  models: ModelOut[];
  value: number | null;
  onChange: (id: number) => void;
  open: boolean;
  onToggle: () => void;
  /** v36: 提供时菜单顶部增加「继承项」（如子代理的「跟随主代理」），
   *  选中回调 onInherit；同时触发器在 value 为空时展示该文案。 */
  inheritLabel?: string;
  onInherit?: () => void;
  /** 菜单弹出方向：贴近窗口底部时用 top（默认）；设置页表单内用 bottom。 */
  side?: "top" | "bottom";
}) {
  // plan-41-227: 弹窗（ui/Dialog）内使用时，菜单 Portal 挂到弹窗内容节点——浮层若挂在 body 下，
  // 焦点一进入菜单就会被 Dialog 的 FocusScope 判为「弹窗外部」而抢回，级联子菜单刚展开即关闭
  //（用户反馈：右移鼠标选模型时模型窗消失）。非弹窗场景保持默认 body，行为不变。
  const overlayHost = useDialogContentHost();

  const groups = useMemo<ModelGroup[]>(() => buildModelGroups(models, value), [models, value]);
  const { credits, busy, refresh } = useWorkbuddyCredits(groups, open);

  /** 积分徽标 + 刷新按钮（一级供应商行与二级菜单头共用） */
  const renderCredits = (g: ModelGroup) => {
    if (!g.workbuddy || g.providerId == null) return null;
    const pid = g.providerId;
    return <CreditsBadge value={credits[pid]} busy={!!busy[pid]} onRefresh={() => void refresh(pid)} />;
  };

  const activeModel = models.find((m) => m.id === value);
  // v36: 未选模型时优先展示继承项文案（子代理场景为「跟随主代理」）
  const label = activeModel
    ? activeModel.provider_name
      ? `${activeModel.provider_name}/${activeModel.name}`
      : activeModel.name
    : inheritLabel || "模型";

  return (
    <div className="composer-model-wrap is-field">
      <DropdownMenu.Root open={open} onOpenChange={(next) => { if (next !== open) onToggle(); }}>
        <DropdownMenu.Trigger asChild>
          <button className={"ui-select-trigger mp-field" + (activeModel ? "" : " is-placeholder")} type="button" aria-label={label}>
            <IconCpu size={14} />
            <span className="mp-label">{label}</span>
            <span className="ui-select-icon"><IconChevronDown size={14} /></span>
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal container={overlayHost ?? undefined}>
          {/* align=start 与触发按钮左缘对齐，保证菜单文本整体左对齐（plan-238-1210）；
              越界翻转/平移由 avoidCollisions 处理 */}
          <DropdownMenu.Content
            className="mp-menu"
            side={side}
            align="start"
            sideOffset={6}
            collisionPadding={8}
          >
            <div className="mp-menu-head">选择模型</div>
            {/* v36: 继承项（子代理「跟随主代理」）——置顶展示，当前为空值时打勾 */}
            {inheritLabel ? (
              <>
                <DropdownMenu.Item
                  className={"mp-item" + (value == null ? " active" : "")}
                  onSelect={() => onInherit?.()}
                >
                  <span className="mp-item-check">{value == null && <IconCheck size={12} />}</span>
                  <span className="mp-item-name">{inheritLabel}</span>
                </DropdownMenu.Item>
                <DropdownMenu.Separator className="mp-sep" />
              </>
            ) : null}
            {groups.length === 0 ? (
              <>
                <Empty size="sm" title="暂无可用模型" desc="请先在设置中添加供应商与模型" />
                <DropdownMenu.Separator className="mp-sep" />
                <DropdownMenu.Item className="mp-item" onSelect={() => openModelSettings()}>
                  <span className="mp-item-check" />
                  <span className="mp-item-name">管理模型</span>
                </DropdownMenu.Item>
              </>
            ) : (
              <>
                {groups.map((g) => {
                  const holdsCurrent = g.models.some((m) => m.id === value);
                  return (
                    <DropdownMenu.Sub key={g.name}>
                      <DropdownMenu.SubTrigger className="mp-group">
                        <Tooltip content={g.name}><span className="mp-group-name">{g.name}</span></Tooltip>
                        {renderCredits(g)}
                        {holdsCurrent
                          ? <span className="mp-group-current"><IconCheck size={12} /></span>
                          : <span className="mp-group-arrow"><IconChevronRight size={13} /></span>}
                      </DropdownMenu.SubTrigger>
                      <DropdownMenu.Portal container={overlayHost ?? undefined}>
                        {/* S10（plan-41-197）：子菜单零间隙 + 无对齐偏移——此前 6px 间隙 +
                            负对齐偏移会让鼠标在“右移选模型”途中掉出 Radix 的指针宽限区，
                            二级菜单被关闭（用户反馈“右移鼠标后模型窗消失，无法选择模型”）。 */}
                        <DropdownMenu.SubContent
                          className="mp-menu mp-sub-menu"
                          sideOffset={0}
                          alignOffset={0}
                          collisionPadding={8}
                        >
                          <div className="mp-menu-head">{g.name}{renderCredits(g)}</div>
                          {g.models.map((m) => (
                            <DropdownMenu.Item
                              key={m.id}
                              className={"mp-item" + (m.id === value ? " active" : "")}
                              title={m.name}
                              onSelect={() => onChange(m.id)}
                            >
                              <span className="mp-item-check">{m.id === value && <IconCheck size={12} />}</span>
                              <span className="mp-item-name">{m.name}</span>
                              <ModelTags m={m} />
                            </DropdownMenu.Item>
                          ))}
                        </DropdownMenu.SubContent>
                      </DropdownMenu.Portal>
                    </DropdownMenu.Sub>
                  );
                })}
                <DropdownMenu.Separator className="mp-sep" />
                <DropdownMenu.Item className="mp-item" onSelect={() => openModelSettings()}>
                  <span className="mp-item-check" />
                  <span className="mp-item-name">管理模型</span>
                </DropdownMenu.Item>
              </>
            )}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  );
}
