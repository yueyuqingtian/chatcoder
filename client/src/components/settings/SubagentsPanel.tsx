/** 设置中心：子代理与子会话（plan-474-2105 并发治理）。
 *
 * 【为什么分成两组】
 * 子代理（只读探索 / 后台调研）与子会话（可独立改文件的任务单元）是两种互相独立的并发资源：
 * 同一条主会话里可能同时跑着多个子代理，也可能同时跑着多个子会话。两者的占用不叠加，
 * 因此上限各自独立配置、分组展示，避免用户把两个数字混着理解。
 *
 * 【为什么开关也分开】（plan-20261020-subsession-switch）
 * 此前一个「启用子代理与子会话」开关同时管住两套能力，用户想只留其中一种（例如保留只读
 * 探索子代理、关掉可改文件的子会话）时无从选择。现在两个开关各自独立、默认都开启；
 * 存量配置升级时子会话跟随旧合并开关的取值，显式关闭过的不会被静默打开。
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
import { api, type GlobalSettingsIn, type ModelOut } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { CardRow, Input, Section } from "../ui";
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

/** 并发上限区间与默认值（与后端字段约束一致：1~30，默认 4） */
const LIMIT_MIN = 1;
const LIMIT_MAX = 30;
const LIMIT_DEFAULT = 4;
/** 输入类设置项统一 300ms 防抖（AGENTS.md 第 2 节） */
const LIMIT_SAVE_DELAY = 300;

type LimitKey = "subagents" | "subsessions";

/** 两个上限各自对应的后端字段（字段名已冻结，不要改动） */
const LIMIT_FIELD: Record<LimitKey, "max_concurrent_subagents" | "max_subsessions_per_parent"> = {
  subagents: "max_concurrent_subagents",
  subsessions: "max_subsessions_per_parent",
};

/** 非阻塞提示：Electron 中 window.alert 是原生模态框，关闭后会破坏窗口焦点，统一改用全局提示条。 */
function notify(msg: string) {
  useChatStore.setState({ error: msg });
}

/** 输入框文本 → 合法上限；后端同样会夹紧，这里先夹一次避免发送明显越界的值 */
function clampLimit(n: number): number {
  if (!Number.isFinite(n)) return LIMIT_MIN;
  return Math.max(LIMIT_MIN, Math.min(LIMIT_MAX, Math.round(n)));
}

/** 组装单字段 patch（分开写是为了让类型收敛到具体字段，避免索引签名不匹配） */
function limitPatch(key: LimitKey, value: number): Partial<GlobalSettingsIn> {
  return key === "subagents"
    ? { max_concurrent_subagents: value }
    : { max_subsessions_per_parent: value };
}

export function SubagentsPanel() {
  const [models, setModels] = useState<ModelOut[]>([]);
  /** 全局设置：子代理 / 子会话两个独立开关 + 两者共用的模型与思考深度 */
  const [cfg, setCfg] = useState({
    enabled: true,
    subsessionEnabled: true,
    modelId: 0,            // 0 = 跟随主代理
    reasoningEffort: "",   // "" = 跟随会话档位
  });
  const [modelOpen, setModelOpen] = useState(false);
  // plan-20261018-operator-boost：GUI 操作交给操控子会话 / 其思考深度
  const [op, setOp] = useState({ enabled: true, effort: "lowest" });
  // plan-474-2105：两个并发上限，各自独立保存
  const [limits, setLimits] = useState({ subagents: LIMIT_DEFAULT, subsessions: LIMIT_DEFAULT });
  /** 输入框显示文本单独保存：允许「清空后重新输入」的中间态不被夹紧值打断 */
  const [limitText, setLimitText] = useState({
    subagents: String(LIMIT_DEFAULT),
    subsessions: String(LIMIT_DEFAULT),
  });
  const limitsRef = useRef(limits);
  limitsRef.current = limits;
  const limitTextRef = useRef(limitText);
  limitTextRef.current = limitText;
  /** 每个上限一个防抖定时器 + 待发值 */
  const limitTimer = useRef<Partial<Record<LimitKey, ReturnType<typeof setTimeout>>>>({});
  const limitPending = useRef<Partial<Record<LimitKey, number>>>({});
  /** 保存失败时的回滚基准（本次输入开始前的服务端值） */
  const limitPrev = useRef<Record<LimitKey, number>>({
    subagents: LIMIT_DEFAULT,
    subsessions: LIMIT_DEFAULT,
  });

  const load = useCallback(async () => {
    try { setModels(await api.listModels()); } catch { /* 模型列表失败不阻塞其它项 */ }
    try {
      const g = await api.getGlobalSettings();
      setCfg({
        enabled: g.subagent_enabled !== false,
        subsessionEnabled: g.subsession_enabled !== false,
        modelId: typeof g.subagent_model_id === "number" ? g.subagent_model_id : 0,
        reasoningEffort: g.subagent_reasoning_effort || "",
      });
      setOp({
        enabled: g.operator_subsession_enabled !== false,
        effort: g.operator_reasoning_effort || "lowest",
      });
      const next = {
        subagents: typeof g.max_concurrent_subagents === "number"
          ? clampLimit(g.max_concurrent_subagents)
          : LIMIT_DEFAULT,
        subsessions: typeof g.max_subsessions_per_parent === "number"
          ? clampLimit(g.max_subsessions_per_parent)
          : LIMIT_DEFAULT,
      };
      setLimits(next);
      setLimitText({ subagents: String(next.subagents), subsessions: String(next.subsessions) });
      limitPrev.current = next;
    } catch { /* 拉取失败沿用默认值 */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  /** 修改即保存（与设置页其它面板一致）：单项立即落盘，失败回滚并提示 */
  const patch = async (partial: Partial<{
    enabled: boolean; subsessionEnabled: boolean; modelId: number; reasoningEffort: string;
  }>) => {
    const prev = cfg;
    const next = { ...cfg, ...partial };
    setCfg(next);
    try {
      await api.setGlobalSettings({
        subagent_enabled: next.enabled,
        subsession_enabled: next.subsessionEnabled,
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

  /** 落盘单个上限：成功后用后端返回值回填（防止「显示 99 实际 30」），失败回滚到本次输入前的值 */
  const saveLimit = useCallback(async (key: LimitKey, value: number) => {
    const field = LIMIT_FIELD[key];
    const sentText = limitTextRef.current[key];
    try {
      const r = await api.setGlobalSettings(limitPatch(key, value));
      const saved = typeof r[field] === "number" ? r[field] : value;
      setLimits((p) => ({ ...p, [key]: saved }));
      // 用户在等待响应期间又改了输入时不覆盖他正在输入的内容
      if (limitTextRef.current[key] === sentText) {
        setLimitText((p) => ({ ...p, [key]: String(saved) }));
      }
      limitPrev.current[key] = saved;
    } catch (e) {
      const rollback = limitPrev.current[key];
      setLimits((p) => ({ ...p, [key]: rollback }));
      setLimitText((p) => ({ ...p, [key]: String(rollback) }));
      notify("保存失败：" + String(e));
    }
  }, []);

  /** 输入变化：文本即时回显、数值乐观更新，300ms 后落盘 */
  const changeLimit = (key: LimitKey, raw: string) => {
    setLimitText((p) => ({ ...p, [key]: raw }));
    const value = clampLimit(Number(raw));
    setLimits((p) => ({ ...p, [key]: value }));

    // 连续输入时回滚基准保持为「本次输入开始前」的值
    if (limitTimer.current[key] == null) limitPrev.current[key] = limitsRef.current[key];
    if (limitTimer.current[key] != null) clearTimeout(limitTimer.current[key]);

    // 清空输入框不提交（等用户输入完整数字），失焦时回填当前生效值
    if (raw.trim() === "") {
      delete limitTimer.current[key];
      delete limitPending.current[key];
      return;
    }

    limitPending.current[key] = value;
    limitTimer.current[key] = setTimeout(() => {
      delete limitTimer.current[key];
      delete limitPending.current[key];
      void saveLimit(key, value);
    }, LIMIT_SAVE_DELAY);
  };

  /** 失焦回填：空值或非法文本还原为当前生效值，不留下「空着的上限」 */
  const resetLimitText = (key: LimitKey) => {
    if (limitTimer.current[key] != null) return; // 还有待发保存，说明用户刚输入，别打断
    const v = String(limitsRef.current[key]);
    setLimitText((p) => (p[key] === v ? p : { ...p, [key]: v }));
  };

  useEffect(() => () => {
    // 卸载时清理定时器；尚未发出的修改立即补发一次，避免「改完就切页」丢失
    (Object.keys(limitTimer.current) as LimitKey[]).forEach((key) => {
      const timer = limitTimer.current[key];
      if (timer != null) clearTimeout(timer);
      const value = limitPending.current[key];
      if (value == null) return;
      void api.setGlobalSettings(limitPatch(key, value)).catch(() => {
        /* 卸载后无法提示，下次进入页面以服务端值为准 */
      });
    });
    limitTimer.current = {};
    limitPending.current = {};
  }, []);

  // 档位候选：优先所选模型支持的档位，未选模型时给全档位
  const selModel = cfg.modelId ? models.find((x) => x.id === cfg.modelId) : null;
  const effortOptions = selModel?.reasoning_efforts?.length ? selModel.reasoning_efforts : REASONING_OPTS;
  /** 默认模型与思考深度是两种能力共用的：只要还有一种开启就不置灰 */
  const sharedEnabled = cfg.enabled || cfg.subsessionEnabled;

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
          className={sharedEnabled ? undefined : "settings-row-disabled"}
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
          className={sharedEnabled ? undefined : "settings-row-disabled"}
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
            value={limitText.subagents}
            onChange={(e) => changeLimit("subagents", e.target.value)}
            onBlur={() => resetLimitText("subagents")}
          />
        </CardRow>
      </Section>

      <Section title="子会话">
        <CardRow
          title="启用子会话"
          desc="关闭后主代理不再派发子会话，并行任务与界面操作都回到当前会话内完成"
        >
          <Sw
            checked={cfg.subsessionEnabled}
            onChange={(v) => void patch({ subsessionEnabled: v })}
          />
        </CardRow>
        <CardRow
          className={cfg.subsessionEnabled ? undefined : "settings-row-disabled"}
          title="单会话同时运行上限"
          desc="一个会话里最多同时运行几个子会话。达到上限后再创建会被拒绝，并提示当前运行数与上限——等一个子会话结束即可继续创建。"
        >
          <Input
            type="number"
            min={LIMIT_MIN}
            max={LIMIT_MAX}
            aria-label="单会话子会话并发上限"
            value={limitText.subsessions}
            onChange={(e) => changeLimit("subsessions", e.target.value)}
            onBlur={() => resetLimitText("subsessions")}
          />
        </CardRow>
      </Section>

      {/* plan-20261018-operator-boost：界面操作交给低思考档的操控子会话，主会话保留高思考深度 */}
      <Section title="操控子会话">
        <CardRow
          className={cfg.subsessionEnabled ? undefined : "settings-row-disabled"}
          title="界面操作交给操控子会话"
          desc="内置浏览器自测、导出文件、电脑操控等多步界面操作交给子会话以低思考深度执行，响应更快；主会话继续用当前思考深度处理其它工作。仅智能体模式生效"
        >
          <Sw checked={op.enabled} onChange={(v) => void patchOperator({ enabled: v })} />
        </CardRow>
        <CardRow
          className={cfg.subsessionEnabled && op.enabled ? undefined : "settings-row-disabled"}
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
    </>
  );
}
