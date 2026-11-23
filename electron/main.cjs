// chatcoder 桌面主进程 (v3 — 健壮启动版)
// 修复: data: URL → loadFile / stdout 安全 / 全局错误捕获 / 图标
// plan-75-335：libuv 线程池须在首次异步 IO 前配置（默认 4，提高到 8 增加主进程 IO 并行度）。
process.env.UV_THREADPOOL_SIZE = process.env.UV_THREADPOOL_SIZE || "8";
const { app, BrowserWindow, ipcMain, dialog, shell, nativeTheme, session, Notification, protocol } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const http = require("http");
const net = require("net");
const fs = require("fs");
// plan-73-323：宠物窗口与宠物资源管理（只管窗口/资源/偏好，任务状态由渲染层直连后端）
const pet = require("./pet.cjs");
// plan-693-2868 §6.1 / D27：微应用库根统一解析（app:// 协议读文件与 spawn 后端注入共用同一套链）
const microApp = require("./microapp-root.cjs");

// plan-693-2868 · docs/app1/11-host-bridge-protocol.md §9-1：宿主桥能力（uid 映射 / 上行转发 /
// 下行推送）实现在 browser-agent.cjs 里——那里已经有 microappGuest 与同一套 guest 校验口径。
// 建窗后（createWindow）用它把 did-attach-webview 钩子挂到主窗口，模块加载末尾赋值。
let browserAgentApi = null;

// ── GPU 加速开关（plan-75-335）──
// 调研结论：Electron 在 Windows 上默认启用 GPU 加速（本机实测 ANGLE/NVIDIA D3D11 已生效）；
// 以下为「保持硬件路径 + 防止静默降级为软件渲染」的显式声明，须在 app ready 之前调用：
app.commandLine.appendSwitch("enable-gpu-rasterization");        // GPU 光栅化：光栅负载从 CPU 移到 GPU
app.commandLine.appendSwitch("enable-zero-copy");                // 零拷贝纹理上传，降低显存带宽占用
app.commandLine.appendSwitch("force_high_performance_gpu");      // 多显示适配器（含虚拟显示器）时优先独显
app.commandLine.appendSwitch("disable-gpu-process-crash-limit"); // GPU 进程崩溃后不永久降级为软件渲染
// plan-20261018-operator-boost：关闭 Windows 原生遮挡检测。Chromium 会把被其它窗口完全盖住的窗口按「后台标签」处理
// （停止出帧、JS 节流），用户切到别的软件时 AI 在内置浏览器里的截图与输入就会受阻。自动化框架（Playwright 等）
// 同样默认关闭遮挡降级；代价是被遮挡时仍按可见渲染，最小化不受影响（仍按隐藏处理）。
app.commandLine.appendSwitch("disable-features", "CalculateNativeWinOcclusion");
// 实验结论（plan-75-335）：num-raster-threads 在渲染进程的有效上限为 4（实测设 2 生效为 2、
// 设 8 被收敛为 4），本机默认已在上限，故不设置该开关，避免无效配置。

// GPU 特性状态诊断（plan-75-335）：每次 GPU 信息更新记录一行，排查「是否被降级为软件渲染」。
app.on("gpu-info-update", () => {
  try { log("[chatcoder] GPU 特性状态:", JSON.stringify(app.getGPUFeatureStatus())); } catch { /* 诊断信息，失败不影响运行 */ }
});

// ── 后端端口选择（默认不常用端口 12973，避免冲突）──
const DEFAULT_PORT = 12973;

function probePort(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => srv.close(() => resolve(true)));
    srv.listen(port, "127.0.0.1");
  });
}

/**
 * 杀死当前软件对应的所有旧后端进程及可能残留占用 12973 端口的孤儿进程。
 * 保证每次启动都是一个全新的当前版本后端，杜绝复用历史旧进程导致的代码未生效。
 */
function killExistingBackendProcesses(targetPort = DEFAULT_PORT) {
  return new Promise((resolve) => {
    log("[chatcoder] 检查并清理历史残留后端进程与端口占用...");
    if (process.platform === "win32") {
      const { exec } = require("child_process");
      // 1. 强杀所有历史 chatcoder-server.exe
      exec("taskkill /F /IM chatcoder-server.exe /T", () => {
        // 2. 检查并强杀可能占用 targetPort 的任何孤儿进程
        const killPortCmd = `powershell -NoProfile -NonInteractive -Command "Get-NetTCPConnection -LocalPort ${targetPort} -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"`;
        exec(killPortCmd, () => {
          setTimeout(resolve, 800); // 留出 800ms 保证操作系统完全释放文件锁与端口
        });
      });
    } else {
      const { exec } = require("child_process");
      exec("pkill -9 -f chatcoder-server || true", () => {
        setTimeout(resolve, 500);
      });
    }
  });
}

async function pickBackendPort() {
  const base = Number(process.env.CHATCODER_PORT || DEFAULT_PORT);
  // 清理完旧进程后，优先使用默认基准端口；若被占用则向后顺延寻找空闲端口
  for (let p = base; p < base + 50; p++) {
    if (await probePort(p)) return p;
  }
  return base;
}

let BACKEND_PORT = Number(process.env.CHATCODER_PORT || DEFAULT_PORT);
let backendProcess = null;
let mainWindow = null;
let backendReady = false;
// plan-308-1555 M0：记录主进程侧实际下发的材质参数（诊断与窗口报告用）
let _glassRecorded = { material: null, backgroundColor: null, transparent: null, at: null };
// plan-26-116：毛玻璃意图的内存态（窗口构造时初始化，IPC 切换时更新）。
// 事件后重放只认这个内存态，不再每次读 glass-pref.json——窗口切换动画期间读文件有时序风险，
// 且 DWM 在最大化/全屏过渡中会重置 backdrop，需要多次重放兜底（见 syncWindowGlass）。
let _glassWanted = false;
// 当前窗口**实际生效**的玻璃状态（仅 createWindow 赋值一次，窗口生命周期内不变）。
// 与 _glassWanted 的区别：后者会被 window:setGlass IPC 同步成"偏好值"（重启才生效），
// 而最大化/拖拽的分流必须看窗口真实材质——玻璃真实开启时绝不能走系统原生 maximize
// （原生最大化重建 DWM 图层，acrylic 直接丢失）；玻璃关闭时则按用户要求切回 Windows
// 原生行为（原生最大化贴合工作区 + 系统原生拖拽）。
let _glassActive = false;

// ── plan-31-151 S3：伪最大化 / 自研拖拽 / 自研双击 / 玻璃运动租约已整体移除 ──
// 历史背景：frame:false + transparent 架构下，系统原生最大化会重建 DWM 图层导致
//   透明/acrylic 标记丢失，因此用 _pseudoMax + fitContentBounds 自绘伪最大化保玻璃，
//   并配套 _dragSession 自研拖拽（系统 drag 区会吞 dblclick）、玻璃运动租约
//   （_glassMotionHolds 系列，运动期禁材质重放）三套机制。
// 本架构（frame:true + titleBarStyle:"hidden" + backgroundMaterial:"acrylic"）下：
//   原生最大化由 DWM 全程合成，玻璃不丢；拖拽/双击/边缘缩放全由系统接管。
//   上述三套机制随之整体删除，窗口几何只有一个写入者（系统）。
// 明确禁止（防回归，与 N14 对齐）：setSkipTaskbar(true) / display.bounds / alwaysOnTop /
//   setFullScreen(true)。前两者会让窗口覆盖或从任务栏消失，最后一个会关闭 DWM 合成导致玻璃直接失效。

// ── 安全写日志(打包后无 stdout 也不崩溃) + 写入文件 ──
const LOG_DIR = app.isPackaged
  ? require("path").join(app.getPath("userData"), "logs")
  : require("path").join(__dirname, "..", "logs");
try { fs.mkdirSync(LOG_DIR, { recursive: true }); } catch {}
const LOG_FILE = path.join(LOG_DIR, "main.log");

/** 运动期日志缓冲（plan-329-1647 S2）。
 *
 *  为何要缓冲：落盘走的是 fs.appendFileSync —— **同步写盘**。主进程在拖窗 / 边缘 resize /
 *  伪全屏补间期间会高频打日志（日志目录在 C 盘时尤其明显），同步 I/O 落在逐帧关键路径上
 *  就是实打实的掉帧。现在把「几何运动期间」的落盘先入内存队列（_glassMotionHolds > 0），
 *  运动租约归零（endGlassMotion）时一次性落盘；队列超限也立即落盘兜底，避免丢日志。
 *  异常路径（logErr）永远直写，保证崩溃现场不丢关键信息。 */
let _logBuffer = [];
const LOG_BUFFER_MAX = 400;

/** 把缓冲中的日志一次性落盘。 */
function flushLog() {
  if (_logBuffer.length === 0) return;
  const text = _logBuffer.join("");
  _logBuffer = [];
  try { fs.appendFileSync(LOG_FILE, text); } catch {}
}

/** 日志落盘统一出口。plan-31-151 S3：玻璃运动租约已移除，恢复直写
 *  （同步 fs.appendFileSync 在主进程几何事件路径上的开销已随 setBounds 循环一并消失）。 */
function writeLogLine(line, force) {
  try { fs.appendFileSync(LOG_FILE, line); } catch {}
}

function log(...args) {
  const msg = args.join(" ");
  try {
    if (process.stdout && process.stdout.writable) {
      process.stdout.write(msg + "\n");
    }
  } catch {}
  writeLogLine(new Date().toISOString() + " " + msg + "\n");
}
function logErr(...args) {
  const msg = args.join(" ");
  try {
    if (process.stderr && process.stderr.writable) {
      process.stderr.write(msg + "\n");
    }
  } catch {}
  flushLog(); // 异常现场：先把已缓冲内容落盘，再直写本次错误
  writeLogLine(new Date().toISOString() + " [ERROR] " + msg + "\n", true);
}

// ── 全局错误捕获,避免静默崩溃 ──
process.on("uncaughtException", (err) => {
  logErr("[chatcoder] uncaughtException:", err && err.stack ? err.stack : err);
});
process.on("unhandledRejection", (reason) => {
  logErr("[chatcoder] unhandledRejection:", reason);
});

// ── 解析资源路径(开发 vs 打包后) ──
function resolveBackendDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "server", "chatcoder-server");
  }
  return path.join(__dirname, "..", "server", "dist", "chatcoder-server");
}
function resolveFrontendDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "frontend");
  }
  return path.join(__dirname, "..", "client", "dist");
}

// ── plan-693-2868 §6.2 / §7 P0-3 / D24：微应用 app:// 协议 + CSP ──
//
// 为什么 CSP 必须由「协议响应头」给出，而不能挂在 http(s) webview 上（方案 §6.2 + §8 风险表
// 「CSP 加错位置 → 破坏浏览器面板」）：
//   1. 右侧浏览器面板的 webview 加载的是任意 http(s) 站点（BrowserPanel.tsx:726-732），它们与主窗口
//      共用默认 session、共用同一套 webPreferences。若在主进程用 onHeadersReceived 统一注入严格 CSP，
//      登录态、SPA 路由、页面内联脚本会整体失灵——而 webview 没有「这一条是微应用、那一条是普通网页」
//      的身份，没法在响应头层面分流。
//   2. 微应用必须断网（§4 D22：无审核，靠沙箱 + CSP 断网兜底），断网只能靠 `connect-src 'none'` /
//      `script-src 'self'`。把 CSP 放进 app:// 的 Response 头，作用域天然收敛到「加载微应用的那一次
//      导航」，浏览器面板一行都不受影响。
//   3. 所以主进程在这里只做两件事：app:// 的响应头带 CSP；will-attach-webview 里只补 sandbox
//      （见 createWindow 内该钩子）。宿主页面（file:// 前端）不注入 CSP，保持现状。
const MICROAPP_CSP =
  // plan-20261116-review-fixes：共享库与应用不同源，只放行本地 vendor，不开放外网。
  "default-src 'self'; img-src 'self' app://vendor data: blob:; media-src 'self' blob:; " +
  "style-src 'self' app://vendor 'unsafe-inline'; script-src 'self' app://vendor; " +
  "font-src 'self' app://vendor data:; connect-src 'none'; " +
  "frame-src 'none'; object-src 'none'; base-uri 'none'";
// style-src 保留 'unsafe-inline' 是方案 D25 的明文决策：antd 5 走 CSS-in-JS 运行时插 <style>，
// 去掉内联就等于所有第三方组件库不可用（§5.4 最后一行）。
// connect-src 'none' 是微应用断网的唯一手段：即便页面里有 fetch/XHR/WebSocket 也会被浏览器拦下。

// 微应用 webview 的独立 partition（方案 §6.2）。P0-6 的 <webview partition="persist:microapp">
// 必须与此常量一致；协议按 session 注册，默认 session 上注册的 handler 不会自动继承到别的 partition。
const MICROAPP_PARTITION = "persist:microapp";

// 宿主桥 preload 的绝对路径（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §3）。
// **只**注入给 src 以 app://microapp/ 开头的 webview，普通网页使用独立的登录监听脚本。
// 见 createWindow 里 will-attach-webview 的条件注入与那里的越权警告注释。
const MICROAPP_PRELOAD_PATH = path.join(__dirname, "microapp-preload.cjs");

/** `will-attach-webview` 的统一加固（主窗口 **与** 微应用独立窗口共用这一份口径）。
 *
 *  为什么抽成模块级函数：微应用可以在独立窗口里打开（microapp:openWindow），那个窗口里同样
 *  有一个 `<webview>`。如果只有主窗口挂这个钩子，独立窗口里的 webview 就拿不到宿主桥 preload
 *  （AI 调 microapp_call 会报 bridge-missing），同时它也绕过了 sandbox / 不注入 preload 的收敛。
 *  两条路径必须逐字同一份规则，所以只能有一处实现。
 */
function hardenWebviewAttach(event, webPreferences, params) {
  // plan-693-2868：微应用桥只按受信来源注入，渲染层传入的 preload 始终丢弃。
  // 顺序是先无条件 delete、再按来源决定是否注入——这样 <webview preload="..."> 属性
  // （渲染层/页面都可控）在任何情况下都不会生效。
  delete webPreferences.preload;
  webPreferences.nodeIntegration = false;
  webPreferences.nodeIntegrationInSubFrames = false;
  webPreferences.contextIsolation = true;
  webPreferences.backgroundThrottling = false;
  // plan-693-2868 §6.2 / §7 P0-4 / D24：webview 一律进 Chromium 渲染器沙箱。
  // 微应用是无审核来源的第三方页面（§4 D22），除 CSP 断网外还需要进程级隔离兜底；
  // 普通网页没有 Node 能力，登录监听脚本在沙箱内的隔离世界运行。
  webPreferences.sandbox = true;

  // ⚠ 高危判定（写错即越权，务必保持原样）：
  // 只认**严格前缀、区分大小写**的 app://microapp/。
  // 不要改成 includes / 正则 / toLowerCase 之类：那样
  //   src = "http://evil.example/app://microapp/x"（includes 命中）
  //   src = "APP://MICROAPP/x"（忽略大小写命中）
  // 这类普通网页也会走到下面的注入分支，浏览器面板里的任意 http 站点就直接拿到
  // window.microapp（exec / 读文件全通），宿主通道等于对公网开放。
  if (String((params && params.src) || "").startsWith("app://microapp/")) {
    // 微应用：注入只暴露 window.microapp 四项能力的 preload（electron/microapp-preload.cjs）。
    // 上面已设置的 sandbox / contextIsolation / nodeIntegration 全部保留，注入不放宽任何一条。
    webPreferences.preload = MICROAPP_PRELOAD_PATH;
  } else if (event.sender === mainWindow?.webContents && /^https?:\/\//i.test(String(params?.src || ""))) {
    // plan-771-3177：固定的登录监听脚本没有 contextBridge，也不接受网页调用。
    webPreferences.preload = path.join(__dirname, "browser-login-preload.cjs");
  }
}

/** 扩展名 → Content-Type（方案 §7 P0-3 列举的 12 类，其余按二进制下发）。 */
const MICROAPP_MIME = {
  ".html": "text/html; charset=utf-8",
  ".htm": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
};

function microAppContentType(filePath) {
  return MICROAPP_MIME[path.extname(filePath).toLowerCase()] || "application/octet-stream";
}

/**
 * 单个路径段的合法性。方案 §7 P0-3 要求「禁 `..`、禁绝对路径」。
 * 注意：这里用「白名单式」的段校验而不是「拼完再查字符串前缀」——`path.resolve` 对
 * `..`、`C:\`、`/` 的处理因平台而异，先掐死段内的分隔符/盘符/父目录引用最可靠。
 */
function isSafeMicroAppSegment(seg) {
  if (typeof seg !== "string" || seg.length === 0) return false;
  if (seg === "." || seg === "..") return false;          // 父目录/当前目录引用
  if (seg.includes("/") || seg.includes("\\")) return false; // 段内夹分隔符（含 Windows 反斜杠）
  if (seg.includes("\0")) return false;                   // NUL 截断
  if (path.isAbsolute(seg)) return false;                 // 绝对路径
  if (/^[A-Za-z]:/.test(seg)) return false;               // 盘符（非 Windows 上 isAbsolute 判不出 "C:"）
  return true;
}

/** target 是否仍在 root 之内（用 path.relative 判定，跨盘符/大小写/分隔符都安全）。 */
function isInsideRoot(root, target) {
  const rel = path.relative(path.resolve(root), path.resolve(target));
  return rel === "" || (rel !== ".." && !rel.startsWith(".." + path.sep) && !path.isAbsolute(rel));
}

/** 协议层的错误响应（不带 CSP：错误页不需要执行任何东西）。 */
function microAppError(status, message) {
  return new Response(message, {
    status,
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
  });
}

/**
 * 注册 `app://` 协议（必须在 app ready 之后调用）。
 *
 * 只服务两个命名空间（方案 §6.2 / §6.3 / §7 P0-3）：
 *   app://microapp/<appId>/<version>/<path...>   → <libraryRoot>/<appId>/<version>/<path...>
 *   app://vendor/<name>/<version>/<path...>      → <libraryRoot>/vendor/<name>/<version>/<path...>
 *
 * 状态码约定：400 命名空间/路径不完整、403 越出根目录、404 文件不存在。
 */
function registerMicroAppProtocol() {
  const libraryRoot = microApp.resolveMicroAppRoot();

  const handler = async (request) => {
    let url;
    try {
      url = new URL(request.url);
    } catch {
      return microAppError(400, "bad url");
    }

    // scheme 已声明为 standard，`app://microapp/...` 的 host 就是我们用来分命名空间的段
    const namespace = url.hostname;
    if (namespace !== "microapp" && namespace !== "vendor") {
      return microAppError(400, "unsupported namespace: " + namespace);
    }

    // 先逐段百分号解码再校验：`%2e%2e` 解码后就是 `..`，若先判后解码等于留了后门
    const segments = [];
    for (const raw of url.pathname.split("/")) {
      if (!raw) continue; // 折叠重复斜杠与末尾斜杠
      let seg;
      try {
        seg = decodeURIComponent(raw);
      } catch {
        return microAppError(400, "bad escape");
      }
      if (!isSafeMicroAppSegment(seg)) return microAppError(403, "forbidden segment");
      segments.push(seg);
    }
    // 至少要凑齐 <id>/<version>/<file>；只有目录没有文件名的请求一律 400（不做 index.html 隐式补全，
    // 入口文件由 manifest 显式声明，行为可预期）
    if (segments.length < 3) return microAppError(400, "incomplete path");

    const [id, version, ...rest] = segments;
    const baseDir =
      namespace === "microapp"
        ? path.join(libraryRoot, id, version)
        // plan-693-2868：共享库目录用 .vendor（点前缀）——microapp_list 会跳过点目录，
        // 否则共享第三方库会被当成一个合法 appId 扫进微应用列表。
        : path.join(libraryRoot, ".vendor", id, version);

    // 越界防护（1/2，逻辑路径）：逐段 path.resolve 后必须仍在 baseDir 内。
    // 段校验已挡掉 `..` 与绝对路径，这里再兜一层 path.resolve 的规范化差异（尾随点、超长段等）。
    let target = path.resolve(baseDir);
    for (const seg of rest) {
      target = path.resolve(target, seg);
      if (!isInsideRoot(baseDir, target)) return microAppError(403, "out of root");
    }

    // 越界防护（2/2，真实路径）：软链接可以把 baseDir 内的文件指向根外，必须用 realpath 复核。
    // 任何一步 realpath 失败（目录/文件不存在）都按 404 处理，不泄露磁盘结构。
    let realBase;
    let realTarget;
    try {
      realBase = await fs.promises.realpath(baseDir);
      realTarget = await fs.promises.realpath(target);
    } catch {
      return microAppError(404, "not found");
    }
    if (!isInsideRoot(realBase, realTarget)) return microAppError(403, "out of root");

    let stat;
    let buffer;
    try {
      stat = await fs.promises.stat(realTarget);
      if (!stat.isFile()) return microAppError(404, "not found");
      buffer = await fs.promises.readFile(realTarget);
    } catch {
      return microAppError(404, "not found");
    }

    return new Response(buffer, {
      status: 200,
      headers: {
        "Content-Type": microAppContentType(realTarget),
        // 方案 D24：CSP 只在这个 Response 头上出现
        "Content-Security-Policy": MICROAPP_CSP,
        // plan-20261116-review-fixes：模块脚本和字体跨本地源仍需 CORS，许可仅授给微应用页面。
        ...(namespace === "vendor" ? { "Access-Control-Allow-Origin": "app://microapp" } : {}),
        "Cache-Control": "no-store",
      },
    });
  };

  protocol.handle("app", handler);
  // 微应用 webview 跑在独立 partition（§6.2），协议注册是按 session 生效的，默认 session 的
  // handler 不会继承过去；同步给 microapp partition 注册一份，否则 P0-6 的微应用页会直接
  // ERR_UNKNOWN_URL_SCHEME。失败只记日志，不影响浏览器面板与主窗口。
  try {
    session.fromPartition(MICROAPP_PARTITION).protocol.handle("app", handler);
  } catch (err) {
    logErr("[chatcoder] microapp partition 协议注册失败:", err && err.message);
  }
  log("[chatcoder] app:// 协议已注册，库根:", libraryRoot);
}

// 方案 §7 P0-3 / §6.2：scheme 特权必须在 app ready **之前**声明（Electron 硬约束，ready 后再调无效），
// 因此放在模块顶层的执行流里，而不是 whenReady 回调内。
//   standard: true   → 有 host/path 语义，`app://microapp/id/ver/x` 才能被解析成命名空间 + 路径，
//                      同时拿到正常的 URL/相对路径/同源（'self'）判定
//   secure: true     → 视为安全上下文（CSP 的 'self'、Workers、加密 API 才有意义）
//   supportFetchAPI  → 微应用内部用 fetch 读同源资源时不会抛「不支持的 scheme」（跨域仍被 CSP connect-src 拦）
//   corsEnabled      → plan-20261116-review-fixes：vendor 模块与字体需要按响应头执行本地跨源检查
//   stream: true     → Response 以流式 Body 返回，大文件（vendor 库）不必整块进内存
protocol.registerSchemesAsPrivileged([
  {
    scheme: "app",
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true },
  },
]);

// ── 启动后端 ──
async function startBackend() {
  try {
    const dir = resolveBackendDir();
    const exe = path.join(dir, "chatcoder-server.exe");
    if (!fs.existsSync(exe)) {
      logErr("[chatcoder] 后端可执行文件不存在:", exe);
      return null;
    }
    log("[chatcoder] 启动新后端进程:", exe, "端口:", BACKEND_PORT);

    // 把后端输出写到文件,便于诊断
    const backendLogPath = path.join(LOG_DIR, "backend.log");
    const backendLogStream = fs.createWriteStream(backendLogPath, { flags: "w" });
    const crashLogPath = path.join(LOG_DIR, "crash-detect.log");

    backendProcess = spawn(exe, [], {
      cwd: dir,
      env: {
        ...process.env,
        SERVER_HOST: "127.0.0.1",
        SERVER_PORT: String(BACKEND_PORT),
        CHATCODER_PORT: String(BACKEND_PORT),
        CORS_ALLOW_ALL: "true",
        PYTHONUNBUFFERED: "1",
        // plan-693-2868 §6.1 / D27：把库根显式下发，Python 侧不再各自猜路径（缺失时它回落
        // ~/.chatcoder/microapps 并记日志）。与 app:// 协议 handler 用同一个解析函数，保证
        // 「后端写的目录」就是「协议读的目录」。
        CHATCODER_MICROAPP_ROOT: microApp.resolveMicroAppRoot(),
      },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });

    // v36: 用 StringDecoder 按 UTF-8 分块解码，正确处理跨 chunk 的多字节字符边界。
    // 此前 d.toString() 在无编码参数时同样按 utf8，但与后端实际编码不一致会产生乱码；
    // 配合后端 logging.py 强制 UTF-8 输出，保证 backend.log 中文可读。
    const { StringDecoder } = require("string_decoder");
    const stdoutDecoder = new StringDecoder("utf8");
    const stderrDecoder = new StringDecoder("utf8");
    backendProcess.stdout.on("data", (d) => {
      const text = stdoutDecoder.write(d);
      if (!text) return;
      try { if (process.stdout && process.stdout.writable) process.stdout.write(text); } catch {}
      try { backendLogStream.write(text); } catch {}
    });
    backendProcess.stderr.on("data", (d) => {
      const text = stderrDecoder.write(d);
      if (!text) return;
      try { if (process.stderr && process.stderr.writable) process.stderr.write(text); } catch {}
      try { backendLogStream.write(text); } catch {}
    });

    backendProcess.on("error", (err) => {
      logErr("[chatcoder] 后端 spawn error:", err.message);
      try { backendLogStream.write("SPAWN ERROR: " + err.message + "\n"); } catch {}
    });

    backendProcess.on("exit", (code, signal) => {
      log("[chatcoder] 后端退出, code =", code, "signal =", signal);
      try { backendLogStream.end(`\n[EXIT] code=${code} signal=${signal}\n`); } catch {}
      if (code !== 0 && code !== null) {
        // 非 0 退出 = 崩溃
        try {
          fs.writeFileSync(crashLogPath, `Backend crashed with code ${code} at ${new Date().toISOString()}\n`);
        } catch {}
        // 通知前端窗口显示错误
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.executeJavaScript(
            `document.getElementById('msg') && (document.getElementById('msg').innerHTML = '后端启动失败 (错误码 ${code})<br>请查看日志: ${LOG_DIR.replace(/\\/g, "/")}/backend.log');`
          ).catch(() => {});
        }
      }
      backendProcess = null;
    });

    return backendProcess;
  } catch (err) {
    logErr("[chatcoder] startBackend 异常:", err);
    return null;
  }
}

// ── 等待端口就绪(后端提前崩溃时立即放弃) ──
function waitForBackend(maxAttempts = 120, intervalMs = 500) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    let crashed = false;

    // 监听后端进程退出(崩溃时快速失败)
    const checkInterval = setInterval(() => {
      if (!backendProcess && !backendReady && attempts > 2) {
        crashed = true;
      }
    }, 500);

    const check = () => {
      attempts++;
      // 如果后端进程已不存在且不是手动停止,说明崩溃了
      if (crashed || (!backendProcess && attempts > 4)) {
        clearInterval(checkInterval);
        reject(new Error("后端进程已崩溃退出"));
        return;
      }
      const req = http.get(
        { host: "127.0.0.1", port: BACKEND_PORT, path: "/api/health", timeout: 2000 },
        (res) => {
          // 只认 JSON 响应：8000 端口可能被 CLodop 打印服务等占用，
          // 它会对任意路径返回 200 HTML，误判后端就绪会导致前端请求打到 HTML 上。
          const chunks = [];
          res.on("data", (c) => chunks.push(c));
          res.on("end", () => {
            const body = Buffer.concat(chunks).toString("utf8");
            let isChatCoder = false;
            try {
              const data = JSON.parse(body);
              isChatCoder = data.status === "ok" && data.service === "chatcoder";
            } catch { isChatCoder = false; }
            if (res.statusCode === 200 && isChatCoder) {
              clearInterval(checkInterval);
              resolve();
            } else if (attempts >= maxAttempts) {
              clearInterval(checkInterval);
              reject(new Error("后端健康检查失败"));
            } else {
              setTimeout(check, intervalMs);
            }
          });
        }
      );
      req.on("error", () => {
        if (attempts >= maxAttempts) {
          clearInterval(checkInterval);
          reject(new Error("后端启动超时"));
        } else {
          setTimeout(check, intervalMs);
        }
      });
      req.on("timeout", () => {
        req.destroy();
        if (attempts >= maxAttempts) {
          clearInterval(checkInterval);
          reject(new Error("后端响应超时"));
        } else {
          setTimeout(check, intervalMs);
        }
      });
    };
    // 首次延迟 500ms,给后端一点启动时间
    setTimeout(check, 500);
  });
}

// ── 防御式模块加载（plan-308-1555 修复）──
// 教训：上一轮新增 electron/glass-diagnostics.cjs 后**忘记加入 package.json 的 build.files 白名单**，
// 打包产物里缺该文件 → 主进程 require 抛 MODULE_NOT_FOUND → ready-to-show 里的
// 圆角/淡入等后续步骤全部中断，窗口停在"已显示但 opacity=0"的状态：
// 任务栏有图标、DWM 预览能渲染出内容，但肉眼看不见、也点不开。
// 现在统一用 defensiveRequire：任何可选模块缺失都只降级，**绝不影响窗口显示**。
let _gdCache;
function glassDiag() {
  if (_gdCache !== undefined) return _gdCache;
  try {
    _gdCache = require("./glass-diagnostics.cjs");
  } catch (err) {
    logErr("[chatcoder] glass-diagnostics 加载失败（将降级，不影响启动）:", err && err.message);
    _gdCache = null;
  }
  return _gdCache;
}

// ── 毛玻璃（plan-548，plan-308-1542 需求4 强化为"框架级"）──
// 用户反馈："要做到微微透出桌面应该要软件框架支持吧，仅仅靠样式应该做不到透出软件"。
// 事实：Electron 31 只在 **Win11 22000+** 提供 backgroundMaterial('acrylic')；Win10
// 没有任何内建模糊 API，纯 CSS 半透明只能看到"未模糊的桌面"，观感不成立。
// 因此这里引入**系统级模糊后端**三通道：
//   win11      → DWM acrylic（非透明窗口，系统材质）
//   win32 旧版 → SetWindowCompositionAttribute + ACCENT_ENABLE_BLURBEHIND（透明窗口 + 系统模糊）
//   darwin     → setVibrancy('sidebar')
// 失败/不支持 → 明确降级（返回 backend="none" + reason），设置页据此提示用户。
function isWin11Plus() {
  if (process.platform !== "win32") return false;
  const m = /^10\.0\.(\d+)/.exec(os.release());
  return !!m && Number(m[1]) >= 22000;
}

/** 探测可用的模糊后端（结果缓存：系统能力在运行期不变）。 */
let _blurBackend = null;
function blurBackend() {
  if (_blurBackend) return _blurBackend;
  if (process.platform === "darwin") {
    _blurBackend = { backend: "vibrancy", reason: "" };
  } else if (process.platform === "win32") {
    if (isWin11Plus()) {
      _blurBackend = { backend: "dwm-acrylic", reason: "" };
    } else {
      // Win10：需要 FFI 调 user32.SetWindowCompositionAttribute
      const ffi = loadAccentFfi();
      _blurBackend = ffi
        ? { backend: "win32-accent", reason: "", _ffi: ffi }
        : { backend: "none", reason: "当前系统无可用模糊后端（Win10 需 koffi 模块，Win11 才支持 DWM acrylic）" };
    }
  } else {
    _blurBackend = { backend: "none", reason: `平台 ${process.platform} 不支持系统级模糊` };
  }
  return _blurBackend;
}

/** 尝试加载 FFI（koffi）用于 Win10 ACCENT 模糊；不可用返回 null（不抛错）。 */
function loadAccentFfi() {
  let koffi;
  try {
    koffi = require("koffi");
  } catch (err) {
    logErr("[chatcoder] glass: koffi 不可用，Win10 系统模糊禁用:", err && err.message);
    return null;
  }
  try {
    const user32 = koffi.load("user32.dll");
    // 结构体定义交给 koffi（与实测验证的写法一致；手工 Buffer 容易错位）
    const ACCENT_POLICY = koffi.struct("ACCENT_POLICY", {
      AccentState: "int",
      AccentFlags: "int",
      GradientColor: "uint",
      AnimationId: "int",
    });
    const WCAD = koffi.struct("WINDOWCOMPOSITIONATTRIBDATA", {
      Attribute: "int",
      Data: "void*",
      SizeOfData: "size_t",
    });
    const SetWindowCompositionAttribute = user32.func(
      "bool __stdcall SetWindowCompositionAttribute(intptr hwnd, _Inout_ WINDOWCOMPOSITIONATTRIBDATA* data)");
    return { koffi, SetWindowCompositionAttribute, ACCENT_POLICY, WCAD };
  } catch (err) {
    logErr("[chatcoder] glass: koffi 绑定 user32 失败:", err && err.message);
    return null;
  }
}

/**
 * 应用/关闭毛玻璃（统一入口）。
 *
 * plan-308-1555 关键改动：每次应用后**回读校验** DWM 实际值，
 * 并把（下发的材质 + 回读结果）写入 `_glassRecorded` 与日志——
 * 这是本轮与历史做法的根本差别：不再以"API 返回 ok"自证成功。
 *
 * plan-31-152 S5-3（用户反馈"双击最大化后玻璃丢失、退出也不恢复、重启才回来"）：
 *   ① `opts.skipVerify` —— 窗口状态切换（最大化/还原/非客户区重建）当下，DWM 会**重置**
 *      窗口 backdrop；此时立即回读必然读到旧值（<2），旧逻辑据此把本次下发判为
 *      `ok:false` 并**阻断后续重试**（调用方看到 ok:false 就不再补发）。重放路径必须
 *      跳过这道校验，先把材质设下去，由多档延时重放覆盖 DWM 重建窗口期。
 *   ② 成功应用后同步更新 `_glassActive`，让"窗口实际玻璃状态"跟随真实回读结果，
 *      不再被建窗期的一次性赋值固化。
 *
 * @param {boolean} [opts.skipVerify] 跳过回读校验阻断（窗口状态切换的重放路径专用）
 * @returns {{ok: boolean, backend: string, reason?: string, verified?: number|null}}
 */
function applyGlass(win, on, opts) {
  const skipVerify = Boolean(opts && opts.skipVerify);
  if (!win || win.isDestroyed()) return { ok: false, backend: "none", reason: "窗口不可用" };
  const cap = blurBackend();
  let res;
  try {
    if (cap.backend === "dwm-acrylic") {
      if (typeof win.setBackgroundMaterial === "function") {
        win.setBackgroundMaterial(on ? "acrylic" : "none");
        res = { ok: true, backend: cap.backend };
      } else {
        res = { ok: false, backend: "none", reason: "Electron 不支持 setBackgroundMaterial" };
      }
    } else if (cap.backend === "vibrancy") {
      if (typeof win.setVibrancy === "function") {
        win.setVibrancy(on ? "sidebar" : null);
        res = { ok: true, backend: cap.backend };
      } else {
        res = { ok: false, backend: "none", reason: "Electron 不支持 setVibrancy" };
      }
    } else if (cap.backend === "win32-accent") {
      res = _applyWin32Accent(win, cap._ffi, on);
    } else {
      res = { ok: false, backend: "none", reason: cap.reason || "无可用模糊后端" };
    }
  } catch (err) {
    logErr("[chatcoder] glass: 应用失败:", err && err.message);
    res = { ok: false, backend: cap.backend, reason: err && err.message };
  }

  // 回读校验：DWM 实际生效值（0=auto / 1=none / 2=mica / 3=acrylic / 4=tabbed）
  // skipVerify=true 时仍然**回读并记录**（诊断价值保留），但不据此把结果判为失败。
  let verified = null;
  try {
    const gd = glassDiag();
    const rb = gd ? gd.dwmReadBack(win) : { ok: false, value: null };
    verified = rb.ok ? rb.value : null;
    if (on && rb.ok && rb.value < 2 && !skipVerify) {
      // API 被接受但 DWM 未启用 → 明确标记为未生效（历史上正是这种"假成功"误导了排查）
      logErr(`[chatcoder] glass: 回读校验未生效！下发=acrylic 但系统实际=${rb.name}(${rb.value})`);
      res = { ...res, ok: false, reason: `系统未启用材质（回读 ${rb.name}）` };
    } else if (on && rb.ok && rb.value < 2 && skipVerify) {
      // 窗口状态切换中：DWM 尚未完成重建属预期，记录下来但不阻断（后续重放会覆盖）
      log(`[chatcoder] glass: 重放（skipVerify）期间回读=${rb.name}(${rb.value})，等待下一档重放`);
    }
  } catch (err) {
    logErr("[chatcoder] glass: 回读校验异常:", err && err.message);
  }

  // plan-31-152 S5-3②：成功下发后同步"窗口实际玻璃状态"，避免建窗期一次性赋值固化。
  //   仅在真正下发成功（res.ok）时置位；关闭玻璃时置 false。
  if (res.ok) _glassActive = Boolean(on);

  _glassRecorded = {
    material: on ? "acrylic" : "none",
    backgroundColor: on ? "#00000000" : null,
    transparent: false,
    verifiedBackdrop: verified,
    at: new Date().toISOString(),
  };
  log("[chatcoder] glass: applied =", JSON.stringify({ ...res, verified }));
  return { ...res, verified };
}

// plan-26-126 P1：原 applyWindowBackground（运行期改写窗口底色）已**删除**。
// 删除原因（用户实测）：运行期翻转 Win11 窗口底色会让 DWM 合成状态与渲染层缓存失步，
//   表现为左侧面板异常色块、设置页返回后残留浅残影，**只有重启才恢复**。
// 现在玻璃开关一律重启生效，窗口底色只在 createWindow 的构造参数里按落盘偏好决定一次。

/** Win10：SetWindowCompositionAttribute(ACCENT_ENABLE_BLURBEHIND)。 */
function _applyWin32Accent(win, ffi, on) {
  try {
    const hwnd = win.getNativeWindowHandle();
    // 取 HWND 数值（Windows 上传回的是 little-endian 指针）
    let hwndVal = 0;
    if (Buffer.isBuffer(hwnd)) {
      hwndVal = hwnd.length >= 8 ? Number(hwnd.readBigUInt64LE(0)) : hwnd.readUInt32LE(0);
    } else if (typeof hwnd === "number") {
      hwndVal = hwnd;
    }
    // AccentState: 4 = ACCENT_ENABLE_BLURBEHIND（系统模糊）；0 = 关闭
    const policy = {
      AccentState: on ? 4 : 0,
      AccentFlags: 0,
      // GradientColor 用 0xAABBGGRR（深色半透明，与深色主题协调）
      GradientColor: on ? 0x9916181d : 0,
      AnimationId: 0,
    };
    // koffi.encode 到 Buffer，再取指针交给 WCAD.Data（与实测验证写法一致）
    const policyBuf = Buffer.alloc(ffi.koffi.sizeof(ffi.ACCENT_POLICY));
    ffi.koffi.encode(policyBuf, ffi.ACCENT_POLICY, policy);
    const data = {
      Attribute: 19, // WCA_ACCENT_POLICY
      Data: ffi.koffi.as(policyBuf, "void*"),
      SizeOfData: ffi.koffi.sizeof(ffi.ACCENT_POLICY),
    };
    const ok = ffi.SetWindowCompositionAttribute(Number(hwndVal), data);
    if (!ok) {
      return { ok: false, backend: "win32-accent", reason: "SetWindowCompositionAttribute 返回 false" };
    }
    return { ok: true, backend: "win32-accent" };
  } catch (err) {
    return { ok: false, backend: "win32-accent", reason: err && err.message };
  }
}

// ── plan-308-1555 M6：DWM 原生窗口圆角 ──
// "窗口缩小后变成直角边框"的根因：本应用是 frame:false 的无边框窗口，
// Windows **不会**自动给无边框窗口加圆角；CSS 的 border-radius 只在窗口背景透明处才看得出。
// zcode 的做法（已实测其实现）：build ≥ 22000 使用系统原生圆角，并把"是否最大化"
// 同步给渲染层（最大化时为直角，避免四角露出桌面）。
const DWMWA_WINDOW_CORNER_PREFERENCE = 33;
const DWMW_CORNER = { DEFAULT: 0, DONOTROUND: 1, ROUND: 2, ROUNDSMALL: 3 };

/** 是否支持系统原生圆角（与 zcode 的 supportsNativeWindowsRoundedCorners 同口径） */
function supportsNativeRoundedCorners() {
  const gd = glassDiag();
  if (!gd) return false;
  try {
    const w = gd.parseWindowsBuild(os.release());
    return Boolean(w && w.isWindows && w.build >= 22000);
  } catch {
    return false;
  }
}

/** 设置圆角偏好：最大化/全屏时必须 DONOTROUND（否则四角会露出桌面）。
 *  plan-26-126 P6：**同时消除 DWM 系统边框**（DWMWA_BORDER_COLOR=NONE）——
 *    实测取证：无边框窗口在 Win11 上仍有一圈 DWM 绘制的系统边框（物理 2px @1.5x），
 *    玻璃态下窗口内部透明，这圈边框就是用户看到的"一圈透出桌面"。两者必须一起设：
 *    只设圆角不设边框，四角圆了但四边仍留一圈系统色。 */
function applyWindowCorners(win) {
  if (!win || win.isDestroyed()) return { ok: false, reason: "窗口不可用" };
  const gd = glassDiag();
  if (!gd) return { ok: false, reason: "glass-diagnostics 不可用" };
  const ffi = gd.loadDwmFfi();
  const hwnd = gd.hwndOf(win);
  if (!ffi || hwnd === null) return { ok: false, reason: "FFI 或 HWND 不可用" };
  const out = { ok: true };
  // ① 系统边框一律不绘制（P6）。**不依赖圆角能力**：老系统上该属性会返回 hr!=0，
  //    此处只记录并继续，绝不影响窗口可用性。
  try {
    const b = (typeof gd.setWindowBorder === "function") ? gd.setWindowBorder(win, true) : { ok: false, reason: "no-api" };
    out.border = b.ok ? "none" : `skip(${b.reason || "unsupported"})`;
  } catch (err) {
    out.border = `skip(${err && err.message})`;
  }
  // ② 圆角：仅 Win11 build ≥ 22000 支持 DWMWA_WINDOW_CORNER_PREFERENCE
  if (!supportsNativeRoundedCorners()) {
    out.preference = "skip(build<22000)";
    return out;
  }
  try {
    const maximized = isMaximizedLike(win);
    const buf = Buffer.alloc(4);
    buf.writeInt32LE(maximized ? DWMW_CORNER.DONOTROUND : DWMW_CORNER.ROUND, 0);
    const hr = ffi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, buf, 4);
    out.preference = hr === 0 ? (maximized ? "DONOTROUND" : "ROUND") : `fail(hr=0x${(hr >>> 0).toString(16)})`;
  } catch (err) {
    out.preference = `fail(${err && err.message})`;
  }
  return out;
}

/** 把"是否最大化/是否有原生圆角"同步给渲染层（CSS 据此决定画不画圆角）。
 *  plan-26-126 P4（卡顿治理）：本函数只在窗口状态**真正变化**时才注入——
 *   历史上它被 resize/move/材质重放反复调用，每次都跑 FFI + 两次 executeJavaScript，
 *   是"拖窗口/拖面板卡顿甚至无响应"的直接原因。现在：
 *     * 用 _lastChromeState 记住上次下发值，未变化则直接返回（零成本）；
 *     * 单次注入（去掉原来的 120ms 重试——重试会与下次调用叠加，形成注入风暴）。 */
let _lastChromeState = null;
function syncWindowChromeState(win) {
  if (!win || win.isDestroyed()) return;
  const maximized = isMaximizedLike(win);
  const native = supportsNativeRoundedCorners();
  // plan-387-1825 A10：把「应用是否深色」并入状态键——主题切换会改变窗口非客户区配色，
  // 必须触发一次重放，否则小窗四周会一直停留在切换前的颜色。
  const dark = isAppDarkTheme();
  // plan-20261016-ux（问题 6）：外扩量并入状态键——最大化动画结束、尺寸落定后才是最终几何，
  // resized 收尾时再调用本函数即可补发（值未变则零成本返回）。
  const inset = clientInsetOf(win);
  const key = `${maximized ? 1 : 0}|${native ? 1 : 0}|${dark ? 1 : 0}|${inset.top},${inset.left},${inset.right}`;
  if (key === _lastChromeState) return; // 状态未变：不注入、不跑 FFI
  _lastChromeState = key;
  try {
    void win.webContents.executeJavaScript(
      `(() => { const r = document.documentElement;`
      + ` r.setAttribute('data-maximized', ${maximized ? "'1'" : "'0'"});`
      + ` r.setAttribute('data-native-corners', ${native ? "'1'" : "'0'"});`
      + ` r.style.setProperty('--win-inset-top', '${inset.top}px');`
      + ` r.style.setProperty('--win-inset-left', '${inset.left}px');`
      + ` r.style.setProperty('--win-inset-right', '${inset.right}px');`
      + ` return true; })()`, true);
  } catch { /* 渲染层未就绪时忽略 */ }
  applyWindowCorners(win);
  // plan-387-1825 A10：同步窗口**非客户区**的深/浅渲染。
  // frame:true 保留了 WS_CAPTION/WS_THICKFRAME，四周存在约 10 逻辑 px 的非客户区；
  // 它由 DWM 按系统主题绘制——系统浅色而应用深色时，小窗就会露出一圈白边。
  // 这里让非客户区跟随应用主题（对 frame:false 窗口为无害 no-op）。
  try {
    const gd = glassDiag();
    if (gd && typeof gd.setImmersiveDarkMode === "function") {
      const r = gd.setImmersiveDarkMode(win, dark);
      log("[chatcoder] 非客户区深色模式:", JSON.stringify(r));
    }
  } catch (err) {
    logErr("[chatcoder] 非客户区深色模式设置失败（不影响使用）:", err && err.message);
  }
}

/**
 * plan-26-116 M2：窗口状态变化后**多次重放**材质（取代原先的单次 reapplyGlass）。
 *
 * 为什么必须多次：DWM 在最大化 / 全屏 / 还原的**过渡动画期间**会重置窗口 backdrop，
 * 事件触发当下立刻调一次常落空——这正是「点最大化就透不出桌面、退出全屏也回不来」
 * 的直接原因。
 *
 * plan-31-152 S5-3（用户反馈"双击最大化后玻璃丢失、重放也没救回来"）：
 *   ① 旧的两档延时 0/160ms 不足：`titleBarStyle:"hidden"` + 原生最大化会**重建窗口非客户区**，
 *      高 DPI 屏上这一步可能耗时 300ms 以上，160ms 那档仍落在重建过程中（下发被 DWM 丢弃）。
 *      现改为 **100 / 320 / 600ms 三档**，最后一档覆盖慢速重建。
 *   ② 旧逻辑在过渡期回读必然读到旧值 → `applyGlass` 把本次判为 `ok:false` 并**阻断重试**。
 *      重放路径现传 `skipVerify`：先把材质设下去，校验只记录不阻断。
 *
 * 为什么用内存态而非读文件：重放属高频路径（resize 节流后仍会走），每次读
 * glass-pref.json 既有 IO 成本也存在「读到旧值」的时序风险；_glassWanted 在启动
 * 与 IPC 切换时同步维护。
 *
 * plan-31-151 S3：玻璃运动租约（_glassMotionHolds / _dragGlassMotion / _fitGlassMotion /
 *   _windowResizeMotion / _resizeGlassMotion / beginGlassMotion / endGlassMotion /
 *   replaceFitGlassMotion / suspendGlassForWindowDrag / restoreGlassAfterWindowDrag /
 *   releaseWindowResizeGlassMotion）已整体删除——原生最大化/拖拽由 DWM 全程合成，
 *   不再需要运动期材质保护。
 */

/** 立即重放材质（100/320/600ms 三档，覆盖 DWM 非客户区重建窗口期）。 */
function syncWindowGlassNow(win) {
  if (!win || win.isDestroyed()) return;
  syncWindowChromeState(win); // 圆角与最大化态：仅在真正变化时才有开销（P4）
  if (!_glassWanted) return;
  const delays = [100, 320, 600];
  delays.forEach((delay, i) => {
    setTimeout(() => {
      if (!win || win.isDestroyed() || !_glassWanted) return;
      try {
        // 窗口状态切换场景：跳过回读校验阻断，优先把材质设下去（见上方 ②）。
        const r = applyGlass(win, true, { skipVerify: true });
        if (i === delays.length - 1 && r && r.ok === false) {
          logErr("[chatcoder] glass: 三档重放后仍未生效 →", JSON.stringify(r));
        }
      } catch (err) {
        logErr("[chatcoder] glass: 材质重放异常:", err && err.message);
      }
    }, delay);
  });
}

/** 材质重放统一入口。plan-31-151 S3：玻璃运动租约已移除，不再做运动期抑制，
 *  保留函数签名（force 参数）以兼容调用方。 */
function syncWindowGlass(win, force) {
  if (!win || win.isDestroyed()) return;
  syncWindowGlassNow(win);
}

// ── plan-26-126 M4：伪最大化已整体移除（plan-31-151 S3）──
//   历史注释保留：效果保真目标（N13）与任务栏保真（N14）现由系统原生最大化直接满足。

/** 本地取 screen 模块（延迟到调用时，避免 app ready 之前访问）。 */
function screenModule() {
  try { return require("electron").screen; } catch { return null; }
}

/** 单一真源判据：系统原生最大化 / 全屏 任一为真。
 *  plan-31-151 S3：伪最大化已移除，isMaximizedLike 只读系统状态。
 *  圆角（DONOTROUND）与 data-maximized 注入全都改用它。 */
function isMaximizedLike(win) {
  if (!win || win.isDestroyed()) return false;
  try { return win.isMaximized() || win.isFullScreen(); } catch { return false; }
}

/** plan-20261016-ux（问题 6）：最大化 / 全屏时客户区超出可见区域的实际外扩量（逻辑像素）。
 *  旧做法假定外扩固定 8px，在 CSS 里给标题栏与导航区统一补 8px 上边距；而当前版本最大化时
 *  客户区与工作区精确贴合（本机实测 contentBounds === workArea，外扩为 0），
 *  这 8px 反而把顶部 logo、图标与标题整体压低约 4px。改为按真实几何计算，正常为 0；
 *  个别环境若仍外扩，按实际值补偿（上限 16px，防异常几何把内容推出视口）。 */
function clientInsetOf(win) {
  const zero = { top: 0, left: 0, right: 0 };
  if (!isMaximizedLike(win)) return zero;
  try {
    const scr = screenModule();
    if (!scr) return zero;
    const cb = win.getContentBounds();
    const disp = scr.getDisplayMatching(win.getBounds());
    const area = win.isFullScreen() ? disp.bounds : disp.workArea;
    const clamp = (v) => Math.max(0, Math.min(16, Math.round(v)));
    return {
      top: clamp(area.y - cb.y),
      left: clamp(area.x - cb.x),
      right: clamp(cb.x + cb.width - (area.x + area.width)),
    };
  } catch {
    return zero;
  }
}

// ── plan-31-151 S3：伪最大化辅助函数已整体删除 ──
//   workAreaOf / _movedAwayFromPseudoRect / _resolveRestoreBounds / restoredBoundsNearCursor
//   全部随伪最大化一并移除；系统原生最大化的 workArea 贴合与 drag-restore 由 Windows 处理。

/** ── plan-26-126 P6：窗口几何"运动中"标记 ──
 *
 * 为什么需要它（用户反馈"毛玻璃下拖尺寸卡顿 / 缩放动画不自然"的直接原因）：
 *   窗口几何每变一帧，渲染进程都要整体重排。本应用里最贵的一段是消息流的
 *   "宽度锚点补偿"——它在每次宽度变化时 capture + 双帧 rAF + 重新测量后写 scrollTop
 *   （见 MessageFlow 的宽度 RO）。窗口缩放/拖拽期间它每帧都跑，把本已紧张的 16ms 帧
 *   预算直接吃穿，于是动画掉帧、拖尺寸发涩。
 *   这里把"窗口正在运动"下发给渲染层，让它**跳过**这类"位置补偿"类重活——
 *   因为整窗缩放的中间帧里，保持内容锚点本来就没有意义（结束后再收敛一次即可）。
 *
 * 下发通道（plan-329-1647 S2 起）：**只走 IPC**（window:motion）。渲染层 App.tsx 的
 *   onWindowMotion 收到后自己写 `__chatcoderWindowMotion` / `data-window-motion` 并派发
 *   `chatcoder:window-motion`，运动结束那一刻的收尾（宽度锚点还原、终端 fit）由该事件驱动，
 *   不再依赖"120ms 轮询"——那正是用户反馈"拖完尺寸后内容收敛慢"的来源。
 *   历史上这里还并行做了一次 executeJavaScript 注入，与 IPC 写同一份状态，已删除（纯冗余）。
 *
 * plan-31-151 S6：驱动源由「伪最大化补间 / 自研拖拽」改为「系统 resize/move 事件 +
 *   settle timer」——窗口边缘缩放与标题栏拖拽都是系统行为，主进程只监听事件，
 *   resize/move 到来时置 true，静止 120ms 后置 false（与渲染层门控语义不变）。
 */
let _windowMotion = false;
let _motionSettleTimer = null;
const MOTION_SETTLE_MS = 120;
function setWindowMotion(win, active) {
  if (!win || win.isDestroyed()) return;
  if (_windowMotion === active) return;
  _windowMotion = active;
  try {
    win.webContents.send("window:motion", { active: !!active });
  } catch { /* 渲染层未就绪时忽略 */ }
}
/** 系统几何事件（resize/move）到来时调用：立即置运动态并重启静止计时器。 */
function noteWindowGeometryEvent(win) {
  setWindowMotion(win, true);
  if (_motionSettleTimer) clearTimeout(_motionSettleTimer);
  _motionSettleTimer = setTimeout(() => {
    _motionSettleTimer = null;
    setWindowMotion(win, false);
  }, MOTION_SETTLE_MS);
}

// ── plan-31-151 S3：fitContentBounds / cancelFitAnimation / contentToFrameRect /
//   fitStepIntervalMs / resetDragStepCache / dragStepMs / FULLSCREEN_ANIM_MS / _fitAnimTimer
//   已整体删除——自研 220ms 补间是 frame:false 架构下"系统动画缺失"的补偿，
//   本架构（frame:true）下最大化/还原动画由 DWM 原生渲染，不再需要逐帧 setBounds。

/** 最大化/还原入口（与原生最大化同语义：未最大化→最大化，已最大化→还原）。
 *
 *  plan-31-151 S2：统一走系统原生 maximize/unmaximize（含玻璃开启）。
 *
 *  plan-31-152 S5-2（用户反馈"右上角**只有**缩放按钮点击无反应，最小化/关闭正常"）：
 *    **根因**——S3 批量删除伪最大化代码时，本函数**定义被一并移除**，只留下了调用点。
 *    于是每次点击缩放按钮，主进程都抛 `ReferenceError: togglePseudoMaximize is not defined`，
 *    被 `process.on("uncaughtException")` 捕获后仅记日志，窗口零响应（看起来"点了没反应"）；
 *    而最小化/关闭按钮直接调 `mainWindow.minimize()/close()`，不经此函数，因此正常。
 *    修复：补回定义，并加两道保险：
 *      ① 状态回读校验（260ms 后核对 isMaximized 是否真的切换）——日志可判定是否生效；
 *      ② 未生效时走 Win32 `WM_SYSCOMMAND`（与系统双击标题栏/标题栏右键菜单**同一条路径**）
 *         兜底——覆盖 Electron API 被系统忽略（CanMaximize 判定等）的极端情况。 */
function togglePseudoMaximize(win) {
  if (!win || win.isDestroyed()) return;
  let wasMax = false;
  try { wasMax = win.isMaximized(); } catch { /* 窗口销毁竞态 */ }
  try {
    if (wasMax) win.unmaximize();
    else win.maximize();
  } catch (err) {
    logErr("[chatcoder] 最大化切换失败:", err && err.message);
    return;
  }
  // ① + ②：回读校验与系统命令兜底
  setTimeout(() => {
    if (!win || win.isDestroyed()) return;
    let nowMax = false;
    try { nowMax = win.isMaximized(); } catch { return; }
    if (nowMax === wasMax) {
      // Electron API 未生效：换用系统命令（SC_RESTORE / SC_MAXIMIZE）
      const gd = glassDiag();
      const cmd = wasMax ? 0xf120 : 0xf030;
      const r = gd && typeof gd.sendSysCommand === "function"
        ? gd.sendSysCommand(win, cmd)
        : { ok: false, reason: "glass-diagnostics 未提供 sendSysCommand" };
      log("[chatcoder] window:maximizeToggle 状态未变化，Win32 兜底:", JSON.stringify(r));
    } else {
      log("[chatcoder] window:maximizeToggle 生效: maximized =", nowMax);
    }
  }, 260);
}

// 玻璃偏好落盘：渲染进程偏好存 localStorage 主进程读不到，而 acrylic 材质必须
// 在窗口显示前（构造参数）决定，因此主进程侧在 IPC 时另存一份供下次启动读取。
const GLASS_PREF_FILE = path.join(app.getPath("userData"), "glass-pref.json");
function readGlassPref() {
  try {
    return JSON.parse(fs.readFileSync(GLASS_PREF_FILE, "utf8")).on === true;
  } catch { return false; }
}
function writeGlassPref(on) {
  try { fs.writeFileSync(GLASS_PREF_FILE, JSON.stringify({ on: !!on })); } catch {}
}

// ── plan-26-116：清理历史遗留的「折射液态玻璃」偏好文件 ──
// 该模式已整体移除；旧版本可能留下 liquid-glass-pref.json（on:true），
// 启动时覆写为关闭，避免旧偏好与「只保留毛玻璃」的新语义冲突。
// 注：electron/liquid-glass.cjs 仍保留在 electron 目录与打包清单中（不再被引用），
//     目的是避免「打包缺文件」类历史事故复现。
const LIQUID_PREF_FILE = path.join(app.getPath("userData"), "liquid-glass-pref.json");
function clearLegacyLiquidPref() {
  try { fs.writeFileSync(LIQUID_PREF_FILE, JSON.stringify({ on: false })); } catch { /* ignore */ }
}

// 主题偏好落盘（同 glass-pref 机制）：启动动画 loading.html 在前端加载前显示，
// 读不到 localStorage，因此渲染进程切主题时经 IPC 另存一份，启动时注入 loading 页。
const THEME_PREF_FILE = path.join(app.getPath("userData"), "theme-pref.json");
function readThemePref() {
  try {
    const t = JSON.parse(fs.readFileSync(THEME_PREF_FILE, "utf8")).theme;
    return t === "light" || t === "dark" ? t : null;
  } catch { return null; }
}
function writeThemePref(theme) {
  try { fs.writeFileSync(THEME_PREF_FILE, JSON.stringify({ theme: theme === "light" ? "light" : "dark", tone: readTonePref() })); } catch {}
}
// plan-114-447：主题由「色调」改为「世界」，仍存在 tone 字段里；旧色调值读取时迁移到对应世界
const TONE_VALUES = ["graphite", "forest", "campfire", "breeze", "tide", "sakura"];
const LEGACY_TONE_MAP = { mint: "forest", ocean: "tide", apricot: "campfire", lavender: "breeze" };
function normalizeTone(t) {
  if (TONE_VALUES.includes(t)) return t;
  return LEGACY_TONE_MAP[t] || "graphite";
}
function readTonePref() {
  try {
    return normalizeTone(JSON.parse(fs.readFileSync(THEME_PREF_FILE, "utf8")).tone);
  } catch { return "graphite"; }
}
function writeTonePref(raw) {
  const tone = normalizeTone(raw);
  const theme = readThemePref() || "dark";
  try { fs.writeFileSync(THEME_PREF_FILE, JSON.stringify({ theme, tone })); } catch {}
}

/** plan-387-1825 A10：应用主题是否为深色——窗口底色与非客户区渲染的**唯一判据**。
 *
 * 此前「窗口底色」与「窗口非客户区配色」是两个独立的东西：
 * 底色按 lightStart 决定，而非客户区（frame:true 保留的 WS_CAPTION/WS_THICKFRAME 边框）
 * 一直跟随**系统主题**，没人同步过。实测本机系统浅色 + 应用深色时，
 * 小窗四周因此留一圈白边（详见 glass-diagnostics.setImmersiveDarkMode 注释）。
 * 抽成单一函数，保证底色与边框配色同源、不会各按各的口径漂移。 */
function isAppDarkTheme() {
  const t = readThemePref();
  return t ? t === "dark" : nativeTheme.shouldUseDarkColors;
}

// ── UI 偏好备份落盘（plan-73-344）──
// 渲染层的外观/排版偏好（消息行距、字号、面板宽度等）存 localStorage，但 Chromium 的
// DOMStorage 是「内存缓存 + 异步批量提交」：app.exit 强退（一键重启）、多实例共享
// userData 的 LevelDB 锁竞争等场景下，最后一次写入在重启后可能不可见（用户实测：
// 行距改 2.2，重启回默认 1.7）。这里在主进程侧另存一份 JSON（fs.writeFileSync 同步
// 落盘，不依赖 Chromium 提交流水线），渲染层启动时与 localStorage 按 savedAt 取较新者。
const UI_PREFS_FILE = path.join(app.getPath("userData"), "ui-prefs.json");
let _uiPrefsPending = null; // 250ms 合并窗口内的最新偏好
let _uiPrefsTimer = null;

function writeUiPrefsBackup(payload) {
  try {
    if (!payload || typeof payload !== "object") return;
    fs.writeFileSync(UI_PREFS_FILE, JSON.stringify(payload));
  } catch { /* 磁盘异常不阻断偏好变更 */ }
}

/** 把挂起的偏好立即写盘（退出/重启/关窗前调用——合并窗口内的最后一次变更不丢） */
function flushUiPrefsBackup() {
  if (_uiPrefsTimer) { clearTimeout(_uiPrefsTimer); _uiPrefsTimer = null; }
  if (_uiPrefsPending) {
    writeUiPrefsBackup(_uiPrefsPending);
    _uiPrefsPending = null;
  }
}

function readUiPrefsBackup() {
  try { return JSON.parse(fs.readFileSync(UI_PREFS_FILE, "utf8")); } catch { return null; }
}

/** 把渲染进程未提交的 DOMStorage（localStorage）数据刷到磁盘。
 *  flushStorageData 触发写入但完成是异步的——调用后需留一小段时间再退出（见 app:relaunch）。 */
function flushDomStorage() {
  try {
    const { session } = require("electron");
    session.defaultSession.flushStorageData();
  } catch { /* ignore */ }
}

// ── plan-26-116：原生折射面板已整体移除 ──
// 历史实现（plan-24-106 M6）通过 electron/liquid-glass.cjs 在窗口 z 序下方钉一层原生
// 折射面板，依赖 `transparent: true` 且与系统 acrylic 互斥（切换需重启窗口），是「折射版
// 会闪 / 普通版有条纹 / 行为不一致」的来源；本轮只保留毛玻璃，故连同管理器一起删除。

// ── 创建主窗口 ──
function createWindow() {
  const win11 = isWin11Plus();
  const cap = blurBackend();
  // plan-26-116：只保留毛玻璃一种模式。Win11 = 非透明窗口 + DWM acrylic（与 transparent
  //   互斥）；Win10 = 透明窗口 + ACCENT 系统模糊（窗口可见后施加）。
  const glassOn = win11 && readGlassPref();
  // 材质意图的内存态：启动时由落盘偏好初始化，IPC 切换时更新（见 window:setGlass）。
  _glassWanted = glassOn;
  // 本窗口实际生效的玻璃状态（窗口生命周期内不变）：原生/自绘行为分流的唯一判据。
  _glassActive = glassOn;
  // 启动时清理历史遗留的折射偏好文件（该模式已移除）
  clearLegacyLiquidPref();
  // 启动期主题：优先用户上次偏好，否则跟随系统（loading 页与窗口底色保持一致，避免闪色）
  // plan-387-1825 A10：改走 isAppDarkTheme()，与窗口非客户区的深/浅渲染同一判据。
  const lightStart = !isAppDarkTheme();
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    // plan-31-151 S1：恢复系统窗口框架——frame:false 是原生特性（Snap 分屏、最大化
    //   过渡动画、边缘拖拽缩放、双击最大化、系统阴影、标题栏右键菜单）批量丢失的根因。
    //   titleBarStyle:"hidden" 隐藏原生标题栏视觉，但保留 WS_CAPTION/WS_THICKFRAME
    //   样式位 → 所有系统行为原生生效；thickFrame 保留缩放边框与 Aero Snap；
    //   roundedCorners 保留 Win11 原生圆角（最大化时由 syncWindowChromeState 改 DONOTROUND）。
    frame: true,
    titleBarStyle: "hidden",
    thickFrame: true,
    roundedCorners: true,
    // plan-31-151 S2：统一走系统原生最大化（含双击 / Snap / Win+↑ / 任务栏菜单）。
    //   原生最大化在 frame:true + backgroundMaterial:"acrylic" 架构下由 DWM 全程合成，
    //   玻璃不丢（plan-26-126「原生最大化丢玻璃」是 frame:false + transparent 架构的
    //   特有问题，本架构不成立——S2 阶段本机实测验证，若丢失则由 maximize 事件后
    //   applyGlass 重放兜底）。
    maximizable: true,
    // plan-548 + plan-308-1542：Win11 非透明窗口 + DWM acrylic（与 transparent 互斥）；
    // Win10 仍走透明窗口（系统模糊在 ready-to-show 后由 ACCENT 通道施加）；
    // glass off 时显式 "none"（默认 auto 可能被 DWM 施加 Mica）。
    transparent: !win11,
    backgroundMaterial: win11 ? (glassOn ? "acrylic" : "none") : undefined,
    // plan-24-106 M2 实测结论（更正 plan-24-105 R3 的推测，实测优先）：
    //   本机 Win11 22621 用「非透明窗口 + acrylic」逐个变体抓屏比对，窗口内部中心 RGB：
    //     A 省略 backgroundColor            → rgb(183,202,229)  透出桌面（蓝调保留）
    //     B backgroundColor:"#00000000"     → rgb(183,202,229)  透出桌面（蓝调保留）
    //     C backgroundColor:"#16181d"(不透明) → rgb(21,23,28)    ✗ 把 acrylic 整个盖住
    //   即：**带 alpha 的透明底色不会屏蔽 acrylic，反而是不透明底色会**。
    //   （官方文档只说"alpha 仅在 transparent:true 时受支持"，并未说透明底色会抑制材质；
    //     原推测"alpha 被忽略后落成不透明黑"与实测不符。）
    //   追加稳定性验证：resize / 放大 / 最小化-还原 后均为 rgb(183,202,230)，未复现 #48440 的变黑。
    //   ⇒ 这里保留 "#00000000"（玻璃开启时必须保持透明，才能看到材质），
    //     仅在玻璃关闭时下发不透明主题底色。
    //     plan-26-116：删去折射分支——玻璃开启必须保持透明底色才能看到材质。
    backgroundColor: win11 ? (glassOn ? "#00000000" : (lightStart ? "#f2f3f7" : "#16181d")) : undefined,
    // plan-548: 延迟到首帧就绪再显示——acrylic 需在窗口可见前应用，
    // 创建即显示会导致 backgroundMaterial 初始化失败（electron#38466）。
    show: false,
    // plan-31-151 S1：titleBarStyle 已在上方统一为 "hidden"（此处不再重复声明）。
    autoHideMenuBar: true,
    icon: resolveIconPath(),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webviewTag: true,
    },
  });
  // plan-308-1555 M7：开屏 loading.html 需要知道"毛玻璃是否开启"以决定玻璃/纯色样式
  const loadingGlassQuery = readGlassPref() ? "1" : "0";
  // plan-26-126 P6：**窗口显示前**就把系统边框设为不绘制。
  //   此时 HWND 已存在（构造完成），DWM 属性对隐藏窗口同样生效；
  //   提前设可避免"先看到一圈系统边框、稍后才消失"的闪烁。
  //   失败不影响任何功能（仅日志），ready-to-show 里还会再同步一次兜底。
  try {
    const gd0 = glassDiag();
    if (gd0 && typeof gd0.setWindowBorder === "function") {
      const rb0 = gd0.setWindowBorder(mainWindow, true);
      log("[chatcoder] glass: border =", JSON.stringify(rb0));
    }
  } catch (err) {
    logErr("[chatcoder] 窗口边框消除失败（不影响使用）:", err && err.message);
  }
  // plan-26-126 M6：开屏页还需知道"系统是否真有模糊材质"——没有时才用页面内轻量兜底模糊
  const loadingMatQuery = cap.backend !== "none" ? "1" : "0";
  // 说明（事故复盘）：这里曾经做过"窗口级 setOpacity(0) + 渐进到 1"的整窗淡入，
  // 一旦后续任一步骤抛异常（上一轮为打包漏文件），窗口会永久停在 opacity=0——
  // 表现为"任务栏有图标、预览有内容，但看不见也点不开"。
  // 现已移除窗口级淡入：开屏的淡入由 loading.html 页面内完成（观感一致），
  // 主进程不再碰窗口不透明度，从根上消除"窗口不可见"的可能。
  log("[chatcoder] glass: backend =", cap.backend, cap.reason ? `(reason: ${cap.reason})` : "");

  mainWindow.once("ready-to-show", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    // ① 显示窗口——这一步之后窗口必须是可见可交互的
    try {
      mainWindow.show();
    } catch (err) {
      logErr("[chatcoder] 显示窗口失败:", err && err.message);
    }
    // ② 保险：显式把不透明度钉到 1。
    //    plan-308-1555 事故复盘：此前这里做了"窗口级 setOpacity(0) 淡入"，
    //    而紧邻的 require("./glass-diagnostics.cjs") 因打包漏文件抛异常，
    //    导致后续"升回 1"的代码没执行 → 窗口永远停在 opacity=0：
    //    任务栏有图标、DWM 预览能渲染内容，但肉眼看不见也点不开。
    //    现在**彻底移除窗口级淡入**（页面内 loading.html 已有淡入，观感无损），
    //    并把这一行作为"窗口一定能被看见"的硬保证。
    try {
      if (typeof mainWindow.setOpacity === "function") mainWindow.setOpacity(1);
    } catch (err) {
      logErr("[chatcoder] 设置窗口不透明度失败:", err && err.message);
    }
    // ③ 装饰性步骤逐个独立 try：任一失败都不得影响窗口可用性
    try {
      // plan-308-1555 M6：首次同步圆角与最大化态（无边框窗口不会自动获得系统圆角）
      // plan-26-126 P6：同时消除 DWM 系统边框（修"四周一圈透出桌面"）
      syncWindowChromeState(mainWindow);
    } catch (err) {
      logErr("[chatcoder] 窗口圆角同步失败（不影响使用）:", err && err.message);
    }
    try {
      // plan-308-1542 需求4：非 Win11 的系统模糊必须在窗口可见后施加
      // （ACCENT/vibrancy 依赖已创建的 HWND；acrylic 已在构造参数中生效）。
      if (glassOn && cap.backend !== "dwm-acrylic") {
        const r = applyGlass(mainWindow, true);
        log("[chatcoder] glass: apply →", JSON.stringify(r));
      }
    } catch (err) {
      logErr("[chatcoder] 玻璃材质应用失败（不影响使用）:", err && err.message);
    }
  });

  // 兜底（plan-308-1555 修复）：即使 ready-to-show 从未触发（页面加载失败等），
  // 也不能让窗口停留在不可见状态——超时后强制显示并把不透明度钉到 1。
  setTimeout(() => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    try {
      if (!mainWindow.isVisible()) {
        logErr("[chatcoder] ready-to-show 超时，强制显示窗口（兜底）");
        mainWindow.show();
      }
      if (typeof mainWindow.setOpacity === "function") mainWindow.setOpacity(1);
    } catch { /* ignore */ }
  }, 8000);
  // 窗口状态变化后重放材质（最大化/还原会让 DWM/ACCENT 状态丢失）。
  // plan-24-106 M2：**移除 focus**——材质是窗口级 DWM 属性，不随焦点丢失；
  //   实测（ai/_m2_stability.cjs）最小化→还原、resize 后材质均完好（RGB 恒定 183,202,230）。
  // plan-26-116 M2：材质重放覆盖窗口状态事件。syncWindowGlass 内部会先同步圆角与最大化态。
  // plan-31-151 S2：maximize/unmaximize 统一走系统原生——同步圆角/data-maximized（DONOTROUND）
  //   并通知渲染层修复 8px 非客户区溢出；同时重放材质兜底（若原生最大化下 acrylic 被 DWM 重置）。
  // plan-73-344：关窗退出前把偏好落盘（关窗是最常见退出路径，同样要 flush——
  // 否则「改完设置立刻关窗」可能丢最后一次写入）。
  mainWindow.on("close", () => {
    flushUiPrefsBackup();
    flushDomStorage();
  });
  // plan-20261016-ux（问题 6）：随最大化状态一并下发真实外扩量（见 clientInsetOf），
  // 渲染层据此补偿标题栏，不再假定固定 8px。
  mainWindow.on("maximize", () => {
    syncWindowGlass(mainWindow);
    try { mainWindow.webContents.send("window:maximize-change", true, clientInsetOf(mainWindow)); } catch { /* 渲染层未就绪时忽略 */ }
  });
  mainWindow.on("unmaximize", () => {
    syncWindowGlass(mainWindow);
    try { mainWindow.webContents.send("window:maximize-change", false, clientInsetOf(mainWindow)); } catch { /* 渲染层未就绪时忽略 */ }
  });
  for (const ev of ["show", "restore", "enter-full-screen", "leave-full-screen"]) {
    mainWindow.on(ev, () => syncWindowGlass(mainWindow));
  }
  // plan-308-1555 M2：resize 也会丢材质；
  // plan-26-126 P4（卡顿治理）：**resize 不再重放材质**——拖动窗口时每帧重放（FFI + 回读）
  //   正是"拖尺寸卡顿甚至无响应"的主因。材质是窗口级 DWM 属性，实测 resize 不会丢；
  //   拖动结束后补一次即可。
  // plan-26-126 P6：再加一道"运动中不重放"——自研全屏动画 / 窗口拖拽期间由 motion 标记
  //   抑制，避免在高频几何变更中叠加 FFI + 回读（那是"毛玻璃下拖尺寸卡顿"的直接来源）。
  // ── resize 收尾调度（plan-329-1647 S2：双定时器合并为单一定时器）──
  // 唯一的「收尾泵」在 resize 开始后启动，按 60ms 节拍检查「距最后一次事件多久」，
  // 到点执行对应阶段后自行停止。plan-31-151 S6：阶段①解除 motion 已由
  //   noteWindowGeometryEvent 的 settle timer 接管（120ms），本泵只保留阶段②材质重放。
  const RESIZE_GLASS_REAPPLY_MS = 240;
  const RESIZE_TICK_MS = 60;
  let _resizeSettleTimer = null;
  let _lastResizeAt = 0;

  function stopResizeSettle() {
    if (_resizeSettleTimer) { clearTimeout(_resizeSettleTimer); _resizeSettleTimer = null; }
  }
  function resizeSettleTick() {
    _resizeSettleTimer = null;
    const quiet = Date.now() - _lastResizeAt;
    // 静止满 240ms → 补一次材质重放（仅非 acrylic 后端需要；acrylic 全程保留）。
    if (quiet >= RESIZE_GLASS_REAPPLY_MS) {
      if (!_windowMotion && (!_glassActive || blurBackend().backend !== "dwm-acrylic")) {
        syncWindowGlass(mainWindow, true);
      }
      return; // 完成：泵停止，等下一次 resize 事件重新启动
    }
    _resizeSettleTimer = setTimeout(resizeSettleTick, RESIZE_TICK_MS);
  }
  mainWindow.on("resize", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    // plan-31-151 S6：resize 期间统一置 motion 标记（渲染层跳过位置补偿类重活），
    //   静止 120ms 后由 settle timer 解除；材质在 resize 落定后补一次重放。
    noteWindowGeometryEvent(mainWindow);
    _lastResizeAt = Date.now();
    if (!_resizeSettleTimer) _resizeSettleTimer = setTimeout(resizeSettleTick, RESIZE_TICK_MS);
  });
  /** resized：窗口尺寸**落定**后触发一次（Windows/macOS 支持）。
   *  比固定延时准确：用户一松手就解除运动标记并补材质，渲染层当场收尾
   *  （宽度锚点还原 / 终端 fit），不再白等一个固定时长。
   *  渲染层的收尾由 setWindowMotion(false) 派发的 chatcoder:window-motion 事件驱动。 */
  // plan-31-152 S5-3③：最大化状态切换的**兜底重放**。
  //   原生最大化/还原会重建窗口非客户区（titleBarStyle:"hidden" 下尤其明显），
  //   高 DPI 屏上重建可能晚于 maximize 事件的 600ms 档。这里在 resized（尺寸落定）
  //   之后再补一次，覆盖"落定后才完成 DWM 重建"的极端时序——用户实测的
  //   "双击最大化玻璃丢失、退出也不恢复、重启才回来"正需要这条兜底。
  let _prevMaxState = isMaximizedLike(mainWindow);
  mainWindow.on("resized", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    stopResizeSettle();
    if (_motionSettleTimer) { clearTimeout(_motionSettleTimer); _motionSettleTimer = null; }
    setWindowMotion(mainWindow, false);
    // 最大化 ⇄ 还原：状态变化后补一次材质重放（skipVerify：不阻断）
    const nowMax = isMaximizedLike(mainWindow);
    if (nowMax !== _prevMaxState) {
      _prevMaxState = nowMax;
      if (_glassWanted) {
        setTimeout(() => {
          if (!mainWindow || mainWindow.isDestroyed() || !_glassWanted) return;
          log("[chatcoder] glass: 最大化状态切换兜底重放（maximized =", nowMax, "）");
          syncWindowGlassNow(mainWindow);
        }, 400);
      }
    }
    // acrylic 全程保留，无需材质补偿；仅非 acrylic 后端补一次。
    if (!_glassActive || blurBackend().backend !== "dwm-acrylic") syncWindowGlass(mainWindow, true);
  });
  // plan-31-151 S3：伪最大化下的 move→drag-restore 逻辑已随伪最大化一并移除。
  //   标题栏拖拽由系统原生处理（-webkit-app-region:drag），move 事件只用于 motion 标记。
  mainWindow.on("move", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized() || !mainWindow.isVisible()) return;
    noteWindowGeometryEvent(mainWindow);
  });
  try {
    const { screen } = require("electron");
    const onDisplayChange = () => {
      _lastChromeState = null; // 圆角/最大化态需重算（P4：清缓存以强制重新注入）
      syncWindowGlass(mainWindow);
    };
    screen.on("display-metrics-changed", onDisplayChange);
    screen.on("display-removed", onDisplayChange);
  } catch { /* 非致命：拿不到 screen 模块时跳过 */ }

  // plan-26-116：折射面板几何同步（syncLiquid）已随折射模式一并移除。

  // plan-114-450：每次导航完成后重注入窗口状态属性（兜底）。
  //   属性写在**当前文档**的 <html> 上，导航会换掉整个文档，而 syncWindowChromeState
  //   有下发缓存（_lastChromeState）——缓存命中即直接 return，主界面属性因此缺失。
  //   这里清缓存强制重注入；preload 已在文档解析前同步写入一次，本监听保证任何加载
  //   路径（含 dev server / 加载失败重试）下属性最终就位。
  mainWindow.webContents.on("dom-ready", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    _lastChromeState = null; // 文档已被替换：清缓存以强制重新注入
    try { syncWindowChromeState(mainWindow); } catch { /* 渲染层未就绪时忽略 */ }
  });

  // plan-308-1555 M0：启动即打印一次诊断结论（历史教训：只记"ok"不够，
  // 必须记录**系统回读值**才能在事后判定材质到底有没有生效）
  mainWindow.webContents.once("did-finish-load", async () => {
    try {
      const gd = glassDiag();
      if (!gd) return;
      const env = gd.envReport(app);
      const rb = gd.dwmReadBack(mainWindow);
      const winInfo = gd.windowReport(mainWindow, _glassRecorded);
      log("[chatcoder] glass: env =", JSON.stringify({
        release: env.release,
        windows: env.windows,
        electron: env.electron,
        chrome: env.chrome,
        isPackaged: env.isPackaged,
      }));
      log("[chatcoder] glass: window =", JSON.stringify(winInfo));
      log("[chatcoder] glass: dwm-readback =", JSON.stringify(rb));
    } catch (err) {
      logErr("[chatcoder] glass: 启动诊断失败:", err && err.message);
    }
  });

  // 先加载本地 loading.html(不用 data: URL)；query 注入主题与毛玻璃开关，
  // 供启动页做深浅色与"玻璃/纯色"两种样式适配（plan-308-1555 M7）
  const loadingPath = path.join(__dirname, "loading.html");
  if (fs.existsSync(loadingPath)) {
    mainWindow.loadFile(loadingPath, {
      query: { theme: lightStart ? "light" : "dark", tone: readTonePref(), glass: loadingGlassQuery, mat: loadingMatQuery },
    });
  }

  // 诊断
  mainWindow.webContents.on("did-fail-load", (_e, code, desc, url) => {
    logErr("[chatcoder] did-fail-load:", code, desc, url);
  });
  mainWindow.webContents.on("console-message", (_e, level, message, line, sourceId) => {
    log("[chatcoder][console:" + level + "] " + message + " (" + sourceId + ":" + line + ")");
  });
  mainWindow.webContents.on("render-process-gone", (_e, details) => {
    logErr("[chatcoder] render-process-gone:", details);
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) {
      shell.openExternal(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });

  // plan-20261015-browser-agent：右侧浏览器面板的 webview 一律是普通网页沙箱——
  // 剥离预载与 Node 集成；创建时即关闭后台节流（面板隐藏/窗口最小化时 AI 仍可在后台操作页面）。
  // 具体规则（含微应用宿主桥 preload 的条件注入）在模块级 hardenWebviewAttach 里，
  // 微应用独立窗口复用同一份实现。
  mainWindow.webContents.on("will-attach-webview", hardenWebviewAttach);

  // plan-693-2868 · docs/app1/11-host-bridge-protocol.md §6：宿主桥需要「uid → guest webContents」
  // 映射与上行转发，实现在 browser-agent.cjs（与 panelGuest 共用同一套 guest 校验口径）。
  // 必须建窗后立刻挂：did-attach-webview 只对「已注册监听的宿主」生效，挂晚了这一次 attach 就漏了。
  try {
    if (browserAgentApi) browserAgentApi.hookMainWindow(mainWindow.webContents);
  } catch (err) {
    logErr("[chatcoder] 宿主桥 hook 挂载失败:", err && err.message);
  }

  // 窗口重新获得焦点：交由 OS 与 Chromium 自动恢复文档焦点，绝不手动抢焦点
  mainWindow.on("focus", () => {
    // 不再调用 webContents.focus()：会与 <webview> guest 焦点协商并重置
    // Windows TSF/IME 关联，导致输入框光标高频闪烁与全局输入卡死。
    try { /* 保留空回调，仅消费事件 */ } catch { /* 窗口销毁竞态 */ }
  });

  mainWindow.on("closed", () => {
    // plan-31-151 S3：伪最大化状态已移除，closed 只需清理 motion settle timer 与主窗口引用。
    if (_motionSettleTimer) { clearTimeout(_motionSettleTimer); _motionSettleTimer = null; }
    stopResizeSettle();
    // 微应用独立窗口跟随主窗口一起收：否则关掉主窗口后本应用只剩一个微应用窗口，
    // 既没有入口也没有退出路径（window-all-closed 也不会触发）。
    closeAllMicroAppWindows();
    mainWindow = null;
  });
}

// ── 图标路径解析 ──
function resolveIconPath() {
  // 开发模式
  const devIcon = path.join(__dirname, "build", "icon.png");
  if (fs.existsSync(devIcon)) return devIcon;
  // 打包后:exe 同目录或 resources
  if (app.isPackaged) {
    const pkgIcon = path.join(process.resourcesPath, "icon.png");
    if (fs.existsSync(pkgIcon)) return pkgIcon;
  }
  return undefined;
}

// ── 加载前端页面 ──
function loadFrontend() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const frontDir = resolveFrontendDir();
  const indexPath = path.join(frontDir, "index.html");
  if (fs.existsSync(indexPath)) {
    log("[chatcoder] 加载前端:", indexPath);
    mainWindow.loadFile(indexPath);
  } else {
    log("[chatcoder] 前端不存在,尝试 dev server");
    mainWindow.loadURL("http://localhost:5173");
  }
}

// ── IPC:目录选择 ──
ipcMain.handle("dialog:selectDirectory", async () => {
  const result = await dialog.showOpenDialog({
    properties: ["openDirectory", "createDirectory"],
    title: "选择工作目录",
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

// ── IPC:多选 md 文件（v1.1: 本地技能导入；v19: 支持目录+文件混合选择）──
ipcMain.handle("dialog:selectFiles", async (_e, filters, opts) => {
  const properties = ["openFile", "multiSelections"];
  if (opts && opts.allowDirectories) properties.push("openDirectory");
  const result = await dialog.showOpenDialog({
    properties,
    filters: filters && filters.length ? filters : [{ name: "Markdown", extensions: ["md"] }],
  });
  return result.canceled ? [] : result.filePaths;
});

// ── IPC:后端端口透传（v2.1: 前端 BASE 去硬编码）──
ipcMain.handle("backend:getPort", () => BACKEND_PORT);

// ── plan-26-116：毛玻璃诊断 IPC（window:glassDiagnostics）已移除 ──
// 诊断/回读/自检属「提示性信息」体系，本轮从用户可见面（设置页 + IPC）整体撤下；
// electron/glass-diagnostics.cjs 仍保留（它提供 DWM FFI 与启动日志，属主进程内部依赖）。

// ── IPC:v19 外挂插件扫描（~/.chatcoder/plugins/<dir>/plugin.json + entry 源码）──
ipcMain.handle("plugins:list", () => {
  const fs = require("fs");
  const path = require("path");
  const os = require("os");
  const root = path.join(os.homedir(), ".chatcoder", "plugins");
  const out = [];
  try {
    if (!fs.existsSync(root)) return out;
    for (const dir of fs.readdirSync(root)) {
      const manifestPath = path.join(root, dir, "plugin.json");
      try {
        if (!fs.existsSync(manifestPath)) continue;
        const m = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
        if (!m.id || !m.slot || !m.entry) continue;
        const entryPath = path.join(root, dir, m.entry);
        if (!fs.existsSync(entryPath)) continue;
        out.push({
          id: String(m.id), name: String(m.name || m.id), slot: String(m.slot),
          description: String(m.description || ""),
          code: fs.readFileSync(entryPath, "utf-8"),
        });
      } catch (e) {
        log("[plugins] 读取插件失败 " + dir + ": " + (e && e.message));
      }
    }
  } catch { /* ignore */ }
  return out;
});

// ── plan-external-microapps · docs/app1/13-external-microapps.md ──
// 外置微应用宿主：扫描 ~/.chatcoder/microapps-ext/<appId>/microapp.json，按声明拉起进程、
// 轮询 health 判定就绪，并提供一条 HTTP 调用通道给 AI。与内置微应用（app:// + 宿主桥）并存。
const { createExternalMicroapps } = require("./external-microapps.cjs");
const externalMicroapps = createExternalMicroapps({
  log: (...args) => log("[microapps-ext]", ...args),
  logErr: (...args) => logErr("[microapps-ext]", ...args),
});

ipcMain.handle("microappsExt:list", () => {
  try { return { ok: true, apps: externalMicroapps.list() }; }
  catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
});
ipcMain.handle("microappsExt:start", async (_event, id) => {
  try { return await externalMicroapps.start(id); }
  catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
});
ipcMain.handle("microappsExt:stop", (_event, id) => {
  try { return externalMicroapps.stop(id); }
  catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
});
ipcMain.handle("microappsExt:status", (_event, id) => {
  try { return { ok: true, status: externalMicroapps.statusOf(id) }; }
  catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
});
ipcMain.handle("microappsExt:call", async (_event, id, method, args) => {
  try { return await externalMicroapps.call(id, method, args); }
  catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
});

// ── 微应用独立窗口（拓展页 / 已安装列表 / 右侧面板「在新窗口打开」）──
//
// 为什么需要独立窗口：微应用此前只能开在右侧面板的标签里，而右侧面板按会话分桶——
// 用户不在任何会话里（首页 / 设置页）时就看不到它，表现为「点了没反应」。
// 独立窗口是应用级窗口，与会话解耦：打开就能看到界面。
//
// 两种承载方式，按来源分流：
//  · 外置微应用（自带前后端）：直接 loadURL 它自己的 http 地址，不注入宿主桥——它有自己的后端；
//  · 内置微应用（app:// + 宿主桥）：加载本应用前端里的 microapp.html 外壳，外壳里放一个
//    `<webview partition="persist:microapp">`，与右侧面板里的承载方式逐字一致（同一份
//    hardenWebviewAttach 规则、同一个分区），所以宿主桥照常工作。
//    窗口里的实例 uid 由渲染层派生为 `<标签uid>-w`（见 client/src/utils/microappWindow.ts），
//    它与面板里的那个标签是两个独立实例，互不抢地址；上行请求仍由**主窗口**渲染层转后端
//    （uid → sessionId 的绑定在那边，会话上下文因此不丢）。
const microAppWindows = new Map(); // `builtin:<appId>` / `external:<appId>` → BrowserWindow

function microAppWindowKey(kind, appId) {
  return (kind === "external" ? "external:" : "builtin:") + String(appId || "");
}

/** 已经开着就唤醒它（最小化时先还原），符合「重复打开要聚焦已有窗口」的预期。 */
function focusMicroAppWindow(win) {
  if (!win || win.isDestroyed()) return false;
  try {
    if (win.isMinimized()) win.restore();
    if (!win.isVisible()) win.show();
    win.focus();
  } catch { return false; }
  return true;
}

function closeAllMicroAppWindows() {
  for (const win of Array.from(microAppWindows.values())) {
    try { if (!win.isDestroyed()) win.close(); } catch { /* 退出路径尽力而为 */ }
  }
  microAppWindows.clear();
}

/** 打开（或聚焦）一个微应用独立窗口。opts 形状见 client/src/types/global.d.ts 的 MicroAppWindowOpts。 */
function openMicroAppWindow(opts) {
  const o = opts && typeof opts === "object" ? opts : {};
  const external = o.kind === "external";
  const appId = String(o.appId || "").trim();
  if (!appId) return { ok: false, error: "缺少 appId" };
  if (external && !/^https?:\/\//i.test(String(o.url || ""))) {
    return { ok: false, error: "外置微应用地址无效：" + String(o.url || "") };
  }
  const key = microAppWindowKey(external ? "external" : "builtin", appId);
  if (focusMicroAppWindow(microAppWindows.get(key))) return { ok: true, key, reused: true };

  const name = String(o.name || appId).trim() || appId;
  let win;
  try {
    win = new BrowserWindow({
      width: 1100,
      height: 780,
      minWidth: 480,
      minHeight: 360,
      title: name + " · 微应用",
      autoHideMenuBar: true,
      // 全屏是用户明确要求的（F11 切换，Esc 退出；见下方 before-input-event）
      fullscreenable: true,
      backgroundColor: "#16181d",
      show: false,
      icon: resolveIconPath(),
      webPreferences: external
        // 外置微应用是第三方页面：不给 preload、不开 Node，与面板 webview 同等沙箱
        ? { contextIsolation: true, nodeIntegration: false, sandbox: true }
        // 内置微应用的窗口外壳是本应用自己的页面：需要 chatcoderAPI（宿主桥 IPC / 全屏控制）
        : { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false, sandbox: false, webviewTag: true },
    });
  } catch (err) {
    return { ok: false, error: "创建窗口失败：" + ((err && err.message) || err) };
  }

  microAppWindows.set(key, win);

  // 与主窗口同一份 webview 加固：内置窗口里的 webview 必须拿到宿主桥 preload，
  // 外置窗口里没有 webview，挂了也无副作用。
  win.webContents.on("will-attach-webview", hardenWebviewAttach);
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(String(url || ""))) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("did-fail-load", (_e, code, desc, url, isMainFrame) => {
    logErr("[chatcoder] 微应用窗口加载失败:", code, desc, url);
    // 外置微应用的前端地址来自 15s TTL 的清单：进程若在这段时间里退出，窗口就会拿到一个
    // 已经死掉的地址。这里必须让用户看见，否则只剩一个白窗口。
    //  -3 = ERR_ABORTED（正常导航被打断），子框架失败也不算窗口失败，两种情况都不提示。
    if (!isMainFrame || code === -3) return;
    try {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("microapp:windowLoadFailed", { key, appId, name, code, desc, url });
      }
    } catch { /* 主窗口已关：应用正在退出，没人需要这条提示 */ }
  });
  // 全屏：F11 切换；已全屏时 Esc 退出（外置微应用页面没有宿主 UI，快捷键是唯一通用入口）。
  // 内置窗口里 `<webview>` 铺满整个窗口，键盘事件进的是**guest** 而不是宿主 webContents，
  // 所以同一个处理函数必须挂在两处：宿主（焦点在页面其它区域时）+ 每个 guest
  // （`did-attach-webview` 补挂，焦点进了微应用页面才算覆盖到）。
  const onFullscreenKey = (event, input) => {
    if (!input || input.type !== "keyDown") return;
    try {
      if (input.key === "F11") {
        event.preventDefault();
        win.setFullScreen(!win.isFullScreen());
      } else if (input.key === "Escape" && win.isFullScreen()) {
        event.preventDefault();
        win.setFullScreen(false);
      }
    } catch { /* 窗口销毁竞态 */ }
  };
  win.webContents.on("before-input-event", onFullscreenKey);
  win.webContents.on("did-attach-webview", (_e, guest) => {
    // guest 销毁时它的监听随之失效，不需要手动解绑
    try { guest.on("before-input-event", onFullscreenKey); } catch { /* guest 已分离 */ }
  });
  // 全屏态回传：内置窗口的外壳据此切换按钮图标与说明（外置页面没有渲染层可收）
  const pushFullscreen = () => {
    try { if (!win.isDestroyed()) win.webContents.send("microapp:windowFullscreen", win.isFullScreen()); } catch { /* 未就绪忽略 */ }
  };
  win.on("enter-full-screen", pushFullscreen);
  win.on("leave-full-screen", pushFullscreen);

  win.on("closed", () => {
    microAppWindows.delete(key);
    // 通知主窗口渲染层释放该窗口实例的宿主桥绑定（uid 绑定不释放会一直占着那张表）
    try {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("microapp:windowClosed", { key, appId });
    } catch { /* 主窗口已关：应用正在退出，无需清理 */ }
  });

  if (!external) {
    // 登记为微应用宿主窗口：它里面的 webview 也是微应用 guest，AI 的 microapp_call 要能找到它
    try { if (browserAgentApi) browserAgentApi.hookMicroappHostWindow(win.webContents); }
    catch (err) { logErr("[chatcoder] 微应用窗口宿主登记失败:", (err && err.message) || err); }
  }

  const query = {
    appId,
    version: String(o.version || ""),
    entry: String(o.entry || ""),
    uid: String(o.uid || ""),
    sessionId: o.sessionId == null ? "" : String(o.sessionId),
    name,
  };
  if (external) {
    win.loadURL(String(o.url));
  } else {
    const file = path.join(resolveFrontendDir(), "microapp.html");
    if (fs.existsSync(file)) {
      win.loadFile(file, { query });
    } else {
      // 开发态（未跑过 build）：与主窗口同一套降级——走 vite dev server
      win.loadURL("http://localhost:5173/microapp.html?" + new URLSearchParams(query).toString());
    }
  }
  win.once("ready-to-show", () => {
    try { if (!win.isDestroyed()) win.show(); } catch { /* 已销毁 */ }
  });
  return { ok: true, key, reused: false };
}

ipcMain.handle("microapp:openWindow", (_event, opts) => {
  try { return openMicroAppWindow(opts); }
  catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
});

/** 内置微应用窗口外壳的「全屏」按钮（外置页面没有渲染层，走 F11）。 */
ipcMain.handle("microapp:windowFullscreenToggle", (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (!win || win.isDestroyed()) return { ok: false, fullscreen: false };
  win.setFullScreen(!win.isFullScreen());
  return { ok: true, fullscreen: win.isFullScreen() };
});

// ── plan-20261017-goal12 项 12：Cordis 兼容插件宿主（dsh 插件协议）──
const { createHost: createCordisHost } = require("./cordis-host.cjs");
const { startPluginBridge } = require("./plugin-bridge.cjs");
// 第三方插件安装在 ~/.chatcoder/plugins/<包名安全化>/（dsh 包格式，组合文件由 dsh.bundle.patch 指定）；
// 安装、加载、启停与卸载都在主进程完成，界面通过 IPC 发起操作并读取状态。
const cordisHost = createCordisHost({
  log: (...args) => log("[cordis]", ...args),
  logErr: (...args) => logErr("[cordis]", ...args),
  sendToRenderer: (channel, payload) => {
    try { if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload); } catch { /* 渲染层未就绪时忽略 */ }
  },
});
ipcMain.handle("cordis:status", () => cordisHost.status());
/** 插件集合变化（安装 / 启停 / 卸载 / 重载）后广播给所有窗口：设置页与插件界面即时刷新，无需重开页面。 */
function broadcastCordisChanged() {
  const plugins = cordisHost.status();
  for (const win of BrowserWindow.getAllWindows()) {
    try { if (!win.isDestroyed()) win.webContents.send("cordis:changed", plugins); } catch { /* 未就绪窗口忽略 */ }
  }
}
ipcMain.handle("cordis:reload", async () => {
  const r = await cordisHost.load();
  void syncPluginConnector();
  broadcastCordisChanged();
  return r;
});

// 第三方插件的工具经本机桥接暴露给 Agent：桥接在主进程启动，连接器（MCP 桥）登记在后端。
const PLUGIN_CONNECTOR = "dsh-plugins";
let pluginBridge = null;
async function ensurePluginBridge() {
  if (!pluginBridge) {
    pluginBridge = await startPluginBridge({ host: cordisHost, root: cordisHost.root(), log: (...a) => log("[cordis]", ...a) });
  }
  return pluginBridge;
}
function backendApi(pathname, init = {}) {
  return fetch(`http://127.0.0.1:${BACKEND_PORT}/api${pathname}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init.headers || {}) },
  }).then(async (r) => {
    if (!r.ok) throw new Error(`后端返回 ${r.status}`);
    return r.status === 204 ? null : r.json();
  });
}
/** 同步「第三方插件」连接器：登记或修正启动参数；已启用时刷新工具清单（插件增删后 Agent 立即看到变化）。 */
async function syncPluginConnector() {
  try {
    const bridge = await ensurePluginBridge();
    const want = {
      command: process.execPath,
      args: [path.join(__dirname, "plugin-mcp.cjs"), bridge.file],
      env: { ELECTRON_RUN_AS_NODE: "1" },
    };
    const rows = (await backendApi("/mcp-servers?source=plugin")) || [];
    let row = rows.find((r) => r.name === PLUGIN_CONNECTOR);
    if (!row) {
      row = await backendApi("/mcp-servers", {
        method: "POST",
        body: JSON.stringify({
          name: PLUGIN_CONNECTOR,
          display_name: "第三方插件",
          description: "在应用内安装的 dsh 第三方插件提供的工具。插件安装后，在此启用连接器即可让 AI 调用。",
          source: "plugin",
          transport: "stdio",
          ...want,
          is_active: false,
        }),
      });
    } else if (JSON.stringify(row.args) !== JSON.stringify(want.args) || row.command !== want.command) {
      await backendApi(`/mcp-servers/${row.id}`, { method: "PATCH", body: JSON.stringify(want) });
    }
    if (row && row.is_active) await backendApi(`/mcp-servers/${row.id}/refresh-tools`, { method: "POST" });
  } catch (e) {
    logErr("[cordis] 同步第三方插件连接器失败：", e && e.message);
  }
}
// 安装：选择插件目录或 npm pack 生成的 .tgz；校验失败时把原因原样返回给界面
ipcMain.handle("cordis:install", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "选择 dsh 插件",
    properties: ["openFile", "openDirectory"],
    filters: [{ name: "插件包", extensions: ["tgz"] }],
  });
  if (result.canceled || !result.filePaths[0]) return { ok: false, canceled: true };
  try {
    const plugin = await cordisHost.install(result.filePaths[0]);
    void syncPluginConnector();
    broadcastCordisChanged();
    return { ok: true, plugin, plugins: cordisHost.status() };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});
ipcMain.handle("cordis:uninstall", async (_e, name) => {
  try {
    await cordisHost.uninstall(String(name));
    void syncPluginConnector();
    broadcastCordisChanged();
    return { ok: true, plugins: cordisHost.status() };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});
ipcMain.handle("cordis:setEnabled", async (_e, name, enabled) => {
  try {
    const plugin = await cordisHost.setEnabled(String(name), Boolean(enabled));
    void syncPluginConnector();
    broadcastCordisChanged();
    return { ok: true, plugin, plugins: cordisHost.status() };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

// ── IPC:系统集成 ──
ipcMain.handle("shell:openPath", (_event, p) => {
  if (p) shell.openPath(p);
});
ipcMain.handle("shell:showItemInFolder", (_event, p) => {
  if (!p) return { ok: false, error: "路径为空" };
  const target = path.normalize(String(p));
  try {
    if (fs.existsSync(target)) {
      shell.showItemInFolder(target);
      return { ok: true };
    }
    // 文件已被删除（变更卡片的「已删除」行）或路径失效时，showItemInFolder
    // 在 Windows 上静默无效，用户会以为「点了没反应」。回退打开最近存在的父目录。
    let dir = path.dirname(target);
    while (dir && !fs.existsSync(dir) && path.dirname(dir) !== dir) dir = path.dirname(dir);
    if (dir && fs.existsSync(dir)) {
      void shell.openPath(dir);
      return { ok: true, fallback: "dir" };
    }
    return { ok: false, error: "路径不存在" };
  } catch (e) {
    return { ok: false, error: (e && e.message) || String(e) };
  }
});
// ── plan-308-1542 需求5：外部应用启动器解析（"从 IDEA 打开"点击无反应）──
// 原实现 `spawn("idea", [p], { shell: true })` 后**立即 return true**，不监听 error：
// PATH 里没有 idea / .bat 经 shell 启动失败时静默无事发生，用户完全无感知。
// 现在：①解析启动器（用户配置 → PATH → 常见安装路径 → 注册表 App Paths → 协议回退）；
//       ②按扩展名选正确的启动方式（.bat/.cmd 走 cmd start；.exe 直接 spawn）；
//       ③监听 error/exit，失败时返回可读原因给前端提示，并支持用户手动指定可执行文件。
const EXTERNAL_APPS_FILE = path.join(app.getPath("userData"), "external-apps.json");

function readExternalApps() {
  try {
    const raw = JSON.parse(fs.readFileSync(EXTERNAL_APPS_FILE, "utf8"));
    return raw && typeof raw === "object" ? raw : {};
  } catch { return {}; }
}

function writeExternalApps(data) {
  try { fs.writeFileSync(EXTERNAL_APPS_FILE, JSON.stringify(data, null, 2)); } catch (e) {
    logErr("[chatcoder] 写入 external-apps.json 失败:", e && e.message);
  }
}

const _APP_CANDIDATES = {
  idea: ["idea.bat", "idea64.exe", "idea.exe", "idea"],
  vscode: ["code.cmd", "code.bat", "code.exe", "code"],
};
const _APP_DISPLAY = { idea: "IntelliJ IDEA", vscode: "VS Code" };

/** 在 PATH 中解析可执行文件（不依赖 `where`，避免额外进程）。 */
function whichInPath(name) {
  const dirs = (process.env.PATH || "").split(path.delimiter).filter(Boolean);
  for (const d of dirs) {
    const full = path.join(d, name);
    try { if (fs.existsSync(full)) return full; } catch { /* ignore */ }
  }
  return null;
}

/** 扫描常见安装目录（含本机实测命中：D:\javaEnvironment\*\bin）。 */
function scanCommonInstallPaths(appName) {
  const found = [];
  const names = _APP_CANDIDATES[appName] || [];
  const roots = [];
  if (process.platform === "win32") {
    const local = process.env.LOCALAPPDATA || "";
    const pf = process.env.ProgramFiles || "C:\\Program Files";
    const pf86 = process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";
    roots.push(
      path.join(local, "JetBrains", "Toolbox", "apps"),
      path.join(local, "Programs"),
      path.join(pf, "JetBrains"),
      path.join(pf86, "JetBrains"),
      "D:\\javaEnvironment",
      "C:\\javaEnvironment",
    );
  }
  for (const root of roots) {
    try {
      if (!root || !fs.existsSync(root)) continue;
      // 最多两层深度（Toolbox: apps/<product>/<channel>/bin/idea64.exe 会超出，
      // 故对 Toolbox 根额外深一层；这里统一用递归限深搜索，够用且可控）
      const hits = _searchLauncher(root, names, 4);
      found.push(...hits);
    } catch { /* ignore */ }
  }
  return found;
}

/** 限深目录搜索：找 names 中的可执行文件。 */
function _searchLauncher(dir, names, depth) {
  const out = [];
  if (depth < 0) return out;
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isFile() && names.includes(ent.name)) {
      out.push(full);
    } else if (ent.isDirectory()) {
      out.push(..._searchLauncher(full, names, depth - 1));
    }
    if (out.length >= 4) break;
  }
  return out;
}

/** 查注册表 App Paths（Windows）：`HK..\...\App Paths\<exe>` 默认值即可执行文件路径。 */
function lookupAppPaths(appName) {
  if (process.platform !== "win32") return null;
  const names = (_APP_CANDIDATES[appName] || []).filter((n) => n.endsWith(".exe"));
  for (const exe of names) {
    try {
      const { execFileSync } = require("child_process");
      const out = execFileSync("reg", [
        "query",
        `HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\${exe}`,
        "/ve",
      ], { encoding: "utf8", timeout: 3000, windowsHide: true });
      const m = /REG_SZ\s+(.+)/.exec(out || "");
      if (m && m[1].trim() && fs.existsSync(m[1].trim())) return m[1].trim();
    } catch { /* 未注册或权限不足 → 继续尝试其它 */ }
  }
  return null;
}

/** 解析某外部应用的启动器路径（带缓存）。 */
const _launcherCache = {};
function resolveLauncher(appName) {
  if (_launcherCache[appName]) return _launcherCache[appName];
  // ① 用户显式配置优先
  const cfg = readExternalApps();
  if (cfg[appName] && fs.existsSync(cfg[appName])) {
    _launcherCache[appName] = cfg[appName];
    return cfg[appName];
  }
  // ② PATH
  for (const n of _APP_CANDIDATES[appName] || []) {
    const hit = whichInPath(n);
    if (hit) { _launcherCache[appName] = hit; return hit; }
  }
  // ③ 常见安装路径
  const scanned = scanCommonInstallPaths(appName);
  if (scanned.length > 0) {
    _launcherCache[appName] = scanned[0];
    return scanned[0];
  }
  // ④ 注册表
  const reg = lookupAppPaths(appName);
  if (reg) { _launcherCache[appName] = reg; return reg; }
  return null;
}

/** 启动外部应用；返回 { ok, launcher?, error? }。 */
function launchExternalApp(appName, projectPath) {
  const { spawn, execFile } = require("child_process");
  const launcher = resolveLauncher(appName);
  const display = _APP_DISPLAY[appName] || appName;
  if (!launcher) {
    return { ok: false, error: `未找到 ${display} 启动器（已尝试：用户配置 / PATH / 常见安装目录 / 注册表）` };
  }
  const lower = launcher.toLowerCase();
  try {
    if (lower.endsWith(".bat") || lower.endsWith(".cmd")) {
      // .bat/.cmd 必须经 cmd 执行；先 cd 到目标目录再打开（IDEA 支持传目录）
      execFile("cmd.exe", ["/c", "start", "", launcher, projectPath],
        { windowsHide: true }, (err) => {
          if (err) logErr(`[chatcoder] 启动 ${display} 失败:`, err.message);
        });
    } else {
      const child = spawn(launcher, [projectPath], { detached: true, stdio: "ignore", windowsHide: true });
      child.on("error", (err) => logErr(`[chatcoder] 启动 ${display} 失败:`, err && err.message));
      child.unref();
    }
    log(`[chatcoder] 已启动 ${display}: ${launcher} → ${projectPath}`);
    return { ok: true, launcher };
  } catch (e) {
    logErr(`[chatcoder] 启动 ${display} 异常:`, e && e.message);
    return { ok: false, launcher, error: `启动 ${display} 失败：${e && e.message}` };
  }
}

// 在特定外部应用中打开目录（explorer / vscode / idea / terminal）
// plan-308-1542 需求5：返回 { ok, launcher?, error? }，失败原因回传前端提示。
ipcMain.handle("shell:openInApp", (_event, target, projectPath) => {
  if (!projectPath) return { ok: false, error: "项目路径为空" };
  try {
    const p = path.normalize(projectPath);
    if (target === "explorer") {
      shell.openPath(p);
      return { ok: true };
    }
    if (target === "vscode" || target === "idea") {
      return launchExternalApp(target, p);
    }
    if (target === "terminal") {
      const { spawn } = require("child_process");
      // 终端按目录打开：部分终端无法直接打开文件（用户口径）。
      // 从标题栏等入口可能仍传文件路径，这里统一取父目录。
      let dir = p;
      try {
        if (fs.existsSync(p) && fs.statSync(p).isFile()) dir = path.dirname(p);
      } catch { /* 路径不可读时按原样传入 */ }
      if (process.platform === "win32") {
        spawn("wt.exe", ["-d", dir], { shell: true, detached: true }).on("error", () => {
          spawn("cmd.exe", ["/c", "start", "powershell.exe", "-NoExit", "-Command", `Set-Location '${dir}'`], { shell: true, detached: true });
        });
      } else {
        shell.openPath(dir);
      }
      return { ok: true };
    }
    shell.openPath(p);
    return { ok: true };
  } catch (e) {
    logErr("[shell:openInApp] 失败: " + e.message);
    return { ok: false, error: e.message };
  }
});

// plan-308-1542 需求5：用户手动指定外部应用可执行文件（自动探测失败时的兜底）。
ipcMain.handle("app:selectApp", async (_event, appName, currentPath) => {
  const display = _APP_DISPLAY[appName] || appName;
  const res = await dialog.showOpenDialog(mainWindow, {
    title: `选择 ${display} 可执行文件`,
    properties: ["openFile"],
    defaultPath: currentPath || undefined,
    filters: [{ name: "可执行文件", extensions: ["exe", "bat", "cmd", "sh"] }],
  });
  if (res.canceled || !res.filePaths.length) return { ok: false, canceled: true };
  const chosen = res.filePaths[0];
  const cfg = readExternalApps();
  cfg[appName] = chosen;
  writeExternalApps(cfg);
  delete _launcherCache[appName];
  return { ok: true, path: chosen };
});

// 查询已解析/已配置的启动器（前端可展示"当前将用哪个"）。
ipcMain.handle("app:getExternalApps", () => {
  const cfg = readExternalApps();
  const out = {};
  for (const name of Object.keys(_APP_CANDIDATES)) {
    out[name] = { configured: cfg[name] || null, resolved: resolveLauncher(name) };
  }
  return out;
});
// v23: 打开外部 URL（ta3 登录授权跳转，走系统默认浏览器）
ipcMain.handle("shell:openExternal", (_event, url) => {
  if (url && /^https?:\/\//i.test(String(url))) shell.openExternal(String(url));
});

// ── IPC:窗口控制 ──
ipcMain.on("window:minimize", () => { if (mainWindow) mainWindow.minimize(); });
ipcMain.on("window:maximizeToggle", () => {
  // plan-31-151 S2：统一走系统原生 maximize/unmaximize（含玻璃开启）。
  // plan-31-152 S5-2：加日志——"点缩放没反应"需要区分「IPC 未到达」与「maximize 被忽略」，
  //   这条日志让两种情况在 main.log 里可判定。
  if (!mainWindow) { log("[chatcoder] window:maximizeToggle 收到，但 mainWindow 为空"); return; }
  let before = false;
  try { before = mainWindow.isMaximized(); } catch { /* ignore */ }
  log("[chatcoder] window:maximizeToggle → isMaximized =", before, "→", before ? "unmaximize" : "maximize");
  togglePseudoMaximize(mainWindow);
});
ipcMain.on("window:close", () => { if (mainWindow) mainWindow.close(); });

// ── plan-31-151 S3：自研标题栏拖拽（_dragSession / pumpWindowDrag / startWindowDrag /
//   endWindowDrag / window:dragStart / window:dragMove / window:dragEnd）已整体删除 ──
//   标题栏改回 -webkit-app-region:drag（系统原生拖拽/双击/右键菜单），
//   不再需要从渲染层 IPC 上报坐标逐帧 setBounds。

// 渲染层查询当前窗口**实际**玻璃状态（保留通道：渲染层据此决定是否降级不透明度）。
ipcMain.handle("window:getGlassActive", () => _glassActive === true);

// ── IPC:修复文本输入状态（输入框"能删不能输"卡死的兜底）──
// 保留 API 兼容与节流，但不再触碰任何焦点：调用 webContents.focus() 会与
// <webview> guest 协商焦点并反复重置 Windows TSF/IME 关联（卡死根因之一）。
let _lastFixTextTime = 0;
ipcMain.handle("window:fixTextInput", () => {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const now = Date.now();
  if (now - _lastFixTextTime < 300) return true; // 300ms 节流防风暴
  _lastFixTextTime = now;
  log("[chatcoder] fixTextInput: requested (no-op: focus managed by OS/Chromium)");
  return true;
});

// ── IPC：毛玻璃模式（plan-546 / plan-548 / plan-308-1542 / plan-26-126 P1）──
// plan-26-126 P1（用户明确要求）：**开启毛玻璃不再直接生效，改为下次重启后生效**。
//
// 为何改成重启生效（三个已确定的根因，均有实测依据）：
//   ① 运行期翻转 Win11 窗口底色（setBackgroundColor）会让 DWM 合成状态与渲染层缓存失步
//      → 左侧面板出现异常色块、从设置页返回后残留上一页浅残影，**重启后即正常**（用户实测）；
//   ② applyUiVars 在**每次偏好变更**时都会调这个 IPC（拖滑杆 / 提交面板宽度 / 切语言…），
//      于是"即时生效"在高频偏好写入下变成持续重创合成管线 → 界面无响应数秒；
//   ③ 窗口底色只能在构造期可靠生效（transparent 也是构造参数）。
// 因此这里只做**落盘**：材质与窗口底色都交由下次 createWindow 一次性决定，
// 返回 needRestart=true，由设置页提示用户重启（不再做任何运行期材质/底色变更）。
ipcMain.handle("window:setGlass", (_e, on) => {
  const want = !!on;
  const applied = readGlassPref(); // 当前窗口**实际**采用的材质状态
  writeGlassPref(want);           // 仅落盘：供下次启动建窗时读取
  _glassWanted = want;            // 同步内存态（供伪最大化/还原时的材质补偿使用）
  const needRestart = want !== applied;
  log("[chatcoder] glass: setGlass =", want, JSON.stringify({ needRestart, appliedBefore: applied }));
  return {
    ok: true,
    backend: blurBackend().backend,
    // 与本次窗口实际生效状态不一致 ⇒ 需重启才能看到变化
    needRestart,
    applied,
    wanted: want,
  };
});

/** plan-26-126 P1：一键重启（设置页提示条上的按钮）。
 *  用 app.relaunch + app.exit：比让渲染层刷新页面更彻底（窗口会按新偏好重建）。 */
ipcMain.handle("app:relaunch", () => {
  try {
    // plan-73-344：重启前先落盘再退出——flush 挂起的偏好备份 + 刷 DOMStorage 未提交数据，
    // 延时 150ms 让异步提交完成。此前直接 app.exit(0)（强退），「改完设置立刻重启」会丢
    // 最后一次写入（用户实测：消息行距改 2.2，重启回到默认 1.7）。
    flushUiPrefsBackup();
    flushDomStorage();
    app.relaunch();
    setTimeout(() => app.exit(0), 150);
    return { ok: true };
  } catch (err) {
    logErr("[chatcoder] 重启失败:", err && err.message);
    return { ok: false, reason: err && err.message };
  }
});

// ── plan-26-116：能力探测 / 折射 / 自检 IPC 已移除 ──
// window:glassCapability、window:liquidGlassCapability、window:setLiquidGlass、
// window:glassSelfCheck 整体删去：设置页不再展示「系统模糊后端」「折射面板」「需重启」
// 「玻璃诊断/自检」等提示性信息，只留一个「毛玻璃效果」开关 + 「玻璃强度」三档。

// ── IPC:保持唤醒（对齐 zcode「运行会话时保持电脑唤醒」）──
// S3（plan-41-197）：语义修正——原实现用 prevent-app-suspension，它只阻止系统
// 挂起应用（App Nap 类），**不阻止显示器/系统睡眠**，与界面上“保持电脑唤醒”的
// 描述不符（用户反馈“配置确认一下是否真的有效，假开关的话请完善”）。
// 改为 prevent-display-sleep：阻止显示器休眠，这才是用户对“保持唤醒”的真实预期。
let _psbId = null;
ipcMain.handle("power:setKeepAwake", (_e, on) => {
  const { powerSaveBlocker } = require("electron");
  if (on) {
    if (_psbId === null) _psbId = powerSaveBlocker.start("prevent-display-sleep");
  } else if (_psbId !== null) {
    try { powerSaveBlocker.stop(_psbId); } catch { /* ignore */ }
    _psbId = null;
  }
  return _psbId !== null;
});

// ── IPC:任务完成系统通知（R4）──
// 三档语义由渲染层按全局设置（task_notify_mode）传入：
//   always     始终弹；
//   unfocused  仅主窗未聚焦/不可见/已最小化时弹（默认）——用户正看着窗口就不打扰；
//   off        不弹。
// 点击通知：还原/聚焦主窗，并通过 app:focusSession 让渲染层切回对应会话
// （App.tsx 已监听该通道，与宠物面板「查看会话」同一路径）。
ipcMain.handle("app:notifyTask", (_e, payload) => {
  try {
    const mode = String(payload?.mode || "unfocused");
    if (mode === "off") return false;
    if (mode !== "always") {
      const focused = !!mainWindow && !mainWindow.isDestroyed()
        && mainWindow.isVisible() && !mainWindow.isMinimized() && mainWindow.isFocused();
      if (focused) return false;
    }
    if (!Notification.isSupported()) return false;
    const sessionId = Number(payload?.sessionId ?? 0) || null;
    const notification = new Notification({
      title: String(payload?.title || "chatcoder"),
      body: String(payload?.body || ""),
    });
    notification.on("click", () => {
      try {
        if (!mainWindow || mainWindow.isDestroyed()) return;
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.show();
        mainWindow.focus();
        if (sessionId) mainWindow.webContents.send("app:focusSession", sessionId);
      } catch (err) {
        logErr("[chatcoder] 通知点击跳转失败:", err && err.message);
      }
    });
    notification.show();
    return true;
  } catch (err) {
    logErr("[chatcoder] 任务完成通知失败:", err && err.message);
    return false;
  }
});

// ── IPC:窗口 chrome 状态（plan-114-450）──
// preload 在**文档解析前**同步读取，写入 <html> 的 data-maximized / data-native-corners——
// 这两个属性决定「窗口四角由 DWM 原生圆角裁剪，还是由 CSS 自绘圆角」。
// 为什么必须新增同步通道：原有注入走 executeJavaScript 且只覆盖"窗口状态变化"，
//   页面导航换掉整个文档后属性丢失却不补发（见 syncWindowChromeState 的下发缓存），
//   渲染层便会退回自绘 12px 圆角，与系统圆角错位成双层弧线。
ipcMain.on("window:chrome-sync-get", (e) => {
  try {
    e.returnValue = {
      maximized: isMaximizedLike(mainWindow),
      nativeCorners: supportsNativeRoundedCorners(),
      inset: clientInsetOf(mainWindow),
    };
  } catch {
    e.returnValue = { maximized: false, nativeCorners: false, inset: { top: 0, left: 0, right: 0 } };
  }
});

// ── IPC:主题偏好落盘（渲染进程切主题时同步，下次启动 loading 页按此适配深浅色）──
ipcMain.on("theme:setTone", (_e, tone) => { writeTonePref(tone); });
ipcMain.on("theme:setPref", (_e, theme) => {
  writeThemePref(theme);
  // plan-387-1825 A10：切换主题后立即同步窗口**非客户区**（WS_CAPTION 边框 / 系统标题栏 /
  // 系统菜单）的深浅渲染。不同步的话，「深色应用 + 浅色系统」在小窗下会一直留一圈白边，
  // 而底色其实已经跟着主题变了——这正是用户看到的"覆盖不全的白底"。
  // 注：这里只改 DWM 的非客户区配色，不动窗口底色（运行期改底色会与 DWM 合成失步，
  // 见下方 glass IPC 的历史结论），因此不会引发色块/残影问题。
  try {
    if (mainWindow && !mainWindow.isDestroyed()) syncWindowChromeState(mainWindow);
  } catch { /* 窗口未就绪时忽略 */ }
});

// ── IPC:UI 偏好备份（plan-73-344）──
// 写入做 250ms 合并（拖滑杆高频变更不逐次写盘；退出前由 flushUiPrefsBackup 强制写完），
// 读取走同步通道（启动时要与 localStorage 比对、决定用哪份，异步会让首帧闪默认值）。
ipcMain.on("ui-prefs:save", (_e, payload) => {
  if (!payload || typeof payload !== "object") return;
  _uiPrefsPending = payload;
  if (_uiPrefsTimer) return;
  _uiPrefsTimer = setTimeout(() => {
    _uiPrefsTimer = null;
    const p = _uiPrefsPending;
    _uiPrefsPending = null;
    if (p) writeUiPrefsBackup(p);
  }, 250);
});
ipcMain.on("ui-prefs:load", (e) => { e.returnValue = readUiPrefsBackup(); });

// ── IPC:终端 PTY（v2.2: node-pty 真 PTY，支持 resize/全屏程序；加载失败回退 spawn）──
const { createPty, usingNodePty } = require("./pty.cjs");
const ptyProcs = new Map(); // id -> pty handle
let ptySeq = 0;
ipcMain.handle("pty:spawn", (_event, opts) => {
  const id = ++ptySeq;
  const pty = createPty({
    id,
    cwd: (opts && opts.cwd) || process.cwd(),
    cols: (opts && opts.cols) || 80,
    rows: (opts && opts.rows) || 24,
    shell: opts && opts.shell,
  });
  if (!pty.pid) {
    log("pty spawn failed, no pid");
    return { id: 0, error: "无法启动终端进程" };
  }
  ptyProcs.set(id, pty);
  pty.onData((chunk) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("pty:data", id, chunk);
    }
  });
  pty.onExit((code) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("pty:exit", id, code);
    }
    ptyProcs.delete(id);
  });
  log("[pty] spawn id=" + id + " pid=" + pty.pid + " nodePty=" + pty.isPty + " shell=" + ((opts && opts.shell) || "auto"));
  return { id, pid: pty.pid, isPty: pty.isPty };
});

ipcMain.on("pty:write", (_event, id, data) => {
  const pty = ptyProcs.get(id);
  if (pty) pty.write(data);
});

ipcMain.on("pty:resize", (_event, id, cols, rows) => {
  const pty = ptyProcs.get(id);
  if (pty) {
    pty.resize(Number(cols) || 80, Number(rows) || 24);
  }
});

ipcMain.on("pty:kill", (_event, id) => {
  const pty = ptyProcs.get(id);
  if (pty) {
    try { pty.kill(); } catch {}
    ptyProcs.delete(id);
  }
});

// ── 内置浏览器 · 本地加密凭据库与 AI 驱动（plan-771-3177 / plan-20261015-browser-agent）──
// safeStorage 加密凭据仅服务用户侧 IPC，不暴露给后端或 AI 工具；同时向 browser-agent 提供右键菜单自动填充能力。
const browserVaultApi = require("./browser-vault.cjs")({ getMainWindow: () => mainWindow, logErr });
browserVaultApi.registerIpc();

// 面板 webview 的后台节流、CDP 网络/弹窗接管、隐藏态截图、原生右键菜单与新窗口转标签，详见 browser-agent.cjs。
// plan-693-2868 · docs/app1/11-host-bridge-protocol.md：微应用宿主桥（uid 映射 / 上行转发 /
// 下行推送）也在同一模块里，建窗时通过返回的 hookMainWindow 挂上。
browserAgentApi = require("./browser-agent.cjs")({
  getMainWindow: () => mainWindow,
  browserVault: browserVaultApi,
  log,
  logErr,
});

// ── IPC:浏览器工作台 ──
ipcMain.handle("browser:writeImage", (event, data) => {
  if (event.sender !== mainWindow?.webContents || typeof data !== "string" || data.length > 40_000_000
      || !data.startsWith("data:image/png;base64,")) return false;
  const { clipboard, nativeImage } = require("electron");
  const image = nativeImage.createFromDataURL(data);
  if (image.isEmpty()) return false;
  clipboard.writeImage(image);
  return true;
});

ipcMain.handle("browser:openDevTools", (_event, targetWebContentsId) => {
  try {
    const { webContents } = require("electron");
    if (targetWebContentsId) {
      const wc = webContents.fromId(targetWebContentsId);
      if (wc) {
        wc.openDevTools({ mode: "detach" });
        return true;
      }
    }
    // 降级：如果未指定 targetWebContentsId，默认查找所有非主窗口的 webContents
    const all = webContents.getAllWebContents();
    const guest = all.find((w) => mainWindow && w.id !== mainWindow.webContents.id);
    if (guest) {
      guest.openDevTools({ mode: "detach" });
      return true;
    }
    if (mainWindow) {
      mainWindow.webContents.openDevTools({ mode: "detach" });
      return true;
    }
  } catch (err) {
    logErr("[chatcoder] browser:openDevTools 失败:", err);
  }
  return false;
});

ipcMain.handle("browser:capturePage", async (_event, targetWebContentsId) => {
  try {
    const { webContents } = require("electron");
    let wc = null;
    if (targetWebContentsId) {
      wc = webContents.fromId(targetWebContentsId);
    } else {
      const all = webContents.getAllWebContents();
      wc = all.find((w) => mainWindow && w.id !== mainWindow.webContents.id) || (mainWindow ? mainWindow.webContents : null);
    }
    if (wc) {
      const image = await wc.capturePage();
      return image.toDataURL(); // 返回 base64 data url
    }
  } catch (err) {
    logErr("[chatcoder] browser:capturePage 失败:", err);
  }
  return null;
});

// ── 自动更新（electron-updater + 自建源 / GitHub Releases）──
// 仅打包版启用：dev 模式无 app-update.yml，检查会直接失败。
// 状态机与下载编排在 update-manager.cjs（可单独做端到端验证）；这里只负责接线与「更新历史」。
// plan-230-1144 M4.2: 透传 releaseNotes / releaseDate；支持按版本区间拉取更新历史；
// 记录 lastSeenVersion 供升级后首启"本次更新"弹窗判定。
// plan-20261021-usage-update：
//  - 点击下载 / 静默自动下载都先切到 downloading(phase=connecting)，不再等第一个数据块才有反馈；
//  - 全量安装包改走自带的断点续传下载器（多连接分块、每块重试、多源切换），断网后接着下而不是重下；
//  - 发现新版本默认自动下载（设置里可关）。
const { createUpdateManager, normalizeReleaseNotes } = require("./update-manager.cjs");

const UPDATE_STATE_FILE = () => path.join(app.getPath("userData"), "update-state.json");

function readUpdateMeta() {
  try {
    return JSON.parse(fs.readFileSync(UPDATE_STATE_FILE(), "utf-8")) || {};
  } catch { return {}; }
}

function writeUpdateMeta(patch) {
  try {
    const cur = readUpdateMeta();
    fs.writeFileSync(UPDATE_STATE_FILE(), JSON.stringify({ ...cur, ...patch }, null, 2), "utf-8");
  } catch (e) { logErr("[updater] 写入 update-state.json 失败:", e && e.message); }
}

function pushUpdateState(state) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send("app:updateStatus", state); } catch {}
  }
}

/** plan-20261024-update-channel：下载通道测速 / 优选结果推给渲染进程（关于页的通道卡片） */
function pushUpdateChannels(snapshot) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send("app:updateChannels", snapshot); } catch {}
  }
}

/** 本地打包的 CHANGELOG.md 路径（打包后位于 resources 或应用根） */
function localChangelogPath() {
  const candidates = [
    path.join(process.resourcesPath || "", "CHANGELOG.md"),
    path.join(app.getAppPath(), "..", "CHANGELOG.md"),
    path.join(app.getAppPath(), "CHANGELOG.md"),
  ];
  for (const p of candidates) {
    try { if (p && fs.existsSync(p)) return p; } catch {}
  }
  return "";
}

/** GitHub Releases（owner/repo 来自 package.json build.publish）：发布通道 + 自动更新备源 */
const UPDATER_REPO = { owner: "yueyuqingtian", repo: "chatcoder" };

/** 自建更新源（主源，plan-90-390）：nginx 静态文件，国内实测 8MB/s+；
 *  useMultipleRangeRequest=false —— 差分下载各段用单区间请求，兼容性最稳。 */
const UPDATES_BASE_URL = "https://service.guyueyu.asia/updates/";
const MIRROR_FEED = { provider: "generic", url: UPDATES_BASE_URL, useMultipleRangeRequest: false };
const GITHUB_FEED = { provider: "github", owner: UPDATER_REPO.owner, repo: UPDATER_REPO.repo };

async function fetchReleases(limit = 20) {
  const url = `https://api.github.com/repos/${UPDATER_REPO.owner}/${UPDATER_REPO.repo}/releases?per_page=${limit}`;
  const resp = await fetch(url, {
    headers: { "Accept": "application/vnd.github+json", "User-Agent": "chatcoder-updater" },
  });
  if (!resp.ok) throw new Error(`GitHub API HTTP ${resp.status}`);
  return await resp.json();
}

/** 自建源更新历史：发布时生成的静态 releases.json（结构与下方 GitHub 映射一致：
 *  {version,name,date,notes,prerelease}），毫秒级返回；失败由调用方回落 GitHub。 */
async function fetchReleasesMirror(limit = 30) {
  const resp = await fetch(`${UPDATES_BASE_URL}releases.json`, {
    headers: { "Accept": "application/json", "User-Agent": "chatcoder-updater" },
  });
  if (!resp.ok) throw new Error(`mirror HTTP ${resp.status}`);
  const data = await resp.json();
  if (!Array.isArray(data) || data.length === 0) throw new Error("mirror payload invalid");
  return data.slice(0, limit);
}

/** 镜像线路专用的直连会话：setProxy({mode:"direct"}) 绕过系统代理（plan-20261022-update-route）。
 *  gh-proxy 是境外 CDN，本机代理绕一圈反而更慢；会话在首次下载时才创建，不影响启动。
 *  返回函数形态供下载器按源选择 fetch：镜像走这里，自建源与 GitHub 仍用 net.fetch（继承系统代理）。 */
let updateDirectSessionPromise = null;
function updateDirectFetch(url, init) {
  if (!updateDirectSessionPromise) {
    try {
      const ses = session.fromPartition("chatcoder-update-direct");
      updateDirectSessionPromise = ses.setProxy({ mode: "direct" }).then(() => ses).catch(() => ses);
    } catch {
      return require("electron").net.fetch(url, init);
    }
  }
  return updateDirectSessionPromise.then((ses) => (
    typeof ses.fetch === "function" ? ses.fetch(url, init) : require("electron").net.fetch(url, init)
  ));
}

const updateManager = createUpdateManager({
  pushState: pushUpdateState,
  pushChannels: pushUpdateChannels,
  meta: { read: readUpdateMeta, write: writeUpdateMeta },
  feeds: { mirror: MIRROR_FEED, github: GITHUB_FEED },
  updatesBaseUrl: UPDATES_BASE_URL,
  repo: UPDATER_REPO,
  currentVersion: app.getVersion(),
  net: require("electron").net,
  directFetch: updateDirectFetch,
  log: (...a) => log(...a),
  logErr: (...a) => logErr(...a),
});

function initAutoUpdater() {
  if (!app.isPackaged) return;
  let updater;
  try {
    ({ autoUpdater: updater } = require("electron-updater"));
  } catch (e) {
    logErr("[updater] require electron-updater 失败:", e && e.message);
    return;
  }
  updateManager.attach(updater);
  updateManager.startTimers();
}

// ── IPC:更新操作（手动检查 / 立即安装 / 查询状态 / 当前版本 / 更新历史）──
ipcMain.handle("app:checkForUpdates", () => updateManager.checkForUpdates());
ipcMain.handle("app:getUpdateState", () => updateManager.getState());
// 不等下载完成：立即进入 downloading(connecting) 并返回，后续进度靠 app:updateStatus 推送
ipcMain.handle("app:downloadUpdate", () => updateManager.downloadNow());
// plan-20261022-update-route：取消后回到「发现新版本」，断点保留；切换自建源 = 停下后从自建源重新开始
ipcMain.handle("app:cancelUpdateDownload", () => updateManager.cancelDownload());
ipcMain.handle("app:switchUpdateSource", () => updateManager.switchToPrimary());
ipcMain.handle("app:getUpdateSettings", () => updateManager.getSettings());
ipcMain.handle("app:setUpdateSettings", (_e, patch) => updateManager.setSettings(patch));
// plan-20261024-update-channel：关于页的下载通道（查测速结果 / 重新测速 / 手动指定）
ipcMain.handle("app:getUpdateChannels", () => updateManager.getChannels());
ipcMain.handle("app:measureUpdateChannels", () => updateManager.measureChannels({ force: true }));
ipcMain.handle("app:setUpdateChannel", (_e, id) => updateManager.setChannel(id));
ipcMain.handle("app:installUpdate", () => updateManager.install());
ipcMain.handle("app:getVersion", () => app.getVersion());

// plan-230-1144 M4.2 / plan-90-390: 更新历史 —— 自建源静态数据 → GitHub API → 本地 CHANGELOG.md 三级回落
ipcMain.handle("app:getReleaseNotes", async (_e, opts) => {
  const limit = (opts && opts.limit) || 20;
  // 1) 自建源 releases.json（国内毫秒级）
  try {
    const releases = await fetchReleasesMirror(limit);
    return {
      ok: true,
      source: "mirror",
      releases: releases.map((r) => ({
        version: String(r.version || "").replace(/^v/, ""),
        name: r.name || "",
        date: r.date || "",
        notes: normalizeReleaseNotes(r.notes),
        prerelease: !!r.prerelease,
      })),
    };
  } catch (e) {
    log("[updater] releases.json 不可用，回落 GitHub:", e && e.message);
  }
  // 2) GitHub Releases API（联网）
  try {
    const releases = await fetchReleases(limit);
    return {
      ok: true,
      source: "github",
      releases: releases.map((r) => ({
        version: String(r.tag_name || "").replace(/^v/, ""),
        name: r.name || r.tag_name || "",
        date: r.published_at || "",
        notes: normalizeReleaseNotes(r.body),
        prerelease: !!r.prerelease,
      })),
    };
  } catch (e) {
    // 3) 网络不可用：回落本地打包 changelog（保证离线可看）
    try {
      const p = localChangelogPath();
      if (p) {
        return { ok: true, source: "local", releases: [{ version: "", name: "本地更新日志", date: "", notes: fs.readFileSync(p, "utf-8") }] };
      }
    } catch {}
    return { ok: false, source: "none", error: String((e && e.message) || e), releases: [] };
  }
});

// plan-230-1144 M4.2: 升级后首启判定 —— lastSeenVersion < 当前版本时返回 true（弹"本次更新"）
ipcMain.handle("app:consumeWhatsNew", () => {
  const cur = app.getVersion();
  const meta = readUpdateMeta();
  const lastSeen = meta.lastSeenVersion || "";
  writeUpdateMeta({ lastSeenVersion: cur });
  if (!lastSeen || lastSeen === cur) return { show: false, version: cur };
  return { show: true, version: cur, from: lastSeen };
});

// plan-230-1144 M4.2 / plan-90-390: 拉取"从 fromVersion 到当前"之间各版本的更新说明（汇总展示）
ipcMain.handle("app:getWhatsNew", async (_e, opts) => {
  const from = (opts && opts.from) || "";
  const to = (opts && opts.to) || app.getVersion();
  // 数据源：自建 releases.json 优先，失败回落 GitHub API
  let releases = null;
  let source = "mirror";
  try {
    releases = await fetchReleasesMirror(30);
  } catch (e) {
    log("[updater] releases.json 不可用，回落 GitHub:", e && e.message);
    source = "github";
    try {
      releases = await fetchReleases(30);
    } catch (e2) {
      return { ok: false, source: "none", error: String((e2 && e2.message) || e2), releases: [] };
    }
  }
  const cmp = (a, b) => {
    const pa = String(a).split(".").map((x) => parseInt(x, 10) || 0);
    const pb = String(b).split(".").map((x) => parseInt(x, 10) || 0);
    for (let i = 0; i < 3; i++) { if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0); }
    return 0;
  };
  const picked = releases
    .map((r) => ({
      version: String(r.version || r.tag_name || "").replace(/^v/, ""),
      name: r.name || "",
      date: r.date || r.published_at || "",
      notes: normalizeReleaseNotes(r.notes || r.body),
    }))
    .filter((r) => r.version && (!from || cmp(r.version, from) > 0) && cmp(r.version, to) <= 0)
    .sort((a, b) => cmp(b.version, a.version));
  return { ok: true, source, releases: picked };
});

// ── plan-693-2868 §3.4 / §6.4 / D20 / D32 / D34 / D36 / D37：微应用社区发布与撤回 ──
// 为什么放在主进程而不是后端：PAT 只在主进程内存与 safeStorage 档里（D20），
// 后端（Python）一条 PAT 也碰不到，「PAT 不出现在 Python 日志」这条验收才成立。
// 这里只做「用户侧」的动作：中转上传 + 提 issue + 轮询 issue 关闭；
// 真正写共享仓库 apps.json 的是社区仓库里的 accept.yml（D32/D38），桌面端没有那份权限（D37）。
const communityGithub = require("./community-github.cjs");

/** 发布进度统一走一个事件名，渲染层订阅一次即可（stage/percent/attempt 等）。 */
function sendCommunityProgress(payload) {
  try {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("community:publish-progress", payload);
  } catch { /* 窗口未就绪时丢弃进度，不影响发布本身 */ }
}

/**
 * 把模块抛出的结构化错误转成可序列化结果。
 * 只透出 code 与 message：token 原文既不在 message 里（模块侧已保证），也不单独回传。
 */
function communityFail(e) {
  return {
    ok: false,
    code: (e && e.code) || "UNKNOWN",
    message: String((e && e.message) || e || "未知错误"),
  };
}

ipcMain.handle("community:token-status", () => {
  try {
    const st = communityGithub.getTokenStatus();
    return { ok: true, saved: st.saved === true, encrypted: st.encrypted === true };
  } catch (e) { return communityFail(e); }
});
// 收 token 后立即加密落盘，返回值只有 {saved, encrypted}——不把 token 回传渲染层
ipcMain.handle("community:set-token", (_e, token) => {
  try {
    const st = communityGithub.setToken(token);
    return { ok: true, saved: st.saved === true, encrypted: st.encrypted === true };
  } catch (e) { return communityFail(e); }
});
ipcMain.handle("community:clear-token", () => {
  try {
    communityGithub.clearToken();
    return { ok: true, saved: false, encrypted: false };
  } catch (e) { return communityFail(e); }
});
ipcMain.handle("community:get-repo", () => {
  try { return { ok: true, repo: communityGithub.getRepo() }; } catch (e) { return communityFail(e); }
});
// 空串 = 关闭社区入口（§7 P5-8）；这里保存规范化后的 owner/repo
ipcMain.handle("community:set-repo", (_e, repo) => {
  try { return { ok: true, repo: communityGithub.setRepo(repo) }; } catch (e) { return communityFail(e); }
});

// 发布：先算哈希再上传中转包，最后提 [upload] issue 并等 Actions 关闭它（D40）
ipcMain.handle("community:publish", async (_e, opts) => {
  const input = opts && typeof opts === "object" ? opts : {};
  try {
    const result = await communityGithub.publish({
      ...input,
      onProgress: (p) => sendCommunityProgress({ ...p, appId: input.appId, version: input.version }),
    });
    // 超时（TIMEOUT）不是失败：issue 已提交，只是 Actions 还没跑完，界面按「稍后查看」提示
    return { ok: true, ...result };
  } catch (e) {
    return communityFail(e);
  }
});

// 撤回：提 [withdraw] issue；Actions 只移除 apps.json 条目，包保留（D35）
ipcMain.handle("community:withdraw", async (_e, opts) => {
  const input = opts && typeof opts === "object" ? opts : {};
  try {
    const result = await communityGithub.withdraw({
      ...input,
      onProgress: (p) => sendCommunityProgress({ ...p, appId: input.appId, version: input.version, action: "withdraw" }),
    });
    return { ok: true, ...result };
  } catch (e) {
    return communityFail(e);
  }
});

// 单独轮询某个 issue（渲染层重开对话框 / 用户点「刷新状态」时用）
ipcMain.handle("community:poll-issue", async (_e, issueNumber, opts) => {
  try {
    const snapshot = await communityGithub.pollIssue(issueNumber, opts && typeof opts === "object" ? opts : {});
    return { ok: true, ...snapshot };
  } catch (e) {
    return communityFail(e);
  }
});

// ── 确保单实例 ──
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

// ── 应用生命周期 ──
app.whenReady().then(async () => {
  log("[chatcoder] app ready, isPackaged =", app.isPackaged);
  // plan-693-2868 §7 P0-3 / §6.2：先注册 app:// 协议再建窗口——窗口一创建就可能加载含微应用的
  // 页面，晚注册会让首个 navigations 拿到 ERR_UNKNOWN_URL_SCHEME。
  try {
    registerMicroAppProtocol();
  } catch (err) {
    logErr("[chatcoder] app:// 协议注册失败:", err && err.message);
  }
  // R4：任务完成系统通知。Windows 下必须先设 AppUserModelID，否则 Notification 被系统
  //   静默丢弃（没报错也不弹）；取值与 package.json 的 build.appId / 快捷方式保持一致。
  if (process.platform === "win32") {
    try { app.setAppUserModelId("com.chatcoder.desktop"); } catch (err) { logErr("[chatcoder] setAppUserModelId 失败:", err && err.message); }
  }
  // Cordis 兼容插件在后台加载，不阻塞窗口与后端启动；结果通过 cordis:status 查询
  void cordisHost.load().catch((e) => logErr("[cordis] 启动加载失败：", e && e.message));
  // 后端启动需要数秒，延迟同步连接器；失败只记日志，之后安装或重新加载时会再次同步
  setTimeout(() => { void syncPluginConnector(); }, 8000);

  // 1. 立即创建窗口并显示加载页
  try {
    createWindow();
  } catch (err) {
    logErr("[chatcoder] createWindow 失败:", err);
  }

  // 1.5 注册宠物 IPC + 挂主窗钩子（必须在渲染层加载前完成，否则设置页/宠物页早期调用会失败）
  try {
    pet.registerPetIpc({
      log, logErr,
      getMainWindow: () => mainWindow,
      getBackendPort: () => BACKEND_PORT,
    });
    pet.hookMainWindow();
  } catch (err) {
    logErr("[chatcoder] 宠物模块初始化失败:", err);
  }

  // 2. 彻底清理所有历史残留的旧后端进程并释放端口，杜绝复用旧版本进程
  try {
    await killExistingBackendProcesses(DEFAULT_PORT);
  } catch (err) {
    logErr("[chatcoder] killExistingBackendProcesses 警告:", err);
  }

  // 3. 确定最终监听端口
  BACKEND_PORT = await pickBackendPort();
  if (BACKEND_PORT !== Number(process.env.CHATCODER_PORT || DEFAULT_PORT)) {
    log("[chatcoder] 默认端口被占用，改用端口:", BACKEND_PORT);
  }

  // 4. 后台启动新后端进程
  try {
    await startBackend();
  } catch (err) {
    logErr("[chatcoder] startBackend 失败:", err);
  }

  // 5. 等待后端就绪
  try {
    await waitForBackend();
    backendReady = true;
    log("[chatcoder] 后端就绪，端口:", BACKEND_PORT);
  } catch (e) {
    logErr("[chatcoder] 后端未就绪:", e.message);
    // 后端崩溃,显示错误信息在加载页
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.executeJavaScript(
        `document.getElementById('msg') && (document.getElementById('msg').innerHTML = '后端启动失败: ${e.message}<br><span style="font-size:11px;opacity:0.6">日志位置: ${LOG_DIR.replace(/\\/g, "/")}</span>');`
      ).catch(() => {});
    }
  }

  // 5. 加载前端
  try {
    loadFrontend();
  } catch (err) {
    logErr("[chatcoder] loadFrontend 失败:", err);
  }

  // 5.5 宠物窗口：按偏好创建（默认关闭；用户安装并启用后出现）
  //     放在后端就绪之后，避免宠物页首帧连不上 WebSocket 空转。
  try {
    pet.createPetWindow();
    pet.syncVisibility();
  } catch (err) {
    logErr("[chatcoder] 宠物窗口创建失败:", err);
  }

  // 6. 初始化自动更新（含定时检查,仅打包版）
  initAutoUpdater();
});

app.on("window-all-closed", () => {
  killBackend();
  app.quit();
});
app.on("before-quit", () => {
  // plan-73-344：退出前兜底落盘（关窗路径已在 close 事件 flush，此处覆盖系统注销等其余退出路径）
  flushUiPrefsBackup();
  flushDomStorage();
  try { cordisHost.unload(); } catch { /* 退出路径尽力而为 */ }
  killBackend();
});
app.on("will-quit", () => { killBackend(); });
process.on("exit", () => { killBackend(); });

// plan-external-microapps：外置微应用是独立进程，退出时必须连子进程一起收，
// 否则用户机器上会留下占端口的孤儿进程（内置形态没有这个问题）。
app.on("before-quit", () => {
  try { externalMicroapps.stopAll(); } catch (err) { logErr("[microapps-ext] 退出前停止失败:", (err && err.message) || err); }
});

function killBackend() {
  if (backendProcess) {
    try {
      if (process.platform === "win32" && backendProcess.pid) {
        const { execSync } = require("child_process");
        try { execSync(`taskkill /F /PID ${backendProcess.pid} /T >nul 2>nul`); } catch {}
      } else {
        backendProcess.kill("SIGKILL");
      }
    } catch {}
    backendProcess = null;
  }
}
