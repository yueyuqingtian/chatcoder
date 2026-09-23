// chatcoder 桌面主进程 (v3 — 健壮启动版)
// 修复: data: URL → loadFile / stdout 安全 / 全局错误捕获 / 图标
const { app, BrowserWindow, ipcMain, dialog, shell, nativeTheme } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const http = require("http");
const net = require("net");
const fs = require("fs");

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
  const key = `${maximized ? 1 : 0}|${native ? 1 : 0}`;
  if (key === _lastChromeState) return; // 状态未变：不注入、不跑 FFI
  _lastChromeState = key;
  try {
    void win.webContents.executeJavaScript(
      `(() => { const r = document.documentElement;`
      + ` r.setAttribute('data-maximized', ${maximized ? "'1'" : "'0'"});`
      + ` r.setAttribute('data-native-corners', ${native ? "'1'" : "'0'"});`
      + ` return true; })()`, true);
  } catch { /* 渲染层未就绪时忽略 */ }
  applyWindowCorners(win);
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
  try { fs.writeFileSync(THEME_PREF_FILE, JSON.stringify({ theme: theme === "light" ? "light" : "dark" })); } catch {}
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
  const themePref = readThemePref();
  const lightStart = themePref ? themePref === "light" : !nativeTheme.shouldUseDarkColors;
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
  mainWindow.on("maximize", () => {
    syncWindowGlass(mainWindow);
    try { mainWindow.webContents.send("window:maximize-change", true); } catch { /* 渲染层未就绪时忽略 */ }
  });
  mainWindow.on("unmaximize", () => {
    syncWindowGlass(mainWindow);
    try { mainWindow.webContents.send("window:maximize-change", false); } catch { /* 渲染层未就绪时忽略 */ }
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
      query: { theme: lightStart ? "light" : "dark", glass: loadingGlassQuery, mat: loadingMatQuery },
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

// ── IPC:系统集成 ──
ipcMain.handle("shell:openPath", (_event, p) => {
  if (p) shell.openPath(p);
});
ipcMain.handle("shell:showItemInFolder", (_event, p) => {
  if (p) shell.showItemInFolder(p);
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
      if (process.platform === "win32") {
        spawn("wt.exe", ["-d", p], { shell: true, detached: true }).on("error", () => {
          spawn("cmd.exe", ["/c", "start", "powershell.exe", "-NoExit", "-Command", `Set-Location '${p}'`], { shell: true, detached: true });
        });
      } else {
        shell.openPath(p);
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
    app.relaunch();
    app.exit(0);
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
let _psbId = null;
ipcMain.handle("power:setKeepAwake", (_e, on) => {
  const { powerSaveBlocker } = require("electron");
  if (on) {
    if (_psbId === null) _psbId = powerSaveBlocker.start("prevent-app-suspension");
  } else if (_psbId !== null) {
    try { powerSaveBlocker.stop(_psbId); } catch { /* ignore */ }
    _psbId = null;
  }
  return _psbId !== null;
});

// ── IPC:主题偏好落盘（渲染进程切主题时同步，下次启动 loading 页按此适配深浅色）──
ipcMain.on("theme:setPref", (_e, theme) => { writeThemePref(theme); });

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

// ── IPC:webview 元素标注与开发者工具 ──
ipcMain.on("webview:annotate", (_event, payload) => {
  // 转发给渲染进程（主窗口），由前端写入 Composer 草稿
  if (mainWindow) mainWindow.webContents.send("browser:annotation", payload);
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

// ── 自动更新（electron-updater + GitHub Releases）──
// 仅打包版启用：dev 模式无 app-update.yml，检查会直接失败。
// 状态机: idle → checking → available|none → downloading → downloaded → (quitAndInstall) / error
// plan-230-1144 M4.2: 透传 releaseNotes / releaseDate；支持按版本区间拉取更新历史；
// 记录 lastSeenVersion 供升级后首启"本次更新"弹窗判定。
let autoUpdater = null;
let updateState = { state: "idle" };

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

function pushUpdateState() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send("app:updateStatus", updateState); } catch {}
  }
}

/** releaseNotes 可能是 string 或 [{version, note}] 数组，统一成 Markdown 文本 */
function normalizeReleaseNotes(notes) {
  if (!notes) return "";
  if (typeof notes === "string") return notes;
  if (Array.isArray(notes)) {
    return notes.map((n) => (typeof n === "string" ? n : (n && (n.note || n.body)) || "")).join("\n\n");
  }
  if (typeof notes === "object") return notes.note || notes.body || "";
  return String(notes);
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

/** GitHub Releases API（owner/repo 来自 package.json build.publish） */
const UPDATER_REPO = { owner: "yueyuqingtian", repo: "chatcoder" };

async function fetchReleases(limit = 20) {
  const url = `https://api.github.com/repos/${UPDATER_REPO.owner}/${UPDATER_REPO.repo}/releases?per_page=${limit}`;
  const resp = await fetch(url, {
    headers: { "Accept": "application/vnd.github+json", "User-Agent": "chatcoder-updater" },
  });
  if (!resp.ok) throw new Error(`GitHub API HTTP ${resp.status}`);
  return await resp.json();
}

function initAutoUpdater() {
  if (!app.isPackaged) return;
  try {
    ({ autoUpdater } = require("electron-updater"));
  } catch (e) {
    logErr("[updater] require electron-updater 失败:", e && e.message);
    return;
  }
  // 手动触发才下载（用户点击侧栏更新按钮后开始），避免后台上传/下载占用带宽
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.on("checking-for-update", () => {
    // 已下载完成待安装时，后续检查不得把状态冲回（否则侧栏「重启」按钮会闪回「下载」）
    if (updateState.state === "downloaded") return;
    updateState = { state: "checking" };
    log("[updater] checking-for-update");
    pushUpdateState();
  });
  autoUpdater.on("update-available", (info) => {
    // 已下载完成（同版本已就绪）时保持 downloaded，避免定时检查把按钮冲回「下载」
    if (updateState.state === "downloaded" && updateState.version === info.version) return;
    // plan-230-1144 M4.2: 透传 releaseNotes/releaseDate——此前只取 version，
    // 前端因此看不到"更新了什么"（问题6 的直接根因）。
    updateState = {
      state: "available",
      version: info.version,
      notes: normalizeReleaseNotes(info.releaseNotes),
      releaseDate: info.releaseDate || "",
    };
    log("[updater] update-available:", info.version, "current:", app.getVersion());
    pushUpdateState();
  });
  autoUpdater.on("update-not-available", (info) => {
    // 已下载完成时保持 downloaded（理论上不会触发，防御性保护）
    if (updateState.state === "downloaded") return;
    updateState = { state: "none", version: info && info.version };
    log("[updater] update-not-available");
    pushUpdateState();
  });
  autoUpdater.on("download-progress", (p) => {
    updateState = {
      state: "downloading",
      percent: Math.round(p.percent || 0),
      transferred: p.transferred || 0,
      total: p.total || 0,
      bytesPerSecond: p.bytesPerSecond || 0,
    };
    pushUpdateState();
  });
  autoUpdater.on("update-downloaded", (info) => {
    updateState = {
      state: "downloaded",
      version: info.version,
      notes: normalizeReleaseNotes(info.releaseNotes),
      releaseDate: info.releaseDate || "",
    };
    log("[updater] update-downloaded:", info.version);
    pushUpdateState();
  });
  autoUpdater.on("error", (err) => {
    updateState = { state: "error", message: String((err && err.message) || err) };
    logErr("[updater] error:", updateState.message);
    pushUpdateState();
  });

  // 打开软件 3s 内立即检查一次，之后每 20 分钟自动检测一次
  // （GitHub 匿名 API 限流 60 次/小时，20 分钟间隔量级安全）
  setTimeout(() => {
    autoUpdater.checkForUpdates().catch((e) => logErr("[updater] 自动检查失败(启动后首查):", e && e.message));
  }, 3 * 1000);
  setInterval(() => {
    // 下载中/已下载完成时跳过自动检查：避免 update-available 事件把 downloaded 状态冲回 available
    if (updateState.state === "downloading" || updateState.state === "downloaded") return;
    autoUpdater.checkForUpdates().catch((e) => logErr("[updater] 自动检查失败(定时):", e && e.message));
  }, 20 * 60 * 1000);
}

// ── IPC:更新操作（手动检查 / 立即安装 / 查询状态 / 当前版本 / 更新历史）──
ipcMain.handle("app:checkForUpdates", async () => {
  if (!autoUpdater) return { state: "unsupported" };
  try {
    return await autoUpdater.checkForUpdates();
  } catch (e) {
    updateState = { state: "error", message: String((e && e.message) || e) };
    pushUpdateState();
    return updateState;
  }
});
ipcMain.handle("app:getUpdateState", () => updateState);
ipcMain.handle("app:downloadUpdate", async () => {
  if (!autoUpdater || updateState.state !== "available") return updateState;
  try {
    await autoUpdater.downloadUpdate();
  } catch (e) {
    updateState = { state: "error", message: String((e && e.message) || e) };
    pushUpdateState();
  }
  return updateState;
});
ipcMain.handle("app:installUpdate", () => {
  if (!autoUpdater || updateState.state !== "downloaded") return false;
  log("[updater] quitAndInstall ->", updateState.version);
  setImmediate(() => { try { autoUpdater.quitAndInstall(); } catch (e) { logErr("[updater] quitAndInstall 失败:", e); } });
  return true;
});
ipcMain.handle("app:getVersion", () => app.getVersion());

// plan-230-1144 M4.2: 更新历史 —— 优先 GitHub Releases API（联网），失败回落本地 CHANGELOG.md
ipcMain.handle("app:getReleaseNotes", async (_e, opts) => {
  const limit = (opts && opts.limit) || 20;
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
    // 网络不可用：回落本地打包 changelog（保证离线可看）
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

// plan-230-1144 M4.2: 拉取"从 fromVersion 到当前"之间各版本的更新说明（汇总展示）
ipcMain.handle("app:getWhatsNew", async (_e, opts) => {
  const from = (opts && opts.from) || "";
  const to = (opts && opts.to) || app.getVersion();
  try {
    const releases = await fetchReleases(30);
    const cmp = (a, b) => {
      const pa = String(a).split(".").map((x) => parseInt(x, 10) || 0);
      const pb = String(b).split(".").map((x) => parseInt(x, 10) || 0);
      for (let i = 0; i < 3; i++) { if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0); }
      return 0;
    };
    const picked = releases
      .map((r) => ({ version: String(r.tag_name || "").replace(/^v/, ""), name: r.name || "", date: r.published_at || "", notes: normalizeReleaseNotes(r.body) }))
      .filter((r) => r.version && (!from || cmp(r.version, from) > 0) && cmp(r.version, to) <= 0)
      .sort((a, b) => cmp(b.version, a.version));
    return { ok: true, source: "github", releases: picked };
  } catch (e) {
    return { ok: false, source: "none", error: String((e && e.message) || e), releases: [] };
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

  // 1. 立即创建窗口并显示加载页
  try {
    createWindow();
  } catch (err) {
    logErr("[chatcoder] createWindow 失败:", err);
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

  // 6. 初始化自动更新（含定时检查,仅打包版）
  initAutoUpdater();
});

app.on("window-all-closed", () => {
  killBackend();
  app.quit();
});
app.on("before-quit", () => {
  killBackend();
});
app.on("will-quit", () => { killBackend(); });
process.on("exit", () => { killBackend(); });

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
