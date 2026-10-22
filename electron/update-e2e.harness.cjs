"use strict";
/** 更新链路端到端验证（在真实 Electron + 真实 electron-updater 上跑，不依赖打包）。
 *
 * 用法：node_modules\electron\dist\electron.exe electron\update-e2e.harness.cjs
 *
 * 起一个本地更新服务器（latest.yml + 安装包，支持 Range，可随机掐断连接），
 * 让 electron-updater 走「检查 → 差分尝试(无 blockmap，回退) → 全量下载」的真实流程，
 * 全量下载由 update-glue / update-downloader 接管，状态机由 update-manager 编排。
 *
 * 场景：
 *  A 下载引擎：随机断连仍能下完；中途取消后再下只补缺的块（断点续传）。
 *  B 静默自动下载：发现新版本 → 不经过 available 直接 downloading(connecting) → downloaded。
 *  C 手动下载：关闭自动下载 → available；点击后同步进入 downloading(connecting)；下载期间再检查不冲状态。
 *  D 全部源不可用：多次重试后给出明确错误，而不是卡在下载态。
 * 结果打印为一行 JSON（E2E_RESULT ...），退出码 0 = 全部通过。
 */
const { app, net } = require("electron");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const MiB = 1024 * 1024;
const NAME = "chatcoder-Setup-9.9.9.exe";
const FILE = crypto.randomBytes(16 * MiB + 54321);
const SHA = crypto.createHash("sha512").update(FILE).digest("base64");

const stats = { requests: 0, ranged: 0, bytes: 0, kills: 0, blockmap404: 0 };
let killRate = 0;
// 每发 64KB 的间隔（毫秒）：需要中途取消 / 观察进度时放慢
let pieceDelay = 0;
// 安装包返回 404（模拟所有下载源不可用）
let exeGone = false;

function startServer() {
  const server = http.createServer((req, res) => {
    stats.requests += 1;
    const url = req.url.split("?")[0];
    if (url === "/latest.yml") {
      const yml = `version: 9.9.9\nfiles:\n  - url: ${NAME}\n    sha512: ${SHA}\n    size: ${FILE.length}\npath: ${NAME}\nsha512: ${SHA}\nreleaseDate: '2026-10-21T00:00:00.000Z'\nreleaseNotes: 'e2e'\n`;
      res.writeHead(200, { "Content-Type": "text/yaml", "Content-Length": Buffer.byteLength(yml) });
      return res.end(yml);
    }
    if (url === `/${NAME}` && !exeGone) {
      const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
      let start = 0;
      let end = FILE.length - 1;
      if (m) { start = Number(m[1]); end = m[2] ? Math.min(Number(m[2]), FILE.length - 1) : end; stats.ranged += 1; }
      res.writeHead(m ? 206 : 200, { "Content-Length": end - start + 1, ...(m ? { "Content-Range": `bytes ${start}-${end}/${FILE.length}` } : {}), "Accept-Ranges": "bytes" });
      const len = end - start + 1;
      const killAt = killRate && m && len > 300 * 1024 && Math.random() < killRate ? start + Math.floor(len * (0.2 + Math.random() * 0.5)) : -1;
      let pos = start;
      const pump = () => {
        if (res.destroyed) return;
        if (killAt >= 0 && pos >= killAt) { stats.kills += 1; return req.socket.destroy(); }
        if (pos > end) return res.end();
        const next = Math.min(end + 1, pos + 64 * 1024);
        stats.bytes += next - pos;
        const ok = res.write(FILE.subarray(pos, next));
        pos = next;
        if (pieceDelay) setTimeout(pump, pieceDelay);
        else if (ok) setImmediate(pump); else res.once("drain", pump);
      };
      return pump();
    }
    if (url.endsWith(".blockmap")) stats.blockmap404 += 1;
    res.statusCode = 404;
    res.end();
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(pred, timeoutMs, label) {
  const t0 = Date.now();
  while (!pred()) {
    if (Date.now() - t0 > timeoutMs) throw new Error(`等待超时：${label}`);
    await sleep(20);
  }
}

/** 造一个全新的 electron-updater 实例（独立缓存目录），并接上 update-manager */
function makeStack(tmp, base, name, { autoDownload }) {
  const { NsisUpdater } = require("electron-updater");
  const { ElectronHttpExecutor } = require("electron-updater/out/electronHttpExecutor");
  const { createUpdateManager } = require("./update-manager.cjs");
  const cacheRoot = path.join(tmp, name, "cache");
  const adapter = {
    version: "0.0.1", name: "chatcoder-e2e", isPackaged: true,
    appUpdateConfigPath: path.join(tmp, name + ".yml"),
    userDataPath: path.join(tmp, name), baseCachePath: cacheRoot,
    whenReady: () => Promise.resolve(), quit() {}, relaunch() {}, onQuit() {},
  };
  fs.writeFileSync(adapter.appUpdateConfigPath, `provider: generic\nurl: ${base}\nupdaterCacheDirName: chatcoder-updater-e2e\n`);
  // 自定义 app 适配器时 httpExecutor 为 null：必须先赋值，再 setFeedURL（Provider 创建时会捕获它）
  const updater = new NsisUpdater(null, adapter);
  updater.httpExecutor = new ElectronHttpExecutor();
  updater.logger = { info() {}, warn() {}, error() {}, debug() {} };
  const metaStore = { autoDownload };
  const pushes = [];
  const manager = createUpdateManager({
    pushState: (s) => pushes.push({ ...s }),
    meta: { read: () => ({ ...metaStore }), write: (p) => Object.assign(metaStore, p) },
    feeds: { mirror: { provider: "generic", url: base }, github: { provider: "generic", url: base } },
    updatesBaseUrl: base,
    repo: null, // 不把真实的 GitHub 列入候选源：测试只认本地服务器
    currentVersion: "0.0.1",
    net,
    log() {}, logErr() {},
    tuning: { chunkSize: 1 * MiB, backoffBaseMs: 20, backoffMaxMs: 200, retryDelayMs: 10 },
  });
  manager.attach(updater);
  const pendingFile = () => path.join(cacheRoot, "chatcoder-updater-e2e", "pending", NAME);
  return { updater, manager, pushes, metaStore, cacheRoot, pendingFile };
}

async function main() {
  const results = {};
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "upd-e2e-"));
  const server = await startServer();
  const base = `http://127.0.0.1:${server.address().port}/`;
  const { CancellationToken, NsisUpdater } = require("electron-updater");
  const { ElectronHttpExecutor } = require("electron-updater/out/electronHttpExecutor");
  const { installResumableDownloader } = require("./update-glue.cjs");

  // ═════════ A 下载引擎（直接驱动 electron-updater + 胶水）═════════
  {
    const cacheRoot = path.join(tmp, "A", "cache");
    const adapter = {
      version: "0.0.1", name: "chatcoder-e2e", isPackaged: true,
      appUpdateConfigPath: path.join(tmp, "A.yml"), userDataPath: path.join(tmp, "A"), baseCachePath: cacheRoot,
      whenReady: () => Promise.resolve(), quit() {}, relaunch() {}, onQuit() {},
    };
    fs.writeFileSync(adapter.appUpdateConfigPath, `provider: generic\nurl: ${base}\nupdaterCacheDirName: chatcoder-updater-e2e\n`);
    const updater = new NsisUpdater(null, adapter);
    updater.httpExecutor = new ElectronHttpExecutor();
    updater.setFeedURL({ provider: "generic", url: base });
    updater.autoDownload = false;
    updater.logger = { info() {}, warn() {}, error() {}, debug() {} };
    let info = null;
    const progress = [];
    const phases = [];
    updater.on("update-available", (i) => { info = i; });
    updater.on("download-progress", (p) => progress.push(p));
    let downloadedEvent = null;
    updater.on("update-downloaded", (e) => { downloadedEvent = e; });
    results.A_patched = installResumableDownloader({
      autoUpdater: updater, getInfo: () => info, buildUrls: (url) => [url.href], net,
      onPhase: (p) => phases.push(p), logger: { info() {}, warn() {} },
      tuning: { chunkSize: 1 * MiB, backoffBaseMs: 20, backoffMaxMs: 200 },
    });
    const check = await updater.checkForUpdates();
    results.A_available = !!(check && check.isUpdateAvailable) && !!info && info.version === "9.9.9";

    killRate = 0.35;
    const files = await updater.downloadUpdate(new CancellationToken());
    killRate = 0;
    results.A_sameContent = Buffer.compare(fs.readFileSync(files[0]), FILE) === 0;
    results.A_kills = stats.kills;
    results.A_progressMonotonic = progress.every((p, i) => i === 0 || p.transferred >= progress[i - 1].transferred);
    results.A_updateDownloadedEvent = !!downloadedEvent && downloadedEvent.version === "9.9.9";
    results.A_blockmapSkipped = stats.blockmap404 === 0; // 已关闭差分下载，不再请求 blockmap，安装包统一走全量下载器
    results.A_phases = [...new Set(phases)];

    // 中途取消 → 再下一次只补缺的块
    const pending = path.join(cacheRoot, "chatcoder-updater-e2e", "pending");
    fs.rmSync(pending, { recursive: true, force: true });
    updater.downloadedUpdateHelper = null;
    const partial = path.join(cacheRoot, "chatcoder-updater-e2e", "partial");
    fs.rmSync(partial, { recursive: true, force: true });
    pieceDelay = 8;
    const token = new CancellationToken();
    const before2 = stats.bytes;
    const onProg = (p) => { if (p.percent >= 40) token.cancel(); };
    updater.on("download-progress", onProg);
    let cancelled = false;
    try { await updater.downloadUpdate(token); } catch { cancelled = true; }
    updater.removeListener("download-progress", onProg);
    results.A_cancelled = cancelled;
    results.A_partialKept = fs.existsSync(partial) && fs.readdirSync(partial).some((f) => f.endsWith(".part.json"));
    const servedFirst = stats.bytes - before2;
    pieceDelay = 0;
    const before3 = stats.bytes;
    const files2 = await updater.downloadUpdate(new CancellationToken());
    results.A_resumedSameContent = Buffer.compare(fs.readFileSync(files2[0]), FILE) === 0;
    const servedSecond = stats.bytes - before3;
    results.A_servedFirstMiB = Math.round(servedFirst / MiB);
    results.A_servedSecondMiB = Math.round(servedSecond / MiB);
    results.A_resumed = servedSecond < FILE.length * 0.8;
  }

  // ═════════ B 静默自动下载 ═════════
  {
    const s = makeStack(tmp, base, "B", { autoDownload: true });
    pieceDelay = 2;
    await s.manager.checkForUpdates();
    // 发现新版本后应「直接」进入下载态，侧栏不会先闪一下 available
    await until(() => s.pushes.some((p) => p.state === "downloading"), 8000, "进入 downloading");
    const firstDownloadingIdx = s.pushes.findIndex((p) => p.state === "downloading");
    results.B_noAvailableFlash = !s.pushes.slice(0, firstDownloadingIdx + 1).some((p) => p.state === "available");
    results.B_firstIsConnecting = s.pushes[firstDownloadingIdx].phase === "connecting" && s.pushes[firstDownloadingIdx].auto === true;
    results.B_notesKept = s.pushes[firstDownloadingIdx].notes === "e2e" && s.pushes[firstDownloadingIdx].version === "9.9.9";
    await until(() => s.manager.getState().state === "downloaded", 30000, "下载完成");
    pieceDelay = 0;
    results.B_downloaded = s.manager.getState().state === "downloaded";
    results.B_sameContent = Buffer.compare(fs.readFileSync(s.pendingFile()), FILE) === 0;
    results.B_sawProgress = s.pushes.some((p) => p.state === "downloading" && p.phase === "downloading" && p.percent > 0);
    results.B_phasesSeen = [...new Set(s.pushes.filter((p) => p.state === "downloading").map((p) => p.phase))];
  }

  // ═════════ C 手动下载（关闭自动下载）═════════
  {
    const s = makeStack(tmp, base, "C", { autoDownload: false });
    await s.manager.checkForUpdates();
    results.C_availableNotDownloading = s.manager.getState().state === "available";
    pieceDelay = 6;
    const ret = s.manager.downloadNow(); // 同步返回：此刻就必须已经是 downloading(connecting)
    results.C_instantFeedback = ret.state === "downloading" && ret.phase === "connecting" && s.pushes.at(-1).state === "downloading";
    await until(() => s.manager.getState().percent > 5, 15000, "出现进度");
    // 下载期间再点「检查更新」：不得把状态冲回 checking / available
    const firstDl = s.pushes.findIndex((p) => p.state === "downloading");
    const duringCheck = await s.manager.checkForUpdates();
    await sleep(300);
    results.C_checkDuringDownloadKeepsState = duringCheck.state === "downloading" && s.manager.getState().state === "downloading"
      && !s.pushes.slice(firstDl).some((p) => p.state === "checking" || p.state === "available");
    pieceDelay = 0;
    await until(() => s.manager.getState().state === "downloaded", 30000, "下载完成");
    results.C_downloaded = true;
    // 不真的运行「安装包」（它只是随机字节）：桩掉 quitAndInstall，只验证管理器在 downloaded 态会调用它
    let installCalled = false;
    s.updater.quitAndInstall = () => { installCalled = true; };
    results.C_installReturnsTrue = s.manager.install() === true;
    await sleep(50);
    results.C_installCalled = installCalled;
  }

  // ═════════ D 所有下载源不可用 ═════════
  {
    const s = makeStack(tmp, base, "D", { autoDownload: true });
    exeGone = true;
    await s.manager.checkForUpdates();
    await until(() => s.manager.getState().state === "error", 30000, "给出错误");
    results.D_errorMessage = s.manager.getState().message;
    results.D_clearError = /所有下载源都不可用/.test(results.D_errorMessage || "");
    results.D_notStuckDownloading = !s.manager._internals.isDownloadActive();
    exeGone = false;
  }

  // ═════════ E 取消下载 / 切换自建源（plan-20261022-update-route，真实 CancellationToken）═════════
  {
    const s = makeStack(tmp, base, "E", { autoDownload: false });
    pieceDelay = 4;
    await s.manager.checkForUpdates();
    await until(() => s.manager.getState().state === "available", 8000, "available");
    s.manager.downloadNow();
    await until(() => (s.manager.getState().percent || 0) >= 15, 30000, "下载过半前");
    await s.manager.cancelDownload();
    results.E_cancelBackToAvailable = s.manager.getState().state === "available" && !s.manager._internals.isDownloadActive();
    const partial = path.join(s.cacheRoot, "chatcoder-updater-e2e", "partial");
    results.E_cancelKeepsPartial = fs.existsSync(partial) && fs.readdirSync(partial).some((f) => f.endsWith(".part.json"));
    // 再次下载 → 下到一半点「切换自建源」→ 重新开始并接着下完
    s.manager.downloadNow();
    await until(() => (s.manager.getState().percent || 0) >= 40, 30000, "续传过 40%");
    await s.manager.switchToPrimary();
    results.E_switchRestarts = s.manager.getState().state === "downloading";
    pieceDelay = 0;
    await until(() => s.manager.getState().state === "downloaded", 60000, "切换后下载完成");
    results.E_switchSameContent = Buffer.compare(fs.readFileSync(s.pendingFile()), FILE) === 0;
  }

  await new Promise((r) => server.close(r));
  fs.rmSync(tmp, { recursive: true, force: true });

  const mustBeTrue = [
    "A_patched", "A_available", "A_sameContent", "A_progressMonotonic", "A_updateDownloadedEvent", "A_blockmapSkipped",
    "A_cancelled", "A_partialKept", "A_resumedSameContent", "A_resumed",
    "B_noAvailableFlash", "B_firstIsConnecting", "B_notesKept", "B_downloaded", "B_sameContent", "B_sawProgress",
    "C_availableNotDownloading", "C_instantFeedback", "C_checkDuringDownloadKeepsState", "C_downloaded", "C_installReturnsTrue", "C_installCalled",
    "D_clearError", "D_notStuckDownloading",
    "E_cancelBackToAvailable", "E_cancelKeepsPartial", "E_switchRestarts", "E_switchSameContent",
  ];
  const failed = mustBeTrue.filter((k) => results[k] !== true);
  process.stdout.write(`E2E_RESULT ${JSON.stringify({ ok: failed.length === 0, failed, ...results })}\n`);
  return failed.length === 0 ? 0 : 1;
}

app.disableHardwareAcceleration();
app.whenReady().then(main).then((code) => app.exit(code), (e) => {
  process.stdout.write(`E2E_RESULT ${JSON.stringify({ ok: false, error: String((e && e.stack) || e) })}\n`);
  app.exit(1);
});
