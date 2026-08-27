/** ModelPicker（v18 级联重写）：两级级联选择器（供应商 → 模型）。
 * 左栏供应商分组，右栏该供应商下的模型列表；ComposerBox / Workspace 空态输入框共用。
 * 底部「管理模型」打开设置页。
 * v19: 菜单改 fixed 定位（右缘对齐触发按钮、clamp 在视口内，窄面板不再溢出）；
 *      多模态角标改为 图标+文字 的精致药丸。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ModelOut } from "../../api/client";
import { IconCpu, IconCheck, IconBox, IconImage } from "../icons";

interface ModelGroup {
  name: string;
  models: ModelOut[];
}

export function openModelSettings() {
  window.dispatchEvent(new CustomEvent("chatcoder:open-settings", { detail: { tab: "models" } }));
}

export function ModelPicker({
  models,
  value,
  onChange,
  open,
  onToggle,
}: {
  models: ModelOut[];
  value: number | null;
  onChange: (id: number) => void;
  open: boolean;
  onToggle: () => void;
}) {
  const groups = useMemo<ModelGroup[]>(() => {
    const map = new Map<string, ModelOut[]>();
    for (const m of models) {
      if (!m.is_active && m.id !== value) continue;
      // trae 供应商：TRAE 目录含大量工具/占位模型，客户端实际可用的才展示
      if (m.api_format === "trae" && !m.trae_available) continue;
      const g = m.provider_name || "独立模型";
      if (!map.has(g)) map.set(g, []);
      map.get(g)!.push(m);
    }
    // 组内按名称排序，组按名称排序（"独立模型" 排最后）
    const arr = [...map.entries()].map(([name, ms]) => ({
      name,
      models: ms.sort((a, b) => a.name.localeCompare(b.name)),
    }));
    arr.sort((a, b) => (a.name === "独立模型" ? 1 : b.name === "独立模型" ? -1 : a.name.localeCompare(b.name)));
    return arr;
  }, [models, value]);

  const activeModel = models.find((m) => m.id === value);
  const [activeGroup, setActiveGroup] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [menuPos, setMenuPos] = useState<{
    left: number;
    top?: number;
    bottom?: number;
    maxHeight: number;
  } | null>(null);

  // Portal 菜单挂到 body，坐标和 getBoundingClientRect() 都使用视口坐标，避免被空态布局的 overflow 或容器查询上下文影响。
  useLayoutEffect(() => {
    if (!open) { setMenuPos(null); return; }
    const calc = () => {
      const r = wrapRef.current?.getBoundingClientRect();
      if (!r) return;
      const W = Math.min(460, Math.max(0, window.innerWidth - 16));
      const left = Math.max(8, Math.min(r.right - W, window.innerWidth - W - 8));
      const gap = 6;
      const spaceAbove = Math.max(0, r.top - gap - 8);
      const spaceBelow = Math.max(0, window.innerHeight - r.bottom - gap - 8);
      // 输入框靠近底部时向上展开；可用高度不足时限制菜单内部滚动，不能溢出窗口。
      if (spaceAbove >= 280 || spaceAbove >= spaceBelow) {
        setMenuPos({ left, bottom: Math.max(8, window.innerHeight - r.top + gap), maxHeight: Math.max(180, spaceAbove) });
      } else {
        setMenuPos({ left, top: Math.max(8, r.bottom + gap), maxHeight: Math.max(180, spaceBelow) });
      }
    };
    calc();
    window.addEventListener("resize", calc);
    window.addEventListener("scroll", calc, true);
    return () => {
      window.removeEventListener("resize", calc);
      window.removeEventListener("scroll", calc, true);
    };
  }, [open]);

  // 打开时定位到当前模型所在供应商
  useEffect(() => {
    if (!open) return;
    const g = groups.find((x) => x.models.some((m) => m.id === value));
    setActiveGroup(g?.name ?? groups[0]?.name ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const currentGroup = groups.find((g) => g.name === activeGroup) ?? groups[0];

  const label = activeModel
    ? activeModel.provider_name
      ? `${activeModel.provider_name}/${activeModel.name}`
      : activeModel.name
    : "模型";

  return (
    <div className="composer-model-wrap" ref={wrapRef}>
      <button className="composer-model-badge" onClick={onToggle} title={label}>
        <IconCpu size={13} />
        <span className="mp-label">{label}</span>
      </button>
      {open && menuPos && createPortal(
        <div
          className="composer-menu composer-model-menu mp-menu"
          style={{
            position: "fixed",
            left: menuPos.left,
            top: menuPos.top ?? "auto",
            bottom: menuPos.bottom ?? "auto",
            right: "auto",
            maxHeight: menuPos.maxHeight,
          }}
        >
          <div className="composer-menu-title mp-menu-head"><span>选择模型</span><span className="mp-menu-hint">供应商 / 模型</span></div>
          {groups.length === 0 ? (
            <div className="composer-menu-empty">
              <div>暂无可用模型，请先在设置中添加</div>
              <div style={{ marginTop: 6 }}>
                <button className="mp-manage" onClick={() => { openModelSettings(); }}>管理模型</button>
              </div>
            </div>
          ) : (
            <div className="mp-cascade">
              <div className="mp-col mp-col-groups">
                <div className="mp-groups-list">
                  {groups.map((g) => (
                    <button
                      key={g.name}
                      className={"mp-group" + (g.name === activeGroup ? " active" : "")}
                      title={g.name}
                      onClick={() => setActiveGroup(g.name)}
                    >
                      <IconBox size={13} className="mp-group-icon" />
                      <span className="mp-group-name">{g.name}</span>
                      <span className="mp-group-count">{g.models.length}</span>
                    </button>
                  ))}
                </div>
                <div className="mp-left-footer">
                  <button className="mp-manage" onClick={() => { openModelSettings(); }}>管理模型</button>
                </div>
              </div>
              <div
                className="mp-col mp-col-models"
                onWheel={(e) => {
                  e.stopPropagation();
                }}
              >
                {currentGroup?.models.map((m) => (
                  <button
                    key={m.id}
                    className={"mp-item" + (m.id === value ? " active" : "")}
                    title={m.name}
                    onClick={() => onChange(m.id)}
                  >
                    <span className="mp-item-check">{m.id === value && <IconCheck size={12} />}</span>
                    <span className="mp-item-name">{m.name}</span>
                    <span className="mp-tags">
                      {m.trae_max_context ? <span className="mp-tag">1M上下文</span> : null}
                      {(m.reasoning_efforts?.length ?? 0) > 0 && (
                        <span className="mp-tag">思考深度</span>
                      )}
                      {m.trae_consumption_rate ? <span className="mp-tag">消耗×{m.trae_consumption_rate}</span> : null}
                      {m.is_multimodal && <span className="mp-tag"><IconImage size={9} />多模态</span>}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}
