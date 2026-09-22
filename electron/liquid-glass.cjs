// chatcoder 原生折射液态玻璃封装（plan-24-106 M6）
//
// ── 为什么需要这个模块（能力边界，必须先说清）──
// Electron 官方文档（tutorial/custom-window-styles.md）明确：
//   "The CSS blur() filter only applies to the window's web contents, so there is
//    no way to apply blur effect to the content below the window."
// 即**纯 CSS 永远无法模糊/折射窗口下方的桌面**。
// 历史上用 `@tomagranate/liquid-glass`（SVG feDisplacementMap）做折射也走不通：
// 它只能对"元素背后的背景副本"做位移，采样不到窗口外的桌面像素（该包本轮已整体移除）。
//
// 真折射只能靠原生模块：@hicccc77/electron-liquid-glass
//   DXGI Desktop Duplication 截取桌面 → D3D11 模糊 + 圆角 SDF 折射 + RGB 色散
//   → DirectComposition 呈现为一个**独立原生窗口**，钉在本应用窗口的 z 序下方。
//
// ── 关键约束（实测得出，务必遵守）──
// 1) 面板是**背景层**：它被钉在应用窗口下方，因此面板所在区域在应用窗口里
//    **必须是透明的**，否则会被窗口自身内容/材质遮住。
//    ⇒ 折射模式与 Win11「非透明窗口 + acrylic」互斥；启用折射时必须让窗口走
//      `transparent: true`，且不要设 backgroundMaterial。
// 2) 坐标必须是**屏幕物理像素**。实测（ai/_m6_dpi_authoritative.cjs）：
//    scaleFactor=1.5 时 dipToScreenPoint(100,100) = (150,150)，
//    而 win.getBounds() 返回 (100,100) ⇒ getBounds() 是 DIP，必须 ×dpr。
// 3) 面板需 `capturePolicy: 'all'`（0.3+ 的默认值）以避免自捕获回环。
//
// ── 防御式设计（沿用 glassDiag 的教训）──
// 上一轮曾因"打包漏文件 → require 抛异常 → 后续窗口初始化步骤全部中断 →
// 窗口停在不可见状态"造成事故。因此这里任何失败都只降级为
// `{ supported: false, reason }`，**绝不抛异常、绝不阻断窗口显示**。

const path = require("path");

/** 延迟加载原生模块（结果缓存：运行期能力不变）。失败返回 {mod:null, reason}。 */
let _modCache;
function loadGlassModule() {
  if (_modCache !== undefined) return _modCache;
  try {
    const mod = require("@hicccc77/electron-liquid-glass");
    if (!mod || typeof mod.createPanel !== "function" || typeof mod.isSupported !== "function") {
      _modCache = { mod: null, reason: "模块接口不完整（createPanel/isSupported 缺失）" };
    } else {
      _modCache = { mod, reason: "" };
    }
  } catch (err) {
    _modCache = { mod: null, reason: "无法加载原生模块：" + (err && err.message ? err.message : String(err)) };
  }
  return _modCache;
}

/** 能力探测：`{ supported, reason, platform }`。绝不抛异常。 */
function capability() {
  const { mod, reason } = loadGlassModule();
  if (!mod) return { supported: false, reason, platform: process.platform };
  try {
    if (process.platform !== "win32") {
      return { supported: false, reason: "原生折射仅支持 Windows（macOS/Linux 后端仍在 roadmap）", platform: process.platform };
    }
    const ok = mod.isSupported();
    return {
      supported: !!ok,
      reason: ok ? "" : "当前系统不支持：需 Windows 10 2004 (build 19041)+ 且原生二进制可用",
      platform: process.platform,
    };
  } catch (err) {
    return { supported: false, reason: "isSupported() 调用失败：" + (err && err.message ? err.message : String(err)), platform: process.platform };
  }
}

/**
 * 面板区域定义（值为 **DIP**，由调用方给出渲染层里的真实尺寸）。
 * 决策：只覆盖**装饰性**区域（标题栏条 + 左侧栏列），
 * 不含消息流/主内容区 —— 那里是长文本与滚动区，折射会拖累性能并影响可读性。
 */
const DEFAULT_REGIONS = {
  titlebarHeight: 40,
  sidebarWidth: 264,
};

/**
 * 折射面板管理器。
 *
 * @param {object} opts
 * @param {import("electron").BrowserWindow} opts.win 应用窗口
 * @param {(msg: string) => void} [opts.log] 日志回调（由主进程注入，避免本模块直接依赖日志实现）
 * @param {(msg: string) => void} [opts.logErr]
 */
function createManager({ win, log = () => {}, logErr = () => {} } = {}) {
  /** @type {Array<{panel: any, key: string}>} */
  let panels = [];
  let enabled = false;
  let regions = { ...DEFAULT_REGIONS };
  /** 面板视觉参数（物理像素口径） */
  const EFFECT = {
    cornerRadius: 0,          // 贴合窗口边缘的直角区不用圆角
    blurSigma: 5,
    displacementScale: 70,    // 标准折射强度
    aberrationIntensity: 2,   // 色散
    saturation: 1.4,
  };
  let destroyed = false;
  let lastDpr = null;

  const usable = () => !destroyed && win && !win.isDestroyed();

  /** 取窗口所在显示器的 scaleFactor（多屏 DPR 不同，必须按窗口所在屏取值）。 */
  function currentDpr() {
    try {
      const { screen } = require("electron");
      const b = win.getBounds();
      const d = screen.getDisplayNearestPoint({ x: b.x + Math.round(b.width / 2), y: b.y + Math.round(b.height / 2) });
      return (d && d.scaleFactor) || 1;
    } catch {
      return 1;
    }
  }

  /**
   * 计算各面板的**屏幕物理像素**矩形。
   * 关键：getBounds() 返回 DIP（实测确认），必须 ×dpr 才能交给原生模块。
   */
  function computeTargets() {
    if (!usable()) return [];
    const b = win.getBounds();
    const dpr = currentDpr();
    const P = (x, y, w, h) => ({
      x: Math.round(x * dpr),
      y: Math.round(y * dpr),
      width: Math.max(1, Math.round(w * dpr)),
      height: Math.max(1, Math.round(h * dpr)),
    });
    const targets = [];
    // ① 标题栏条：整宽 × 顶部高
    if (regions.titlebarHeight > 0) {
      targets.push({ key: "titlebar", bounds: P(b.x, b.y, b.width, regions.titlebarHeight) });
    }
    // ② 左侧栏列：从标题栏下沿到窗口底部
    if (regions.sidebarWidth > 0) {
      targets.push({
        key: "sidebar",
        bounds: P(b.x, b.y + regions.titlebarHeight, regions.sidebarWidth, b.height - regions.titlebarHeight),
      });
    }
    return targets.map((t) => ({ ...t, dpr }));
  }

  function destroyPanels() {
    for (const { panel } of panels) {
      try { panel.destroy(); } catch (err) { logErr("[liquid-glass] 销毁面板失败: " + (err && err.message)); }
    }
    panels = [];
  }

  /** 按当前窗口几何重建/更新面板。 */
  function sync() {
    if (!enabled || !usable()) return;
    const targets = computeTargets();
    const dpr = targets.length ? targets[0].dpr : currentDpr();

    // DPR 变化（跨屏移动/缩放设置变更）：原生面板内部几何常量按 dpr 缩放，
    // 必须重建才能正确渲染。
    if (lastDpr !== null && lastDpr !== dpr) {
      log("[liquid-glass] dpr 变化 " + lastDpr + " → " + dpr + "，重建面板");
      destroyPanels();
    }

    // 数量/结构变化：整体重建（简单且不会残留错位面板）
    const sameShape = panels.length === targets.length
      && targets.every((t, i) => panels[i] && panels[i].key === t.key);
    if (!sameShape) {
      destroyPanels();
      const { mod } = loadGlassModule();
      for (const t of targets) {
        let panel = null;
        try {
          panel = mod.createPanel({
            ...t.bounds,
            ...EFFECT,
            dpr: t.dpr,
            anchorWindow: win,
            // capturePolicy 默认 'all'：把面板自身排除出屏幕捕获，避免自捕获回环
          });
        } catch (err) {
          logErr("[liquid-glass] 创建面板失败(" + t.key + "): " + (err && err.message));
        }
        if (panel) panels.push({ panel, key: t.key });
      }
      log("[liquid-glass] 已创建面板 " + panels.length + "/" + targets.length);
    } else {
      // 位置/尺寸变化：只更新 bounds（代价远低于重建）
      for (let i = 0; i < targets.length; i++) {
        try { panels[i].panel.setBounds(targets[i].bounds); }
        catch (err) { logErr("[liquid-glass] setBounds 失败: " + (err && err.message)); }
      }
    }
    lastDpr = dpr;
  }

  /** 显示/隐藏（窗口最小化、失焦隐藏等场景）。 */
  function setVisible(visible) {
    if (!enabled || !usable()) return;
    for (const { panel, key } of panels) {
      try {
        if (visible) panel.show(120);
        else panel.hide(100);
      } catch (err) {
        logErr("[liquid-glass] " + (visible ? "show" : "hide") + " 失败(" + key + "): " + (err && err.message));
      }
    }
  }

  return {
    /**
     * 启用折射。返回 `{ ok, reason }`。
     * 失败一律降级（不抛），调用方据此回退到 acrylic + CSS 质感路线。
     */
    enable(nextRegions) {
      if (!usable()) return { ok: false, reason: "窗口不可用" };
      const cap = capability();
      if (!cap.supported) return { ok: false, reason: cap.reason };
      if (nextRegions) regions = { ...regions, ...nextRegions };
      enabled = true;
      destroyPanels();      // 强制重建，确保用最新区域
      lastDpr = null;
      sync();
      setVisible(true);
      if (!panels.length) {
        enabled = false;
        return { ok: false, reason: "面板创建失败（详见日志）" };
      }
      return { ok: true, panels: panels.length };
    },

    /** 关闭折射并销毁面板。 */
    disable() {
      enabled = false;
      destroyPanels();
      lastDpr = null;
      return { ok: true };
    },

    /** 窗口几何变化后同步（move/resize/restore/display-metrics-changed）。 */
    sync,

    /** 显隐联动（minimize/show/hide）。 */
    setVisible,

    /** 仅更新区域参数（设置页调整侧栏宽度/标题栏高度时）。 */
    setRegions(next) {
      if (next) regions = { ...regions, ...next };
      if (enabled) { destroyPanels(); lastDpr = null; sync(); }
    },

    /** 是否处于启用状态。 */
    isEnabled: () => enabled,

    /** 当前面板数量（诊断用）。 */
    count: () => panels.length,

    /** 彻底释放（窗口关闭/应用退出）。 */
    dispose() {
      destroyed = true;
      enabled = false;
      destroyPanels();
    },
  };
}

/** 进程退出前停止原生 worker 线程（模块自身也会在 exit 前自动调用，这里显式兜底）。 */
function shutdownAll() {
  const { mod } = loadGlassModule();
  if (!mod || typeof mod.shutdown !== "function") return;
  try { mod.shutdown(); } catch (err) { /* 退出期失败无需处理 */ }
}

module.exports = {
  capability,
  createManager,
  shutdownAll,
  loadGlassModule,
  DEFAULT_REGIONS,
};
