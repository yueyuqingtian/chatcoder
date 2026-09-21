/** 设置中心：外观（v2.2 对齐 zcode 3.18）。
 * 主题模式、毛玻璃效果、布局宽度、字号、左侧面板外观。 */
import { useEffect, useState } from "react";
import { useThemeStore, type Theme } from "../../store/theme";
import { useUiStore, type MotionLevel } from "../../store/ui";
import {
  fetchGlassDiagnostics, setGlassSelfCheck,
  GLASS_VERDICT_LABEL, GLASS_VERDICT_TONE, type GlassDiagnostics,
} from "../../utils/glassProbe";
import { Slider } from "../ui";
import { Row, Sw } from "./shared";

const THEMES: Record<Theme, string> = { light: "浅色", dark: "深色" };

/** plan-308-1542 需求4：系统级模糊后端的中文说明（设置页据此告知用户） */
const GLASS_BACKEND_LABEL: Record<string, string> = {
  "dwm-acrylic": "Win11 DWM Acrylic",
  "win32-accent": "Win32 ACCENT 系统模糊",
  vibrancy: "macOS Vibrancy",
  none: "无（不支持系统级模糊）",
};

/** plan-248-1258 M5: 动画效果三档（低配机可减弱以提升流畅度） */
const MOTION_LEVELS: Record<MotionLevel, { label: string; desc: string }> = {
  full: { label: "标准", desc: "完整动效" },
  reduced: { label: "减弱", desc: "缩短时长、禁用循环动画" },
  off: { label: "关闭", desc: "瞬时呈现" },
};

/** plan-308-1555 M9：玻璃强度三档（0 轻柔 / 1 标准 / 2 深邃）——不透明度由 ui.ts 计算 */
const GLASS_STRENGTHS: Array<{ value: number; label: string; desc: string }> = [
  { value: 0, label: "轻柔", desc: "面板更不透明，文字最清晰" },
  { value: 1, label: "标准", desc: "默认：可读性与透出感平衡" },
  { value: 2, label: "深邃", desc: "透出桌面最明显（深色壁纸下更通透）" },
];

export function AppearancePanel() {
  const { theme, setTheme } = useThemeStore();
  const ui = useUiStore();
  // plan-308-1542 需求4：查询系统级模糊后端能力（窗口初始化时探测，运行期不变）
  const [glassBackend, setGlassBackend] = useState<string>("");
  const [glassCapability, setGlassCapability] = useState<{ backend: string; supported: boolean; reason?: string } | null>(null);
  // plan-308-1555 M9：诊断结果（来自主进程：DWM 回读值 + 渲染层 alpha 链路）
  const [diag, setDiag] = useState<GlassDiagnostics | null>(null);
  const [diagBusy, setDiagBusy] = useState(false);
  const [selfCheck, setSelfCheck] = useState(false);
  useEffect(() => {
    const api = window.chatcoderAPI;
    if (!api?.glassCapability) return;
    let alive = true;
    void api.glassCapability().then((cap) => {
      if (!alive || !cap) return;
      setGlassCapability(cap);
      setGlassBackend(cap.backend || "none");
    }).catch(() => { /* 探测失败：不阻断设置页 */ });
    return () => { alive = false; };
  }, []);

  /** 运行一次玻璃诊断（主进程回读 DWM + 注入探针采样渲染层 alpha 链路）。 */
  const runDiagnostics = async () => {
    setDiagBusy(true);
    try {
      const res = await fetchGlassDiagnostics();
      setDiag(res);
    } finally {
      setDiagBusy(false);
    }
  };

  /** 切换自检模式：临时把面板 alpha 压到极低 + 高饱和描边，一眼判定桌面是否混入。 */
  const toggleSelfCheck = async (on: boolean) => {
    const ok = await setGlassSelfCheck(on);
    if (ok) setSelfCheck(on);
  };
  return (
    <div className="settings-card-stack">
      <div className="settings-card">
        <Row title="主题模式" desc="浅色 / 深色">
          <div style={{ display: "flex", gap: 6 }}>
            {(Object.keys(THEMES) as Theme[]).map((t) => (
              <button
                key={t}
                className={"settings-pill" + (theme === t ? " active" : "")}
                onClick={() => setTheme(t)}
              >
                {THEMES[t]}
              </button>
            ))}
          </div>
        </Row>
        <Row
          title="毛玻璃效果"
          desc={
            ui.glassmorphism
              ? `液态玻璃：面板微透出桌面（系统级模糊：${GLASS_BACKEND_LABEL[glassBackend] ?? glassBackend}）`
              : "启用窗口与侧边栏半透明磨砂背景"
          }
        >
          <Sw checked={ui.glassmorphism} onChange={(v) => ui.setPrefs({ glassmorphism: v })} />
        </Row>
        {/* plan-308-1542 需求4：液态玻璃风格 + 系统能力提示。
            用户反馈"仅靠样式做不到透出软件"——这里明确告知是否具备系统级模糊后端，
            不具备时不谎报效果（样式侧同时置 data-glass-degraded 降级为纯色）。 */}
        {ui.glassmorphism && (
          <Row title="液态玻璃" desc="面板边缘折射 + 高光斜面 + 内描边（关闭则仅系统模糊，无液态质感）">
            <Sw
              checked={(ui.glassStyle || "liquid") === "liquid"}
              onChange={(v) => ui.setPrefs({ glassStyle: v ? "liquid" : "solid" })}
            />
          </Row>
        )}
        {/* plan-308-1555 M9：强度三档——控制面板不透明度（越深邃越透出桌面） */}
        {ui.glassmorphism && (
          <Row title="玻璃强度" desc="右面板/侧栏的不透明度。深色壁纸下若看不出来，请选「深邃」">
            <div style={{ display: "flex", gap: 6 }}>
              {GLASS_STRENGTHS.map((s) => (
                <button
                  key={s.value}
                  className={"settings-pill" + ((ui.glassStrength ?? 1) === s.value ? " active" : "")}
                  title={s.desc}
                  onClick={() => ui.setPrefs({ glassStrength: s.value })}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </Row>
        )}
        {ui.glassmorphism && glassBackend && glassBackend !== "none" && (
          <Row title="系统模糊后端" desc={`当前由 ${GLASS_BACKEND_LABEL[glassBackend] ?? glassBackend} 提供真实透出桌面`}>
            <span className="settings-badge-ok">已启用</span>
          </Row>
        )}
        {ui.glassmorphism && glassBackend === "none" && (
          <Row
            title="系统模糊不可用"
            desc={`当前系统无法提供系统级模糊（${glassCapability?.reason || "需要 Win11，或 Win10 安装 koffi 模块"}）；已降级为半透明纯色（不会透出桌面）`}
          >
            <span className="settings-badge-warn">已降级</span>
          </Row>
        )}

        {/* plan-308-1555 M9：玻璃诊断——正面解决"改了看不出、到底哪儿不对"的历史困局。
            这里展示**系统侧回读值**（而非 API 返回值），并给出可执行的判定结论。 */}
        {ui.glassmorphism && (
          <Row
            title="玻璃诊断"
            desc="回读系统实际生效的材质 + 检查透明度链路，给出「该修哪里」的结论"
          >
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <button className="btn btn-ghost btn-sm" onClick={() => void runDiagnostics()} disabled={diagBusy}>
                {diagBusy ? "检测中…" : "运行诊断"}
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => void toggleSelfCheck(!selfCheck)}
                title="把面板调成半透明+高亮描边，一眼看出桌面有没有混进来"
              >
                {selfCheck ? "退出自检" : "自检模式"}
              </button>
            </div>
          </Row>
        )}
        {ui.glassmorphism && diag && (
          <div className="glass-diag">
            <div className="glass-diag-head">
              <span className={`settings-badge-${GLASS_VERDICT_TONE[diag.conclusion.verdict] === "ok" ? "ok" : "warn"}`}>
                {GLASS_VERDICT_LABEL[diag.conclusion.verdict]}
              </span>
              <span className="glass-diag-line">{diag.line}</span>
            </div>
            <div className="glass-diag-reason">{diag.conclusion.reason}</div>
            {selfCheck && (
              <div className="glass-diag-reason">
                自检模式已开启：面板已调至极低不透明度。若能明显看到壁纸颜色 ⇒ 材质与链路正常（此前只是对比度不足）；
                若毫无变化 ⇒ 材质未生效或被遮挡。
              </div>
            )}
          </div>
        )}
        <Row title="动画效果" desc="低配置设备可选择「减弱」或「关闭」以提升流畅度">
          <div style={{ display: "flex", gap: 6 }}>
            {(Object.keys(MOTION_LEVELS) as MotionLevel[]).map((m) => (
              <button
                key={m}
                className={"settings-pill" + ((ui.motionLevel || "full") === m ? " active" : "")}
                title={MOTION_LEVELS[m].desc}
                onClick={() => ui.setPrefs({ motionLevel: m })}
              >
                {MOTION_LEVELS[m].label}
              </button>
            ))}
          </div>
        </Row>
      </div>

      <div className="settings-card">
        <Row title="左侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <Slider min={200} max={480} step={4} value={ui.leftPanelWidth}
            onChange={(v) => ui.setPrefs({ leftPanelWidth: v })} format={(v) => `${v}px`} aria-label="左侧面板宽度" />
        </Row>
        <Row title="右侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <Slider min={200} max={1200} step={10} value={ui.rightPanelWidth}
            onChange={(v) => ui.setPrefs({ rightPanelWidth: v })} format={(v) => `${v}px`} aria-label="右侧面板宽度" />
        </Row>
        <Row title="对话字号" desc="控制对话消息的文字大小（立即生效）">
          <Slider min={11} max={18} step={1} value={ui.chatFontSize}
            onChange={(v) => ui.setPrefs({ chatFontSize: v })} format={(v) => `${v}px`} aria-label="对话字号" />
        </Row>
        <Row title="消息行距" desc="控制对话消息的行间间距倍率（立即生效）">
          <Slider min={1.2} max={2.2} step={0.05} value={ui.chatLineHeight}
            onChange={(v) => ui.setPrefs({ chatLineHeight: v })} format={(v) => v.toFixed(2)} aria-label="消息行距" />
        </Row>
        <Row title="内容展示宽度" desc="0 表示不限制，填满可视区域">
          <Slider min={0} max={1200} step={50} value={ui.contentMaxWidth}
            onChange={(v) => ui.setPrefs({ contentMaxWidth: v })}
            format={(v) => (v === 0 ? "不限" : `${v}px`)} aria-label="内容展示宽度" />
        </Row>
      </div>

      <div className="settings-card">
        <div className="settings-card-title">左侧面板外观</div>
        <Row title="文字大小" desc="左侧面板会话与导航文字大小">
          <Slider min={11} max={16} step={1} value={ui.sidebarFontSize}
            onChange={(v) => ui.setPrefs({ sidebarFontSize: v })} format={(v) => `${v}px`} aria-label="侧栏文字大小" />
        </Row>
        <Row title="图标大小" desc="左侧面板图标尺寸">
          <Slider min={12} max={20} step={1} value={ui.sidebarIconSize}
            onChange={(v) => ui.setPrefs({ sidebarIconSize: v })} format={(v) => `${v}px`} aria-label="侧栏图标大小" />
        </Row>
      </div>
    </div>
  );
}
