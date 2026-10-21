"use strict";
/** 更新安装包下载器：断点续传 + 多连接 + 多源故障切换（plan-20261021-usage-update）。
 *
 * 为什么自己写：electron-updater 的全量下载是「一条连接、失败即删、从头再来」。安装包 300MB+，
 * 网络一抖（切 WiFi、休眠唤醒、代理抽风）就整包作废；单连接在跨境线路上还常被限速。
 *
 * 做法：
 *  - 分块：把文件切成固定大小的块（默认 4MiB），多条连接并行用 Range 拉取，每块独立重试；
 *  - 断点：已完成的块序号写进 <文件>.part.json，进程重启 / 断网恢复后只补缺的块；
 *  - 多源：先用一小段 Range 探测各个源的吞吐，择优下载；连续失败自动换源，永不放弃网络类故障；
 *  - 校验：整包 sha512 与 latest.yml 一致才改名落地，不一致就清掉重来；
 *  - 不支持 Range 的源退化为单连接顺序下载（失败从头来，但仍会换源重试）。
 *
 * 纯 Node 模块：fetch 由调用方注入——主进程传 Electron 的 net.fetch（继承系统代理 / 证书设置），
 * 单测传全局 fetch。不依赖 electron。
 */
const fs = require("fs");
const fsp = fs.promises;
const path = require("path");
const crypto = require("crypto");

const MiB = 1024 * 1024;
const PROBE_BYTES = 256 * 1024;

class DownloadError extends Error {
  /** kind: network | http | range | size | checksum | disk | aborted | fatal */
  constructor(kind, message, extra) {
    super(message);
    this.name = "DownloadError";
    this.kind = kind;
    Object.assign(this, extra || {});
  }
}

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal && signal.aborted) return reject(new DownloadError("aborted", "已取消"));
  const t = setTimeout(() => { if (signal) signal.removeEventListener("abort", onAbort); resolve(); }, ms);
  const onAbort = () => { clearTimeout(t); reject(new DownloadError("aborted", "已取消")); };
  if (signal) signal.addEventListener("abort", onAbort, { once: true });
});

/** 解析 `bytes a-b/total` */
function parseContentRange(value) {
  const m = /bytes\s+(\d+)-(\d+)\/(\d+|\*)/i.exec(String(value || ""));
  if (!m) return null;
  return { start: Number(m[1]), end: Number(m[2]), total: m[3] === "*" ? 0 : Number(m[3]) };
}

/** 流式 sha512（base64，与 latest.yml 同格式） */
function hashFileSha512(file) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash("sha512");
    fs.createReadStream(file, { highWaterMark: 1 * MiB })
      .on("error", reject)
      .on("data", (d) => h.update(d))
      .on("end", () => resolve(h.digest("base64")));
  });
}

/** 把 HTTP 状态码归类：可重试（暂时性）还是该源已确定不可用 */
function httpFailureKind(status) {
  if (status === 408 || status === 425 || status === 429 || status >= 500) return "transient";
  return "dead"; // 400 / 401 / 403 / 404 / 410 / 416 ...：重试也不会好
}

async function loadMeta(metaPath) {
  try {
    const raw = JSON.parse(await fsp.readFile(metaPath, "utf-8"));
    if (raw && Array.isArray(raw.done)) return raw;
  } catch { /* 不存在或损坏：当作没有进度 */ }
  return null;
}

async function saveMeta(metaPath, meta) {
  try {
    const tmp = metaPath + ".tmp";
    await fsp.writeFile(tmp, JSON.stringify(meta), "utf-8");
    await fsp.rename(tmp, metaPath);
  } catch { /* 进度落盘失败只影响续传，不影响本次下载 */ }
}

/** 删除断点文件（校验失败 / 下载完成后清理） */
async function discardPartial(partPath) {
  await fsp.unlink(partPath).catch(() => {});
  await fsp.unlink(partPath + ".json").catch(() => {});
  await fsp.unlink(partPath + ".json.tmp").catch(() => {});
}

/**
 * @param {object} opts
 * @param {string[]} opts.urls          同一个文件的候选下载地址
 * @param {string}   opts.dest          校验通过后的最终路径
 * @param {string}   opts.partDir       断点文件目录（与 dest 同卷，最后一步才能原子改名）
 * @param {string}   opts.sha512        期望的整包 sha512（base64）
 * @param {number}   [opts.size]        期望总字节数（缺省时取探测到的 Content-Range 总长）
 * @param {Function} [opts.fetchImpl]   (url, init) => Promise<Response>
 * @param {Function} [opts.isOnline]    () => boolean；网络类失败后用来判断是否先等网络
 * @param {Function} [opts.onProgress]  ({total, delta, transferred, percent, bytesPerSecond}) => void
 * @param {Function} [opts.onPhase]     (phase, info) => void；phase: connecting|downloading|waiting|retrying|verifying
 * @param {AbortSignal} [opts.signal]
 * @returns {Promise<{path: string, bytes: number, source: string, retries: number, resumedBytes: number}>}
 */
async function downloadResumable(opts) {
  const {
    urls, dest, partDir, sha512,
    fetchImpl = globalThis.fetch,
    isOnline = () => true,
    onProgress = () => {},
    onPhase = () => {},
    signal,
    logger = {},
    chunkSize = 4 * MiB,
    concurrency = 4,
    connectTimeoutMs = 20000,
    stallTimeoutMs = 15000,
    probeTimeoutMs = 12000,
    probeGraceMs = 800,
    backoffBaseMs = 500,
    backoffMaxMs = 30000,
    offlineMaxWaitMs = 60000,
    switchAfterFailures = 3,
  } = opts;
  const log = (level, ...a) => { try { (logger[level] || logger.info || (() => {}))(...a); } catch { /* ignore */ } };
  if (!Array.isArray(urls) || urls.length === 0) throw new DownloadError("fatal", "没有可用的下载地址");
  if (typeof fetchImpl !== "function") throw new DownloadError("fatal", "缺少 fetch 实现");

  // 内部停止信号：外部取消，或任一 worker 致命失败时置位，让其余 worker 与在途请求立即停下
  const stopCtl = new AbortController();
  const stopSignal = stopCtl.signal;
  if (signal) {
    if (signal.aborted) stopCtl.abort();
    else signal.addEventListener("abort", () => stopCtl.abort(), { once: true });
  }
  const throwIfAborted = () => { if (stopSignal.aborted) throw new DownloadError("aborted", "已取消"); };
  throwIfAborted();

  await fsp.mkdir(partDir, { recursive: true });
  const partPath = path.join(partDir, path.basename(dest) + ".part");
  const metaPath = partPath + ".json";
  // 清理其它版本遗留的断点文件：用户跳过某个版本后，旧的半成品不会再被用到，却白占几百 MB 磁盘
  try {
    for (const f of await fsp.readdir(partDir)) {
      if (f.startsWith(path.basename(dest) + ".part")) continue;
      if (/\.part(\.json(\.tmp)?)?$/.test(f)) await fsp.unlink(path.join(partDir, f)).catch(() => {});
    }
  } catch { /* 目录刚创建或不可读：忽略 */ }

  let size = Number(opts.size) || 0;
  let retries = 0;

  /** 带超时 / 取消联动的 fetch：返回 {res, ctl, dispose}；超时只覆盖「等响应头」阶段 */
  async function request(url, headers, timeoutMs) {
    const ctl = new AbortController();
    const onAbort = () => ctl.abort();
    stopSignal.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    const dispose = () => { clearTimeout(timer); stopSignal.removeEventListener("abort", onAbort); };
    try {
      const res = await fetchImpl(url, { headers, signal: ctl.signal, redirect: "follow", cache: "no-store" });
      clearTimeout(timer);
      return { res, ctl, dispose };
    } catch (e) {
      dispose();
      throwIfAborted();
      throw new DownloadError("network", String((e && e.message) || e));
    }
  }

  /** 读完整个响应体，写入 buf；读不到数据超过 stallTimeoutMs 视为卡死并中断 */
  async function readBody(res, ctl, buf, onBytes) {
    const reader = res.body.getReader();
    let got = 0;
    try {
      for (;;) {
        let timer;
        const stalled = new Promise((_, rej) => { timer = setTimeout(() => { ctl.abort(); rej(new DownloadError("network", "数据停滞")); }, stallTimeoutMs); });
        let chunk;
        try {
          chunk = await Promise.race([reader.read(), stalled]);
        } finally {
          clearTimeout(timer);
        }
        if (chunk.done) break;
        const v = chunk.value;
        if (got + v.length > buf.length) throw new DownloadError("range", "服务器返回的数据多于请求的范围");
        buf.set(v, got);
        got += v.length;
        onBytes(got);
      }
    } catch (e) {
      try { await reader.cancel(); } catch { /* ignore */ }
      throwIfAborted();
      if (e instanceof DownloadError) throw e;
      throw new DownloadError("network", String((e && e.message) || e));
    }
    return got;
  }

  // ───────────── 1) 探测：各源吞吐 / 是否支持 Range / 文件大小是否吻合 ─────────────
  onPhase("connecting", {});
  const sources = urls.map((url, i) => ({ url, id: i, dead: false, noRange: false, speed: 0, fails: 0, label: hostOf(url) }));

  async function probe(src) {
    const t0 = Date.now();
    try {
      const { res, ctl, dispose } = await request(src.url, { Range: `bytes=0-${PROBE_BYTES - 1}` }, probeTimeoutMs);
      try {
        if (res.status === 206) {
          const cr = parseContentRange(res.headers.get("content-range"));
          if (size && cr && cr.total && cr.total !== size) { src.dead = true; src.reason = "size"; return; }
          if (!size && cr && cr.total) size = cr.total;
          // 读完探测段以估算吞吐（含建连）
          const want = Math.min(PROBE_BYTES, (cr ? cr.end - cr.start + 1 : PROBE_BYTES));
          const tmp = Buffer.allocUnsafe(want);
          await readBody(res, ctl, tmp, () => {});
          src.speed = want / Math.max(1, Date.now() - t0) * 1000;
          return;
        }
        if (res.status === 200) {
          src.noRange = true;
          const len = Number(res.headers.get("content-length") || 0);
          if (size && len && len !== size) { src.dead = true; src.reason = "size"; }
          if (!size && len) size = len;
          try { await res.body.cancel(); } catch { /* ignore */ }
          src.speed = 1; // 能通但不能分块：排在支持 Range 的源之后
          return;
        }
        if (httpFailureKind(res.status) === "dead") { src.dead = true; src.reason = `http ${res.status}`; }
        try { await res.body.cancel(); } catch { /* ignore */ }
      } finally { dispose(); }
    } catch (e) {
      if (e && e.kind === "aborted") throw e;
      src.reason = String((e && e.message) || e);
    }
  }

  /** 探测各源。第一个可用的源出现后，只再给其它源一个很短的宽限期（它们可能更快）就开始下载：
   *  被墙的源（比如国内访问 GitHub）要等探测超时才有结果，不能拖慢启动；没探测完的源留在后备里继续探测，
   *  下载结束时一并中断。 */
  async function probeAll() {
    for (let round = 0; ; round++) {
      throwIfAborted();
      const live = sources.filter((s) => !s.dead);
      const tasks = live.map((s) => probe(s));
      await new Promise((resolve) => {
        let finished = 0;
        let grace = null;
        const finish = () => { if (grace) clearTimeout(grace); resolve(); };
        const check = () => {
          if (finished === tasks.length) return finish();
          // 只有「支持分段」的源出现才开始宽限计时：只通了一个不支持 Range 的源时，还要等其它源的结果，
          // 否则会因为它先到而放弃分块下载（退化成单连接、失败从头来）
          if (!grace && live.some((s) => s.speed > 0 && !s.noRange)) grace = setTimeout(finish, probeGraceMs);
        };
        for (const t of tasks) t.then(() => { finished += 1; check(); }, () => { finished += 1; check(); });
        if (stopSignal.aborted) finish(); else stopSignal.addEventListener("abort", finish, { once: true });
      });
      throwIfAborted();
      const alive = sources.filter((s) => !s.dead && s.speed > 0);
      if (alive.length) return alive;
      if (sources.every((s) => s.dead)) {
        throw new DownloadError("fatal", `所有下载源都不可用：${sources.map((s) => `${s.label}(${s.reason || "?"})`).join("、")}`);
      }
      // 全是网络类失败：等网络恢复后再探测（不放弃）
      onPhase("waiting", { reason: "probe" });
      await waitForNetwork(Math.min(backoffMaxMs, backoffBaseMs * 2 ** Math.min(round, 6)));
    }
  }

  async function waitForNetwork(minDelay) {
    const deadline = Date.now() + offlineMaxWaitMs;
    await sleep(minDelay, stopSignal);
    while (!isOnline() && Date.now() < deadline) await sleep(2000, stopSignal);
  }

  const alive = await probeAll();
  // 支持 Range 的源按吞吐降序；不支持的排后面；探测时偶发失败（但没被判死）的源留作后备，
  // 之后换源时仍有机会轮到它（它可能只是探测那一刻网络抖了一下）
  const ranked = alive.sort((a, b) => (Number(a.noRange) - Number(b.noRange)) || (b.speed - a.speed))
    .concat(sources.filter((s) => !s.dead && !(s.speed > 0)));
  const chunked = !ranked[0].noRange;
  if (!size) throw new DownloadError("fatal", "无法确定更新包大小");
  log("info", `[update-dl] 源排序：${ranked.map((s) => `${s.label}(${Math.round(s.speed / 1024)}KB/s${s.noRange ? ",无Range" : ""})`).join(" > ")} size=${size}`);

  // ───────────── 进度记账 ─────────────
  const nChunks = Math.ceil(size / chunkSize);
  const chunkLen = (i) => Math.min(chunkSize, size - i * chunkSize);
  const done = new Set();
  let doneBytes = 0;
  const inflight = new Map(); // 块 -> 已收字节（失败时扣回，避免进度虚高）
  let resumedBytes = 0;
  let lastEmit = 0;
  let lastEmitBytes = 0;
  let samples = [{ t: Date.now(), b: 0 }];
  let phaseNow = "";
  // 最近一次收到数据的时刻：单个分块偶发失败（其余连接仍在出数据）不对外报「重试中」，避免界面来回闪
  let lastBytesAt = Date.now();

  const transferred = () => { let n = doneBytes; for (const v of inflight.values()) n += v; return n; };
  function emitProgress(force) {
    const now = Date.now();
    if (!force && now - lastEmit < 250) return;
    lastEmit = now;
    const tr = Math.min(size, transferred());
    const delta = tr - lastEmitBytes;
    lastEmitBytes = tr;
    samples.push({ t: now, b: tr });
    while (samples.length > 2 && now - samples[0].t > 3000) samples.shift();
    const first = samples[0];
    const bps = now > first.t ? Math.max(0, (tr - first.b) / ((now - first.t) / 1000)) : 0;
    onProgress({ total: size, delta, transferred: tr, percent: size ? (tr / size) * 100 : 0, bytesPerSecond: bps });
  }

  // ───────────── 2) 准备断点文件 ─────────────
  let meta = await loadMeta(metaPath);
  let reuse = !!meta && meta.size === size && meta.sha512 === sha512 && meta.chunkSize === chunkSize && chunked;
  if (reuse) {
    try { reuse = (await fsp.stat(partPath)).size === size; } catch { reuse = false; }
  }
  if (!reuse) { await discardPartial(partPath); meta = { size, sha512, chunkSize, done: [] }; }
  const fh = await fsp.open(partPath, reuse ? "r+" : "w+").catch((e) => { throw new DownloadError("disk", `无法创建下载文件：${e.message}`); });
  try {
    if (!reuse) await fh.truncate(size);
  } catch (e) {
    await fh.close().catch(() => {});
    throw new DownloadError("disk", `磁盘空间不足或无法写入：${e.message}`);
  }
  for (const i of meta.done) {
    if (Number.isInteger(i) && i >= 0 && i < nChunks && !done.has(i)) { done.add(i); doneBytes += chunkLen(i); }
  }
  resumedBytes = doneBytes;
  if (resumedBytes) log("info", `[update-dl] 断点续传：已有 ${Math.round(resumedBytes / MiB)}MiB / ${Math.round(size / MiB)}MiB`);

  let lastSave = 0;
  const persist = async (force) => {
    const now = Date.now();
    if (!force && now - lastSave < 1000) return;
    lastSave = now;
    await saveMeta(metaPath, { size, sha512, chunkSize, done: [...done].sort((a, b) => a - b) });
  };

  // ───────────── 3) 源选择与故障切换 ─────────────
  let active = 0; // ranked 下标
  const currentSource = () => ranked[active % ranked.length];
  function recordFailure(src, err) {
    src.fails += 1;
    if (err && err.kind === "http" && err.hard) { src.dead = true; src.reason = err.message; }
    if (err && (err.kind === "size" || err.kind === "range")) { src.dead = true; src.reason = err.message; }
    if (src.dead || src.fails >= switchAfterFailures) {
      src.fails = 0;
      const live = ranked.filter((s) => !s.dead);
      if (live.length === 0) throw new DownloadError("fatal", `所有下载源都不可用：${ranked.map((s) => `${s.label}(${s.reason || "?"})`).join("、")}`);
      const idx = live.indexOf(src);
      const next = live[(idx + 1) % live.length];
      active = ranked.indexOf(next);
      log("warn", `[update-dl] 切换下载源 -> ${next.label}`);
    }
  }
  const recordSuccess = (src) => { src.fails = 0; };

  // ───────────── 4) 分块下载 ─────────────
  async function fetchChunk(i) {
    const src = currentSource();
    const start = i * chunkSize;
    const len = chunkLen(i);
    const end = start + len - 1;
    const { res, ctl, dispose } = await request(src.url, { Range: `bytes=${start}-${end}` }, connectTimeoutMs);
    try {
      if (res.status !== 206) {
        if (res.status === 200) throw new DownloadError("range", "下载源不支持分段请求");
        const kind = httpFailureKind(res.status);
        throw new DownloadError("http", `HTTP ${res.status}`, { status: res.status, hard: kind === "dead" });
      }
      const cr = parseContentRange(res.headers.get("content-range"));
      if (!cr || cr.start !== start || cr.end !== end || (cr.total && cr.total !== size)) {
        throw new DownloadError("size", "下载源返回的文件范围与预期不符（版本可能已变化）");
      }
      const buf = Buffer.allocUnsafe(len);
      inflight.set(i, 0);
      try {
        const got = await readBody(res, ctl, buf, (n) => { inflight.set(i, n); lastBytesAt = Date.now(); emitProgress(false); });
        if (got !== len) throw new DownloadError("network", `第 ${i} 块数据不完整`);
      } catch (e) {
        inflight.delete(i);
        throw e;
      }
      try {
        await fh.write(buf, 0, len, start);
      } catch (e) {
        inflight.delete(i);
        throw new DownloadError("disk", `写入下载文件失败：${e.message}`);
      }
      inflight.delete(i);
      done.add(i);
      doneBytes += len;
      emitProgress(false);
      await persist(false);
    } finally {
      dispose();
    }
  }

  async function fetchChunkWithRetry(i) {
    for (let attempt = 0; ; attempt++) {
      throwIfAborted();
      const src = currentSource();
      try {
        await fetchChunk(i);
        recordSuccess(src);
        if (phaseNow !== "downloading") { phaseNow = "downloading"; onPhase("downloading", { source: src.label }); }
        return;
      } catch (e) {
        if (e && e.kind === "aborted") throw e;
        if (e && (e.kind === "disk" || e.kind === "fatal")) throw e;
        retries += 1;
        recordFailure(src, e);
        const next = currentSource();
        log("warn", `[update-dl] 第 ${i} 块失败(${e && e.kind}): ${e && e.message}；来源 ${src.label} -> ${next.label}；重试 ${attempt + 1}`);
        const quiet = Date.now() - lastBytesAt > 3000; // 已经好几秒没有任何数据进来，才值得告诉用户
        if (quiet) { onPhase("retrying", { attempt: attempt + 1, source: next.label, message: String((e && e.message) || e) }); phaseNow = "retrying"; }
        const networkLike = !e || e.kind === "network";
        const delay = Math.min(backoffMaxMs, backoffBaseMs * 2 ** Math.min(attempt, 6)) * (0.7 + Math.random() * 0.6);
        if (networkLike && !isOnline()) {
          onPhase("waiting", { reason: "offline" }); phaseNow = "waiting"; // 确定离线：不受 quiet 限制，立刻告知
          await waitForNetwork(Math.min(delay, 2000));
        } else {
          await sleep(delay, stopSignal);
        }
      }
    }
  }

  async function runChunked() {
    const queue = [];
    for (let i = 0; i < nChunks; i++) if (!done.has(i)) queue.push(i);
    let cursor = 0;
    const workers = Array.from({ length: Math.max(1, Math.min(concurrency, queue.length || 1)) }, async () => {
      for (;;) {
        throwIfAborted();
        const i = queue[cursor++];
        if (i === undefined) return;
        await fetchChunkWithRetry(i);
      }
    });
    // 任一 worker 失败 → 立即通知其余停止（在途请求也会被中断），抛出的是「最先出现的真实原因」
    let primaryErr = null;
    await Promise.all(workers.map((w) => w.catch((e) => {
      if (!primaryErr || (primaryErr.kind === "aborted" && e && e.kind !== "aborted")) primaryErr = e;
      stopCtl.abort();
    })));
    if (primaryErr) throw primaryErr;
  }

  // 不支持 Range：单连接顺序下载（失败整体重来，但仍然会换源重试）
  async function runStreaming() {
    for (let attempt = 0; ; attempt++) {
      throwIfAborted();
      const src = currentSource();
      try {
        const { res, ctl, dispose } = await request(src.url, {}, connectTimeoutMs);
        try {
          if (res.status !== 200) {
            throw new DownloadError("http", `HTTP ${res.status}`, { status: res.status, hard: httpFailureKind(res.status) === "dead" });
          }
          inflight.clear();
          doneBytes = 0;
          let pos = 0;
          const reader = res.body.getReader();
          try {
            for (;;) {
              let timer;
              const stalled = new Promise((_, rej) => { timer = setTimeout(() => { ctl.abort(); rej(new DownloadError("network", "数据停滞")); }, stallTimeoutMs); });
              let chunk;
              try { chunk = await Promise.race([reader.read(), stalled]); } finally { clearTimeout(timer); }
              if (chunk.done) break;
              await fh.write(chunk.value, 0, chunk.value.length, pos);
              pos += chunk.value.length;
              doneBytes = pos;
              emitProgress(false);
            }
          } catch (e) {
            try { await reader.cancel(); } catch { /* ignore */ }
            throwIfAborted();
            throw e instanceof DownloadError ? e : new DownloadError("network", String((e && e.message) || e));
          }
          if (pos !== size) throw new DownloadError("network", "数据不完整");
          return;
        } finally { dispose(); }
      } catch (e) {
        if (e && (e.kind === "aborted" || e.kind === "disk")) throw e;
        retries += 1;
        recordFailure(src, e);
        onPhase("retrying", { attempt: attempt + 1, source: currentSource().label, message: String((e && e.message) || e) });
        if (!isOnline()) { onPhase("waiting", { reason: "offline" }); await waitForNetwork(2000); }
        else await sleep(Math.min(backoffMaxMs, backoffBaseMs * 2 ** Math.min(attempt, 6)), stopSignal);
      }
    }
  }

  // ───────────── 5) 执行 → 校验 → 落地 ─────────────
  try {
    onPhase("downloading", { source: currentSource().label });
    phaseNow = "downloading";
    emitProgress(true);
    if (done.size < nChunks || !chunked) {
      if (chunked) await runChunked(); else await runStreaming();
    }
    await persist(true);
  } catch (e) {
    stopCtl.abort();
    await persist(true);
    await fh.close().catch(() => {});
    throw e;
  }
  await fh.close().catch(() => {});
  emitProgress(true);

  onPhase("verifying", {});
  const actual = await hashFileSha512(partPath);
  if (actual !== sha512) {
    await discardPartial(partPath);
    throw new DownloadError("checksum", "安装包校验失败（sha512 不一致），已清除，请重试");
  }
  // Windows 上杀毒软件可能短暂占用刚写完的文件：改名遇到 EBUSY / EPERM 时重试
  for (let i = 0; ; i++) {
    try {
      await fsp.rm(dest, { force: true });
      await fsp.rename(partPath, dest);
      break;
    } catch (e) {
      if (i >= 30 || !(e && (e.code === "EBUSY" || e.code === "EPERM" || e.code === "EACCES"))) {
        throw new DownloadError("disk", `无法写入安装包：${e.message}`);
      }
      await sleep(500, stopSignal);
    }
  }
  await fsp.unlink(metaPath).catch(() => {});
  stopCtl.abort(); // 还在后台探测的源（比如被墙的 GitHub）此时不再需要，立刻释放连接与定时器
  return { path: dest, bytes: size, source: currentSource().label, retries, resumedBytes };
}

function hostOf(url) {
  try { return new URL(url).host; } catch { return String(url).slice(0, 40); }
}

module.exports = { downloadResumable, DownloadError, hashFileSha512, parseContentRange, discardPartial };
