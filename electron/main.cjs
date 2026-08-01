// chatcoder 桌面主进程 (v3 — 健壮启动版)
// 修复: data: URL → loadFile / stdout 安全 / 全局错误捕获 / 图标
const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const fs = require("fs");

const BACKEND_PORT = Number(process.env.CHATCODER_PORT || 8000);
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

    backendProcess.stdout.on("data", (d) => {
      const text = d.toString();
      try { if (process.stdout && process.stdout.writable) process.stdout.write(text); } catch {}
      try { backendLogStream.write(text); } catch {}
    });
    backendProcess.stderr.on("data", (d) => {
      const text = d.toString();
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
          res.resume();
          if (res.statusCode === 200) {
            clearInterval(checkInterval);
            resolve();
          } else if (attempts >= maxAttempts) {
            clearInterval(checkInterval);
            reject(new Error("后端健康检查失败"));
          } else {
            setTimeout(check, intervalMs);
          }
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

// ── 创建主窗口 ──
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    frame: false,
    backgroundColor: "#16181d",
    show: true,
    titleBarStyle: "hidden",
    autoHideMenuBar: true,
    icon: resolveIconPath(),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
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

// ── IPC:系统集成 ──
ipcMain.handle("shell:openPath", (_event, p) => {
  if (p) shell.openPath(p);
});
ipcMain.handle("shell:showItemInFolder", (_event, p) => {
  if (p) shell.showItemInFolder(p);
});

// ── IPC:窗口控制 ──
ipcMain.on("window:minimize", () => { if (mainWindow) mainWindow.minimize(); });
ipcMain.on("window:maximizeToggle", () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on("window:close", () => { if (mainWindow) mainWindow.close(); });

// ── IPC:终端 PTY（child_process.spawn 起 pwsh/cmd，转发 stdin/stdout）──
const ptyProcs = new Map(); // id -> { proc, buf }
let ptySeq = 0;
ipcMain.handle("pty:spawn", (_event, opts) => {
  const id = ++ptySeq;
  const cwd = (opts && opts.cwd) || process.cwd();
  const shellName = process.platform === "win32" ? "pwsh.exe" : (process.env.SHELL || "bash");
  const args = process.platform === "win32" ? ["-NoLogo"] : [];
  let proc;
  try {
    proc = spawn(shellName, args, { cwd, env: process.env });
  } catch (e) {
    log("pty spawn failed:", String(e));
    return { id: 0, error: String(e) };
  }
  ptyProcs.set(id, { proc });
  proc.stdout.on("data", (chunk) => {
    if (mainWindow) mainWindow.webContents.send("pty:data", id, chunk.toString());
  });
  proc.stderr.on("data", (chunk) => {
    if (mainWindow) mainWindow.webContents.send("pty:data", id, chunk.toString());
  });
  proc.on("close", (code) => {
    if (mainWindow) mainWindow.webContents.send("pty:exit", id, code);
    ptyProcs.delete(id);
  });
  return { id };
});

ipcMain.on("pty:write", (_event, id, data) => {
  const entry = ptyProcs.get(id);
  if (entry && entry.proc.stdin && entry.proc.stdin.writable) {
    entry.proc.stdin.write(data);
  }
});

ipcMain.on("pty:kill", (_event, id) => {
  const entry = ptyProcs.get(id);
  if (entry) {
    try { entry.proc.kill(); } catch {}
    ptyProcs.delete(id);
  }
});

// ── IPC:webview 元素标注回传 ──
ipcMain.on("webview:annotate", (_event, payload) => {
  // 转发给渲染进程（主窗口），由前端写入 Composer 草稿
  if (mainWindow) mainWindow.webContents.send("browser:annotation", payload);
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

  // 2. 后台启动后端
  try {
    startBackend();
  } catch (err) {
    logErr("[chatcoder] startBackend 失败:", err);
  }

  // 3. 等待后端就绪
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

  // 4. 加载前端
  try {
    loadFrontend();
  } catch (err) {
    logErr("[chatcoder] loadFrontend 失败:", err);
  }
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
