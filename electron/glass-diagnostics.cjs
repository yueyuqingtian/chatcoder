// chatcoder 毛玻璃诊断模块（plan-308-1555 M0）
//
// 为什么需要它（正面解决历史失败根因）：
//   过去十几轮"改毛玻璃"反复失败，不是因为没调 API，而是**缺少可验证的反馈环**——
//   代码只能拿到"API 调用返回 ok"，拿不到"像素层究竟有没有把桌面混进来"。
//   于是每轮都停在「改了 → 看不到 → 再改别的」的循环里。
//
// 本模块提供三件事：
//   1) envReport()      —— 系统/Electron 版本事实（判断 acrylic 是否可用）
//   2) dwmReadBack()    —— **回读** DWM 实际生效的 backdrop 类型（不靠 API 返回值自证）
//   3) buildConclusion() —— 把「窗口层回读值 + 渲染层 alpha 链路采样」合成一行确定结论
//
// 设计约定：纯函数（版本解析 / 结论合成）与系统调用（FFI）分离，
// 前者可被单测覆盖，后者失败一律降级为"未知"，绝不抛异常打断主流程。

const os = require("os");

// ───────────────────────────────────────────────────────────────
// DWMWA / ACCENT 常量（与 Windows SDK 同名，便于对照文档）
// ───────────────────────────────────────────────────────────────
const DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
const DWMWA_SYSTEMBACKDROP_TYPE = 38;
// plan-26-126 P6：DWM 窗口边框色。无边框窗口（frame:false）在 Win11 上仍会被 DWM 画一圈
// **约 1 DIP 的系统边框**（实测：物理 2px @1.5x，颜色由系统/壁纸决定，与内容无关）。
// 玻璃态下这圈边框在深色主题上最显眼——用户看到的"边框透出桌面"就是它。
// DWMWA_COLOR_NONE 显式取消绘制（实测：设为 NONE 后四边像素立即变为内容色，且开启
// acrylic 材质后依然干净）。
const DWMWA_BORDER_COLOR = 34;
const DWMWA_COLOR_NONE = 0xfffffffe; // DWMWA_COLOR_NONE：不画边框（系统未定义常量，按文档字面值）
// 鼠标左键虚拟键码（GetAsyncKeyState 用）：自研窗口拖拽期间据此判断"用户是否已松开"。
const VK_LBUTTON = 0x01;
// plan-31-152 S5-2：窗口系统命令消息（自定义窗口按钮的兜底路径，与系统标题栏同源）。
const WM_SYSCOMMAND = 0x0112;
const SC_MINIMIZE = 0xf020;
const SC_MAXIMIZE = 0xf030;
const SC_RESTORE = 0xf120;

/** DWMSBT_* 值 → 可读名（回读值即用它解释） */
const BACKDROP_NAMES = {
  0: "auto",
  1: "none",
  2: "mica",
  3: "acrylic",       // DWMSBT_TRANSIENTWINDOW：桌面 Acrylic，真实透出桌面
  4: "tabbed",        // DWMSBT_TABBEDWINDOW：Mica Alt
};

// ───────────────────────────────────────────────────────────────
// 纯函数区（可单测）
// ───────────────────────────────────────────────────────────────

/**
 * 从 `os.release()` 字符串解析 Windows 构建号。
 * Windows 11 报 "10.0.26200"；Windows 10 报 "10.0.19045"。
 * 返回 { major, minor, build, isWindows, isWin11, supportsBackdrop } 或 null。
 */
function parseWindowsBuild(release) {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(String(release || ""));
  if (!m) return null;
  const major = Number(m[1]);
  const minor = Number(m[2]);
  const build = Number(m[3]);
  return {
    major,
    minor,
    build,
    isWindows: true,
    // 22000+ 即 Windows 11（21H2 首发）。注意：22000 本身不支持
    // DWMWA_SYSTEMBACKDROP_TYPE（需 22621+），故另给 supportsBackdrop 区分。
    isWin11: build >= 22000,
    supportsBackdrop: build >= 22621,
  };
}

/** 回读值（数字或字符串）→ 可读名；未知返回 `unknown(<raw>)`。 */
function backdropName(value) {
  if (value === null || value === undefined) return "unknown";
  const n = Number(value);
  if (Number.isNaN(n)) return `unknown(${value})`;
  return BACKDROP_NAMES[n] || `unknown(${n})`;
}

/**
 * 合成一行确定结论（本模块的核心产出）。
 *
 * @param {object} p
 * @param {number|string|null} p.backdropRaw   DWM 回读的 DWMWA_SYSTEMBACKDROP_TYPE 原值
 * @param {string} p.backend                   blurBackend() 探测到的后端名
 * @param {string|null} p.alphaPath            'ready' 或 'blocked@<selector>'
 * @param {boolean} p.degraded                 是否已降级
 * @returns {{ verdict: string, reason: string, line: string }}
 *
 * verdict 取值（供 UI 与日志直接判断）：
 *   material-live   材质已生效 → 若仍看不见属视觉对比问题（H3）
 *   material-dead   材质未生效 → 属合成路径问题（H1/H4/H5）
 *   blocked         材质可能生效但被不透明层遮挡（H2）
 *   unknown         无法判定（缺回读能力，如无 FFI）
 */
function buildConclusion({ backdropRaw, backend, alphaPath, degraded } = {}) {
  const name = backdropName(backdropRaw);
  const isLive = Number(backdropRaw) >= 2;
  const blocked = String(alphaPath || "").startsWith("blocked@");

  let verdict;
  let reason;
  if (backdropRaw === null || backdropRaw === undefined) {
    verdict = "unknown";
    reason = "无法回读 DWM 材质（FFI 不可用）——请检查 koffi 是否随包分发";
  } else if (isLive && blocked) {
    verdict = "blocked";
    reason = `材质已生效（${name}）但被不透明层遮挡：${String(alphaPath).replace("blocked@", "")}`;
  } else if (isLive) {
    verdict = "material-live";
    reason = `材质已生效（${name}）且透明度链路通畅；若肉眼仍看不出，属对比度不足（换彩色壁纸/提高强度）`;
  } else {
    verdict = "material-dead";
    reason = `材质未生效（回读 backdrop=${name}）——API 可能被接受但 DWM 未启用，需走 FFI 兼容路径`;
  }

  const line = [
    `backdrop=${name}(${backdropRaw ?? "-"})`,
    `alphaPath=${alphaPath || "unknown"}`,
    `backend=${backend || "unknown"}`,
    `degraded=${degraded ? "true" : "false"}`,
    `verdict=${verdict}`,
  ].join(" | ");

  return { verdict, reason, line };
}

// ───────────────────────────────────────────────────────────────
// FFI（koffi）：DWM 回读。失败一律降级，不抛异常
// ───────────────────────────────────────────────────────────────

let _ffiCache; // undefined=未尝试；null=不可用；对象=可用

/** 加载 user32/dwmapi 绑定（仅 Windows）。失败返回 null（不抛）。 */
function loadDwmFfi() {
  if (_ffiCache !== undefined) return _ffiCache;
  if (process.platform !== "win32") {
    _ffiCache = null;
    return null;
  }
  try {
    const koffi = require("koffi");
    const dwmapi = koffi.load("dwmapi.dll");
    const user32 = koffi.load("user32.dll");
    _ffiCache = {
      koffi,
      DwmGetWindowAttribute: dwmapi.func(
        "long __stdcall DwmGetWindowAttribute(intptr hwnd, uint attr, _Out_ void* pvAttribute, uint cbAttribute)"),
      DwmSetWindowAttribute: dwmapi.func(
        "long __stdcall DwmSetWindowAttribute(intptr hwnd, uint attr, _In_ void* pvAttribute, uint cbAttribute)"),
      DwmExtendFrameIntoClientArea: dwmapi.func(
        "long __stdcall DwmExtendFrameIntoClientArea(intptr hwnd, _In_ void* margins)"),
      SetWindowCompositionAttribute: user32.func(
        "bool __stdcall SetWindowCompositionAttribute(intptr hwnd, _Inout_ void* data)"),
      // plan-26-126 P6：自研窗口拖拽的按键状态查询（GetAsyncKeyState 返回高位=当前按下）
      GetAsyncKeyState: user32.func("short __stdcall GetAsyncKeyState(int vKey)"),
      // plan-31-152 S5-2：窗口命令兜底（自定义窗口按钮在系统 caption 命中区被吞时的补救路径）
      SendMessageW: user32.func(
        "intptr __stdcall SendMessageW(intptr hwnd, uint msg, intptr wParam, intptr lParam)"),
    };
    return _ffiCache;
  } catch (err) {
    _ffiCache = null;
    return null;
  }
}

/**
 * plan-26-126 P6：把窗口的系统边框设为"不绘制"。
 *
 * 为什么必须做（实测取证）：`frame:false` 的窗口在 Win11 上仍有一圈 DWM 绘制的系统边框
 * （物理 2px @1.5x）。玻璃开启时窗口内部透明，这圈边框就是用户看到的"一圈透出桌面的边"。
 * 实测结论：设 DWMWA_BORDER_COLOR = DWMWA_COLOR_NONE 后，四边外侧像素立刻并入内容色；
 * 并且叠加 acrylic 材质后依然干净（材质与边框色互不影响）。
 *
 * @param {BrowserWindow} win
 * @param {boolean} none true=不画边框（默认）；false=恢复系统默认
 * @returns {{ok: boolean, reason?: string, value?: number}}
 */
function setWindowBorder(win, none = true) {
  if (process.platform !== "win32") return { ok: false, reason: "非 Windows 平台" };
  const ffi = loadDwmFfi();
  const hwnd = hwndOf(win);
  if (!ffi) return { ok: false, reason: "FFI 不可用（koffi 缺失）" };
  if (hwnd === null) return { ok: false, reason: "无法获取 HWND" };
  try {
    const buf = Buffer.alloc(4);
    // DWMWA_COLOR_DEFAULT = 0xFFFFFFFF（恢复默认）；NONE = 0xFFFFFFFE
    buf.writeUInt32LE((none ? DWMWA_COLOR_NONE : 0xffffffff) >>> 0, 0);
    const hr = ffi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, buf, 4);
    if (hr !== 0) {
      return { ok: false, reason: `DwmSetWindowAttribute(BORDER_COLOR) 失败(hr=0x${(hr >>> 0).toString(16)})` };
    }
    return { ok: true, value: none ? DWMWA_COLOR_NONE : -1 };
  } catch (err) {
    return { ok: false, reason: err && err.message };
  }
}

/** 左键当前是否按下（自研拖拽的松开检测；FFI 不可用时返回 null） */
function isLeftButtonDown() {
  const ffi = loadDwmFfi();
  if (!ffi || typeof ffi.GetAsyncKeyState !== "function") return null;
  try {
    return (ffi.GetAsyncKeyState(VK_LBUTTON) & 0x8000) !== 0;
  } catch {
    return null;
  }
}

/** plan-31-152 S5-2：向窗口发送 WM_SYSCOMMAND 系统命令。
 *
 *  为什么需要（用户反馈"右上角只有缩放按钮点击无反应，最小化/关闭正常"）：
 *  该按钮位置可能落在 Windows 系统 caption 命中区，点击被系统吞掉且无任何行为；
 *  另一种可能是 Electron 的 `win.maximize()` 被系统忽略（CanMaximize 判定）。
 *  这里提供与「系统双击标题栏 / 标题栏右键菜单」**完全同一条路径**的兜底命令：
 *  WM_SYSCOMMAND + SC_MAXIMIZE / SC_RESTORE，命中率高于 Electron API。
 *
 * @param {BrowserWindow} win
 * @param {number} command SC_MAXIMIZE(0xF030) / SC_RESTORE(0xF120) / SC_MINIMIZE(0xF020)
 * @returns {{ok: boolean, reason?: string}}
 */
function sendSysCommand(win, command) {
  if (process.platform !== "win32") return { ok: false, reason: "非 Windows 平台" };
  const ffi = loadDwmFfi();
  if (!ffi || typeof ffi.SendMessageW !== "function") return { ok: false, reason: "FFI 不可用（koffi 缺失）" };
  const hwnd = hwndOf(win);
  if (hwnd === null) return { ok: false, reason: "无法获取 HWND" };
  try {
    ffi.SendMessageW(hwnd, WM_SYSCOMMAND, command, 0);
    return { ok: true };
  } catch (err) {
    return { ok: false, reason: err && err.message };
  }
}

/** 从 Electron BrowserWindow 取 HWND 数值（Buffer 为 little-endian 指针）。 */
function hwndOf(win) {
  if (!win || win.isDestroyed()) return null;
  try {
    const h = win.getNativeWindowHandle();
    if (Buffer.isBuffer(h)) {
      return h.length >= 8 ? Number(h.readBigUInt64LE(0)) : h.readUInt32LE(0);
    }
    if (typeof h === "number") return h;
    return null;
  } catch {
    return null;
  }
}

/**
 * 回读 DWM 实际生效的 backdrop 类型（**这是"是否真生效"的唯一权威判据**）。
 * 返回 { ok, value, name, hresult } 或 { ok:false, reason }。
 */
function dwmReadBack(win) {
  const ffi = loadDwmFfi();
  const hwnd = hwndOf(win);
  if (!ffi) return { ok: false, value: null, name: "unknown", reason: "FFI 不可用（koffi 缺失或非 Windows）" };
  if (hwnd === null) return { ok: false, value: null, name: "unknown", reason: "无法获取 HWND" };
  try {
    const buf = Buffer.alloc(4);
    const hr = ffi.DwmGetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, buf, 4);
    if (hr !== 0) {
      return { ok: false, value: null, name: "unknown", hresult: hr,
               reason: `DwmGetWindowAttribute 失败(hr=0x${(hr >>> 0).toString(16)})；该系统可能不支持 DWMWA_SYSTEMBACKDROP_TYPE` };
    }
    const value = buf.readInt32LE(0);
    return { ok: true, value, name: backdropName(value) };
  } catch (err) {
    return { ok: false, value: null, name: "unknown", reason: err && err.message };
  }
}

// ───────────────────────────────────────────────────────────────
// 渲染层 alpha 链路探针（自包含脚本，由主进程注入执行）
// ───────────────────────────────────────────────────────────────

/** 采样顺序：自窗口底向上（越外层越先采，先命中者即遮挡源候选） */
// plan-24-106 M7：复核选择器与真实 DOM 一致 —— 设置侧栏现由 SidebarShell 渲染
// （`sidebar sb settings-sidebar`），旧名 `.settings-nav` 已不是实际容器，补上真名，
// 否则"设置页被不透明层遮挡"这类问题会漏判。
const PROBE_SELECTORS = [
  "html",
  "body",
  "#root",
  ".app-shell",
  ".app-right",
  ".app-body",
  ".app-pane-left",
  ".sidebar.sb",
  ".titlebar",
  ".app-pane-right",
  ".right-panel",
  ".settings-page-overlay",
  ".settings-sidebar",
  // 兼容旧命名（若仍存在则采样，不存在时 querySelector 返回 null 自动跳过）
  ".settings-nav",
  // 决策 B：以下按设计保持不透明，仅记录、不参与 blocked 判定
  ".app-main",
];

/** 决策 B「消息流与主内容区保持不透明」的**预期**不透明白名单。 */
const EXPECTED_OPAQUE_SELECTORS = [
  ".app-main",
  ".message-flow",
  ".message-flow-wrap",
  ".composer",
  ".composer-main",
];

/**
 * 解析 CSS 颜色的 alpha（0..1）。
 * 支持 transparent / rgba() / rgb() / #RRGGBBAA / 现代空格语法 `rgb(r g b / a)`。
 * 无背景（none）按 0；纯 hex/rgb（无 alpha）按 1。
 */
function parseColorAlpha(color) {
  if (!color) return 0;
  const c = String(color).trim().toLowerCase();
  if (["transparent", "none", "initial", "unset", "rgba(0, 0, 0, 0)"].includes(c)) return 0;
  const rgba = /^rgba?\(([^)]+)\)$/.exec(c);
  if (rgba) {
    const parts = rgba[1].split(/[,/\s]+/).filter(Boolean);
    if (parts.length >= 4) {
      const a = Number(parts[3]);
      return Number.isNaN(a) ? 0 : Math.max(0, Math.min(1, a));
    }
    return 1;
  }
  const hex8 = /^#([0-9a-f]{8})$/.exec(c);
  if (hex8) return parseInt(hex8[1].slice(6, 8), 16) / 255;
  if (/^#[0-9a-f]{3,6}$/.test(c)) return 1;
  return 0;
}

/**
 * 由采样结果判定透明度链路是否通畅。
 * 规则：某层「背景完全不透明」且「没有使用 backdrop-filter」⇒ 材质被它挡住 ⇒ blocked@该层。
 * 白名单层（决策 B 预期不透明）跳过判定。
 */
function analyzeAlphaChain(samples) {
  const opaqueUnexpected = [];
  for (const s of samples || []) {
    if (s.isExpectedOpaque) continue;
    const alpha = parseColorAlpha(s.backgroundColor);
    const hasBackdrop = Boolean(s.backdropFilter && s.backdropFilter !== "none");
    if (alpha >= 1 && !hasBackdrop) opaqueUnexpected.push(s.selector);
  }
  return {
    alphaPath: opaqueUnexpected.length ? `blocked@${opaqueUnexpected[0]}` : "ready",
    blockedSelector: opaqueUnexpected[0] || null,
    opaqueUnexpected,
  };
}

/**
 * 生成注入渲染进程执行的**自包含**探针脚本字符串。
 *
 * 设计取舍：为什么把 JS 写成字符串而不是 import 前端模块？
 *   探针必须在「前端可能未加载完 / 前端构建产物未更新」时也能跑出结论——
 *   这正是本计划要打破的历史困局（改了代码但运行的仍是旧包）。
 *   自包含脚本由主进程注入，与前端构建产物解耦，永远可用。
 */
function probeScript() {
  // plan-24-106 M0 关键修复（历史十几轮"改了看不到"的直接原因）：
  //   analyzeAlphaChain 序列化后的函数体内部引用的是 **parseColorAlpha**，
  //   而旧代码注入时只把它赋给了 `parseAlpha` —— 注入后该标识符不存在，
  //   整段脚本一进入 analyze() 就抛 ReferenceError，又被主进程 catch 静默吞掉，
  //   于是 alphaPath 永远是 unknown（截图实测：backdrop=acrylic(3) | alphaPath=unknown）。
  //   修复：注入时使用**与源码一致的函数名** parseColorAlpha（并保留 parseAlpha 别名），
  //   同时整段脚本自带 try/catch，把异常作为 `error` 字段回传，后续不再静默失败。
  return `(() => {
    const SELECTORS = ${JSON.stringify(PROBE_SELECTORS)};
    const EXPECTED = ${JSON.stringify(EXPECTED_OPAQUE_SELECTORS)};
    const parseColorAlpha = ${parseColorAlpha.toString()};
    const parseAlpha = parseColorAlpha;
    const analyzeAlphaChain = ${analyzeAlphaChain.toString()};
    const analyze = analyzeAlphaChain;
    try {
      const samples = [];
      for (const sel of SELECTORS) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const cs = window.getComputedStyle(el);
        samples.push({
          selector: sel,
          backgroundColor: cs.backgroundColor,
          backdropFilter: cs.backdropFilter || cs.webkitBackdropFilter || "none",
          backgroundImage: (cs.backgroundImage && cs.backgroundImage !== "none") ? "has-image" : "none",
          isExpectedOpaque: EXPECTED.includes(sel),
        });
      }
      const a = analyze(samples);
      return { ok: true, ...a, samples, error: null };
    } catch (err) {
      // 探针自身异常必须可见：否则又会退回"只能看到 unknown、查不出为什么"的困局
      return {
        ok: false,
        error: (err && err.message) ? err.message : String(err),
        alphaPath: null,
        blockedSelector: null,
        opaqueUnexpected: [],
        samples: [],
      };
    }
  })()`;
}

/** 环境事实（版本/平台/是否打包），供诊断与日志使用。 */
function envReport(app, explicitRelease) {
  const release = explicitRelease || os.release();
  const win = parseWindowsBuild(release);
  return {
    platform: process.platform,
    release,
    windows: win,
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
    modules: process.versions.modules,
    isPackaged: Boolean(app && app.isPackaged),
  };
}

/** 窗口事实（含主进程侧记录的材质参数）。 */
function windowReport(win, recorded) {
  if (!win || win.isDestroyed()) return { ok: false, reason: "窗口不可用" };
  const base = {
    ok: true,
    visible: win.isVisible(),
    maximized: win.isMaximized(),
    minimized: win.isMinimized(),
    fullScreen: win.isFullScreen(),
  };
  // Electron 44 起 BrowserWindow.isTransparent() 已被移除 → 改按能力探测（诊断字段，缺失即 null）
  base.transparent = typeof win.isTransparent === "function" ? win.isTransparent() : null;
  return { ...base, recorded: recorded || null };
}

module.exports = {
  DWMWA_USE_IMMERSIVE_DARK_MODE,
  DWMWA_SYSTEMBACKDROP_TYPE,
  DWMWA_BORDER_COLOR,
  DWMWA_COLOR_NONE,
  VK_LBUTTON,
  BACKDROP_NAMES,
  PROBE_SELECTORS,
  EXPECTED_OPAQUE_SELECTORS,
  parseWindowsBuild,
  backdropName,
  parseColorAlpha,
  analyzeAlphaChain,
  probeScript,
  buildConclusion,
  loadDwmFfi,
  hwndOf,
  dwmReadBack,
  setWindowBorder,
  isLeftButtonDown,
  sendSysCommand,
  WM_SYSCOMMAND,
  SC_MINIMIZE,
  SC_MAXIMIZE,
  SC_RESTORE,
  envReport,
  windowReport,
};
