"use strict";
/** 应用自动更新的状态机（electron-updater 的外层编排）。
 *
 * 从 main.cjs 抽出，原因：下载链路（立即反馈 / 断点续传 / 静默自动下载 / 失败重试）逻辑变多了，
 * 需要在真实的 electron-updater 上做端到端验证（见 update-e2e.harness.cjs），
 * 而 main.cjs 一启动就拉起窗口和后端，没法单独跑。依赖全部由 createUpdateManager 注入。
 *
 * 状态机: idle → checking → available|none → downloading → downloaded → (quitAndInstall) / error
 * 下载中（downloading）带 phase：connecting | downloading | waiting | retrying | verifying。
 */
const path = require("path");
const { installResumableDownloader } = require("./update-glue.cjs");
// plan-20261024-update-channel：三个通道（镜像1 / 镜像2 / github）的地址拼装与测速
const {
  CHANNEL_LABEL, CHANNEL_IDS, CHANNEL_TIER, isChannel, channelUrls, measureChannels, publicReport,
} = require("./update-channels.cjs");

/** GitHub 加速镜像前缀（plan-20261022-update-route）：拼在 GitHub 原始地址前面即可，支持 Range */
const GH_PROXY_PREFIX = "https://cdn.gh-proxy.org/";

/** releaseNotes 可能是 string 或 [{version, note}] 数组，统一成 Markdown 文本 */
function normalizeReleaseNotes(notes) {
  if (!notes) return "";
  if (typeof notes === "string") return notes;
  if (Array.isArray(notes)) {
    return notes.map((n) => (typeof n === "string" ? n : (n && (n.note || n.body)) || "")).join("\n\n");
  }
  if (typeof notes === "object") return notes.note || notes.body || "";
  return String(notes);
}

/**
 * @param {object} deps
 * @param {(state: object) => void} deps.pushState      把状态推给渲染进程
 * @param {{read: () => object, write: (patch: object) => void}} deps.meta  update-state.json 读写
 * @param {object} deps.feeds             { mirror, github } 两个 electron-updater feed 配置
 * @param {string} deps.updatesBaseUrl    自建源根地址（以 / 结尾）
 * @param {{owner: string, repo: string}|null} deps.repo   GitHub 仓库；为空则不把 GitHub Release 列入候选源（测试用）
 * @param {string} deps.currentVersion
 * @param {object} deps.net               Electron net
 * @param {(...a) => void} deps.log
 * @param {(...a) => void} deps.logErr
 * @param {string|null} [deps.ghProxy]    GitHub 加速镜像前缀；null 表示不用镜像（测试用）
 * @param {Function} [deps.createCancellationToken]  () => CancellationToken；缺省取 builder-util-runtime
 * @param {object} [deps.tuning]          透传给下载器的调参（测试用）
 * @param {(snapshot: object) => void} [deps.pushChannels] 通道测速 / 优选结果推给渲染进程
 */
function createUpdateManager(deps) {
  const { pushState, meta, feeds, updatesBaseUrl, repo, currentVersion, net, directFetch, log, logErr, tuning } = deps;
  const pushChannelsState = typeof deps.pushChannels === "function" ? deps.pushChannels : () => {};
  const ghProxy = deps.ghProxy === undefined ? GH_PROXY_PREFIX : deps.ghProxy;
  const createCancellationToken = deps.createCancellationToken || (() => {
    const { CancellationToken } = require("builder-util-runtime");
    return new CancellationToken();
  });

  let autoUpdater = null;
  let state = { state: "idle" };
  // 最近一次检查的版本信息（含 files[].size / sha512，以及 latest.yml 里可选的 mirrors），下载器与测速据此拼候选源
  let lastUpdateInfo = null;
  // 一次下载任务（含重试）正在进行：期间 electron-updater 抛出的 error 事件、周期检查都不得冲掉 downloading 状态
  let downloadActive = false;
  // 当前下载任务的取消令牌，以及取消的原因（cancel=用户取消 / restart=切换线路后重新开始）
  let downloadToken = null;
  let cancelIntent = null;
  // 静默自动下载：同一版本本次运行最多尝试几次，避免每 20 分钟检查一次就重复失败
  const AUTO_DOWNLOAD_MAX_ATTEMPTS = 3;
  const autoDownloadAttempts = new Map();
  const timers = [];
  // plan-20261024-update-channel：通道测速结果与自动优选。
  //  preferred = 测速得出的最快通道；用户手动指定时以设置为准（channel != "auto"）。
  let measureReport = null; // { channels: {...}, best, testedAt }（已脱敏，不含真实地址）
  let preferredChannel = null;
  let measuring = false;
  let measureSeq = 0; // 并发测速时只让最后一次结果生效
  let lastAutoMeasureAt = 0;
  // 自动测速的最小间隔：下载失败后重新检查也会跑测速，太频繁没有意义、还白白消耗流量
  const AUTO_MEASURE_MIN_INTERVAL_MS = 60 * 1000;

  // 恢复上次测速结果与优选通道：启动时就能展示，不必等新一轮跑完
  {
    const saved = meta.read() || {};
    if (saved.channelReport && typeof saved.channelReport === "object") measureReport = saved.channelReport;
    if (isChannel(saved.preferredChannel)) preferredChannel = saved.preferredChannel;
  }

  const push = () => { try { pushState(state); } catch { /* 窗口已销毁 */ } };

  function getSettings() {
    const m = meta.read();
    return {
      autoDownload: m.autoDownload !== false,
      // "auto" = 按测速结果自动选；否则为手动指定的通道 id
      channel: isChannel(m.channel) ? m.channel : "auto",
    };
  }

  /** 实际用于下载的通道：手动指定优先，否则用测速得到的最优，最后回落镜像1 */
  function effectiveChannel() {
    const s = getSettings();
    if (s.channel !== "auto") return s.channel;
    return preferredChannel || "mirror1";
  }

  /** 通道顺序：优选/指定通道排第一，其余按固定顺序跟进（用于选源排序与检查更新的 feed 链） */
  function orderedChannels() {
    const first = effectiveChannel();
    return [first, ...CHANNEL_IDS.filter((c) => c !== first)];
  }

  /** 测速用的文件名：有具体版本就测安装包本身，否则测 latest.yml（体积小、任何状态下都能测连通性） */
  function probeName() {
    const info = lastUpdateInfo || {};
    const files = Array.isArray(info.files) ? info.files : [];
    const f = files.find((x) => x && /\.exe$/i.test(String(x.url)));
    return f ? decodeURIComponent(path.posix.basename(String(f.url).split("?")[0])) : "latest.yml";
  }

  function channelSnapshot() {
    return {
      report: measureReport,
      preferred: preferredChannel,
      effective: effectiveChannel(),
      mode: getSettings().channel,
      measuring,
      labels: CHANNEL_LABEL,
    };
  }

  function pushChannels() {
    try { pushChannelsState(channelSnapshot()); } catch { /* 窗口已销毁 */ }
  }

  /** 并行测三条通道并记录最优：检查更新后自动跑一次，也供「重新测速」手动触发 */
  async function measureNow({ force = false } = {}) {
    if (!autoUpdater) return channelSnapshot();
    if (!net || typeof net.fetch !== "function") return channelSnapshot(); // 无网络栈（单测环境）：跳过
    if (measuring) return channelSnapshot();
    // 自动触发时节流：定时检查 20 分钟一次不受影响，下载失败重查不会把它变成高频动作
    if (!force && Date.now() - lastAutoMeasureAt < AUTO_MEASURE_MIN_INTERVAL_MS) return channelSnapshot();
    lastAutoMeasureAt = Date.now();
    const seq = ++measureSeq;
    measuring = true;
    pushChannels();
    const name = probeName();
    const info = lastUpdateInfo || {};
    const urlsByChannel = {};
    for (const ch of CHANNEL_IDS) urlsByChannel[ch] = channelUrls({ channel: ch, name, info, repo, updatesBaseUrl, ghProxy });
    let report = null;
    try {
      report = await measureChannels({
        urlsByChannel,
        // 镜像1（gh-proxy）是境外 CDN，和下载一致地绕开系统代理
        fetchFor: (ch) => (ch === "mirror1" && typeof directFetch === "function"
          ? directFetch
          : (u, init) => net.fetch(u, init)),
      });
    } catch (e) {
      logErr("[updater] 通道测速失败:", e && e.message);
    }
    if (seq !== measureSeq) return channelSnapshot(); // 已有更新的测速在跑，丢弃本次
    measuring = false;
    if (report) {
      measureReport = publicReport(report);
      preferredChannel = report.best || null;
      // 落盘：下次打开软件先显示上次的测速结果，不用等新一轮跑完
      meta.write({ channelReport: measureReport, preferredChannel });
      log("[updater] 通道测速:", JSON.stringify(measureReport.channels), "最优:", preferredChannel);
    }
    pushChannels();
    return channelSnapshot();
  }

  /** 手动指定通道；下载中就停下并按新通道重新开始（已完成的块保留，续传接着下） */
  async function setChannel(id) {
    if (id !== "auto" && !isChannel(id)) return channelSnapshot();
    meta.write({ channel: id === "auto" ? "auto" : id });
    log("[updater] 下载通道设为:", id);
    if (downloadActive) {
      const auto = !!state.auto;
      await stopDownload("restart");
      if (state.state === "available") void startDownload({ auto });
    }
    pushChannels();
    return channelSnapshot();
  }

  /** 合并进当前 downloading 状态并推送（非下载态忽略，避免迟到的进度事件把别的状态冲掉） */
  function setDownloadState(patch) {
    if (state.state !== "downloading") return;
    state = { ...state, ...patch };
    push();
  }

  function onDownloadPhase(phase, info) {
    const i = info || {};
    setDownloadState({
      phase,
      source: i.label || i.source || undefined,
      tier: i.tier || undefined,
      attempt: i.attempt || undefined,
      retryInMs: i.retryInMs != null ? i.retryInMs : undefined,
      nextLabel: i.nextLabel || undefined,
    });
  }

  /** 安装包候选下载地址（plan-20261024-update-channel）：按「优选/指定通道 > 其余通道」排列，
   *  每个地址带上 label（镜像1 / 镜像2 / github）与 rank（显式优先级，供选源排序用）。
   *  整包 sha512 来自 latest.yml，下载自哪个通道都要通过同一个校验。 */
  function buildDownloadUrls(_url, name) {
    const info = lastUpdateInfo || {};
    const list = [];
    const seen = new Set();
    let rank = 0;
    for (const ch of orderedChannels()) {
      for (const url of channelUrls({ channel: ch, name, info, repo, updatesBaseUrl, ghProxy })) {
        if (seen.has(url)) continue;
        seen.add(url);
        list.push({ url, tier: CHANNEL_TIER[ch], channel: ch, label: CHANNEL_LABEL[ch], rank: rank++ });
      }
    }
    return list;
  }

  /** 检查更新用的 feed 链：同样按「优选/指定通道 > 其余」排列。
   *  未测速时等价于旧行为：镜像1(gh-proxy) > 镜像2(自建源) > github。 */
  function feedChain() {
    const chain = [];
    for (const ch of orderedChannels()) {
      if (ch === "mirror1") {
        if (repo && ghProxy) {
          chain.push([CHANNEL_LABEL.mirror1, { provider: "generic", url: `${ghProxy}https://github.com/${repo.owner}/${repo.repo}/releases/latest/download/`, useMultipleRangeRequest: false }]);
        }
      } else if (ch === "mirror2") {
        chain.push([CHANNEL_LABEL.mirror2, feeds.mirror]);
      } else if (feeds.github) {
        chain.push([CHANNEL_LABEL.github, feeds.github]);
      }
    }
    return chain;
  }

  async function checkForUpdatesSmart() {
    if (!autoUpdater) return null;
    const chain = feedChain();
    let result = null;
    let lastErr = null;
    for (let i = 0; i < chain.length; i++) {
      const [label, feed] = chain[i];
      try {
        autoUpdater.setFeedURL(feed);
        result = await autoUpdater.checkForUpdates();
        lastErr = null;
        break;
      } catch (e) {
        lastErr = e;
        if (i === chain.length - 1) break;
        logErr(`[updater] ${label}检查失败，改用${chain[i + 1][0]}:`, e && e.message);
      }
    }
    // plan-20261024-update-channel：每次检查（启动 / 手动 / 定时）后都重新测速并优选下载通道。
    // 放在检查之后而不是之前：先拿到 latest.yml 的版本信息，测的才是真正的安装包地址。
    // 不阻塞检查结果的返回；测速失败不影响任何已有行为。
    void measureNow().catch((e) => logErr("[updater] 自动测速失败:", e && e.message));
    if (lastErr) throw lastErr;
    return result;
  }

  function friendlyError(e) {
    const kind = e && e.kind;
    if (kind === "disk") return "磁盘空间不足或无法写入更新文件，请清理磁盘后重试";
    if (kind === "checksum") return "下载的安装包校验失败，已清除，请重试";
    return String((e && e.message) || e);
  }

  /** 开始下载（手动点击 / 静默自动）。立即进入 downloading(connecting)，让界面第一时间有反馈；
   *  失败按类型重试：校验失败重下，所有源不可用则重新检查（服务器上可能已换成更新的版本），磁盘问题直接报错。 */
  async function startDownload({ auto = false } = {}) {
    if (!autoUpdater || downloadActive || state.state !== "available") return state;
    const keep = { version: state.version, notes: state.notes, releaseDate: state.releaseDate };
    downloadActive = true;
    cancelIntent = null;
    state = { state: "downloading", ...keep, percent: 0, transferred: 0, total: 0, bytesPerSecond: 0, phase: "connecting", auto };
    push();
    log("[updater] 开始下载", keep.version, auto ? "(静默自动)" : "(手动)");
    try {
      for (let attempt = 1; ; attempt++) {
        try {
          downloadToken = createCancellationToken();
          // 每次下载都新建选源状态机：取消后重新下载 / 切换线路都会从镜像开始（已下载的块保留）
          await autoUpdater.downloadUpdate(downloadToken); // 成功后 update-downloaded 事件把状态置为 downloaded
          return state;
        } catch (e) {
          const kind = e && e.kind;
          if (cancelIntent) throw e; // 用户取消 / 切换线路：不重试，由外层统一处理
          logErr(`[updater] 下载失败(第 ${attempt} 次, ${kind || "unknown"}):`, e && e.message);
          if (kind === "disk" || kind === "aborted" || attempt >= 3) throw e;
          if (kind === "fatal") {
            // 所有下载源都不可用：多半是服务器上已经换成了更新的版本，重新检查后对新版本重来
            await checkForUpdatesSmart().catch(() => {});
            if (lastUpdateInfo && lastUpdateInfo.version) setDownloadState({ version: lastUpdateInfo.version });
          } else {
            await new Promise((r) => setTimeout(r, (tuning && tuning.retryDelayMs != null ? tuning.retryDelayMs : 3000) * attempt));
          }
          setDownloadState({ percent: 0, transferred: 0, phase: "connecting" });
        }
      }
    } catch (e) {
      if (cancelIntent) {
        // 取消不是错误：回到「发现新版本」，断点文件保留，再点下载会接着下
        log("[updater] 下载已停止:", cancelIntent);
        state = { state: "available", ...keep };
      } else {
        state = { state: "error", message: friendlyError(e) };
      }
      push();
      return state;
    } finally {
      downloadActive = false;
      downloadToken = null;
    }
  }

  /** 取消当前下载并等它真正停下（取消令牌触发后，下载器会中断在途请求并落盘进度） */
  async function stopDownload(intent) {
    if (!downloadActive) return;
    cancelIntent = intent;
    try { if (downloadToken) downloadToken.cancel(); } catch { /* ignore */ }
    for (let i = 0; i < 400 && downloadActive; i++) await new Promise((r) => setTimeout(r, 25));
  }

  /** 用户在「关于」里手动取消。同一版本本次运行不再静默自动下载，避免定时检查又把它拉起来 */
  async function cancelDownload() {
    if (!downloadActive) return state;
    if (state.version) autoDownloadAttempts.set(state.version, AUTO_DOWNLOAD_MAX_ATTEMPTS);
    await stopDownload("cancel");
    return state;
  }

  /** 「切换线路」：停掉当前下载并立刻重新开始——新的选源状态机从第一优先级（镜像）起步，断点续传接着下 */
  async function switchToPrimary() {
    if (!downloadActive) return state;
    const auto = !!state.auto;
    await stopDownload("restart");
    if (state.state === "available") void startDownload({ auto });
    return state;
  }

  /** 绑定 electron-updater 实例：事件 → 状态机；全量下载换成断点续传 */
  function attach(updater) {
    autoUpdater = updater;
    // 下载由我们自己触发：用户点击，或「发现新版本自动下载」打开时静默触发（见 update-available）
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;
    const ok = installResumableDownloader({
      autoUpdater,
      getInfo: () => lastUpdateInfo,
      buildUrls: buildDownloadUrls,
      net,
      directFetch,
      onPhase: onDownloadPhase,
      logger: { info: (...a) => log(...a), warn: (...a) => log(...a) },
      tuning,
    });
    if (!ok) logErr("[updater] httpExecutor 不可用，沿用 electron-updater 默认下载");

    autoUpdater.on("checking-for-update", () => {
      // 已下载完成待安装 / 正在下载时，后续检查不得把状态冲回（否则侧栏「重启」「进度」会闪回「检查中」）
      if (state.state === "downloaded" || state.state === "downloading") return;
      state = { state: "checking" };
      log("[updater] checking-for-update");
      push();
    });
    autoUpdater.on("update-available", (info) => {
      lastUpdateInfo = info;
      // 已下载完成（同版本已就绪）/ 下载中：保持原状态，避免定时检查把按钮冲回「下载」
      if (state.state === "downloaded" && state.version === info.version) return;
      if (state.state === "downloading") return;
      // plan-230-1144 M4.2: 透传 releaseNotes/releaseDate——此前只取 version，
      // 前端因此看不到"更新了什么"（问题6 的直接根因）。
      state = {
        state: "available",
        version: info.version,
        notes: normalizeReleaseNotes(info.releaseNotes),
        releaseDate: info.releaseDate || "",
      };
      log("[updater] update-available:", info.version, "current:", currentVersion);
      // 静默自动下载：直接进入下载态，不让侧栏先闪一下「更新」按钮
      const tried = autoDownloadAttempts.get(info.version) || 0;
      if (getSettings().autoDownload && tried < AUTO_DOWNLOAD_MAX_ATTEMPTS) {
        autoDownloadAttempts.set(info.version, tried + 1);
        void startDownload({ auto: true });
        return;
      }
      push();
    });
    autoUpdater.on("update-not-available", (info) => {
      // 已下载完成 / 下载中：保持原状态（防御性保护）
      if (state.state === "downloaded" || state.state === "downloading") return;
      // plan-20261116-update-probe：没有新版本也会返回完整信息，否则两条 GitHub 通道因缺少版本而从未发起测速。
      lastUpdateInfo = info || null;
      state = { state: "none", version: info && info.version };
      log("[updater] update-not-available");
      push();
    });
    autoUpdater.on("download-progress", (p) => {
      setDownloadState({
        percent: Math.round((p.percent || 0) * 10) / 10,
        transferred: p.transferred || 0,
        total: p.total || 0,
        bytesPerSecond: p.bytesPerSecond || 0,
        phase: "downloading",
      });
    });
    autoUpdater.on("update-downloaded", (info) => {
      state = {
        state: "downloaded",
        version: info.version,
        notes: normalizeReleaseNotes(info.releaseNotes),
        releaseDate: info.releaseDate || "",
      };
      log("[updater] update-downloaded:", info.version);
      push();
    });
    autoUpdater.on("error", (err) => {
      // 下载任务自己会按类型重试并在最终失败时设置 error 状态；中途的 error 事件只记日志
      if (downloadActive) { logErr("[updater] (下载中) error:", String((err && err.message) || err)); return; }
      state = { state: "error", message: String((err && err.message) || err) };
      logErr("[updater] error:", state.message);
      push();
    });
  }

  /** 打开软件 3s 内立即检查一次，之后每 20 分钟自动检测一次。
   *  （主源为自建服务器；GitHub 备源匿名 API 限流 60 次/小时，20 分钟间隔量级安全）
   *  检查到新版本且「自动下载」开启时，会在 update-available 里直接开始静默下载。 */
  function startTimers({ firstMs = 3 * 1000, intervalMs = 20 * 60 * 1000 } = {}) {
    timers.push(setTimeout(() => {
      checkForUpdatesSmart().catch((e) => logErr("[updater] 自动检查失败(启动后首查):", e && e.message));
    }, firstMs));
    timers.push(setInterval(() => {
      // 下载中/已下载完成时跳过自动检查：避免 update-available 事件把 downloaded 状态冲回 available
      if (state.state === "downloading" || state.state === "downloaded") return;
      checkForUpdatesSmart().catch((e) => logErr("[updater] 自动检查失败(定时):", e && e.message));
    }, intervalMs));
  }

  function stopTimers() {
    for (const t of timers.splice(0)) { clearTimeout(t); clearInterval(t); }
  }

  // ── 供 IPC 使用的入口 ──
  async function checkForUpdates() {
    if (!autoUpdater) return { state: "unsupported" };
    // 下载中 / 已下载：再检查只会把状态冲回去，直接返回当前状态
    if (state.state === "downloading" || state.state === "downloaded") return state;
    autoDownloadAttempts.clear(); // 手动检查 = 用户想重试：静默下载的次数额度重置
    try {
      return await checkForUpdatesSmart();
    } catch (e) {
      state = { state: "error", message: String((e && e.message) || e) };
      push();
      return state;
    }
  }

  /** 不等下载完成：立即进入 downloading(connecting) 并返回，后续进度靠状态推送 */
  function downloadNow() {
    if (autoUpdater && state.state === "available") void startDownload({ auto: false });
    return state;
  }

  function setSettings(patch) {
    if (patch && typeof patch.autoDownload === "boolean") meta.write({ autoDownload: patch.autoDownload });
    // plan-20261024-update-channel：通道选择与自动下载共用一个设置入口
    if (patch && typeof patch.channel === "string") {
      const id = patch.channel;
      if (id === "auto" || isChannel(id)) {
        meta.write({ channel: id });
        pushChannels();
        // 下载中改通道：停下按新通道重开（已完成的块保留）
        if (downloadActive && state.state === "downloading") void setChannel(id);
      }
    }
    const settings = getSettings();
    // 刚打开「自动下载」且已经发现了新版本：立即开始，不用等下一次检查
    if (settings.autoDownload && autoUpdater && state.state === "available") {
      autoDownloadAttempts.clear();
      void startDownload({ auto: true });
    }
    return settings;
  }

  /** 通道快照（给「关于」页的下载通道卡片）：含测速结果、优选通道、当前生效通道 */
  function getChannels() {
    return channelSnapshot();
  }

  function install() {
    if (!autoUpdater || state.state !== "downloaded") return false;
    log("[updater] quitAndInstall ->", state.version);
    setImmediate(() => { try { autoUpdater.quitAndInstall(); } catch (e) { logErr("[updater] quitAndInstall 失败:", e); } });
    return true;
  }

  return {
    attach, startTimers, stopTimers,
    getState: () => state,
    checkForUpdates, downloadNow, cancelDownload, switchToPrimary, getSettings, setSettings, install,
    // plan-20261024-update-channel：关于页的下载通道（测速 / 选择）
    getChannels, measureChannels: measureNow, setChannel,
    // 供测试观察
    _internals: {
      buildDownloadUrls, startDownload, checkForUpdatesSmart, feedChain, onDownloadPhase,
      isDownloadActive: () => downloadActive, autoDownloadAttempts,
      effectiveChannel, orderedChannels, probeName, setPreferredChannel: (c) => { preferredChannel = isChannel(c) ? c : null; },
    },
  };
}

module.exports = { createUpdateManager, normalizeReleaseNotes, GH_PROXY_PREFIX };
