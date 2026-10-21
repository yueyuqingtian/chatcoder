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
  u.downloadUpdate = async () => { u.calls.push(["download"]); };
  u.quitAndInstall = () => { u.calls.push(["install"]); };
  return u;
}

function makeManager(over = {}) {
  const pushes = [];
  const metaStore = { ...(over.meta || {}) };
  const m = createUpdateManager({
    pushState: (s) => pushes.push({ ...s }),
    meta: { read: () => ({ ...metaStore }), write: (p) => Object.assign(metaStore, p) },
    feeds: { mirror: { provider: "generic", url: "https://m.example/updates/" }, github: { provider: "github" } },
    updatesBaseUrl: "https://m.example/updates/",
    repo: over.repo === undefined ? { owner: "o", repo: "r" } : over.repo,
    currentVersion: "0.8.5",
    net: {},
    log() {}, logErr() {},
    tuning: { retryDelayMs: 1 },
  });
  const u = fakeUpdater();
  m.attach(u);
  return { m, u, pushes, metaStore };
}

const info = (extra = {}) => ({ version: "0.9.0", releaseNotes: "n", releaseDate: "d", files: [{ url: "chatcoder-Setup-0.9.0.exe", sha512: "x", size: 1 }], ...extra });

test("候选下载源：当前 feed 解析出的地址优先，其次自建源与 GitHub，额外镜像只收 https，且去重", () => {
  const { m, u } = makeManager();
  u.emit("update-available", info({ mirrors: ["https://cdn.example/x", "http://insecure.example/", "https://m.example/updates/"] }));
  const urls = m._internals.buildDownloadUrls(new URL("https://github.com/o/r/releases/download/v0.9.0/chatcoder-Setup-0.9.0.exe"), "chatcoder-Setup-0.9.0.exe");
  assert.deepEqual(urls, [
    "https://github.com/o/r/releases/download/v0.9.0/chatcoder-Setup-0.9.0.exe",
    "https://m.example/updates/chatcoder-Setup-0.9.0.exe",
    "https://cdn.example/x/chatcoder-Setup-0.9.0.exe",
  ]);
});

test("repo 为空时不把 GitHub 列入候选源", () => {
  const { m, u } = makeManager({ repo: null });
  u.emit("update-available", info());
  const urls = m._internals.buildDownloadUrls(new URL("https://m.example/updates/chatcoder-Setup-0.9.0.exe"), "chatcoder-Setup-0.9.0.exe");
  assert.deepEqual(urls, ["https://m.example/updates/chatcoder-Setup-0.9.0.exe"]);
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
