/** 设置中心：子代理（plan-156-619 S4e 精简）。
 *
 * 【为什么只剩这几个开关】
 * 旧面板让用户按「类型」配置工具白名单、系统提示词、模型、思考深度，再启用/停用类型。
 * 实际上工具范围应当由**执行模式**决定（只读 / 计划 / 智能体 / 工作流），
 * 而执行模式由主代理在创建子会话时按能力矩阵指定——让用户在这里手工勾选工具，
 * 等于把「能不能写文件」这件事交给两份配置互相覆盖。
 *
 * 现在只保留真正该由用户决定的：
 *   · 总开关（关闭后主代理不再派发任何子代理 / 子会话）；
 *   · 默认模型（空 = 跟随主代理）；
 *   · 思考深度（空 = 跟随会话档位）；
 *   · 运行限制（并发上限 / 每轮派发上限）。
 *
 * 历史数据（subagent_profiles 表）保留不删，仅作为模型/思考深度的兜底来源，
 * 回滚到旧版本时用户配置不会丢失。
 */
import { useCallback, useEffect, useState } from "react";
import { api, type ModelOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { Modal } from "../Modal";
import { Button, CardRow, Input, Section } from "../ui";
import { Sw } from "./shared";
import { ModelPicker } from "../chat/ModelPicker";

/** plan-330-1648 M2: 思考深度档位候选（与 ModelsPanel REASONING_OPTS 同口径） */
const REASONING_OPTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
/** plan-20261018-operator-boost：操控子会话思考深度——操作类工作只需低档，候选只给低档位 */
const OPERATOR_EFFORT_OPTS: Array<{ value: string; label: string }> = [
  { value: "lowest", label: "最低档" },
  { value: "none", label: "none" },
  { value: "minimal", label: "minimal" },
  { value: "low", label: "low" },
  { value: "medium", label: "medium" },
  { value: "follow", label: "跟随子会话" },
];

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点，统一改用全局提示条。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

export function SubagentsPanel() {
  const [models, setModels] = useState<ModelOut[]>([]);
  /** 全局设置：总开关 / 默认模型 / 思考深度 */
  const [cfg, setCfg] = useState({
    enabled: true,
    modelId: 0,            // 0 = 跟随主代理
    reasoningEffort: "",   // "" = 跟随会话档位
  });
  const [modelOpen, setModelOpen] = useState(false);
  // plan-20261018-operator-boost：GUI 操作交给操控子会话 / 其思考深度
  const [op, setOp] = useState({ enabled: true, effort: "lowest" });
  // v36: 并发治理配置（全局设置）——同时运行上限 + 每轮派发总量上限
  const [limits, setLimits] = useState({ concurrent: 6, perTurn: 10 });
  const [limitsSaving, setLimitsSaving] = useState(false);
  // S10（plan-41-197）：并发配置收进弹窗（按钮触发），不再常驻表单
  const [limitsOpen, setLimitsOpen] = useState(false);

  const load = useCallback(async () => {
    try { setModels(await api.listModels()); } catch { /* 模型列表失败不阻塞其它项 */ }
    try {
      const g = await api.getGlobalSettings();
      setCfg({
        enabled: g.subagent_enabled !== false,
        modelId: typeof g.subagent_model_id === "number" ? g.subagent_model_id : 0,
        reasoningEffort: g.subagent_reasoning_effort || "",
      });
      setOp({
        enabled: g.operator_subsession_enabled !== false,
        effort: g.operator_reasoning_effort || "lowest",
      });
      setLimits({
        concurrent: typeof g.max_concurrent_subagents === "number" ? g.max_concurrent_subagents : 6,
        perTurn: typeof g.max_subagents_per_turn === "number" ? g.max_subagents_per_turn : 10,
      });
    } catch { /* 拉取失败沿用默认值 */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  /** 修改即保存（与设置页其它面板一致）：单项立即落盘，失败回滚并提示 */
  const patch = async (partial: Partial<{ enabled: boolean; modelId: number; reasoningEffort: string }>) => {
    const prev = cfg;
    const next = { ...cfg, ...partial };
    setCfg(next);
    try {
      await api.setGlobalSettings({
        subagent_enabled: next.enabled,
        subagent_model_id: next.modelId,
        subagent_reasoning_effort: next.reasoningEffort,
      });
    } catch (e) {
      setCfg(prev);
      notify("保存失败：" + String(e));
    }
  };

  const patchOperator = async (partial: Partial<{ enabled: boolean; effort: string }>) => {
    const prev = op;
    const next = { ...op, ...partial };
    setOp(next);
    try {
      await api.setGlobalSettings({
        operator_subsession_enabled: next.enabled,
        operator_reasoning_effort: next.effort,
      });
    } catch (e) {
      setOp(prev);
      notify("保存失败：" + String(e));
    }
  };

  /** v36: 保存并发上限（后端夹紧到合法区间并即时生效，无需重启） */
  const saveLimits = async () => {
    setLimitsSaving(true);
    try {
      await api.setGlobalSettings({
        max_concurrent_subagents: Math.max(1, Math.min(16, Math.round(limits.concurrent) || 6)),
        max_subagents_per_turn: Math.max(1, Math.min(32, Math.round(limits.perTurn) || 10)),
      });
      notify("已保存：子代理并发上限即时生效");
      load();
    } catch (e) { notify("保存失败: " + String(e)); } finally { setLimitsSaving(false); }
  };

  // 档位候选：优先所选模型支持的档位，未选模型时给全档位
  const selModel = cfg.modelId ? models.find((x) => x.id === cfg.modelId) : null;
  const effortOptions = selModel?.reasoning_efforts?.length ? selModel.reasoning_efforts : REASONING_OPTS;

  return (
    <>
      <Section title="子代理">
        <CardRow
          title="启用子代理与子会话"
          desc="关闭后主代理不再派发任何子代理 / 子会话，全部工作在当前会话内完成"
        >
          <Sw checked={cfg.enabled} onChange={(v) => void patch({ enabled: v })} />
        </CardRow>
        <CardRow
          className={cfg.enabled ? undefined : "settings-row-disabled"}
          title="默认模型"
          desc="子代理与子会话使用的模型；留空则跟随主代理"
        >
          <ModelPicker
            variant="field"
            models={models}
            value={cfg.modelId || null}
            onChange={(id) => void patch({ modelId: Number(id) })}
            open={modelOpen}
            onToggle={() => setModelOpen((v) => !v)}
            inheritLabel="跟随主代理"
            onInherit={() => void patch({ modelId: 0 })}
            side="bottom"
          />
        </CardRow>
        <CardRow
          className={cfg.enabled ? undefined : "settings-row-disabled"}
          title="思考深度"
          desc="子代理与子会话的推理档位；留空则跟随会话当前档位"
        >
          <div className="settings-chips">
            <button
              type="button"
              className={"settings-chip" + (cfg.reasoningEffort === "" ? " on" : "")}
              onClick={() => void patch({ reasoningEffort: "" })}
            >跟随会话</button>
            {effortOptions.map((eff) => (
              <button
                key={eff}
                type="button"
                className={"settings-chip" + (cfg.reasoningEffort === eff ? " on" : "")}
                onClick={() => void patch({ reasoningEffort: eff })}
              >{eff}</button>
            ))}
          </div>
        </CardRow>
      </Section>

      {/* plan-20261018-operator-boost：界面操作交给低思考档的操控子会话，主会话保留高思考深度 */}
      <Section title="操控子会话">
        <CardRow
          className={cfg.enabled ? undefined : "settings-row-disabled"}
          title="界面操作交给操控子会话"
          desc="内置浏览器自测、导出文件、电脑操控等多步界面操作交给子会话以低思考深度执行，响应更快；主会话继续用当前思考深度处理其它工作。仅智能体模式生效"
        >
          <Sw checked={op.enabled} onChange={(v) => void patchOperator({ enabled: v })} />
        </CardRow>
        <CardRow
          className={cfg.enabled && op.enabled ? undefined : "settings-row-disabled"}
          title="操控子会话思考深度"
          desc="默认「最低档」：使用当前模型支持的最低档位（不关闭思考）"
        >
          <div className="settings-chips">
            {OPERATOR_EFFORT_OPTS.map((o) => (
              <button
                key={o.value}
                type="button"
                className={"settings-chip" + (op.effort === o.value ? " on" : "")}
                onClick={() => void patchOperator({ effort: o.value })}
              >{o.label}</button>
            ))}
          </div>
        </CardRow>
      </Section>

      {/* S10（plan-41-197）：并发与上限 = 摘要行 + 配置弹窗；plan-114-450：独立成组，不与列表混在同一张卡片里 */}
      <Section title="运行限制">
        <CardRow title="并发与上限" desc={`同时运行上限 ${limits.concurrent} · 每轮派发上限 ${limits.perTurn}`}>
          <Button variant="secondary" size="sm" onClick={() => setLimitsOpen(true)}>配置</Button>
        </CardRow>
      </Section>

      {/* S10（plan-41-197）：并发与上限配置弹窗（保存后即时生效） */}
      <Modal
        open={limitsOpen}
        onClose={() => setLimitsOpen(false)}
        title="并发与上限"
        subtitle="超出同时运行上限时新派发排队等待；每轮总量超限直接拒绝"
        width={520}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setLimitsOpen(false)}>取消</button>
            <button className="btn btn-primary btn-sm" disabled={limitsSaving} aria-busy={limitsSaving}
              onClick={async () => { await saveLimits(); setLimitsOpen(false); }}>
              {limitsSaving ? "保存中…" : "保存"}
            </button>
          </>
        }
      >
        <div className="settings-modal-form" style={{ padding: 18 }}>
          <div className="settings-modal-form-row">
            <label>同时运行的子代理上限</label>
            <Input type="number" min={1} max={16} value={String(limits.concurrent)}
              onChange={(e) => setLimits((p) => ({ ...p, concurrent: Number(e.target.value) || 1 }))} />
            <div className="ui-field-hint">达到上限后新派发排队等待（1~16）</div>
          </div>
          <div className="settings-modal-form-row">
            <label>每轮派发总量上限</label>
            <Input type="number" min={1} max={32} value={String(limits.perTurn)}
              onChange={(e) => setLimits((p) => ({ ...p, perTurn: Number(e.target.value) || 1 }))} />
            <div className="ui-field-hint">单个回合内最多派发数量，含已完成（1~32）</div>
          </div>
        </div>
      </Modal>
    </>
  );
}
