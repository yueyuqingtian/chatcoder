/** 模型 · 思考深度合并选择（plan-20261025-composer，参考图1 / 图2）。
 *
 * 触发器是纯文字「模型名 档位 ⌄」（不带图标），紧挨发送按钮；点开一个浮层就能看全「用哪个模型、想多深」：
 *  ① 当前供应商行：悬停 120ms / 点击 / → 在旁边展开供应商列表（离开有 220ms 宽限，穿过两层之间的缝不误关）；
 *     列表里点选只切换下方的模型列表、不改当前模型；底部「管理模型」去设置页；
 *  ② 「模型」：所浏览供应商的模型，点选即切换并收起；
 *  ③ 「思考深度」：所选模型配置了档位才出现；分档滑块可连续拖动，拉到最高档轨道里有粒子流动。
 * 每次打开都从当前模型所在的供应商看起；Esc 先关二级再关一级，↑↓ 在条目间移动，→ / ← 进出供应商列表。
 * 此前模型是 Radix 级联菜单、思考深度是另一枚胶囊加浮窗：两套交互、两枚带图标的胶囊挤在发送按钮旁。
 *
 * 两层浮层都用 ui/Floating（挂到 body、按实测矩形定位）而不是 Popover：供应商列表贴在主浮层旁边，
 * 向右会越出中间卡片、窄窗翻到左侧会压到侧栏，absolute 浮层都会被卡片的 overflow 裁掉；
 * 两层同属 Floating 的浮层栈，Esc 才能先关二级。 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { ModelOut } from "../../api/client";
import { Badge, Empty, Floating, StepSlider, Tooltip } from "../ui";
import { IconCheck, IconChevronDown, IconChevronRight, IconSliders } from "../icons";
import { ProviderAvatar } from "../settings/ProviderAvatar";
import {
  buildModelGroups, CreditsBadge, ModelTagBadges, openModelSettings, providerKindLabel, useWorkbuddyCredits, type ModelGroup,
} from "../chat/modelGroups";
import { effortLabel, sortEfforts } from "./modeMeta";

/** 在 root 内的可聚焦条目间按方向移动焦点（首尾循环） */
function moveFocus(root: HTMLElement, dir: 1 | -1, selector: string) {
  const items = Array.from(root.querySelectorAll<HTMLElement>(selector));
  if (items.length === 0) return;
  const i = items.indexOf(document.activeElement as HTMLElement);
  const next = i < 0 ? (dir > 0 ? 0 : items.length - 1) : (i + dir + items.length) % items.length;
  items[next].focus();
}

export function ModelEffortPicker({ models, value, onChange, open, onToggle, onClose, effort, onEffortChange }: {
  models: ModelOut[];
  /** 当前模型 id */
  value: number | null;
  onChange: (id: number) => void;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  /** 当前生效的思考深度档位 */
  effort: string | null;
  /** 滑块越过档位即回报（拖动中实时调用，浮层保持打开、可一路拖下去） */
  onEffortChange: (e: string) => void;
}) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const provRowRef = useRef<HTMLButtonElement>(null);
  /** 主浮层内容盒：供应商列表以它为锚贴边展开 */
  const mainBoxRef = useRef<HTMLDivElement>(null);
  /** 供应商列表内容盒：主浮层判定「点外部」时要排除它（两层各自挂在 body 下，DOM 上互不包含） */
  const subBoxRef = useRef<HTMLDivElement>(null);
  // 引用必须稳定：Floating 的关闭监听以它为依赖，每次渲染换新数组会把主浮层重新压到浮层栈顶，Esc 就先关了主浮层
  const mainIgnore = useMemo(() => [subBoxRef], []);

  const groups = useMemo<ModelGroup[]>(() => buildModelGroups(models, value), [models, value]);
  const { credits, busy, refresh } = useWorkbuddyCredits(groups, open);

  const activeModel = models.find((m) => m.id === value) ?? null;
  const efforts = sortEfforts(activeModel?.reasoning_efforts ?? []);
  const supportsReasoning = efforts.length > 0;
  const effortIdx = effort ? efforts.indexOf(effort) : -1;
  const sliderIdx = effortIdx >= 0 ? effortIdx : Math.max(0, efforts.length - 1);
  /** plan-562-2318 B8：滑块与文字必须同一个来源。
   *  切到不支持当前档位的新模型后 `effort` 仍是旧值（不在 `efforts` 里），滑块兜底到某一档，
   *  而文字仍渲染 `effort`——两者对不上，直到用户拖动一次才纠正。这里统一取滑块那一档。 */
  const shownEffort = efforts[sliderIdx] ?? effort;

  /** 正在浏览的供应商（null = 当前模型所在供应商） */
  const [browse, setBrowse] = useState<string | null>(null);
  const [provOpen, setProvOpen] = useState(false);
  const currentGroup = activeModel ? activeModel.provider_name || "独立模型" : null;
  const shown = groups.find((g) => g.name === (browse ?? currentGroup)) ?? groups[0] ?? null;

  // ── 供应商二级：悬停延时打开 / 离开宽限关闭（与「+」菜单的拓展二级同一套手感） ──
  // 模型行悬停不立即收起二级：斜向移往列表下方的供应商时指针会擦过模型行，只靠 220ms 宽限兜住。
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  const clearTimers = () => {
    if (openTimer.current) { window.clearTimeout(openTimer.current); openTimer.current = null; }
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
  };
  useEffect(() => () => clearTimers(), []);
  // 每次打开从当前模型所在供应商看起；layout 阶段重置，浮层首帧即是正确内容
  useLayoutEffect(() => {
    if (!open) { clearTimers(); return; }
    setBrowse(null);
    setProvOpen(false);
  }, [open]);

  const hoverProv = () => {
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
    if (provOpen || openTimer.current) return;
    openTimer.current = window.setTimeout(() => { openTimer.current = null; setProvOpen(true); }, 120);
  };
  const leaveProv = () => {
    if (openTimer.current) { window.clearTimeout(openTimer.current); openTimer.current = null; }
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => { closeTimer.current = null; setProvOpen(false); }, 220);
  };
  /** 键盘打开二级时把焦点移进列表（鼠标悬停打开不抢焦点） */
  const focusSubRef = useRef(false);
  const openProv = (byKeyboard: boolean) => {
    clearTimers();
    focusSubRef.current = byKeyboard;
    setProvOpen(true);
  };
  const closeProv = (refocus: boolean) => {
    clearTimers();
    setProvOpen(false);
    if (refocus) provRowRef.current?.focus();
  };
  const pickProv = (name: string) => {
    setBrowse(name);
    closeProv(true);
  };
  // Floating 落位前是 visibility:hidden（隐藏元素无法聚焦），挂载后的下一个任务里再把焦点移进列表
  useEffect(() => {
    if (!provOpen || !focusSubRef.current) return;
    focusSubRef.current = false;
    const t = window.setTimeout(() => {
      const box = subBoxRef.current;
      (box?.querySelector<HTMLElement>(".pop-item.is-selected") ?? box?.querySelector<HTMLElement>(".pop-item"))?.focus();
    }, 0);
    return () => window.clearTimeout(t);
  }, [provOpen]);

  const onMenuKey = (e: KeyboardEvent<HTMLDivElement>) => {
    // 滑块拇指自己消费方向键（调档位）；二级的按键在它自己那里处理并停止冒泡
    if (e.defaultPrevented || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    e.preventDefault();
    moveFocus(e.currentTarget, e.key === "ArrowDown" ? 1 : -1, ".pop-item, .ui-step-thumb");
  };
  const onSubKey = (e: KeyboardEvent<HTMLDivElement>) => {
    // 列表翻到主浮层左侧时，「返回」方向随之对调
    const back = e.currentTarget.getAttribute("data-side") === "left" ? "ArrowRight" : "ArrowLeft";
    if (e.key === back) { e.preventDefault(); e.stopPropagation(); closeProv(true); return; }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    e.stopPropagation();
    moveFocus(e.currentTarget, e.key === "ArrowDown" ? 1 : -1, ".pop-item");
  };

  const renderKind = (g: ModelGroup) => {
    const kind = providerKindLabel(g);
    return kind ? <Badge size="xs">{kind}</Badge> : null;
  };
  const renderCredits = (g: ModelGroup) => {
    if (!g.workbuddy || g.providerId == null) return null;
    const pid = g.providerId;
    return <CreditsBadge value={credits[pid]} busy={!!busy[pid]} onRefresh={() => void refresh(pid)} />;
  };

  const tip = (activeModel
    ? `模型：${activeModel.provider_name ? `${activeModel.provider_name}/` : ""}${activeModel.name}`
    : "选择模型")
    + (supportsReasoning ? ` · 思考深度：${effortLabel(effort)}` : "");

  const manageItem = (
    <button type="button" className="pop-item" onClick={() => { onClose(); openModelSettings(); }}>
      <span className="pop-item-icon"><IconSliders size={15} /></span>
      <span className="pop-item-title">管理模型</span>
    </button>
  );

  return (
    <div className="cc-anchor">
      <Tooltip content={tip} side="top" disabled={open}>
        <button
          ref={btnRef}
          type="button"
          className={"cc-chip cc-model-chip" + (open ? " is-open" : "")}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label={tip}
          onClick={onToggle}
          onKeyDown={(e) => {
            // 浮层向上展开：打开时 ↑ / ↓ 直接进入浮层，落在当前模型上
            if (!open || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
            const box = mainBoxRef.current;
            if (!box) return;
            e.preventDefault();
            (box.querySelector<HTMLElement>(".cc-mp-model.is-selected") ?? box.querySelector<HTMLElement>(".pop-item"))?.focus();
          }}
        >
          <span className="cc-model-name">{activeModel?.name ?? "选择模型"}</span>
          {supportsReasoning && <span className="cc-model-effort">{effortLabel(effort)}</span>}
          <IconChevronDown size={12} className="cc-chip-caret" />
        </button>
      </Tooltip>
      <Floating
        open={open}
        anchorRef={btnRef}
        onClose={onClose}
        side="top"
        align="end"
        gap={6}
        aria-label="模型与思考深度"
        ignoreRefs={mainIgnore}
        onKeyDown={onMenuKey}
      >
        <div ref={mainBoxRef} className="cc-model-pop">
          {!shown ? (
            <>
              <Empty size="sm" title="暂无可用模型" desc="请先在设置中添加供应商与模型" />
              <div className="pop-sep" />
              {manageItem}
            </>
          ) : (
            <>
              <div className="cc-mp-prov" onMouseEnter={hoverProv} onMouseLeave={leaveProv}>
                <button
                  ref={provRowRef}
                  type="button"
                  className={"pop-item cc-mp-prov-row" + (provOpen ? " is-hl" : "")}
                  aria-haspopup="menu"
                  aria-expanded={provOpen}
                  onClick={(e) => openProv(e.detail === 0)}
                  onKeyDown={(e) => { if (e.key === "ArrowRight") { e.preventDefault(); openProv(true); } }}
                >
                  <ProviderAvatar name={shown.name} size={20} />
                  <span className="pop-item-title">{shown.name}</span>
                  {renderKind(shown)}
                  {renderCredits(shown)}
                  <span className="pop-item-end"><IconChevronRight size={14} /></span>
                </button>
                {/* 供应商列表：React 树上仍是这一行的子节点（悬停宽限按 React 树判定进出），
                    DOM 上挂到 body；外部点击由主浮层统一收起 */}
                <Floating
                  open={open && provOpen}
                  anchorRef={mainBoxRef}
                  onClose={() => closeProv(Boolean(subBoxRef.current?.contains(document.activeElement)))}
                  side="right"
                  align="start"
                  gap={6}
                  role="menu"
                  aria-label="选择供应商"
                  closeOnOutside={false}
                  onKeyDown={onSubKey}
                >
                  <div ref={subBoxRef} className="cc-prov-pop">
                    <div className="pop-scroll cc-prov-list">
                      {groups.map((g) => {
                        const on = g.name === shown.name;
                        return (
                          <button
                            key={g.name}
                            type="button"
                            role="menuitemradio"
                            aria-checked={on}
                            className={"pop-item cc-prov-item" + (on ? " is-selected" : "")}
                            onClick={() => pickProv(g.name)}
                          >
                            <span className="pop-item-title">{g.name}</span>
                            {renderKind(g)}
                            {renderCredits(g)}
                            <span className="pop-item-end">{on && <span className="pop-item-check"><IconCheck size={14} /></span>}</span>
                          </button>
                        );
                      })}
                    </div>
                    <div className="pop-sep" />
                    {manageItem}
                  </div>
                </Floating>
              </div>

              <div className="pop-sep" />
              <div className="pop-label">模型</div>
              <div className="pop-scroll cc-mp-models" role="listbox" aria-label="模型">
                {shown.models.map((m) => {
                  const on = m.id === value;
                  return (
                    <button
                      key={m.id}
                      type="button"
                      role="option"
                      aria-selected={on}
                      className={"pop-item cc-mp-model" + (on ? " is-selected" : "")}
                      onClick={() => { onChange(m.id); onClose(); }}
                    >
                      <span className="pop-item-title">{m.name}</span>
                      <ModelTagBadges m={m} />
                      <span className="pop-item-end">{on && <span className="pop-item-check"><IconCheck size={14} /></span>}</span>
                    </button>
                  );
                })}
              </div>
            </>
          )}

          {supportsReasoning && (
            <>
              <div className="pop-sep" />
              <div className="cc-mp-effort">
                <Tooltip content="深度越高，推理越充分、回答越周全，但耗时与用量也更多" side="top">
                  <div className="cc-mp-effort-head">
                    <span className="cc-mp-effort-title">思考深度</span>
                    {/* key 随档位变化重挂载，档位名从下方轻轻升起 */}
                    <span className="cc-mp-effort-value" key={shownEffort ?? ""}>{effortLabel(shownEffort)}</span>
                  </div>
                </Tooltip>
                <StepSlider
                  steps={efforts.map((e) => ({ key: e, label: effortLabel(e) }))}
                  index={sliderIdx}
                  onIndexChange={(i) => { const e = efforts[i]; if (e && e !== effort) onEffortChange(e); }}
                  disabled={efforts.length <= 1}
                  showLabels={false}
                  flowAtMax
                  aria-label="思考深度"
                />
              </div>
            </>
          )}
        </div>
      </Floating>
    </div>
  );
}
