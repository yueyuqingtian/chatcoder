/** 设置中心：子代理（plan-474-2105 并发治理 · plan-549-2281 收敛）。
 *
 * 【为什么只剩子代理】
 * 此前这里还管着「子会话」的开关与并发上限，并行执行有子代理与子会话两套资源。
 * plan-549-2281 起并行统一走子代理（工具 `spawn_subagent`），子会话的创建与控制下线，
 * 因此相关开关与上限一并移除——**已存在的历史子会话仍然照常展示与打开**，
 * 只是不再产生新的子会话。sessions.side_parent_id / side_kind 字段保留不动。
 *
 * 【为什么没有「每轮派发总量上限」】
 * 单轮总量既挡不住瞬时资源占用（并发才是真正的瓶颈），又会让长时间任务中途无法再派发。
 * plan-474-2105 起只按并发限流，单轮总量不再设上限。
 *
 * 【为什么去掉弹窗与保存按钮】
 * 项目约定设置页「修改即保存」：并发上限改动即时生效，不存在「改了没点保存就切页」的丢失。
 *
 * 历史数据（subagent_profiles 表）保留不删，仅作为模型/思考深度的兜底来源，
 * 回滚到旧版本时用户配置不会丢失。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ModelOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { CardRow, Input, Section } from "../ui";
import { Sw } from "./shared";
import { ModelPicker } from "../chat/ModelPicker";

/** plan-330-1648 M2: 思考深度档位候选（与 ModelsPanel REASONING_OPTS 同口径） */
const REASONING_OPTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
/** plan-20261018-operator-boost：操控子代理思考深度——操作类工作只需低档，候选只给低档位 */
const OPERATOR_EFFORT_OPTS: Array<{ value: string; label: string }> = [
  { value: "lowest", label: "最低档" },
  { value: "none", label: "none" },
  { value: "minimal", label: "minimal" },
  { value: "low", label: "low" },
  { value: "medium", label: "medium" },
  { value: "follow", label: "跟随子代理" },
];

/** 并发上限区间与默认值（与后端字段约束一致：1~30，默认 4） */
const LIMIT_MIN = 1;
const LIMIT_MAX = 30;
const LIMIT_DEFAULT = 4;
/** 输入类设置项统一 300ms 防抖（AGENTS.md 第 2 节） */
const LIMIT_SAVE_DELAY = 300;

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点，统一改用全局提示条。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

/** 输入框文本 → 合法上限；后端同样会夹紧，这里先夹一次避免发送明显越界的值 */
function clampLimit(n: number): number {
  if (!Number.isFinite(n)) return LIMIT_MIN;
  return Math.max(LIMIT_MIN, Math.min(LIMIT_MAX, Math.round(n)));
}

export function SubagentsPanel() {
  const [models, setModels] = useState<ModelOut[]>([]);
  /** 全局设置：子代理开关 + 模型与思考深度 */
  const [cfg, setCfg] = useState({
    enabled: true,
    modelId: 0,            // 0 = 跟随主代理
    reasoningEffort: "",   // "" = 跟随会话档位
  });
  const [modelOpen, setModelOpen] = useState(false);
  // plan-549-2281：GUI 操作交给操控子代理 / 其思考深度（沿用原操控开关语义）
  const [op, setOp] = useState({ enabled: true, effort: "lowest" });
  /** 单会话子代理并发上限 */
  const [limit, setLimit] = useState(LIMIT_DEFAULT);
  /** 输入框显示文本单独保存：允许「清空后重新输入」的中间态不被夹紧值打断 */
  const [limitText, setLimitText] = useState(String(LIMIT_DEFAULT));
  const limitRef = useRef(limit);
  limitRef.current = limit;
  // plan-560-2311 E3：patch / patchOperator 改为从 ref 读最新状态合成 payload。
  // 此前直接捕获渲染闭包里的 cfg / op，连续快速点击（未及重渲染）会用旧值互相覆盖。
  const cfgRef = useRef(cfg);
  cfgRef.current = cfg;
  const opRef = useRef(op);
  opRef.current = op;
  const limitTextRef = useRef(limitText);
  limitTextRef.current = limitText;
  /** 防抖定时器 + 待发值 */
  const limitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const limitPending = useRef<number | null>(null);
  /** 保存失败时的回滚基准（本次输入开始前的服务端值） */
  const limitPrev = useRef<number>(LIMIT_DEFAULT);

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
        enabled: g.operator_subagent_enabled !== false,
        effort: g.operator_subagent_effort || "lowest",
      });
      const next = typeof g.max_concurrent_subagents === "number"
        ? clampLimit(g.max_concurrent_subagents)
        : LIMIT_DEFAULT;
      setLimit(next);
      setLimitText(String(next));
      limitPrev.current = next;
    } catch { /* 拉取失败沿用默认值 */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  /** 修改即保存（与设置页其它面板一致）：单项立即落盘，失败回滚并提示 */
  const patch = async (partial: Partial<{ enabled: boolean; modelId: number; reasoningEffort: string }>) => {
    const prev = cfgRef.current;
    const next = { ...prev, ...partial };
    cfgRef.current = next;  // 同帧内的后续调用立即看到本次修改，不被旧闭包覆盖
    setCfg(next);
    try {
      await api.setGlobalSettings({
        subagent_enabled: next.enabled,
        subagent_model_id: next.modelId,
        subagent_reasoning_effort: next.reasoningEffort,
      });
    } catch (e) {
      cfgRef.current = prev;
      setCfg(prev);
      notify("保存失败：" + String(e));
    }
  };

  const patchOperator = async (partial: Partial<{ enabled: boolean; effort: string }>) => {
    const prev = opRef.current;
    const next = { ...prev, ...partial };
    opRef.current = next;  // 同上：连续点击按最新状态合成
    setOp(next);
    try {
      await api.setGlobalSettings({
        operator_subagent_enabled: next.enabled,
        operator_subagent_effort: next.effort,
      });
    } catch (e) {
      opRef.current = prev;
      setOp(prev);
      notify("保存失败：" + String(e));
    }
  };

  /** 落盘上限：成功后用后端返回值回填（防止「显示 99 实际 30」），失败回滚到本次输入前的值 */
  const saveLimit = useCallback(async (value: number) => {
    const sentText = limitTextRef.current;
    try {
      const r = await api.setGlobalSettings({ max_concurrent_subagents: value });
      const saved = typeof r.max_concurrent_subagents === "number" ? r.max_concurrent_subagents : value;
      setLimit(saved);
      // 用户在等待响应期间又改了输入时不覆盖他正在输入的内容
      if (limitTextRef.current === sentText) setLimitText(String(saved));
      limitPrev.current = saved;
    } catch (e) {
      const rollback = limitPrev.current;
      setLimit(rollback);
      setLimitText(String(rollback));
      notify("保存失败：" + String(e));
    }
  }, []);

  /** 输入变化：文本即时回显、数值乐观更新，300ms 后落盘 */
  const changeLimit = (raw: string) => {
    setLimitText(raw);

    // 清空输入框不提交（等用户输入完整数字），失焦时回填当前生效值。
    // plan-560-2311 E3：这段必须早于下面的 setLimit——Number("") 是 0，clampLimit 会把它
    // 夹成 LIMIT_MIN 并写进 limitRef.current，于是失焦回填显示 1、后端却仍是原值。
    if (raw.trim() === "") {
      if (limitTimer.current != null) clearTimeout(limitTimer.current);
      limitTimer.current = null;
      limitPending.current = null;
      return;
    }

    const value = clampLimit(Number(raw));
    setLimit(value);

    // 连续输入时回滚基准保持为「本次输入开始前」的值（limitRef 只在渲染时更新，此刻仍是旧值）
    if (limitTimer.current == null) limitPrev.current = limitRef.current;
    if (limitTimer.current != null) clearTimeout(limitTimer.current);

    limitPending.current = value;
    limitTimer.current = setTimeout(() => {
      limitTimer.current = null;
      limitPending.current = null;
      void saveLimit(value);
    }, LIMIT_SAVE_DELAY);
  };

  /** 失焦回填：空值或非法文本还原为当前生效值，不留下「空着的上限」 */
  const resetLimitText = () => {
    if (limitTimer.current != null) return; // 还有待发保存，说明用户刚输入，别打断
    // plan-560-2311 E3：回填「后端实际生效值」而不是乐观显示值——limitPrev.current 是最近一次
    // 已知的服务端值（load 时写入、保存成功后更新为返回值、失败时作为回滚基准），
    // 而 limitRef.current 可能带着尚未落盘的乐观夹紧值。
    const v = String(limitPrev.current);
    setLimitText((p) => (p === v ? p : v));
  };

  useEffect(() => () => {
    // 卸载时清理定时器；尚未发出的修改立即补发一次，避免「改完就切页」丢失
    if (limitTimer.current != null) clearTimeout(limitTimer.current);
    const value = limitPending.current;
    limitTimer.current = null;
    limitPending.current = null;
    if (value == null) return;
    void api.setGlobalSettings({ max_concurrent_subagents: value }).catch(() => {
      /* 卸载后无法提示，下次进入页面以服务端值为准 */
    });
  }, []);

  // 档位候选：优先所选模型支持的档位，未选模型时给全档位
  const selModel = cfg.modelId ? models.find((x) => x.id === cfg.modelId) : null;
  const effortOptions = selModel?.reasoning_efforts?.length ? selModel.reasoning_efforts : REASONING_OPTS;

  return (
    <>
      <Section title="子代理">
        <CardRow
          title="启用子代理"
          desc="关闭后主代理不再派发子代理（代码探索、并行调研等），相关工具不再下发给模型"
        >
          <Sw checked={cfg.enabled} onChange={(v) => void patch({ enabled: v })} />
        </CardRow>
        <CardRow
          className={cfg.enabled ? undefined : "settings-row-disabled"}
          title="默认模型"
          desc="子代理使用的模型；留空则跟随主代理"
        >
          <ModelPicker
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
          desc="子代理的推理档位；留空则跟随会话当前档位"
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
        <CardRow
          className={cfg.enabled ? undefined : "settings-row-disabled"}
          title="单会话同时运行上限"
          desc="一个会话里最多同时运行几个子代理。达到上限后新的派发会被直接拒绝，并把当前运行数与上限告知主代理——等有子代理结束再派发即可，避免一次任务把本机资源占满。"
        >
          <Input
            type="number"
            min={LIMIT_MIN}
            max={LIMIT_MAX}
            aria-label="单会话子代理并发上限"
            value={limitText}
            onChange={(e) => changeLimit(e.target.value)}
            onBlur={resetLimitText}
          />
        </CardRow>
      </Section>

      {/* plan-549-2281：界面操作交给低思考档的操控子代理，主会话保留高思考深度 */}
      <Section title="操控子代理">
        <CardRow
          className={cfg.enabled ? undefined : "settings-row-disabled"}
          title="界面操作交给操控子代理"
          desc="内置浏览器自测、导出文件、电脑操控等多步界面操作交给子代理以低思考深度执行，响应更快；主会话继续用当前思考深度处理其它工作。仅智能体模式生效"
        >
          <Sw checked={op.enabled} onChange={(v) => void patchOperator({ enabled: v })} />
        </CardRow>
        <CardRow
          className={cfg.enabled && op.enabled ? undefined : "settings-row-disabled"}
          title="操控子代理思考深度"
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
    </>
  );
}
