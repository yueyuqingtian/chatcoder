/** 宠物窗口与宠物资源管理（plan-73-323 阶段1）。
 *
 * 职责边界：本模块只管「窗口 + 资源 + 偏好」三件与 UI 无关的事。
 * 任务状态一律由宠物窗口渲染层直连后端 WebSocket 获取，主进程**不做状态镜像**——
 * chatcoder 自身就是 agent 宿主，事件源在第一方后端，无需 hook/管道。
 *
 * 设计要点：
 *   ① 窗口宽度固定、只切高度（形态档位），且切换时**保持底边对齐**：
 *      宠物在屏幕上的位置不动，只有上方胶囊/面板区伸缩，避免悬停/展开时宠物跳动。
 *   ② 透明区域不挡下层点击：非交互态保持 setIgnoreMouseEvents(true,{forward:true})，
 *      渲染层做命中检测后经 IPC 切成可交互；面板展开期间整窗可交互。
 *   ③ 偏好单一数据源在本模块（userData/pet-pref.json），两个窗口只监听变更事件，
 *      不各自维护副本——避免"设置页改了、宠物窗口没变"。
 *   ④ 资源下载走 Electron net（遵循系统代理），落盘前做魔数/网格/体积校验，
 *      并先写临时文件再原子 rename——避免半截文件被当成有效宠物。
 */
"use strict";

const { app, BrowserWindow, ipcMain, screen, dialog, net, shell } = require("electron");
const path = require("path");
const fs = require("fs");
// plan-73-340：消除 DWM 系统边框（主窗同款手段，宠物窗此前遗漏——
// 用户看到的「浮窗周围半透明色块」就是系统边框在透明窗口上被 DWM 合成出来的）
const glassDiag = require("./glass-diagnostics.cjs");

// ─────────────────────────── 常量 ───────────────────────────

/** 宠物包网格（petdex 官方格式：8 列 × 9 行，v2 为 8×11，单帧 192×208） */
const GRID_COLS = 8;
const FRAME_W = 192;
const FRAME_H = 208;
const GRID_ROWS_V1 = 9;
const GRID_ROWS_V2 = 11;

/** ── 尺寸模型（plan-73-341：窗口恒定尺寸）──
 *
 * **窗口尺寸固定，生命周期内不再改变** —— 这是消除"展开浮窗时残影/抖动"的根因手段：
 *   · 改窗口尺寸时，DWM 会在 resize 期间拉伸旧帧做合成 → 透明窗口上表现为残影；
 *   · 窗口尺寸（主进程）与页面布局（渲染层）跨进程推进，**永远无法原子同步**；
 *   · 业界同类实现（petdex 官方端 / claude-code-island / qoder）一律是固定窗口 +
 *     窗口内绘制变化，从不 resize —— 这是被反复验证过的做法。
 *
 * 因此形态（仅宠物 / 浮窗块 / 展开面板）全部改为**窗口内 CSS 动画**表达：
 * 透明区域依旧穿透，命中检测按元素矩形判定，大窗口不挡桌面点击。
 */
const WIN_W = 384;
const WIN_H = 520;
/** 宠物显示基准尺寸（渲染层 SpritePlayer 按 scale 派生实际像素） */
const PET_BASE_W = 96;
const PET_BASE_H = 104;
/** 缩放上下限（手柄拖拽 / 设置页滑块共用） */
const SCALE_MIN = 0.6;
const SCALE_MAX = 2;
/** 拖拽/缩放自循环帧间隔（约 60fps；只做一次 setBounds 或一次偏好写入） */
const DRAG_TICK_MS = 16;
/** 拖拽方向判定死区（px）：自上次切换以来光标水平累计位移超过它才认定换向。
 *
 * 为什么方向必须由**主进程**判定：拖拽期间窗口跟随光标移动，鼠标相对窗口的位置恒定，
 * Chromium 因此不再派发 pointermove —— 渲染层拿不到新坐标，方向会冻结在启动值，
 * 表现为"无论怎么拖都朝同一方向走"；只有拖到屏幕边缘、窗口被钳制停住时事件才恢复
 * （用户反馈的"只有向右贴边才向右走"正是此现象）。
 * 主进程每帧都能读到光标真实位置，是唯一可靠的方向来源。
 *
 * 取值 5px 是"即时响应"与"抗手抖"的折中：16ms 一帧下最快约 80ms 内换向，
 * 而人手抖动幅度通常 <3px，不会误触发来回切换。 */
const DRAG_DIR_DEADZONE = 5;
/** 渲染层布局常量（与 pet.css 的 --edge / --handle-h / 徽标槽**严格对应**；
 *  拖拽越界钳制要靠它换算"宠物在窗口内的位置"，不一致就会钳错） */
const PET_EDGE = 8;
const HANDLE_H = 20;
const PET_BADGE_W = 28;

/** 初始位置距工作区边缘的距离；拖动结束的吸附阈值 */
const EDGE_MARGIN = 24;
const SNAP_THRESHOLD = 28;

/** manifest 缓存有效期、单文件体积上限、已安装宠物上限 */
const MANIFEST_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_SPRITE_BYTES = 8 * 1024 * 1024;
const MAX_PETS = 20;
const DOWNLOAD_TIMEOUT_MS = 20000;
const DOWNLOAD_RETRY = 2;

const PETDEX_MANIFEST_URL = "https://petdex.dev/api/manifest";
const PETDEX_PET_URL = (slug) => `https://petdex.dev/pets/${encodeURIComponent(slug)}`;

/** 偏好默认值（enabled 默认关：未安装宠物前不出现窗口，避免"莫名多出一个窗口"） */
const DEFAULT_PREF = {
  enabled: false,
  slug: "",
  displayId: null,
  /** 距工作区右边缘的间距（left 贴边时忽略）；与 bottomY 共同构成位置锚点 */
  rightGap: null,
  bottomY: null,
  edge: null,
  /** 当前是否显示（手柄一键隐藏时置 false，可从设置页恢复） */
  visible: true,
  /** 浮窗（任务块）是否折叠——手柄只有这一个语义（plan-73-340） */
  floatCollapsed: false,
  /** 浮窗块的显式顺序（会话 id 数组，索引 0 = 最外层/最靠近宠物）。
   *  用户在浮窗里拖拽调整后写入；为空则按自动排序（状态优先级 + 开始时间）。 */
  blockOrder: [],
  scale: 1,
  clickThrough: true,
  showCapsule: true,
  /** 聚焦展开后最多显示的任务块数（未聚焦时固定折叠为 1 块，对齐 qoder） */
  maxCapsuleRows: 3,
  showBadge: true,
  hideOnFullscreen: false,
  hideOnMinimize: true,
};

// ─────────────────────────── 模块状态 ───────────────────────────

let pref = null;
let petWindow = null;
let interactive = false;
let registered = false;
let mainHooked = false;
let ctx = { log: () => {}, logErr: () => {}, getMainWindow: () => null, getBackendPort: () => 12973 };

const log = (...a) => ctx.log("[pet]", ...a);
const logErr = (...a) => ctx.logErr("[pet]", ...a);

// ─────────────────────────── 路径与偏好 ───────────────────────────

function prefFile() { return path.join(app.getPath("userData"), "pet-pref.json"); }
function petsDir() { return path.join(app.getPath("userData"), "pets"); }
function manifestCacheFile() { return path.join(app.getPath("userData"), "pet-manifest.json"); }
/** 会话标题持久缓存（plan-73-341）：解决"浮窗长期显示「会话 N」"—— */
function titlesFile() { return path.join(app.getPath("userData"), "pet-titles.json"); }
function petDir(slug) { return path.join(petsDir(), slug); }

/** 读取标题缓存（id → title）。损坏时返回空表，绝不抛错。 */
function readTitles() {
  try {
    const raw = JSON.parse(fs.readFileSync(titlesFile(), "utf8"));
    return raw && typeof raw === "object" ? raw : {};
  } catch {
    return {};
  }
}

/** 合并写入标题缓存。
 *  为什么落盘：标题事件只在会话**首次命名**时携带标题，宠物窗口晚于该时刻启动就永远收不到；
 *  落盘后冷启动即可立刻显示正确标题，不依赖网络时序。 */
function mergeTitles(map) {
  if (!map || typeof map !== "object") return;
  const cur = readTitles();
  let changed = false;
  for (const [k, v] of Object.entries(map)) {
    if (typeof v === "string" && v && cur[k] !== v) { cur[k] = v; changed = true; }
  }
  if (!changed) return;
  try { fs.writeFileSync(titlesFile(), JSON.stringify(cur)); } catch { /* 写失败不影响本次使用 */ }
}

/** slug 白名单过滤：仅允许小写字母/数字/连字符/下划线，防止路径穿越 */
function safeSlug(slug) {
  const s = String(slug || "").trim().toLowerCase();
  return /^[a-z0-9][a-z0-9_-]{0,63}$/.test(s) ? s : "";
}

function readPref() {
  try {
    const raw = JSON.parse(fs.readFileSync(prefFile(), "utf8"));
    return { ...DEFAULT_PREF, ...(raw && typeof raw === "object" ? raw : {}) };
  } catch {
    return { ...DEFAULT_PREF };
  }
}

function writePref(patch) {
  pref = { ...pref, ...(patch || {}) };
  try {
    fs.writeFileSync(prefFile(), JSON.stringify(pref, null, 2));
  } catch (e) {
    logErr("偏好写入失败:", e && e.message);
  }
  return pref;
}

/** 偏好变更广播给所有窗口（含主窗设置页与宠物窗口） */
function broadcastPref() {
  for (const w of BrowserWindow.getAllWindows()) {
    try { w.webContents.send("pet:prefChanged", pref); } catch { /* 窗口正在销毁 */ }
  }
}

// ─────────────────────────── WEBP 解析与宠物包校验 ───────────────────────────

/** 解析 WEBP 容器：格式、canvas 尺寸、是否带 alpha（用于安装前校验网格与透明通道） */
function parseWebp(buf) {
  if (!buf || buf.length < 16) return { ok: false, reason: "文件过小" };
  const riff = buf.toString("ascii", 0, 4);
  const webp = buf.toString("ascii", 8, 12);
  if (riff !== "RIFF" || webp !== "WEBP") return { ok: false, reason: "不是 WEBP 容器" };
  const chunks = [];
  let off = 12;
  while (off + 8 <= buf.length) {
    const name = buf.toString("ascii", off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    chunks.push({ name, size });
    off += 8 + size + (size % 2);
  }
  const fourcc = chunks.length ? chunks[0].name : "";
  let w = 0;
  let h = 0;
  if (fourcc === "VP8X") {
    w = 1 + (buf[24] | (buf[25] << 8) | (buf[26] << 16));
    h = 1 + (buf[27] | (buf[28] << 8) | (buf[29] << 16));
  } else if (fourcc === "VP8L") {
    const b1 = buf[21], b2 = buf[22], b3 = buf[23], b4 = buf[24];
    w = 1 + (((b2 & 0x3f) << 8) | b1);
    h = 1 + (((b4 & 0x0f) << 10) | (b3 << 2) | (b2 >> 6));
  } else if (fourcc === "VP8 ") {
    w = buf.readUInt16LE(26) & 0x3fff;
    h = buf.readUInt16LE(28) & 0x3fff;
  } else {
    return { ok: false, reason: `未知 WEBP 变体 ${fourcc || "(空)"}` };
  }
  const flags = fourcc === "VP8X" ? buf[20] : 0;
  const alpha = fourcc === "VP8L" ? true : (flags & 0x10) !== 0 || chunks.some((c) => c.name === "ALPH");
  return { ok: true, fourcc, w, h, alpha };
}

/** 校验宠物目录内的 sprite.webp + pet.json，返回展示用元信息（不合法则抛错） */
function validatePetPackage(dir) {
  const sprite = path.join(dir, "sprite.webp");
  const metaPath = path.join(dir, "pet.json");
  if (!fs.existsSync(sprite)) throw new Error("缺少 sprite.webp");
  if (!fs.existsSync(metaPath)) throw new Error("缺少 pet.json");
  let meta = {};
  try { meta = JSON.parse(fs.readFileSync(metaPath, "utf8")); } catch { meta = {}; }
  const buf = fs.readFileSync(sprite);
  if (buf.length > MAX_SPRITE_BYTES) throw new Error(`精灵图超过 ${Math.round(MAX_SPRITE_BYTES / 1024 / 1024)}MB 上限`);
  const info = parseWebp(buf);
  if (!info.ok) throw new Error(info.reason || "不是有效的 WEBP 图片");
  if (info.w % GRID_COLS !== 0 || info.w / GRID_COLS !== FRAME_W) {
    throw new Error(`宽不符合 8 列 × 192px（实际 ${info.w}）`);
  }
  if (info.h !== FRAME_H * GRID_ROWS_V1 && info.h !== FRAME_H * GRID_ROWS_V2) {
    throw new Error(`高不符合 9 或 11 行 × 208px（实际 ${info.h}）`);
  }
  const rows = info.h / FRAME_H;
  return {
    displayName: String(meta.displayName || meta.id || path.basename(dir)),
    description: String(meta.description || ""),
    width: info.w,
    height: info.h,
    rows,
    alpha: info.alpha,
    bytes: buf.length,
  };
}

/** 列出已安装宠物（含校验元信息；损坏项带 broken 标记而非直接隐藏，便于用户清理） */
function listInstalled() {
  const out = [];
  let names = [];
  try { names = fs.readdirSync(petsDir(), { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name); } catch { return out; }
  for (const slug of names) {
    const dir = petDir(slug);
    try {
      const info = validatePetPackage(dir);
      out.push({ slug, source: slug.startsWith("local-") ? "local" : "petdex", ...info });
    } catch (e) {
      out.push({ slug, source: slug.startsWith("local-") ? "local" : "petdex", broken: true, reason: e && e.message });
    }
  }
  out.sort((a, b) => a.slug.localeCompare(b.slug));
  return out;
}

// ─────────────────────────── 图库与下载 ───────────────────────────

/** 读取/刷新 petdex 图库清单（24h 缓存；force=true 强制刷新；失败回落缓存） */
async function fetchManifest(force) {
  const file = manifestCacheFile();
  let cached = null;
  try { cached = JSON.parse(fs.readFileSync(file, "utf8")); } catch { cached = null; }
  if (!force && cached && Array.isArray(cached.pets) && Date.now() - (cached.at || 0) < MANIFEST_TTL_MS) {
    return { ...cached, fromCache: true };
  }
  try {
    const res = await net.fetch(PETDEX_MANIFEST_URL, { signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const pets = Array.isArray(data.pets) ? data.pets : [];
    const cache = { at: Date.now(), total: Number(data.total) || pets.length, pets };
    try { fs.writeFileSync(file, JSON.stringify(cache)); } catch { /* 缓存写失败不影响本次结果 */ }
    return { ...cache, fromCache: false };
  } catch (e) {
    if (cached && Array.isArray(cached.pets)) {
      log("图库拉取失败，回落本地缓存:", e && e.message);
      return { ...cached, fromCache: true, stale: true, error: String(e && e.message) };
    }
    throw e;
  }
}

/** 下载到目标路径：带超时与重试，先写 .tmp 再原子 rename */
async function downloadFile(url, dest, attempt = 0) {
  try {
    const res = await net.fetch(url, { signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const buf = Buffer.from(await res.arrayBuffer());
    if (buf.length > MAX_SPRITE_BYTES) throw new Error(`文件超过 ${Math.round(MAX_SPRITE_BYTES / 1024 / 1024)}MB 上限`);
    const tmp = `${dest}.tmp`;
    fs.writeFileSync(tmp, buf);
    fs.renameSync(tmp, dest);
    return buf.length;
  } catch (e) {
    if (attempt < DOWNLOAD_RETRY) {
      await new Promise((r) => setTimeout(r, 600 * (attempt + 1)));
      return downloadFile(url, dest, attempt + 1);
    }
    throw e;
  }
}

/** 安装图库宠物：下载精灵图与元信息 → 校验 → 登记 */
async function installPet(slugRaw) {
  const slug = safeSlug(slugRaw);
  if (!slug) throw new Error("宠物标识不合法");
  const man = await fetchManifest(false);
  const item = man.pets.find((p) => p.slug === slug);
  if (!item) throw new Error("图库中不存在该宠物（可尝试刷新图库）");
  if (!item.spritesheetUrl || !item.petJsonUrl) throw new Error("该宠物缺少下载地址");
  const installed = listInstalled();
  if (installed.length >= MAX_PETS && !installed.some((p) => p.slug === slug)) {
    throw new Error(`已安装 ${MAX_PETS} 只宠物，请先移除部分再安装`);
  }
  const dir = petDir(slug);
  fs.mkdirSync(dir, { recursive: true });
  try {
    await downloadFile(item.spritesheetUrl, path.join(dir, "sprite.webp"));
    await downloadFile(item.petJsonUrl, path.join(dir, "pet.json"));
    const info = validatePetPackage(dir);
    log("安装完成:", slug, `${info.width}x${info.height}`, `${Math.round(info.bytes / 1024)}KB`);
    return { slug, source: "petdex", author: item.submittedBy || "", kind: item.kind || "", ...info };
  } catch (e) {
    // 安装失败不留残档，避免列表里出现"损坏宠物"
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
    throw e;
  }
}

/** 移除已安装宠物（同时清理目录；若为当前宠物则清空 slug 并隐藏窗口） */
function removePet(slugRaw) {
  const slug = safeSlug(slugRaw);
  if (!slug) throw new Error("宠物标识不合法");
  try { fs.rmSync(petDir(slug), { recursive: true, force: true }); } catch (e) {
    throw new Error(`移除失败：${e && e.message}`);
  }
  if (pref.slug === slug) {
    writePref({ slug: "" });
    broadcastPref();
    pushAssets();
    syncVisibility();
  }
  return { slug };
}

/** 本地导入：选目录 → 复制为本地宠物（兼容 pet.json 的 spritesheetPath 命名） */
async function importLocalPet(parentWindow) {
  const r = await dialog.showOpenDialog(parentWindow && !parentWindow.isDestroyed() ? parentWindow : null, {
    properties: ["openDirectory"],
    title: "选择宠物目录（需含 pet.json 与精灵图）",
  });
  if (r.canceled || !r.filePaths || !r.filePaths.length) return { ok: false, canceled: true };
  const src = r.filePaths[0];
  let meta = {};
  try { meta = JSON.parse(fs.readFileSync(path.join(src, "pet.json"), "utf8")); } catch { meta = {}; }
  const spriteName = String(meta.spritesheetPath || "spritesheet.webp");
  const candidates = [spriteName, "sprite.webp", "spritesheet.webp", "spritesheet.png"];
  let spriteSrc = null;
  for (const name of candidates) {
    const p = path.join(src, path.basename(name));
    if (fs.existsSync(p)) { spriteSrc = p; break; }
  }
  if (!spriteSrc) throw new Error("目录内未找到精灵图（spritesheet.webp / sprite.webp）");
  if (!fs.existsSync(path.join(src, "pet.json"))) throw new Error("目录内未找到 pet.json");
  const slug = `local-${safeSlug(String(meta.id || path.basename(src))).slice(0, 40) || "pet"}`;
  const dir = petDir(slug);
  fs.mkdirSync(dir, { recursive: true });
  try {
    fs.copyFileSync(spriteSrc, path.join(dir, "sprite.webp"));
    fs.writeFileSync(path.join(dir, "pet.json"), JSON.stringify({
      id: String(meta.id || slug),
      displayName: String(meta.displayName || path.basename(src)),
      description: String(meta.description || ""),
      spritesheetPath: "sprite.webp",
    }, null, 2));
    const info = validatePetPackage(dir);
    return { ok: true, slug, source: "local", ...info };
  } catch (e) {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
    throw e;
  }
}

// ─────────────────────────── 窗口几何 ───────────────────────────

/** 宠物显示尺寸（与渲染层 SpritePlayer 按同一 scale 派生） */
function petDisplaySize() {
  const s = Math.min(Math.max(Number(pref && pref.scale) || 1, SCALE_MIN), SCALE_MAX);
  return { w: Math.round(PET_BASE_W * s), h: Math.round(PET_BASE_H * s) };
}

/** 宠物本体在窗口内的矩形（与 pet.css 布局严格对应：右侧留 PET_EDGE、底部留 PET_EDGE+HANDLE_H） */
function petRectInWindow() {
  const { w, h } = petDisplaySize();
  return { x: WIN_W - PET_EDGE - w, y: WIN_H - PET_EDGE - HANDLE_H - h, w, h };
}

/** 钳制窗口位置：保证**宠物本体**始终有足够面积留在工作区内。
 *
 * 为什么按宠物而不是按窗口钳制：窗口远大于宠物（还含浮窗/面板区），
 * 若按窗口边界钳制，宠物本体仍可被推到屏幕外 —— 用户反馈的"拖出去就抓不回来"正是此因。
 * `KEEP_VISIBLE` 为宠物至少要留在可视区内的像素数（够看清并抓住即可）。
 */
const KEEP_VISIBLE = 48;
function clampWindowPos(x, y, wa) {
  const r = petRectInWindow();
  const minX = wa.x + KEEP_VISIBLE - (r.x + r.w);
  const maxX = wa.x + wa.width - KEEP_VISIBLE - r.x;
  const minY = wa.y + KEEP_VISIBLE - (r.y + r.h);
  const maxY = wa.y + wa.height - KEEP_VISIBLE - r.y;
  return {
    x: Math.round(Math.min(Math.max(x, minX), maxX)),
    y: Math.round(Math.min(Math.max(y, minY), maxY)),
  };
}

function pickDisplay() {
  const all = screen.getAllDisplays();
  if (pref.displayId != null) {
    const found = all.find((d) => d.id === pref.displayId);
    if (found) return found;
  }
  return screen.getPrimaryDisplay();
}

/** 解析窗口位置：窗口尺寸固定，位置由「右边缘间距 + 底边」锚点推导 */
function resolvePosition() {
  const disp = pickDisplay();
  const wa = disp.workArea;
  const bottom = Number.isFinite(pref.bottomY) ? pref.bottomY : wa.y + wa.height - EDGE_MARGIN;
  let x;
  if (pref.edge === "left") {
    x = wa.x + EDGE_MARGIN;
  } else {
    const gap = Number.isFinite(pref.rightGap) ? pref.rightGap : EDGE_MARGIN;
    x = wa.x + wa.width - WIN_W - gap;
  }
  return clampWindowPos(x, bottom - WIN_H, wa);
}

/** 记忆位置：以「右边缘间距 + 底边」为准，宽度变化时视觉锚点不丢 */
function rememberPosition() {
  if (!petWindow || petWindow.isDestroyed()) return;
  const b = petWindow.getBounds();
  const disp = screen.getDisplayMatching(b);
  const wa = disp.workArea;
  const patch = { bottomY: b.y + b.height, displayId: disp.id };
  if (pref.edge !== "left") patch.rightGap = Math.max(0, wa.x + wa.width - (b.x + b.width));
  writePref(patch);
}

// ─────────────────────────── 窗口生命周期 ───────────────────────────

function setInteractive(on) {
  interactive = !!on;
  if (!petWindow || petWindow.isDestroyed()) return;
  try {
    // clickThrough 关闭时始终可交互（用户显式禁用了穿透）
    const ignore = pref.clickThrough && !interactive;
    petWindow.setIgnoreMouseEvents(ignore, { forward: true });
  } catch (e) {
    logErr("穿透切换失败:", e && e.message);
  }
}

/* ─────────────── 拖拽与缩放：主进程自循环（plan-73-341）───────────────
 *
 * 为什么把交互搬到主进程（历史上反复修不好的两个根因）：
 *  ① **坐标换算**：渲染层鼠标事件坐标与主进程窗口 API 分属两套换算，
 *     150% 缩放屏上误差被放大，表现为"往左上拖却往右下跑"。
 *     改为 `screen.getCursorScreenPoint()` —— 它与 `setBounds` 是**同一 DIP 坐标系**，
 *     没有换算环节，从根上消除漂移。
 *  ② **事件不可靠**：穿透窗口（`setIgnoreMouseEvents(true,{forward:true})`）只转发
 *     mousemove、**不转发 mouseup**；窗口尺寸变化还会产生合成事件。
 *     改为 `GetAsyncKeyState` 查询按键状态 —— 不依赖任何事件送达，
 *     既不会残留拖拽状态（"没点击宠物自己动"），也不会被合成事件误判（"缩放只抖一下"）。
 *
 * 交互期间只做一件事：`setBounds` 改位置（拖拽）或改偏好（缩放），
 * **不改窗口尺寸**（窗口恒定），因此不会触发 DWM 的 resize 合成。
 */

/** 当前交互会话：{ kind: 'drag'|'scale', ... }；null = 无 */
let interaction = null;
let interactionTimer = null;
/** 诊断开关：CHATCODER_PET_DEBUG=1 时打印每帧数值，便于定位（规则禁止启动应用实测时的替代手段） */
const debugOn = process.env.CHATCODER_PET_DEBUG === "1";

/** 把拖拽方向推给渲染层（仅方向**变化**时调用，因此 IPC 次数极少、不会造成每帧开销）。
 *  渲染层据此切换 running-left / running-right 行走动画。 */
function sendDragDir(dir) {
  if (!petWindow || petWindow.isDestroyed()) return;
  try { petWindow.webContents.send("pet:dragDir", dir); } catch { /* 窗口销毁竞态，忽略 */ }
}

/** 结束交互：停循环 + 收尾（拖拽走贴边吸附与位置记忆；缩放补一次落盘） */
function endInteraction(commit) {
  if (interactionTimer) { clearInterval(interactionTimer); interactionTimer = null; }
  const it = interaction;
  interaction = null;
  if (!it) return;
  if (!petWindow || petWindow.isDestroyed()) return;

  if (it.kind === "drag" && commit) {
    const b = petWindow.getBounds();
    const disp = screen.getDisplayMatching(b);
    const wa = disp.workArea;
    // 吸附：距左右工作区边缘小于阈值即贴边
    let edge = null;
    if (Math.abs(b.x - wa.x) <= SNAP_THRESHOLD) edge = "left";
    else if (Math.abs(wa.x + wa.width - (b.x + b.width)) <= SNAP_THRESHOLD) edge = "right";
    let x = b.x;
    if (edge === "left") x = wa.x + EDGE_MARGIN;
    else if (edge === "right") x = wa.x + wa.width - WIN_W - EDGE_MARGIN;
    petWindow.setBounds({ x: Math.round(x), y: b.y, width: WIN_W, height: WIN_H });
    writePref({ edge });
    rememberPosition();
    if (debugOn) log("[debug] drag end", { edge, bounds: petWindow.getBounds() });
  }

  if (it.kind === "scale" && it.saveTimer) {
    clearTimeout(it.saveTimer);
    it.saveTimer = null;
    writePref({}); // 补一次落盘（缩放期间为省 IO 只改内存）
  }

  try { petWindow.webContents.send("pet:interactionEnded", { kind: it.kind }); } catch { /* ignore */ }
}

/** 开始拖拽：offset 为「鼠标在窗口客户区内的偏移」，拖动时保持不变即"抓取点跟手"。
 *  initialDir 为渲染层在**启动帧**给出的初判方向（那一刻窗口尚未移动、坐标可信）；
 *  传入后立即生效，避免等主进程首帧判定造成方向短暂缺失。 */
function beginDrag(offsetX, offsetY, initialDir) {
  if (!petWindow || petWindow.isDestroyed()) return { ok: false };
  endInteraction(false);
  interaction = {
    kind: "drag",
    offsetX: Number(offsetX) || 0,
    offsetY: Number(offsetY) || 0,
    /** 缓存的工作区：跨越显示器时才重算（`getDisplayMatching` 每帧调用是拖拽掉帧的主因之一） */
    wa: pickDisplay().workArea,
    /** 上一帧实际落位，用于跳过"位置未变"的冗余 setBounds（透明大窗口每次重合成都不便宜） */
    last: { x: NaN, y: NaN },
    /** 方向判定状态：上一帧光标 x，以及自上次换向以来的水平累计位移 */
    lastX: screen.getCursorScreenPoint().x,
    dirAcc: 0,
    dir: initialDir === "left" || initialDir === "right" ? initialDir : null,
  };
  if (interaction.dir) sendDragDir(interaction.dir);
  // 用户主动移动后不再维持贴边（松手时按位置重新判定）
  if (pref.edge) writePref({ edge: null });

  const tick = () => {
    if (!petWindow || petWindow.isDestroyed()) { endInteraction(false); return; }
    const it = interaction;
    if (!it || it.kind !== "drag") return;
    const p = screen.getCursorScreenPoint();

    // ── 方向判定：按光标**水平累计位移**换向 ──
    // 用增量而非"相对起点的位移"，语义是「跟随当前移动方向」：向右拖即向右走、
    // 向左拖即向左走，中途反向也能立刻跟上（相对起点判定做不到，回拖时符号仍是正的）。
    it.dirAcc += p.x - it.lastX;
    it.lastX = p.x;
    if (it.dirAcc >= DRAG_DIR_DEADZONE) {
      it.dirAcc = 0;
      if (it.dir !== "right") { it.dir = "right"; sendDragDir("right"); }
    } else if (it.dirAcc <= -DRAG_DIR_DEADZONE) {
      it.dirAcc = 0;
      if (it.dir !== "left") { it.dir = "left"; sendDragDir("left"); }
    }

    const x = p.x - it.offsetX;
    const y = p.y - it.offsetY;
    // 仅在光标越出当前缓存工作区时才重新查询显示器（多屏场景）
    const wa = it.wa;
    if (x < wa.x - 40 || x > wa.x + wa.width + 40 || y < wa.y - 40 || y > wa.y + wa.height + 40) {
      it.wa = screen.getDisplayMatching({ x: p.x, y: p.y, width: 1, height: 1 }).workArea;
    }
    const pos = clampWindowPos(x, y, it.wa);
    if (pos.x !== it.last.x || pos.y !== it.last.y) {
      it.last.x = pos.x;
      it.last.y = pos.y;
      // 位置已变才落位；尺寸恒定只传坐标，减少合成开销
      petWindow.setBounds({ x: pos.x, y: pos.y, width: WIN_W, height: WIN_H });
    }
    if (debugOn) log("[debug] drag", { cursor: p, pos, dir: it.dir });
    if (glassDiag.isLeftButtonDown() === false) endInteraction(true);
  };
  tick();
  interactionTimer = setInterval(tick, DRAG_TICK_MS);
  return { ok: true };
}

/** 开始缩放：纵向位移 1:1 映射到宠物显示尺寸（拖 20px ≈ 尺寸变 20px）
 *  只改偏好（渲染层据此调整宠物尺寸与窗口内布局），**窗口尺寸不变**。 */
function beginScale(startScale) {
  if (!petWindow || petWindow.isDestroyed()) return { ok: false };
  endInteraction(false);
  const base = Math.min(Math.max(Number(startScale) || 1, SCALE_MIN), SCALE_MAX);
  interaction = { kind: "scale", startY: null, startScale: base, saveTimer: null };

  const tick = () => {
    if (!petWindow || petWindow.isDestroyed()) { endInteraction(false); return; }
    const it = interaction;
    if (!it || it.kind !== "scale") return;
    const p = screen.getCursorScreenPoint();
    if (it.startY == null) { it.startY = p.y; return; } // 首帧只记录基准，避免跳变
    const delta = it.startY - p.y; // 向上拖 = 变大（与常见窗口缩放手感一致）
    const next = Math.round(
      Math.min(Math.max(it.startScale + delta / PET_BASE_H, SCALE_MIN), SCALE_MAX) * 100
    ) / 100;
    if (next !== pref.scale) {
      pref = { ...pref, scale: next }; // 先改内存：渲染层即时跟随
      broadcastPref();
      if (!it.saveTimer) {
        // 落盘节流：缩放期间每帧写文件太重
        it.saveTimer = setTimeout(() => {
          const cur = interaction;
          if (cur && cur.kind === "scale") cur.saveTimer = null;
          writePref({});
        }, 300);
      }
    }
    if (debugOn) log("[debug] scale", { delta, scale: next });
    if (glassDiag.isLeftButtonDown() === false) endInteraction(true);
  };
  tick();
  interactionTimer = setInterval(tick, DRAG_TICK_MS);
  return { ok: true };
}

/** 推送当前宠物资源给渲染层（切换宠物 / 安装完成后调用） */
function pushAssets() {
  if (!petWindow || petWindow.isDestroyed()) return;
  const slug = safeSlug(pref.slug);
  if (!slug) {
    try { petWindow.webContents.send("pet:assets", { pet: null }); } catch { /* ignore */ }
    return;
  }
  try {
    const info = validatePetPackage(petDir(slug));
    const buf = fs.readFileSync(path.join(petDir(slug), "sprite.webp"));
    petWindow.webContents.send("pet:assets", {
      pet: {
        slug,
        ...info,
        // 以 data URL 下发：渲染层 canvas 需读像素做命中检测，file:// 下跨源会污染画布
        spriteDataUrl: `data:image/webp;base64,${buf.toString("base64")}`,
      },
    });
  } catch (e) {
    logErr("资源下发失败:", e && e.message);
    try { petWindow.webContents.send("pet:assets", { pet: null, error: String(e && e.message) }); } catch { /* ignore */ }
  }
}

/** 按偏好同步窗口可见性（enabled + 未手动隐藏 + 有可用宠物 才显示） */
function syncVisibility() {
  if (!petWindow || petWindow.isDestroyed()) return;
  const slug = safeSlug(pref.slug);
  let usable = false;
  if (slug && pref.enabled && pref.visible !== false) {
    try { validatePetPackage(petDir(slug)); usable = true; } catch { usable = false; }
  }
  if (usable) {
    if (!petWindow.isVisible()) petWindow.showInactive();
  } else if (petWindow.isVisible()) {
    petWindow.hide();
  }
  pushAssets();
}

function loadPetFrontend() {
  const frontDir = app.isPackaged
    ? path.join(process.resourcesPath, "frontend")
    : path.join(__dirname, "..", "client", "dist");
  const file = path.join(frontDir, "pet.html");
  if (fs.existsSync(file)) {
    log("加载宠物页:", file);
    petWindow.loadFile(file);
  } else {
    log("宠物页不存在，尝试 dev server");
    petWindow.loadURL("http://localhost:5173/pet.html");
  }
}

function createPetWindow() {
  if (petWindow && !petWindow.isDestroyed()) return petWindow;
  const pos = resolvePosition();
  petWindow = new BrowserWindow({
    width: WIN_W,
    height: WIN_H,
    x: pos.x,
    y: pos.y,
    transparent: true,
    frame: false,
    hasShadow: false,
    // plan-73-342（半透明色块根治）：以下两项是 Electron **原生**开关，不依赖 koffi：
    //  · thickFrame:false —— 去掉 Windows 标准边框（含 resize 热区），透明窗口上它会被
    //    DWM 合成成一圈半透明贴片；
    //  · roundedCorners:false —— Win11 会给无边框窗口画圆角 + 配套合成，同样留下痕迹。
    // 之前只靠 DWM FFI 清理，而 koffi 缺失时整条链路静默降级（日志里能看到原因），
    // 因此这里必须同时用原生参数兜底。
    thickFrame: false,
    roundedCorners: false,
    resizable: false,
    maximizable: false,
    minimizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    show: false,
    // 不抢焦点：宠物常驻桌面，点开/悬停都不应夺走用户正在输入的焦点
    focusable: false,
    backgroundColor: "#00000000",
    // plan-73-326（修复「浮窗与桌面之间有一块半透明方块」）：必须显式关掉材质。
    // transparent + frameless 窗口下，Electron 默认 backgroundMaterial="auto" 会让 DWM
    // 给**整个窗口矩形**施加材质（Mica/acrylic），在透明区域表现为一块半透明贴片。
    // 主窗代码里已有同类结论："glass off 时显式 none（默认 auto 可能被 DWM 施加 Mica）"。
    backgroundMaterial: "none",
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  try { petWindow.setAlwaysOnTop(true, "screen-saver"); } catch { /* 平台差异，忽略 */ }
  // plan-73-341：清理 DWM 非客户区合成（浮窗周围那圈半透明色块的完整解法）。
  // 仅设 transparent/frame:false/hasShadow:false/backgroundMaterial 不足以消除——
  // DWM 仍会为非客户区画系统边框 + 窗口圆角 + 可能的背景材质。
  applyDwmCleanup("构造期");
  setInteractive(false);
  loadPetFrontend();
  petWindow.once("ready-to-show", () => {
    if (pref.enabled && pref.visible !== false) petWindow.showInactive();
    // 首帧后再兜底一次（与主窗同策略）：部分属性需窗口真正上屏后才生效
    applyDwmCleanup("首帧后");
    pushAssets();
    // 开局同步一次主窗焦点态（waving 状态的输入）
    const mw = ctx.getMainWindow && ctx.getMainWindow();
    try { petWindow.webContents.send("pet:mainFocus", !!(mw && !mw.isDestroyed() && mw.isFocused())); } catch { /* ignore */ }
  });
  petWindow.on("closed", () => {
    endInteraction(false);
    petWindow = null;
  });
  log("宠物窗口已创建", pos, `${WIN_W}x${WIN_H}`);
  return petWindow;
}

/** DWM 清理的日志包装（构造期 / 首帧后各调一次，结果写日志便于核对） */
function applyDwmCleanup(stage) {
  if (!petWindow || petWindow.isDestroyed()) return;
  try {
    const r = glassDiag.applyFramelessCleanup(petWindow);
    log(`DWM 清理(${stage}):`, JSON.stringify(r.steps || r));
  } catch (e) {
    logErr(`DWM 清理(${stage})失败（不影响使用）:`, e && e.message);
  }
}

function destroyPetWindow() {
  if (petWindow && !petWindow.isDestroyed()) petWindow.destroy();
  petWindow = null;
}

/** 主窗钩子：受偏好控制跟随最小化/全屏隐藏（默认最小化跟随，全屏不隐藏） */
function hookMainWindow() {
  if (mainHooked) return;
  const mw = ctx.getMainWindow && ctx.getMainWindow();
  if (!mw || mw.isDestroyed()) return;
  mainHooked = true;
  mw.on("minimize", () => { if (pref.hideOnMinimize && petWindow && !petWindow.isDestroyed()) petWindow.hide(); });
  mw.on("restore", () => { if (pref.enabled) syncVisibility(); });
  mw.on("enter-full-screen", () => { if (pref.hideOnFullscreen && petWindow && !petWindow.isDestroyed()) petWindow.hide(); });
  mw.on("leave-full-screen", () => { if (pref.enabled) syncVisibility(); });
  mw.on("focus", () => {
    if (petWindow && !petWindow.isDestroyed()) {
      try { petWindow.setAlwaysOnTop(true, "screen-saver"); } catch { /* ignore */ }
      // plan-73-326：把主窗焦点态推给宠物页（waving = 主窗未聚焦时的完成提醒）
      try { petWindow.webContents.send("pet:mainFocus", true); } catch { /* ignore */ }
    }
  });
  mw.on("blur", () => {
    if (petWindow && !petWindow.isDestroyed()) {
      try { petWindow.webContents.send("pet:mainFocus", false); } catch { /* ignore */ }
    }
  });
  // 主窗关闭 = 应用退出：宠物窗口常驻会让 window-all-closed（"所有窗口已关闭"）永不成立，
  // 因此必须在此显式销毁宠物窗口，才能让既有退出语义（window-all-closed → killBackend → quit）继续成立。
  mw.on("closed", () => { destroyPetWindow(); });
}

// ─────────────────────────── IPC 注册 ───────────────────────────

function registerPetIpc(options) {
  if (registered) return;
  registered = true;
  ctx = {
    log: (options && options.log) || (() => {}),
    logErr: (options && options.logErr) || (() => {}),
    getMainWindow: (options && options.getMainWindow) || (() => null),
    getBackendPort: (options && options.getBackendPort) || (() => 12973),
  };
  pref = readPref();

  // 宠物窗口启动时一次性取全：偏好 + 当前宠物资源 + 已安装列表 + 后端端口 + 标题缓存
  ipcMain.handle("pet:getBoot", () => {
    const installed = listInstalled();
    const slug = safeSlug(pref.slug) || (installed[0] && installed[0].slug) || "";
    let pet = null;
    if (slug) {
      try {
        const info = validatePetPackage(petDir(slug));
        const buf = fs.readFileSync(path.join(petDir(slug), "sprite.webp"));
        pet = { slug, ...info, spriteDataUrl: `data:image/webp;base64,${buf.toString("base64")}` };
      } catch (e) {
        logErr("getBoot 资源读取失败:", e && e.message);
      }
    }
    return {
      pref: { ...pref },
      pet,
      installed: installed.map(({ slug: s, displayName, rows, bytes, broken }) => ({ slug: s, displayName, rows, bytes, broken: !!broken })),
      backendPort: ctx.getBackendPort(),
      // plan-73-341：把持久化的会话标题随首屏下发 —— 冷启动即可显示真实标题，
      // 不再依赖"标题事件恰好被收到"（那是一条一次性、静默失败的链路）。
      titles: readTitles(),
    };
  });

  ipcMain.handle("pet:getPref", () => ({ ...pref }));

  ipcMain.handle("pet:setPref", (_e, patch) => {
    const before = { ...pref };
    const next = writePref(patch || {});
    broadcastPref();
    // 副作用分流：只有真正影响窗口的字段才动窗口
    if (next.enabled !== before.enabled || next.slug !== before.slug) {
      if (next.enabled && safeSlug(next.slug)) {
        createPetWindow();
        hookMainWindow();
        syncVisibility();
      } else {
        syncVisibility();
      }
    }
    if (next.clickThrough !== before.clickThrough) setInteractive(interactive);
    // 手动隐藏/恢复
    if (next.visible !== before.visible) syncVisibility();
    // 注：scale 变化不再动窗口 —— 窗口尺寸恒定（plan-73-341），
    //     宠物尺寸由渲染层按同一 scale 在窗口内自适应。
    return { ...pref };
  });

  ipcMain.handle("pet:listInstalled", () => listInstalled());

  // 会话快照 / 引擎步骤：宠物页以 file:// 加载，向 http://127.0.0.1 发 fetch 属跨源（Origin: null），
  // 会被 CORS 拦截；WebSocket 无此限制（仍由渲染层直连），故 REST 一律由主进程 net.fetch 代取。
  ipcMain.handle("pet:snapshot", async () => {
    const port = ctx.getBackendPort();
    const res = await net.fetch(`http://127.0.0.1:${port}/api/sessions`, { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();
    // plan-73-341：顺手把 id → title 写入持久缓存（下次冷启动直接可用）
    try {
      const map = {};
      for (const s of Array.isArray(list) ? list : []) {
        if (s && s.id != null && typeof s.title === "string" && s.title) map[s.id] = s.title;
      }
      mergeTitles(map);
    } catch { /* 缓存写入失败不影响本次返回 */ }
    return list;
  });

  ipcMain.handle("pet:tasks", async (_e, sessionId) => {
    const sid = Number(sessionId);
    if (!Number.isFinite(sid) || sid <= 0) return [];
    const port = ctx.getBackendPort();
    const res = await net.fetch(`http://127.0.0.1:${port}/turns/sessions/${sid}/tasks`, { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  });

  ipcMain.handle("pet:listManifest", async (_e, opts) => {
    const force = !!(opts && opts.force);
    const man = await fetchManifest(force);
    const installedSlugs = new Set(listInstalled().map((p) => p.slug));
    const kw = String((opts && opts.query) || "").trim().toLowerCase();
    let pets = man.pets.map((p) => ({
      slug: p.slug,
      displayName: p.displayName || p.slug,
      kind: p.kind || "",
      author: p.submittedBy || "",
      // 设置页图库缩略图：petdex 每只宠物都有 preview.webp（体积远小于精灵图，
      // 实测可直接访问；加载失败时前端回退到精灵图首帧裁剪）。
      previewUrl: `https://assets.petdex.dev/pets/${encodeURIComponent(p.slug)}/preview.webp`,
      spritesheetUrl: p.spritesheetUrl || "",
      installed: installedSlugs.has(p.slug),
    }));
    if (kw) {
      pets = pets.filter((p) => p.slug.toLowerCase().includes(kw) || p.displayName.toLowerCase().includes(kw));
    }
    return { total: man.total || pets.length, shown: pets.length, pets: pets.slice(0, 300), fromCache: !!man.fromCache, stale: !!man.stale, error: man.error || null };
  });

  ipcMain.handle("pet:install", async (_e, slug) => installPet(slug));

  ipcMain.handle("pet:remove", (_e, slug) => removePet(slug));

  ipcMain.handle("pet:importLocal", async () => importLocalPet(ctx.getMainWindow()));

  ipcMain.handle("pet:openPetPage", (_e, slug) => {
    const s = safeSlug(slug);
    if (!s) return false;
    void shell.openExternal(PETDEX_PET_URL(s));
    return true;
  });

  ipcMain.handle("pet:revealPet", (_e, slug) => {
    const s = safeSlug(slug);
    if (!s) return false;
    const dir = petDir(s);
    if (!fs.existsSync(dir)) return false;
    shell.showItemInFolder(path.join(dir, "sprite.webp"));
    return true;
  });

  // 宠物窗口侧：交互态 / 拖拽 / 缩放 / 跳转
  // 注（plan-73-341）：窗口尺寸恒定，故不再有「形态切换」IPC；
  // 拖拽与缩放改为主进程自循环（见 beginDrag / beginScale 的说明）。
  ipcMain.handle("pet:setInteractive", (_e, on) => { setInteractive(on); return interactive; });

  /** 光标在窗口客户区内的坐标（DIP，与页面 CSS 像素一致；窗口已销毁返回 null）。
   *  用途（plan-73-344）：渲染层做「悬停态兜底」。透明穿透窗口在用户**快速移出**时
   *  会丢失 mouseleave / mousemove，最后坐标可能仍停在浮窗矩形内 → 浮窗卡在展开态。
   *  定时查询真实光标位置，即可在**不依赖任何鼠标事件**的前提下校正。 */
  ipcMain.handle("pet:cursorClient", () => {
    if (!petWindow || petWindow.isDestroyed()) return null;
    try {
      const p = screen.getCursorScreenPoint();
      const b = petWindow.getBounds();
      return { x: p.x - b.x, y: p.y - b.y };
    } catch {
      return null;
    }
  });

  /** 拖拽开始：传入「鼠标在窗口客户区内的偏移」（抓取点语义）与启动帧的初判方向。
   *  之后主进程自己轮询光标定位窗口并持续判定行走方向，渲染层不再上报任何坐标 ——
   *  从根上避免"跨进程坐标换算误差"、"合成事件反馈环"与"拖拽期间事件停发导致方向冻结"。 */
  ipcMain.handle("pet:dragBegin", (_e, payload) => {
    const p = payload || {};
    return beginDrag(p.offsetX, p.offsetY, p.initialDir);
  });

  /** 拖拽提前收尾（渲染层监听到 pointerup 时调用；主进程也会自行判定按键松开收尾） */
  ipcMain.handle("pet:interactionEnd", () => { endInteraction(true); return true; });

  /** 缩放开始：只改偏好（渲染层据此调宠物尺寸），窗口尺寸不变 */
  ipcMain.handle("pet:scaleBegin", (_e, startScale) => beginScale(startScale));

  /** 停止会话运行（浮窗块右侧「停止」角标）：转发后端取消接口。
   * 与会话级 WS 的 cancel 事件同一条链路（engine.cancel_turn），
   * 走 REST 是因为浮窗只对主任务开了会话连接，而块可能属于任意会话。 */
  ipcMain.handle("pet:cancelTurn", async (_e, turnId) => {
    const tid = Number(turnId);
    if (!Number.isFinite(tid) || tid <= 0) return { ok: false, error: "invalid_turn" };
    try {
      const port = ctx.getBackendPort();
      const res = await net.fetch(`http://127.0.0.1:${port}/turns/${tid}/cancel`, {
        method: "POST",
        signal: AbortSignal.timeout(8000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json().catch(() => ({}));
      return { ok: true, result: data };
    } catch (e) {
      logErr("停止会话失败:", e && e.message);
      return { ok: false, error: String(e && e.message) };
    }
  });

  ipcMain.handle("pet:focusSession", (_e, sessionId) => {
    const mw = ctx.getMainWindow && ctx.getMainWindow();
    if (!mw || mw.isDestroyed()) return false;
    try {
      if (mw.isMinimized()) mw.restore();
      mw.show();
      mw.focus();
      mw.webContents.send("app:focusSession", sessionId);
    } catch (e) {
      logErr("跳转会话失败:", e && e.message);
      return false;
    }
    return true;
  });

  // 隐藏手柄（qoder 式角标）：写偏好 + 广播，任务运行中同样可隐藏；设置页可恢复
  ipcMain.handle("pet:hideTemporarily", () => {
    writePref({ visible: false });
    broadcastPref();
    if (petWindow && !petWindow.isDestroyed()) petWindow.hide();
    return true;
  });

  ipcMain.handle("pet:showPet", () => {
    writePref({ visible: true });
    broadcastPref();
    if (petWindow && !petWindow.isDestroyed()) {
      createPetWindow();
      hookMainWindow();
      syncVisibility();
    } else {
      // 窗口已被销毁（重新启用场景）：重建
      if (pref.enabled && safeSlug(pref.slug)) {
        createPetWindow();
        hookMainWindow();
        syncVisibility();
      }
    }
    return true;
  });

  // 面板底部齿轮：请求主窗打开「设置 → 宠物」分区（主窗尚未监听时静默无效，不影响使用）
  ipcMain.handle("pet:openSettings", () => {
    const mw = ctx.getMainWindow && ctx.getMainWindow();
    if (!mw || mw.isDestroyed()) return false;
    try {
      if (mw.isMinimized()) mw.restore();
      mw.show();
      mw.focus();
      mw.webContents.send("app:openPetSettings");
    } catch (e) {
      logErr("打开设置失败:", e && e.message);
      return false;
    }
    return true;
  });

  log("IPC 已注册");
}

// ─────────────────────────── 导出 ───────────────────────────

module.exports = {
  registerPetIpc,
  createPetWindow,
  destroyPetWindow,
  hookMainWindow,
  syncVisibility,
  pushAssets,
  getPref: () => ({ ...pref }),
  isEnabled: () => !!(pref && pref.enabled && pref.visible !== false && safeSlug(pref.slug)),
};
