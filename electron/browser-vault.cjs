/**
 * 内置浏览器 · 本地加密凭据库（plan-771-3177）
 *
 * 安全红线：
 *   1. 使用 Electron safeStorage 加密密码，密文存于 <userData>/browser-vault.enc（0600 + tmp+rename 原子写）；
 *   2. 如果 safeStorage.isEncryptionAvailable() 为假，严格拒绝明文持久化（绝不落盘明文密码）；
 *   3. 列表接口（list）永不返回 passwordEnc 或明文密码；
 *   4. 自动填充（autofill）由主进程校验目标 webview 与 origin 一致后直接向页面填表，不回传明文密码；
 *   5. 仅响应用户交互 IPC，后端 Python / AI 工具绝对无法读取或触发本模块。
 */
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const secure = require("./secure-file.cjs");

function electronApi() {
  try {
    const mod = require("electron");
    return mod && typeof mod === "object" ? mod : {};
  } catch {
    return {};
  }
}

const VAULT_FILENAME = "browser-vault.enc";

function defaultVaultPath() {
  const { app } = electronApi();
  if (!app || typeof app.getPath !== "function") {
    throw new Error("Electron app.getPath 不可用");
  }
  return path.join(app.getPath("userData"), VAULT_FILENAME);
}

function getSafeStorage(override) {
  if (override) return override;
  try {
    const electron = require("electron");
    if (electron && typeof electron === "object" && electron.safeStorage) {
      return electron.safeStorage;
    }
  } catch { /* 非 Electron 环境（如 node 单测） */ }
  return null;
}

/** 归一化站点 origin（仅接受 http/https）；非法地址返回空串。 */
function normalizeOrigin(input) {
  const raw = String(input == null ? "" : input).trim();
  if (!raw) return "";
  try {
    const u = new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    return u.origin;
  } catch {
    return "";
  }
}

/** 脱敏输出：绝不附带 passwordEnc 或明文密码。 */
function sanitizeItem(item) {
  return {
    id: String(item.id || ""),
    origin: String(item.origin || ""),
    url: String(item.url || ""),
    username: String(item.username || ""),
    label: String(item.label || ""),
    updatedAt: String(item.updatedAt || ""),
  };
}

function createBrowserVault({
  getMainWindow,
  vaultPathFn,
  safeStorageOverride,
  logErr,
} = {}) {
  const resolvePath = typeof vaultPathFn === "function" ? vaultPathFn : defaultVaultPath;
  const ss = () => getSafeStorage(safeStorageOverride);
  const errLog = typeof logErr === "function" ? logErr : () => {};
  const protectedGuests = new WeakSet();
  const agentGuests = new WeakSet();
  const pending = new Map();
  const pendingByGuest = new WeakMap();
  let clipboardDigest = "";

  function encryptionAvailable() {
    const mod = ss();
    try {
      return !!(mod && typeof mod.isEncryptionAvailable === "function" && mod.isEncryptionAvailable()
        && mod.getSelectedStorageBackend?.() !== "basic_text");
    } catch {
      return false;
    }
  }

  /**
   * 读取凭据库（plan-771-3177）。
   * 必须区分「还没有库」与「文件存在但读不出来」：后者若当成空库继续读写，
   * 下一次全量写回会把已保存的全部凭据静默覆盖掉，所以写入路径一律拒绝。
   */
  function readStoreChecked() {
    const file = resolvePath();
    let exists = false;
    try {
      exists = fs.existsSync(file);
    } catch {
      // 存在性判断本身失败时按「存在」处理：宁可拒绝写入，也不覆盖可能完好的旧库
      exists = true;
    }
    if (!exists) {
      return { ok: true, store: { items: [], neverOrigins: [] } };
    }
    const data = secure.readPrivateJson(file);
    if (!data || typeof data !== "object") {
      return { ok: false, error: "凭据库读取失败（文件损坏或被占用），已停止写入以免覆盖已保存的密码" };
    }
    const items = Array.isArray(data.items) ? data.items.filter((x) => x && typeof x === "object") : [];
    const neverOrigins = Array.isArray(data.neverOrigins)
      ? Array.from(new Set(data.neverOrigins.map((x) => normalizeOrigin(x)).filter(Boolean)))
      : [];
    return { ok: true, store: { items, neverOrigins } };
  }

  /** 只读场景（列表/展示）沿用空库兜底；写入必须走 readStoreChecked 判定失败。 */
  function readStore() {
    const res = readStoreChecked();
    return res.ok ? res.store : { items: [], neverOrigins: [] };
  }

  function writeStore(store) {
    const file = resolvePath();
    // 写前留一份 .bak：即使本次写入被中断，上一版仍可人工恢复
    try {
      if (fs.existsSync(file)) fs.copyFileSync(file, `${file}.bak`);
    } catch { /* 备份失败不阻塞正常写入 */ }
    secure.writePrivateJson(file, {
      items: store.items,
      neverOrigins: store.neverOrigins,
    });
  }

  function encryptPassword(plain) {
    if (!encryptionAvailable()) {
      return { ok: false, error: "当前系统不支持加密凭据存储" };
    }
    try {
      const buf = ss().encryptString(String(plain));
      return { ok: true, enc: buf.toString("base64") };
    } catch (e) {
      errLog("[browser-vault] 加密凭据失败:", e && e.message);
      return { ok: false, error: "系统加密凭据失败" };
    }
  }

  function decryptPassword(encBase64) {
    if (!encryptionAvailable()) {
      return { ok: false, error: "当前系统不支持加密凭据解密" };
    }
    const mod = ss();
    if (!mod || typeof mod.decryptString !== "function") {
      return { ok: false, error: "系统解密接口不可用" };
    }
    try {
      const buf = Buffer.from(String(encBase64 || ""), "base64");
      if (!buf.length) return { ok: false, error: "凭据密文为空" };
      const plain = mod.decryptString(buf);
      return { ok: true, password: String(plain || "") };
    } catch (e) {
      errLog("[browser-vault] 解密凭据失败:", e && e.message);
      return { ok: false, error: "凭据解密失败（系统密钥可能已变更）" };
    }
  }

  function list(originInput) {
    const store = readStore();
    const filterOrigin = originInput ? normalizeOrigin(originInput) : "";
    let rows = store.items;
    if (originInput && !filterOrigin) {
      rows = [];
    } else if (filterOrigin) {
      rows = rows.filter((it) => normalizeOrigin(it.origin || it.url) === filterOrigin);
    }
    rows = rows.slice().sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
    return {
      ok: true,
      encryptedAvailable: encryptionAvailable(),
      items: rows.map(sanitizeItem),
      neverOrigins: store.neverOrigins,
      isNever: filterOrigin ? store.neverOrigins.includes(filterOrigin) : false,
    };
  }

  function save(entry) {
    const input = entry && typeof entry === "object" ? entry : {};
    const rawUrl = String(input.url || "").trim();
    let url = "";
    try {
      const parsed = new URL(rawUrl);
      url = parsed.origin;
    } catch { /* 由站点校验统一返回错误。 */ }
    const origin = normalizeOrigin(url || input.origin);
    if (!origin) {
      return { ok: false, error: "请提供有效的 http/https 网址" };
    }
    const username = String(input.username == null ? "" : input.username).trim();
    if (!username) {
      return { ok: false, error: "用户名不能为空" };
    }
    const hasPasswordInput = typeof input.password === "string" && input.password.length > 0;
    const storeRes = readStoreChecked();
    if (!storeRes.ok) return { ok: false, error: storeRes.error };
    const store = storeRes.store;
    const idStr = input.id ? String(input.id).trim() : "";
    let existing = idStr ? store.items.find((it) => String(it.id) === idStr) : null;
    if (!existing) {
      existing = store.items.find(
        (it) => normalizeOrigin(it.origin || it.url) === origin && String(it.username || "") === username,
      );
    }

    if (!existing && !hasPasswordInput) {
      return { ok: false, error: "密码不能为空" };
    }

    let passwordEnc = existing ? String(existing.passwordEnc || "") : "";
    if (hasPasswordInput || !existing) {
      const encRes = encryptPassword(String(input.password || ""));
      if (!encRes.ok) return { ok: false, error: encRes.error };
      passwordEnc = encRes.enc;
    }

    const now = new Date().toISOString();
    const label = input.label !== undefined
      ? String(input.label || "").trim().slice(0, 120)
      : (existing ? String(existing.label || "") : "");

    if (existing) {
      existing.origin = origin;
      existing.url = url || existing.url || origin;
      existing.username = username;
      existing.passwordEnc = passwordEnc;
      existing.label = label;
      existing.updatedAt = now;
      writeStore(store);
      return { ok: true, item: sanitizeItem(existing), updated: true };
    }

    const created = {
      id: typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `bv_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`,
      origin,
      url: url || origin,
      username,
      passwordEnc,
      label,
      updatedAt: now,
    };
    store.items.push(created);
    // 用户明确保存凭据时，若该站点曾标记过永不提示，自动解除
    store.neverOrigins = store.neverOrigins.filter((o) => o !== origin);
    writeStore(store);
    return { ok: true, item: sanitizeItem(created), updated: false };
  }

  function remove(id) {
    const idStr = String(id || "").trim();
    if (!idStr) return { ok: false, error: "缺少凭据 ID" };
    const storeRes = readStoreChecked();
    if (!storeRes.ok) return { ok: false, error: storeRes.error };
    const store = storeRes.store;
    const before = store.items.length;
    store.items = store.items.filter((it) => String(it.id) !== idStr);
    if (store.items.length === before) {
      return { ok: false, error: "凭据不存在" };
    }
    writeStore(store);
    return { ok: true, deleted: true };
  }

  function reveal(id) {
    const idStr = String(id || "").trim();
    if (!idStr) return { ok: false, error: "缺少凭据 ID" };
    const store = readStore();
    const item = store.items.find((it) => String(it.id) === idStr);
    if (!item) return { ok: false, error: "凭据不存在" };
    const dec = decryptPassword(item.passwordEnc);
    if (!dec.ok) return { ok: false, error: dec.error };
    return { ok: true, password: dec.password };
  }

  function copyPassword(id) {
    const res = reveal(id);
    if (!res.ok) return res;
    try {
      const { clipboard } = electronApi();
      clipboard.writeText(res.password);
      const digest = crypto.createHash("sha256").update(res.password).digest("hex");
      clipboardDigest = digest;
      setTimeout(() => {
        if (clipboardDigest !== digest) return;
        if (isClipboardProtected(clipboard.readText())) clipboard.clear();
        clipboardDigest = "";
      }, 60_000).unref();
      return { ok: true };
    } catch (e) {
      return { ok: false, error: "写入剪贴板失败：" + ((e && e.message) || e) };
    }
  }

  function isClipboardProtected(text) {
    return !!clipboardDigest && crypto.createHash("sha256").update(String(text)).digest("hex") === clipboardDigest;
  }

  function setNeverOrigin(originInput, never = true) {
    const origin = normalizeOrigin(originInput);
    if (!origin) return { ok: false, error: "无效的站点地址" };
    const storeRes = readStoreChecked();
    if (!storeRes.ok) return { ok: false, error: storeRes.error };
    const store = storeRes.store;
    const set = new Set(store.neverOrigins);
    if (never) set.add(origin);
    else set.delete(origin);
    store.neverOrigins = Array.from(set);
    writeStore(store);
    return { ok: true, origin, never: !!never, neverOrigins: store.neverOrigins };
  }

  function resolvePanelGuest(wcId) {
    const { webContents } = electronApi();
    const mw = typeof getMainWindow === "function" ? getMainWindow() : null;
    const wc = webContents && typeof webContents.fromId === "function" ? webContents.fromId(Number(wcId)) : null;
    if (!wc || wc.isDestroyed() || !mw || mw.isDestroyed()) return null;
    if (wc.getType() !== "webview") return null;
    const host = wc.hostWebContents;
    if (!host || host.id !== mw.webContents.id) return null;
    return wc;
  }

  function emitToHost(payload) {
    const mw = getMainWindow?.();
    if (mw && !mw.isDestroyed()) mw.webContents.send("browserAgent:event", payload);
  }

  function protectGuest(wc) {
    if (agentGuests.has(wc)) return false;
    if (!protectedGuests.has(wc)) {
      protectedGuests.add(wc);
      emitToHost({ type: "login-protected", wcId: wc.id });
    }
    return true;
  }

  function reserveForAgent(wc) {
    if (protectedGuests.has(wc)) return false;
    agentGuests.add(wc);
    return true;
  }

  function loginGuest(event, origin) {
    const wc = resolvePanelGuest(event.sender?.id);
    if (!wc || !event.senderFrame || event.senderFrame !== wc.mainFrame) return null;
    if (!origin || normalizeOrigin(event.senderFrame.url) !== origin || !protectGuest(wc)) return null;
    return wc;
  }

  function offerLogin(event, data) {
    const origin = normalizeOrigin(data?.origin);
    const wc = loginGuest(event, origin);
    if (!wc || !encryptionAvailable() || !getMainWindow?.()?.isFocused()) return;
    const username = typeof data?.username === "string" ? data.username.trim().slice(0, 300) : "";
    const password = typeof data?.password === "string" ? data.password.slice(0, 4096) : "";
    if (!username || !password) return;
    const store = readStore();
    if (store.neverOrigins.includes(origin)) return;
    const existing = store.items.find((entry) => normalizeOrigin(entry.origin) === origin && entry.username === username);
    if (existing && decryptPassword(existing.passwordEnc).password === password) return;
    const encrypted = encryptPassword(password);
    if (!encrypted.ok) return;
    const previous = pendingByGuest.get(wc);
    if (previous) pending.delete(previous);
    const id = crypto.randomUUID();
    pendingByGuest.set(wc, id);
    pending.set(id, { origin, username, encryptedPassword: encrypted.enc, existingId: existing?.id });
    const expire = setTimeout(() => pending.delete(id), 120_000);
    expire.unref();
    wc.once("destroyed", () => { pending.delete(id); clearTimeout(expire); });
    emitToHost({ type: "login-offer", wcId: wc.id, id, origin, username, update: !!existing });
  }

  function resolveLogin(id, action) {
    if (!["save", "never", "skip"].includes(action)) return { ok: false, error: "无效操作" };
    const item = pending.get(id);
    if (!item) return { ok: false, error: "此保存请求已过期" };
    if (action === "skip") { pending.delete(id); return { ok: true }; }
    if (action === "never") {
      const result = setNeverOrigin(item.origin);
      if (result.ok) pending.delete(id);
      return result;
    }
    const decrypted = decryptPassword(item.encryptedPassword);
    if (!decrypted.ok) return decrypted;
    const result = save({ id: item.existingId, url: item.origin, username: item.username, password: decrypted.password });
    if (result.ok) pending.delete(id);
    return { ok: !!result.ok, error: result.error };
  }

  async function autofill(wcId, id) {
    const wc = resolvePanelGuest(wcId);
    if (!wc) return { ok: false, error: "目标网页已关闭或不属于浏览器面板" };

    const idStr = String(id || "").trim();
    if (!idStr) return { ok: false, error: "缺少凭据 ID" };
    const store = readStore();
    const item = store.items.find((it) => String(it.id) === idStr);
    if (!item) return { ok: false, error: "凭据不存在" };

    const currentOrigin = normalizeOrigin(wc.getURL());
    const itemOrigin = normalizeOrigin(item.origin || item.url);
    if (!currentOrigin || currentOrigin !== itemOrigin) {
      return { ok: false, error: "当前页面域名与凭据不匹配，已阻止自动填充" };
    }
    if (agentGuests.has(wc)) return { ok: false, error: "此标签已有 AI 访问记录，请在新的个人标签页填充密码" };

    const dec = decryptPassword(item.passwordEnc);
    if (!dec.ok) return { ok: false, error: dec.error };

    // 直接在目标 webview 页面上下文执行 React/原生受控表单兼容填充，绝不经由 IPC 返回明文密码
    if (!protectGuest(wc)) return { ok: false, error: "当前标签页无法安全填充密码" };
    const script = `(() => {
      if (location.origin !== ${JSON.stringify(itemOrigin)}) return { ok: false, error: '页面地址已变更' };
      const uname = ${JSON.stringify(String(item.username || ""))};
      const pwd = ${JSON.stringify(String(dec.password || ""))};
      const isVisible = (el) => {
        if (!el || el.disabled || el.readOnly) return false;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        const st = getComputedStyle(el);
        return st.visibility !== 'hidden' && st.display !== 'none';
      };
      const setNativeVal = (el, val) => {
        const proto = el instanceof HTMLInputElement ? HTMLInputElement.prototype : Object.getPrototypeOf(el);
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && typeof desc.set === 'function') desc.set.call(el, val);
        else el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const active = document.activeElement;
      const allPwds = Array.from(document.querySelectorAll('input[type="password"]')).filter(isVisible);
      const pwdEl = (active && active.tagName === 'INPUT' && active.type === 'password' && isVisible(active))
        ? active
        : (allPwds[0] || null);
      if (!pwdEl) return { ok: false, error: '当前页面未检测到可见密码输入框' };

      const scope = pwdEl.form || pwdEl.closest('form') || document;
      const candidates = Array.from(scope.querySelectorAll('input')).filter((el) => {
        if (!isVisible(el) || el === pwdEl) return false;
        const t = (el.type || 'text').toLowerCase();
        return t === 'text' || t === 'email' || t === 'tel' || t === 'search' || t === '';
      });
      // 选在密码框之前最近的一个账号/邮箱输入框
      const allInputs = Array.from(scope.querySelectorAll('input'));
      const pwdIdx = allInputs.indexOf(pwdEl);
      let userEl = null;
      for (let i = pwdIdx - 1; i >= 0; i--) {
        if (candidates.includes(allInputs[i])) { userEl = allInputs[i]; break; }
      }
      if (!userEl && candidates.length) userEl = candidates[0];

      if (userEl && uname) setNativeVal(userEl, uname);
      setNativeVal(pwdEl, pwd);
      try { pwdEl.focus(); } catch {}
      return { ok: true, filledUsername: !!(userEl && uname), filledPassword: true };
    })()`;

    try {
      const result = await wc.executeJavaScript(script, true);
      if (!result || !result.ok) {
        return { ok: false, error: (result && result.error) || "页面表单自动填充失败" };
      }
      return {
        ok: true,
        filledUsername: !!result.filledUsername,
        filledPassword: !!result.filledPassword,
      };
    } catch {
      return { ok: false, error: "执行自动填充脚本失败" };
    }
  }

  function registerIpc() {
    const { ipcMain } = electronApi();
    if (!ipcMain || typeof ipcMain.handle !== "function") return;
    const handle = (name, fn) => ipcMain.handle(`browserVault:${name}`, async (event, ...args) => {
      const host = getMainWindow?.()?.webContents;
      if (!host || host !== event.sender || event.senderFrame !== host.mainFrame) return { ok: false, error: "来源无权访问密码库" };
      try { return await fn(...args); }
      catch { return { ok: false, error: "无法完成密码库操作" }; }
    });
    handle("list", list);
    handle("save", save);
    handle("delete", remove);
    handle("copyPassword", copyPassword);
    handle("autofill", autofill);
    handle("setNever", setNeverOrigin);
    handle("resolveLogin", resolveLogin);
    ipcMain.on("browserVault:protectLogin", (event, origin) => {
      try { loginGuest(event, normalizeOrigin(origin)); } catch { /* 网页已经关闭。 */ }
    });
    ipcMain.on("browserVault:offerLogin", (event, data) => {
      try { offerLogin(event, data); } catch { /* 页面可能正在导航或关闭。 */ }
    });
  }

  return {
    registerIpc,
    list,
    save,
    delete: remove,
    reveal,
    copyPassword,
    autofill,
    setNeverOrigin,
    encryptionAvailable,
    reserveForAgent,
    isProtected: (wc) => protectedGuests.has(wc),
    isAgentGuest: (wc) => agentGuests.has(wc),
    isClipboardProtected,
    _internals: { normalizeOrigin, sanitizeItem },
  };
}

module.exports = createBrowserVault;
