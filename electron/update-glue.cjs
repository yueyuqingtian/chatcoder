"use strict";
/** electron-updater 与断点续传下载器之间的胶水（plan-20261021-usage-update）。
 *
 * 把 autoUpdater.httpExecutor.download（NSIS 全量安装包的下载入口）换成 update-downloader：
 * 其余请求（latest.yml、blockmap、差分下载的区间请求）仍走原实现。
 * 单独成文件是为了不依赖 main.cjs 的全局状态，能在真实的 electron-updater + Electron net 上做端到端验证。
 */
const path = require("path");
const { downloadResumable } = require("./update-downloader.cjs");

/**
 * @param {object} deps
 * @param {object} deps.autoUpdater  electron-updater 的 AppUpdater 实例
 * @param {() => object|null} deps.getInfo      最近一次 update-available 的 info（取 files[].size / sha512）
 * @param {(url: URL, name: string) => string[]} deps.buildUrls  安装包候选下载地址
 * @param {object} deps.net          Electron net（fetch / isOnline）
 * @param {(phase: string, info?: object) => void} [deps.onPhase]
 * @param {{info?: Function, warn?: Function}} [deps.logger]
 * @param {object} [deps.tuning]     透传给下载器的调参（测试用）
 * @returns {boolean} 是否已接管
 */
function installResumableDownloader({ autoUpdater, getInfo, buildUrls, net, onPhase, logger, tuning }) {
  const exec = autoUpdater && autoUpdater.httpExecutor;
  if (!exec || typeof exec.download !== "function") return false;
  const stock = exec.download.bind(exec);
  exec.download = async (url, destination, options) => {
    if (!/\.exe$/i.test((url && url.pathname) || "")) return stock(url, destination, options);
    const name = decodeURIComponent(path.posix.basename(url.pathname));
    const files = (getInfo() && getInfo().files) || [];
    const file = files.find((f) => decodeURIComponent(path.posix.basename(String(f.url).split("?")[0])) === name);
    const ac = new AbortController();
    const token = options && options.cancellationToken;
    if (token && typeof token.onCancel === "function") token.onCancel(() => ac.abort());
    await downloadResumable({
      urls: buildUrls(url, name),
      dest: destination,
      // 断点目录与 electron-updater 的 pending 目录同卷（同属其缓存目录），最后一步才能原子改名
      partDir: path.join(path.dirname(path.dirname(destination)), "partial"),
      sha512: (options && options.sha512) || (file && file.sha512),
      size: file && Number(file.size),
      // Chromium 网络栈：继承系统代理 / 证书设置，与 electron-updater 其余请求一致
      fetchImpl: (u, init) => net.fetch(u, init),
      isOnline: () => net.isOnline(),
      onProgress: (p) => { if (options && typeof options.onProgress === "function") options.onProgress(p); },
      onPhase: onPhase || (() => {}),
      signal: ac.signal,
      logger: logger || {},
      ...(tuning || {}),
    });
    return destination;
  };
  return true;
}

module.exports = { installResumableDownloader };
