import ReactDOM from "react-dom/client";
import App from "./App";
import { initTheme } from "./store/theme";
import { initUi, useUiStore } from "./store/ui";
import { registerBuiltinPlugins } from "./plugins/builtin";
import { loadExternalPlugins } from "./plugins/external";
import { install as installPerfMetrics } from "./perf/metrics";
import "./styles/fonts.css";
import "./styles/global.css";
// plan-282-1416: 组件库样式层（依赖 tokens.css，须在 global.css 之后以便覆盖旧兼容类）
import "./styles/components.css";
import "./styles/workflow.css";
// plan-20261009-ui：本轮重做的新类名样式层（只承载新类名，避免与 global.css 旧规则互相覆盖），
// 必须在 components 之后、motion/focus 之前加载——动效降级与焦点规则仍需最后生效。
import "./styles/overlay.css";
import "./styles/icons.css";
import "./styles/composer.css";
import "./styles/home.css";
import "./styles/panel.css";
// plan-20261017-frontend-polish：侧边对话 / Git / 工作树三块右侧面板各自独立样式层（sd- / gx- / wtc- 前缀），
// 不再与 settings-c.css 等共用类名；紧随 panel.css 之后，随后续层正常覆盖。
import "./styles/side-chat.css";
import "./styles/git-panel.css";
import "./styles/worktree.css";
import "./styles/browser.css";
import "./styles/market.css";
import "./styles/motion.css";
import "./styles/focus.css";
import "./styles/model-picker.css";
import "./styles/themes.css";
import "./styles/tw.css";
import "./styles/revamp.css";
import "./styles/settings-a.css";
import "./styles/settings-b.css";
import "./styles/settings-c.css";
import "./styles/usage.css";
import "./styles/flow.css";
// plan-20261018-turn-changes：每轮文件变更卡片（tcc- 前缀），紧随消息流样式之后
import "./styles/turn-changes.css";
/* plan-156-619 S7：会话右上角综合工具卡（tcard- 前缀，避开工具树的 .tc-*）。 */
import "./styles/toolcard.css";
import "./styles/connector.css";
import "./styles/debug.css";
import "./styles/mascot.css";
// plan-114-450：设置页统一排版的最后一层（模型页 / 记忆页 / 拓展页 / 弹窗按钮 / 全选态），必须最后加载
import "./styles/settings-unify.css";
import "./styles/update.css";

initTheme();
initUi();
// v19: 插件化——内置组件注册为 slot 插件 + 加载用户外挂插件
registerBuiltinPlugins();
void loadExternalPlugins();
// v1.1: 启动时拉取后端全局设置（todos/reasoning 显示开关等）
void useUiStore.getState().refreshGlobalFlags();

// plan-329-1647 S12：dev 构建挂载性能度量护栏（window.__chatcoderPerf）。
// 生产默认不挂载（零开销）；设置中心的隐藏「性能诊断」开关写入本地标记后，下次启动即挂载，便于反馈卡顿时采集。
const DIAG_FLAG = "chatcoder.diagnostics";
let diagnosticsOn = false;
try { diagnosticsOn = localStorage.getItem(DIAG_FLAG) === "1"; } catch { /* 存储不可用时按关闭处理 */ }
if (import.meta.env.DEV || diagnosticsOn) installPerfMetrics();

// v4.5: 移除 StrictMode — 双重 effect 放大 WS handler 重复注册等竞态
ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
