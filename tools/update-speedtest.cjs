"use strict";
/** 更新源测速工具（诊断用，不参与打包）。
 *
 * 为什么单独做一个：应用只在「发现新版本」时才会真正下载，已是最新版本时无法触发，
 * 也就看不到自建源的速度。而用 curl / Node 的 fetch 测又被代理与 TLS 栈差异干扰
 * （curl 不读 Windows 系统代理，Node fetch 不继承证书设置），测出来的数与应用内对不上。
 * 这里直接复用客户端下载器的那套网络栈与分块参数，测的就是真实链路。
 *
 * 参数与 electron/update-downloader.cjs 的默认值保持一致：
 *   chunkSize = 4MiB、concurrency = 4、Range 单区间请求。
 *
 * 用法（PowerShell，在仓库根目录）：
 *   node_modules\electron\dist\electron.exe tools\update-speedtest.cjs
 *   ... --size 128 --conn 4          # 测 128MiB、4 连接并行
 *   ... --mode stream --conn 1       # 单连接长流，看单条连接上限
 *   ... --timeout 600                # 整体超时秒数（默认 300）
 *
 * 换源对比请用环境变量 CHATCODER_SPEEDTEST_URL：Electron 会把 --url 后面的地址当成
 * 自己的启动参数吃掉，脚本收不到（表现为进程无任何输出）。
 *   $env:CHATCODER_SPEEDTEST_URL="https://..."; node_modules\electron\dist\electron.exe tools\update-speedtest.cjs
 *
 * 结果同时写入 update-speedtest.log：Electron 在 Windows 控制台的 stdout 偶尔会丢，
 * 卡住时也能从文件里看到测到了哪一步。
 */
const fs = require("fs");
const path = require("path");
const { app, net, session } = require("electron");

const MiB = 1024 * 1024;

/** 读命令行参数：--name value */
function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && i + 1 < process.argv.length) return process.argv[i + 1];
  return fallback;
}

const TARGET_URL = process.env.CHATCODER_SPEEDTEST_URL || arg("url", "https://service.guyueyu.asia/updates/chatcoder-Setup-0.9.0.exe");
const TOTAL_BYTES = Math.max(1, Number(arg("size", "64"))) * MiB;
const CONNECTIONS = Math.max(1, Number(arg("conn", "4")));
const CHUNK_BYTES = Math.max(1, Number(arg("chunk", "4"))) * MiB;
const MODE = arg("mode", "chunked"); // chunked=多连接分块（与客户端一致） / stream=单连接长流
const TIMEOUT_MS = Math.max(10, Number(arg("timeout", "300"))) * 1000;
// 单个请求的最长等待：源卡死时尽快报错，而不是让整个测速无声挂住
const REQUEST_TIMEOUT_MS = 120 * 1000;
const OUT_FILE = arg("out", path.join(require("os").tmpdir(), "chatcoder-update-speedtest.log"));

try { fs.writeFileSync(OUT_FILE, "", "utf-8"); } catch { /* 不可写则只走控制台 */ }

/** 同时输出到控制台与日志文件 */
function log(line) {
  console.log(line);
  try { fs.appendFileSync(OUT_FILE, line + "\n", "utf-8"); } catch { /* 忽略 */ }
}

const stats = { bytes: 0, firstByteAt: 0, startedAt: 0 };
const perConnBytes = [];

function onData(n) {
  if (!stats.firstByteAt) stats.firstByteAt = Date.now();
  stats.bytes += n;
}

function human(n) {
  if (n >= MiB) return (n / MiB).toFixed(1) + "MiB";
  if (n >= 1024) return (n / 1024).toFixed(1) + "KiB";
  return n + "B";
}

const speed = (bytes, sec) => (sec > 0 ? (bytes / sec / MiB).toFixed(2) + " MiB/s" : "-");

/** 请求限时：既响应外部取消（整体超时），也限制单个请求的最长耗时 */
function requestSignal(signal) {
  return AbortSignal.any ? AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]) : signal;
}

/** 单连接长流：一次 Range 请求读满 TOTAL_BYTES，测单条连接的持续吞吐 */
async function runStream(signal) {
  const res = await net.fetch(TARGET_URL, { headers: { Range: `bytes=0-${TOTAL_BYTES - 1}` }, signal: requestSignal(signal) });
  if (res.status !== 206 && res.status !== 200) throw new Error(`HTTP ${res.status}`);
  if (res.status === 200) log("注意: 该源忽略 Range，返回整包 200，测的是完整文件下载");
  const reader = res.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    onData(value.length);
    if (stats.bytes >= TOTAL_BYTES) {
      try { await reader.cancel(); } catch { /* 主动停止 */ }
      break;
    }
  }
}

/** 多连接分块：与 update-downloader 的 runChunked 同构，但只收数据不落盘、不校验 */
async function runChunked(signal) {
  const starts = [];
  for (let off = 0; off < TOTAL_BYTES; off += CHUNK_BYTES) starts.push(off);
  let cursor = 0;

  async function worker(idx) {
    for (;;) {
      const i = cursor++;
      if (i >= starts.length) return;
      const start = starts[i];
      const end = Math.min(TOTAL_BYTES, start + CHUNK_BYTES) - 1;
      const res = await net.fetch(TARGET_URL, { headers: { Range: `bytes=${start}-${end}` }, signal: requestSignal(signal) });
      if (res.status !== 206) {
        if (res.status === 200) throw new Error("该源忽略 Range（返回 200），无法多连接，请改用 --mode stream");
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        onData(value.length);
        perConnBytes[idx] = (perConnBytes[idx] || 0) + value.length;
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(CONNECTIONS, starts.length) }, (_, i) => worker(i)));
}

app.disableHardwareAcceleration();

log(`[启动] ${new Date().toISOString()} pid=${process.pid}`);

app.whenReady().then(async () => {
  let proxy = "?";
  try { proxy = await session.defaultSession.resolveProxy(TARGET_URL); } catch { /* 取不到不影响测速 */ }
  log(`目标: ${TARGET_URL}`);
  log(`网络栈: Electron net.fetch（与客户端一致） | 代理解析: ${proxy} | 在线: ${net.isOnline()}`);
  log(`模式: ${MODE} | 连接数: ${MODE === "stream" ? 1 : CONNECTIONS} | 分块: ${human(CHUNK_BYTES)} | 总量: ${human(TOTAL_BYTES)}`);
  log("---- 开始 ----");

  const ctl = new AbortController();
  const killer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  const timer = setInterval(() => {
    const sec = (Date.now() - stats.startedAt) / 1000;
    log(`  ${sec.toFixed(0)}s 已下载 ${human(stats.bytes)} | 平均 ${speed(stats.bytes, sec)}`);
  }, 5000);

  stats.startedAt = Date.now();
  let failure = null;
  try {
    if (MODE === "stream") await runStream(ctl.signal);
    else await runChunked(ctl.signal);
  } catch (e) {
    failure = e;
  }
  clearInterval(timer);
  clearTimeout(killer);

  const sec = (Date.now() - stats.startedAt) / 1000;
  const ttfb = stats.firstByteAt ? (stats.firstByteAt - stats.startedAt) / 1000 : -1;
  log("---- 结果 ----");
  if (failure) log(`失败: ${(failure && failure.message) || failure}`);
  log(`用时 ${sec.toFixed(2)}s | 首字节 ${ttfb >= 0 ? ttfb.toFixed(2) + "s" : "无"} | 下载 ${human(stats.bytes)} | 平均 ${speed(stats.bytes, sec)}`);
  if (MODE !== "stream" && perConnBytes.length) {
    log(`每连接: ${perConnBytes.map((b) => speed(b, sec)).join(" / ")}`);
  }
  log(`结果文件: ${OUT_FILE}`);
  app.quit();
});

setTimeout(() => {
  log("总超时，强制退出");
  app.quit();
}, TIMEOUT_MS + 30000);
