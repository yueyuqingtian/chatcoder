/** 宠物窗口 preload：以 contextBridge 暴露最小权限 API。
 *
 * 只暴露宠物窗口真正需要的能力：窗口交互（穿透 / 拖拽 / 缩放 / 跳转）与主进程事件订阅。
 * 任务状态不经过主进程 —— 渲染层用 getBoot 拿到的 backendPort 直连后端 WebSocket，
 * 因此这里不提供任何"读任务 / 改任务"的接口。
 *
 * plan-73-341 变更：窗口尺寸恒定，故移除 `setForm`；拖拽与缩放改为主进程自循环，
 * 渲染层只发「开始」信号，不再逐帧上报坐标（消除跨进程坐标换算误差）。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("petAPI", {
  /** 启动一次性数据：{ pref, pet:{slug,...,spriteDataUrl}, installed:[], backendPort, titles } */
  getBoot: () => ipcRenderer.invoke("pet:getBoot"),
  /** 写入偏好（复用主进程同一份真相；用于「隐藏浮窗」这类宠物窗口内的开关） */
  setPref: (patch) => ipcRenderer.invoke("pet:setPref", patch),
  /** 交互态切换：false=穿透（透明区域不挡下层点击），true=可交互 */
  setInteractive: (on) => ipcRenderer.invoke("pet:setInteractive", !!on),

  /** 开始拖拽：传入「鼠标在窗口客户区内的偏移」（抓取点语义）与启动帧的初判方向。
   *  之后主进程自己轮询光标定位窗口**并持续判定行走方向**（拖拽期间窗口跟随光标，
   *  鼠标相对窗口不再移动，渲染层收不到 pointermove —— 方向只能由主进程判定）。 */
  dragBegin: (offsetX, offsetY, initialDir) =>
    ipcRenderer.invoke("pet:dragBegin", { offsetX, offsetY, initialDir }),
  /** 开始缩放：传入当前 scale，主进程按光标纵向位移 1:1 调整（窗口尺寸不变） */
  scaleBegin: (startScale) => ipcRenderer.invoke("pet:scaleBegin", startScale),
  /** 交互提前收尾（渲染层监听到 pointerup 时调用；主进程也会自行判定按键松开） */
  interactionEnd: () => ipcRenderer.invoke("pet:interactionEnd"),
  /** 光标在窗口客户区内的坐标（DIP）；窗口已销毁返回 null。
   *  用于悬停态兜底：透明穿透窗口会丢 mouseleave，需按真实光标位置校正。 */
  cursorClient: () => ipcRenderer.invoke("pet:cursorClient"),

  /** 跳回主窗对应会话（主窗聚焦并切到该会话） */
  focusSession: (sessionId) => ipcRenderer.invoke("pet:focusSession", sessionId),
  /** 停止会话运行（浮窗块右侧「停止」角标；走 REST 取消接口） */
  cancelTurn: (turnId) => ipcRenderer.invoke("pet:cancelTurn", turnId),
  /** 隐藏宠物（写偏好，任务运行中同样有效；可从设置页或右键菜单恢复） */
  hideTemporarily: () => ipcRenderer.invoke("pet:hideTemporarily"),
  /** 恢复显示（设置页或右键菜单调用） */
  showPet: () => ipcRenderer.invoke("pet:showPet"),
  /** 打开主窗「设置 → 宠物」分区（右键菜单） */
  openSettings: () => ipcRenderer.invoke("pet:openSettings"),
  /** 在图库页面打开该宠物来源（合规：标注来源并可跳转） */
  openPetPage: (slug) => ipcRenderer.invoke("pet:openPetPage", slug),

  /** 偏好变更（主窗设置页改动后同步到宠物窗口） */
  onPrefChanged: (cb) => {
    const handler = (_e, pref) => cb(pref);
    ipcRenderer.on("pet:prefChanged", handler);
    return () => ipcRenderer.removeListener("pet:prefChanged", handler);
  },
  /** 资源变更（切换宠物 / 安装完成后主进程主动下发） */
  onAssets: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("pet:assets", handler);
    return () => ipcRenderer.removeListener("pet:assets", handler);
  },
  /** 主窗焦点态：主窗未聚焦且任务完成时宠物播 waving 提醒 */
  onMainFocus: (cb) => {
    const handler = (_e, focused) => cb(focused === true);
    ipcRenderer.on("pet:mainFocus", handler);
    return () => ipcRenderer.removeListener("pet:mainFocus", handler);
  },
  /** 交互结束（主进程判定按键松开后通知）：渲染层据此复位拖拽/缩放的视觉态 */
  onInteractionEnded: (cb) => {
    const handler = (_e, payload) => cb(payload || {});
    ipcRenderer.on("pet:interactionEnded", handler);
    return () => ipcRenderer.removeListener("pet:interactionEnded", handler);
  },
  /** 拖拽行走方向（主进程按光标水平位移判定，仅方向变化时推送）：
   *  渲染层据此切换 running-left / running-right 动画。 */
  onDragDir: (cb) => {
    const handler = (_e, dir) => cb(dir === "left" || dir === "right" ? dir : null);
    ipcRenderer.on("pet:dragDir", handler);
    return () => ipcRenderer.removeListener("pet:dragDir", handler);
  },

  /** 会话快照（主进程 net.fetch 代取）。
   *  为什么必须代取：宠物页以 file:// 加载，直接 fetch 127.0.0.1 属跨源（Origin: null）会被 CORS 拦。 */
  snapshot: () => ipcRenderer.invoke("pet:snapshot"),
  /** 引擎步骤（仅选中无清单的任务时按需拉取一次） */
  tasks: (sessionId) => ipcRenderer.invoke("pet:tasks", sessionId),
});

// 平台标记：供 CSS 做平台感知（与主窗 preload 保持一致的约定）
try {
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.setAttribute("data-platform", process.platform);
  }
} catch { /* 非浏览器环境忽略 */ }
