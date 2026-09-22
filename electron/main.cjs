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

// ── plan-26-126 M4：伪最大化状态（保住玻璃 + 不惊动任务栏）──
// 为何要伪最大化：系统原生最大化会改写 HWND 样式并触发 DWM 图层重建，透明/acrylic 标记
//   在其中丢失（用户实测"点最大化后透不出桌面、退出也回不来"）。改为自行 setBounds(workArea)
//   就不触发该行为，玻璃全程保留。
// 状态：
//   _pseudoMax           —— 是否处于伪最大化（**唯一判据源**：圆角/直角与 data-maximized 都看它）
//   _pseudoPrevBounds    —— 还原用矩形快照（含位置）
//   _pseudoSuppressUntil —— 程序化 setBounds 的抑制窗，避免被误判成"用户拖动"
//   _convertingNativeMax —— 原生最大化 → 伪最大化的转换防重入
let _pseudoMax = false;
let _pseudoPrevBounds = null;
let _pseudoSuppressUntil = 0;
let _convertingNativeMax = false;
// 明确禁止（防回归，与 N14 对齐）：setSkipTaskbar(true) / display.bounds / alwaysOnTop /
//   setFullScreen(true)。前两者会让窗口覆盖或从任务栏消失，最后一个会关闭 DWM 合成导致玻璃直接失效。

// ── 安全写日志(打包后无 stdout 也不崩溃) + 写入文件 ──
const LOG_DIR = app.isPackaged
  ? require("path").join(app.getPath("userData"), "logs")
  : require("path").join(__dirname, "..", "logs");
try { fs.mkdirSync(LOG_DIR, { recursive: true }); } catch {}
const LOG_FILE = path.join(LOG_DIR, "main.log");

function log(...args) {
  const msg = args.join(" ");
  try {
    if (process.stdout && process.stdout.writable) {
      process.stdout.write(msg + "\n");
    }
  } catch {}
  try {
    fs.appendFileSync(LOG_FILE, new Date().toISOString() + " " + msg + "\n");
  } catch {}
}
function logErr(...args) {
  const msg = args.join(" ");
  try {
    if (process.stderr && process.stderr.writable) {
      process.stderr.write(msg + "\n");
    }
  } catch {}
  try {
    fs.appendFileSync(LOG_FILE, new Date().toISOString() + " [ERROR] " + msg + "\n");
  } catch {}
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
 * @returns {{ok: boolean, backend: string, reason?: string, verified?: number|null}}
 */
function applyGlass(win, on) {
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
  let verified = null;
  try {
    const gd = glassDiag();
    const rb = gd ? gd.dwmReadBack(win) : { ok: false, value: null };
    verified = rb.ok ? rb.value : null;
    if (on && rb.ok && rb.value < 2) {
      // API 被接受但 DWM 未启用 → 明确标记为未生效（历史上正是这种"假成功"误导了排查）
      logErr(`[chatcoder] glass: 回读校验未生效！下发=acrylic 但系统实际=${rb.name}(${rb.value})`);
      res = { ...res, ok: false, reason: `系统未启用材质（回读 ${rb.name}）` };
    }
  } catch (err) {
    logErr("[chatcoder] glass: 回读校验异常:", err && err.message);
  }

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
 * 的直接原因。因此在 0 / 120 / 400ms 各重放一次，最后一次覆盖过渡窗口期。
 *
 * 为什么用内存态而非读文件：重放属高频路径（resize 节流后仍会走），每次读
 * glass-pref.json 既有 IO 成本也存在「读到旧值」的时序风险；_glassWanted 在启动
 * 与 IPC 切换时同步维护。
 */
// ── 动画期免抖：材质重放延后到几何动画结束 ──
//
// 为什么（用户反馈"全屏⇄小窗动画不流畅、界面和内容抖动严重"）：
//   自研补间每帧都在改窗口几何，而 applyGlass 走的是 DWM 材质重设 + **同步回读**
//   （dwmReadBack 也是 FFI 调用）。把它放在动画进行中，等于每帧额外插一段
//   系统调用 + 一次同步等待，与"改几何"抢同一个 UI 线程 ⇒ 掉帧、窗口与内容相位错开。
//   而材质在过渡期本来就会被 DWM 重置，动画中间帧重放也留不住。
//   因此：动画进行中只登记意图，等几何落定（终帧回调）再重放。
let _glassReapplyTimer = null;
function scheduleGlassReapply(win, delay = 0) {
  if (_glassReapplyTimer) clearTimeout(_glassReapplyTimer);
  _glassReapplyTimer = setTimeout(() => {
    _glassReapplyTimer = null;
    syncWindowGlassNow(win);
  }, delay);
}

/** 立即重放材质（原 syncWindowGlass 主体的同步版）。 */
function syncWindowGlassNow(win) {
  if (!win || win.isDestroyed()) return;
  syncWindowChromeState(win); // 圆角与最大化态：仅在真正变化时才有开销（P4）
  if (!_glassWanted) return;
  for (const delay of [0, 160]) {
    setTimeout(() => {
      if (!win || win.isDestroyed() || !_glassWanted) return;
      try {
        const r = applyGlass(win, true);
        if (delay === 160 && r && r.ok === false) {
          logErr("[chatcoder] glass: 重放后仍未生效 →", JSON.stringify(r));
        }
      } catch (err) {
        logErr("[chatcoder] glass: 材质重放异常:", err && err.message);
      }
    }, delay);
  }
}

/** 材质重放统一入口：**运动中自动延后**，静止时立即执行。
 *
 *  plan-26-116 M2 原本要求"事件后立刻多次重放"（DWM 在过渡期会重置 backdrop）；
 *  但本轮用户反馈"动画抖动严重"，实测根因是 applyGlass 内部的 FFI + 同步回读
 *  与自研补间抢同一个 UI 线程。因此保留"多次重放"语义，只把**执行时机**挪到
 *  几何落定之后（终帧回调 / 拖拽结束），动画中间帧一律不再插系统调用。
 *
 *  @param {boolean} [force] true = 忽略运动状态立即重放（拖拽结束、窗口事件等明确收尾点）
 */
function syncWindowGlass(win, force) {
  if (!win || win.isDestroyed()) return;
  if (!force && (_fitAnimTimer || _dragSession)) {
    scheduleGlassReapply(win, 0); // 动画/拖拽进行中：登记意图，落定后补一次
    return;
  }
  syncWindowGlassNow(win);
}

// ── plan-26-126 M4：伪最大化（保住玻璃 + 任务栏保持现状） ──
// 效果保真目标（N13）：铺满**工作区**、四角直角、还原矩形一致、按钮行为一致。
// 任务栏保真（N14）：只用 workArea（不覆盖任务栏）、不调用 setSkipTaskbar（不从任务栏消失）、
//   不置顶（不盖住任务栏）。

/** 本地取 screen 模块（延迟到调用时，避免 app ready 之前访问）。 */
function screenModule() {
  try { return require("electron").screen; } catch { return null; }
}

/** 单一真源判据：伪最大化 / 系统最大化 / 全屏 三者任一为真。
 *  圆角（DONOTROUND）与 data-maximized 注入全都改用它，避免两套判据打架。 */
function isMaximizedLike(win) {
  if (!win || win.isDestroyed()) return false;
  if (_pseudoMax) return true;
  try { return win.isMaximized() || win.isFullScreen(); } catch { return false; }
}

/** 伪最大化矩形 = 窗口所在显示器的 **workArea**（DIP，不含任务栏）。
 *  多屏场景用 getDisplayMatching 取窗口所在屏，不固定主屏。 */
function workAreaOf(win) {
  const fallback = win.getBounds();
  const sc = screenModule();
  if (!sc) return fallback;
  try {
    const d = sc.getDisplayMatching(fallback);
    return (d && d.workArea) || fallback;
  } catch { return fallback; }
}

/** 窗口是否**真的**离开了伪最大化矩形（用于把"用户拖动"与"同值 move 通知"区分开）。
 *
 *  为什么需要：Windows 在最小化瞬间也会发 move 事件，且此时的 bounds 与最小化前
 *  完全一致（实测取证）。旧代码只看 `_pseudoMax`，于是这类"假移动"会被误判成用户
 *  拖窗口，提前把还原快照消费掉 —— 这正是"全屏→最小化→恢复后点缩放还原不了"的根因。
 *  阈值取 2 DIP：吸收 DIP↔物理像素换算的 1px 抖动，又远小于"人手动拖一下"的位移。 */
function _movedAwayFromPseudoRect(win) {
  try {
    const b = win.getBounds();
    const wa = workAreaOf(win);
    return Math.abs(b.x - wa.x) > 2 || Math.abs(b.y - wa.y) > 2
      || Math.abs(b.width - wa.width) > 2 || Math.abs(b.height - wa.height) > 2;
  } catch {
    return false;
  }
}

/** 还原矩形是否**可用**：非空、尺寸合理、且不等于铺满工作区（避免"还原"其实没还原）。
 *
 *  为什么需要这道校验（日志实测暴露）：还原目标读到 `1708x1020`，那正是 workArea 铺满尺寸。
 *  说明 `_pseudoPrevBounds` 在某些路径下被写成了"最大化时的矩形"（例如抑制窗内发生的
 *  resize 先把快照换成了铺满值）。此时执行还原等于原地不动，用户看到的就是"点缩放没反应"。
 *  校验失败时退回"默认小窗尺寸"，宁可还原到一个合理小窗，也不要停在铺满态。
 *
 *  注意：只用于校验**快照**。调用方显式传入的目标矩形属于用户意图（例如用户拖边缘
 *  把窗口拉成与工作区一样大），不可套用本规则，否则会被莫名换成默认小窗。 */
function _resolveRestoreBounds(win, candidate) {
  const wa = workAreaOf(win);
  const okSize = (r) => r && r.width > 0 && r.height > 0
    && !(r.width >= wa.width - 2 && r.height >= wa.height - 2); // 等于铺满 ⇒ 不是有效小窗
  if (okSize(candidate)) return candidate;
  // 兜底：默认小窗尺寸（与建窗一致），位置取工作区居中偏上，避免还原后跑出屏幕
  const w = Math.min(1280, Math.max(960, wa.width - 200));
  const h = Math.min(820, Math.max(640, wa.height - 160));
  const r = {
    x: wa.x + Math.max(0, Math.round((wa.width - w) / 2)),
    y: wa.y + Math.max(0, Math.round((wa.height - h) / 3)),
    width: w,
    height: h,
  };
  log("[chatcoder] glass: 还原快照不可用，退回默认小窗", JSON.stringify(r),
      "| candidate =", JSON.stringify(candidate || null));
  return r;
}

/** 近似原生 drag-restore：把窗口还原到**光标附近**（尺寸取快照，位置为光标居中偏上）。 */
function restoredBoundsNearCursor(win) {
  // 快照可能缺失或被污染（见 _resolveRestoreBounds 说明），先过一道可用性校验
  const prev = _resolveRestoreBounds(win, _pseudoPrevBounds);
  const wa = workAreaOf(win);
  let px = wa.x + Math.round(wa.width / 2);
  let py = wa.y + 40;
  try {
    const sc = screenModule();
    if (sc) { const p = sc.getCursorScreenPoint(); px = p.x; py = p.y; }
  } catch { /* 拿不到光标就用工作区顶部居中 */ }
  // 夹紧范围用 max(lo, hi)：窗口比工作区还大时（多屏/分辨率变化）仍能落回可见区
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
  return {
    x: clamp(px - Math.round(prev.width / 2), wa.x, wa.x + wa.width - prev.width),
    y: clamp(py - 24, wa.y, wa.y + wa.height - prev.height),
    width: prev.width,
    height: prev.height,
  };
}

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
 * 为什么用 executeJavaScript 而不是 IPC：注入一次即完成（无事件订阅/无监听泄漏），
 *   渲染层同步读取 `window.__chatcoderWindowMotion`，零 React 重渲染。
 *   本轮追加：置位/解除时**派发一个 DOM 事件**（chatcoder:window-motion），
 *   渲染层据此在运动结束的那一刻做收尾（宽度锚点还原、终端 fit），
 *   不再依赖"120ms 轮询"——那正是用户反馈"拖完尺寸后内容收敛慢"的来源。
 */
let _windowMotion = false;
function setWindowMotion(win, active) {
  if (!win || win.isDestroyed()) return;
  if (_windowMotion === active) return;
  _windowMotion = active;
  try {
    void win.webContents.executeJavaScript(
      `(() => { window.__chatcoderWindowMotion = ${active ? "true" : "false"};`
      + ` try { document.documentElement.setAttribute('data-window-motion', ${active ? "'1'" : "'0'"}); } catch (e) {}`
      + ` try { window.dispatchEvent(new CustomEvent('chatcoder:window-motion', { detail: { active: ${active ? "true" : "false"} } })); } catch (e) {}`
      + ` return true; })()`, true);
  } catch { /* 渲染层未就绪时忽略 */ }
}

/** 与 Windows 原生全屏过渡同量级（系统默认约 200~240ms，取 220ms 最接近原生观感）。
 *  实测对照：180ms 偏"急"，260ms 偏"拖"。 */
const FULLSCREEN_ANIM_MS = 220;

/** 用**客户区**矩形贴合目标区域（而不是窗口外框）。
 *
 * 为何用 setContentBounds：无边框窗口在 Windows 上仍带一圈不可见的 resize 边框，
 *   setBounds 设的是**外框**，可见内容会内缩约 1px；setContentBounds 直接指定可见客户区。
 *   注（P6 实测更正）：本机实测 `getBounds() === getContentBounds()`（无边框窗口两者等值），
 *   因此内缩并非主因；用户看到的"四周一圈透出桌面"真凶是 **DWM 画的系统边框**
 *   （见 applyWindowCorners 里的 DWMWA_BORDER_COLOR=NONE）。两种口径并存不冲突，保留。
 *
 * 为何要自研补间（plan-26-126 P3/P6）：Electron 的 animate 参数**仅 macOS 生效**，
 *   Windows 上直接 setContentBounds 是"瞬移"。本函数复刻系统缩放缓动：
 *     * 缓动：easeOutCubic（先快后慢，与 Windows 全屏过渡一致）；
 *     * 时长：220ms；
 *     * 驱动：8ms 定时 + **按真实时间插值**（定时抖动不影响曲线形状），
 *       并**合并相同整数矩形**——避免把无意义的重复 setBounds 推给系统；
 *     * 运动中置 motion 标记：让渲染层关门停掉位置补偿类重活（这是流畅度的关键）。
 *
 * @param {BrowserWindow} win
 * @param {{x,y,width,height}} rect 目标矩形
 * @param {boolean} animate 是否带动画
 * @param {() => void} [onDone] 动画结束回调（用于解除 motion 标记）
 */
let _fitAnimTimer = null;

/** 取消进行中的贴合/还原动画（纯清理，不动 motion 标记）。
 *
 *  拖拽起点必须调用：动画每帧都会写 宽/高，与逐帧移动窗口抢同一组几何属性，
 *  表现即用户反馈的"拖拽时窗口大小也在变"。
 *
 *  刻意**不**在这里解除 motion 标记：本函数也被 fitContentBounds 在每次新动画开始时
 *  调用，若顺手置 false，就会把调用方刚置好的 true 覆盖掉，导致开头几帧渲染层误以为
 *  "已静止"而跑起位置补偿类重活。motion 的解除一律交给拖拽结束（endWindowDrag）
 *  或动画终帧回调，两处都是明确的收尾点。 */
function cancelFitAnimation() {
  if (!_fitAnimTimer) return;
  clearTimeout(_fitAnimTimer);
  _fitAnimTimer = null;
}

/** 把"客户区矩形"换算成"窗口外框矩形"（无边框窗口两者可能差 1~2 DIP）。
 *
 *  为什么需要（用户反馈"全屏⇄小窗动画抖动严重"的直接成因之一）：
 *  补间中间帧写的是 setBounds（**外框**），终帧写的是 setContentBounds（**客户区**），
 *  两个口径混用时最后一帧会与倒数第二帧差出边框量 —— 表现为动画收尾时"顿一下/跳一下"。
 *  这里按"当前客户区相对外框的偏移"补偿，让整段动画全部走外框口径，
 *  终帧再切回客户区精确贴合。
 *
 *  偏移取法：客户区左上角在外框内的位移（left = c.x - b.x，top = c.y - b.y），
 *  尺寸取外框与客户区的差 —— 不能假设上下边框等宽（无边框窗口的隐形边框上下并不对称）。 */
function contentToFrameRect(win, rect) {
  try {
    if (typeof win.getContentBounds !== "function") return rect;
    const c = win.getContentBounds();
    const b = win.getBounds();
    return {
      x: Math.round(rect.x - (c.x - b.x)),
      y: Math.round(rect.y - (c.y - b.y)),
      width: Math.round(rect.width + (b.width - c.width)),
      height: Math.round(rect.height + (b.height - c.height)),
    };
  } catch { return rect; }
}

/** 补间步进间隔（毫秒）——按显示器刷新率对齐，**每帧只写一次几何**。
 *
 *  为什么（用户反馈"全屏⇄小窗动画不流畅、界面与内容抖动严重"）：
 *  原实现固定 `setTimeout(step, 8)`，在 60Hz 屏上约合**每帧写两次** setBounds。
 *  每次写几何都会让渲染进程重排一次；一帧排两次就等于把该帧的布局预算翻倍，
 *  且两次排版的内容会落在同一帧的两个不同位置上 —— 视觉上正是"抖动/相位错开"。
 *  现在按刷新率取 1 帧（120Hz ⇒ 8.3ms，60Hz ⇒ 16.7ms），并下限 8ms 兜底。 */
function fitStepIntervalMs(win) {
  try {
    const sc = screenModule();
    if (sc && typeof win.getBounds === "function") {
      const d = sc.getDisplayMatching(win.getBounds());
      const hz = Number(d && d.displayFrequency);
      if (Number.isFinite(hz) && hz >= 30) return Math.max(8, Math.round(1000 / hz));
    }
  } catch { /* 取不到刷新率就用 60Hz 口径 */ }
  return 16;
}

function fitContentBounds(win, rect, animate, onDone) {
  if (!win || win.isDestroyed()) return;
  cancelFitAnimation();
  const hasContent = typeof win.setContentBounds === "function";
  const apply = (r) => (hasContent ? win.setContentBounds(r, false) : win.setBounds(r, false));
  try {
    if (!animate) {
      apply(rect);
      // 瞬时贴合没有"运动过程"，因此必须在这里解除 motion：若本次调用取消了上一段
      // 动画，那段动画的 onDone（唯一会解除标记的地方）已不会再执行，标记就会永久
      // 卡在 true，渲染层此后一直跳过位置补偿。拖拽中除外——此时标记由拖拽会话接管。
      if (!_dragSession) setWindowMotion(win, false);
      if (onDone) onDone();
      return;
    }
    const from = (typeof win.getBounds === "function" ? win.getBounds() : rect);
    // 补间区间**全走外框口径**：终帧才切回客户区精确贴合。
    const toFrame = contentToFrameRect(win, rect);
    const stepMs = fitStepIntervalMs(win);
    const t0 = Date.now();
    const ease = (t) => 1 - Math.pow(1 - t, 3); // easeOutCubic：先快后慢
    let lastKey = "";
    const step = () => {
      if (!win || win.isDestroyed()) { _fitAnimTimer = null; return; }
      const p = Math.min(1, (Date.now() - t0) / FULLSCREEN_ANIM_MS);
      const k = ease(p);
      if (p < 1) {
        const r = {
          x: Math.round(from.x + (toFrame.x - from.x) * k),
          y: Math.round(from.y + (toFrame.y - from.y) * k),
          width: Math.round(from.width + (toFrame.width - from.width) * k),
          height: Math.round(from.height + (toFrame.height - from.height) * k),
        };
        const key = `${r.x},${r.y},${r.width},${r.height}`;
        if (key !== lastKey) { lastKey = key; win.setBounds(r, false); }
        _fitAnimTimer = setTimeout(step, stepMs); // 按刷新率对齐：每帧只写一次几何
      } else {
        _fitAnimTimer = null;
        apply(rect); // 终帧：客户区口径，精确铺满
        if (onDone) onDone();
      }
    };
    step();
  } catch (err) {
    _fitAnimTimer = null;
    logErr("[chatcoder] 贴合客户区矩形失败:", err && err.message);
    if (onDone) onDone();
  }
}

/** 进入伪最大化（不调用 win.maximize()，从根上避开 DWM 图层重建）。 */
function enterPseudoMax(win, restoreBounds) {
  if (!win || win.isDestroyed() || _pseudoMax) return;
  // 快照用**客户区**：与实际可见尺寸同口径，还原后才不会尺寸漂移
  _pseudoPrevBounds = restoreBounds || win.getContentBounds();
  _pseudoMax = true;
  _pseudoSuppressUntil = Date.now() + 700;              // 抑制贴合/动画引发的 move/resize 误判（覆盖 220ms 动画 + 余量）
  syncWindowChromeState(win);                           // data-maximized=1 + DWM DONOTROUND（纯属性，无 FFI 回读）
  // 材质重放**延后到动画结束**（见 fitContentBounds 终帧回调）：
  //   动画中途 applyGlass 的 FFI + 回读与改几何抢 UI 线程，正是动画抖动的直接来源；
  //   而 DWM 在过渡期本就会重置 backdrop，中间帧重放也留不住。
  // 动画开始前先置 motion（渲染层停掉位置补偿类重活），动画结束再解除。
  // 顺序很重要：若先跑动画再置标记，最前面的几帧仍会被渲染层的重活拖慢。
  setWindowMotion(win, true);
  fitContentBounds(win, workAreaOf(win), true, () => {
    setWindowMotion(win, false);
    // 几何已落定：延迟一点再重放材质，让终帧先完成合成（避免与收尾同帧竞争）
    scheduleGlassReapply(win, 60);
  });
  log("[chatcoder] glass: pseudo-max on", JSON.stringify(win.getBounds()));
}

/** 退出伪最大化。overrideBounds 用于"拖动还原到光标附近"与"用户改尺寸后保留新尺寸"。
 *  快照/传入矩形均为**客户区**口径，与进入时一致。
 *  opts.animate 显式指定是否补间：默认按"是否正在拖窗口"决定（拖动中不能补间）。
 *  @returns {{x,y,width,height}|null} 本次实际应用的目标矩形。
 *    返回值供"拖拽起点"直接采用——避免再 getBounds() 二次读回：在非整数缩放屏上
 *    读回值可能因 DIP↔物理像素换算带 1~2 DIP 偏差，拖拽全程冻结它就会把这点偏差固定下来。 */
function exitPseudoMax(win, overrideBounds, opts) {
  if (!win || win.isDestroyed() || !_pseudoMax) return null;
  _pseudoMax = false;
  // 只有**快照**需要过可用性校验（它可能被污染成铺满矩形 → "点缩放没反应"）。
  // 调用方显式传入的 overrideBounds 是用户意图（拖边缘改尺寸后保留新尺寸、拖动还原到
  // 光标附近），必须原样采用，不能套用"等于铺满即视为污染"的规则。
  const b = overrideBounds || _resolveRestoreBounds(win, _pseudoPrevBounds);
  _pseudoPrevBounds = null;
  _pseudoSuppressUntil = Date.now() + 700;
  syncWindowChromeState(win);                           // data-maximized=0 + DWM ROUND（先于动画：圆角应立即回来）
  setWindowMotion(win, true);
  // 拖拽已经开始时不做补间：动画每帧写 width/height 会与逐帧 setBounds 打架，
  // 表现为"拖拽时窗口尺寸也在变"（用户实测）。此时直接落到目标矩形。
  const _animate = opts && opts.animate !== undefined ? opts.animate : !_dragSession;
  if (b) {
    fitContentBounds(win, b, _animate, () => {
      setWindowMotion(win, false);
      // 同 enterPseudoMax：带动画时材质延后到几何落定之后；瞬时贴合则立即同步
      if (_animate) scheduleGlassReapply(win, 60);
      else syncWindowGlassNow(win);
    });
  } else {
    setWindowMotion(win, false);
    syncWindowGlass(win, true);
  }
  log("[chatcoder] glass: pseudo-max off", JSON.stringify(win.getBounds()));
  return b || null;
}

/** 最大化/还原按钮入口（与原生最大化同语义：未最大化→最大化，已最大化→还原）。 */
function togglePseudoMaximize(win) {
  if (!win || win.isDestroyed()) return;
  if (_pseudoMax) { exitPseudoMax(win); return; }
  // 兜底：若此刻已是系统原生最大化态（如系统快捷键先触发了），本次点击按"还原"处理
  if (win.isMaximized()) {
    try { win.unmaximize(); } catch { /* ignore */ }
    syncWindowGlass(win);
    return;
  }
  enterPseudoMax(win);
}

/** 显示器变化后按新 workArea 重新贴合（仅伪最大化状态下生效）。 */
function refitPseudoMax(win) {
  if (!_pseudoMax || !win || win.isDestroyed()) return;
  try {
    _pseudoSuppressUntil = Date.now() + 450;
    _lastChromeState = null; // 圆角需按新显示器重算
    fitContentBounds(win, workAreaOf(win), false); // 贴合不需动画（属于被动重排）
  } catch (err) {
    logErr("[chatcoder] 伪最大化重贴合失败:", err && err.message);
  }
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
    frame: false,
    // plan-26-126 P2：**禁用系统原生最大化**——它是"双击标题栏后玻璃永久丢失"的根因。
    // 原生最大化会改写 HWND 样式并重建 DWM 图层，透明/acrylic 标记在其中丢失且不保证恢复；
    // 关闭后双击 / Snap / Win+↑ 不再触发原生最大化，最大化一律走伪最大化（保玻璃）。
    // 注：主窗口需要最小化与关闭能力，故只关 maximizable。
    maximizable: false,
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
    titleBarStyle: "hidden",
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
  // 注（plan-26-126 M4）："maximize" 不绑 syncWindowGlass —— 它是兜底转换入口（见下）。
  for (const ev of ["show", "restore", "unmaximize", "enter-full-screen", "leave-full-screen"]) {
    mainWindow.on(ev, () => syncWindowGlass(mainWindow));
  }
  // plan-26-126 M4/P2：**防御性兑底**——万一仍然发生了系统原生最大化，立刻回收转伪最大化。
  //   P2 已把窗口设为 maximizable:false，并把标题栏/侧栏头部改为自研拖拽 + 双击伪全屏，
  //   正常情况下本回调**不会触发**；保留它是因为：
  //     * Win+↑ / 任务栏右键"最大化" 等系统入口不受 maximizable 完全屏蔽；
  //     * 代价极低（只在真的发生时跑一次），而漏掉一次的代价是玻璃永久丢失。
  mainWindow.on("maximize", () => {
    if (_convertingNativeMax) return;
    _convertingNativeMax = true;
    try {
      const normal = mainWindow.getNormalBounds(); // 原生最大化前的还原矩形
      mainWindow.unmaximize();                     // 立刻退回，避免停留在原生最大化态
      // 等还原动画起步后再贴合 workArea（同一 tick 内 setBounds 会被动画覆盖）
      setTimeout(() => {
        try {
          if (!mainWindow || mainWindow.isDestroyed()) return;
          if (!_pseudoMax) enterPseudoMax(mainWindow, normal);
        } catch (err) {
          logErr("[chatcoder] 伪最大化转换失败:", err && err.message);
        } finally {
          _convertingNativeMax = false;
        }
      }, 20);
    } catch (err) {
      _convertingNativeMax = false;
      logErr("[chatcoder] 原生最大化回收失败:", err && err.message);
    }
  });
  // plan-308-1555 M2：resize 也会丢材质；
  // plan-26-126 P4（卡顿治理）：**resize 不再重放材质**——拖动窗口时每帧重放（FFI + 回读）
  //   正是"拖尺寸卡顿甚至无响应"的主因。材质是窗口级 DWM 属性，实测 resize 不会丢；
  //   拖动结束后补一次即可。
  // plan-26-126 P6：再加一道"运动中不重放"——自研全屏动画 / 窗口拖拽期间由 motion 标记
  //   抑制，避免在高频几何变更中叠加 FFI + 回读（那是"毛玻璃下拖尺寸卡顿"的直接来源）。
  let _resizeTimer = null;
  // 窗口 resize 的"运动"解除定时器（见下方说明：resize 没有确定终点，只能延时解除）
  let _motionTimer = null;
  mainWindow.on("resize", () => {
    // 伪最大化下用户拖动边缘改尺寸 ⇒ 视为"取消最大化"，保留新尺寸退出（快照用客户区口径）。
    // 最小化/不可见期间不判定：那是系统收窗引起的尺寸变化，不是用户拖边缘。
    if (_pseudoMax && !mainWindow.isMinimized() && mainWindow.isVisible()
        && Date.now() > _pseudoSuppressUntil) {
      exitPseudoMax(mainWindow, mainWindow.getContentBounds());
    }
    // 卡顿治理（用户反馈"尺寸变化时非常卡顿，尤其会话正在运行时"）：
    //   窗口连续 resize 期间，渲染层每帧都要整体重排；其中最贵的是消息流的
    //   "宽度锚点补偿"（capture + 双帧 rAF + 重测后写 scrollTop，写入又触发重排）。
    //   而该补偿此前只在"自研全屏动画 / 拖标题栏"两种情形被抑制，**用户拖窗口边缘改尺寸
    //   这条最高频的路径反而没有**——于是每帧一次全量补偿，与终端/canvas 重排叠加就卡。
    //   这里在 resize 期间统一置 motion 标记，让渲染层跳过这类"位置补偿"重活。
    //   解除策略：停止 resize 一段时间后延时解除（不能用 onDone，因为无确定终点）；
    //   拖拽会话/全屏动画自己会置/清该标记，故二者进行中不解除，避免互相覆盖。
    if (!_dragSession && !_fitAnimTimer) setWindowMotion(mainWindow, true);
    if (_motionTimer) clearTimeout(_motionTimer);
    // 兜底：若 resized 事件未到达（部分程序化 resize 路径），延时解除。
    // 从 180ms 收到 120ms —— 用户反馈"拖完尺寸后内容收敛慢"，这里每多等 60ms
    // 渲染层就多等 60ms 才做宽度锚点还原与终端 fit。
    _motionTimer = setTimeout(() => {
      _motionTimer = null;
      if (_dragSession || _fitAnimTimer) return; // 由它们负责解除
      // 只解除标记：材质重放交给下面的 _resizeTimer 统一做一次，
      // 避免这里和它各跑一次 FFI + 回读（那正是要削减的开销）。
      setWindowMotion(mainWindow, false);
    }, 120);
    if (_resizeTimer) clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      _resizeTimer = null;
      if (_windowMotion) return; // 运动中：材质重放留给结束后的补偿
      syncWindowGlass(mainWindow, true);
    }, 240);
  });
  /** resized：窗口尺寸**落定**后触发一次（Windows/macOS 支持）。
   *  比固定延时准确：用户一松手就解除运动标记并补材质，渲染层当场收尾
   *  （宽度锚点还原 / 终端 fit），不再白等一个固定时长。
   *  渲染层的收尾由 setWindowMotion(false) 派发的 chatcoder:window-motion 事件驱动。 */
  mainWindow.on("resized", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (_dragSession || _fitAnimTimer) return; // 拖拽会话/补间动画自有收尾点
    if (_motionTimer) { clearTimeout(_motionTimer); _motionTimer = null; }
    setWindowMotion(mainWindow, false);
    if (_resizeTimer) { clearTimeout(_resizeTimer); _resizeTimer = null; }
    syncWindowGlass(mainWindow, true);
  });
  // plan-26-126 M4：伪最大化下用户拖动窗口 ⇒ 近似原生 drag-restore（还原到光标附近）。
  //
  // 用户实测问题（本轮修正）：伪全屏 → 最小化（点任务栏图标）→ 再点图标恢复后，
  //   **点「缩放」无法还原**。根因就在这个监听里：Windows 在**最小化瞬间也会发 move 事件**
  //   （实测：bounds 与最小化前完全相同，纯粹是状态切换通知）。旧代码只判 `_pseudoMax`
  //   与抑制窗，于是这次"假移动"被当成用户拖动 → 提前执行 exitPseudoMax，把还原快照
  //   `_pseudoPrevBounds` 消费掉；而此时窗口已被最小化，补间落空，窗口尺寸仍停在
  //   最大化时的铺满值。恢复后 `_pseudoMax=false`，点「缩放」只会再次进入伪最大化
  //   → 看起来"点了没反应/还原不了"。
  // 修正：要求"确实被用户拖动了"，即 ①窗口可见且未最小化；②不在我们自己的拖拽会话中；
  //   ③bounds 相对贴合后的矩形**真的变了**（最小化那种同值 move 直接忽略）；
  //   ④按住左键（真实拖动才有；系统重排/最小化/程序化移动都没有按键）。
  mainWindow.on("move", () => {
    if (!_pseudoMax || !mainWindow || mainWindow.isDestroyed()) return;
    if (Date.now() <= _pseudoSuppressUntil) return;      // 我们自己的程序化移动
    if (_dragSession) return;                            // 自研拖拽会话已在别处处理
    if (mainWindow.isMinimized() || !mainWindow.isVisible()) return;
    if (!_movedAwayFromPseudoRect(mainWindow)) return;   // 同值 move（最小化瞬间等）
    const gd = glassDiag();
    if (gd && typeof gd.isLeftButtonDown === "function" && gd.isLeftButtonDown() === false) return;
    exitPseudoMax(mainWindow, restoredBoundsNearCursor(mainWindow));
  });
  try {
    const { screen } = require("electron");
    const onDisplayChange = () => {
      _lastChromeState = null; // 圆角/最大化态需重算（P4：清缓存以强制重新注入）
      refitPseudoMax(mainWindow); // plan-26-126 M4：按新工作区重新贴合（不跑回主屏）
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
    // plan-26-126 M4：重置伪最大化状态，避免下次建窗沿用旧快照/标志
    _pseudoMax = false;
    _pseudoPrevBounds = null;
    _pseudoSuppressUntil = 0;
    _convertingNativeMax = false;
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
  // plan-26-126 M4：改走**伪最大化**（不再调用 win.maximize()/unmaximize()）。
  // 原因：系统原生最大化会触发 DWM 图层重建，透明/acrylic 标记丢失（玻璃"点一下就没了"）。
  if (!mainWindow) return;
  togglePseudoMaximize(mainWindow);
});
ipcMain.on("window:close", () => { if (mainWindow) mainWindow.close(); });

// ── plan-26-126 P6：自研标题栏拖拽（让双击可被渲染层接管）──
// 为何要自研：`-webkit-app-region: drag` 区域由**系统**处理拖拽与双击，DOM 收不到 dblclick；
//   双击会直接触发系统原生最大化（破坏玻璃且无法还原）。
//   改为渲染层发起拖拽后，双击就可由渲染层自行处理（转伪最大化）。
//
// 历史实现的两个致命错（本轮修正）：
//   ① 用了 `mainWindow.startDrag(...)` —— **BrowserWindow 上不存在这个方法**
//      （`startDrag` 只在 WebContents 上，且语义是"拖文件"，不是拖窗口）。
//      于是每次拖拽都抛 TypeError 后被 catch 静默吞掉 ⇒ 用户反馈"非全屏模式下
//      顶部完全拖不动窗口"。日志里连一行都没有，因为 catch 只写了 logErr 且
//      该分支从未被触发过（说明它连 throw 都发生在更外层）。
//   ② 即便存在，那份实现也只在"按下后立刻调用"时可用；本应用要求 180ms 延迟以
//      区分双击，系统那套拖动循环早已错过时机。
//
// 现在改为**自研拖动循环**：渲染层持续上报屏幕坐标（pointermove），主进程按
//   光标与"按下点相对窗口的偏移"反算新位置，并**显式回写冻结的宽高**。这样：
//     * 与双击判定天然共存（拖拽由渲染层决定何时开始）；
//     * 拖动中不触发 DWM 图层重建，玻璃全程保留；
//     * 到位精确（按屏幕坐标算，不依赖系统拖动循环的时机）；
//     * 尺寸全程冻结（见下方"用户实测问题"的根因说明）。
// 拖拽中置 motion 标记：渲染层停掉位置补偿类重活，拖窗口不发涩。
//
// ── 用户实测问题（本轮修正）：拖拽标题栏时"窗口尺寸会变大，拖得越慢变化越大" ──
//
// 根因（本机 probe 实测，非推断）：**非整数缩放屏上的 setPosition 会把尺寸慢慢撑大**。
//   本机 scaleFactor = 1.5（1 DIP = 1.5 物理像素）。逐帧调用 win.setPosition(x, y) 时
//   Chromium 要把位置换算成物理像素再回写，非整数倍换算带小数，每次调用都会让尺寸
//   向上取整 1 DIP。实测（1204×760 窗口、1.5× 屏、每帧只移动 1 DIP）：
//     · setPosition 200 帧                        ⇒ 宽 +200（**每调用一次涨 1**）
//     · setPosition 每帧 +2 DIP（对齐整数物理像素）⇒ 0（物理像素对齐即免疫）
//     · setPosition 原地不动 200 帧                ⇒ 0（位置不变不触发换算）
//     · setBounds 显式带冻结宽高 200~300 帧        ⇒ 恒为 0
//   这正好解释用户的核心观察"拖得越慢变化越大"：慢拖意味着同样的手部位移产生了更多次
//   pointermove，调用次数越多累加越多；快拖调用少，漂移小到看不出来。
//   也与"顶部中间拖拽时光标没变成缩放箭头"吻合——尺寸不是系统 NC 缩放区改的，
//   而是在我们的拖动循环里自己累加出来的。
//
// 修法：
//   ① 拖拽起点**一次性冻结宽高**（_dragSession.width/height），全程不读窗口实时矩形；
//   ② 移动改用 setBounds({x,y,width,height}) 显式带上冻结宽高——实测漂移恒为 0；
//   ③ 渲染层 pointermove 按 rAF 合并，减少单位时间的几何写入次数（见 useWindowDrag.ts）。
let _dragSession = null; // { offsetX, offsetY, width, height } —— 光标偏移 + **冻结宽高**（DIP）

function endWindowDrag() {
  if (!_dragSession) return;
  _dragSession = null;
  setWindowMotion(mainWindow, false);
}

// 用户实测问题（上一轮修正）：拖拽顶部标题栏时"位置在变、尺寸也在变（乱变）"。
// 当时的根因是**两套几何写入者同时跑**：伪最大化下 move 事件触发 exitPseudoMax，
//   其 220ms 补间每帧写 x/y/width/height；同时逐帧上报按"最大化时的偏移"不断移动。
// 修正（对齐原生 drag-restore 语义）：起点先取消动画，伪最大化时**即时**还原小窗，
//   再按还原后几何重算偏移；配合下方"冻结宽高"彻底消除尺寸变化。
function startWindowDrag() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  try {
    const { screen } = require("electron");
    cancelFitAnimation();       // 停掉贴合动画：它每帧也写 width/height
    let applied = null;
    if (_pseudoMax) {           // 伪最大化 → 原生式还原（不做补间，拖动必须即时跟手）
      // 复用 exitPseudoMax：圆角/材质/抑制窗/motion 标记都在那里统一处置，
      // 避免这里手写一份状态变更与它分叉；并接收它**实际应用**的目标矩形。
      applied = exitPseudoMax(mainWindow, restoredBoundsNearCursor(mainWindow), { animate: false });
    }
    // 基准几何：优先用 exitPseudoMax 返回的目标矩形（精确整数，不经换算），
    // 否则再读当前 bounds。非整数缩放屏上二次读回可能带 1~2 DIP 偏差，
    // 而拖拽全程会冻结这份几何，偏差会被固定下来。
    const p = screen.getCursorScreenPoint();
    const b = applied || mainWindow.getBounds();
    _dragSession = {
      offsetX: p.x - b.x,
      offsetY: p.y - b.y,
      // 【关键】冻结宽高：本次拖拽的每一帧都显式回写这两个值，
      // 既不让位置换算的舍入漂移累积，也不受其它组件改尺寸的影响。
      width: b.width,
      height: b.height,
    };
    setWindowMotion(mainWindow, true);
  } catch (err) {
    _dragSession = null;
    logErr("[chatcoder] 窗口拖拽起点失败:", err && err.message);
  }
}

ipcMain.on("window:dragStart", startWindowDrag);

ipcMain.on("window:dragMove", () => {
  const s = _dragSession;
  if (!s || !mainWindow || mainWindow.isDestroyed()) return;
  try {
    const { screen } = require("electron");
    // 按键状态兜底：渲染层若因指针离开窗口而漏发 pointerup，这里能自行结束，
    // 避免"鼠标已松开但窗口仍跟着光标跑"的粘滞感。
    const gd = glassDiag();
    if (gd && typeof gd.isLeftButtonDown === "function") {
      const down = gd.isLeftButtonDown();
      if (down === false) { endWindowDrag(); return; }
    }
    const p = screen.getCursorScreenPoint();
    // 只改位置：宽高始终用起点冻结值显式回写（见上方根因说明）。
    mainWindow.setBounds({
      x: p.x - s.offsetX,
      y: p.y - s.offsetY,
      width: s.width,
      height: s.height,
    }, false);
  } catch (err) {
    logErr("[chatcoder] 窗口拖拽失败:", err && err.message);
  }
});

ipcMain.on("window:dragEnd", () => {
  endWindowDrag();
  if (!mainWindow || mainWindow.isDestroyed()) return;
  // 拖动结束后补一次材质（DWM 在窗口移动后偶发重置 backdrop）与状态同步
  syncWindowGlass(mainWindow);
});

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
