"use strict";
/** 更新下载的三条通道（plan-20261024-update-channel）。
 *
 * 用户可见的通道只有三个，命名固定：镜像1 / 镜像2 / github。
 * 它们各自对应一条真实的下载地址，与 update-manager 的选源层级一一对应：
 *   镜像1  → gh-proxy 加速的 GitHub 地址（tier=mirror，境外 CDN，绕系统代理）
 *   镜像2  → 自建源 https://<updates>/（tier=primary）
 *   github → GitHub Releases 原始地址（tier=github，慢速兜底）
 *
 * 为什么单独成模块：通道的「叫什么」「怎么测速」「谁优先」是用户直接感知的部分，
 * 而 update-manager 负责的是状态机。分开后测速逻辑能脱离 electron-updater 单测。
 *
 * 纯逻辑 + 注入 fetch：不发硬编码请求，不依赖 electron，时间可注入，方便单测。
 */

const MiB = 1024 * 1024;

/** 通道 id → 用户可见名称。界面上的名称只认这里，避免前端各处散落字符串 */
const CHANNEL_LABEL = { mirror1: "镜像1", mirror2: "镜像2", github: "github" };
const CHANNEL_IDS = ["mirror1", "mirror2", "github"];
/** 通道 → 选源层级（update-source-policy 的 tier）。镜像1 属 mirror，镜像2 属 primary */
const CHANNEL_TIER = { mirror1: "mirror", mirror2: "primary", github: "github" };
/** 层级 → 默认通道（同一层级只有一个通道时用来反查） */
const TIER_CHANNEL = { mirror: "mirror1", primary: "mirror2", github: "github" };

const isChannel = (id) => Object.prototype.hasOwnProperty.call(CHANNEL_LABEL, id);

/** 测速探测的字节数：只测「建立连接 + 稳定出数据」的速度，不为了测速白下几 MB */
const PROBE_BYTES = 256 * 1024;
/** 单个通道的测速超时：超时按「不可用」处理，不让最慢的一条拖住整个测速 */
const DEFAULT_PROBE_TIMEOUT_MS = 6000;

/**
 * 拼出某个通道的候选地址。
 * @param {object} o
 * @param {string} o.channel   通道 id
 * @param {string} o.name      安装包文件名（已 decode 的原始名）
 * @param {object} o.info      最近一次 update-available 的 info（取 version / mirrors）
 * @param {{owner: string, repo: string}|null} o.repo
 * @param {string} o.updatesBaseUrl 自建源根地址（以 / 结尾）
 * @param {string|null} [o.ghProxy]  GitHub 加速前缀；null = 不用加速
 * @returns {string[]} 该通道的候选地址（同通道可能有多个备份地址，按顺序重试）
 */
function channelUrls({ channel, name, info, repo, updatesBaseUrl, ghProxy }) {
  const enc = encodeURIComponent(name);
  const version = info && info.version;
  // plan-20261116-update-probe：首次检查前只探测 latest.yml，不能因尚无版本号就跳过两条 GitHub 通道。
  const releasePath = version ? `download/v${version}` : name === "latest.yml" ? "latest/download" : "";
  const gh = releasePath && repo
    ? `https://github.com/${repo.owner}/${repo.repo}/releases/${releasePath}/${enc}`
    : "";
  if (channel === "github") return gh ? [gh] : [];
  if (channel === "mirror2") {
    const list = [new URL(enc, updatesBaseUrl).href];
    // latest.yml 里的额外镜像（对象存储 / CDN）算作镜像2 的备份：它们同属服务端可控的镜像
    for (const base of Array.isArray(info && info.mirrors) ? info.mirrors : []) {
      if (typeof base !== "string" || !/^https:\/\//i.test(base)) continue;
      const u = new URL(enc, base.endsWith("/") ? base : base + "/").href;
      if (!list.includes(u)) list.push(u);
    }
    return list;
  }
  if (channel === "mirror1") return gh && ghProxy ? [ghProxy + gh] : [];
  return [];
}

const nowMs = () => Date.now();

/**
 * 测一条通道：先看「连上要多久」（TTFB），再按实际收到的字节算速率。
 * 用 Range 只取前 PROBE_BYTES，避免为了测速白下几十 MB；不支持 Range 的源（200）
 * 读到够用的字节就主动 cancel，不继续拉整包。
 * @returns {Promise<{ok: boolean, ms: number, bytesPerSecond: number, reason?: string}>}
 */
async function probeChannel({ url, fetchImpl, timeoutMs = DEFAULT_PROBE_TIMEOUT_MS, signal, probeBytes = PROBE_BYTES }) {
  const started = nowMs();
  // plan-20261116-review-fixes：响应头不代表测速结束，停滞的响应体也必须能超时和取消。
  const ctl = new AbortController();
  const onAbort = () => ctl.abort();
  let rejectAbort;
  const aborted = new Promise((_, reject) => { rejectAbort = reject; });
  const failAbort = () => rejectAbort(new DOMException("测速已中止", "AbortError"));
  ctl.signal.addEventListener("abort", failAbort, { once: true });
  const timer = setTimeout(onAbort, timeoutMs);
  if (signal?.aborted) onAbort();
  else signal?.addEventListener("abort", onAbort, { once: true });
  const bounded = (operation) => Promise.race([operation, aborted]);
  let reader = null;
  let res = null;
  try {
    res = await bounded(Promise.resolve().then(() => {
      ctl.signal.throwIfAborted();
      return fetchImpl(url, { headers: { Range: `bytes=0-${probeBytes - 1}` }, signal: ctl.signal, redirect: "follow", cache: "no-store" });
    }));
    const ttfb = nowMs() - started;
    if (res.status !== 206 && res.status !== 200) {
      return { ok: false, ms: ttfb, bytesPerSecond: 0, reason: `HTTP ${res.status}` };
    }
    const len = Number(res.headers.get("content-length") || 0);
    const want = Math.min(probeBytes, len > 0 ? len : probeBytes);
    reader = res.body && res.body.getReader ? res.body.getReader() : null;
    if (!reader) return { ok: true, ms: ttfb, bytesPerSecond: 0 };
    let got = 0;
    const bodyStart = nowMs();
    for (;;) {
      const chunk = await bounded(reader.read());
      if (chunk.done) break;
      got += chunk.value.length;
      if (got >= want) break; // 够用就停：不把整包拉下来
    }
    const elapsed = Math.max(1, nowMs() - bodyStart);
    if (got <= 0) return { ok: false, ms: ttfb, bytesPerSecond: 0, reason: "没有收到数据" };
    // 速率只按「收数据」的耗时算，不含握手延迟；握手快慢单独用 ms 表达
    return { ok: true, ms: ttfb, bytesPerSecond: Math.round((got / elapsed) * 1000) };
  } catch (e) {
    const ms = nowMs() - started;
    const aborted = e && (e.name === "AbortError" || /abort/i.test(String(e.message || "")));
    return { ok: false, ms, bytesPerSecond: 0, reason: aborted ? "连接超时" : String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
    ctl.signal.removeEventListener("abort", failAbort);
    // plan-20261116-review-fixes：取消也可能遇到坏网络栈，不能因此占住下一次测速机会。
    try { void Promise.resolve(reader ? reader.cancel() : res?.body?.cancel()).catch(() => {}); } catch { /* 尽力释放响应 */ }
  }
}

/**
 * 三条通道并行测速并给出优选结果。任一条失败都不影响其余，
 * 全部失败时返回 best=null（界面据此回落到原来的固定优先级）。
 * @param {object} o
 * @param {Record<string, string[]>} o.urlsByChannel  每个通道的候选地址（channelUrls 的结果）
 * @param {Function} o.fetchFor   (channel, url) => fetch 实现；镜像1 需要绕系统代理
 * @param {number} [o.timeoutMs]
 * @param {AbortSignal} [o.signal]
 * @returns {Promise<{results: Record<string, object>, best: string|null, testedAt: number}>}
 */
async function measureChannels({ urlsByChannel, fetchFor, timeoutMs = DEFAULT_PROBE_TIMEOUT_MS, signal, probeBytes }) {
  const ids = CHANNEL_IDS.filter((id) => Array.isArray(urlsByChannel[id]) && urlsByChannel[id].length > 0);
  const results = {};
  await Promise.all(ids.map(async (id) => {
    // 同一通道有多个备份地址时取最快的一个：任一可用即代表该通道可用
    let best = null;
    let lastFail = null;
    for (const url of urlsByChannel[id]) {
      if (signal && signal.aborted) break;
      const r = await probeChannel({ url, fetchImpl: fetchFor(id, url), timeoutMs, signal, probeBytes });
      if (r.ok) {
        if (!best || r.bytesPerSecond > best.bytesPerSecond) best = { ...r, url };
        break; // 首个可用地址即可代表该通道，不重复消耗流量
      }
      lastFail = r;
    }
    results[id] = best || { ok: false, ms: (lastFail && lastFail.ms) || 0, bytesPerSecond: 0, reason: (lastFail && lastFail.reason) || "未测速" };
  }));
  // 只把「测通了」的通道纳入比较：失败的不参与排序
  const usable = ids.filter((id) => results[id] && results[id].ok);
  const best = usable.length
    ? usable.reduce((a, b) => (results[b].bytesPerSecond > results[a].bytesPerSecond ? b : a))
    : null;
  return { results, best, testedAt: nowMs() };
}

/** 把测速结果整理成给渲染层的最小结构（不含 url，界面不需要也拿不到内网地址） */
function publicReport({ results, best, testedAt }) {
  const out = {};
  for (const id of CHANNEL_IDS) {
    const r = results[id];
    out[id] = r
      ? { ok: !!r.ok, ms: r.ms || 0, bytesPerSecond: r.bytesPerSecond || 0, reason: r.reason || "" }
      : { ok: false, ms: 0, bytesPerSecond: 0, reason: "未测速" };
  }
  return { channels: out, best: best || null, testedAt: testedAt || 0 };
}

module.exports = {
  CHANNEL_LABEL, CHANNEL_IDS, CHANNEL_TIER, TIER_CHANNEL, isChannel,
  channelUrls, probeChannel, measureChannels, publicReport,
  PROBE_BYTES, DEFAULT_PROBE_TIMEOUT_MS, MiB,
};
