import ReactDOM from "react-dom/client";
import App from "./App";
import { initTheme } from "./store/theme";
import { initUi, useUiStore } from "./store/ui";
import { registerBuiltinPlugins } from "./plugins/builtin";
import { loadExternalPlugins } from "./plugins/external";
import { install as installPerfMetrics } from "./perf/metrics";
import "./styles/global.css";
// plan-282-1416: 组件库样式层（依赖 tokens.css，须在 global.css 之后以便覆盖旧兼容类）
import "./styles/components.css";
import "./styles/motion.css";
import "./styles/focus.css";
import "./styles/model-picker.css";

initTheme();
initUi();
// v19: 插件化——内置组件注册为 slot 插件 + 加载用户外挂插件
registerBuiltinPlugins();
void loadExternalPlugins();
// v1.1: 启动时拉取后端全局设置（todos/reasoning 显示开关等）
void useUiStore.getState().refreshGlobalFlags();

// plan-329-1647 S12：dev 构建挂载性能度量护栏（window.__chatcoderPerf）；生产不挂载，零开销。
if (import.meta.env.DEV) installPerfMetrics();

// v4.5: 移除 StrictMode — 双重 effect 放大 WS handler 重复注册等竞态
ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
