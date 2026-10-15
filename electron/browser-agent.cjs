// 内置浏览器 · 主进程能力（plan-20261015-browser-agent）
//
// 右侧面板里的每个 <webview> 标签挂载后，渲染层调用 browserAgent:prepare 把它交给这里：
//   ① 关闭后台节流：面板折叠 / 切到别的标签 / 窗口最小化时页面照常运行，AI 可在后台操作；
//      （实测：节流不关时隐藏页的 rAF 停摆、CDP 鼠标事件等不到回执）
//   ② 挂 CDP 调试器：记录网络请求、接管网页 JS 弹窗——webview 默认会弹**系统原生对话框**
//      并卡住页面（实测），AI 操作期间由 AI 按策略应答，用户操作触发的转给渲染层用应用内对话框询问；
//   ③ 网页打开新窗口（target=_blank / window.open）一律转成面板里的新标签，不再静默失败。
// 截图走 capturePage(stayHidden)：webview 停泊在视口内时，即使面板隐藏也能截（实测 7–12 ms）。
//
// 只接受「宿主是主窗口」的 webview guest，任何其它 webContents 一律拒绝。
const { ipcMain, webContents, clipboard, nativeImage } = require("electron");

const NET_MAX = 400;

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

module.exports = function setupBrowserAgent({ getMainWindow, log, logErr }) {
  /** wcId → { wc, attached, net: [], netById: Map, seq, aiUntil, accept, promptText, events: [], pendingDialog } */
  const guests = new Map();

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
    } else if (method === "Network.loadingFailed") {
      const e = st.netById.get(params.requestId);
      if (e) { e.status = "failed"; e.error = params.canceled ? "已取消" : String(params.errorText || "").slice(0, 200); }
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
  }

  function prepare(wcId) {
    const wc = panelGuest(wcId);
    if (!wc) return { ok: false, error: "不是面板内的网页" };
    const existing = guests.get(wc.id);
    if (existing) return { ok: true, debugger: existing.attached };
    const st = { wc, attached: false, net: [], netById: new Map(), seq: 0, aiUntil: 0, accept: true, promptText: "", events: [], pendingDialog: null };
    guests.set(wc.id, st);
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
      wc.executeJavaScript("[innerWidth, innerHeight, document.documentElement.scrollWidth, document.documentElement.scrollHeight]", false),
      5000, "读取页面尺寸超时",
    );
    const [vw, vh, sw, sh] = metrics.map((n) => Math.max(1, Math.round(Number(n) || 1)));
    const quality = clampInt(a.quality, 30, 95, 72);
    const maxSide = clampInt(a.max_side, 320, 4096, 1280);
    let img = null;
    let cssW = vw;
    let cssH = vh;
    if (a.full_page && st && st.attached) {
      cssW = Math.min(Math.max(vw, sw), 4000);
      cssH = Math.min(sh, 12000);
      try {
        const res = await withTimeout(cdp(st, "Page.captureScreenshot", {
          format: "jpeg", quality, captureBeyondViewport: true,
          clip: { x: 0, y: 0, width: cssW, height: cssH, scale: 1 },
        }), 12000, "整页截图超时");
        img = nativeImage.createFromBuffer(Buffer.from(res.data, "base64"));
      } catch (err) {
        log("[browser-agent] 整页截图失败，退回视口截图:", err && err.message);
        img = null;
        cssW = vw;
        cssH = vh;
      }
    }
    if (!img) {
      let rect;
      if (a.clip) {
        const x = Math.max(0, Math.round(Number(a.clip.x) || 0));
        const y = Math.max(0, Math.round(Number(a.clip.y) || 0));
        rect = { x, y, width: Math.max(1, Math.min(vw - x, Math.round(Number(a.clip.width) || 1))), height: Math.max(1, Math.min(vh - y, Math.round(Number(a.clip.height) || 1))) };
        cssW = rect.width;
        cssH = rect.height;
      }
      img = await withTimeout(wc.capturePage(rect, { stayHidden: true, stayAwake: true }), 15000, "截图超时（页面未在渲染）");
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
    return { base64: buf.toString("base64"), mime: "image/jpeg", width: tw, height: th, css_width: cssW, css_height: cssH };
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
        return {};
      }
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
    try {
      return { ok: true, result: await exec(wcId, String(op || ""), args || {}) };
    } catch (err) {
      return { ok: false, error: (err && err.message) || String(err) };
    }
  });
};
