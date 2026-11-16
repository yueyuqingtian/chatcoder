/**
 * 社区微应用的发布与撤回（GitHub issue 驱动）—— Electron 主进程侧
 * 依据：plan-693-2868 §3.4「B 方案的完整链路」/ §6.4「社区：共享仓库 + issue 驱动」
 *      / D20（PAT 与 GitHub 写操作都在主进程）/ D32（写入由 owner 的 Actions 执行）
 *      / D34（issue 驱动）/ D36（首次发布弹窗引导 PAT）/ D37（不内置 owner 写 token）
 *
 * 职责边界：
 *   · 本模块**只**做「用户侧」的事：拿用户自己的 PAT，把包上传到**用户自己的**中转仓库，
 *     再向社区仓库提一个 `[upload]` / `[withdraw]` issue，然后轮询 issue 被 Actions 关闭。
 *   · 写共享仓库 `apps.json` 的是社区仓库里的 `.github/workflows/accept.yml`（D32/D38），
 *     本模块**没有**、也永远不会有共享仓库的写权限（D37）。
 *   · issue 正文的 json 块必须能被 accept.yml 的 awk + jq 解析，字段契约见 docs/community-template。
 *
 * 安全要求（§6.4 末条）：
 *   PAT 只在本模块内存与 `<userData>/gh-token.enc`（或降级的 `.plain`）里，
 *   不进 Python 进程、不写日志、不回显给渲染层。任何错误信息都只带状态码与 GitHub 返回的 message；
 *   需要提到 token 时只允许用 maskToken() 打前 4 位。见下方 assertNoTokenLeak 的用法约定。
 *
 * 依赖：只用 Node 内置（https / crypto / fs / path / os）+ Electron 的 safeStorage，
 *      不引入任何 npm 包（方案要求「零新增依赖」）。
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const https = require("https");
const crypto = require("crypto");

const secure = require("./secure-file.cjs");

// ── 常量 ──────────────────────────────────────────────────────────────────────
const API_HOST = "api.github.com";
const UPLOAD_HOST = "uploads.github.com";
const USER_AGENT = "chatcoder";
/** 中转仓库固定名字：发布成功后会被删除，只是「把包临时放到一个 GitHub 能下载到的地方」。 */
const STAGING_REPO = "chatcoder-microapp-staging";
const RELEASE_TAG_PREFIX = "staging";
/** 接口超时 30s；上传单独放宽到 600s（D29 / §6.4 体积表：100MB 包在慢网下可能远超 30s）。 */
const API_TIMEOUT_MS = 30_000;
const UPLOAD_TIMEOUT_MS = 600_000;
/** 轮询：5s 一次、最多 60 次 = 5 分钟（方案 §9 待确认第 3 条给的默认值）。 */
const POLL_INTERVAL_MS = 5_000;
const POLL_MAX_ATTEMPTS = 60;
/** 429/5xx 的退避序列：2s、4s、8s，最多重试 3 次。 */
const RETRY_DELAYS_MS = [2_000, 4_000, 8_000];
/**
 * 实际使用的退避序列。单测（electron/community-github.test.cjs）会把它压成 [0,0,0]，
 * 否则一个「5xx 重试」用例要真等 14 秒。生产路径从不修改它。
 */
let retryDelays = RETRY_DELAYS_MS.slice();
/** 包上限 100MB，与 accept.yml 的 size 校验、D18 保持一致。 */
const MAX_PACKAGE_BYTES = 100 * 1024 * 1024;
/** appId / version 约束（D26 + accept.yml 的正则）：本地先拦一次，免得白传 100MB 再被 Actions 拒。 */
const APP_ID_RE = /^[a-z0-9][a-z0-9._-]{1,62}$/;
const VERSION_RE = /^\d+\.\d+\.\d+$/;
const REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9._-]+$/;

// 本地文件名（§6.4 明确指定了 gh-token.enc 这个名字）
const PREF_FILE = "community-pref.json";
const TOKEN_ENC_FILE = "gh-token.enc";
const TOKEN_PLAIN_FILE = "gh-token.plain";

// ── 结构化错误 ────────────────────────────────────────────────────────────────

/**
 * 统一的失败载体：既有 Error 语义（可 throw / 有 message / 有堆栈），
 * 又把机器可读的 code 挂在属性上，供 IPC 层转成 `{ok:false, code, message}` 给渲染层。
 * 约定：message 里**绝不**出现 token 原文（本模块内所有 throw 点都遵守）。
 * @param {string} code 机器可读错误码（NO_TOKEN / BAD_TOKEN / TIMEOUT / ...）
 * @param {string} message 给用户看的中文说明
 * @param {object} [extra] 附加字段（status、issueUrl、retryable 等）
 */
function communityError(code, message, extra) {
  const err = new Error(message);
  err.code = code;
  if (extra && typeof extra === "object") Object.assign(err, extra);
  return err;
}

/** 判断是否本模块的结构化错误（IPC 层据此区分「预期内的业务失败」与真正的程序异常）。 */
function isCommunityError(e) {
  return Boolean(e && typeof e.code === "string");
}

// ── token 脱敏 ────────────────────────────────────────────────────────────────

/**
 * token 唯一允许出现在文本里的形式：前 4 位 + 省略号。
 * 例：maskToken("ghp_abcdefghijklmnop") => "ghp_…"
 * @param {string} token
 * @returns {string}
 */
function maskToken(token) {
  const s = String(token == null ? "" : token);
  if (!s) return "（空）";
  return `${s.slice(0, 4)}…`;
}

/**
 * 断言文本里没有泄露 token 原文，返回脱敏后的文本。
 * 用在「可能把外部返回体拼进错误信息」的地方做最后一道保险：
 * 万一 GitHub 的响应体里回显了 Authorization（不该发生），也不会被打进日志/界面。
 * @param {string} text
 * @param {string} token
 * @returns {string}
 */
function redactToken(text, token) {
  const s = String(text == null ? "" : text);
  const t = String(token == null ? "" : token);
  if (!t || t.length < 8) return s;
  return s.split(t).join(maskToken(t));
}

// ── 本地目录与偏好 ────────────────────────────────────────────────────────────

/**
 * 社区配置目录。
 * 顺序：环境变量 CHATCODER_COMMUNITY_DIR（测试与调试用）→ Electron userData（§6.4 指定的落点）
 *      → `~/.chatcoder`（非 Electron 环境，例如 node 单测直接 require 本模块）。
 */
function dataDir() {
  const fromEnv = String(process.env.CHATCODER_COMMUNITY_DIR || "").trim();
  if (fromEnv) return path.resolve(fromEnv);
  try {
    const electron = require("electron");
    // 非 Electron 运行时 require("electron") 返回可执行文件路径字符串，不是对象（同 microapp-root.cjs 的写法）
    if (electron && typeof electron === "object" && electron.app) return electron.app.getPath("userData");
  } catch { /* 单测 / 纯 node 环境走下面的兜底 */ }
  return path.join(os.homedir(), ".chatcoder");
}

function prefFile() { return path.join(dataDir(), PREF_FILE); }
function tokenEncFile() { return path.join(dataDir(), TOKEN_ENC_FILE); }
function tokenPlainFile() { return path.join(dataDir(), TOKEN_PLAIN_FILE); }

/**
 * 仅测试用：注入 safeStorage 替身。
 * Electron 之外（node 单测）拿不到真实的 safeStorage，而「加密档」是生产主路径，必须有办法覆盖。
 * 生产路径从不调用它。
 */
let safeStorageOverride = null;

/** 取 Electron safeStorage；不可用时返回 null（单测环境）。 */
function safeStorageModule() {
  if (safeStorageOverride) return safeStorageOverride;
  try {
    const electron = require("electron");
    if (electron && typeof electron === "object" && electron.safeStorage) return electron.safeStorage;
  } catch { /* ignore */ }
  return null;
}

/**
 * 规范化社区仓库地址：接受 `owner/repo` 或完整 GitHub URL，空串表示社区关闭（§7 P5-8）。
 * @param {string} input
 * @returns {string} 规范化后的 `owner/repo`，或空串
 */
function normalizeRepo(input) {
  const raw = String(input == null ? "" : input).trim();
  if (!raw) return ""; // 留空 = 关闭社区入口（P5-8），不是错误
  const stripped = raw
    .replace(/^https?:\/\/(www\.)?github\.com\//i, "")
    .replace(/\.git$/i, "")
    .replace(/\/+$/, "");
  if (!REPO_RE.test(stripped)) {
    throw communityError("INVALID_REPO", `社区仓库地址必须形如 owner/repo：${raw}`);
  }
  return stripped;
}

/** 读社区仓库地址；未配置返回空串（不抛错，渲染层据此隐藏入口）。 */
function getRepo() {
  const pref = secure.readPrivateJson(prefFile());
  return pref && typeof pref.repo === "string" ? pref.repo : "";
}

/** 写社区仓库地址；返回规范化后的值。 */
function setRepo(ownerRepo) {
  const repo = normalizeRepo(ownerRepo);
  const pref = secure.readPrivateJson(prefFile()) || {};
  pref.repo = repo;
  secure.writePrivateJson(prefFile(), pref);
  return repo;
}

/**
 * token 状态：**只**返回「有没有」与「加没加密」，永不返回 token 本身（§6.4 末条）。
 * encrypted=false 且 saved=true 时，渲染层必须明示「本机未启用系统加密，PAT 以明文存放」。
 * @returns {{saved: boolean, encrypted: boolean}}
 */
function getTokenStatus() {
  const enc = fs.existsSync(tokenEncFile());
  const plain = fs.existsSync(tokenPlainFile());
  return { saved: enc || plain, encrypted: enc };
}

/**
 * 保存 PAT。
 *
 * 路径 ①（首选）：`safeStorage.isEncryptionAvailable()` 为真 → encryptString 后写 `gh-token.enc`。
 *   加密由操作系统提供（Windows DPAPI / macOS Keychain / Linux libsecret），文件被单独拷走也无法解密，
 *   所以这条路径下 `getTokenStatus().encrypted = true`。
 *
 * 路径 ②（降级）：`isEncryptionAvailable()` 为假（Linux 没装 keyring、精简 Windows 环境、
 *   某些远程会话等）。此时**不静默丢弃用户的 PAT**，否则「填了也白填」比明文更糟；
 *   改为写 `gh-token.plain`（仍然 0600），并把 `encrypted=false` 交给 UI 明示风险。
 *   这是方案 §6.4「isEncryptionAvailable() 为 false 时走明文 + 0600 + UI 明示」的落地。
 *
 * @param {string} token
 * @returns {{saved: boolean, encrypted: boolean}}
 */
function setToken(token) {
  const raw = String(token == null ? "" : token).trim();
  if (!raw) throw communityError("EMPTY_TOKEN", "PAT 不能为空");
  if (/\s/.test(raw)) throw communityError("INVALID_TOKEN", "PAT 中不能有空白字符（粘贴时容易带上末尾换行）");
  if (raw.length < 20 || raw.length > 255) {
    throw communityError("INVALID_TOKEN", "PAT 长度不像有效令牌（应为 20~255 字符）");
  }

  const ss = safeStorageModule();
  if (ss && typeof ss.isEncryptionAvailable === "function" && ss.isEncryptionAvailable()) {
    const buf = ss.encryptString(raw);
    // 二进制写入 + 0600：复用 secure-file 的 tmp+rename 原子写，避免写一半留下坏文件
    secure.writePrivateFile(tokenEncFile(), buf);
    secure.removePrivateFile(tokenPlainFile()); // 清掉历史降级留下的明文
    return { saved: true, encrypted: true };
  }

  secure.writePrivateFile(tokenPlainFile(), raw);
  secure.removePrivateFile(tokenEncFile());
  return { saved: true, encrypted: false };
}

/**
 * 读 PAT（仅本模块内部使用；任何地方都不得把返回值交给渲染层或写日志）。
 * 优先加密档；加密档存在但解不开时明确报错，而不是静默当「没填过」——
 * 后者会让用户反复重填也无效，排查困难。
 * @returns {string} 空串表示未保存
 */
function readToken() {
  const enc = tokenEncFile();
  if (fs.existsSync(enc)) {
    const ss = safeStorageModule();
    if (!ss || typeof ss.decryptString !== "function") {
      throw communityError("TOKEN_UNREADABLE", "本机系统加密不可用，无法解密已保存的 PAT，请重新填写");
    }
    const buf = secure.readPrivateFile(enc);
    if (!buf || !buf.length) throw communityError("TOKEN_UNREADABLE", "PAT 存档为空，请重新填写");
    let out = "";
    try {
      out = ss.decryptString(buf);
    } catch {
      throw communityError("TOKEN_UNREADABLE", "PAT 解密失败（系统密钥可能已变更），请重新填写");
    }
    const s = String(out || "").trim();
    if (!s) throw communityError("TOKEN_UNREADABLE", "PAT 存档内容为空，请重新填写");
    return s;
  }

  const plain = tokenPlainFile();
  if (fs.existsSync(plain)) {
    const buf = secure.readPrivateFile(plain);
    const s = buf ? buf.toString("utf8").trim() : "";
    if (s) return s;
  }
  return "";
}

/** 清除 PAT（两个文件一起删，避免降级/加密两种历史状态残留一个）。 */
function clearToken() {
  secure.removePrivateFile(tokenEncFile());
  secure.removePrivateFile(tokenPlainFile());
  return { saved: false, encrypted: false };
}

// ── manifest → issue 字段（纯逻辑，便于单测）────────────────────────────────

/** 把可能是字符串或 {name} 的数组元素统一成字符串数组。 */
function toStringArray(value) {
  if (!Array.isArray(value)) return [];
  const out = [];
  for (const item of value) {
    if (typeof item === "string" && item) out.push(item);
    else if (item && typeof item === "object") {
      const name = item.name || item.id || item.key;
      if (typeof name === "string" && name) out.push(name);
    }
  }
  return out;
}

/** vendor 归一为 [{name, version}]：apps.json 里就是这个结构（见 docs/community-template/README.md）。 */
function normalizeVendors(value) {
  if (!Array.isArray(value)) return [];
  const out = [];
  for (const item of value) {
    if (typeof item === "string" && item) out.push({ name: item, version: "" });
    else if (item && typeof item === "object" && item.name) {
      out.push({ name: String(item.name), version: String(item.version || "") });
    }
  }
  return out;
}

/**
 * 从 manifest 里挑出需要随 issue 一起提交的元信息。
 * manifest 的权威定义在 P0（另有人负责），这里**容错读取**多个可能字段名，
 * 拿不到就给安全默认值，不因为字段名不一致而让发布会失败。
 * @param {object} manifest
 * @param {string} appId
 * @param {string} [descOverride] 用户这次填的说明（优先于 manifest 里的）
 */
function manifestSummary(manifest, appId, descOverride) {
  const m = manifest && typeof manifest === "object" ? manifest : {};
  const name = String(m.name || m.title || appId || "").trim() || String(appId || "");
  const rawDesc = typeof descOverride === "string" && descOverride.trim()
    ? descOverride
    : (m.description || m.desc || "");
  // desc 里若出现 ``` 会把 fenced json 块截断，Actions 的 awk 就解析不到 payload —— 必须先清理。
  const desc = String(rawDesc || "").replace(/```+/g, "ˋˋˋ").trim().slice(0, 500);
  const perms = toStringArray(m.permissions || m.perms);
  const methods = toStringArray(m.methods || (m.api && m.api.methods) || (m.runtime && m.runtime.methods));
  const vendors = normalizeVendors(m.vendors || (m.runtime && m.runtime.vendors));
  return { name, desc, perms, methods, vendors };
}

/**
 * 拼 [upload] issue 的 json payload。
 * 字段与 docs/community-template/.github/workflows/accept.yml 逐条对应：
 *   appId / version  → 正则校验 + tag 拼接原料
 *   downloadUrl      → 必须是 https 的 github.com 地址（browser_download_url 满足）
 *   sha256           → 必须 64 位小写十六进制
 *   size             → 1 ~ 104857600
 *   其余字段（name/desc/perms/methods/vendors/authorId/authorName）原样进 apps.json 条目。
 */
function buildUploadPayload(input) {
  const appId = String(input.appId || "").trim();
  const version = String(input.version || "").trim();
  const summary = manifestSummary(input.manifest, appId, input.desc);
  return {
    appId,
    version,
    name: summary.name,
    desc: summary.desc,
    size: Number(input.size) || 0,
    sha256: String(input.sha256 || "").toLowerCase(),
    downloadUrl: String(input.downloadUrl || ""),
    authorId: String(input.authorId || ""),
    authorName: String(input.authorName || ""),
    perms: summary.perms,
    methods: summary.methods,
    vendors: summary.vendors,
  };
}

/** 拼 [withdraw] issue 的 json payload（Actions 侧只需要 appId + version）。 */
function buildWithdrawPayload(input) {
  return {
    appId: String(input.appId || "").trim(),
    version: String(input.version || "").trim(),
    authorId: String(input.authorId || ""),
    authorName: String(input.authorName || ""),
    reason: String(input.reason || "").replace(/```+/g, "ˋˋˋ").slice(0, 300),
  };
}

/**
 * 拼 issue 正文。
 * 硬约束：必须包含**一个** ```json 围栏块，且围栏行自成一行（accept.yml 用
 * `awk '/^```json[[:space:]]*$/{f=1;next} /^```[[:space:]]*$/{if(f){exit}} f'` 抓取）。
 * 所以：
 *   1. json 块放在最前面，避免正文里其他内容干扰 awk 的状态机；
 *   2. 用户可控的 desc / reason 里的 ``` 已在上面被替换掉，不会提前闭合围栏。
 * @param {"upload"|"withdraw"} kind
 * @param {object} payload
 */
function buildIssueBody(kind, payload) {
  const json = JSON.stringify(payload, null, 2);
  const who = payload.authorName
    ? `${payload.authorName}（${payload.authorId || "无 id"}）`
    : String(payload.authorId || "未署名");
  const lines = [
    "```json",
    json,
    "```",
    "",
    `> 由 ChatCoder 自动提交（plan-693-2868 §3.4 / D34）。动作：${kind === "withdraw" ? "撤回" : "发布"}。`,
    `> 作者：${who}`,
    "> Actions 会校验字段、哈希与体积后自动处理并关闭本 issue；**请不要手工编辑上面的 json 块**。",
  ];
  if (kind === "withdraw") {
    lines.push("> 撤回只从 apps.json 移除条目，**包不会删除**（D35），已下载的链接继续有效。");
  }
  return lines.join("\n");
}

/** issue 标题：`[upload] <appId>@<version>` / `[withdraw] <appId>@<version>`（accept.yml 靠前缀分派）。 */
function buildIssueTitle(kind, appId, version) {
  return `[${kind}] ${appId}@${version}`;
}

// ── 轮询判定（纯逻辑，便于单测）──────────────────────────────────────────────

/**
 * 根据 issue 最后一条评论判断 Actions 给的是「成功」还是「拒绝」。
 * accept.yml 的回执文案是固定几句（见 workflow 第 91/98/155/187/196 行），这里按关键词分类；
 * 拿不准就返回 unknown —— 交给用户点开 issue 自己看，绝不假装成功。
 * @param {string} comment 最后一条评论正文
 * @returns {"ok"|"rejected"|"unknown"}
 */
function classifyOutcome(comment) {
  const text = String(comment || "");
  if (!text) return "unknown";
  // 先判失败：失败回执里也会出现「已发布」这类词之外的关键词，顺序反了会把失败当成功
  if (/处理失败|不允许覆盖|已存在|没有找到|不合法|不符|超出|超限|失败/.test(text)) return "rejected";
  if (/已发布|已从索引移除|下载地址/.test(text)) return "ok";
  return "unknown";
}

/**
 * 轮询终止判定（D40：发布结果以 issue 状态为准）。
 * 终止条件只有两个：① issue 已 closed；② 尝试次数用尽。
 * 次数用尽返回 timedOut=true，**不算失败** —— issue 已经提交成功了，只是 Actions 还没跑完，
 * 用户稍后去网页看即可（方案 §3.4 第 5 点）。
 * @param {{state?: string, comments?: Array<{body?: string}>}} snapshot pollIssue 的结果
 * @param {{attempt: number, maxAttempts: number, kind?: "upload"|"withdraw"}} options
 */
function evaluatePoll(snapshot, options) {
  const attempt = Number(options && options.attempt) || 1;
  const maxAttempts = Number(options && options.maxAttempts) || POLL_MAX_ATTEMPTS;
  const kind = (options && options.kind) === "withdraw" ? "withdraw" : "upload";
  const comments = snapshot && Array.isArray(snapshot.comments) ? snapshot.comments : [];
  const lastComment = comments.length ? String(comments[comments.length - 1].body || "") : "";
  const state = String((snapshot && snapshot.state) || "");

  if (state === "closed") {
    const outcome = classifyOutcome(lastComment);
    const status = outcome === "ok"
      ? (kind === "withdraw" ? "withdrawn" : "published")
      : (outcome === "rejected" ? "rejected" : "unknown");
    return { done: true, timedOut: false, state, outcome, status, lastComment };
  }
  if (attempt >= maxAttempts) {
    return { done: true, timedOut: true, state: state || "open", outcome: "unknown", status: "pending", lastComment };
  }
  return { done: false, timedOut: false, state: state || "open", outcome: "unknown", status: "pending", lastComment };
}

// ── HTTP 底层 ────────────────────────────────────────────────────────────────

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 本模块所有请求共用的请求头。注意：headers 里含 token，**永远不要整体打印**。 */
function headersFor(token, extra) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": USER_AGENT,
    "X-GitHub-Api-Version": "2022-11-28",
    ...(extra && typeof extra === "object" ? extra : {}),
  };
}

function safeParseJson(text) {
  try { return JSON.parse(String(text || "")); } catch { return null; }
}

/** 429 与 5xx 可重试；其余（含 401/403/404/422）立即失败。 */
function isRetryableStatus(status) {
  return status === 429 || status >= 500;
}

/**
 * 把 HTTP 失败响应映射成结构化错误。
 * 401 / 403 → BAD_TOKEN（D36 的「PAT 失效要有明确提示」）；
 * 其余按状态码给出可读 code，message 里带 GitHub 的 message 便于用户判断。
 * 注意：message 只从这里拼（状态码 + 截断后的响应体），响应体是 GitHub 返回的，不含我们的 token；
 *       仍走一次 redactToken 兜底。
 */
function httpFailure(status, text, hint, token) {
  const body = safeParseJson(text);
  const detail = redactToken(String((body && body.message) || text || "").slice(0, 300), token);
  const where = hint || "GitHub 请求";
  if (status === 401 || status === 403) {
    return communityError("BAD_TOKEN", `${where}被拒绝（HTTP ${status}）：PAT 无效、过期或权限不足（需 public_repo）。${detail}`.trim(), { status });
  }
  if (status === 404) {
    return communityError("NOT_FOUND", `${where}失败（HTTP 404）：${detail || "对象不存在或无权访问"}`, { status });
  }
  if (status === 422) {
    return communityError("REJECTED", `${where}失败（HTTP 422）：${detail || "GitHub 拒绝了这个请求"}`, { status });
  }
  return communityError(`HTTP_${status}`, `${where}失败（HTTP ${status}）：${detail || "未知原因"}`, { status });
}

/**
 * 把底层网络异常（DNS/连接重置/证书等）包成结构化错误，
 * 让 IPC 层能给出 code:NETWORK 而不是一条裸的 ENOTFOUND。
 * 已经是结构化错误（带 code，例如 TIMEOUT_LOCAL）就原样返回。
 */
function networkFailure(e, hint) {
  if (e && typeof e.code === "string") return e;
  const detail = String((e && e.message) || e || "").slice(0, 200);
  return communityError("NETWORK", `${hint || "请求"}失败：无法连接 GitHub（${detail}）`, { retryable: true });
}

/**
 * 发一次请求，拿回 {status, text}（不判断成败，交给调用方）。
 * 超时用 req.destroy(自定义 Error) 实现：既断开连接，又把「超时」这个语义交给 reject。
 */
function requestOnce({ method = "GET", host = API_HOST, path: reqPath, headers = {}, body = null, timeoutMs = API_TIMEOUT_MS }) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer = null;
    const finish = (fn, arg) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      fn(arg);
    };

    const req = https.request({ method, host, path: reqPath, headers }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => finish(resolve, {
        status: res.statusCode || 0,
        headers: res.headers || {},
        text: Buffer.concat(chunks).toString("utf8"),
      }));
      res.on("error", (e) => finish(reject, e));
    });

    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        finish(reject, communityError("TIMEOUT_LOCAL", `请求超时（${Math.round(timeoutMs / 1000)}s）：${method} ${reqPath}`, { retryable: true }));
        try { req.destroy(); } catch { /* ignore */ }
      }, timeoutMs);
    }

    req.on("error", (e) => finish(reject, e));
    if (body != null) req.write(body);
    req.end();
  });
}

/**
 * 带重试的 GitHub API 调用（返回解析后的 JSON）。
 * 重试策略：仅 429 / 5xx，间隔 2/4/8s，最多 3 次；401/403 立即抛 BAD_TOKEN。
 * tolerate：把这些状态码当作「正常响应」返回 `{status, text, data}` 而不是抛错（例如探测仓库是否存在时的 404）。
 */
async function ghRequest({ method = "GET", host = API_HOST, path: reqPath, token, body = null, timeoutMs = API_TIMEOUT_MS, tolerate = [], hint }) {
  const payload = body == null ? null : (typeof body === "string" ? body : JSON.stringify(body));
  const headers = headersFor(token, payload ? { "Content-Type": "application/json", "Content-Length": String(Buffer.byteLength(payload)) } : {});

  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      res = await requestOnce({ method, host, path: reqPath, headers, body: payload, timeoutMs });
    } catch (e) {
      // 网络层失败（DNS / 断网 / 超时）：可重试，重试用尽后按 NETWORK 抛出
      if (attempt < retryDelays.length) {
        await delay(retryDelays[attempt]);
        continue;
      }
      throw networkFailure(e, hint);
    }
    if (Array.isArray(tolerate) && tolerate.includes(res.status)) {
      return { status: res.status, data: safeParseJson(res.text), text: res.text };
    }
    if (res.status >= 200 && res.status < 300) {
      return { status: res.status, data: safeParseJson(res.text), text: res.text };
    }
    if (isRetryableStatus(res.status) && attempt < retryDelays.length) {
      await delay(retryDelays[attempt]);
      continue;
    }
    throw httpFailure(res.status, res.text, hint, token);
  }
}

/** 计算文件体积与 sha256（流式，100MB 也不占内存）。 */
function hashFile(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    let size = 0;
    const rs = fs.createReadStream(filePath);
    rs.on("data", (chunk) => { size += chunk.length; hash.update(chunk); });
    rs.on("error", reject);
    rs.on("end", () => resolve({ size, sha256: hash.digest("hex") }));
  });
}

/**
 * 上传一次 release asset（POST uploads.github.com，流式读文件，600s 超时 + 进度回调）。
 * 用 https.request 而不是 fetch：上传需要「边读边发 + 精确的 Content-Length + 中断控制」，
 * 流式 request 的语义最直接，也不依赖 undici 的 duplex 支持细节。
 */
function uploadAssetOnce({ login, token, releaseId, assetName, filePath, size, onProgress, timeoutMs = UPLOAD_TIMEOUT_MS }) {
  return new Promise((resolve, reject) => {
    const reqPath = `/repos/${encodeURIComponent(login)}/${STAGING_REPO}/releases/${releaseId}/assets?name=${encodeURIComponent(assetName)}`;
    let settled = false;
    let timer = null;
    let req = null;
    const finish = (fn, arg) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      fn(arg);
    };

    req = https.request({
      method: "POST",
      host: UPLOAD_HOST,
      path: reqPath,
      headers: headersFor(token, {
        "Content-Type": "application/octet-stream",
        "Content-Length": String(size),
      }),
    }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => finish(resolve, { status: res.statusCode || 0, text: Buffer.concat(chunks).toString("utf8") }));
      res.on("error", (e) => finish(reject, e));
    });

    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        finish(reject, communityError("TIMEOUT_LOCAL", `上传超时（${Math.round(timeoutMs / 1000)}s），请检查网络后重试`, { retryable: true }));
        try { req.destroy(); } catch { /* ignore */ }
      }, timeoutMs);
    }

    const rs = fs.createReadStream(filePath);
    let sent = 0;
    rs.on("data", (chunk) => {
      if (settled) return;
      sent += chunk.length;
      if (typeof onProgress === "function") {
        const percent = size > 0 ? Math.min(100, Math.floor((sent / size) * 100)) : 0;
        try { onProgress({ stage: "uploading", percent, sent, total: size }); } catch { /* 进度回调异常不影响上传 */ }
      }
    });
    rs.on("error", (e) => { finish(reject, e); try { req.destroy(); } catch { /* ignore */ } });
    req.on("error", (e) => finish(reject, e));
    rs.pipe(req);
  });
}

/** 带重试的上传（429/5xx 各退避 2/4/8s 重传整包；D29：不做断点续传）。 */
async function uploadAsset(options) {
  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      res = await uploadAssetOnce(options);
    } catch (e) {
      if (attempt < retryDelays.length) {
        await delay(retryDelays[attempt]);
        continue;
      }
      throw networkFailure(e, "上传中转包");
    }
    if (res.status >= 200 && res.status < 300) {
      const asset = safeParseJson(res.text);
      if (!asset || !asset.browser_download_url) {
        throw communityError("UPLOAD_NO_URL", "上传成功但 GitHub 未返回 browser_download_url");
      }
      return asset;
    }
    if (isRetryableStatus(res.status) && attempt < retryDelays.length) {
      await delay(retryDelays[attempt]);
      continue;
    }
    throw httpFailure(res.status, res.text, "上传中转包", options.token);
  }
}

// ── GitHub 业务动作 ──────────────────────────────────────────────────────────

/** 校验 PAT 有效并拿到 login（D36 的「PAT 失效有明确提示」在这里落地）。 */
async function fetchLogin(token) {
  const res = await ghRequest({ path: "/user", token, hint: "校验 PAT" });
  const login = res.data && res.data.login;
  if (!login) throw communityError("BAD_TOKEN", "PAT 校验失败：GitHub 未返回 login");
  return String(login);
}

/**
 * 确保中转仓库存在。
 * 404 → 建一个公开空仓库（auto_init=false，不需要初始提交，反正只放 Release）。
 * 已存在 → 必须属于当前用户；属于别人则报错（说明同名仓库被别人占了，不能让用户以为传到了自己仓库）。
 */
async function ensureStagingRepo(login, token, onProgress) {
  const res = await ghRequest({
    path: `/repos/${encodeURIComponent(login)}/${STAGING_REPO}`,
    token,
    tolerate: [404],
    hint: "检查中转仓库",
  });
  if (res.status !== 404) {
    const owner = res.data && res.data.owner && res.data.owner.login;
    if (owner && String(owner).toLowerCase() !== String(login).toLowerCase()) {
      throw communityError("REPO_CONFLICT", `仓库 ${login}/${STAGING_REPO} 已存在但属于 ${owner}，无法作为中转仓库`);
    }
    return { created: false };
  }
  if (typeof onProgress === "function") onProgress({ stage: "staging-repo", percent: 0 });
  await ghRequest({
    method: "POST",
    path: "/user/repos",
    token,
    body: { name: STAGING_REPO, private: false, auto_init: false },
    hint: "创建中转仓库",
  });
  return { created: true };
}

/**
 * 在中转仓库建一个 staging release，返回 release 对象（含 id / upload_url）。
 * 兜底：auto_init=false 建出来的仓库没有提交，个别情况下 GitHub 会以 422 拒绝建 release；
 *       此时用 contents API 写一个 README 造出首个提交再重试一次（只走一次，不递归）。
 */
async function createStagingRelease({ login, token, tag, appId, version, retried = false }) {
  const repoPath = `/repos/${encodeURIComponent(login)}/${STAGING_REPO}`;
  try {
    const res = await ghRequest({
      method: "POST",
      path: `${repoPath}/releases`,
      token,
      body: {
        tag_name: tag,
        name: `${appId}@${version}（中转）`,
        body: `ChatCoder 中转包：${appId} ${version}。社区仓库接收成功后本 release 会被删除（plan-693-2868 §9-2）。`,
        draft: false,
        prerelease: false,
      },
      hint: "创建中转 Release",
    });
    const release = res.data;
    if (!release || !release.id) throw communityError("RELEASE_NO_ID", "GitHub 未返回中转 Release 的 id");
    return release;
  } catch (e) {
    if (!retried && e && e.status === 422) {
      // 空仓库没有可指向的提交：写一个占位文件产生首个提交，然后重试建 release
      await ghRequest({
        method: "PUT",
        path: `${repoPath}/contents/README.md`,
        token,
        body: {
          message: "init staging repo",
          content: Buffer.from("ChatCoder 微应用中转仓库（发布后自动清理）。\n").toString("base64"),
        },
        hint: "初始化中转仓库",
      });
      return createStagingRelease({ login, token, tag, appId, version, retried: true });
    }
    throw e;
  }
}

/** 拉取单个 issue 的状态与全部评论（D40：结果以 issue 状态为准）。 */
async function pollIssue(issueNumber, options = {}) {
  const repo = options.repo || getRepo();
  const token = options.token || readToken();
  const number = Number(issueNumber);
  if (!repo) throw communityError("NO_REPO", "未配置社区仓库");
  if (!token) throw communityError("NO_TOKEN", "还没有保存 GitHub PAT");
  if (!Number.isInteger(number) || number <= 0) throw communityError("INVALID_ISSUE", `issue 编号不合法：${issueNumber}`);

  const issuePath = `/repos/${repo}/issues/${number}`;
  const issue = await ghRequest({ path: issuePath, token, hint: "查询 issue 状态" });
  const commentsRes = await ghRequest({ path: `${issuePath}/comments?per_page=100`, token, hint: "查询 issue 回执" });
  const comments = Array.isArray(commentsRes.data) ? commentsRes.data : [];
  return {
    number,
    state: String((issue.data && issue.data.state) || ""),
    title: String((issue.data && issue.data.title) || ""),
    htmlUrl: String((issue.data && issue.data.html_url) || ""),
    closeReason: (issue.data && issue.data.state_reason) || null,
    comments: comments.map((c) => ({ body: String((c && c.body) || ""), createdAt: (c && c.created_at) || null })),
  };
}

/** 轮询直到 issue 关闭或用尽次数（sleep 可注入，便于单测跳过真实等待）。 */
async function pollUntilClosed({ issueNumber, repo, token, kind, intervalMs = POLL_INTERVAL_MS, maxAttempts = POLL_MAX_ATTEMPTS, sleep = delay, onProgress }) {
  const issueUrl = `https://github.com/${repo}/issues/${issueNumber}`;
  let attempt = 0;
  for (;;) {
    attempt += 1;
    const snapshot = await pollIssue(issueNumber, { repo, token });
    const verdict = evaluatePoll(snapshot, { attempt, maxAttempts, kind });
    if (verdict.done) return { ...verdict, issueNumber, issueUrl };
    if (typeof onProgress === "function") {
      try { onProgress({ stage: "waiting", percent: null, attempt, maxAttempts, issueUrl }); } catch { /* ignore */ }
    }
    await sleep(intervalMs);
  }
}

/** 删除中转 release（§9 第 2 条建议：发布成功后清理，避免用户仓库堆积）。失败不影响主流程。 */
async function deleteStagingRelease({ login, token, releaseId }) {
  await ghRequest({
    method: "DELETE",
    path: `/repos/${encodeURIComponent(login)}/${STAGING_REPO}/releases/${releaseId}`,
    token,
    tolerate: [404], // 已经没了就是删成功了，不需要报错
    hint: "清理中转 Release",
  });
}

/**
 * 发布到社区（§3.4 用户侧 1~5 步 + §7 P5-4）。
 * @param {object} opts
 * @param {string} opts.appId
 * @param {string} opts.version
 * @param {string} opts.filePath 已打包好的 .ccapp 绝对路径
 * @param {object} [opts.manifest] manifest 内容（用于 name/desc/perms/methods/vendors）
 * @param {string} [opts.authorId] 本机 usr_ 标识（D15）
 * @param {string} [opts.authorName] 可选昵称
 * @param {string} [opts.desc] 本次填的说明（覆盖 manifest 里的）
 * @param {Function} [opts.onProgress] ({stage, percent, ...}) => void
 * @param {boolean} [opts.cleanup=true] 成功后是否删除中转 release
 * @param {number} [opts.intervalMs] [opts.maxAttempts] [opts.sleep] 轮询参数（单测用）
 * @returns {Promise<object>} {status, issueUrl, issueNumber, comment, downloadUrl, ...}
 */
async function publish(opts = {}) {
  const onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};
  const report = (payload) => { try { onProgress(payload); } catch { /* 进度回调异常不影响发布 */ } };
  const appId = String(opts.appId || "").trim();
  const version = String(opts.version || "").trim();

  // ① 本地护栏：appId / version / 文件，先把「注定被 Actions 拒」的情况挡在前面
  if (!APP_ID_RE.test(appId)) throw communityError("INVALID_APP_ID", `appId 不合法：${appId}（需匹配 ^[a-z0-9][a-z0-9._-]{1,62}$）`);
  if (!VERSION_RE.test(version)) throw communityError("INVALID_VERSION", `版本号必须是 MAJOR.MINOR.PATCH：${version}`);
  const filePath = opts.filePath ? path.resolve(String(opts.filePath)) : "";
  if (!filePath) throw communityError("NO_FILE", "缺少已打包的文件路径");
  let stat = null;
  try { stat = fs.statSync(filePath); } catch { throw communityError("FILE_NOT_FOUND", `找不到要发布的包：${filePath}`); }
  if (!stat.isFile()) throw communityError("FILE_NOT_FOUND", `不是文件：${filePath}`);
  // 只放行包文件的扩展名：这条 IPC 能从渲染层触发「读本地文件并上传到 GitHub」，
  // 限定 .ccapp/.zip 可以让它不能被顺手改造成「上传任意文件」的通道（例如误传 .env）。
  if (!/\.(ccapp|zip)$/i.test(filePath)) {
    throw communityError("INVALID_FILE_TYPE", `只能发布 .ccapp（或 .zip）包：${path.basename(filePath)}`);
  }
  if (stat.size <= 0) throw communityError("BAD_SIZE", "包体积为 0，无法发布");
  if (stat.size > MAX_PACKAGE_BYTES) throw communityError("BAD_SIZE", `包体积 ${stat.size} 字节超过 100MB 上限`);

  // ② 社区仓库与 PAT
  const communityRepo = normalizeRepo(opts.repo != null ? opts.repo : getRepo());
  if (!communityRepo) throw communityError("NO_REPO", "未配置社区仓库，社区入口处于关闭状态");
  const token = readToken();
  if (!token) throw communityError("NO_TOKEN", "还没有保存 GitHub PAT：请先填写（勾选 public_repo 即可）");

  try {
    // ③ 起包摘要
    report({ stage: "hashing", percent: 0 });
    const { size, sha256 } = await hashFile(filePath);
    if (size > MAX_PACKAGE_BYTES) throw communityError("BAD_SIZE", `包体积 ${size} 字节超过 100MB 上限`);

    // ④ 校验 PAT 并取 login
    report({ stage: "auth", percent: 0 });
    const login = await fetchLogin(token);

    // ⑤ 中转仓库
    await ensureStagingRepo(login, token, report);

    // ⑥ 中转 release
    report({ stage: "release", percent: 0 });
    const tag = `${RELEASE_TAG_PREFIX}-${appId}-${version}-${Date.now()}`;
    const release = await createStagingRelease({ login, token, tag, appId, version });

    // ⑦ 上传包（600s 超时 + 进度）
    const assetName = `${appId}-${version}.ccapp`;
    const asset = await uploadAsset({
      login,
      token,
      releaseId: release.id,
      assetName,
      filePath,
      size,
      onProgress: (p) => report(p),
    });
    const downloadUrl = String(asset.browser_download_url);

    // ⑧ 提 [upload] issue
    report({ stage: "issue", percent: 100 });
    const payload = buildUploadPayload({
      appId,
      version,
      manifest: opts.manifest,
      desc: opts.desc,
      size,
      sha256,
      downloadUrl,
      authorId: opts.authorId,
      authorName: opts.authorName,
    });
    const issueRes = await ghRequest({
      method: "POST",
      path: `/repos/${communityRepo}/issues`,
      token,
      body: { title: buildIssueTitle("upload", appId, version), body: buildIssueBody("upload", payload) },
      hint: "提交发布 issue",
    });
    const issue = issueRes.data || {};
    const issueNumber = Number(issue.number) || 0;
    const issueUrl = String(issue.html_url || `https://github.com/${communityRepo}/issues/${issueNumber}`);

    // ⑨ 等 Actions 处理（D40）
    report({ stage: "waiting", percent: null, issueUrl });
    const verdict = await pollUntilClosed({
      issueNumber,
      repo: communityRepo,
      token,
      kind: "upload",
      intervalMs: Number(opts.intervalMs) > 0 ? Number(opts.intervalMs) : POLL_INTERVAL_MS,
      maxAttempts: Number(opts.maxAttempts) > 0 ? Number(opts.maxAttempts) : POLL_MAX_ATTEMPTS,
      sleep: typeof opts.sleep === "function" ? opts.sleep : delay,
      onProgress: report,
    });

    // ⑩ 清理中转 release：只在「确认成功」时删。
    //    删失败不影响主流程 —— 包已经在社区仓库里了，残留的只是用户自己仓库里的一个 Release，
    //    用户手动删也可以；因此这里只回一条 cleanupWarning 供界面提示。
    let cleanup = { attempted: false, ok: true, warning: "" };
    if (opts.cleanup !== false && verdict.status === "published") {
      cleanup = { attempted: true, ok: true, warning: "" };
      try {
        await deleteStagingRelease({ login, token, releaseId: release.id });
      } catch (e) {
        cleanup.ok = false;
        cleanup.warning = `中转 Release 未删除（${redactToken(String((e && e.message) || e), token)}），可稍后在自己的仓库里手动删除 ${login}/${STAGING_REPO}`;
      }
    }

    report({ stage: "done", percent: 100, status: verdict.status });
    const result = {
      status: verdict.status, // published | rejected | unknown | pending（pending = 超时未完成）
      timedOut: verdict.timedOut === true,
      outcome: verdict.outcome,
      comment: verdict.lastComment || "",
      issueNumber,
      issueUrl,
      downloadUrl,
      size,
      sha256,
      staging: { repo: `${login}/${STAGING_REPO}`, releaseId: release.id, tag, cleanup },
    };
    if (verdict.timedOut) {
      // 超时不是失败：issue 已经提上去了，Actions 可能还在排队（§3.4）。
      result.code = "TIMEOUT";
      result.message = `issue 已提交（${issueUrl}），但 5 分钟内未等到 Actions 完成；可稍后打开该链接查看结果`;
    }
    return result;
  } catch (e) {
    // 失败也推一条终态进度：UI 的进度条不能永远转着。message 里不含 token（本模块所有 throw 点都保证）。
    report({ stage: "error", percent: null, message: String((e && e.message) || e) });
    throw e;
  }
}

/**
 * 从社区撤回（§3.4 撤回 / D35）：提 `[withdraw]` issue → Actions 移除 apps.json 条目 → 关 issue。
 * 注意：撤回**不删**社区仓库里的 Release（D35），所以这里不碰任何 release。
 */
async function withdraw(opts = {}) {
  const onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};
  const report = (payload) => { try { onProgress(payload); } catch { /* ignore */ } };
  const appId = String(opts.appId || "").trim();
  const version = String(opts.version || "").trim();

  if (!APP_ID_RE.test(appId)) throw communityError("INVALID_APP_ID", `appId 不合法：${appId}`);
  if (!VERSION_RE.test(version)) throw communityError("INVALID_VERSION", `版本号必须是 MAJOR.MINOR.PATCH：${version}`);

  const communityRepo = normalizeRepo(opts.repo != null ? opts.repo : getRepo());
  if (!communityRepo) throw communityError("NO_REPO", "未配置社区仓库，社区入口处于关闭状态");
  const token = readToken();
  if (!token) throw communityError("NO_TOKEN", "还没有保存 GitHub PAT：请先填写（勾选 public_repo 即可）");

  report({ stage: "issue", percent: 0 });
  const payload = buildWithdrawPayload({ appId, version, authorId: opts.authorId, authorName: opts.authorName, reason: opts.reason });
  const issueRes = await ghRequest({
    method: "POST",
    path: `/repos/${communityRepo}/issues`,
    token,
    body: { title: buildIssueTitle("withdraw", appId, version), body: buildIssueBody("withdraw", payload) },
    hint: "提交撤回 issue",
  });
  const issue = issueRes.data || {};
  const issueNumber = Number(issue.number) || 0;
  const issueUrl = String(issue.html_url || `https://github.com/${communityRepo}/issues/${issueNumber}`);

  report({ stage: "waiting", percent: null, issueUrl });
  const verdict = await pollUntilClosed({
    issueNumber,
    repo: communityRepo,
    token,
    kind: "withdraw",
    intervalMs: Number(opts.intervalMs) > 0 ? Number(opts.intervalMs) : POLL_INTERVAL_MS,
    maxAttempts: Number(opts.maxAttempts) > 0 ? Number(opts.maxAttempts) : POLL_MAX_ATTEMPTS,
    sleep: typeof opts.sleep === "function" ? opts.sleep : delay,
    onProgress: report,
  });

  report({ stage: "done", percent: 100, status: verdict.status });
  const result = {
    status: verdict.status, // withdrawn | rejected | unknown | pending
    timedOut: verdict.timedOut === true,
    outcome: verdict.outcome,
    comment: verdict.lastComment || "",
    issueNumber,
    issueUrl,
  };
  if (verdict.timedOut) {
    result.code = "TIMEOUT";
    result.message = `撤回 issue 已提交（${issueUrl}），但 5 分钟内未等到 Actions 完成；可稍后打开该链接查看结果`;
  }
  return result;
}

module.exports = {
  // 本地状态
  getRepo,
  setRepo,
  getTokenStatus,
  setToken,
  clearToken,
  // 业务动作
  publish,
  withdraw,
  pollIssue,
  // 常量（渲染层展示上限时用得上）
  MAX_PACKAGE_BYTES,
  POLL_INTERVAL_MS,
  POLL_MAX_ATTEMPTS,
  /** 纯逻辑与内部工具：单测（electron/community-github.test.cjs）直接引用，不在 IPC 暴露 */
  _internals: {
    APP_ID_RE,
    VERSION_RE,
    REPO_RE,
    STAGING_REPO,
    communityError,
    isCommunityError,
    maskToken,
    redactToken,
    normalizeRepo,
    normalizeVendors,
    manifestSummary,
    buildUploadPayload,
    buildWithdrawPayload,
    buildIssueBody,
    buildIssueTitle,
    classifyOutcome,
    evaluatePoll,
    isRetryableStatus,
    httpFailure,
    networkFailure,
    headersFor,
    safeParseJson,
    hashFile,
    readToken,
    dataDir,
    prefFile,
    tokenEncFile,
    tokenPlainFile,
    /** 仅测试用：把退避序列压成 [0,0,0]，避免单测真等 14 秒 */
    setRetryDelays(ms) {
      retryDelays = Array.isArray(ms) && ms.length ? ms.map((n) => Number(n) || 0) : RETRY_DELAYS_MS.slice();
    },
    /** 仅测试用：注入 safeStorage 替身（传 null 复原） */
    setSafeStorage(stub) {
      safeStorageOverride = stub || null;
    },
  },
};
