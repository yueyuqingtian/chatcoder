/** ModelPicker（v18 级联重写）：两级级联选择器（供应商 → 模型）。
 * 左栏供应商分组，右栏该供应商下的模型列表；ComposerBox / Workspace 空态输入框共用。
 * 底部「管理模型」打开设置页。
 */
import { useEffect, useMemo, useState } from "react";
import type { ModelOut } from "../../api/client";
import { IconCpu, IconCheck, IconBox } from "../icons";

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
    <div className="composer-model-wrap">
      <button className="composer-model-badge" onClick={onToggle} title={label}>
        <IconCpu size={13} />
        <span className="mp-label">{label}</span>
      </button>
      {open && (
        <div className="composer-menu composer-model-menu mp-menu">
          <div className="composer-menu-title">选择模型</div>
          {groups.length === 0 ? (
            <div className="composer-menu-empty">暂无可用模型，请先在设置中添加</div>
          ) : (
            <div className="mp-cascade">
              <div className="mp-col mp-col-groups">
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
              <div className="mp-col mp-col-models">
                {currentGroup?.models.map((m) => (
                  <button
                    key={m.id}
                    className={"mp-item" + (m.id === value ? " active" : "")}
                    title={m.name}
                    onClick={() => onChange(m.id)}
                  >
                    <span className="mp-item-check">{m.id === value && <IconCheck size={12} />}</span>
                    <span className="mp-item-name">{m.name}</span>
                    {m.is_multimodal && <span className="mp-tag">多模态</span>}
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="mp-footer">
            <button className="mp-manage" onClick={() => { openModelSettings(); }}>管理模型</button>
          </div>
        </div>
      )}
    </div>
  );
}
