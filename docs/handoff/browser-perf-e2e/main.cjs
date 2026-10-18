// 隔离 E2E 主进程（plan-20261015-browser-perf 基准脚手架）：加载 client/dist + 真实 preload / browser-agent.cjs，
// 不拉起后端、不杀已安装应用进程（electron/main.cjs 启动时会 taskkill chatcoder-server.exe，故不用它）；
// 开启远程调试端口供 probe_host.py 读主窗口状态。用法见交接文档 §6。
const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");

const PORT = process.env.E2E_PORT || "22973";
// 本文件位于 docs/handoff/browser-perf-e2e/，仓库根目录在上三级
const ROOT = process.env.REPO_ROOT || path.resolve(__dirname, "..", "..", "..");
// 与 electron/main.cjs 一致：关闭 Windows 原生遮挡检测（plan-20261018-operator-boost）
app.commandLine.appendSwitch("disable-features", "CalculateNativeWinOcclusion");
app.commandLine.appendSwitch("remote-debugging-port", process.env.E2E_CDP || "9339");

function log(...a) { console.log("[e2e-main]", ...a); }
function logErr(...a) { console.error("[e2e-main]", ...a); }

ipcMain.handle("backend:getPort", () => Number(PORT));
for (const ch of [
  "app:getVersion", "app:getUpdateState", "app:getReleaseNotes", "app:getWhatsNew",
  "app:consumeWhatsNew", "app:getExternalApps", "app:checkForUpdates",
  "power:setKeepAwake", "window:getGlassActive", "shell:openPath", "shell:showItemInFolder",
  "shell:openExternal", "shell:openInApp", "app:selectApp", "app:relaunch",
  "dialog:selectDirectory", "dialog:selectFiles", "plugins:list",
  "pty:spawn", "window:fixTextInput", "window:setGlass", "theme:setPref", "theme:setTone",
  "ui-prefs:save", "browser:openDevTools", "browser:capturePage",
  "webview:annotate", "pet:getPref", "pet:setPref", "pet:listInstalled", "pet:listManifest",
  "pet:install", "pet:remove", "pet:importLocal", "pet:openPetPage", "pet:revealPet", "pet:showPet",
]) {
  if (ipcMain._events[ch]) continue;
  try { ipcMain.handle(ch, () => null); } catch { /* 已注册 */ }
}
// preload 用 sendSync 读的通道：只注册 handle 不会应答，渲染进程会一直卡住
ipcMain.on("window:chrome-sync-get", (e) => { e.returnValue = {}; });
ipcMain.on("ui-prefs:load", (e) => { e.returnValue = null; });
// 测「最小化」时由测试脚本经 CDP 调 chatcoderAPI.minimizeWindow() / toggleMaximize()（恢复）
ipcMain.on("window:minimize", () => { const w = BrowserWindow.getAllWindows()[0]; if (w) w.minimize(); });
ipcMain.on("window:maximizeToggle", () => { const w = BrowserWindow.getAllWindows()[0]; if (w) w.restore(); });
for (const ch of ["pty:write", "pty:kill", "pty:resize", "window:close", "window:setExternalBackdrop"]) {
  if (!ipcMain._events[ch]) { try { ipcMain.on(ch, () => {}); } catch { /* ignore */ } }
}

require(path.join(ROOT, "electron", "browser-agent.cjs"))({
  getMainWindow: () => BrowserWindow.getAllWindows()[0] || null,
  log, logErr,
});

function createWindow() {
  const win = new BrowserWindow({
    width: 1480, height: 940, show: true, autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(ROOT, "electron", "preload.cjs"),
      contextIsolation: true, nodeIntegration: false, sandbox: false, webviewTag: true,
    },
  });
  win.webContents.on("console-message", (_e, level, message) => { if (level >= 2) log("[renderer]", message); });
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-attach-webview", (_event, webPreferences) => {
    delete webPreferences.preload;
    webPreferences.nodeIntegration = false;
    webPreferences.nodeIntegrationInSubFrames = false;
    webPreferences.contextIsolation = true;
    webPreferences.backgroundThrottling = false;
  });
  win.loadFile(path.join(ROOT, "client", "dist", "index.html"));
}

app.whenReady().then(createWindow);
app.on("window-all-closed", () => app.quit());
