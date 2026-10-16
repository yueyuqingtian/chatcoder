// preload:通过 contextBridge 暴露受限 API 给前端
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("chatcoderAPI", {
  // 选择工作目录(返回绝对路径或 null)
  selectDirectory: () => ipcRenderer.invoke("dialog:selectDirectory"),
  // plan-20261009-ui U1：拖入文件夹取绝对路径（Electron 32+ 移除了 File.path，只能经 webUtils；
  // 非磁盘文件（JS 构造的 File）返回空串，调用方据此提示「请拖入文件夹」）
  getPathForFile: (file) => {
    try { return webUtils.getPathForFile(file) || ""; } catch { return ""; }
  },
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
  // plan-20261015-browser-agent：AI 驱动内置浏览器（节流 / CDP / 隐藏态截图 / 弹窗与新窗口事件）
  browserAgentPrepare: (webContentsId) => ipcRenderer.invoke("browserAgent:prepare", webContentsId),
  browserAgentExec: (webContentsId, op, args) => ipcRenderer.invoke("browserAgent:exec", webContentsId, op, args || {}),
  onBrowserAgentEvent: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("browserAgent:event", handler);
    return () => ipcRenderer.removeListener("browserAgent:event", handler);
  },
  // 窗口控制
  minimizeWindow: () => ipcRenderer.send("window:minimize"),
  toggleMaximize: () => ipcRenderer.send("window:maximizeToggle"),
  closeWindow: () => ipcRenderer.send("window:close"),
  // plan-31-151 S2：主进程在 maximize/unmaximize 时通知渲染层——修复 frame:true +
  //   titleBarStyle:"hidden" 最大化时客户区外扩 8px 非客户区导致的边缘内容裁切。
  // plan-20261016-ux（问题 6）：第二个参数为客户区真实外扩量 { top, left, right }（正常为 0）
  onMaximizeChange: (cb) => {
    const handler = (_e, isMax, inset) => cb(isMax === true, inset && typeof inset === "object" ? inset : null);
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
  setTonePref: (tone) => ipcRenderer.send("theme:setTone", String(tone)),
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
  // plan-387-1825 A2：右键菜单「隐藏宠物」后唯一的恢复入口。此前只声明未暴露，
  // 设置页「重新显示」按钮（store/pets.ts 的 showPet）恒拿到 undefined 静默早退，
  // 隐藏后彻底无法恢复。
  petShowPet: () => ipcRenderer.invoke("pet:showPet"),
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

// plan-114-450：文档解析前同步窗口 chrome 状态属性（与 data-platform 同一最早时机）。
//   为什么必须前置：data-native-corners 决定窗口四角由 DWM 原生圆角裁剪、还是由 CSS 自绘。
//   若等主进程在导航后注入，首帧已按自绘圆角渲染 → 与系统圆角错位成双层弧线，
//   即用户看到的「刚打开软件时四角弧度与边框不一致」。故此处同步（sendSync）取实时值。
function applyWindowChromeAttrs() {
  const root = document.documentElement;
  if (!root) return false;
  try {
    const s = ipcRenderer.sendSync("window:chrome-sync-get") || {};
    root.setAttribute("data-maximized", s.maximized ? "1" : "0");
    root.setAttribute("data-native-corners", s.nativeCorners ? "1" : "0");
    // plan-20261016-ux（问题 6）：首帧即按真实外扩量补偿标题栏（正常为 0，不再假定 8px）
    const inset = s.inset || {};
    root.style.setProperty("--win-inset-top", `${Number(inset.top) || 0}px`);
    root.style.setProperty("--win-inset-left", `${Number(inset.left) || 0}px`);
    root.style.setProperty("--win-inset-right", `${Number(inset.right) || 0}px`);
  } catch { /* 通道不可用：CSS 默认不自绘窗口圆角，不会出现双层弧线 */ }
  return true;
}
try {
  if (typeof document !== "undefined" && !applyWindowChromeAttrs()) {
    // documentElement 尚未创建：等 DOM 树一出现就写入（早于 DOMContentLoaded 与首帧）
    const mo = new MutationObserver(() => { if (applyWindowChromeAttrs()) mo.disconnect(); });
    mo.observe(document, { childList: true });
  }
} catch { /* 非浏览器环境忽略 */ }
