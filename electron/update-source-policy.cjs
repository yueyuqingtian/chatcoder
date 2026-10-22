"use strict";
/** 更新下载的选源策略（plan-20261022-update-route）。
 *
 * 为什么单独成模块：旧实现按 256KB 探测测速排序，测的主要是握手延迟，挂代理时 GitHub 常常「更快」被排到第一；
 * 换源只往后转、不回头，回退到 GitHub 后再也回不到自建源；4 个并发分块被同一次抖动同时打断，
 * 共用的失败计数瞬间凑满就换源。这里把「用哪个源」收拢成一个状态机，由下载器的所有 worker 共享：
 *
 *  - 优先级固定：镜像 > 自建源 > GitHub，不测速、不重排（镜像实测更快，见 plan-20261022-update-route）；
 *  - 镜像线路固定单连接：gh-proxy 对多连接并发会主动降速；
 *  - 切换序列：M → P → M → P → G → G …（两轮镜像 / 自建源都失败才进 GitHub），每次切换前按 2s、4s、8s… 退避；
 *  - 同一时刻只试一个源：退避期间所有 worker 都停在闸门上；切到一个还没连通的源时只放一个请求过去探路；
 *  - 只认「源故障」：某个请求失败时，若同一源在它发出之后还有别的连接收到过数据，只算单块失败，同源重试；
 *  - 自动回切：不在最高优先级源上时，由下载器定时探测更高优先级的源，通了就 promote 回去。
 *
 * 纯逻辑，不发请求、不依赖 electron；时间与随机数可注入，方便单测。
 */

const TIER_LABEL = { mirror: "镜像", primary: "自建源", github: "GitHub" };
const TIER_RANK = { mirror: 0, primary: 1, github: 2 };
/** 各层级默认的并发上限：镜像只允许一个连接，避免 gh-proxy 因并发降速 */
const TIER_CONCURRENCY = { mirror: 1 };

function hostOf(url) {
  try { return new URL(url).host; } catch { return String(url).slice(0, 40); }
}

/** 统一成带层级的源列表。旧调用方传字符串数组：第一个视为自建源，其余视为镜像（保持原有先后） */
function normalizeSources(urls) {
  const list = [];
  (Array.isArray(urls) ? urls : []).forEach((u, i) => {
    const o = typeof u === "string" ? { url: u, tier: i === 0 ? "primary" : "mirror" } : { ...u };
    if (!o || !o.url) return;
    if (!TIER_RANK.hasOwnProperty(o.tier)) o.tier = "mirror";
    if (list.some((s) => s.url === o.url)) return;
    list.push(o);
  });
  // 稳定排序：层级优先，同层保持传入顺序
  list.sort((a, b) => TIER_RANK[a.tier] - TIER_RANK[b.tier]);
  return list.map((o, id) => ({
    id, url: o.url, tier: o.tier, label: o.label || TIER_LABEL[o.tier], host: hostOf(o.url),
    dead: false, noRange: false, reason: "", lastOkAt: 0,
    // 单源在途请求上限：镜像默认 1（多连接并发会被 gh-proxy 降速），其余不限制
    maxConcurrency: Number.isFinite(o.maxConcurrency)
      ? Math.max(1, Math.floor(o.maxConcurrency))
      : (TIER_CONCURRENCY[o.tier] || Infinity),
  }));
}

/**
 * @param {object} o
 * @param {Array} o.sources            normalizeSources 的结果
 * @param {number[]} [o.backoffSeqMs]  切换源前的退避序列，超出后取最后一项
 * @param {number} [o.rounds]          自建 / 镜像轮换几轮后才进 GitHub
 * @param {number} [o.jitter]          退避抖动比例（±）
 */
function createSourceController(o) {
  const sources = o.sources;
  const backoffSeqMs = o.backoffSeqMs && o.backoffSeqMs.length ? o.backoffSeqMs : [2000, 4000, 8000, 16000, 30000];
  const rounds = Math.max(1, o.rounds || 2);
  const jitter = o.jitter == null ? 0.15 : o.jitter;
  const now = o.now || Date.now;
  const random = o.random || Math.random;
  if (!sources.length) throw new Error("没有可用的下载源");

  const nonGh = sources.filter((s) => s.tier !== "github");
  const gh = sources.filter((s) => s.tier === "github");
  const live = (list) => list.filter((s) => !s.dead);

  // 序列下标：[0, rounds*nonGh.length) 是两轮自建 / 镜像，之后是尾段（GitHub 循环；没有 GitHub 则全体存活源循环）
  let step = 0;
  let cur = null;
  let switchedAt = 0;
  let backoffStep = 0;
  let gateUntil = 0;
  let lease = null; // 冷源探路许可：同一时刻只有一个请求能去试一个还没连通的源
  let waiters = [];
  // 各源在途请求数：镜像线路单连接靠它限流（acquire 时占位，release 时归还）
  const active = new Map();
  const limitOf = (src) => (src.maxConcurrency > 0 ? src.maxConcurrency : Infinity);
  const isFull = (src) => (active.get(src) || 0) >= limitOf(src);

  function seqAt(k) {
    const head = rounds * nonGh.length;
    if (k < head) return nonGh[k % nonGh.length];
    const tail = live(gh).length ? gh : sources;
    return tail[(k - head) % tail.length];
  }
  /** 从下标 k 开始找第一个存活源；序列有限长 + 尾段循环，最多扫两遍就能确定 */
  function liveFrom(k) {
    const bound = k + rounds * nonGh.length + sources.length * 2 + 1;
    for (let i = k; i < bound; i++) { const s = seqAt(i); if (!s.dead) return { k: i, s }; }
    return null;
  }
  /** 源在序列首轮的位置：用于数据恢复流动后把轮数清零（「两轮」从最近一次正常时算起） */
  function homeStep(src) {
    const i = nonGh.indexOf(src);
    return i >= 0 ? i : rounds * nonGh.length;
  }

  function notify() { const w = waiters; waiters = []; for (const f of w) f(); }
  /** 等到状态变化或超时，可被 signal 打断 */
  function waitChange(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) return reject(abortErr());
      let timer = null;
      const done = () => { if (timer) clearTimeout(timer); if (signal) signal.removeEventListener("abort", onAbort); resolve(); };
      const onAbort = () => { if (timer) clearTimeout(timer); waiters = waiters.filter((f) => f !== done); reject(abortErr()); };
      if (ms != null) timer = setTimeout(done, Math.max(0, ms));
      waiters.push(done);
      if (signal) signal.addEventListener("abort", onAbort, { once: true });
    });
  }
  function abortErr() { const e = new Error("已取消"); e.kind = "aborted"; return e; }

  function setCurrent(src, k) {
    cur = src;
    step = k;
    switchedAt = now();
    lease = null;
    notify();
  }

  const first = liveFrom(0);
  setCurrent(first.s, first.k);
  switchedAt = 0; // 初始源不算「刚切过来」

  const isWarm = (src) => src.lastOkAt > 0 && src.lastOkAt >= switchedAt;

  /** 占一个并发位并返回租约 */
  function take(src, cold) {
    const l = { src, cold, startedAt: now(), released: false };
    active.set(src, (active.get(src) || 0) + 1);
    if (cold) lease = l;
    return l;
  }

  /** 取一个可用的源发请求。返回租约 {src, cold, startedAt}，请求结束后必须 release */
  async function acquire(signal) {
    for (;;) {
      const wait = gateUntil - now();
      if (wait > 0) { await waitChange(wait, signal); continue; }
      const src = cur;
      if (isFull(src)) { await waitChange(null, signal); continue; }
      if (isWarm(src)) return take(src, false);
      if (!lease) return take(src, true);
      await waitChange(null, signal);
    }
  }

  function release(l) {
    if (!l || l.released) return;
    l.released = true;
    const n = (active.get(l.src) || 0) - 1;
    if (n > 0) active.set(l.src, n); else active.delete(l.src);
    if (lease === l) lease = null;
    notify();
  }

  /** 收到数据：标记该源连通；当前源恢复流动后退避与轮数清零 */
  function reportAlive(src) {
    const wasWarm = isWarm(src);
    src.lastOkAt = now();
    if (src === cur) {
      backoffStep = 0;
      step = homeStep(src);
      if (!wasWarm) { lease = null; notify(); }
    }
  }

  function markDead(src, reason) {
    if (src.dead) return;
    src.dead = true;
    src.reason = reason || src.reason || "不可用";
  }

  function allDead() { return sources.every((s) => s.dead); }

  /** 切到序列里的下一个存活源，delayMs 后才放行 */
  function advance(delayMs) {
    const nx = liveFrom(step + 1);
    if (!nx) return null;
    setCurrent(nx.s, nx.k);
    gateUntil = now() + delayMs;
    return nx.s;
  }

  function nextDelay() {
    const base = backoffSeqMs[Math.min(backoffStep, backoffSeqMs.length - 1)];
    backoffStep += 1;
    return Math.round(base * (1 - jitter + random() * jitter * 2));
  }

  /**
   * 上报一次请求失败。
   * @param {{src, startedAt}} l   acquire 返回的租约
   * @param {object} err           DownloadError（kind / hard）
   * @returns {{action: "stale"|"retry"|"switched"|"fatal", delayMs?: number, from?: object, to?: object}}
   */
  function reportFailure(l, err) {
    const src = l.src;
    release(l);
    const hard = !!err && ((err.kind === "http" && err.hard) || err.kind === "size" || err.kind === "range");
    if (hard) {
      markDead(src, String((err && err.message) || err));
      if (err.kind === "range") src.noRange = true;
    }
    if (allDead()) return { action: "fatal" };
    // 切换之前发出的旧请求：结论已经被别的 worker 用过了，回去排队即可
    if (src !== cur) return { action: "stale" };
    if (hard) {
      // 该源已确定不可用：不必等待，直接换下一个
      const to = advance(0);
      return to ? { action: "switched", delayMs: 0, from: src, to } : { action: "fatal" };
    }
    // 这个请求发出后，同源的别的连接还收到过数据：源是好的，只是这一块倒霉
    if (src.lastOkAt > l.startedAt) return { action: "retry" };
    const delayMs = nextDelay();
    const to = advance(delayMs);
    return to ? { action: "switched", delayMs, from: src, to } : { action: "fatal" };
  }

  /** 比当前源优先级更高的存活源（按优先级排列），用于定时回切探测 */
  function higherThanCurrent() {
    return sources.filter((s) => !s.dead && TIER_RANK[s.tier] < TIER_RANK[cur.tier]);
  }

  /** 回切到更高优先级的源（探测已经确认它连通） */
  function promote(src) {
    if (src.dead || src === cur) return;
    setCurrent(src, homeStep(src));
    src.lastOkAt = now();
    backoffStep = 0;
    gateUntil = 0;
  }

  return {
    acquire, release, reportAlive, reportFailure, markDead, promote, higherThanCurrent,
    current: () => cur,
    isWarm: (src) => isWarm(src || cur),
    gateRemainingMs: () => Math.max(0, gateUntil - now()),
    allDead,
    sources,
  };
}

module.exports = { createSourceController, normalizeSources, TIER_LABEL, TIER_RANK };
