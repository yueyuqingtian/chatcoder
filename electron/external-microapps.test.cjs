"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

function hostFor(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "cc-review-ext-"));
  t.after(() => {
    assert.ok(path.resolve(root).startsWith(path.resolve(os.tmpdir()) + path.sep));
    fs.rmSync(root, { recursive: true, force: true });
  });
  const dir = path.join(root, "demo-app");
  fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, "microapp.json"), JSON.stringify({
    id: "demo-app", version: "1.0.0", frontend: { url: "http://127.0.0.1:{port}/" },
    server: { command: "fake-server", port: 0, readyTimeoutMs: 15 },
    capabilities: { path: "/__microapp/capabilities", methods: [{ name: "ping" }] },
  }));
  const processes = [];
  let kills = 0;
  let healthy = true;
  let answerDelay = 0;
  const requests = [];
  const spawn = (command, args) => {
    const proc = new EventEmitter();
    proc.stdout = new EventEmitter();
    proc.stderr = new EventEmitter();
    if (command === "taskkill") {
      kills++;
      const victim = processes.find((item) => item.pid === Number(args[1]));
      setImmediate(() => { victim?.emit("exit", 0); proc.emit("exit", 0); });
    } else {
      proc.pid = 70000 + processes.length;
      processes.push(proc);
      process.nextTick(() => proc.stdout.emit("data", "http://127.0.0.1:61585"));
    }
    return proc;
  };
  const request = (url, options, callback) => {
    const req = new EventEmitter();
    let body = "";
    req.write = (chunk) => { body += chunk; };
    req.destroy = () => {};
    req.end = () => {
      requests.push({ url, method: options.method, body });
      setTimeout(() => {
        const res = new EventEmitter();
        res.setEncoding = () => {};
        res.statusCode = healthy ? 200 : 500;
        callback(res);
        res.emit("data", JSON.stringify(url.endsWith("/call") ? { ok: true, result: { pong: 1 } } : { ok: healthy }));
        res.emit("end");
      }, answerDelay);
    };
    return req;
  };
  const module = { exports: {} };
  // plan-20261116-review-fixes：只替换操作系统进程和传输，扫描、寻址与启动状态机仍执行真实模块。
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "external-microapps.cjs"), "utf8"), {
    module, exports: module.exports, require: (name) => name === "child_process" ? { spawn } : name === "http" ? { request } : require(name),
    process: { platform: "win32", env: { CHATCODER_MICROAPP_EXT_ROOT: root } },
    Buffer, URL, setTimeout, clearTimeout,
  });
  return {
    host: module.exports.createExternalMicroapps(), processes, requests,
    get kills() { return kills; },
    set healthy(value) { healthy = value; },
    set answerDelay(value) { answerDelay = value; },
  };
}

test("并发打开同一应用只启动一次，运行后再打开复用进程", async (t) => {
  const env = hostFor(t);
  const [a, b] = await Promise.all([env.host.start("demo-app"), env.host.start("demo-app")]);
  assert.equal(a.ok, true);
  assert.equal(b.url, "http://127.0.0.1:61585/");
  assert.equal(env.processes.length, 1);
  assert.equal((await env.host.start("demo-app")).reused, true);
  assert.equal(env.processes.length, 1);
});

test("动态端口应用未打开界面也能按需启动并调用", async (t) => {
  const env = hostFor(t);
  const result = await env.host.call("demo-app", "ping", { x: 1 });
  assert.equal(result.ok, true);
  assert.equal(result.result.pong, 1);
  assert.equal(env.processes.length, 1);
  assert.equal(env.requests.at(-1).url, "http://127.0.0.1:61585/__microapp/call");
  assert.deepEqual(JSON.parse(env.requests.at(-1).body), { method: "ping", args: { x: 1 } });
});

test("就绪失败会终止进程，退出后可以重新启动", async (t) => {
  const env = hostFor(t);
  env.healthy = false;
  assert.equal((await env.host.start("demo-app")).ok, false);
  assert.equal(env.kills, 1);
  await new Promise(setImmediate);
  env.healthy = true;
  assert.equal((await env.host.start("demo-app")).ok, true);
  assert.equal(env.processes.length, 2);
});

test("启动期间停止，不会被迟到健康回包重新标记运行", async (t) => {
  const env = hostFor(t);
  env.answerDelay = 10;
  const pending = env.host.start("demo-app");
  assert.equal(env.host.stop("demo-app").ok, true);
  assert.equal((await pending).ok, false);
  assert.notEqual(env.host.statusOf("demo-app").state, "running");
  assert.equal(env.kills, 1);
});
