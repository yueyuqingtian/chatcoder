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
  // plan-31-151 S2：主进程在 maximize/unmaximize 时通知渲染层——修复 frame:true +
  //   titleBarStyle:"hidden" 最大化时客户区外扩 8px 非客户区导致的边缘内容裁切。
  onMaximizeChange: (cb) => {
    const handler = (_e, isMax) => cb(isMax === true);
    ipcRenderer.on("window:maximize-change", handler);
    return () => ipcRenderer.removeListener("window:maximize-change", handler);
  },
  // plan-31-151 S3：自研拖拽通道已移除（标题栏改回系统 -webkit-app-region:drag）。
  relaunchApp: () => ipcRenderer.invoke("app:relaunch"),
  // 修复文本输入状态（输入框"能删不能输"卡死的兜底：主进程重新同步焦点）
  fixTextInput: () => ipcRenderer.invoke("window:fixTextInput"),
  // 主进程完成 WebContents 焦点同步后通知渲染层重试 DOM 输入框聚焦。
  onRendererFocus: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("window:renderer-focus", handler);
    return () => ipcRenderer.removeListener("window:renderer-focus", handler);
  },
  // 窗口几何运动：主进程在改尺寸前同步通知，渲染层据此暂停消息流重测。
  onWindowMotion: (cb) => {
    const handler = (_e, payload) => cb(payload || { active: false });
    ipcRenderer.on("window:motion", handler);
    return () => ipcRenderer.removeListener("window:motion", handler);
  },
  // 外部穿透开关（透桌面/其他软件颜色）
  setExternalBackdrop: (on) => ipcRenderer.send("window:setExternalBackdrop", !!on),
  // plan-546/plan-308-1542: 毛玻璃模式（Win11 acrylic / Win10 ACCENT 系统模糊 / mac vibrancy）
  setGlassMode: (on) => ipcRenderer.invoke("window:setGlass", !!on),
  // 查询当前窗口实际玻璃状态（false = 毛玻璃已关，标题栏/最大化走 Windows 原生行为）
  getGlassActive: () => ipcRenderer.invoke("window:getGlassActive"),
  // plan-26-116：液态玻璃（折射版）/玻璃诊断/自检的渲染层入口已全部移除（只保留毛玻璃）。
  // 当前系统用户名（侧栏底部用户条展示，对齐 zcode）
  getUsername: () => {
    try { return Promise.resolve(require("os").userInfo().username || ""); }
    catch { return Promise.resolve(""); }
  },
  // 保持唤醒开关（powerSaveBlocker）
  setKeepAwake: (on) => ipcRenderer.invoke("power:setKeepAwake", !!on),
  // 主题偏好同步（主进程落盘，下次启动 loading 页按此适配深浅色）
  setThemePref: (theme) => ipcRenderer.send("theme:setPref", theme === "light" ? "light" : "dark"),
  // UI 偏好备份（plan-73-344）：localStorage 之外的第二落盘通道；读取为同步调用
  // （启动时须与 localStorage 比对取较新者，异步会让首帧闪默认值）
  setUiPrefs: (payload) => ipcRenderer.send("ui-prefs:save", payload),
  getUiPrefs: () => {
    try { return ipcRenderer.sendSync("ui-prefs:load"); } catch { return null; }
  },
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
  // plan-73-323：宠物系统（设置页入口；偏好真相在主进程 userData/pet-pref.json）
  petGetPref: () => ipcRenderer.invoke("pet:getPref"),
  petSetPref: (patch) => ipcRenderer.invoke("pet:setPref", patch),
  petListInstalled: () => ipcRenderer.invoke("pet:listInstalled"),
  petListManifest: (opts) => ipcRenderer.invoke("pet:listManifest", opts || {}),
  petInstall: (slug) => ipcRenderer.invoke("pet:install", slug),
  petRemove: (slug) => ipcRenderer.invoke("pet:remove", slug),
  petImportLocal: () => ipcRenderer.invoke("pet:importLocal"),
  petOpenPetPage: (slug) => ipcRenderer.invoke("pet:openPetPage", slug),
  petRevealPet: (slug) => ipcRenderer.invoke("pet:revealPet", slug),
  onPetPrefChanged: (cb) => {
    const handler = (_e, pref) => cb(pref);
    ipcRenderer.on("pet:prefChanged", handler);
    return () => ipcRenderer.removeListener("pet:prefChanged", handler);
  },
  onPetFocusSession: (cb) => {
    const handler = (_e, sessionId) => cb(sessionId);
    ipcRenderer.on("app:focusSession", handler);
    return () => ipcRenderer.removeListener("app:focusSession", handler);
  },
  onPetOpenSettings: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("app:openPetSettings", handler);
    return () => ipcRenderer.removeListener("app:openPetSettings", handler);
  },
});

// 注入平台到 <html data-platform>，供 CSS 平台感知样式（如 Win11 微圆角+四角透桌面）使用
try {
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.setAttribute("data-platform", process.platform);
  }
} catch { /* 非浏览器环境忽略 */ }
