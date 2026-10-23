// 内置浏览器 · 主进程能力（plan-20261015-browser-agent）
//
// 右侧面板里的每个 <webview> 标签挂载后，渲染层调用 browserAgent:prepare 把它交给这里：
//   ① 关闭后台节流：面板折叠 / 切到别的标签 / 窗口最小化时页面照常运行，AI 可在后台操作；
//      （实测：节流不关时隐藏页的 rAF 停摆、CDP 鼠标事件等不到回执）
//   ② 挂 CDP 调试器：记录网络请求、接管网页 JS 弹窗——webview 默认会弹**系统原生对话框**
//      并卡住页面（实测），AI 操作期间由 AI 按策略应答，用户操作触发的转给渲染层用应用内对话框询问；
//   ③ 网页打开新窗口（target=_blank / window.open）一律转成面板里的新标签，不再静默失败。
// 截图走 capturePage(stayHidden)：webview 停泊在视口内时，即使面板隐藏也能截（实测 7–12 ms）。
// plan-20261015-browser-perf：主窗口被遮挡/最小化时合成器不出帧，capturePage 会一直等；
// 此时限时后改走 CDP 截图兜底，仍失败就在几秒内给出可读原因，而不是卡满 15 秒。
// 同样因为宿主页面此时被判为 hidden、定时器被节流到 1 秒级，宿主的等待一律走这里的 sleep（主进程计时不受影响）。
//
// plan-20261018-operator-boost：
//   ① AI 浏览器动作期间整段关闭主窗口后台节流（每次动作续期，空闲 AGENT_IDLE_MS 后恢复）。此前每个动作
//      各自临时开关一次（截图兜底、导航后补帧），窗口最小化时一次点击要多付约 1.5 s；截图 / 补帧与动作窗口
//      共用一个引用计数，互不提前恢复节流。
//   ② 面板网页的下载一律保存到「浏览器下载目录」（设置可改，默认安装目录下的 downloads，不存在自动创建），
//      不再弹系统保存框（常被主窗口挡住，用户与 AI 都不知道文件去哪了）；进度与结果作为页面事件回报给 AI。
//
// 只接受「宿主是主窗口」的 webview guest，任何其它 webContents 一律拒绝。
const { app, ipcMain, webContents, clipboard, nativeImage, shell } = require("electron");
const fs = require("fs");
const path = require("path");

const NET_MAX = 400;
// 动作结果里回报的接口：只看页面脚本发起的请求（图片、样式等资源的失败不算业务报错）
const API_TYPES = new Set(["XHR", "Fetch"]);
const BODY_MAX = 300;

function withTimeout(promise, ms, msg) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(msg || "操作超时")), ms)),
  ]);
}

function clampInt(v, lo, hi, dflt) {
  const n = Number(v);
  if (!Number.isFinite(n)) return dflt;
  return Math.max(lo, Math.min(hi, Math.round(n)));
}

// AI 动作窗口：最后一次动作之后多久恢复主窗口节流
const AGENT_IDLE_MS = 20000;
// 不算「AI 在操作网页」的原语：设置页读写下载目录、剪贴板、纯等待、读下载记录
const PASSIVE_OPS = new Set(["downloadDir", "downloads", "clipboard", "sleep"]);
const DOWNLOAD_PREF_FILE = () => path.join(app.getPath("userData"), "browser-download-pref.json");
const DOWNLOADS_MAX = 50;

/** 默认下载目录：安装目录（exe 所在目录）下的 downloads；开发态没有安装目录，用用户数据目录。 */
function defaultDownloadDir() {
  if (app.isPackaged) return path.join(path.dirname(app.getPath("exe")), "downloads");
  return path.join(app.getPath("userData"), "downloads");
}

function writableDir(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.accessSync(dir, fs.constants.W_OK);
    return true;
  } catch { return false; }
}

function readDownloadPref() {
  try { return String(JSON.parse(fs.readFileSync(DOWNLOAD_PREF_FILE(), "utf8")).dir || ""); } catch { return ""; }
}

/** 实际使用的下载目录：用户配置 → 默认目录 → 用户数据目录（安装在只读位置时的回落），并确保已创建。 */
function effectiveDownloadDir() {
  const custom = readDownloadPref();
  if (custom && writableDir(custom)) return { dir: custom, custom: true, fallback: false };
  const dflt = defaultDownloadDir();
  if (writableDir(dflt)) return { dir: dflt, custom: false, fallback: !!custom };
  const alt = path.join(app.getPath("userData"), "downloads");
  writableDir(alt);
  return { dir: alt, custom: false, fallback: true };
}

/** 同名文件不覆盖：report.xlsx → report (1).xlsx */
function uniquePath(dir, name) {
  const safe = String(name || "download").replace(/[\\/:*?"<>|]+/g, "_").slice(0, 180) || "download";
  const ext = path.extname(safe);
  const base = safe.slice(0, safe.length - ext.length);
  let p = path.join(dir, safe);
  for (let i = 1; fs.existsSync(p) && i < 1000; i++) p = path.join(dir, `${base} (${i})${ext}`);
  return p;
}

module.exports = function setupBrowserAgent({ getMainWindow, log, logErr }) {
  /** wcId → { wc, attached, net: [], netById: Map, seq, aiUntil, accept, promptText, events: [], pendingDialog } */
  const guests = new Map();
  // 最近一次「宿主隐藏导致截图失败」的时刻与窗口：最小化期间不重复空等
  let lastHiddenFail = 0;
  let hiddenFailWin = 0;

  // ── 主窗口出帧保持（plan-20261018-operator-boost）──
  // holds：截图 / 补帧这类「这一刻必须出帧」的临时需求；agentActive：AI 动作窗口。任一存在即关闭主窗口节流。
  // 只恢复「我们关掉的」节流：本来就关着的不去动。
  let frameHolds = 0;
  let agentTimer = null;
  let agentActive = false;
  let throttleOwned = false;
  function applyThrottle() {
    const mw = getMainWindow();
    if (!mw || mw.isDestroyed()) { throttleOwned = false; return; }
    const need = frameHolds > 0 || agentActive;
    try {
      const wc = mw.webContents;
      const current = typeof wc.getBackgroundThrottling === "function" ? wc.getBackgroundThrottling() : !throttleOwned;
      if (need && current) { wc.setBackgroundThrottling(false); throttleOwned = true; }
      else if (!need && throttleOwned) { wc.setBackgroundThrottling(true); throttleOwned = false; }
    } catch { /* 窗口销毁竞态 */ }
  }
  function holdFrames() {
    frameHolds++;
    applyThrottle();
    let released = false;
    return () => { if (released) return; released = true; frameHolds = Math.max(0, frameHolds - 1); applyThrottle(); };
  }
  function markAgentActive() {
    agentActive = true;
    applyThrottle();
    if (agentTimer) clearTimeout(agentTimer);
    agentTimer = setTimeout(() => { agentTimer = null; agentActive = false; applyThrottle(); }, AGENT_IDLE_MS);
  }

  // ── 下载（plan-20261018-operator-boost）──
  /** 最近的下载记录（新的在后），供 downloads 原语查看 */
  const downloads = [];
  const downloadWaiters = new Set();
  let downloadSeq = 0;
  const hookedSessions = new WeakSet();
  function hookDownloads(ses) {
    if (!ses || hookedSessions.has(ses)) return;
    hookedSessions.add(ses);
    ses.on("will-download", (_event, item, wc) => {
      // 只接管面板网页的下载；主窗口自身（如设置页导出 JSON）保持系统默认行为
      const guest = wc && !wc.isDestroyed() ? panelGuest(wc.id) : null;
      if (!guest) return;
      const { dir } = effectiveDownloadDir();
      const savePath = uniquePath(dir, item.getFilename());
      try { item.setSavePath(savePath); } catch (err) { logErr("[browser-agent] 设置下载路径失败:", err && err.message); }
      const rec = {
        id: ++downloadSeq, wcId: guest.id, filename: path.basename(savePath), path: savePath,
        url: String(item.getURL() || "").slice(0, 300), mime: String(item.getMimeType() || ""),
        total: item.getTotalBytes() || 0, received: 0, state: "progressing", startedAt: Date.now(), endedAt: 0,
      };
      downloads.push(rec);
      if (downloads.length > DOWNLOADS_MAX) downloads.shift();
      const st = guests.get(guest.id);
      if (st) st.events.push({ type: "download", state: "started", filename: rec.filename, path: rec.path, url: rec.url, id: rec.id });
      emit({ type: "download", wcId: guest.id, state: "started", filename: rec.filename, path: rec.path });
      item.on("updated", () => { rec.received = item.getReceivedBytes() || rec.received; rec.total = item.getTotalBytes() || rec.total; });
      item.once("done", (_e, state) => {
        rec.state = state; // completed / cancelled / interrupted
        rec.received = item.getReceivedBytes() || rec.received;
        rec.endedAt = Date.now();
        const st2 = guests.get(guest.id);
        if (st2) st2.events.push({ type: "download", state, filename: rec.filename, path: rec.path, bytes: rec.received, id: rec.id });
        emit({ type: "download", wcId: guest.id, state, filename: rec.filename, path: rec.path, bytes: rec.received });
        for (const w of Array.from(downloadWaiters)) w();
      });
    });
  }

  /** downloads 原语：列出最近下载；wait_ms>0 时等到进行中的下载结束（或超时）再返回 */
  async function listDownloads(a) {
    const since = Number(a.since_id) || 0;
    const waitMs = clampInt(a.wait_ms, 0, 120000, 0);
    const deadline = Date.now() + waitMs;
    const pick = () => downloads.filter((d) => d.id > since);
    while (waitMs && pick().some((d) => d.state === "progressing") && Date.now() < deadline) {
      await new Promise((resolve) => {
        const done = () => { clearTimeout(t); downloadWaiters.delete(done); resolve(); };
        const t = setTimeout(done, Math.min(1000, Math.max(0, deadline - Date.now())));
        downloadWaiters.add(done);
      });
    }
    const limit = clampInt(a.limit, 1, DOWNLOADS_MAX, 10);
    return { dir: effectiveDownloadDir().dir, items: pick().slice(-limit).map((d) => ({ ...d })) };
  }

  /** 设置页：读取 / 修改 / 打开浏览器下载目录（dir 为空 = 恢复默认） */
  async function downloadDirOp(a) {
    const action = String(a.action || "get");
    if (action === "set") {
      const dir = String(a.dir || "").trim();
      if (dir && !writableDir(dir)) throw new Error("该目录不可写，请换一个目录");
      try { fs.writeFileSync(DOWNLOAD_PREF_FILE(), JSON.stringify({ dir }), "utf8"); } catch (err) { throw new Error("保存下载目录失败：" + ((err && err.message) || err)); }
    } else if (action === "open") {
      const { dir } = effectiveDownloadDir();
      const err = await shell.openPath(dir);
      if (err) throw new Error(err);
    }
    const eff = effectiveDownloadDir();
    return { ...eff, default_dir: defaultDownloadDir(), configured: readDownloadPref() };
  }

  function emit(payload) {
    const mw = getMainWindow();
    if (!mw || mw.isDestroyed()) return;
    try { mw.webContents.send("browserAgent:event", payload); } catch { /* 渲染层未就绪时丢弃 */ }
  }

  function panelGuest(wcId) {
    const mw = getMainWindow();
    const wc = webContents.fromId(Number(wcId));
    if (!wc || wc.isDestroyed() || !mw || mw.isDestroyed()) return null;
    if (wc.getType() !== "webview") return null;
    const host = wc.hostWebContents;
    if (!host || host.id !== mw.webContents.id) return null;
    return wc;
  }

  function cdp(st, method, params) {
    if (!st.attached) return Promise.reject(new Error("调试通道不可用"));
    return st.wc.debugger.sendCommand(method, params || {});
  }

  function onCdp(st, method, params) {
    if (method === "Network.requestWillBeSent") {
      const entry = {
        seq: ++st.seq, id: params.requestId, method: (params.request && params.request.method) || "GET",
        url: String((params.request && params.request.url) || "").slice(0, 500), type: params.type || "", status: null,
        startedAt: Date.now(), done: false,
      };
      st.net.push(entry);
      st.netById.set(params.requestId, entry);
      if (st.net.length > NET_MAX) {
        const old = st.net.shift();
        if (old) st.netById.delete(old.id);
      }
    } else if (method === "Network.responseReceived") {
      const e = st.netById.get(params.requestId);
      if (e && params.response) { e.status = params.response.status; e.mime = String(params.response.mimeType || "").slice(0, 80); }
    } else if (method === "Network.loadingFinished") {
      const e = st.netById.get(params.requestId);
      if (e) e.done = true;
    } else if (method === "Network.loadingFailed") {
      const e = st.netById.get(params.requestId);
      if (e) { e.status = "failed"; e.done = true; e.error = params.canceled ? "已取消" : String(params.errorText || "").slice(0, 200); }
    } else if (method === "Page.javascriptDialogOpening") {
      const dlg = { type: params.type, message: String(params.message || "").slice(0, 500), defaultPrompt: params.defaultPrompt || "" };
      if (Date.now() < st.aiUntil) {
        // AI 操作期间触发：按 AI 指定的策略应答（alert / 离开确认只能确认），结果随下一次动作回报
        const accept = dlg.type === "alert" || dlg.type === "beforeunload" ? true : st.accept;
        cdp(st, "Page.handleJavaScriptDialog", accept ? { accept: true, promptText: st.promptText || dlg.defaultPrompt } : { accept: false })
          .catch(() => {});
        st.events.push({ type: "dialog", dialog_type: dlg.type, message: dlg.message, accepted: accept });
      } else {
        st.pendingDialog = dlg;
        emit({ type: "dialog", wcId: st.wc.id, dialogType: dlg.type, message: dlg.message, defaultPrompt: dlg.defaultPrompt });
      }
    } else if (method === "Page.javascriptDialogClosed") {
      if (st.pendingDialog) { st.pendingDialog = null; emit({ type: "dialog-closed", wcId: st.wc.id }); }
    }
    // 动作后的稳定判定在等这些事件：请求开始 / 结束、网页弹窗都要立刻唤醒重判，而不是等下一轮轮询
    if (st.wakers.size && (method === "Network.requestWillBeSent" || method === "Network.loadingFinished"
      || method === "Network.loadingFailed" || method === "Page.javascriptDialogOpening")) {
      for (const w of Array.from(st.wakers)) w();
    }
  }

  function prepare(wcId) {
    const wc = panelGuest(wcId);
    if (!wc) return { ok: false, error: "不是面板内的网页" };
    const existing = guests.get(wc.id);
    if (existing) return { ok: true, debugger: existing.attached };
    const st = { wc, attached: false, net: [], netById: new Map(), seq: 0, aiUntil: 0, accept: true, promptText: "", events: [], pendingDialog: null, wakers: new Set(), needFrame: true };
    guests.set(wc.id, st);
    try { hookDownloads(wc.session); } catch (err) { logErr("[browser-agent] 挂载下载处理失败:", err && err.message); }
    // 新文档提交后、出第一帧前，输入事件会被丢弃（见 pumpFrame）
    wc.on("did-navigate", () => { st.needFrame = true; });
    // plan-20261018-operator-boost：AI 操作期间、应用窗口在后台时，新文档一就绪就先补一帧——
    // 否则紧接着的第一次点击会落在「新页面尚未出帧」的窗口里被丢弃，要靠送达校验失败后重派（多花约 0.5 s）
    wc.on("dom-ready", () => {
      if (!agentActive || !st.needFrame) return;
      const mw = getMainWindow();
      if (!mw || mw.isDestroyed() || (!mw.isMinimized() && mw.isVisible() && !isOccluded(mw))) return;
      pumpFrame(wc, st).then((ok) => { if (ok) st.needFrame = false; }, () => {});
    });
    try { wc.setBackgroundThrottling(false); } catch { /* 旧版本无此 API 时忽略 */ }
    try {
      wc.setWindowOpenHandler(({ url, disposition }) => {
        if (/^https?:|^file:|^about:blank/i.test(url || "")) emit({ type: "popup", wcId: wc.id, url, disposition });
        return { action: "deny" };
      });
    } catch { /* 忽略 */ }
    try {
      if (!wc.debugger.isAttached()) wc.debugger.attach("1.3");
      st.attached = true;
      wc.debugger.on("message", (_e, method, params) => {
        try { onCdp(st, method, params || {}); } catch (err) { logErr("[browser-agent] CDP 事件处理失败:", err && err.message); }
      });
      wc.debugger.on("detach", () => { st.attached = false; });
      cdp(st, "Page.enable").catch(() => {});
      cdp(st, "Network.enable", { maxTotalBufferSize: 20000000, maxResourceBufferSize: 4000000 }).catch(() => {});
    } catch (err) {
      // 用户已用其它调试客户端独占时会失败：降级为无网络日志、弹窗走原生路径
      st.attached = false;
      log("[browser-agent] 调试器附着失败（降级运行）:", err && err.message);
    }
    wc.once("destroyed", () => guests.delete(wc.id));
    return { ok: true, debugger: st.attached };
  }

  async function capture(wc, st, a) {
    const metrics = await withTimeout(
      wc.executeJavaScript("[innerWidth, innerHeight, document.documentElement.scrollWidth, document.documentElement.scrollHeight, scrollX, scrollY, location.href]", false),
      5000, "读取页面尺寸超时",
    );
    const [vw, vh, sw, sh] = metrics.slice(0, 4).map((n) => Math.max(1, Math.round(Number(n) || 1)));
    // 截图时的滚动位置、视口与地址：之后按截图坐标点击时据此补偿滚动、拒绝过期坐标
    const scroll = { x: Math.round(Number(metrics[4]) || 0), y: Math.round(Number(metrics[5]) || 0) };
    const pageUrl = String(metrics[6] || "");
    const quality = clampInt(a.quality, 30, 95, 72);
    const maxSide = clampInt(a.max_side, 320, 4096, 1280);
    let img = null;
    let cssW = vw;
    let cssH = vh;
    // plan-537-2251：mode 记录「实际走的截图路径」，不按请求参数反推——整页失败会静默退回视口，
    // 若仍按整页口径告诉模型坐标，点击会落到错误位置。truncated 标记整页被 12000px 上限截断。
    let mode = "viewport";
    let truncated = false;
    if (a.full_page && st && st.attached) {
      cssW = Math.min(Math.max(vw, sw), 4000);
      cssH = Math.min(sh, 12000);
      try {
        const res = await withTimeout(cdp(st, "Page.captureScreenshot", {
          format: "jpeg", quality, captureBeyondViewport: true,
          clip: { x: 0, y: 0, width: cssW, height: cssH, scale: 1 },
        }), 12000, "整页截图超时");
        img = nativeImage.createFromBuffer(Buffer.from(res.data, "base64"));
        mode = "full";
        truncated = sh > 12000;
      } catch (err) {
        log("[browser-agent] 整页截图失败，退回视口截图:", err && err.message);
        img = null;
        mode = "viewport";
        truncated = false;
        cssW = vw;
        cssH = vh;
      }
    }
    if (!img) {
      let rect;
      if (a.clip) {
        mode = "clip";
        const x = Math.max(0, Math.round(Number(a.clip.x) || 0));
        const y = Math.max(0, Math.round(Number(a.clip.y) || 0));
        rect = { x, y, width: Math.max(1, Math.min(vw - x, Math.round(Number(a.clip.width) || 1))), height: Math.max(1, Math.min(vh - y, Math.round(Number(a.clip.height) || 1))) };
        cssW = rect.width;
        cssH = rect.height;
      }
      img = await captureViewport(wc, st, rect, quality, rect ? null : { width: vw, height: vh });
    }
    if (!img || img.isEmpty()) throw new Error("截图为空（页面可能尚未加载完成）");
    // 输出按 CSS 像素（再按最长边限幅）：截图像素坐标与页面坐标一一对应，模型可直接拿来点击
    let tw = cssW;
    let th = cssH;
    const big = Math.max(tw, th);
    if (big > maxSide) { const r = maxSide / big; tw = Math.max(1, Math.round(tw * r)); th = Math.max(1, Math.round(th * r)); }
    const size = img.getSize();
    if (size.width !== tw || size.height !== th) img = img.resize({ width: tw, height: th, quality: "good" });
    const buf = img.toJPEG(quality);
    return {
      base64: buf.toString("base64"), mime: "image/jpeg", width: tw, height: th, css_width: cssW, css_height: cssH,
      scroll, viewport: { width: vw, height: vh }, page_url: pageUrl,
      // 实际截图口径：full=整页、viewport=视口、clip=指定区域；服务端据此决定坐标换算并给模型正确提示
      mode, truncated,
    };
  }

  /** 视口截图：先 capturePage（快）。主窗口被遮挡/最小化时合成器不出帧：临时关闭主窗口的后台节流让它出帧，
   *  截完立即恢复（只在截图这一刻多耗电）；仍不行再试 CDP 截图，失败就在几秒内给出可读原因。
   *  expect：不裁剪时的页面视口（CSS 像素），用来识别「拿到的图远小于视口」的残缺截图（plan-20261017-ux-overhaul 阶段 F）。 */
  async function captureViewport(wc, st, rect, quality, expect) {
    const mw = getMainWindow();
    const hostHidden = !!(mw && !mw.isDestroyed() && (mw.isMinimized() || !mw.isVisible() || isOccluded(mw)));
    if (hostHidden && Date.now() - lastHiddenFail < 30000 && hiddenFailWin === mw.id && mw.isMinimized()) {
      // 刚确认过最小化时截不到：不再空等，直接说明
      throw new Error("截图失败：应用窗口最小化时页面不渲染，请把窗口切到前台后重试");
    }
    // 被遮挡不一定能检测到（isOccluded 视平台而定），所以截图期间总是保持主窗口出帧；可见时这是空操作
    const restore = holdFrames();
    if (hostHidden) { try { wc.invalidate(); } catch { /* 忽略 */ } }
    try {
      try {
        // 不传 rect 即截 webview 自身的完整区域（与它在宿主里露出多少无关）
        const img = await withTimeout(wc.capturePage(rect, { stayHidden: true, stayAwake: true }), 2000, "__CAPTURE_TIMEOUT__");
        if (usableShot(img, rect, expect, wc)) return img;
        if (!st || !st.attached) {
          if (img && !img.isEmpty()) return img;
          throw new Error("截图为空：页面当前没有可用的渲染帧，请稍后重试");
        }
        log("[browser-agent] capturePage 结果残缺，改用 CDP 截图:", img && !img.isEmpty() ? JSON.stringify(img.getSize()) : "空图");
      } catch (err) {
        if (!st || !st.attached) {
          if (err && /截图为空/.test(err.message || "")) throw err;
          throw new Error("截图超时：应用窗口被遮挡或最小化时页面不渲染，请把窗口切到前台后重试");
        }
        log("[browser-agent] capturePage 未出帧，改用 CDP 截图:", err && err.message);
      }
      const params = { format: "jpeg", quality, optimizeForSpeed: true };
      if (rect) params.clip = { x: rect.x, y: rect.y, width: rect.width, height: rect.height, scale: 1 };
      // CDP 截图取 guest 自身的合成表面，与宿主里面板是否展开、webview 是否在前台无关；
      // 先 invalidate 让 guest 立即出一帧，避免等到页面下一次自发重绘
      try { wc.invalidate(); } catch { /* 忽略 */ }
      try {
        const res = await withTimeout(cdp(st, "Page.captureScreenshot", params), 3000, "CDP 截图超时");
        return nativeImage.createFromBuffer(Buffer.from(res.data, "base64"));
      } catch (err) {
        if (hostHidden && mw && !mw.isDestroyed()) { lastHiddenFail = Date.now(); hiddenFailWin = mw.id; }
        const why = (err && err.message) || "未知原因";
        // plan-20261018-operator-boost：窗口在前台时不再误报「被遮挡或最小化」（用户实测窗口可见时也看到这句）
        if (!hostHidden) throw new Error("截图失败：网页当前没有渲染帧（" + why + "），请稍后重试；持续失败时展开右侧面板再试");
        throw new Error("截图失败：应用窗口被遮挡或最小化时页面不渲染（" + why + "），请把窗口切到前台后重试");
      }
    } finally {
      restore();
    }
  }

  /** 逼 guest 出一帧（plan-20261017-hidden-input）。
   *  根因：Chromium 在新文档出第一帧之前丢弃发给它的输入事件——sendInputEvent 静默丢弃、CDP Input 等不到回执。
   *  宿主窗口被遮挡 / 最小化时合成器不出帧，隐藏期间导航（或刷新）后的页面就一直收不到点击和按键，
   *  直到用户把窗口切回前台。实测出过一帧之后输入恢复正常，直到下一次导航。
   *  做法同截图兜底：临时放开主窗口节流 + invalidate，再截一个 1×1 像素逼合成器出帧；不行再试 CDP 截图。 */
  async function pumpFrame(wc, st) {
    const restore = holdFrames();
    try {
      try { wc.invalidate(); } catch { /* 忽略 */ }
      try {
        const img = await withTimeout(wc.capturePage({ x: 0, y: 0, width: 1, height: 1 }, { stayHidden: true, stayAwake: true }), 1500, "__PUMP_TIMEOUT__");
        if (img && !img.isEmpty()) return true;
      } catch { /* 改走 CDP */ }
      if (!st || !st.attached) return false;
      try {
        await withTimeout(cdp(st, "Page.captureScreenshot", { format: "jpeg", quality: 30, optimizeForSpeed: true, clip: { x: 0, y: 0, width: 1, height: 1, scale: 1 } }), 1500, "出帧超时");
        return true;
      } catch { return false; }
    } finally {
      restore();
    }
  }

  /** capturePage 的结果是否可用：非空，且不裁剪时尺寸不小于页面视口的一半。
   *  停泊容器裁掉 webview 时会拿到 1×1 之类的残图——这种直接走 CDP 兜底。 */
  function usableShot(img, rect, expect, wc) {
    if (!img || img.isEmpty()) return false;
    const size = img.getSize();
    if (size.width < 4 || size.height < 4) return false;
    if (rect || !expect) return true;
    let zoom = 1;
    try { zoom = Number(wc.getZoomFactor()) || 1; } catch { zoom = 1; }
    return size.width >= expect.width * zoom * 0.5 && size.height >= expect.height * zoom * 0.5;
  }

  function isOccluded(mw) {
    try { return typeof mw.isOccluded === "function" ? mw.isOccluded() : false; } catch { return false; }
  }

  /** 动作起点之后页面脚本发起的请求：在途数、错误请求（含响应体摘要）与业务报错（200 但响应里带错误码）。 */
  async function netSince(st, a) {
    if (!st || !st.attached) return { available: false, pending: 0, errors: [] };
    const since = Number(a.since) || 0;
    const mine = st.net.filter((e) => e.seq > since && API_TYPES.has(e.type));
    const pending = mine.filter((e) => !e.done).length;
    const out = { available: true, pending, errors: [], seq: st.seq };
    if (!a.bodies) return out;
    const failed = mine.filter((e) => e.status === "failed" || (typeof e.status === "number" && e.status >= 400)).slice(-2);
    // 业务报错：HTTP 200 的 JSON 响应里 success=false 或 code 非 0/200 且带提示文字
    const okJson = mine.filter((e) => e.done && typeof e.status === "number" && e.status < 400 && /json/i.test(e.mime || "")).slice(-4);
    // 响应体一次并发取回（plan-20261017-ux-overhaul 阶段 F：原先逐个 await，最多 6 次串行 CDP 往返）
    const [failedBodies, okBodies] = await Promise.all([
      Promise.all(failed.map((e) => (e.status === "failed" ? "" : bodyHead(st, e)))),
      Promise.all(okJson.map((e) => bodyHead(st, e, 4000))),
    ]);
    failed.forEach((e, i) => {
      out.errors.push({ id: e.id, method: e.method, url: e.url, status: e.status, error: e.error || "", body: failedBodies[i] });
    });
    okJson.forEach((e, i) => {
      if (out.errors.length >= 3) return;
      const biz = bizError(okBodies[i]);
      if (biz) out.errors.push({ id: e.id, method: e.method, url: e.url, status: e.status, biz, body: okBodies[i].slice(0, BODY_MAX) });
    });
    return out;
  }

  /** 动作后的稳定判定（plan-20261017-ux-overhaul 阶段 F）：一次 IPC 内完成，取代宿主「每 30 ms 一轮、每轮两次往返」的轮询。
   *  三路先到先得：① 主框架开始跳转（立即返回 navigated，由宿主等新页面就绪）；② 稳定 = 本次动作发起的接口全部返回
   *  且 DOM 静默 quiet_ms（页面内 MutationObserver 计时，见 kit quiet）；③ 预算用尽。
   *  接口在途时不轮询：挂在 CDP 网络事件上等唤醒。collect=true 时顺带取回页面变化与接口错误，省掉收尾的两次往返。 */
  async function settle(wc, st, a) {
    const t0 = Date.now();
    const budget = clampInt(a.budget_ms, 0, 15000, 3000);
    const quietMs = clampInt(a.quiet_ms, 30, 1000, 100);
    const deadline = t0 + budget;
    const since = a.since === null || a.since === undefined ? null : Number(a.since) || 0;
    const netOn = since !== null && !!(st && st.attached);
    const pendingNow = () => (netOn ? st.net.filter((e) => e.seq > since && !e.done && API_TYPES.has(e.type)).length : 0);
    let navigated = false;
    const wakers = st ? st.wakers : new Set();
    let wakeNav = () => {};
    const navP = new Promise((resolve) => { wakeNav = resolve; });
    // Electron ≥ 25 只传 details（isMainFrame / isSameDocument）；旧签名为 (event, url, isInPlace, isMainFrame)
    const onNav = (d, _url, inPlace, mainFrame) => {
      const isMain = d && typeof d.isMainFrame === "boolean" ? d.isMainFrame : !!mainFrame;
      const same = d && typeof d.isSameDocument === "boolean" ? d.isSameDocument : !!inPlace;
      if (isMain && !same) { navigated = true; wakeNav(); for (const w of Array.from(wakers)) w(); }
    };
    wc.on("did-start-navigation", onNav);
    /** 等到任一网络 / 跳转事件或 ms 到期 */
    const nap = (ms) => new Promise((resolve) => {
      const done = () => { clearTimeout(t); wakers.delete(done); resolve(); };
      const t = setTimeout(done, Math.max(0, ms));
      wakers.add(done);
    });
    let state = null;
    let pending = 0;
    try {
      for (;;) {
        const left = deadline - Date.now();
        if (navigated || left <= 0) break;
        pending = pendingNow();
        if (pending > 0) { await nap(left); continue; }
        // DOM 静默：页面内一次往返等到静默或预算用尽；跳转发生时立即放弃等待
        const expr = "window.__ccKit ? window.__ccKit.call('quiet', " + JSON.stringify({ quiet_ms: quietMs, timeout_ms: Math.max(quietMs, left) }) + ") : null";
        const r = await Promise.race([
          withTimeout(wc.executeJavaScript(expr, false), left + 1000, "__QUIET_TIMEOUT__").catch(() => null),
          navP.then(() => null),
        ]);
        if (navigated) break;
        if (r && r.ok) state = r.value;
        pending = pendingNow();
        // 静默期间又发起了请求：等它返回后再判一次静默（报错提示通常随接口返回出现）
        if (pending > 0 && Date.now() < deadline) continue;
        break;
      }
    } finally {
      wc.removeListener("did-start-navigation", onNav);
    }
    const out = { navigated, pending: pendingNow(), state, waited: Date.now() - t0 };
    if (a.collect && !navigated) {
      const [chg, net] = await Promise.all([
        withTimeout(wc.executeJavaScript("window.__ccKit ? window.__ccKit.call('changes', {}) : null", false), 4000, "读取页面变化超时").catch(() => null),
        netOn ? netSince(st, { since, bodies: true }).catch(() => null) : Promise.resolve(null),
      ]);
      if (chg && chg.ok) out.changes = chg.value;
      if (net) out.net = net;
    }
    return out;
  }

  async function bodyHead(st, e, max) {
    try {
      const res = await withTimeout(cdp(st, "Network.getResponseBody", { requestId: e.id }), 1500, "读取响应体超时");
      if (res.base64Encoded) return "";
      return String(res.body || "").slice(0, max || BODY_MAX);
    } catch { return ""; }
  }

  function bizError(body) {
    let j;
    try { j = JSON.parse(body); } catch { return ""; }
    if (!j || typeof j !== "object" || Array.isArray(j)) return "";
    const msg = j.msg || j.message || j.errMsg || j.error_msg || j.errorMessage || (typeof j.error === "string" ? j.error : "");
    const code = j.code ?? j.errcode ?? j.errCode;
    const badCode = code !== undefined && code !== null && !["0", "200", "00000", "success", "ok"].includes(String(code).toLowerCase());
    if (j.success === false || j.ok === false || badCode) return String(msg || ("错误码 " + code)).slice(0, 200);
    return "";
  }

  function netLogs(st, a) {
    let entries = st ? st.net.slice() : [];
    if (a.only_errors) entries = entries.filter((e) => e.status === "failed" || (typeof e.status === "number" && e.status >= 400));
    const pat = String(a.pattern || "").toLowerCase();
    if (pat) entries = entries.filter((e) => (e.url || "").toLowerCase().includes(pat));
    const total = entries.length;
    const limit = clampInt(a.limit, 1, 300, 50);
    entries = entries.slice(-limit);
    if (a.clear && st) { st.net = []; st.netById.clear(); }
    return { entries, total, available: !!(st && st.attached) };
  }

  async function exec(wcId, op, a) {
    if (op === "clipboard") {
      if (a.action === "write") { clipboard.writeText(String(a.text || "")); return { text: String(a.text || "") }; }
      return { text: clipboard.readText() };
    }
    if (op === "sleep") {
      // 宿主页面被判为 hidden 时自身定时器被节流到秒级；主进程计时不受影响（plan-20261015-browser-perf）
      await new Promise((r) => setTimeout(r, clampInt(a.ms, 0, 2000, 0)));
      return {};
    }
    if (op === "downloadDir") return downloadDirOp(a);
    if (op === "downloads") return listDownloads(a);
    const wc = panelGuest(wcId);
    if (!wc) throw new Error("目标网页已关闭或不属于浏览器面板");
    const st = guests.get(wc.id) || (prepare(wc.id).ok ? guests.get(wc.id) : null);
    switch (op) {
      case "aiWindow": {
        // AI 动作窗口：期间网页弹窗按 AI 策略自动应答。若此刻还挂着一个等用户处理的弹窗，
        // AI 继续操作会被阻塞——按「取消」收掉（不替用户确认任何东西）并回报。
        if (!st) return {};
        st.aiUntil = Date.now() + clampInt(a.ms, 500, 60000, 4000);
        st.accept = a.accept !== false;
        st.promptText = String(a.prompt_text || "");
        if (st.pendingDialog) {
          const d = st.pendingDialog;
          st.pendingDialog = null;
          await cdp(st, "Page.handleJavaScriptDialog", { accept: d.type === "alert" }).catch(() => {});
          st.events.push({ type: "dialog", dialog_type: d.type, message: d.message, accepted: d.type === "alert" });
          emit({ type: "dialog-closed", wcId: wc.id });
        }
        // 动作起点：之后发起的请求才算本次动作触发的
        return { net_seq: st.seq };
      }
      case "netSince":
        return netSince(st, a);
      case "settle":
        return settle(wc, st, a);
      case "zoom":
        return { factor: wc.getZoomFactor() };
      case "events": {
        if (!st) return { events: [] };
        const events = st.events;
        st.events = [];
        return { events };
      }
      case "dialogReply": {
        if (!st || !st.pendingDialog) return {};
        st.pendingDialog = null;
        const accept = a.accept !== false;
        await cdp(st, "Page.handleJavaScriptDialog", accept ? { accept: true, promptText: String(a.prompt_text || "") } : { accept: false });
        return {};
      }
      case "capture":
        return capture(wc, st, a);
      case "network":
        return netLogs(st, a);
      case "netBody": {
        if (!st || !st.attached) throw new Error("网络记录不可用（调试通道未连接）");
        const res = await withTimeout(cdp(st, "Network.getResponseBody", { requestId: String(a.request_id || "") }), 8000, "读取响应体超时");
        return { body: String(res.body || "").slice(0, 400000), base64: !!res.base64Encoded, mime: (st.netById.get(String(a.request_id)) || {}).mime || "" };
      }
      case "ensureFrame": {
        // 宿主在后台时、输入动作之前调用：页面导航后还没出过帧才补一帧；force 用于点击校验失败后的重试
        if (!st) return { pumped: false };
        if (!a.force && !st.needFrame) return { pumped: false };
        if (!a.force) {
          // 窗口在前台时合成器自然出帧，不必补（plan-20261018-operator-boost：宿主不再自行判断前后台）
          const mw = getMainWindow();
          if (mw && !mw.isDestroyed() && !mw.isMinimized() && mw.isVisible() && !isOccluded(mw)) return { pumped: false };
        }
        const ok = await pumpFrame(wc, st);
        if (ok) st.needFrame = false;
        return { pumped: ok };
      }
      case "mouseCdp": {
        // 点击的备用通道（plan-20261017-hidden-input）：宿主校验到 sendInputEvent 的点击没有送达页面时改走 CDP。
        // 坐标是页面 CSS 像素（实测 CDP 不需要像 sendInputEvent 那样乘网页缩放）
        if (!st || !st.attached) throw new Error("调试通道未连接");
        const x = Number(a.x) || 0;
        const y = Number(a.y) || 0;
        const button = a.button === "right" ? "right" : a.button === "middle" ? "middle" : "left";
        const count = clampInt(a.count, 1, 3, 1);
        const mods = Array.isArray(a.modifiers) ? a.modifiers.map((m) => String(m).toLowerCase()) : [];
        const modifiers = (mods.includes("alt") ? 1 : 0) | (mods.includes("control") ? 2 : 0) | (mods.includes("meta") ? 4 : 0) | (mods.includes("shift") ? 8 : 0);
        const send = (type, extra) => withTimeout(cdp(st, "Input.dispatchMouseEvent", { type, x, y, modifiers, ...extra }), 3000, "派发鼠标事件超时");
        await send("mouseMoved", { button: "none" });
        for (let i = 1; i <= count; i++) {
          await send("mousePressed", { button, clickCount: i });
          await send("mouseReleased", { button, clickCount: i });
        }
        return {};
      }
      case "setZoom": {
        const z = Number(a.factor);
        if (Number.isFinite(z) && z > 0.2 && z < 5) wc.setZoomFactor(z);
        return { factor: wc.getZoomFactor() };
      }
      default:
        throw new Error("未知的浏览器操作：" + op);
    }
  }

  ipcMain.handle("browserAgent:prepare", (_event, wcId) => {
    try { return prepare(wcId); } catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
  });
  ipcMain.handle("browserAgent:exec", async (_event, wcId, op, args) => {
    // AI 正在操作网页：动作窗口续期（期间主窗口不节流，后台 / 最小化时也照常出帧、计时）
    if (!PASSIVE_OPS.has(String(op || ""))) markAgentActive();
    try {
      return { ok: true, result: await exec(wcId, String(op || ""), args || {}) };
    } catch (err) {
      return { ok: false, error: (err && err.message) || String(err) };
    }
  });
};
