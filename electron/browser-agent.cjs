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
// plan-693-2868 P0-4：微应用方法调用（op = microappCall）
//   微应用界面是右侧面板里另一个 <webview>（partition = persist:microapp），没有「浏览器标签」概念，
//   因此这条 op 不走 panelGuest(wcId)，而是按 appId + version 在 persist:microapp 分区里找它的 guest，
//   在 guest 里执行 window.__MICROAPP_METHODS__[method](args) 并把返回值 JSON 化后带回。
//   之所以放在这个模块：它和浏览器面板共用同一条「渲染层 → 主进程」的 IPC 与同一套 guest 校验口径，
//   另起一条通道只会多一处「谁有权限操作 guest」的分叉。
//
// 只接受「宿主是主窗口」的 webview guest，任何其它 webContents 一律拒绝。
//
// plan-693-2868 · docs/app1/11-host-bridge-protocol.md（宿主桥 v2）：
//   微应用页面里的 window.microapp.invoke 走「页面 preload →（sendToHost）主进程 → 渲染层 → 后端」
//   三跳。本模块负责中间那一跳：记录 uid → guest 映射（§6 的实例寻址）、把上行请求转给渲染层、
//   把下行事件（含 stream.log 的 100 ms / 200 行聚批，§5）按 uid 投递给正确的 guest。
//   渲染层只需要认识三个 IPC 频道（见下方 MICROAPP_REQ/REPLY/PUSH），别的都是主进程内部的事。
const { app, ipcMain, webContents, session, clipboard, nativeImage, shell } = require("electron");
const fs = require("fs");
const path = require("path");
const os = require("os");

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

// 微应用 webview 的独立分区（与 main.cjs 的 MICROAPP_PARTITION 同值，见 main.cjs 的 app:// 协议注册处）。
// 不过滤这一条会连浏览器面板的 webview 一起扫进来——它们同样挂在主窗口上、同样是 webview。
const MICROAPP_PARTITION = "persist:microapp";

// ── 宿主桥 v2 的频道与常量（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §4-§6）──
// 渲染层只认下面三个频道；多一个都要同步改前端与协议文档，所以名字集中在这里。
const MICROAPP_REQ_CHANNEL = "microapp:hostRequest"; // 主进程 → 渲染层：上行请求
const MICROAPP_REPLY_CHANNEL = "microapp:hostReply"; // 渲染层 → 主进程：上行结果
const MICROAPP_PUSH_CHANNEL = "microapp:hostPush";   // 渲染层/后端 → 主进程：下行推送
// 以下两条只在「主进程 ↔ guest preload」之间走，渲染层不参与（与协议 §4/§5 报文一一对应）。
// 名字必须与 electron/microapp-preload.cjs 里的常量一致。
const MICROAPP_INVOKE_CHANNEL = "microapp:invoke";       // guest preload → 主进程（sendToHost）
const MICROAPP_GUEST_REPLY_CHANNEL = "microapp:reply";   // 主进程 → guest preload（回包）
const MICROAPP_GUEST_EVENT_CHANNEL = "microapp:event";   // 主进程 → guest preload（下行事件）
// 上行请求在宿主的等待上限：与 preload 的 30 s 对齐。超时只负责回收 pending 并给页面一条明确失败，
// 不中断已经发到后端的工作。
const MICROAPP_HOST_TIMEOUT_MS = 30000;
// 下行 stream.log 的聚批（协议 §5）：100 ms 或 200 行，先到者发。
const MICROAPP_BATCH_MS = 100;
const MICROAPP_BATCH_LINES = 200;
// op 名形状（exec.start / fs.read / workspace.info）：只做格式校验，具体 op 由渲染层与后端决定。
// 页面是无审核来源，这条是挡「乱传字段 / 超长字符串」的粗筛，不是权限控制（协议 §2：能力完全放开）。
const MICROAPP_OP_RE = /^[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)*$/;
// 宿主桥 op 的参数错误前缀。只在本进程内的调用方（渲染层 / AI 工具）看到，
// 不进 MICROAPP_ERR：那套前缀与 server/app/orchestration/tools/microapp.py 的 _BRIDGE_ERROR_HINTS
// 一一对应，新增前缀需要两边同步上线，这里刻意不扩。
const MICROAPP_PUSH_ERR = "microapp-host-push-invalid";
// 单次方法调用的等待上限：一次往返正常 < 20 ms，余量全部留给方法自己的计算（画布渲染 / 大表格排序）。
// 超时只放弃等待，**不中断**页面里已经开始的方法，所以超时后界面状态未知，文案要求调用方先核对再重试。
const MICROAPP_CALL_TIMEOUT_MS = 15000;
// 方法名 / appId / version 的合法性在这里再挡一次：它们会被拼进 executeJavaScript 的源码，
// 字符串拼接一旦失控就是注入面。前端（服务端工具）已按同样口径校验过，这里是最后一道。
const MICROAPP_ID_RE = /^[a-z0-9][a-z0-9._-]{1,62}$/;
const MICROAPP_VERSION_RE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;
const MICROAPP_METHOD_RE = /^[A-Za-z_$][A-Za-z0-9_$]{0,63}$/;
// 失败原因前缀：服务端（server/app/orchestration/tools/microapp.py 的 _BRIDGE_ERROR_HINTS）按前缀
// 给出可执行的 hint，改名字必须两边一起改，否则错误会退化成「未归类失败」。
// 传成「错误串 + 冒号 + 细节」，冒号后的细节会原样进模型看到的 error。
const MICROAPP_ERR = {
  GUEST_MISSING: "microapp-guest-missing",
  GUEST_LOADING: "microapp-guest-loading",
  BRIDGE_MISSING: "microapp-bridge-missing",
  METHOD_MISSING: "microapp-method-missing",
  METHOD_INVALID: "microapp-method-invalid",
  NOT_SERIALIZABLE: "microapp-not-serializable",
  THREW: "microapp-threw",
  TIMEOUT: "microapp-timeout",
};
/** uid 白名单：渲染层的标签 uid 形如 "microapp#3"（client/src/store/panel.ts 的 nextUid）。
 *  它会被当作 Map 的 key 上级地址，只放行这些字符并限长；不合法就当没给（空串）。 */
const MICROAPP_UID_RE = /^[A-Za-z0-9._:#-]{1,80}$/;
function sanitizeUid(v) {
  const s = String(v === undefined || v === null ? "" : v).trim();
  return MICROAPP_UID_RE.test(s) ? s : "";
}

// 不算「AI 在操作网页」的原语：设置页读写下载目录、剪贴板、纯等待、读下载记录
const PASSIVE_OPS = new Set(["downloadDir", "downloads", "clipboard", "sleep"]);
const DOWNLOAD_PREF_FILE = () => path.join(app.getPath("userData"), "browser-download-pref.json");
const DOWNLOADS_MAX = 50;

/** 默认下载目录：用户目录下（plan-693-2868 D23）。
 *  此前打包态落在安装目录，升级时随 $INSTDIR 一起被删；与宠物同一处缺陷，同一轮修掉。
 */
function defaultDownloadDir() {
  return path.join(os.homedir(), ".chatcoder", "downloads");
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

  // ══ 宿主桥 v2（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §4-§6）══
  //
  // 三跳链路（渲染层只参与中间一跳，且只认三个频道）：
  //   上行：页面 window.microapp.invoke → preload(sendToHost "microapp:invoke") → 本模块
  //         →("microapp:hostRequest") 渲染层 → 后端 →("microapp:hostReply") 本模块
  //         → guest("microapp:reply") → preload 按 reqId resolve
  //   下行：渲染层/后端 →("microapp:hostPush") 本模块 → guest("microapp:event") → 页面 on()
  //
  // 为什么寻址要靠 uid：同 appId 可以开多个标签（协议 §6），只按 appId+version 取第一个会在
  // 多标签时把请求/输出打到别的界面上去。uid 由渲染层写进 webview src 的 query，页面 preload 读到后
  // 随每个上行请求带上；attach 时也从 URL 解析一次，用于「页面还没调过 invoke 就收到下行推送」的场景。

  /** uid → { wc, uid, appId, version, src, lastActiveAt } */
  const microappGuests = new Map();
  /** wc.id → 同一份实例记录（老客户端没带 uid / 只给 wcId 时兜底） */
  const microappByWc = new Map();
  /** `${uid}|${reqId}` → { rec, uid, reqId, timer, startedAt }，等渲染层回包的上行请求 */
  const hostPending = new Map();
  /** `${wc.id}|${streamId}` → 已发出的最大 seq（协议 §5：同一 streamId 内单调递增） */
  const streamSeq = new Map();
  /** `${wc.id}|${streamId}|${stream}` → { lines, timer, … }，等聚批的 stream.log */
  const microappBatches = new Map();
  /** 已挂过 did-attach-webview 的主窗口 webContents.id（幂等用） */
  let hookedMainWcId = 0;
  /** 已挂过 did-attach-webview 的宿主 webContents.id（主窗口 + 微应用独立窗口，幂等用） */
  const hookedHostWcIds = new Set();
  /** 微应用**独立窗口**的渲染层 webContents.id（main.cjs 建窗时登记）。
   *
   *  为什么需要这份名单：微应用界面除了挂在主窗口的右侧面板里，还可以在独立窗口里打开
   *  （拓展页 / 右侧面板「在新窗口打开」）。这些窗口里的 webview 同样是微应用 guest，
   *  但 `hostWebContents` 指向的是那个独立窗口而不是主窗口——下面的扫描/定位若只认主窗口，
   *  AI 的 microapp_call / microappProbe 就会报「界面没打开」。
   *  注意：上行请求仍然回**主窗口**渲染层（`microappHost` 单例在那边持有 uid → sessionId 绑定），
   *  所以这里只放宽「guest 的宿主是谁」的判定，不改转发目标。 */
  const microappHostWcIds = new Set();

  /** 与 main.cjs 的 will-attach-webview 判定**必须逐字一致**：只认 app://microapp/ 前缀，区分大小写。 */
  function isMicroappSrc(src) {
    return String(src || "").startsWith("app://microapp/");
  }

  /** 从 app://microapp/<appId>/<version>/<path...>?uid=… 里取身份；取不到就给空串（不猜）。 */
  function parseMicroappSrc(src) {
    const out = { appId: "", version: "", uid: "" };
    let u;
    try { u = new URL(String(src || "")); } catch { return out; }
    const seg = String(u.pathname || "").split("/").filter(Boolean);
    if (seg.length >= 1 && MICROAPP_ID_RE.test(seg[0])) out.appId = seg[0];
    if (seg.length >= 2 && MICROAPP_VERSION_RE.test(seg[1])) out.version = seg[1];
    try { out.uid = sanitizeUid(u.searchParams.get("uid")); } catch { out.uid = ""; }
    // 客户端 uid 形如 "microapp#3"：渲染层若忘了 encodeURIComponent，"#" 会变成 fragment 起点，
    // uid 被截断成 "microapp"，同一 appId 的多个标签在宿主侧就撞成一个。把裸的 "#数字" 补回去
    // （与 electron/microapp-preload.cjs 的 readInstance 同一条规则）。
    const hash = String(u.hash || "");
    if (out.uid && out.uid.indexOf("#") === -1 && /^#\d{1,6}$/.test(hash)) out.uid = sanitizeUid(out.uid + hash);
    return out;
  }

  /** 把 uid 绑到某条实例记录上（uid 可能来自 URL，也可能来自页面自己的上行请求）。 */
  function bindMicroappUid(rec, uid) {
    const u = sanitizeUid(uid);
    if (!rec || !u) return;
    if (rec.uid && rec.uid !== u && microappGuests.get(rec.uid) === rec) microappGuests.delete(rec.uid);
    rec.uid = u;
    microappGuests.set(u, rec);
  }

  /** 向 guest 页面投递一条 IPC（webContents.send 是异步投递，不等页面渲染完成）。 */
  function sendToGuest(rec, channel, payload) {
    const wc = rec && rec.wc;
    if (!wc || wc.isDestroyed()) return false;
    try { wc.send(channel, payload); return true; }
    catch (err) {
      logErr("[microapp-host] 向微应用投递失败(" + channel + "):", (err && err.message) || err);
      return false;
    }
  }

  /** 记录一条微应用 guest（幂等）。非 app://microapp/ 的 guest 一律不碰——浏览器面板不经过这里。 */
  function registerMicroappGuest(guestWc, params) {
    if (!guestWc || guestWc.isDestroyed()) return null;
    const src = String((params && params.src) || "");
    const prev = microappByWc.get(guestWc.id);
    if (prev) {
      // 重复登记（例如补挂）：只更新身份，不重复挂监听，否则一次上行会被转发两遍
      const again = parseMicroappSrc(src || prev.src || "");
      if (again.appId) prev.appId = again.appId;
      if (again.version) prev.version = again.version;
      if (again.uid) bindMicroappUid(prev, again.uid);
      if (src) prev.src = src;
      return prev;
    }
    if (!isMicroappSrc(src)) return null;

    const meta = parseMicroappSrc(src);
    const rec = { wc: guestWc, uid: "", appId: meta.appId, version: meta.version, src, lastActiveAt: Date.now() };
    microappByWc.set(guestWc.id, rec);
    bindMicroappUid(rec, meta.uid);

    // 上行第一跳：preload 的 ipcRenderer.sendToHost 会落到 guest 的 ipc-message
    guestWc.on("ipc-message", (_event, channel, ...args) => {
      if (channel !== MICROAPP_INVOKE_CHANNEL) return;
      rec.lastActiveAt = Date.now();
      onMicroappGuestInvoke(rec, args.length ? args[0] : null);
    });
    // 身份跟着 URL 走：webview 内部导航后重解析，避免 uid 绑到错的实例上
    guestWc.on("did-navigate", (_event, url) => {
      rec.lastActiveAt = Date.now();
      if (!isMicroappSrc(url)) return;
      const again = parseMicroappSrc(url);
      rec.src = String(url);
      if (again.appId) rec.appId = again.appId;
      if (again.version) rec.version = again.version;
      if (again.uid) bindMicroappUid(rec, again.uid);
    });
    // 微应用只允许在 app://microapp/ 内导航：条件注入 preload 之后，若页面把自己导航到 http(s)，
    // 那个 preload 会跟着 webview 留在新文档里（webPreferences 属于 webview，不随导航重置）。
    // 这里直接掐断。只作用于微应用 guest——浏览器面板的 webview 根本进不了这条分支。
    guestWc.on("will-navigate", (event, url) => {
      if (isMicroappSrc(url)) return;
      try { event.preventDefault(); } catch { /* 事件对象不可用时忽略 */ }
      logErr("[microapp-host] 拒绝微应用导航到非 app://microapp/ 地址:", String(url || "").slice(0, 200));
    });
    guestWc.once("destroyed", () => forgetMicroappGuest(rec));
    return rec;
  }

  /** attach 时 src 还是 about:blank（React 先建元素后设 src）：只对 persist:microapp 分区的 guest
   *  留一个「导航后补登记」。浏览器面板的 guest 落在默认分区，连监听都不会挂上。 */
  function deferTrackMicroappGuest(guestWc) {
    if (!guestWc || guestWc.isDestroyed()) return;
    let samePartition = false;
    try { samePartition = guestWc.session === session.fromPartition(MICROAPP_PARTITION); } catch { samePartition = false; }
    if (!samePartition) return;
    const onNavigate = () => {
      const url = String(guestWc.getURL() || "");
      if (!isMicroappSrc(url)) return;
      guestWc.removeListener("did-navigate", onNavigate);
      registerMicroappGuest(guestWc, { src: url });
    };
    guestWc.on("did-navigate", onNavigate);
    guestWc.once("destroyed", () => guestWc.removeListener("did-navigate", onNavigate));
  }

  /** guest 被关掉：清 uid 绑定、它名下的等待请求、聚批残留与 seq 计数（防 webContents.id 复用时串台）。 */
  function forgetMicroappGuest(rec) {
    if (!rec) return;
    if (rec.wc && microappByWc.get(rec.wc.id) === rec) microappByWc.delete(rec.wc.id);
    if (rec.uid && microappGuests.get(rec.uid) === rec) microappGuests.delete(rec.uid);
    for (const [key, entry] of Array.from(hostPending)) {
      if (entry.rec !== rec) continue;
      if (entry.timer) clearTimeout(entry.timer);
      hostPending.delete(key);
    }
    for (const [key, b] of Array.from(microappBatches)) {
      if (b.rec !== rec) continue;
      if (b.timer) clearTimeout(b.timer);
      microappBatches.delete(key);
    }
    if (rec.wc) {
      const prefix = rec.wc.id + "|";
      for (const key of Array.from(streamSeq.keys())) if (key.startsWith(prefix)) streamSeq.delete(key);
    }
  }

  /**
   * 在一个宿主 webContents 上挂 did-attach-webview，记录 uid → guest（协议 §6 / §9-1）。
   * 幂等（按 webContents.id 去重）；宿主窗口重建后由 main.cjs 在建窗时重新调用。
   * 只注册监听，不改变 webview 的任何行为——preload 注入在 main.cjs 的 will-attach-webview 里。
   */
  function hookHostWc(wc) {
    if (!wc || wc.isDestroyed()) return false;
    if (hookedHostWcIds.has(wc.id)) return true;
    hookedHostWcIds.add(wc.id);
    wc.on("did-attach-webview", (_event, guestWc, params) => {
      try {
        const rec = registerMicroappGuest(guestWc, params);
        if (!rec) deferTrackMicroappGuest(guestWc);
      } catch (err) {
        logErr("[microapp-host] 记录微应用 guest 失败:", (err && err.message) || err);
      }
    });
    wc.once("destroyed", () => {
      hookedHostWcIds.delete(wc.id);
      // 独立窗口关掉后把登记也摘掉，避免 webContents.id 复用后把新窗口误判成微应用宿主
      microappHostWcIds.delete(wc.id);
      if (hookedMainWcId === wc.id) hookedMainWcId = 0;
      flushAllMicroappBatches(); // 宿主没了，聚批里的日志没有收件人
    });
    return true;
  }

  /** 主窗口：除挂监听外还记下它的 id（`scanMicroappByUid` 等仍以它为「默认宿主」）。 */
  function hookMainWindow(wc) {
    if (!wc || wc.isDestroyed()) return false;
    if (hookedMainWcId === wc.id) return true;
    hookedMainWcId = wc.id;
    return hookHostWc(wc);
  }

  /** 微应用**独立窗口**：登记为宿主（它的 webview 也算微应用 guest），并挂 did-attach-webview。 */
  function hookMicroappHostWindow(wc) {
    if (!wc || wc.isDestroyed()) return false;
    microappHostWcIds.add(wc.id);
    return hookHostWc(wc);
  }

  /** 这个 webContents 是不是「可以承载微应用 guest 的宿主」（主窗口或独立微应用窗口）。 */
  function isMicroappHostWc(wc) {
    if (!wc || wc.isDestroyed()) return false;
    const mw = getMainWindow();
    if (mw && !mw.isDestroyed() && wc.id === mw.webContents.id) return true;
    return microappHostWcIds.has(wc.id);
  }

  /** 主窗口已存在就把钩子补上（正常路径是 main.cjs 建窗后显式调用 hookMainWindow）。 */
  function ensureMainHooked() {
    const mw = getMainWindow();
    if (!mw || mw.isDestroyed()) return false;
    return hookMainWindow(mw.webContents);
  }

  /** map 未命中时按 URL 扫一遍（hook 挂晚 / 主窗口重建后仍能定位到实例）。 */
  function scanMicroappByUid(uid) {
    const mw = getMainWindow();
    if (!mw || mw.isDestroyed()) return null;
    let microSession;
    try { microSession = session.fromPartition(MICROAPP_PARTITION); } catch { return null; }
    for (const wc of webContents.getAllWebContents()) {
      if (!wc || wc.isDestroyed() || wc.getType() !== "webview") continue;
      // 宿主可以是主窗口，也可以是微应用独立窗口（见 hookMicroappHostWindow）
      if (!isMicroappHostWc(wc.hostWebContents)) continue;
      if (wc.session !== microSession) continue;
      const url = String(wc.getURL() || "");
      if (!isMicroappSrc(url)) continue;
      if (parseMicroappSrc(url).uid !== uid) continue;
      return microappByWc.get(wc.id) || registerMicroappGuest(wc, { src: url });
    }
    return null;
  }

  /** 按 uid（优先）或 wcId 找实例记录；找不到返回 null。 */
  function findMicroappInstance(uid, wcId) {
    const wantUid = sanitizeUid(uid);
    let rec = wantUid ? microappGuests.get(wantUid) : null;
    if (rec && (!rec.wc || rec.wc.isDestroyed())) { forgetMicroappGuest(rec); rec = null; }
    if (!rec && wcId) {
      const byWc = microappByWc.get(Number(wcId));
      if (byWc && byWc.wc && !byWc.wc.isDestroyed()) rec = byWc;
    }
    if (!rec && wantUid) rec = scanMicroappByUid(wantUid);
    return rec || null;
  }

  /** 上行第二跳：把 guest 的请求转给渲染层（协议 §4）。 */
  function onMicroappGuestInvoke(rec, msg) {
    const wc = rec && rec.wc;
    if (!wc || wc.isDestroyed()) return;
    const m = msg && typeof msg === "object" ? msg : {};
    const reqId = String(m.reqId || "");
    const op = String(m.op || "");
    const args = m.args && typeof m.args === "object" ? m.args : {};

    // 页面自带的身份最可信（协议 §6：发起方自己带 uid），空则回落到 attach 时从 URL 解析的
    if (m.uid) bindMicroappUid(rec, m.uid);
    if (m.appId && !rec.appId && MICROAPP_ID_RE.test(String(m.appId))) rec.appId = String(m.appId);
    if (m.version && !rec.version && MICROAPP_VERSION_RE.test(String(m.version))) rec.version = String(m.version);

    const fail = (reason, message) => sendToGuest(rec, MICROAPP_GUEST_REPLY_CHANNEL, { kind: "reply", reqId, ok: false, reason, message });

    // 只接受当前 URL 仍在 app://microapp/ 的页面：防「微应用把自己导航到 http 后残留的 preload」越权
    if (!isMicroappSrc(wc.getURL())) { fail("origin-rejected", "宿主只接受 app://microapp/ 页面发起的请求"); return; }
    if (!reqId || reqId.length > 128) { fail("req-invalid", "reqId 缺失或过长"); return; }
    if (!MICROAPP_OP_RE.test(op)) { fail("op-invalid", "op 必须是形如 exec.start 的操作名（收到：" + op.slice(0, 40) + "）"); return; }

    const mw = getMainWindow();
    if (!mw || mw.isDestroyed()) { fail("host-unavailable", "宿主窗口不可用"); return; }

    const key = rec.uid + "|" + reqId;
    if (hostPending.has(key)) { fail("duplicate-request", "同一个 reqId 已经有一个请求在等宿主回包"); return; }

    const entry = { rec, uid: rec.uid, reqId, startedAt: Date.now(), timer: null };
    // 30 s 兜底：preload 自己在 30 s 也会 reject；这里同时回收 pending，
    // 并给页面一条明确的失败（而不是让它等到自己超时，排查时看不出是宿主没回）
    entry.timer = setTimeout(() => {
      hostPending.delete(key);
      sendToGuest(rec, MICROAPP_GUEST_REPLY_CHANNEL, { kind: "reply", reqId, ok: false, reason: "host-timeout", message: "宿主 30 秒内没有返回结果" });
    }, MICROAPP_HOST_TIMEOUT_MS);
    hostPending.set(key, entry);

    try {
      mw.webContents.send(MICROAPP_REQ_CHANNEL, {
        uid: rec.uid,
        appId: rec.appId,
        version: rec.version,
        wcId: wc.id,
        reqId,
        op,
        args,
      });
    } catch (err) {
      clearTimeout(entry.timer);
      hostPending.delete(key);
      fail("host-unavailable", "请求转发到渲染层失败：" + ((err && err.message) || err));
    }
  }

  /** 上行第三跳：渲染层把结果交回主进程，原样回给发起方页面（协议 §4）。 */
  function microappHostReply(payload, senderWc) {
    const mw = getMainWindow();
    if (!mw || mw.isDestroyed()) return { ok: false, error: "主窗口不可用" };
    // 只认主窗口渲染层：微应用 guest 发不出这条（它没有 ipcRenderer，宿主也不是它）
    if (senderWc && senderWc.id !== mw.webContents.id) return { ok: false, error: "只有主窗口渲染层可以回包" };
    const p = payload && typeof payload === "object" ? payload : {};
    const reqId = String(p.reqId || "");
    if (!reqId) return { ok: false, error: "缺少 reqId" };

    const uid = sanitizeUid(p.uid);
    let key = uid ? uid + "|" + reqId : "";
    let entry = key ? hostPending.get(key) : null;
    if (!entry) {
      // 渲染层没带 uid（或带错了）：按 reqId 唯一匹配。preload 的 reqId 含自增+随机后缀，
      // 跨实例撞车概率可忽略；真撞了也会先在 onMicroappGuestInvoke 里被 duplicate-request 挡住。
      for (const [k, v] of hostPending) {
        if (k.slice(k.lastIndexOf("|") + 1) === reqId) { key = k; entry = v; break; }
      }
    }
    if (!entry) return { ok: false, error: "没有等待回包的请求（可能已超时，或 uid+reqId 不匹配）" };

    hostPending.delete(key);
    if (entry.timer) clearTimeout(entry.timer);
    const body = p.ok
      ? { kind: "reply", reqId: entry.reqId, ok: true, result: p.result === undefined ? null : p.result }
      : { kind: "reply", reqId: entry.reqId, ok: false, reason: String(p.reason || "failed"), message: String(p.message || "") };
    const delivered = sendToGuest(entry.rec, MICROAPP_GUEST_REPLY_CHANNEL, body);
    return {
      ok: true,
      delivered,
      uid: entry.uid,
      appId: entry.rec ? entry.rec.appId : "",
      version: entry.rec ? entry.rec.version : "",
    };
  }

  function microappBatchKey(rec, streamId, stream) {
    return rec.wc.id + "|" + streamId + "|" + stream;
  }

  function ensureMicroappBatch(rec, streamId, stream) {
    const key = microappBatchKey(rec, streamId, stream);
    let b = microappBatches.get(key);
    if (!b) { b = { key, rec, streamId, stream, lines: [], timer: null }; microappBatches.set(key, b); }
    return b;
  }

  /** 协议 §5：100 ms 或 200 行（先到者）聚成一批。seq 由主进程按批重编号，保证同 streamId 单调递增。 */
  function enqueueMicroappLogBatch(rec, payload) {
    const streamId = String(payload.streamId || "");
    const stream = String(payload.stream || "stdout");
    const incoming = Array.isArray(payload.lines)
      ? payload.lines
      : (payload.line === undefined || payload.line === null ? [] : [payload.line]);
    let flushed = 0;
    for (const line of incoming) {
      const b0 = ensureMicroappBatch(rec, streamId, stream);
      b0.lines.push(String(line));
      if (b0.lines.length >= MICROAPP_BATCH_LINES) { flushMicroappBatch(b0.key); flushed++; }
    }
    const b = ensureMicroappBatch(rec, streamId, stream);
    if (!b.lines.length) {
      microappBatches.delete(b.key); // 刚好在 200 行整触发：不要留下空批次占内存
      return { ok: true, batched: true, flushed, uid: rec.uid, appId: rec.appId, version: rec.version, buffered: 0 };
    }
    if (!b.timer) b.timer = setTimeout(() => flushMicroappBatch(b.key), MICROAPP_BATCH_MS);
    rec.lastActiveAt = Date.now();
    return { ok: true, batched: true, flushed, uid: rec.uid, appId: rec.appId, version: rec.version, buffered: b.lines.length };
  }

  function flushMicroappBatch(key) {
    const b = microappBatches.get(key);
    if (!b) return;
    microappBatches.delete(key);
    if (b.timer) { clearTimeout(b.timer); b.timer = null; }
    const rec = b.rec;
    if (!rec || !rec.wc || rec.wc.isDestroyed() || !b.lines.length) return;
    const seqKey = rec.wc.id + "|" + b.streamId;
    const seq = (streamSeq.get(seqKey) || 0) + 1;
    streamSeq.set(seqKey, seq);
    sendToGuest(rec, MICROAPP_GUEST_EVENT_CHANNEL, {
      kind: "stream", event: "stream.log", streamId: b.streamId, seq, stream: b.stream, lines: b.lines, uid: rec.uid,
    });
  }

  function flushAllMicroappBatches() {
    for (const key of Array.from(microappBatches.keys())) flushMicroappBatch(key);
  }

  /** 下行入口：渲染层/后端 → 主进程 → 目标 guest（协议 §5）。
   *  投递后立即返回（webContents.send 是异步的，不等页面渲染）；stream.log 走聚批。 */
  function microappHostPush(a) {
    const args = a && typeof a === "object" ? a : {};
    const uid = sanitizeUid(args.uid);
    const wcId = Number(args.wcId || args.wc_id || 0);
    const event = String(args.event || "");
    const payload = args.payload && typeof args.payload === "object" ? args.payload : {};

    if (!event) throw new Error(MICROAPP_PUSH_ERR + ": 缺少 event（如 stream.log / stream.state）");
    if (!uid && !wcId) throw new Error(MICROAPP_PUSH_ERR + ": 需要 uid（优先）或 wcId 才能定位微应用实例");

    const rec = findMicroappInstance(uid, wcId);
    if (!rec) {
      throw new Error(MICROAPP_ERR.GUEST_MISSING + ": 没有找到" + (uid ? " uid=" + uid : " wcId=" + wcId) + " 的微应用界面");
    }
    if (!isMicroappSrc(rec.wc.getURL())) {
      throw new Error(MICROAPP_ERR.GUEST_LOADING + ": 目标界面当前不在 app://microapp/ 内，不能下发");
    }

    if (event === "stream.log") return enqueueMicroappLogBatch(rec, payload);

    // 非日志事件（stream.state / 自定义事件）立即下发
    const delivered = sendToGuest(rec, MICROAPP_GUEST_EVENT_CHANNEL, Object.assign({ kind: "stream" }, payload, { event, uid: rec.uid }));
    return { ok: true, delivered, batched: false, uid: rec.uid, appId: rec.appId, version: rec.version };
  }

  /** 诊断用：当前记录到的微应用实例（uid / appId / version / wcId），排查「打到别的标签」时先看它。 */
  function microappInstances() {
    return Array.from(microappByWc.values()).map((r) => ({
      wcId: r.wc ? r.wc.id : 0,
      uid: r.uid,
      appId: r.appId,
      version: r.version,
      url: r.wc && !r.wc.isDestroyed() ? String(r.wc.getURL() || "") : "",
    }));
  }

  /** 找微应用的 guest webview（plan-693-2868 P0-4；v2 起支持 uid 精确寻址，见协议 §6）。
   *
   *  判定顺序：
   *    ① 给了 uid：直接查 uid → guest 映射（map 未命中时按 URL 扫一遍兜底），命中即返回；
   *    ② 没给 uid：按 appId(+version) 在 persist:microapp 分区里扫，多个命中时取**最近活跃**的
   *       那个（lastActiveAt = 最近一次上行请求 / 导航），并回报命中数与 uid，便于排查串台；
   *    ③ 扫描口径不变：类型 webview + 宿主是主窗口 + session 同一性比较 + URL 在 app://microapp/ 内。
   *
   *  getURL() 在首次 did-finish-load 之前可能是 "" 或 about:blank：那说明界面已经挂上但还没加载完，
   *  单独回报「还没加载完」，而不是笼统说「界面没打开」（后者会让模型以为用户没点开面板）。
   *
   *  返回值统一带 uid（空串 = 这台 guest 还没上报过身份），调用方把它带进结果里。
   */
  function microappGuest(appId, version, uid) {
    const mw = getMainWindow();
    if (!mw || mw.isDestroyed()) {
      return { error: MICROAPP_ERR.GUEST_MISSING, detail: "主窗口不可用，微应用界面无从操作" };
    }
    const wantUid = sanitizeUid(uid);
    if (wantUid) {
      const rec = findMicroappInstance(wantUid, 0);
      if (!rec) {
        return { error: MICROAPP_ERR.GUEST_MISSING, detail: "没有找到 uid=" + wantUid + " 的微应用界面（标签可能已关闭或还没加载）" };
      }
      if (appId && rec.appId && rec.appId !== appId) {
        return { error: MICROAPP_ERR.GUEST_MISSING, detail: "uid=" + wantUid + " 对应的界面是 " + rec.appId + "，不是 " + appId };
      }
      return { wc: rec.wc, uid: rec.uid, appId: rec.appId || appId, version: rec.version || version, matches: 1 };
    }

    let target;
    try {
      target = session.fromPartition(MICROAPP_PARTITION);
    } catch (err) {
      return { error: MICROAPP_ERR.GUEST_MISSING, detail: "微应用分区不可用：" + ((err && err.message) || err) };
    }
    // appId / version 已由调用方按正则校验；这里再拼前缀，尾随斜杠保证 demo 不会误中 demo-app
    const prefix = version
      ? "app://microapp/" + appId + "/" + version + "/"
      : "app://microapp/" + appId + "/";
    let sawLoading = false;
    const hits = [];
    for (const wc of webContents.getAllWebContents()) {
      if (!wc || wc.isDestroyed() || wc.getType() !== "webview") continue;
      // 宿主可以是主窗口，也可以是微应用独立窗口（见 hookMicroappHostWindow）
      if (!isMicroappHostWc(wc.hostWebContents)) continue;
      if (wc.session !== target) continue;
      const url = String(wc.getURL() || "");
      if (!url.startsWith(prefix)) {
        if (!url || url === "about:blank") sawLoading = true;
        continue;
      }
      const rec = microappByWc.get(wc.id) || registerMicroappGuest(wc, { src: url });
      hits.push({
        wc,
        uid: rec ? rec.uid : "",
        appId: (rec && rec.appId) || appId,
        version: (rec && rec.version) || version,
        at: (rec && rec.lastActiveAt) || 0,
      });
    }
    if (hits.length) {
      // 多实例：取最近活跃的那个；都没有活动记录时保持「第一个」（与历史行为一致）
      hits.sort((x, y) => y.at - x.at);
      const best = hits[0];
      return { wc: best.wc, uid: best.uid, appId: best.appId, version: best.version, matches: hits.length };
    }
    if (sawLoading) {
      return {
        error: MICROAPP_ERR.GUEST_LOADING,
        detail: "微应用 " + appId + " 的界面已挂上但还没加载完（webview 仍是空白页）",
      };
    }
    return {
      error: MICROAPP_ERR.GUEST_MISSING,
      detail: "没有找到 " + appId + (version ? " v" + version : "") + " 的界面（右侧面板里没打开这个微应用）",
    };
  }

  /** 就绪探针（本轮回归修复）：只回报「界面挂上了、桥也注册了」，**不调用任何方法**（无副作用）。
   *
   *  为什么需要：界面刚挂上时 webview 还在加载，盲重试 microappCall 等于在不确定就绪的情况下
   *  反复执行方法；有探针就能「先问就绪、就绪后再调一次」。
   *  返回 { appId, version, ready: true, uid }；不满足时抛与 microappCall 同一套前缀的错误
   *  （guest-missing / guest-loading / bridge-missing），宿主按前缀决定继续等还是立刻失败。
   *  （uid 是 v2 新增：同 appId 多标签时靠它确认「探的是哪一个界面」，见协议 §6）
   */
  async function microappProbe(a) {
    const appId = String(a.appId || "");
    const version = String(a.version || "");
    if (!MICROAPP_ID_RE.test(appId)) throw new Error(MICROAPP_ERR.GUEST_MISSING + ": appId 非法（" + appId.slice(0, 40) + "）");
    if (version && !MICROAPP_VERSION_RE.test(version)) throw new Error(MICROAPP_ERR.GUEST_MISSING + ": version 非法（" + version.slice(0, 40) + "）");

    const found = microappGuest(appId, version, a.uid);
    if (found.error) throw new Error(found.error + ": " + found.detail);

    // 桥由 app.js 在顶层注册：页面还没执行到这里时取到 undefined，即「还没就绪」而不是「写错了」
    let bridged;
    try {
      bridged = await withTimeout(
        found.wc.executeJavaScript(
          "!!(window.__MICROAPP_METHODS__ && typeof window.__MICROAPP_METHODS__ === 'object')", false,
        ),
        2000,
        MICROAPP_ERR.GUEST_LOADING + ": 微应用界面 2 秒内没有响应探针（可能仍在加载）",
      );
    } catch (err) {
      if (found.wc.isDestroyed()) throw new Error(MICROAPP_ERR.GUEST_MISSING + ": 微应用界面在探针期间被关闭");
      throw err instanceof Error ? err : new Error((err && err.message) || String(err));
    }
    if (!bridged) throw new Error(MICROAPP_ERR.BRIDGE_MISSING + ": 界面已加载，但 window.__MICROAPP_METHODS__ 还没注册");
    return { appId, version, ready: true, uid: found.uid || "" };
  }

  /** 调用微应用界面里的方法（plan-693-2868 P0-4）。
   *
   *  耗时：一次调用 = 一次 executeJavaScript 往返（正常 < 20 ms）+ 方法自身的耗时，上限
   *  MICROAPP_CALL_TIMEOUT_MS（15 s）。executeJavaScript 会 await 方法返回的 Promise，
   *  因此异步方法也能拿到真实结果；超时只是不再等，页面里的方法仍会跑完（界面状态未知）。
   *
   *  安全：方法名按严格的正则白名单取值，参数用 JSON.stringify 嵌入源码——不做字符串拼接，
   *  避免参数里的引号 / 换行把脚本改写成别的代码。
   */
  async function microappCall(a) {
    const appId = String(a.appId || "");
    const version = String(a.version || "");
    const method = String(a.method || "");
    if (!MICROAPP_ID_RE.test(appId)) throw new Error(MICROAPP_ERR.GUEST_MISSING + ": appId 非法（" + appId.slice(0, 40) + "）");
    if (version && !MICROAPP_VERSION_RE.test(version)) throw new Error(MICROAPP_ERR.GUEST_MISSING + ": version 非法（" + version.slice(0, 40) + "）");
    if (!MICROAPP_METHOD_RE.test(method)) throw new Error(MICROAPP_ERR.METHOD_INVALID + ": 方法名不是合法的 JavaScript 标识符（" + method.slice(0, 40) + "）");

    // a.uid 可选：给了就精确命中那一个标签实例（协议 §6），没给仍按 appId(+version) 扫
    const found = microappGuest(appId, version, a.uid);
    if (found.error) throw new Error(found.error + ": " + found.detail);

    const callArgs = a.args && typeof a.args === "object" ? a.args : {};
    // 页面侧只做「取方法 → 调用 → JSON 化结果」三件事，任何异常都在页面里收成结构化 code，
    // 不让 Promise reject 穿到 executeJavaScript（那样只能拿到一句英文 Script failed to execute）。
    // 注意：下面 code 是**字符串字面量**而不是 MICROAPP_ERR 常量——这段源码在页面上下文里执行，
    // 引用不到主进程变量；改前缀要同步改这里的字面量与 Python 的 _BRIDGE_ERROR_HINTS。
    const expr = "(async () => {"
      + " const m = window.__MICROAPP_METHODS__;"
      + " if (!m || typeof m !== 'object') return { code: 'microapp-bridge-missing' };"
      + " const fn = m[" + JSON.stringify(method) + "];"
      + " if (typeof fn !== 'function') return { code: 'microapp-method-missing' };"
      + " let v;"
      + " try { v = await fn(" + JSON.stringify(callArgs) + "); }"
      + " catch (e) { return { code: 'microapp-threw', detail: String((e && e.message) || e) }; }"
      + " try { return { ok: true, value: JSON.parse(JSON.stringify(v === undefined ? null : v)) }; }"
      + " catch (e) { return { code: 'microapp-not-serializable', detail: String((e && e.message) || e) }; }"
      + "})()";
    let r;
    try {
      r = await withTimeout(
        found.wc.executeJavaScript(expr, false),
        MICROAPP_CALL_TIMEOUT_MS,
        MICROAPP_ERR.TIMEOUT + ": 微应用方法 " + method + " 在 " + (MICROAPP_CALL_TIMEOUT_MS / 1000) + " 秒内没有返回",
      );
    } catch (err) {
      const msg = (err && err.message) || String(err);
      // 界面刚好在调用途中被关闭 / 重载：按「界面没打开」回报，比抛一句 guest 崩溃更容易恢复
      if (found.wc.isDestroyed()) throw new Error(MICROAPP_ERR.GUEST_MISSING + ": 微应用界面在调用过程中被关闭");
      throw err instanceof Error ? err : new Error(msg);
    }
    if (!r || typeof r !== "object") {
      throw new Error(MICROAPP_ERR.THREW + ": 微应用页面返回了无法识别的结果（界面可能正在重载）");
    }
    if (r.ok) return { appId, version, method, result: r.value === undefined ? null : r.value, uid: found.uid || "" };
    throw new Error(String(r.code || MICROAPP_ERR.THREW) + (r.detail ? ": " + r.detail : ""));
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
    // plan-693-2868 P0-4：微应用方法调用没有 wcId（界面不由浏览器面板的标签承载），
    // 必须在 panelGuest(wcId) 之前分支，否则会被判成「目标网页已关闭」；
    // microappProbe 是它的只读前置探针（判断界面与桥是否已经就绪），同样不走 panelGuest。
    if (op === "microappProbe") return microappProbe(a);
    if (op === "microappCall") return microappCall(a);
    // plan-693-2868 · docs/app1/11-host-bridge-protocol.md §5：下行推送（渲染层/后端 → guest）。
    // 与上面两条一样不带 wcId，必须在 panelGuest(wcId) 之前分支；按 uid 精确定位实例。
    if (op === "microappHostPush") return microappHostPush(a);
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

  // ── 宿主桥的三条渲染层频道（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §4-§6）──
  // 渲染层只认这三个名字；上行结果走 hostReply（ipcMain.on，不回值），下行推送走 hostPush（handle）。
  ipcMain.handle(MICROAPP_PUSH_CHANNEL, (_event, payload) => {
    try { return { ok: true, result: microappHostPush(payload || {}) }; }
    catch (err) { return { ok: false, error: (err && err.message) || String(err) }; }
  });
  ipcMain.on(MICROAPP_REPLY_CHANNEL, (event, payload) => {
    try {
      const r = microappHostReply(payload || {}, event && event.sender);
      if (!r.ok) logErr("[microapp-host] hostReply 未投递:", r.error);
    } catch (err) {
      logErr("[microapp-host] hostReply 处理异常:", (err && err.message) || err);
    }
  });

  // 主窗口已存在就立刻挂 did-attach-webview（正常路径是 main.cjs 建窗后调用 hookMainWindow；
  // 这里兜住「main.cjs 尚未改到 / 窗口在模块加载前就已建好」的顺序差异）
  ensureMainHooked();

  return {
    /** 由 main.cjs 在建窗后调用：挂 did-attach-webview，记录 uid → guest（协议 §6） */
    hookMainWindow,
    /** 由 main.cjs 在创建微应用独立窗口后调用：把它登记为宿主，里面的 webview 也算微应用 guest */
    hookMicroappHostWindow,
    /** 下行推送（与 op = microappHostPush 同一实现），供主进程内部/测试直接调用 */
    microappHostPush,
    /** 诊断：当前记录到的微应用实例列表 */
    microappInstances,
  };
};
