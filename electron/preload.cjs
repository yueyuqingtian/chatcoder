// preload:通过 contextBridge 暴露受限 API 给前端
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("chatcoderAPI", {
  // 选择工作目录(返回绝对路径或 null)
  selectDirectory: () => ipcRenderer.invoke("dialog:selectDirectory"),
  // 系统集成
  openPath: (p) => ipcRenderer.invoke("shell:openPath", p),
  showItemInFolder: (p) => ipcRenderer.invoke("shell:showItemInFolder", p),
  // 终端 PTY
  ptySpawn: (opts) => ipcRenderer.invoke("pty:spawn", opts),
  ptyWrite: (id, data) => ipcRenderer.send("pty:write", id, data),
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
  // webview 元素标注回传
  onBrowserAnnotation: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("browser:annotation", handler);
    return () => ipcRenderer.removeListener("browser:annotation", handler);
  },
  // 窗口控制
  minimizeWindow: () => ipcRenderer.send("window:minimize"),
  toggleMaximize: () => ipcRenderer.send("window:maximizeToggle"),
  closeWindow: () => ipcRenderer.send("window:close"),
  // 外部穿透开关（透桌面/其他软件颜色）
  setExternalBackdrop: (on) => ipcRenderer.send("window:setExternalBackdrop", !!on),
});

// 注入平台到 <html data-platform>，供 CSS 平台感知样式（如 Win11 微圆角+四角透桌面）使用
try {
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.setAttribute("data-platform", process.platform);
  }
} catch { /* 非浏览器环境忽略 */ }
