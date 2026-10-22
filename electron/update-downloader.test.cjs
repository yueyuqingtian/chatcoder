"use strict";
/** 更新下载器单测（node --test electron/update-downloader.test.cjs）。
 * 起本地 HTTP 服务器模拟：Range、断连、5xx / 404、不支持 Range、卡死、校验失败，
 * 验证断点续传、多源切换、断网等待与整包校验。不依赖 electron。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { downloadResumable, DownloadError } = require("./update-downloader.cjs");

const MiB = 1024 * 1024;
const FILE = crypto.randomBytes(12 * MiB + 12345); // 不是块大小的整数倍，覆盖末块
const SHA = crypto.createHash("sha512").update(FILE).digest("base64");

function startServer(buf, opts = {}) {
  const stats = { requests: 0, bytesServed: 0, ranged: 0, failures: 0 };
  const server = http.createServer((req, res) => {
    stats.requests += 1;
    if (opts.hang) return; // 一直不响应（模拟被墙的源）
    if (opts.status) { res.statusCode = opts.status; return res.end(); }
    if (opts.failAfterRequests != null && stats.requests > opts.failAfterRequests) { res.statusCode = 500; return res.end(); }
    const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
    let start = 0;
    let end = buf.length - 1;
    let partial = false;
    if (m && !opts.noRange) { start = Number(m[1]); end = m[2] ? Math.min(Number(m[2]), buf.length - 1) : end; partial = true; stats.ranged += 1; }
    const len = end - start + 1;
    if (partial) {
      res.writeHead(206, { "Content-Range": `bytes ${start}-${end}/${buf.length}`, "Content-Length": len, "Accept-Ranges": "bytes" });
    } else {
      res.writeHead(200, { "Content-Length": len, "Accept-Ranges": opts.noRange ? "none" : "bytes" });
    }
    // 随机断连：发出一部分就掐掉连接（只对分块请求生效，探测不受影响）
    const killAt = opts.failRate && partial && len > 300 * 1024 && Math.random() < opts.failRate
      ? start + Math.floor(len * (0.2 + Math.random() * 0.5)) : -1;
    // 卡死：发出一点点数据后不再发（只卡一次）
    const stall = opts.stallOnce && partial && len > 300 * 1024 && !opts._stalled;
    if (stall) opts._stalled = true;
    let pos = start;
    const PIECE = 64 * 1024;
    const pump = () => {
      if (res.destroyed) return;
      if (stall && pos - start >= 1024) return; // 不再写，也不结束
      if (killAt >= 0 && pos >= killAt) { stats.failures += 1; return req.socket.destroy(); }
      if (pos > end) return res.end();
      const next = Math.min(end + 1, pos + (stall ? 1024 : PIECE));
      const piece = buf.subarray(pos, next);
      stats.bytesServed += piece.length;
      pos = next;
      const ok = res.write(piece);
      if (opts.delayMs) setTimeout(pump, opts.delayMs);
      else if (ok) setImmediate(pump); else res.once("drain", pump);
    };
    pump();
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => {
    resolve({ server, stats, url: `http://127.0.0.1:${server.address().port}/f.exe`, close: () => new Promise((r) => { server.closeAllConnections?.(); server.close(r); }) });
  }));
}

function tmpDir() { return fs.mkdtempSync(path.join(os.tmpdir(), "upd-dl-")); }
function base(extra = {}) {
  const dir = tmpDir();
  return {
    dir,
    opts: {
      dest: path.join(dir, "pending", "f.exe"),
      partDir: path.join(dir, "partial"),
      sha512: SHA, size: FILE.length,
      chunkSize: 1 * MiB, concurrency: 4,
      backoffBaseMs: 5, backoffMaxMs: 40, stallTimeoutMs: 15000, connectTimeoutMs: 5000, probeTimeoutMs: 5000,
      firstConnectTimeoutMs: 3000, switchBackoffMs: [5, 10, 20, 40], recoverIntervalMs: 60000,
      ...extra,
    },
  };
}
async function prep(o) { fs.mkdirSync(path.dirname(o.dest), { recursive: true }); return o; }
const sameFile = (p) => Buffer.compare(fs.readFileSync(p), FILE) === 0;

test("基本：并行分块下载，内容与 sha512 一致，进度单调到 100%", async () => {
  const s = await startServer(FILE);
  const { dir, opts } = base();
  const progress = [];
  try {
    const r = await downloadResumable(await prep({ ...opts, urls: [s.url], onProgress: (p) => progress.push(p) }));
    assert.equal(r.bytes, FILE.length);
    assert.ok(sameFile(opts.dest));
    assert.ok(s.stats.ranged >= 12, "应走分块请求");
    for (let i = 1; i < progress.length; i++) assert.ok(progress[i].transferred >= progress[i - 1].transferred, "进度单调不减");
    assert.equal(Math.round(progress.at(-1).percent), 100);
    assert.ok(!fs.existsSync(path.join(opts.partDir, "f.exe.part")), "完成后断点文件应清理");
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("断点续传：中途取消后再来，只补缺的块", async () => {
  const s = await startServer(FILE, { delayMs: 1 });
  const { dir, opts } = base({ concurrency: 2 });
  try {
    const ac = new AbortController();
    await assert.rejects(
      downloadResumable(await prep({ ...opts, urls: [s.url], signal: ac.signal, onProgress: (p) => { if (p.percent >= 45) ac.abort(); } })),
      (e) => e instanceof DownloadError && e.kind === "aborted",
    );
    const metaPath = path.join(opts.partDir, "f.exe.part.json");
    assert.ok(fs.existsSync(metaPath), "取消后应保留断点进度");
    const doneChunks = JSON.parse(fs.readFileSync(metaPath, "utf-8")).done.length;
    assert.ok(doneChunks >= 1, `取消前至少完成了一块（实际 ${doneChunks}）`);
    await new Promise((r) => setTimeout(r, 50)); // 让服务器侧被中断的响应收尾，避免把它们算进第二次
    const before = s.stats.bytesServed;

    const r = await downloadResumable({ ...opts, urls: [s.url] });
    assert.ok(r.resumedBytes >= doneChunks * MiB - 1, "应识别出已完成的块");
    assert.ok(sameFile(opts.dest));
    // 不变量：第二次传输量 ≈ 缺失部分（不是整包）；留 2MiB 余量给取消时刻在途的块
    const second = s.stats.bytesServed - before;
    assert.ok(second <= FILE.length - r.resumedBytes + 2 * MiB, `第二次只应补缺的块（缺 ${FILE.length - r.resumedBytes}，实际又传了 ${second} 字节）`);
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("断网抖动：随机掐断连接也能下完，且内容正确", async () => {
  const s = await startServer(FILE, { failRate: 0.4 });
  const { dir, opts } = base();
  try {
    const r = await downloadResumable(await prep({ ...opts, urls: [s.url] }));
    assert.ok(r.retries > 0, "应发生过重试");
    assert.ok(s.stats.failures > 0);
    assert.ok(sameFile(opts.dest));
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("多源：主源 404 直接换源", async () => {
  const bad = await startServer(FILE, { status: 404 });
  const good = await startServer(FILE);
  const { dir, opts } = base();
  try {
    const r = await downloadResumable(await prep({ ...opts, urls: [bad.url, good.url] }));
    assert.ok(r.source.endsWith(String(good.server.address().port)));
    assert.ok(sameFile(opts.dest));
  } finally { await bad.close(); await good.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("清理：其它版本遗留的断点文件会在新下载开始时删除，同名的不动", async () => {
  const s = await startServer(FILE);
  const { dir, opts } = base();
  try {
    fs.mkdirSync(opts.partDir, { recursive: true });
    const stale = ["old-0.8.4.exe.part", "old-0.8.4.exe.part.json", "other.txt"].map((n) => path.join(opts.partDir, n));
    for (const f of stale) fs.writeFileSync(f, "x");
    await downloadResumable(await prep({ ...opts, urls: [s.url] }));
    assert.ok(!fs.existsSync(stale[0]) && !fs.existsSync(stale[1]), "遗留的 .part / .part.json 应被清掉");
    assert.ok(fs.existsSync(stale[2]), "非断点文件不能误删");
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("多源：镜像一直不响应，首个连接短超时后切到自建源，不会等满常规超时", async () => {
  const hung = await startServer(FILE, { hang: true });
  const good = await startServer(FILE);
  const { dir, opts } = base({ firstConnectTimeoutMs: 300, connectTimeoutMs: 8000 });
  const t0 = Date.now();
  try {
    const r = await downloadResumable(await prep({ ...opts, urls: [{ url: hung.url, tier: "mirror" }, { url: good.url, tier: "primary" }] }));
    const elapsed = Date.now() - t0;
    assert.ok(elapsed < 4000, `不应等满常规连接超时（实际 ${elapsed}ms）`);
    assert.equal(r.tier, "primary");
    assert.equal(hung.stats.requests, 1, "冷源同一时刻只放一个请求探路");
    assert.ok(sameFile(opts.dest));
  } finally { await hung.close(); await good.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("镜像单连接：并发 4 也只发一个在途请求，并走 fetchFor 指定的直连实现", async () => {
  const mirror = await startServer(FILE, { delayMs: 2 });
  const { dir, opts } = base({ chunkSize: 512 * 1024, concurrency: 4 });
  let directCalls = 0;
  let inflight = 0;
  let peak = 0;
  // 在应用层统计并发：响应体读完才释放计数，避免服务端 close 事件的时序造成误判
  const countingFetch = async (u, init) => {
    directCalls += 1;
    inflight += 1;
    peak = Math.max(peak, inflight);
    const res = await fetch(u, init);
    if (!res.body) { inflight -= 1; return res; }
    const reader = res.body.getReader();
    let settled = false;
    const done = () => { if (!settled) { settled = true; inflight -= 1; } };
    const stream = new ReadableStream({
      async pull(ctrl) {
        const { done: end, value } = await reader.read();
        if (end) { done(); ctrl.close(); } else ctrl.enqueue(value);
      },
      cancel() { done(); return reader.cancel(); },
    });
    return new Response(stream, { status: res.status, statusText: res.statusText, headers: res.headers });
  };
  try {
    const r = await downloadResumable(await prep({
      ...opts,
      urls: [{ url: mirror.url, tier: "mirror" }],
      fetchFor: () => countingFetch,
    }));
    assert.equal(r.tier, "mirror");
    assert.ok(directCalls > 0, "镜像请求应走 fetchFor 指定的直连实现");
    assert.equal(peak, 1, `镜像线路应串行（实际峰值 ${peak}）`);
    assert.ok(sameFile(opts.dest));
  } finally { await mirror.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("优先级固定：GitHub 更快也不会排到自建源前面", async () => {
  const slowPrimary = await startServer(FILE, { delayMs: 1 });
  const fastGh = await startServer(FILE);
  const { dir, opts } = base();
  try {
    const r = await downloadResumable(await prep({ ...opts, urls: [{ url: fastGh.url, tier: "github" }, { url: slowPrimary.url, tier: "primary" }] }));
    assert.equal(r.tier, "primary");
    assert.equal(fastGh.stats.requests, 0, "自建源正常时不应碰 GitHub");
    assert.ok(sameFile(opts.dest));
  } finally { await slowPrimary.close(); await fastGh.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("切换序列：镜像 → 自建源 → 镜像 → 自建源 → GitHub，切换前按退避等待", async () => {
  const p = await startServer(FILE, { status: 503 });
  const m = await startServer(FILE, { status: 503 });
  const g = await startServer(FILE);
  const order = [];
  for (const [name, srv] of [["P", p], ["M", m], ["G", g]]) srv.server.prependListener("request", () => order.push(name));
  const { dir, opts } = base({ switchBackoffMs: [40, 80, 160, 320] });
  const retrying = [];
  const t0 = Date.now();
  try {
    const r = await downloadResumable(await prep({
      ...opts,
      urls: [{ url: p.url, tier: "primary" }, { url: m.url, tier: "mirror" }, { url: g.url, tier: "github" }],
      onPhase: (ph, info) => { if (ph === "retrying" && info.retryInMs != null) retrying.push(`${info.label}->${info.nextLabel}`); },
    }));
    const elapsed = Date.now() - t0;
    assert.equal(r.tier, "github");
    assert.deepEqual(order.slice(0, 5), ["M", "P", "M", "P", "G"], `实际顺序 ${order.join(",")}`);
    assert.deepEqual(retrying, ["镜像->自建源", "自建源->镜像", "镜像->自建源", "自建源->GitHub"]);
    // 40 + 80 + 160 + 320 = 600ms（±15% 抖动）
    assert.ok(elapsed >= 500, `应按退避等待（实际 ${elapsed}ms）`);
    assert.ok(sameFile(opts.dest));
  } finally { await p.close(); await m.close(); await g.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("多连接同时被掐断：只算单块失败，不换源", async () => {
  const primary = await startServer(FILE, { failRate: 0.5 });
  const backup = await startServer(FILE);
  const { dir, opts } = base();
  try {
    // 用两个同层（自建源）的源：同层保持传入顺序，第一个一直有数据流入就不应切到第二个
    const r = await downloadResumable(await prep({ ...opts, urls: [{ url: primary.url, tier: "primary" }, { url: backup.url, tier: "primary" }] }));
    assert.ok(primary.stats.failures > 0, "应发生过断连");
    assert.equal(r.tier, "primary");
    assert.equal(backup.stats.requests, 0, "当前源一直有数据流入，不应切到备用源");
    assert.ok(sameFile(opts.dest));
  } finally { await primary.close(); await backup.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("自动回切：落到 GitHub 后自建源恢复，探测通过即切回自建源", async () => {
  const primary = await startServer(FILE, { status: 503 });
  const gh = await startServer(FILE, { delayMs: 4 });
  const { dir, opts } = base({ recoverIntervalMs: 150, concurrency: 2 });
  const tiers = [];
  try {
    const run = downloadResumable(await prep({
      ...opts,
      urls: [{ url: primary.url, tier: "primary" }, { url: gh.url, tier: "github" }],
      onPhase: (ph, info) => { if (ph === "downloading") tiers.push(info.tier); },
    }));
    // GitHub 出数据后再让自建源恢复
    for (let i = 0; i < 200 && !tiers.includes("github"); i++) await new Promise((r) => setTimeout(r, 10));
    primary.server.removeAllListeners("request");
    primary.server.on("request", okHandler());
    const r = await run;
    assert.ok(tiers.includes("github"), "应先落到 GitHub");
    assert.equal(tiers.at(-1), "primary", `最后应回到自建源（实际 ${tiers.join(",")}）`);
    assert.equal(r.tier, "primary");
    assert.ok(sameFile(opts.dest));
  } finally { await primary.close(); await gh.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("取消原因：abort(reason) 透传到 aborted 错误上", async () => {
  const s = await startServer(FILE, { delayMs: 2 });
  const { dir, opts } = base();
  try {
    const ac = new AbortController();
    await assert.rejects(
      downloadResumable(await prep({ ...opts, urls: [s.url], signal: ac.signal, onProgress: (p) => { if (p.percent > 5) ac.abort("restart"); } })),
      (e) => e.kind === "aborted" && e.reason === "restart",
    );
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("源不支持 Range：退化为单连接顺序下载仍能完成", async () => {
  const s = await startServer(FILE, { noRange: true });
  const { dir, opts } = base();
  try {
    await downloadResumable(await prep({ ...opts, urls: [s.url] }));
    assert.ok(sameFile(opts.dest));
    assert.equal(s.stats.ranged, 0);
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("校验失败：sha512 不一致时清掉断点文件并报错", async () => {
  const s = await startServer(FILE);
  const { dir, opts } = base({ sha512: crypto.createHash("sha512").update("other").digest("base64") });
  try {
    await assert.rejects(downloadResumable(await prep({ ...opts, urls: [s.url] })), (e) => e.kind === "checksum");
    assert.ok(!fs.existsSync(path.join(opts.partDir, "f.exe.part")));
    assert.ok(!fs.existsSync(path.join(opts.partDir, "f.exe.part.json")));
    assert.ok(!fs.existsSync(opts.dest));
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("文件大小与预期不符：该源被判定不可用并报致命错误", async () => {
  const s = await startServer(FILE);
  const { dir, opts } = base({ size: FILE.length + 10 });
  try {
    await assert.rejects(downloadResumable(await prep({ ...opts, urls: [s.url] })), (e) => e.kind === "fatal");
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("数据停滞：卡死的请求被中断并重试", async () => {
  const s = await startServer(FILE, { stallOnce: true });
  const { dir, opts } = base({ stallTimeoutMs: 400 });
  try {
    const r = await downloadResumable(await prep({ ...opts, urls: [s.url] }));
    assert.ok(r.retries >= 1);
    assert.ok(sameFile(opts.dest));
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test("断网等待：离线时先等网络，恢复后继续（上报 waiting 阶段）", async () => {
  const s = await startServer(FILE, { failRate: 1, delayMs: 0 }); // 先把分块请求全掐掉
  const { dir, opts } = base({ offlineMaxWaitMs: 4000 });
  let calls = 0;
  const phases = [];
  try {
    // 前几次被问到是否在线时说「不在线」，同时服务器恢复正常
    const isOnline = () => { calls += 1; if (calls === 3) s.server.removeAllListeners("request"), s.server.on("request", okHandler(s)); return calls > 3; };
    const r = await downloadResumable(await prep({ ...opts, urls: [s.url], isOnline, onPhase: (p) => phases.push(p) }));
    assert.ok(phases.includes("waiting"), `应上报 waiting 阶段（实际 ${phases.join(",")}）`);
    assert.ok(sameFile(opts.dest));
    assert.ok(r.retries > 0);
  } finally { await s.close(); fs.rmSync(dir, { recursive: true, force: true }); }
});

/** 一个始终正常的 Range 处理器（用来在「断网」测试中让服务器恢复） */
function okHandler() {
  return (req, res) => {
    const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
    const start = m ? Number(m[1]) : 0;
    const end = m && m[2] ? Math.min(Number(m[2]), FILE.length - 1) : FILE.length - 1;
    res.writeHead(m ? 206 : 200, { "Content-Range": `bytes ${start}-${end}/${FILE.length}`, "Content-Length": end - start + 1 });
    res.end(FILE.subarray(start, end + 1));
  };
}
