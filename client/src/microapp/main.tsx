/** 微应用独立窗口入口（拓展页 / 已安装列表 / 右侧面板「在新窗口打开」）。
 *
 *  为什么单独一个 Vite 入口而不是复用主窗 index.html：主窗是整应用（会话、WS、消息流），
 *  在第二个窗口里再跑一遍会产生两套状态与两条 WS 连接；这里只需要「承载一个微应用界面 +
 *  一个全屏按钮」。与 pet.html 同一种做法：独立入口、独立 chunk，不增加主窗包体积。
 *
 *  身份（appId / version / entry / uid）全部由主进程在打开窗口时写进 query
 *  （见 electron/main.cjs 的 openMicroAppWindow）；宿主桥的 uid → 会话绑定不在这个窗口里做，
 *  由**主窗口**渲染层统一持有（client/src/utils/microappWindow.ts 打开窗口时绑定），
 *  这样会话上下文不丢、也不会为了一个窗口多开一条 WS 连接。
 */
import { createRoot } from "react-dom/client";
import { MicroAppWindow } from "./MicroAppWindow";
import "./microapp-window.css";

// 主题跟随系统（窗口外壳只有一条浮层工具条，按 prefers-color-scheme 取令牌即可）
function syncTheme() {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
}
syncTheme();
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncTheme);

const el = document.getElementById("microapp-root");
if (el) {
  const params = new URLSearchParams(window.location.search);
  createRoot(el).render(
    <MicroAppWindow
      appId={params.get("appId") || ""}
      version={params.get("version") || ""}
      entry={params.get("entry") || ""}
      uid={params.get("uid") || ""}
      name={params.get("name") || ""}
    />,
  );
}
