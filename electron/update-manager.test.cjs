"use strict";
/** update-manager 单测（node --test electron/update-manager.test.cjs）：候选源拼装、设置、状态守卫。
 * 与真实 electron-updater 的端到端见 update-e2e.harness.cjs。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("events");
const { createUpdateManager, normalizeReleaseNotes } = require("./update-manager.cjs");

/** 假的 electron-updater：只有事件 + 下载入口，足够驱动状态机 */
function fakeUpdater() {
  const u = new EventEmitter();
  u.httpExecutor = { download: async () => {} };
  u.calls = [];
  u.setFeedURL = (f) => { u.calls.push(["setFeedURL", f]); };
  u.checkForUpdates = async () => { u.calls.push(["check"]); return { isUpdateAvailable: false }; };
  u.downloadUpdate = async (token) => { u.calls.push(["download", token]); };
  u.quitAndInstall = () => { u.calls.push(["install"]); };
  return u;
}

/** 假的取消令牌：cancel 时触发 onCancel 回调 */
function fakeToken() {
  const cbs = [];
  return { cancelled: false, onCancel: (f) => cbs.push(f), cancel() { this.cancelled = true; for (const f of cbs) f(); } };
}

/** 一个可被取消令牌中断的假下载：返回 Promise，取消时以 aborted 拒绝 */
function hangingDownload(u, starts) {
  u.downloadUpdate = (token) => new Promise((_, reject) => {
    starts.push(token);
    token.onCancel(() => { const e = new Error("已取消"); e.kind = "aborted"; reject(e); });
  });
}

function makeManager(over = {}) {
  const pushes = [];
  const channelPushes = [];
  const metaStore = { ...(over.meta || {}) };
  const m = createUpdateManager({
    pushState: (s) => pushes.push({ ...s }),
    pushChannels: (c) => channelPushes.push(c),
    meta: { read: () => ({ ...metaStore }), write: (p) => Object.assign(metaStore, p) },
    feeds: { mirror: { provider: "generic", url: "https://m.example/updates/" }, github: { provider: "github" } },
    updatesBaseUrl: "https://m.example/updates/",
    repo: over.repo === undefined ? { owner: "o", repo: "r" } : over.repo,
    currentVersion: "0.8.5",
    // plan-20261024-update-channel：给了 fetch 才能真正跑测速；不给时测速会自行跳过
    net: over.fetch ? { fetch: over.fetch } : {},
    log() {}, logErr() {},
    tuning: { retryDelayMs: 1 },
    ghProxy: over.ghProxy,
    createCancellationToken: fakeToken,
  });
  const u = fakeUpdater();
  m.attach(u);
  return { m, u, pushes, channelPushes, metaStore };
}

/** 假的测速响应：按 host 返回指定大小/延迟，用于验证「测速优选」与「手动指定」 */
function fakeProbeFetch(speedByHost = {}) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url: String(url), headers: (init && init.headers) || {} });
    const host = new URL(String(url)).host;
    const spec = speedByHost[host];
    if (!spec) return new Response(null, { status: 404 });
    if (spec.hang) return new Promise((_, reject) => {
      const sig = init && init.signal;
      const fail = () => { const e = new Error("aborted"); e.name = "AbortError"; reject(e); };
      if (sig) { if (sig.aborted) fail(); else sig.addEventListener("abort", fail, { once: true }); }
    });
    // 每个 chunk 的间隔越长，算出来的速率越低
    const size = 64 * 1024;
    const chunks = spec.chunks || 4;
    const delay = spec.delayMs || 0;
    let sent = 0;
    const body = new ReadableStream({
      async pull(ctl) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        if (sent >= chunks) { ctl.close(); return; }
        sent += 1;
        ctl.enqueue(new Uint8Array(size));
      },
    });
    return new Response(body, { status: 206, headers: { "content-range": `bytes 0-${size * chunks - 1}/${size * chunks}`, "content-length": String(size * chunks) } });
  };
  return { fetch, calls };
}

const info = (extra = {}) => ({ version: "0.9.0", releaseNotes: "n", releaseDate: "d", files: [{ url: "chatcoder-Setup-0.9.0.exe", sha512: "x", size: 1 }], ...extra });

test("候选下载源：默认镜像1 > 镜像2 > github，每条地址带通道名与优先级", () => {
  const { m, u } = makeManager();
  u.emit("update-available", info({ mirrors: ["https://cdn.example/x", "http://insecure.example/", "https://m.example/updates/"] }));
  const urls = m._internals.buildDownloadUrls(new URL("https://github.com/o/r/releases/download/v0.9.0/chatcoder-Setup-0.9.0.exe"), "chatcoder-Setup-0.9.0.exe");
  assert.deepEqual(urls, [
    { url: "https://cdn.gh-proxy.org/https://github.com/o/r/releases/download/v0.9.0/chatcoder-Setup-0.9.0.exe", tier: "mirror", channel: "mirror1", label: "镜像1", rank: 0 },
    { url: "https://m.example/updates/chatcoder-Setup-0.9.0.exe", tier: "primary", channel: "mirror2", label: "镜像2", rank: 1 },
    // latest.yml 里的额外镜像算镜像2 的备份地址，不单独占一个通道
    { url: "https://cdn.example/x/chatcoder-Setup-0.9.0.exe", tier: "primary", channel: "mirror2", label: "镜像2", rank: 2 },
    { url: "https://github.com/o/r/releases/download/v0.9.0/chatcoder-Setup-0.9.0.exe", tier: "github", channel: "github", label: "github", rank: 3 },
  ]);
});

test("repo 为空时只剩镜像2（不拼镜像1 与 github）", () => {
  const { m, u } = makeManager({ repo: null });
  u.emit("update-available", info());
  const urls = m._internals.buildDownloadUrls(new URL("https://m.example/updates/chatcoder-Setup-0.9.0.exe"), "chatcoder-Setup-0.9.0.exe");
  assert.deepEqual(urls, [
    { url: "https://m.example/updates/chatcoder-Setup-0.9.0.exe", tier: "primary", channel: "mirror2", label: "镜像2", rank: 0 },
  ]);
});

test("检查更新：每次都从镜像开始，失败依次走自建源、GitHub，不会黏在 GitHub 上", async () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  let failFirst = 2;
  u.checkForUpdates = async () => { u.calls.push(["check"]); if (failFirst-- > 0) throw new Error("down"); return {}; };
  await m._internals.checkForUpdatesSmart();
  const feeds1 = u.calls.filter((c) => c[0] === "setFeedURL").map((c) => c[1].url || c[1].provider);
  assert.deepEqual(feeds1, [
    "https://cdn.gh-proxy.org/https://github.com/o/r/releases/latest/download/",
    "https://m.example/updates/",
    "github",
  ]);
  u.calls.length = 0;
  await m._internals.checkForUpdatesSmart(); // 定时检查：镜像已恢复，必须先试镜像
  assert.equal(u.calls.find((c) => c[0] === "setFeedURL")[1].url, "https://cdn.gh-proxy.org/https://github.com/o/r/releases/latest/download/");
});

test("取消下载：回到 available（保留版本与说明），且同一版本不再被静默自动拉起", async () => {
  const { m, u } = makeManager();
  const starts = [];
  hangingDownload(u, starts);
  u.emit("update-available", info());
  assert.equal(m.getState().state, "downloading");
  await m.cancelDownload();
  assert.equal(m.getState().state, "available");
  assert.equal(m.getState().version, "0.9.0");
  assert.equal(starts.length, 1);
  assert.equal(m._internals.isDownloadActive(), false);
  u.emit("update-available", info()); // 20 分钟后的定时检查
  assert.equal(m.getState().state, "available", "用户取消后不应自动重下");
});

test("切换自建源：停掉当前下载并立刻用新的取消令牌重新开始", async () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  const starts = [];
  hangingDownload(u, starts);
  u.emit("update-available", info());
  m.downloadNow();
  await new Promise((r) => setTimeout(r, 10));
  await m.switchToPrimary();
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(starts.length, 2, "应重新发起一次下载");
  assert.ok(starts[0].cancelled && !starts[1].cancelled);
  assert.equal(m.getState().state, "downloading");
  assert.equal(m.getState().phase, "connecting");
  await m.cancelDownload();
});

test("下载阶段透传线路信息：层级、友好名、退避时长与下一线路", async () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  hangingDownload(u, []);
  u.emit("update-available", info());
  m.downloadNow();
  m._internals.onDownloadPhase("retrying", { source: "m.example", tier: "primary", label: "快速通道", attempt: 1, retryInMs: 2000, nextLabel: "镜像" });
  let st = m.getState();
  assert.equal(st.phase, "retrying");
  assert.equal(st.tier, "primary");
  assert.equal(st.source, "快速通道");
  assert.equal(st.retryInMs, 2000);
  assert.equal(st.nextLabel, "镜像");
  m._internals.onDownloadPhase("downloading", { source: "github.com", tier: "github", label: "GitHub" });
  st = m.getState();
  assert.equal(st.tier, "github");
  assert.equal(st.retryInMs, undefined, "恢复下载后不应残留退避信息");
  await m.cancelDownload();
});

test("设置：默认开启自动下载；关闭后发现新版本只停在 available", async () => {
  const { m, u, pushes } = makeManager();
  assert.equal(m.getSettings().autoDownload, true);
  m.setSettings({ autoDownload: false });
  assert.equal(m.getSettings().autoDownload, false);
  u.emit("update-available", info());
  assert.equal(m.getState().state, "available");
  assert.ok(!u.calls.some((c) => c[0] === "download"));
  assert.equal(pushes.at(-1).state, "available");
});

test("自动下载开启：发现新版本直接进入 downloading(connecting)，不推 available", async () => {
  const { m, u, pushes } = makeManager();
  u.emit("update-available", info());
  assert.equal(pushes[0].state, "downloading");
  assert.equal(pushes[0].phase, "connecting");
  assert.equal(pushes[0].auto, true);
  assert.ok(!pushes.some((p) => p.state === "available"));
  await new Promise((r) => setImmediate(r));
  assert.ok(u.calls.some((c) => c[0] === "download"));
});

test("手动下载：点击同步进入 downloading(connecting)，并带上版本与更新说明", () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  u.emit("update-available", info());
  const s = m.downloadNow();
  assert.equal(s.state, "downloading");
  assert.equal(s.phase, "connecting");
  assert.equal(s.version, "0.9.0");
  assert.equal(s.notes, "n");
  assert.equal(s.auto, false);
});

test("下载中：检查更新不冲状态，迟到的 update-available / not-available / checking 都被忽略", async () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  u.downloadUpdate = () => new Promise(() => {}); // 一直下载中
  u.emit("update-available", info());
  m.downloadNow();
  const r = await m.checkForUpdates();
  assert.equal(r.state, "downloading");
  u.emit("checking-for-update");
  u.emit("update-not-available", {});
  u.emit("update-available", info());
  assert.equal(m.getState().state, "downloading");
  // 进度事件按 phase=downloading 合并进同一个状态
  u.emit("download-progress", { percent: 12.34, transferred: 5, total: 40, bytesPerSecond: 3 });
  assert.equal(m.getState().phase, "downloading");
  assert.equal(m.getState().percent, 12.3);
});

test("下载失败（非致命）重试 3 次后落到 error，并给出可读文案", async () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  let n = 0;
  u.downloadUpdate = async () => { n += 1; const e = new Error("boom"); e.kind = "network"; throw e; };
  u.emit("update-available", info());
  m.downloadNow();
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(n, 3);
  assert.equal(m.getState().state, "error");
  assert.equal(m.getState().message, "boom");
});

test("磁盘类错误不重试，直接给出明确提示", async () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  let n = 0;
  u.downloadUpdate = async () => { n += 1; const e = new Error("ENOSPC"); e.kind = "disk"; throw e; };
  u.emit("update-available", info());
  m.downloadNow();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(n, 1);
  assert.match(m.getState().message, /磁盘空间不足/);
});

test("静默自动下载：同一版本本次运行最多自动尝试 3 次，之后停在 available 等用户手动点", async () => {
  const { m, u } = makeManager();
  u.downloadUpdate = async () => { const e = new Error("x"); e.kind = "disk"; throw e; };
  for (let i = 0; i < 3; i++) {
    u.emit("update-available", info());
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(m.getState().state, "error");
  }
  u.emit("update-available", info());
  assert.equal(m.getState().state, "available", "超过额度后不再自动下载");
  // 用户手动检查 = 额度重置
  await m.checkForUpdates();
  u.emit("update-available", info());
  assert.equal(m.getState().state, "downloading");
});

test("已下载完成：定时检查发现同一版本不会把状态冲回 available", () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  u.emit("update-downloaded", info());
  assert.equal(m.getState().state, "downloaded");
  u.emit("checking-for-update");
  u.emit("update-available", info());
  assert.equal(m.getState().state, "downloaded");
  assert.equal(m.install(), true);
});

test("normalizeReleaseNotes：兼容字符串 / 数组 / 对象", () => {
  assert.equal(normalizeReleaseNotes("a"), "a");
  assert.equal(normalizeReleaseNotes([{ note: "x" }, "y"]), "x\n\ny");
  assert.equal(normalizeReleaseNotes({ body: "z" }), "z");
  assert.equal(normalizeReleaseNotes(null), "");
});

// ── plan-20261024-update-channel：三条通道的测速优选与手动指定 ──

test("已是最新版本也保存安装包信息，自动和重复手动测速均请求三条真实通道", async () => {
  const probe = fakeProbeFetch({ "m.example": {}, "cdn.gh-proxy.org": {}, "github.com": {} });
  const { m, u } = makeManager({ fetch: probe.fetch, meta: { autoDownload: false } });
  u.checkForUpdates = async () => {
    const latest = info({ version: "0.8.5", files: [{ url: "chatcoder-Setup-0.8.5.exe" }] });
    u.emit("update-not-available", latest);
    return { isUpdateAvailable: false, updateInfo: latest };
  };
  await m.checkForUpdates();
  for (let i = 0; i < 200 && m.getChannels().measuring; i++) await new Promise((r) => setTimeout(r, 10));
  assert.equal(m.getState().state, "none");
  const expected = [
    "https://cdn.gh-proxy.org/https://github.com/o/r/releases/download/v0.8.5/chatcoder-Setup-0.8.5.exe",
    "https://m.example/updates/chatcoder-Setup-0.8.5.exe",
    "https://github.com/o/r/releases/download/v0.8.5/chatcoder-Setup-0.8.5.exe",
  ];
  assert.deepEqual(probe.calls.map((c) => c.url).sort(), expected.toSorted());
  for (const id of ["mirror1", "mirror2", "github"]) assert.equal(m.getChannels().report.channels[id].ok, true);
  probe.calls.length = 0;
  await m.measureChannels({ force: true });
  assert.deepEqual(probe.calls.map((c) => c.url).sort(), expected.toSorted());
  assert.equal(m.getState().state, "none");
  assert.equal(u.calls.some(([kind]) => kind === "download"), false);
});

test("首次检查前重新测速也会探测三条通道的 latest.yml", async () => {
  const probe = fakeProbeFetch({ "m.example": {}, "cdn.gh-proxy.org": {}, "github.com": {} });
  const { m } = makeManager({ fetch: probe.fetch });
  const snap = await m.measureChannels({ force: true });
  assert.deepEqual(probe.calls.map((c) => c.url).sort(), [
    "https://cdn.gh-proxy.org/https://github.com/o/r/releases/latest/download/latest.yml",
    "https://m.example/updates/latest.yml",
    "https://github.com/o/r/releases/latest/download/latest.yml",
  ].sort());
  for (const id of ["mirror1", "mirror2", "github"]) assert.equal(snap.report.channels[id].ok, true);
});

test("测速优选：自动模式下按测速结果把最快通道排到第一位", async () => {
  // 镜像2（自建源）最快，镜像1 次之，github 最慢
  const probe = fakeProbeFetch({
    "m.example": { chunks: 8, delayMs: 0 },
    "cdn.gh-proxy.org": { chunks: 4, delayMs: 2 },
    "github.com": { chunks: 2, delayMs: 10 },
  });
  const { m, u } = makeManager({ fetch: probe.fetch, meta: { autoDownload: false } });
  u.emit("update-available", info());
  const snap = await m.measureChannels({ force: true });
  assert.equal(snap.preferred, "mirror2", "应选出自建源最快");
  assert.equal(snap.mode, "auto");
  assert.equal(snap.effective, "mirror2");
  assert.equal(snap.report.channels.mirror2.ok, true);
  assert.ok(snap.report.channels.mirror2.bytesPerSecond > 0);
  assert.equal(snap.labels.mirror1, "镜像1");
  // 候选源顺序随之改变：镜像2 排第一
  const urls = m._internals.buildDownloadUrls(new URL("https://m.example/updates/x.exe"), "chatcoder-Setup-0.9.0.exe");
  assert.equal(urls[0].channel, "mirror2");
  assert.equal(urls[0].rank, 0);
  // 检查更新的 feed 链也从最快通道开始
  assert.equal(m._internals.feedChain()[0][0], "镜像2");
});

test("测速：不可用的通道不参与优选，全部失败时回落镜像1", async () => {
  const probe = fakeProbeFetch({ "m.example": { chunks: 8 }, "cdn.gh-proxy.org": { hang: true } });
  const { m, u } = makeManager({ fetch: probe.fetch, meta: { autoDownload: false } });
  u.emit("update-available", info());
  // github 没配 → 404；镜像1 挂住 → 超时；只有镜像2 通
  const snap = await m.measureChannels({ force: true });
  assert.equal(snap.preferred, "mirror2");
  assert.equal(snap.report.channels.github.ok, false);
  assert.equal(snap.report.channels.mirror1.ok, false);
  assert.ok(snap.report.channels.mirror1.reason, "失败要给出可读原因");

  // 全部失败：优选为 null，生效通道回落镜像1（保持旧行为，不会没得下）
  const dead = fakeProbeFetch({});
  const m2 = makeManager({ fetch: dead.fetch, meta: { autoDownload: false } }).m;
  const snap2 = await m2.measureChannels({ force: true });
  assert.equal(snap2.preferred, null);
  assert.equal(snap2.effective, "mirror1");
});

test("手动指定通道：设置落盘、优先生效，并以该通道开头", async () => {
  const probe = fakeProbeFetch({ "m.example": { chunks: 8 }, "cdn.gh-proxy.org": { chunks: 2 }, "github.com": { chunks: 1 } });
  const { m, u, metaStore } = makeManager({ fetch: probe.fetch, meta: { autoDownload: false } });
  u.emit("update-available", info());
  await m.measureChannels({ force: true });
  assert.equal(m.getChannels().effective, "mirror2", "自动模式先按测速");

  const snap = await m.setChannel("github");
  assert.equal(metaStore.channel, "github", "手动指定要落盘");
  assert.equal(snap.mode, "github");
  assert.equal(snap.effective, "github", "手动指定优先于测速结果");
  assert.equal(m._internals.feedChain()[0][0], "github");

  // 改回 auto：重新按测速结果走
  const back = await m.setChannel("auto");
  assert.equal(back.effective, "mirror2");
});

test("手动指定非法通道被忽略", async () => {
  const { m } = makeManager({ meta: { autoDownload: false } });
  await m.setChannel("not-a-channel");
  assert.equal(m.getChannels().mode, "auto");
});

test("下载中切换通道：停下并按新通道重开，已完成的部分保留", async () => {
  const { m, u } = makeManager({ meta: { autoDownload: false } });
  const starts = [];
  hangingDownload(u, starts);
  u.emit("update-available", info());
  m.downloadNow();
  await new Promise((r) => setTimeout(r, 10));
  await m.setChannel("github");
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(starts.length, 2, "切换通道应重新发起下载");
  assert.ok(starts[0].cancelled && !starts[1].cancelled);
  assert.equal(m.getState().phase, "connecting");
  await m.cancelDownload();
});

test("测速结果落盘并在下次启动时恢复，无需等新一轮跑完", async () => {
  const probe = fakeProbeFetch({ "m.example": { chunks: 8 }, "cdn.gh-proxy.org": { chunks: 1 } });
  const first = makeManager({ fetch: probe.fetch, meta: { autoDownload: false } });
  first.u.emit("update-available", info());
  const snap = await first.m.measureChannels({ force: true });
  assert.equal(snap.preferred, "mirror2");
  assert.ok(first.metaStore.channelReport, "测速结果应落盘");
  assert.equal(first.metaStore.preferredChannel, "mirror2");

  // 用同一份 meta 重新创建管理器：启动即带上次的测速结果
  const second = makeManager({ fetch: probe.fetch, meta: first.metaStore });
  const restored = second.m.getChannels();
  assert.equal(restored.preferred, "mirror2");
  assert.equal(restored.effective, "mirror2");
  assert.ok(restored.report.testedAt > 0);
});

test("检查更新后自动测速：有网络栈时会在检查之后跑一次优选", async () => {
  const probe = fakeProbeFetch({ "m.example": { chunks: 8 }, "cdn.gh-proxy.org": { chunks: 1 } });
  const { m, u, channelPushes } = makeManager({ fetch: probe.fetch, meta: { autoDownload: false } });
  u.checkForUpdates = async () => { u.emit("update-available", info()); return { isUpdateAvailable: true }; };
  await m.checkForUpdates();
  // 测速是异步触发的：等它落地
  for (let i = 0; i < 200 && m.getChannels().preferred !== "mirror2"; i++) await new Promise((r) => setTimeout(r, 10));
  assert.equal(m.getChannels().preferred, "mirror2");
  assert.ok(channelPushes.length > 0, "测速过程 / 结果应推送给界面");
});
