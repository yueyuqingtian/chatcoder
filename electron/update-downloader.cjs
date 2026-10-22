"use strict";
/** 更新安装包下载器：断点续传 + 多连接 + 多源故障切换（plan-20261021-usage-update）。
 *
 * 为什么自己写：electron-updater 的全量下载是「一条连接、失败即删、从头再来」。安装包 300MB+，
 * 网络一抖（切 WiFi、休眠唤醒、代理抽风）就整包作废；单连接在跨境线路上还常被限速。
 *
 * 做法：
 *  - 分块：把文件切成固定大小的块（默认 4MiB），多条连接并行用 Range 拉取，每块独立重试；
 *  - 断点：已完成的块序号写进 <文件>.part.json，进程重启 / 断网恢复后只补缺的块；
 *  - 多源：按固定优先级 镜像 > 自建源 > GitHub 选源，失败按退避有序切换，不在最高优先级源上时
 *    定时探测并自动回切（plan-20261022-update-route，见 update-source-policy.cjs）；网络类故障永不放弃；
 *  - 镜像单连接：gh-proxy 对多连接并发会主动降速，镜像线路固定一个在途请求；
 *  - 校验：整包 sha512 与 latest.yml 一致才改名落地，不一致就清掉重来；
 *  - 所有源都不支持 Range 时退化为单连接顺序下载（失败从头来，但仍会换源重试）。
 *
 * 纯 Node 模块：fetch 由调用方注入——主进程传 Electron 的 net.fetch（继承系统代理 / 证书设置），
 * 单测传全局 fetch。镜像线路需要绕开代理，可用 fetchFor 按源指定直连实现。不依赖 electron。
 */
const fs = require("fs");
const fsp = fs.promises;
const path = require("path");
const crypto = require("crypto");
const { createSourceController, normalizeSources } = require("./update-source-policy.cjs");

const MiB = 1024 * 1024;
const PROBE_BYTES = 64 * 1024;

class DownloadError extends Error {
  /** kind: network | http | range | size | checksum | disk | aborted | fatal */
  constructor(kind, message, extra) {
    super(message);
    this.name = "DownloadError";
    this.kind = kind;
    Object.assign(this, extra || {});
  }
}

/** 取消原因（cancel=用户取消 / restart=切换线路后重新开始），随 aborted 错误抛出，供上层区分 */
const abortReason = (signal) => (signal && typeof signal.reason === "string" ? signal.reason : "cancel");

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal && signal.aborted) return reject(new DownloadError("aborted", "已取消", { reason: abortReason(signal) }));
  const t = setTimeout(() => { if (signal) signal.removeEventListener("abort", onAbort); resolve(); }, ms);
  const onAbort = () => { clearTimeout(t); reject(new DownloadError("aborted", "已取消", { reason: abortReason(signal) })); };
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
 * @param {Array<string|{url: string, tier: "primary"|"mirror"|"github", label?: string}>} opts.urls
 *        同一个文件的候选下载地址；按层级 自建源 > 镜像 > GitHub 使用（字符串数组：第一个视为自建源）
 * @param {string}   opts.dest          校验通过后的最终路径
 * @param {string}   opts.partDir       断点文件目录（与 dest 同卷，最后一步才能原子改名）
 * @param {string}   opts.sha512        期望的整包 sha512（base64）
 * @param {number}   [opts.size]        期望总字节数（缺省时从第一个连通的源取 Content-Range 总长）
 * @param {Function} [opts.fetchImpl]   (url, init) => Promise<Response>
 * @param {Function} [opts.fetchFor]    (src) => fetch 实现；用于镜像走直连、自建源 / GitHub 走默认代理
 * @param {Function} [opts.isOnline]    () => boolean；网络类失败后用来判断是否先等网络
 * @param {Function} [opts.onProgress]  ({total, delta, transferred, percent, bytesPerSecond}) => void
 * @param {Function} [opts.onPhase]     (phase, info) => void；phase: connecting|downloading|waiting|retrying|verifying
 *        info: {source, tier, label, attempt?, retryInMs?, nextLabel?, message?}
 * @param {AbortSignal} [opts.signal]   abort(reason) 的 reason 会带到 aborted 错误的 reason 上
 * @returns {Promise<{path: string, bytes: number, source: string, tier: string, label: string, retries: number, resumedBytes: number}>}
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
    // 切到一个还没连通的源时，首个请求的等待上限：被墙 / 卡死的源不能拖住整个下载
    firstConnectTimeoutMs = 8000,
    stallTimeoutMs = 15000,
    probeTimeoutMs = 12000,
    backoffBaseMs = 500,
    backoffMaxMs = 30000,
    // 换源前的退避序列（plan-20261022-update-route：2s → 4s → 8s → 16s → 30s 封顶）
    switchBackoffMs = [2000, 4000, 8000, 16000, 30000],
    switchRounds = 2,
    // 不在最高优先级源上时，每隔多久探测一次更高优先级的源，通了就切回去
    recoverIntervalMs = 60000,
    offlineMaxWaitMs = 60000,
  } = opts;
  const log = (level, ...a) => { try { (logger[level] || logger.info || (() => {}))(...a); } catch { /* ignore */ } };
  const sources = normalizeSources(urls);
  if (sources.length === 0) throw new DownloadError("fatal", "没有可用的下载地址");
  if (typeof fetchImpl !== "function") throw new DownloadError("fatal", "缺少 fetch 实现");

  // 内部停止信号：外部取消，或任一 worker 致命失败时置位，让其余 worker 与在途请求立即停下
  const stopCtl = new AbortController();
  const stopSignal = stopCtl.signal;
  let cancelReason = "cancel";
  if (signal) {
    const onExternalAbort = () => { cancelReason = abortReason(signal); stopCtl.abort(); };
    if (signal.aborted) onExternalAbort();
    else signal.addEventListener("abort", onExternalAbort, { once: true });
  }
  const abortedErr = () => new DownloadError("aborted", "已取消", { reason: cancelReason });
  // 当前这一轮工作的停止信号：分块下载期间是 runChunked 自己的信号（某个 worker 致命失败时只停这一轮，
  // 之后仍可退化为单连接下载），其余时候就是 stopSignal
  let workSignal = stopSignal;
  const throwIfAborted = () => {
    if (stopSignal.aborted) throw abortedErr();
    if (workSignal.aborted) throw new DownloadError("aborted", "本轮下载已停止", { reason: "internal" });
  };
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

  /** 按源解析 fetch：镜像可注入直连实现（绕过系统代理），其余沿用默认 */
  const fetchFor = typeof opts.fetchFor === "function" ? opts.fetchFor : () => fetchImpl;

  /** 带超时 / 取消联动的 fetch：返回 {res, ctl, dispose}；超时只覆盖「等响应头」阶段 */
  async function request(src, headers, timeoutMs) {
    const ctl = new AbortController();
    const sig = workSignal;
    const onAbort = () => ctl.abort();
    if (sig.aborted) ctl.abort(); else sig.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    const dispose = () => { clearTimeout(timer); sig.removeEventListener("abort", onAbort); };
    try {
      const res = await fetchFor(src)(src.url, { headers, signal: ctl.signal, redirect: "follow", cache: "no-store" });
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

  async function waitForNetwork(minDelay) {
    const deadline = Date.now() + offlineMaxWaitMs;
    await sleep(minDelay, workSignal).catch((e) => { throwIfAborted(); throw e; });
    while (!isOnline() && Date.now() < deadline) await sleep(2000, workSignal).catch((e) => { throwIfAborted(); throw e; });
  }

  /** 小段 Range 探测：确认源连通、支持分段、文件大小吻合。用于未知大小时取总长，以及定时回切。
   *  返回 true=可用；false=暂时不通；致命问题（404 / 大小不符）直接标记该源不可用 */
  async function probe(src) {
    try {
      const { res, ctl, dispose } = await request(src, { Range: `bytes=0-${PROBE_BYTES - 1}` }, probeTimeoutMs);
      try {
        if (res.status === 206) {
          const cr = parseContentRange(res.headers.get("content-range"));
          if (size && cr && cr.total && cr.total !== size) { controller.markDead(src, "文件大小与预期不符"); return false; }
          if (!size && cr && cr.total) size = cr.total;
          const want = Math.min(PROBE_BYTES, cr ? cr.end - cr.start + 1 : PROBE_BYTES);
          await readBody(res, ctl, Buffer.allocUnsafe(want), () => {});
          return true;
        }
        if (res.status === 200) {
          // 能通但不支持分段：只在所有源都不支持分段时才会被退化下载用到
          const len = Number(res.headers.get("content-length") || 0);
          if (!size && len) size = len;
          try { await res.body.cancel(); } catch { /* ignore */ }
          src.noRange = true;
          return false;
        }
        if (httpFailureKind(res.status) === "dead") controller.markDead(src, `HTTP ${res.status}`);
        try { await res.body.cancel(); } catch { /* ignore */ }
        return false;
      } finally { dispose(); }
    } catch (e) {
      if (e && e.kind === "aborted") throw e;
      return false;
    }
  }

  // ───────────── 1) 选源状态机（优先级固定，不测速） ─────────────
  const controller = createSourceController({ sources, backoffSeqMs: switchBackoffMs, rounds: switchRounds });
  const srcInfo = (s) => ({ source: s.host, tier: s.tier, label: s.label });
  onPhase("connecting", srcInfo(controller.current()));
  log("info", `[update-dl] 候选源（按优先级）：${sources.map((s) => `${s.label}(${s.host})`).join(" > ")}`);

  // 没给大小（latest.yml 缺字段）：按优先级逐个探测取总长；全是网络类失败就退避后再来，不放弃
  for (let round = 0; !size; round++) {
    throwIfAborted();
    for (const s of sources) {
      if (s.dead) continue;
      await probe(s);
      if (size) break;
    }
    if (size) break;
    if (controller.allDead()) throw new DownloadError("fatal", `所有下载源都不可用：${sources.map((s) => `${s.label}(${s.reason || "?"})`).join("、")}`);
    onPhase("waiting", { ...srcInfo(controller.current()), reason: "probe" });
    await waitForNetwork(Math.min(backoffMaxMs, backoffBaseMs * 2 ** Math.min(round, 6)));
  }

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
  let phaseSource = null;
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
  /** 数据在流动：阶段切回 downloading；换了源（含自动回切）也要重新上报，界面据此更新线路提示 */
  function markFlowing(src) {
    if (phaseNow === "downloading" && phaseSource === src) return;
    phaseNow = "downloading";
    phaseSource = src;
    onPhase("downloading", srcInfo(src));
  }

  // ───────────── 2) 准备断点文件 ─────────────
  let meta = await loadMeta(metaPath);
  let reuse = !!meta && meta.size === size && meta.sha512 === sha512 && meta.chunkSize === chunkSize;
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

  let streaming = false; // 退化为单连接顺序下载后，分块进度不再有意义，不再落盘
  let lastSave = 0;
  const persist = async (force) => {
    if (streaming) return;
    const now = Date.now();
    if (!force && now - lastSave < 1000) return;
    lastSave = now;
    await saveMeta(metaPath, { size, sha512, chunkSize, done: [...done].sort((a, b) => a - b) });
  };

  const allDeadError = () => new DownloadError("fatal", `所有下载源都不可用：${sources.map((s) => `${s.label}(${s.reason || "?"})`).join("、")}`);

  // ───────────── 3) 分块下载 ─────────────
  async function fetchChunk(i, lease) {
    const src = lease.src;
    const start = i * chunkSize;
    const len = chunkLen(i);
    const end = start + len - 1;
    const timeout = lease.cold ? Math.min(firstConnectTimeoutMs, connectTimeoutMs) : connectTimeoutMs;
    const { res, ctl, dispose } = await request(src, { Range: `bytes=${start}-${end}` }, timeout);
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
        const got = await readBody(res, ctl, buf, (n) => {
          inflight.set(i, n);
          lastBytesAt = Date.now();
          controller.reportAlive(src);
          if (controller.current() === src) markFlowing(src);
          emitProgress(false);
        });
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
      const lease = await controller.acquire(workSignal).catch((e) => { throwIfAborted(); throw e; });
      try {
        await fetchChunk(i, lease);
        controller.release(lease);
        return;
      } catch (e) {
        if (e && e.kind === "aborted") { controller.release(lease); throw e; }
        if (e && (e.kind === "disk" || e.kind === "fatal")) { controller.release(lease); throw e; }
        retries += 1;
        const verdict = controller.reportFailure(lease, e);
        const from = lease.src;
        log("warn", `[update-dl] 第 ${i} 块失败(${e && e.kind}): ${e && e.message}；${from.label} -> ${verdict.action}${verdict.to ? ` ${verdict.to.label}` : ""}`);
        if (verdict.action === "fatal") throw allDeadError();
        if (verdict.action === "stale") continue; // 已经换过源了：回去排队，按新的源重来
        if (verdict.action === "switched") {
          // 换源：不管有没有数据都要告诉用户（这一刻已经没有数据在流动了）
          log("warn", `[update-dl] 切换下载源 ${from.label} -> ${verdict.to.label}，${verdict.delayMs}ms 后重试`);
          phaseNow = "retrying"; phaseSource = null;
          onPhase("retrying", {
            ...srcInfo(from), attempt: attempt + 1, retryInMs: verdict.delayMs,
            nextLabel: verdict.to.label, nextTier: verdict.to.tier, message: String((e && e.message) || e),
          });
          if ((!e || e.kind === "network") && !isOnline()) {
            onPhase("waiting", { ...srcInfo(verdict.to), reason: "offline" }); phaseNow = "waiting";
            await waitForNetwork(2000);
          }
          continue; // 退避由选源闸门统一执行（acquire 会等到闸门打开）
        }
        // 同源单块失败：源本身是好的，短暂退避后同源重试
        const quiet = Date.now() - lastBytesAt > 3000; // 已经好几秒没有任何数据进来，才值得告诉用户
        if (quiet) { onPhase("retrying", { ...srcInfo(from), attempt: attempt + 1, message: String((e && e.message) || e) }); phaseNow = "retrying"; phaseSource = null; }
        const delay = Math.min(backoffMaxMs, backoffBaseMs * 2 ** Math.min(attempt, 6)) * (0.7 + Math.random() * 0.6);
        if ((!e || e.kind === "network") && !isOnline()) {
          onPhase("waiting", { ...srcInfo(from), reason: "offline" }); phaseNow = "waiting"; // 确定离线：不受 quiet 限制，立刻告知
          await waitForNetwork(Math.min(delay, 2000));
        } else {
          await sleep(delay, workSignal).catch((err) => { throwIfAborted(); throw err; });
        }
      }
    }
  }

  /** 定时回切：不在最高优先级源上时，串行探测更高优先级的源，通了就切回去（已完成的块不受影响） */
  async function recoverLoop(recoverCtl) {
    for (;;) {
      await sleep(recoverIntervalMs, recoverCtl.signal);
      for (const cand of controller.higherThanCurrent()) {
        if (recoverCtl.signal.aborted) return;
        if (await probe(cand)) {
          const from = controller.current();
          controller.promote(cand);
          log("info", `[update-dl] ${cand.label} 已恢复，从 ${from.label} 切回`);
          break;
        }
      }
    }
  }

  async function runChunked() {
    const queue = [];
    for (let i = 0; i < nChunks; i++) if (!done.has(i)) queue.push(i);
    let cursor = 0;
    const runCtl = new AbortController();
    const onStop = () => runCtl.abort();
    stopSignal.addEventListener("abort", onStop, { once: true });
    workSignal = runCtl.signal;
    const recoverCtl = new AbortController();
    const recovering = recoverLoop(recoverCtl).catch(() => { /* 取消 / 结束时退出 */ });
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
      runCtl.abort();
    })));
    recoverCtl.abort();
    await recovering;
    stopSignal.removeEventListener("abort", onStop);
    workSignal = stopSignal;
    if (stopSignal.aborted) throw abortedErr();
    if (primaryErr) throw primaryErr;
  }

  // 所有源都不支持 Range：单连接顺序下载，按优先级轮换（失败整体重来）
  async function runStreaming() {
    streaming = true;
    done.clear();
    await fsp.unlink(metaPath).catch(() => {});
    const list = sources.filter((s) => s.noRange);
    for (let attempt = 0; ; attempt++) {
      throwIfAborted();
      const src = list[attempt % list.length];
      try {
        const { res, ctl, dispose } = await request(src, {}, connectTimeoutMs);
        try {
          if (res.status !== 200) {
            if (httpFailureKind(res.status) === "dead") { list.splice(list.indexOf(src), 1); if (!list.length) throw allDeadError(); }
            throw new DownloadError("http", `HTTP ${res.status}`, { status: res.status });
          }
          inflight.clear();
          doneBytes = 0;
          let pos = 0;
          const reader = res.body.getReader();
          markFlowing(src);
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
          phaseSource = src;
          return;
        } finally { dispose(); }
      } catch (e) {
        if (e && (e.kind === "aborted" || e.kind === "disk" || e.kind === "fatal")) throw e;
        retries += 1;
        const next = list[(attempt + 1) % list.length];
        const delay = switchBackoffMs[Math.min(attempt, switchBackoffMs.length - 1)];
        phaseNow = "retrying"; phaseSource = null;
        onPhase("retrying", { ...srcInfo(src), attempt: attempt + 1, retryInMs: delay, nextLabel: next.label, nextTier: next.tier, message: String((e && e.message) || e) });
        if (!isOnline()) { onPhase("waiting", { ...srcInfo(next), reason: "offline" }); await waitForNetwork(2000); }
        else await sleep(delay, stopSignal);
      }
    }
  }

  // ───────────── 4) 执行 → 校验 → 落地 ─────────────
  try {
    emitProgress(true);
    if (done.size < nChunks) {
      try {
        await runChunked();
      } catch (e) {
        // 分块下载因「所有源都不可用」失败，但其中有源只是不支持 Range：退化为单连接下载
        if (!(e && e.kind === "fatal" && sources.some((s) => s.noRange))) throw e;
        log("warn", "[update-dl] 所有源都不支持分段请求，退化为单连接下载");
        await runStreaming();
      }
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

  onPhase("verifying", srcInfo(phaseSource || controller.current()));
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
  const used = phaseSource || controller.current();
  stopCtl.abort(); // 还在后台的回切探测此时不再需要，立刻释放连接与定时器
  return { path: dest, bytes: size, source: used.host, tier: used.tier, label: used.label, retries, resumedBytes };
}

module.exports = { downloadResumable, DownloadError, hashFileSha512, parseContentRange, discardPartial };
