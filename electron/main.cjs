// chatcoder 桌面主进程 (v3 — 健壮启动版)
// 修复: data: URL → loadFile / stdout 安全 / 全局错误捕获 / 图标
const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const http = require("http");
const net = require("net");
const fs = require("fs");

// ── 后端端口选择（v2.1: 冲突时自动选空闲端口）──
function probePort(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => srv.close(() => resolve(true)));
    srv.listen(port, "127.0.0.1");
  });
}

async function pickBackendPort() {
  const base = Number(process.env.CHATCODER_PORT || 8000);
  // 探活：端口已被占用则向后找空闲端口（上限 +50）
  for (let p = base; p < base + 50; p++) {
    // 8000 可能被 CLodop 打印服务占用（已知共存场景），跳过占用的端口即可
    if (await probePort(p)) return p;
  }
  return base; // 全部被占时退回默认，交给后端报错
}

let BACKEND_PORT = Number(process.env.CHATCODER_PORT || 8000);
let backendProcess = null;
let mainWindow = null;
let backendReady = false;

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
function startBackend() {
  try {
    const dir = resolveBackendDir();
    const exe = path.join(dir, "chatcoder-server.exe");
    if (!fs.existsSync(exe)) {
      logErr("[chatcoder] 后端可执行文件不存在:", exe);
      return null;
    }
    log("[chatcoder] 启动后端:", exe);

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
            let isJson = false;
            try {
              const ct = res.headers["content-type"] || "";
              isJson = ct.includes("application/json") || (body.trim().startsWith("{") && JSON.parse(body).status === "ok");
            } catch { isJson = false; }
            if (res.statusCode === 200 && isJson) {
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

// ── 毛玻璃（plan-548）──
// Win11 才支持 DWM backgroundMaterial(acrylic)，且与 transparent: true 互斥——
// Win11 必须建非透明窗口走系统材质；Win10/mac 保持透明窗口 + CSS 半透明降级。
function isWin11Plus() {
  if (process.platform !== "win32") return false;
  const m = /^10\.0\.(\d+)/.exec(os.release());
  return !!m && Number(m[1]) >= 22000;
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

// ── 创建主窗口 ──
function createWindow() {
  const win11 = isWin11Plus();
  const glassOn = win11 && readGlassPref();
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    frame: false,
    // plan-548: Win11 非透明窗口 + DWM acrylic（与 transparent 互斥）；glass off 时
    // 显式 "none"（默认 auto 可能被 DWM 施加 Mica）；Win10/mac 维持透明窗口原状。
    transparent: !win11,
    backgroundMaterial: win11 ? (glassOn ? "acrylic" : "none") : undefined,
    backgroundColor: win11 ? (glassOn ? "#00000000" : "#16181d") : undefined,
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
  mainWindow.once("ready-to-show", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.show();
  });

  // 先加载本地 loading.html(不用 data: URL)
  const loadingPath = path.join(__dirname, "loading.html");
  if (fs.existsSync(loadingPath)) {
    mainWindow.loadFile(loadingPath);
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

  mainWindow.on("closed", () => { mainWindow = null; });
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
// 在特定外部应用中打开目录（explorer / vscode / idea / terminal）
ipcMain.handle("shell:openInApp", (_event, target, projectPath) => {
  if (!projectPath) return false;
  try {
    const p = path.normalize(projectPath);
    if (target === "explorer") {
      shell.openPath(p);
      return true;
    }
    if (target === "vscode") {
      const { spawn } = require("child_process");
      spawn("code", [p], { shell: true, detached: true });
      return true;
    }
    if (target === "idea") {
      const { spawn } = require("child_process");
      spawn("idea", [p], { shell: true, detached: true, windowsHide: true });
      return true;
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
      return true;
    }
    shell.openPath(p);
    return true;
  } catch (e) {
    logErr("[shell:openInApp] 失败: " + e.message);
    return false;
  }
});
// v23: 打开外部 URL（ta3 登录授权跳转，走系统默认浏览器）
ipcMain.handle("shell:openExternal", (_event, url) => {
  if (url && /^https?:\/\//i.test(String(url))) shell.openExternal(String(url));
});

// ── IPC:窗口控制 ──
ipcMain.on("window:minimize", () => { if (mainWindow) mainWindow.minimize(); });
ipcMain.on("window:maximizeToggle", () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on("window:close", () => { if (mainWindow) mainWindow.close(); });

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

// ── IPC:毛玻璃模式（plan-546 / plan-548）──
// Win11：切换 DWM acrylic 真磨砂（非透明窗口，运行时双向切换有效）；
// Win10/老版：setBackgroundMaterial 不可用或无效，静默降级为 CSS 半透明（透明窗口已开）。
ipcMain.handle("window:setGlass", (_e, on) => {
  writeGlassPref(!!on); // plan-548: 落盘，下次启动直接以正确材质建窗（见 createWindow）
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  try {
    if (typeof mainWindow.setBackgroundMaterial === "function") {
      mainWindow.setBackgroundMaterial(on ? "acrylic" : "none");
      log("[chatcoder] glass: material =", on ? "acrylic" : "none");
    }
  } catch (err) {
    log("[chatcoder] glass: setBackgroundMaterial unavailable, fallback to CSS alpha:", err && err.message);
  }
  return true;
});

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
let autoUpdater = null;
let updateState = { state: "idle" };

function pushUpdateState() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send("app:updateStatus", updateState); } catch {}
  }
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
    updateState = { state: "checking" };
    log("[updater] checking-for-update");
    pushUpdateState();
  });
  autoUpdater.on("update-available", (info) => {
    updateState = { state: "available", version: info.version };
    log("[updater] update-available:", info.version, "current:", app.getVersion());
    pushUpdateState();
  });
  autoUpdater.on("update-not-available", (info) => {
    updateState = { state: "none", version: info && info.version };
    log("[updater] update-not-available");
    pushUpdateState();
  });
  autoUpdater.on("download-progress", (p) => {
    updateState = { state: "downloading", percent: Math.round(p.percent || 0), transferred: p.transferred || 0, total: p.total || 0 };
    pushUpdateState();
  });
  autoUpdater.on("update-downloaded", (info) => {
    updateState = { state: "downloaded", version: info.version };
    log("[updater] update-downloaded:", info.version);
    pushUpdateState();
  });
  autoUpdater.on("error", (err) => {
    updateState = { state: "error", message: String((err && err.message) || err) };
    logErr("[updater] error:", updateState.message);
    pushUpdateState();
  });

  // 打开软件 3s 内立即检查一次，之后每 30 分钟自动检测一次
  // （GitHub 匿名 API 限流 60 次/小时，30 分钟间隔量级安全）
  setTimeout(() => {
    autoUpdater.checkForUpdates().catch((e) => logErr("[updater] 自动检查失败(启动后首查):", e && e.message));
  }, 3 * 1000);
  setInterval(() => {
    autoUpdater.checkForUpdates().catch((e) => logErr("[updater] 自动检查失败(定时):", e && e.message));
  }, 30 * 60 * 1000);
}

// ── IPC:更新操作（手动检查 / 立即安装 / 查询状态 / 当前版本）──
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

  // 2. 端口探活（8000 被打印服务等占用时自动换空闲端口）
  BACKEND_PORT = await pickBackendPort();
  if (BACKEND_PORT !== Number(process.env.CHATCODER_PORT || 8000)) {
    log("[chatcoder] 默认端口被占用，改用端口:", BACKEND_PORT);
  }

  // 3. 后台启动后端
  try {
    startBackend();
  } catch (err) {
    logErr("[chatcoder] startBackend 失败:", err);
  }

  // 4. 等待后端就绪
  try {
    await waitForBackend();
    backendReady = true;
    log("[chatcoder] 后端就绪");
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
app.on("before-quit", () => { killBackend(); });
app.on("will-quit", () => { killBackend(); });
process.on("exit", () => { killBackend(); });

function killBackend() {
  if (backendProcess) {
    try { backendProcess.kill("SIGTERM"); } catch {}
    backendProcess = null;
  }
}
