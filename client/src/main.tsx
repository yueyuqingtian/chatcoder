import ReactDOM from "react-dom/client";
import App from "./App";
import { initTheme } from "./store/theme";
import "./styles/global.css";

initTheme();

// v4.5: 移除 StrictMode — 双重 effect 放大 WS handler 重复注册等竞态
ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
