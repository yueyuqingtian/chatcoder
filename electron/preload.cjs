// preload:通过 contextBridge 暴露受限 API 给前端
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("chatcoderAPI", {
  // 选择工作目录(返回绝对路径或 null)
  selectDirectory: () => ipcRenderer.invoke("dialog:selectDirectory"),
  // 多选 md 文件（v1.1: 本地技能导入；v19: opts.allowDirectories 支持目录+文件混合选择）
  selectFiles: (filters, opts) => ipcRenderer.invoke("dialog:selectFiles", filters, opts),
  // 后端端口（主进程探活后选定的实际端口，前端去硬编码）
  getBackendPort: () => ipcRenderer.invoke("backend:getPort"),
  // 系统集成
  openPath: (p) => ipcRenderer.invoke("shell:openPath", p),
  showItemInFolder: (p) => ipcRenderer.invoke("shell:showItemInFolder", p),
  // plan-308-1542 需求5：在外部应用中打开目录（返回 { ok, launcher?, error? }，失败可提示）
  openInApp: (target, p) => ipcRenderer.invoke("shell:openInApp", target, p),
  // plan-308-1542：手动指定外部应用可执行文件（自动探测失败兼底）
  selectApp: (appName, currentPath) => ipcRenderer.invoke("app:selectApp", appName, currentPath),
  // plan-308-1542：查询已解析/已配置的启动器
  getExternalApps: () => ipcRenderer.invoke("app:getExternalApps"),
  // v23: 打开外部 URL（ta3 登录授权跳转，走系统默认浏览器）
  openExternal: (url) => ipcRenderer.invoke("shell:openExternal", url),
  // 终端 PTY
  ptySpawn: (opts) => ipcRenderer.invoke("pty:spawn", opts),
  ptyWrite: (id, data) => ipcRenderer.send("pty:write", id, data),
  ptyResize: (id, cols, rows) => ipcRenderer.send("pty:resize", id, cols, rows),
  ptyKill: (id) => ipcRenderer.send("pty:kill", id),
  onPtyData: (cb) => {
    const handler = (_e, id, data) => cb(id, data);
    ipcRenderer.on("pty:data", handler);
    return () => ipcRenderer.removeListener("pty:data", handler);
  },
  onPtyExit: (cb) => {
    const handler = (_e, id, code) => cb(id, code);
    ipcRenderer.on("pty:exit", handler);
    return () => ipcRenderer.removeListener("pty:exit", handler);
  },
  // webview 元素标注回传与 DevTools/截图
  onBrowserAnnotation: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("browser:annotation", handler);
    return () => ipcRenderer.removeListener("browser:annotation", handler);
  },
  openBrowserDevTools: (webContentsId) => ipcRenderer.invoke("browser:openDevTools", webContentsId),
  captureBrowserPage: (webContentsId) => ipcRenderer.invoke("browser:capturePage", webContentsId),
  // 窗口控制
  minimizeWindow: () => ipcRenderer.send("window:minimize"),
  toggleMaximize: () => ipcRenderer.send("window:maximizeToggle"),
  closeWindow: () => ipcRenderer.send("window:close"),
  // 修复文本输入状态（输入框"能删不能输"卡死的兜底：主进程重新同步焦点）
  fixTextInput: () => ipcRenderer.invoke("window:fixTextInput"),
  // 主进程完成 WebContents 焦点同步后通知渲染层重试 DOM 输入框聚焦。
  onRendererFocus: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("window:renderer-focus", handler);
    return () => ipcRenderer.removeListener("window:renderer-focus", handler);
  },
  // 外部穿透开关（透桌面/其他软件颜色）
  setExternalBackdrop: (on) => ipcRenderer.send("window:setExternalBackdrop", !!on),
  // plan-546/plan-308-1542: 毛玻璃模式（Win11 acrylic / Win10 ACCENT 系统模糊 / mac vibrancy）
  setGlassMode: (on) => ipcRenderer.invoke("window:setGlass", !!on),
  // plan-308-1542：模糊能力探测（设置页据此告知用户当前系统支持哪种玻璃）
  glassCapability: () => ipcRenderer.invoke("window:glassCapability"),
  // plan-308-1555 M0：毛玻璃诊断（DWM 回读 + 渲染层 alpha 链路），把"看不到"变成可判定结论
  glassDiagnostics: () => ipcRenderer.invoke("window:glassDiagnostics"),
  // plan-308-1555 M5：玻璃自检模式（临时调淡面板 alpha，肉眼一眼判定桌面是否混入）
  setGlassSelfCheck: (on) => ipcRenderer.invoke("window:glassSelfCheck", !!on),
  // 当前系统用户名（侧栏底部用户条展示，对齐 zcode）
  getUsername: () => {
    try { return Promise.resolve(require("os").userInfo().username || ""); }
    catch { return Promise.resolve(""); }
  },
  // 保持唤醒开关（powerSaveBlocker）
  setKeepAwake: (on) => ipcRenderer.invoke("power:setKeepAwake", !!on),
  // 主题偏好同步（主进程落盘，下次启动 loading 页按此适配深浅色）
  setThemePref: (theme) => ipcRenderer.send("theme:setPref", theme === "light" ? "light" : "dark"),
  // v19: 外挂插件列表（manifest + 源码文本）
  listUserPlugins: () => ipcRenderer.invoke("plugins:list"),
  // 自动更新（electron-updater + GitHub Releases）
  checkForUpdates: () => ipcRenderer.invoke("app:checkForUpdates"),
  getUpdateState: () => ipcRenderer.invoke("app:getUpdateState"),
  downloadUpdate: () => ipcRenderer.invoke("app:downloadUpdate"),
  installUpdate: () => ipcRenderer.invoke("app:installUpdate"),
  getAppVersion: () => ipcRenderer.invoke("app:getVersion"),
  onUpdateStatus: (cb) => {
    const handler = (_e, state) => cb(state);
    ipcRenderer.on("app:updateStatus", handler);
    return () => ipcRenderer.removeListener("app:updateStatus", handler);
  },
  // plan-230-1144 M4.2: 更新内容可见
  getReleaseNotes: (opts) => ipcRenderer.invoke("app:getReleaseNotes", opts || {}),
  getWhatsNew: (opts) => ipcRenderer.invoke("app:getWhatsNew", opts || {}),
  consumeWhatsNew: () => ipcRenderer.invoke("app:consumeWhatsNew"),
});

// 注入平台到 <html data-platform>，供 CSS 平台感知样式（如 Win11 微圆角+四角透桌面）使用
try {
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.setAttribute("data-platform", process.platform);
  }
} catch { /* 非浏览器环境忽略 */ }
