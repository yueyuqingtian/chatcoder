/**
 * 外置微应用宿主（plan-external-microapps · docs/app1/13-external-microapps.md）。
 *
 * 内置微应用是 `app://` 页面 + 宿主桥；外置微应用反过来——**它自己就是一个小应用**：
 * 自己的进程、自己的端口、自己的前后端。chatcoder 只做四件事：
 *
 *   1. 扫描库根（`~/.chatcoder/microapps-ext/<appId>/microapp.json`）认识它们；
 *   2. 按 `server` 声明拉起/停止进程，并轮询 health 判定就绪；
 *   3. 把 `frontend.url` 里的 `{port}` 换成实际端口，交给渲染层塞进面板 webview；
 *   4. 提供一条 HTTP 调用通道（`POST /__microapp/call`），让 AI 能调微应用能力。
 *
 * 为什么进程管理放在主进程而不是渲染层：渲染层拿到的是 webview，进程生命周期必须跟着
 * 应用窗口走（窗口关了要连子进程一起收），而且 `app.quit` 前必须终止整棵进程树，
 * 否则会在用户机器上留下占端口的孤儿——这正是内置形态不需要、外置形态必须解决的差异。
 *
 * 刻意不做的事：
 *  - 不给 http 页面注入宿主桥 preload（协议 §7）：外置微应用的能力靠它自己的后端，
 *    宿主桥只服务 `app://microapp/` 的内置页面；
 *  - 不在崩溃后自动无限重启（会变成崩溃循环），只标记状态并提供手动重试；
 *  - 不校验签名/信任根（协议 §7）：本机代码本来就能被用户直接运行，做假安全感没有意义。
 */
"use strict";

const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

/** 微应用库根：环境变量优先，缺省 ~/.chatcoder/microapps-ext。 */
function resolveRoot() {
  const raw = String(process.env.CHATCODER_MICROAPP_EXT_ROOT || "").trim();
  if (raw) return raw;
  return path.join(os.homedir(), ".chatcoder", "microapps-ext");
}

const ID_RE = /^[a-z0-9][a-z0-9._-]{1,62}$/;
const VERSION_RE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;

/** 默认端点前缀（协议 §3）。 */
const DEFAULT_PREFIX = "/__microapp";

/** 日志环形缓冲上限：崩溃时要把尾部给用户看，但不需要无限占内存。 */
const LOG_TAIL_LINES = 400;
/** 单次调用的结果字符上限（与 AI 侧 microapp_call 同量级，避免一次返回吃掉上下文）。 */
const CALL_RESULT_MAX_CHARS = 8000;

function text(v) {
  return typeof v === "string" ? v.trim() : (typeof v === "number" ? String(v) : "");
}

/**
 * 校验 `microapp.json`（纯函数，便于单测）。返回问题数组，空数组即合规。
 * 与 server 侧 `microapps_ext.validate_microapp_manifest` 保持同一口径：
 * 两侧都会校验同一份文件，口径漂移会导致「列表里有、调用时找不到」。
 */
function validateManifest(manifest, dirName) {
  const problems = [];
  const m = manifest && typeof manifest === "object" ? manifest : {};
  const id = text(m.id);
  if (!id) problems.push("缺少 id");
  else if (!ID_RE.test(id)) problems.push(`id 非法（要求 ${ID_RE}）：${id}`);
  else if (dirName && id !== dirName) problems.push(`id（${id}）与目录名（${dirName}）不一致`);

  const version = text(m.version);
  if (!version) problems.push("缺少 version");
  else if (!VERSION_RE.test(version)) problems.push(`version 不是合法语义化版本：${version}`);

  const frontend = m.frontend && typeof m.frontend === "object" ? m.frontend : {};
  const url = text(frontend.url);
  if (!url) problems.push("缺少 frontend.url");
  else if (!/^https?:\/\//i.test(url)) problems.push(`frontend.url 必须是 http(s) 地址：${url}`);

  if (m.server != null) {
    const server = typeof m.server === "object" ? m.server : null;
    if (!server) problems.push("server 必须是对象");
    else if (!text(server.command)) problems.push("server.command 缺失（有 server 段时必须给启动命令）");
  }

  const caps = m.capabilities && typeof m.capabilities === "object" ? m.capabilities : {};
  if (caps.methods != null && !Array.isArray(caps.methods)) problems.push("capabilities.methods 必须是数组");
  return problems;
}

/** 读一份 microapp.json；读不到/解析失败返回 { manifest:null, problems }。 */
function readManifest(dir) {
  const file = path.join(dir, "microapp.json");
  let raw;
  try {
    raw = fs.readFileSync(file, "utf-8");
  } catch (err) {
    return { file, manifest: null, problems: ["读不到 microapp.json：" + ((err && err.message) || err)] };
  }
  try {
    const manifest = JSON.parse(raw);
    const problems = validateManifest(manifest, path.basename(dir));
    return { file, manifest, problems };
  } catch (err) {
    return { file, manifest: null, problems: ["microapp.json 不是合法 JSON：" + ((err && err.message) || err)] };
  }
}

/** 一次 HTTP 请求（自带超时），返回 { status, body }；body 已按 JSON 解析（失败则给原文）。 */
function request(url, options) {
  const o = options || {};
  return new Promise((resolve, reject) => {
    let settled = false;
    const done = (fn, value) => {
      if (settled) return;
      settled = true;
      fn(value);
    };
    let req;
    try {
      req = http.request(url, { method: o.method || "GET", headers: o.headers || {}, timeout: o.timeout || 8000 }, (res) => {
        let data = "";
        res.setEncoding("utf-8");
        res.on("data", (chunk) => {
          data += chunk;
          if (data.length > 4 * 1024 * 1024) {
            try { req.destroy(); } catch { /* 忽略 */ }
            done(reject, new Error("响应体过大"));
          }
        });
        res.on("end", () => {
          let body = data;
          try { body = JSON.parse(data); } catch { /* 非 JSON 就保留原文 */ }
          done(resolve, { status: res.statusCode || 0, body });
        });
      });
    } catch (err) {
      done(reject, err);
      return;
    }
    req.on("timeout", () => {
      try { req.destroy(); } catch { /* 忽略 */ }
      done(reject, new Error("请求超时：" + url));
    });
    req.on("error", (err) => done(reject, err));
    if (o.body != null) {
      try { req.write(o.body); } catch (err) { done(reject, err); return; }
    }
    req.end();
  });
}

/**
 * 建一台外置微应用宿主。`log` / `logErr` 由 main.cjs 注入（写主进程日志）。
 */
function createExternalMicroapps({ log = () => {}, logErr = () => {} } = {}) {
  /** appId → 运行时记录：{ id, dir, manifest, problems, proc, port, logs, state, ... } */
  const running = new Map();
  const starting = new Map();

  function root() {
    return resolveRoot();
  }

  /** 扫描库根，返回所有外置微应用（含无效条目，problems 非空）。 */
  function scan() {
    const base = root();
    const out = [];
    let dirs = [];
    try {
      dirs = fs.readdirSync(base, { withFileTypes: true });
    } catch {
      return out; // 库根不存在 = 一个都没装，不是错误
    }
    for (const d of dirs) {
      if (d.name.startsWith(".")) continue; // 点目录留给工具自己（.git 之类）
      const dir = path.join(base, d.name);
      // 目录联接（junction）/ 符号链接：readdirSync(withFileTypes) 对它们报 isDirectory()=false，
      // 只判 isDirectory 会把「用户把微应用目录链进来」这种最自然的接入方式整体漏掉。
      // 这里用 statSync 复核（跟随链接），失败才跳过。
      if (!d.isDirectory()) {
        let isDirViaLink = false;
        try {
          isDirViaLink = fs.statSync(dir).isDirectory();
        } catch {
          continue; // 断链：目标不存在，跳过
        }
        if (!isDirViaLink) continue;
      }
      const { manifest, problems } = readManifest(dir);
      out.push({
        id: text(manifest && manifest.id) || d.name,
        dir,
        dirName: d.name,
        manifest,
        problems,
      });
    }
    out.sort((a, b) => String(a.id).localeCompare(String(b.id)));
    return out;
  }

  function findEntry(id) {
    const want = text(id);
    if (!want) return null;
    return scan().find((e) => e.id === want) || null;
  }

  /** 运行时状态（渲染层与 AI 都要看）。 */
  function statusOf(id) {
    const entry = findEntry(id);
    if (!entry) return null;
    const rt = running.get(entry.id);
    return {
      id: entry.id,
      name: text(entry.manifest && entry.manifest.name) || entry.id,
      version: text(entry.manifest && entry.manifest.version),
      description: text(entry.manifest && entry.manifest.description),
      dir: entry.dir,
      problems: entry.problems,
      // valid=false 时不要拉起进程：配置本身不完整，起了也连不上
      valid: entry.problems.length === 0,
      hasServer: !!(entry.manifest && entry.manifest.server),
      state: rt ? rt.state : "stopped",
      port: rt ? rt.port : null,
      exitCode: rt ? rt.exitCode : null,
      url: rt && rt.url ? rt.url : "",
      logTail: rt ? rt.logs.slice(-60) : [],
      methods: entry.manifest && entry.manifest.capabilities && Array.isArray(entry.manifest.capabilities.methods)
        ? entry.manifest.capabilities.methods.map((x) => text(x && x.name)).filter(Boolean)
        : [],
    };
  }

  function list() {
    return scan().map((e) => statusOf(e.id)).filter(Boolean);
  }

  function pushLog(rt, chunk, isErr) {
    const s = String(chunk || "");
    if (!s) return;
    for (const line of s.split(/\r?\n/)) {
      if (!line) continue;
      rt.logs.push((isErr ? "[stderr] " : "") + line);
    }
    if (rt.logs.length > LOG_TAIL_LINES) rt.logs.splice(0, rt.logs.length - LOG_TAIL_LINES);
  }

  /** 把 `{port}` 占位符换成实际端口；没有占位符就原样返回。 */
  function fillUrl(template, port) {
    const raw = text(template);
    if (!raw) return "";
    return raw.replace(/\{port\}/g, String(port || ""));
  }

  /** 从进程输出里等它自报监听端口（协议 §3.1：`server.port=0` 时由微应用决定端口）。
   *
   *  为什么必须这样：声明 0 时 chatcoder 还不知道端口，无法拼出 health 地址；
   *  等 health 回来再取端口会形成鸡生蛋问题（连不上就取不到端口）。
   *  约定微应用在启动输出打印一条 `http://127.0.0.1:<port>`（本协议示例即如此），
   *  这里从日志尾部扫描它——比硬编码端口更适合「多个微应用并存」的场景。
   */
  function discoverPort(rt, timeoutMs) {
    const deadline = Date.now() + (timeoutMs || 20000);
    const re = /https?:\/\/(?:127\.0\.0\.1|localhost):(\d{2,5})/i;
    return new Promise((resolve) => {
      (function tick() {
        for (let i = rt.logs.length - 1; i >= 0; i--) {
          const m = re.exec(String(rt.logs[i]));
          if (m) return resolve(Number(m[1]));
        }
        if (Date.now() > deadline || rt.state === "exited" || rt.cancelled) return resolve(0);
        setTimeout(tick, 200);
      })();
    });
  }

  async function waitHealthy(url, healthPath, timeoutMs, cancelled = () => false) {
    const deadline = Date.now() + (timeoutMs || 20000);
    const target = String(url).replace(/\/$/, "") + (healthPath || DEFAULT_PREFIX + "/health");
    let lastErr = "";
    while (Date.now() < deadline) {
      if (cancelled()) return { ok: false, error: "启动已取消" };
      try {
        const res = await request(target, { timeout: 3000 });
        if (res.status >= 200 && res.status < 300 && !cancelled()) return { ok: true, body: res.body };
        lastErr = "HTTP " + res.status;
      } catch (err) {
        lastErr = (err && err.message) || String(err);
      }
      await new Promise((r) => setTimeout(r, 400));
    }
    return { ok: false, error: lastErr || "超时" };
  }

  /**
   * 启动一个外置微应用。返回 { ok, url, port, state } 或 { ok:false, error }。
   * 已在运行时直接返回现状（幂等）——面板反复点开不应该反复起进程。
   */
  function start(id) {
    // plan-20261116-review-fixes：多个入口必须等待同一次启动，不能覆盖仍在启动的进程句柄。
    const key = text(id);
    if (starting.has(key)) return starting.get(key);
    const pending = startOnce(key).then((result) => {
      const rt = running.get(key);
      if (!result.ok && rt && !rt.cancelled) {
        stop(key);
        rt.state = "failed";
      }
      return result;
    }).finally(() => { if (starting.get(key) === pending) starting.delete(key); });
    starting.set(key, pending);
    return pending;
  }

  async function startOnce(id) {
    const entry = findEntry(id);
    if (!entry) return { ok: false, error: "microapp-not-found: 没有找到 " + text(id) };
    if (entry.problems.length) return { ok: false, error: "microapp-invalid: " + entry.problems.join("；"), problems: entry.problems };
    const manifest = entry.manifest || {};
    const existing = running.get(entry.id);
    if (existing && existing.state === "running" && existing.proc) {
      return { ok: true, url: existing.url, port: existing.port, state: existing.state, reused: true };
    }
    if (existing?.proc) return { ok: false, error: "microapp-not-ready: 上一次进程正在退出，请稍后重试" };

    const frontend = manifest.frontend || {};
    const server = manifest.server;

    // 纯前端微应用：没有 server 段，直接用给定地址（外部已在运行的服务）
    if (!server) {
      const url = fillUrl(frontend.url, text(frontend.url).match(/:(\d+)/) ? RegExp.$1 : "");
      const rt = { id: entry.id, dir: entry.dir, manifest, proc: null, port: null, url, logs: [], state: "running", exitCode: null };
      running.set(entry.id, rt);
      return { ok: true, url, port: null, state: "running", external: true };
    }

    const command = text(server.command);
    const cwdRel = text(server.cwd) || ".";
    const cwd = path.resolve(entry.dir, cwdRel);
    if (!fs.existsSync(cwd)) {
      return { ok: false, error: "microapp-invalid: server.cwd 不存在：" + cwd };
    }

    const rt = {
      id: entry.id, dir: entry.dir, manifest,
      proc: null, port: null, url: "", logs: [], state: "starting", exitCode: null,
    };
    running.set(entry.id, rt);

    // Windows 下用 shell 拉起，才能支持 `node server.cjs` 这种带空格的命令行；
    // 命令字符串来自用户自己的 microapp.json，属受信输入（协议 §7「声明即信任」）。
    let proc;
    try {
      proc = spawn(command, {
        cwd,
        shell: true,
        windowsHide: true,
        env: Object.assign({}, process.env, (server && server.env) || {}),
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (err) {
      rt.state = "exited";
      rt.exitCode = -1;
      pushLog(rt, "启动失败：" + ((err && err.message) || err), true);
      return { ok: false, error: "microapp-spawn-failed: " + ((err && err.message) || err) };
    }

    rt.proc = proc;
    proc.stdout.on("data", (c) => pushLog(rt, c, false));
    proc.stderr.on("data", (c) => pushLog(rt, c, true));
    proc.on("error", (err) => {
      rt.state = "exited";
      rt.exitCode = -1;
      pushLog(rt, "进程错误：" + ((err && err.message) || err), true);
      logErr("[microapps-ext] " + entry.id + " 进程错误:", (err && err.message) || err);
    });
    proc.on("exit", (code, signal) => {
      rt.state = "exited";
      rt.exitCode = code == null ? (signal ? -1 : 0) : code;
      rt.proc = null;
      pushLog(rt, `[进程退出] code=${rt.exitCode}${signal ? " signal=" + signal : ""}`, true);
      log(`[microapps-ext] ${entry.id} 已退出 code=${rt.exitCode}`);
    });

    // 端口：优先用声明值；声明 0 表示微应用自己选端口，此时必须从它的启动输出里解析
    // （协议 §3.1 的补充：`server.port=0` 时微应用要在启动日志打印最终地址，
    //  chatcoder 从中取端口——health 的 port 字段无法用于「首次连接」，那会形成鸡生蛋问题）。
    let port = Number(server.port) || 0;
    const devUrl = text(frontend.devUrl);
    const readyTimeout = Number(server.readyTimeoutMs) || 20000;

    // 开发态优先：devUrl 通了就直接用它（改前端不用重启 chatcoder，协议 §2「即改即用」）
    if (devUrl) {
      const probe = await waitHealthy(devUrl, text(server.healthPath), 2500, () => rt.cancelled);
      if (rt.cancelled) return { ok: false, error: "microapp-not-ready: 启动已取消", state: "stopped" };
      if (probe.ok) {
        rt.state = "running";
        rt.url = devUrl;
        rt.port = Number((devUrl.match(/:(\d+)/) || [])[1]) || null;
        pushLog(rt, "已连上 devUrl：" + devUrl);
        return { ok: true, url: rt.url, port: rt.port, state: rt.state, dev: true };
      }
    }

    // 端口未声明：从进程输出里等它自报（形如 http://127.0.0.1:61585）
    if (!port) {
      port = await discoverPort(rt, readyTimeout);
      if (rt.cancelled) return { ok: false, error: "microapp-not-ready: 启动已取消", state: "stopped" };
      if (!port) {
        rt.state = "failed";
        return {
          ok: false,
          error: "microapp-not-ready: 没能在启动日志里找到监听端口（server.port=0 时请在启动输出打印 http://127.0.0.1:<port>）",
          logTail: rt.logs.slice(-40),
        };
      }
    }

    const url = fillUrl(frontend.url, port);

    const health = await waitHealthy(url, text(server.healthPath), Number(server.readyTimeoutMs) || 20000, () => rt.cancelled);
    if (rt.cancelled) return { ok: false, error: "microapp-not-ready: 启动已取消", state: "stopped" };
    if (!health.ok) {
      rt.state = "failed";
      pushLog(rt, "就绪检查失败：" + (health.error || "未知"), true);
      return { ok: false, error: "microapp-not-ready: " + (health.error || "健康检查未通过"), logTail: rt.logs.slice(-40) };
    }

    rt.state = "running";
    rt.url = url;
    rt.port = port;
    pushLog(rt, "已就绪 " + url);
    log(`[microapps-ext] ${entry.id} 已就绪 ${url}`);
    return { ok: true, url, port, state: "running" };
  }

  /** 停止（整棵进程树；Windows 用 taskkill /T，其它平台用负进程组）。 */
  function stop(id) {
    const entry = findEntry(id);
    if (!entry) return { ok: false, error: "microapp-not-found: " + text(id) };
    const rt = running.get(entry.id);
    if (rt) rt.cancelled = true;
    if (!rt || !rt.proc) {
      if (rt) rt.state = "stopped";
      return { ok: true, already: true };
    }
    const pid = rt.proc.pid;
    try {
      if (process.platform === "win32" && pid) {
        spawn("taskkill", ["/pid", String(pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
      } else if (pid) {
        try { process.kill(-pid, "SIGTERM"); } catch { rt.proc.kill("SIGTERM"); }
      }
    } catch (err) {
      logErr("[microapps-ext] 停止 " + entry.id + " 失败:", (err && err.message) || err);
      return { ok: false, error: String((err && err.message) || err) };
    }
    rt.state = "stopped";
    return { ok: true, pid };
  }

  /** 全部停止（应用退出前调用，避免留下孤儿进程）。 */
  function stopAll() {
    const ids = Array.from(running.keys());
    ids.forEach((id) => {
      try { stop(id); } catch { /* 退出路径尽力而为 */ }
    });
    return ids;
  }

  /** 从微应用取能力清单（`GET <prefix>/capabilities`）。 */
  async function capabilities(id) {
    const entry = findEntry(id);
    if (!entry || entry.problems.length) return { ok: false, error: "microapp-invalid" };
    const rt = running.get(entry.id);
    if (!rt || rt.state !== "running" || !rt.url) return { ok: false, error: "microapp-not-running" };
    const caps = (entry.manifest && entry.manifest.capabilities) || {};
    const target = String(rt.url).replace(/\/$/, "") + (text(caps.path) || DEFAULT_PREFIX + "/capabilities");
    try {
      const res = await request(target, { timeout: 8000 });
      return { ok: true, status: res.status, body: res.body };
    } catch (err) {
      return { ok: false, error: String((err && err.message) || err) };
    }
  }

  /**
   * 调用微应用的一个方法。
   * 方法必须在 `microapp.json` 的 capabilities.methods 里声明过（静态白名单），
   * 与微应用自述清单取交集——两处都声明才放行（协议 §3.2）。
   */
  async function call(id, method, args) {
    const entry = findEntry(id);
    if (!entry) return { ok: false, error: "microapp-not-found: " + text(id) };
    if (entry.problems.length) return { ok: false, error: "microapp-invalid: " + entry.problems.join("；") };

    const declared = entry.manifest && entry.manifest.capabilities && Array.isArray(entry.manifest.capabilities.methods)
      ? entry.manifest.capabilities.methods.map((x) => text(x && x.name)).filter(Boolean)
      : [];
    const want = text(method);
    if (!want) return { ok: false, error: "microapp-bad-request: 缺少 method" };
    if (!declared.length) return { ok: false, error: "microapp-no-methods: 该微应用没有声明任何方法" };
    if (!declared.includes(want)) {
      return { ok: false, error: `method-not-declared: ${want}（已声明：${declared.join(", ")}）` };
    }

    // 未运行就先拉起来：AI 调一个停止状态的微应用，期望是「能用」而不是「先让用户点一下」
    let rt = running.get(entry.id);
    if (!rt || rt.state !== "running" || !rt.url) {
      const started = await start(entry.id);
      if (!started.ok) return { ok: false, error: started.error, logTail: started.logTail };
      rt = running.get(entry.id);
    }
    if (!rt || !rt.url) return { ok: false, error: "microapp-not-running" };

    const caps = (entry.manifest && entry.manifest.capabilities) || {};
    const target = String(rt.url).replace(/\/$/, "") + (text(caps.path) ? String(caps.path).replace(/capabilities$/, "call") : DEFAULT_PREFIX + "/call");
    const payload = JSON.stringify({ method: want, args: args && typeof args === "object" ? args : {} });
    try {
      const res = await request(target, {
        method: "POST",
        headers: { "content-type": "application/json", "content-length": Buffer.byteLength(payload) },
        body: payload,
        timeout: 30000,
      });
      const body = res.body;
      if (res.status >= 400) {
        return { ok: false, error: "microapp-http-" + res.status, detail: typeof body === "string" ? body.slice(0, 500) : body };
      }
      if (body && typeof body === "object" && body.ok === false) {
        return { ok: false, error: text(body.error) || "microapp-call-failed", message: text(body.message) };
      }
      const result = body && typeof body === "object" ? body.result : body;
      let out = result;
      let truncated = false;
      try {
        const s = JSON.stringify(out);
        if (s && s.length > CALL_RESULT_MAX_CHARS) {
          out = s.slice(0, CALL_RESULT_MAX_CHARS);
          truncated = true;
        }
      } catch { /* 不可序列化的结果原样带回，让上层自己处理 */ }
      return { ok: true, result: out, truncated };
    } catch (err) {
      return { ok: false, error: "microapp-unreachable: " + ((err && err.message) || err) };
    }
  }

  return { root, scan, list, statusOf, start, stop, stopAll, call, capabilities, validateManifest, readManifest };
}

module.exports = { createExternalMicroapps, validateManifest, readManifest, resolveRoot, DEFAULT_PREFIX };
