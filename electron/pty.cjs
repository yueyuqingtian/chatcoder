// 终端 PTY 层（v2.2 对齐 zcode 3.15）
// 优先使用 node-pty（真 PTY，支持 vim/less/top 等全屏交互程序、resize、ANSI 控制）。
// 若原生模块不可用（未编译/ABI 不匹配）则回退 child_process.spawn 伪 PTY，
// 保留基本命令执行能力，避免终端功能完全失效。
const { spawn } = require("child_process");

let ptyModule = null;
try {
  ptyModule = require("node-pty");
} catch {
  ptyModule = null;
}

const usingNodePty = Boolean(ptyModule && typeof ptyModule.spawn === "function");

/** 解析要启动的 shell：优先调用方指定，其次环境变量，最后平台默认。
 * 支持设置项值：auto / pwsh / powershell / cmd / git-bash */
const SHELL_RESOLVE = {
  pwsh: "pwsh.exe",
  powershell: "powershell.exe",
  cmd: "cmd.exe",
};
const GIT_BASH_CANDIDATES = [
  process.env.PROGRAMFILES + "\\Git\\bin\\bash.exe",
  process.env["PROGRAMFILES(X86)"] + "\\Git\\bin\\bash.exe",
  process.env.LOCALAPPDATA + "\\Programs\\Git\\bin\\bash.exe",
];
function resolveGitBash() {
  for (const p of GIT_BASH_CANDIDATES) {
    if (p && require("fs").existsSync(p)) return p;
  }
  return "bash.exe";
}
// v19: win32 默认 shell 按存在性探测（部分机器无 pwsh，直接 spawn 会失败导致终端白屏）
function resolveWinDefaultShell() {
  const fs = require("fs");
  const sys32 = process.env.SystemRoot ? require("path").join(process.env.SystemRoot, "System32") : "";
  const candidates = ["pwsh.exe", "powershell.exe", "cmd.exe"];
  for (const c of candidates) {
    if (sys32 && fs.existsSync(require("path").join(sys32, c))) return c;
  }
  return "cmd.exe";
}
function resolveShell(opts) {
  const want = (opts.shell && opts.shell !== "auto") ? opts.shell : process.env.CHATCODER_SHELL;
  if (want) {
    if (want === "git-bash") return resolveGitBash();
    if (SHELL_RESOLVE[want]) return SHELL_RESOLVE[want];
    return want;
  }
  if (process.platform === "win32") return resolveWinDefaultShell();
  return process.env.SHELL || "bash";
}

/**
 * 创建 PTY 会话。
 * @param {object} opts { id, cwd, cols, rows, shell, env }
 * @returns {{ id:number, pid:number, write:(d:string)=>void, resize:(c:number,r:number)=>void,
 *            kill:()=>void, onData:(cb:Function)=>void, onExit:(cb:Function)=>void, isPty:boolean }}
 */
function createPty(opts) {
  const id = opts.id;
  const cwd = opts.cwd || process.cwd();
  const shell = resolveShell(opts);
  const cols = opts.cols || 80;
  const rows = opts.rows || 24;
  const env = { ...process.env, ...(opts.env || {}) };
  const dataListeners = [];
  const exitListeners = [];
  let proc = null;
  let exited = false;

  const emitData = (d) => { for (const cb of dataListeners) { try { cb(d); } catch {} } };
  const emitExit = (code) => {
    if (exited) return;
    exited = true;
    for (const cb of exitListeners) { try { cb(code); } catch {} }
  };
  const onData = (cb) => {
    dataListeners.push(cb);
    return () => { const i = dataListeners.indexOf(cb); if (i >= 0) dataListeners.splice(i, 1); };
  };
  const onExit = (cb) => {
    exitListeners.push(cb);
    return () => { const i = exitListeners.indexOf(cb); if (i >= 0) exitListeners.splice(i, 1); };
  };

  if (usingNodePty) {
    try {
      proc = ptyModule.spawn(shell, [], {
        name: "xterm-256color",
        cols,
        rows,
        cwd,
        env,
        useConpty: process.platform === "win32",
      });
      proc.onData((d) => emitData(d));
      proc.onExit(({ exitCode }) => emitExit(exitCode));
      return {
        id,
        pid: proc.pid,
        isPty: true,
        write: (d) => { try { proc.write(d); } catch {} },
        resize: (c, r) => { try { proc.resize(c, r); } catch {} },
        kill: () => { try { proc.kill(); } catch {} },
        onData,
        onExit,
      };
    } catch (e) {
      // node-pty spawn 失败时回退到 spawn
      proc = null;
    }
  }

  // 回退：child_process.spawn 伪 PTY（无 ANSI resize，交互程序不完整）
  const args = process.platform === "win32" ? ["-NoLogo"] : [];
  proc = spawn(shell, args, { cwd, env, windowsHide: true });
  proc.stdout && proc.stdout.on("data", (c) => emitData(c.toString()));
  proc.stderr && proc.stderr.on("data", (c) => emitData(c.toString()));
  proc.on("close", (code) => emitExit(code));
  proc.on("error", (err) => emitData(`\r\n[终端错误] ${err.message}\r\n`));
  return {
    id,
    pid: proc.pid,
    isPty: false,
    write: (d) => { if (proc.stdin && proc.stdin.writable) proc.stdin.write(d); },
    resize: () => { /* spawn 伪 PTY 不支持 resize */ },
    kill: () => { try { proc.kill(); } catch {} },
    onData,
    onExit,
  };
}

module.exports = { createPty, usingNodePty };
