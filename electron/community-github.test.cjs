/**
 * electron/community-github.cjs 与 electron/secure-file.cjs 的纯逻辑单测
 * 依据：plan-693-2868 §3.4 / §6.4 / D20 / D32 / D34 / D36 / D37
 *
 * 覆盖点（对应验收清单）：
 *  1. issue 正文拼装：```json 围栏块能被 accept.yml 的 awk 逻辑取出，字段满足 workflow 的逐项校验；
 *  2. 错误映射：401/403 → BAD_TOKEN，404 → NOT_FOUND，422 → REJECTED，429/5xx 可重试；
 *  3. 轮询终止条件：closed 终止、次数用尽 → pending（超时不等于失败）；
 *  4. token 脱敏：maskToken / redactToken，以及错误信息里绝不出现 token 原文；
 *  5. 本地存储：偏好与 PAT 的读写、降级（明文 0600）、清除；getTokenStatus 不返回 token；
 *  6. secure-file：原子写不留 .tmp、坏 JSON 返回 null。
 *
 * 全部用例**不发真实网络请求**（没有凭据，也会污染真实仓库）。
 * 运行：node --test electron/community-github.test.cjs
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");

const gh = require("./community-github.cjs");
const secure = require("./secure-file.cjs");

const {
  APP_ID_RE, VERSION_RE, communityError, isCommunityError, maskToken, redactToken,
  normalizeRepo, buildUploadPayload, buildWithdrawPayload, buildIssueBody, buildIssueTitle,
  classifyOutcome, evaluatePoll, isRetryableStatus, httpFailure, headersFor, hashFile,
} = gh._internals;

// ─── 测试辅助 ────────────────────────────────────────────────────────────────

/**
 * 起一个隔离的社区配置目录，避免碰真实的 userData / ~/.chatcoder。
 * 同时支持同步回调（返回 undefined）与返回 Promise 的回调（等它结束再清理）。
 */
function withTempDir(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-community-"));
  const prev = process.env.CHATCODER_COMMUNITY_DIR;
  process.env.CHATCODER_COMMUNITY_DIR = dir;
  const cleanup = () => {
    if (prev === undefined) delete process.env.CHATCODER_COMMUNITY_DIR;
    else process.env.CHATCODER_COMMUNITY_DIR = prev;
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  };
  let out;
  try {
    out = fn(dir);
  } catch (e) {
    cleanup();
    throw e;
  }
  if (out && typeof out.then === "function") {
    return out.then(
      (v) => { cleanup(); return v; },
      (e) => { cleanup(); throw e; },
    );
  }
  cleanup();
  return out;
}

/**
 * 复刻 accept.yml 第 51-53 行的 awk：取正文里第一个 ```json 围栏块。
 * awk 程序：/^```json[[:space:]]*$/ {f=1;next}  /^```[[:space:]]*$/ {if(f){exit}}  f
 */
function extractJsonFence(body) {
  const lines = String(body).split(/\r?\n/);
  let open = false;
  const out = [];
  for (const line of lines) {
    if (/^```json[ \t]*$/.test(line)) { open = true; continue; }
    if (/^```[ \t]*$/.test(line)) { if (open) break; else continue; }
    if (open) out.push(line);
  }
  return open ? out.join("\n") : "";
}

/** 复刻 accept.yml 第 59-68、109-123 行的校验分支，用来证明我们的 payload 一定过得了。 */
function validatePayloadAsWorkflow(payload) {
  const problems = [];
  if (!APP_ID_RE.test(String(payload.appId || ""))) problems.push("appId 不合法");
  if (!/^[0-9]+\.[0-9]+\.[0-9]+$/.test(String(payload.version || ""))) problems.push("version 不合法");
  const url = String(payload.downloadUrl || "");
  if (!/^https:\/\/github\.com\//.test(url) && !/^https:\/\/objects\.githubusercontent\.com\//.test(url)) {
    problems.push("downloadUrl 不是 github.com 的 https 地址");
  }
  if (!/^[0-9a-f]{64}$/.test(String(payload.sha256 || ""))) problems.push("sha256 不是 64 位小写十六进制");
  const size = Number(payload.size);
  if (!(size > 0 && size <= 104857600)) problems.push("size 越界");
  return problems;
}

/**
 * 断言文件是 0600。
 * Windows 不实现 POSIX 权限位（stat 的 mode 恒为 0o666），所以只在类 Unix 上断言；
 * 代码里仍然显式传 `mode: 0o600`（见 secure-file.cjs），Windows 下靠用户目录 ACL 兜底。
 */
function assertPrivateMode(file) {
  if (process.platform === "win32") return;
  assert.equal(fs.statSync(file).mode & 0o777, 0o600, "私有文件必须是 0600");
}

const SAMPLE_MANIFEST = {
  name: "管线画布",
  desc: "把管线协议画成时序图",
  permissions: ["read_workspace"],
  methods: ["drawTimeline", "highlight"],
  vendors: [{ name: "antd", version: "5.20.0" }],
};

// ─── 1. issue 正文拼装 ──────────────────────────────────────────────────────

test("upload：json 块能被 accept.yml 的 awk 取出，且通过 workflow 逐项校验", () => {
  const payload = buildUploadPayload({
    appId: "pipeline-canvas",
    version: "1.0.0",
    manifest: SAMPLE_MANIFEST,
    size: 1048576,
    sha256: "a".repeat(64),
    downloadUrl: "https://github.com/someone/chatcoder-microapp-staging/releases/download/staging-pipeline-canvas-1.0.0-1/pipeline-canvas-1.0.0.ccapp",
    authorId: "usr_7f3c1a2b",
    authorName: "小明",
  });
  const body = buildIssueBody("upload", payload);
  const extracted = extractJsonFence(body);
  assert.notEqual(extracted, "", "正文里必须有可被 awk 取出的 ```json 块");

  const parsed = JSON.parse(extracted); // jq -e . 等价
  assert.deepEqual(validatePayloadAsWorkflow(parsed), []);

  // 字段集合必须与 accept.yml / README 的条目结构一致
  assert.deepEqual(Object.keys(parsed).sort(), [
    "appId", "authorId", "authorName", "desc", "downloadUrl", "methods",
    "name", "perms", "sha256", "size", "vendors", "version",
  ]);
  assert.deepEqual(parsed.perms, ["read_workspace"]);
  assert.deepEqual(parsed.methods, ["drawTimeline", "highlight"]);
  assert.deepEqual(parsed.vendors, [{ name: "antd", version: "5.20.0" }]);
  assert.equal(parsed.size, 1048576);
  assert.equal(typeof parsed.size, "number"); // jq 的 size 校验是数值比较，必须是 number
});

test("upload：desc 里带 ``` 也不会提前闭合围栏（Actions 仍能解析）", () => {
  const payload = buildUploadPayload({
    appId: "bad-desc-app",
    version: "2.3.4",
    manifest: { ...SAMPLE_MANIFEST, desc: "示例：```js\nconsole.log(1)\n```" },
    size: 2048,
    sha256: "b".repeat(64),
    downloadUrl: "https://github.com/u/chatcoder-microapp-staging/releases/download/t/a.ccapp",
    authorId: "usr_x",
    authorName: "",
  });
  const body = buildIssueBody("upload", payload);
  const extracted = extractJsonFence(body);
  assert.notEqual(extracted, "");
  const parsed = JSON.parse(extracted);
  assert.ok(!parsed.desc.includes("```"), "desc 里的三反引号必须被替换掉");
  assert.equal(parsed.authorName, "");
});

test("withdraw：标题前缀与 json 块满足 accept.yml 的 withdraw 分支", () => {
  assert.equal(buildIssueTitle("withdraw", "pipeline-canvas", "1.0.0"), "[withdraw] pipeline-canvas@1.0.0");
  assert.equal(buildIssueTitle("upload", "pipeline-canvas", "1.0.0"), "[upload] pipeline-canvas@1.0.0");

  const payload = buildWithdrawPayload({ appId: "pipeline-canvas", version: "1.0.0", authorId: "usr_1" });
  const parsed = JSON.parse(extractJsonFence(buildIssueBody("withdraw", payload)));
  assert.equal(parsed.appId, "pipeline-canvas");
  assert.equal(parsed.version, "1.0.0");
});

test("manifest 容错：缺字段时给安全默认值，方法/供应商支持字符串数组", () => {
  const payload = buildUploadPayload({
    appId: "mini-app",
    version: "0.0.1",
    manifest: { methods: [{ name: "run" }, "stop"], vendors: ["vue"] },
    size: 1,
    sha256: "c".repeat(64),
    downloadUrl: "https://github.com/u/r/releases/download/t/a.ccapp",
  });
  assert.equal(payload.name, "mini-app");
  assert.deepEqual(payload.methods, ["run", "stop"]);
  assert.deepEqual(payload.vendors, [{ name: "vue", version: "" }]);
  assert.deepEqual(payload.perms, []);
});

// ─── 2. 错误映射 ────────────────────────────────────────────────────────────

test("错误映射：401/403 → BAD_TOKEN，404 → NOT_FOUND，422 → REJECTED，5xx/429 可重试", () => {
  const body401 = JSON.stringify({ message: "Bad credentials" });
  assert.equal(httpFailure(401, body401, "校验 PAT").code, "BAD_TOKEN");
  assert.equal(httpFailure(403, body401, "校验 PAT").code, "BAD_TOKEN");
  assert.equal(httpFailure(404, JSON.stringify({ message: "Not Found" }), "查仓库").code, "NOT_FOUND");
  assert.equal(httpFailure(422, JSON.stringify({ message: "Validation Failed" }), "建 Release").code, "REJECTED");
  assert.equal(httpFailure(500, "oops", "提交 issue").code, "HTTP_500");
  assert.match(httpFailure(401, body401, "校验 PAT").message, /HTTP 401/);
  assert.match(httpFailure(401, body401, "校验 PAT").message, /Bad credentials/);

  assert.equal(isRetryableStatus(429), true);
  assert.equal(isRetryableStatus(500), true);
  assert.equal(isRetryableStatus(503), true);
  assert.equal(isRetryableStatus(401), false);
  assert.equal(isRetryableStatus(403), false);
  assert.equal(isRetryableStatus(404), false);
  assert.equal(isRetryableStatus(422), false);

  // 结构化错误既像 Error（可 throw / 有 message），又带机器可读 code
  const err = communityError("NO_TOKEN", "没有 token");
  assert.equal(err instanceof Error, true);
  assert.equal(err.code, "NO_TOKEN");
  assert.equal(isCommunityError(err), true);
  assert.equal(isCommunityError(new Error("x")), false);
});

test("错误映射：httpFailure 的文本里绝不出现 token 原文", () => {
  const token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  // 构造一个「万一」把 Authorization 回显进响应体的极端情况
  const leaky = JSON.stringify({ message: `bad header Authorization: Bearer ${token}` });
  const err = httpFailure(401, leaky, "校验 PAT", token);
  assert.ok(!err.message.includes(token), "错误信息不能包含 token 原文");
  assert.match(err.message, /ghp_…/);
});

// ─── 3. 轮询终止条件 ────────────────────────────────────────────────────────

test("轮询终止：issue 仍 open 且未达上限 → 继续等待", () => {
  const v = evaluatePoll({ state: "open", comments: [] }, { attempt: 3, maxAttempts: 60, kind: "upload" });
  assert.equal(v.done, false);
  assert.equal(v.timedOut, false);
  assert.equal(v.status, "pending");
});

test("轮询终止：closed + 成功回执 → published / withdrawn", () => {
  const okUpload = evaluatePoll(
    { state: "closed", comments: [{ body: "已发布 pipeline-canvas-1.0.0。下载地址：https://github.com/o/r/..." }] },
    { attempt: 2, maxAttempts: 60, kind: "upload" },
  );
  assert.equal(okUpload.done, true);
  assert.equal(okUpload.status, "published");
  assert.equal(okUpload.outcome, "ok");

  const okWithdraw = evaluatePoll(
    { state: "closed", comments: [{ body: "已从索引移除 pipeline-canvas@1.0.0。Release 里的包保留。" }] },
    { attempt: 2, maxAttempts: 60, kind: "withdraw" },
  );
  assert.equal(okWithdraw.status, "withdrawn");
});

test("轮询终止：closed + 失败回执 → rejected（失败优先于成功关键词）", () => {
  const v = evaluatePoll(
    { state: "closed", comments: [{ body: "版本 x@1.0.0 已存在且不允许覆盖，请升版本后重新发布。" }] },
    { attempt: 5, maxAttempts: 60, kind: "upload" },
  );
  assert.equal(v.status, "rejected");
  assert.equal(v.outcome, "rejected");

  // Actions 的通用失败回执（含「处理失败」并罗列常见原因）也必须判为 rejected
  assert.equal(classifyOutcome("处理失败，请检查上面 Actions 日志里的原因（常见：字段缺失、sha256 不匹配）"), "rejected");
  assert.equal(classifyOutcome(""), "unknown");
  assert.equal(classifyOutcome("随便说点什么"), "unknown");
});

test("轮询终止：次数用尽 → timedOut，返回 pending 而不是失败", () => {
  const v = evaluatePoll({ state: "open", comments: [] }, { attempt: 60, maxAttempts: 60, kind: "upload" });
  assert.equal(v.done, true);
  assert.equal(v.timedOut, true);
  assert.equal(v.status, "pending");
});

// ─── 4. token 脱敏 ──────────────────────────────────────────────────────────

test("token 脱敏：maskToken 只露前 4 位，redactToken 替换正文里的全部出现", () => {
  const token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  const masked = maskToken(token);
  assert.equal(masked, "ghp_…");
  assert.ok(!masked.includes("ABCDEF"));
  assert.equal(maskToken(""), "（空）");

  const text = `a ${token} b ${token} c`;
  const red = redactToken(text, token);
  assert.ok(!red.includes(token));
  assert.equal(red.split("ghp_…").length - 1, 2);
  // 太短的值不做替换（避免把正常文本切碎）
  assert.equal(redactToken("ghp_abc", "ghp_abc"), "ghp_abc");
});

// ─── 5. 本地存储（偏好 / PAT）───────────────────────────────────────────────

test("setRepo/getRepo：接受 owner/repo 与完整 URL，空串表示关闭社区", () => {
  withTempDir((dir) => {
    assert.equal(gh.getRepo(), "", "未配置时返回空串");
    assert.equal(gh.setRepo("someone/chatcoder-microapps"), "someone/chatcoder-microapps");
    assert.equal(gh.getRepo(), "someone/chatcoder-microapps");
    assert.equal(gh.setRepo("https://github.com/other/chatcoder-microapps.git"), "other/chatcoder-microapps");
    assert.equal(gh.setRepo(""), "", "留空 = 关闭社区（§7 P5-8）");
    assert.throws(() => gh.setRepo("not-a-repo"), (e) => e.code === "INVALID_REPO");
    assert.equal(normalizeRepo("  o/r  "), "o/r");
    assert.ok(fs.existsSync(path.join(dir, "community-pref.json")));
  });
});

test("PAT：无 safeStorage 时降级为明文 0600，getTokenStatus 不返回 token", () => {
  withTempDir((dir) => {
    assert.deepEqual(gh.getTokenStatus(), { saved: false, encrypted: false });

    const st = gh.setToken("ghp_TESTTESTTESTTESTTESTTESTTESTTEST1234");
    assert.deepEqual(st, { saved: true, encrypted: false }, "单测环境无 safeStorage → 走明文降级");

    const plainFile = path.join(dir, "gh-token.plain");
    assert.ok(fs.existsSync(plainFile));
    assertPrivateMode(plainFile);
    assert.equal(gh._internals.readToken(), "ghp_TESTTESTTESTTESTTESTTESTTESTTEST1234");

    const status = gh.getTokenStatus();
    assert.deepEqual(Object.keys(status).sort(), ["encrypted", "saved"]);
    assert.ok(!JSON.stringify(status).includes("ghp_"), "状态里绝不能带 token");

    // 非法输入
    assert.throws(() => gh.setToken(""), (e) => e.code === "EMPTY_TOKEN");
    assert.throws(() => gh.setToken("short"), (e) => e.code === "INVALID_TOKEN");
    assert.throws(() => gh.setToken("ghp_AAAA AAAA"), (e) => e.code === "INVALID_TOKEN");

    gh.clearToken();
    assert.deepEqual(gh.getTokenStatus(), { saved: false, encrypted: false });
    assert.equal(gh._internals.readToken(), "");
  });
});

test("publish/withdraw：无 PAT 抛 NO_TOKEN；未配置仓库抛 NO_REPO（都在发网络请求之前）", async () => {
  const file = path.join(os.tmpdir(), `cc-fake-package-${process.pid}.ccapp`);
  fs.writeFileSync(file, "x");
  try {
    await withTempDir(async () => {
      // publish/withdraw 是 async：失败表现为 rejected promise（IPC 层用 await 接住再转成 {ok:false, code}）
      await assert.rejects(
        gh.publish({ appId: "demo-app", version: "1.0.0", filePath: file }),
        (e) => e.code === "NO_REPO",
      );
      gh.setRepo("o/r");
      await assert.rejects(
        gh.publish({ appId: "demo-app", version: "1.0.0", filePath: file }),
        (e) => e.code === "NO_TOKEN",
      );
      await assert.rejects(gh.withdraw({ appId: "demo-app", version: "1.0.0" }), (e) => e.code === "NO_TOKEN");
    });
  } finally {
    fs.rmSync(file, { force: true });
  }
});

test("PAT 加密档（safeStorage 可用）：写 .enc、清 .plain、encrypted=true，且文件里没有明文", () => {
  withTempDir((dir) => {
    // 用替身模拟 Electron safeStorage：真实实现只在 Electron 运行时存在
    const fake = {
      isEncryptionAvailable: () => true,
      encryptString: (s) => Buffer.from(`ENC:${s}`, "utf8"),
      decryptString: (b) => b.toString("utf8").replace(/^ENC:/, ""),
    };
    gh._internals.setSafeStorage(fake);
    try {
      const token = "ghp_ENCPATHPATHPATHPATHPATHPATH000000";
      const st = gh.setToken(token);
      assert.deepEqual(st, { saved: true, encrypted: true });
      assert.ok(fs.existsSync(path.join(dir, "gh-token.enc")), "应写加密档");
      assert.ok(!fs.existsSync(path.join(dir, "gh-token.plain")), "不应留明文档");
      assert.equal(gh.getTokenStatus().encrypted, true);
      assert.equal(gh._internals.readToken(), token);

      // 解密失败必须明确报 TOKEN_UNREADABLE，而不是静默当作「没填过」
      gh._internals.setSafeStorage({ ...fake, decryptString: () => { throw new Error("bad key"); } });
      assert.throws(() => gh._internals.readToken(), (e) => e.code === "TOKEN_UNREADABLE");
    } finally {
      gh._internals.setSafeStorage(null);
    }
  });
});

test("publish：参数护栏（appId / version / 文件）在联网前就拦下", async () => {
  await withTempDir(async () => {
    gh.setRepo("o/r");
    gh.setToken("ghp_TESTTESTTESTTESTTESTTESTTESTTEST1234");
    const guard = (code) => (e) => e.code === code;
    await assert.rejects(gh.publish({ appId: "Bad App", version: "1.0.0", filePath: "x" }), guard("INVALID_APP_ID"));
    await assert.rejects(gh.publish({ appId: "demo-app", version: "1.0", filePath: "x" }), guard("INVALID_VERSION"));
    await assert.rejects(gh.publish({ appId: "demo-app", version: "1.0.0" }), guard("NO_FILE"));
    await assert.rejects(
      gh.publish({ appId: "demo-app", version: "1.0.0", filePath: path.join(os.tmpdir(), "cc-not-exist.ccapp") }),
      guard("FILE_NOT_FOUND"),
    );
    // 这条 IPC 能从渲染层触发「读本地文件并上传」，所以限定扩展名，避免被当成任意文件外传通道
    const notPackage = path.join(os.tmpdir(), `cc-not-a-package-${process.pid}.txt`);
    fs.writeFileSync(notPackage, "secret");
    try {
      await assert.rejects(
        gh.publish({ appId: "demo-app", version: "1.0.0", filePath: notPackage }),
        guard("INVALID_FILE_TYPE"),
      );
    } finally {
      fs.rmSync(notPackage, { force: true });
    }
  });
});

// ─── 6. secure-file ─────────────────────────────────────────────────────────

test("secure-file：原子写不留 .tmp，坏 JSON 返回 null，删除幂等", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-secure-"));
  try {
    const file = path.join(dir, "nested", "pref.json");
    secure.writePrivateJson(file, { repo: "o/r" });
    assert.deepEqual(secure.readPrivateJson(file), { repo: "o/r" });
    assertPrivateMode(file);
    assert.deepEqual(fs.readdirSync(path.dirname(file)), ["pref.json"], "不得残留临时文件");

    fs.writeFileSync(file, "{ 这不是 json");
    assert.equal(secure.readPrivateJson(file), null, "坏 JSON 返回 null 而不是抛错");

    assert.equal(secure.readPrivateJson(path.join(dir, "不存在.json")), null);
    assert.equal(secure.removePrivateFile(file), true);
    assert.equal(secure.removePrivateFile(file), true, "重复删除也返回 true");

    // hashFile 供上传前算 sha256/size（与 accept.yml 的校验口径一致）
    const pkg = path.join(dir, "pkg.ccapp");
    fs.writeFileSync(pkg, "hello");
    const { size, sha256 } = await hashFile(pkg);
    assert.equal(size, 5);
    assert.equal(sha256, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("headersFor：带 Bearer / Accept / User-Agent，且不额外泄露字段", () => {
  const h = headersFor("ghp_x");
  assert.equal(h.Authorization, "Bearer ghp_x");
  assert.equal(h.Accept, "application/vnd.github+json");
  assert.equal(h["User-Agent"], "chatcoder");
  assert.deepEqual(Object.keys(h).sort(), ["Accept", "Authorization", "User-Agent", "X-GitHub-Api-Version"]);
  assert.ok(VERSION_RE.test("1.0.0"));
  assert.equal(VERSION_RE.test("1.0"), false);
});

// ─── 7. 全链路 mock（顶掉 https.request，绝不发真实请求）────────────────────

const httpsMod = require("node:https");
const { Writable, Readable } = require("node:stream");

const TOKEN = "ghp_TESTTESTTESTTESTTESTTESTTESTTEST1234";

/** 造一个假 IncomingMessage：push 一次 body 然后结束。 */
function makeResponse(statusCode, payload) {
  const res = new Readable({ read() { /* 由 push 驱动 */ } });
  res.statusCode = statusCode;
  res.headers = {};
  setImmediate(() => {
    const text = typeof payload === "string" ? payload : JSON.stringify(payload || {});
    if (text) res.push(text);
    res.push(null);
  });
  return res;
}

/**
 * 顶掉 https.request。resolveRoute(call) 返回 [status, payload]。
 * 记录每次调用的 method/host/path/headers/body，供断言请求顺序与请求体。
 * 上传用的是 `rs.pipe(req)`，所以假 req 必须是可写流（Writable）。
 */
function mockHttps(resolveRoute) {
  const original = httpsMod.request;
  const calls = [];
  httpsMod.request = function (options, onResponse) {
    const call = { method: options.method || "GET", host: options.host, path: options.path, headers: options.headers || {}, body: "" };
    calls.push(call);
    const req = new Writable({
      write(chunk, _enc, next) { call.body += chunk.toString("utf8"); next(); },
    });
    const realEnd = req.end.bind(req);
    req.end = function (chunk, enc, cb) {
      realEnd(chunk, enc, cb);
      const [status, payload] = resolveRoute(call) || [500, { message: `未预期的请求 ${call.method} ${call.path}` }];
      setImmediate(() => onResponse(makeResponse(status, payload)));
    };
    req.destroy = () => { /* 假连接：超时路径里调用它不应有副作用 */ };
    return req;
  };
  return {
    calls,
    restore() { httpsMod.request = original; },
    paths() { return calls.map((c) => `${c.method} ${c.path}`); },
  };
}

const STAGING_REPO_FULL = "alice/chatcoder-microapp-staging";
const COMMUNITY_REPO = "community/chatcoder-microapps";
const ASSET_URL = `https://github.com/${STAGING_REPO_FULL}/releases/download/staging-demo-app-1.0.0-1/demo-app-1.0.0.ccapp`;

test("mock：publish 全链路（user → 建中转仓库 → 建 release → 上传 → 提 issue → 轮询关闭 → 删中转）", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-publish-"));
  const prevDir = process.env.CHATCODER_COMMUNITY_DIR;
  process.env.CHATCODER_COMMUNITY_DIR = dir;
  const pkg = path.join(dir, "demo-app-1.0.0.ccapp");
  fs.writeFileSync(pkg, "package-bytes");

  let pollCount = 0;
  const mock = mockHttps((call) => {
    if (call.host === "uploads.github.com") return [201, { browser_download_url: ASSET_URL }];
    if (call.path === "/user" && call.method === "GET") return [200, { login: "alice" }];
    if (call.path === `/repos/${STAGING_REPO_FULL}` && call.method === "GET") return [404, { message: "Not Found" }];
    if (call.path === "/user/repos" && call.method === "POST") return [201, { name: "chatcoder-microapp-staging", owner: { login: "alice" } }];
    if (call.path === `/repos/${STAGING_REPO_FULL}/releases` && call.method === "POST") return [201, { id: 777 }];
    if (call.path === `/repos/${COMMUNITY_REPO}/issues` && call.method === "POST") {
      return [201, { number: 12, html_url: `https://github.com/${COMMUNITY_REPO}/issues/12` }];
    }
    if (call.path === `/repos/${COMMUNITY_REPO}/issues/12` && call.method === "GET") {
      pollCount += 1;
      // 第一次仍是 open（验证轮询会继续等），第二次已 closed
      return [200, pollCount >= 2 ? { state: "closed", state_reason: "completed" } : { state: "open" }];
    }
    if (call.path === `/repos/${COMMUNITY_REPO}/issues/12/comments?per_page=100`) {
      return [200, [{ body: "已发布 demo-app-1.0.0。下载地址：https://github.com/x/y（索引最长 5 分钟后对所有人可见）" }]];
    }
    if (call.path === `/repos/${STAGING_REPO_FULL}/releases/777` && call.method === "DELETE") return [204, ""];
    return null;
  });

  try {
    gh.setRepo(COMMUNITY_REPO);
    gh.setToken(TOKEN);
    const stages = [];
    const result = await gh.publish({
      appId: "demo-app",
      version: "1.0.0",
      filePath: pkg,
      manifest: SAMPLE_MANIFEST,
      authorId: "usr_abc",
      authorName: "小明",
      desc: "画流程图",
      intervalMs: 1,
      maxAttempts: 5,
      sleep: async () => { /* 跳过真实等待 */ },
      onProgress: (p) => stages.push(p.stage),
    });

    assert.equal(result.status, "published");
    assert.equal(result.timedOut, false);
    assert.equal(result.issueNumber, 12);
    assert.equal(result.downloadUrl, ASSET_URL);
    assert.equal(result.size, "package-bytes".length);
    assert.equal(result.sha256.length, 64);
    assert.equal(result.staging.releaseId, 777);
    assert.deepEqual(result.staging.cleanup, { attempted: true, ok: true, warning: "" });

    // 请求顺序：先校验 PAT，再确保中转仓库，再 release、上传、提 issue、轮询，最后清理
    const paths = mock.paths();
    assert.deepEqual(paths.slice(0, 5), [
      "GET /user",
      `GET /repos/${STAGING_REPO_FULL}`,
      "POST /user/repos",
      `POST /repos/${STAGING_REPO_FULL}/releases`,
      `POST /repos/${STAGING_REPO_FULL}/releases/777/assets?name=demo-app-1.0.0.ccapp`,
    ]);
    assert.equal(paths[5], `POST /repos/${COMMUNITY_REPO}/issues`);
    assert.ok(paths.includes(`DELETE /repos/${STAGING_REPO_FULL}/releases/777`), "发布成功后应清理中转 release");

    // 每条请求都带鉴权头；上传还必须是 octet-stream + 精确 Content-Length
    for (const c of mock.calls) {
      assert.equal(c.headers.Authorization, `Bearer ${TOKEN}`);
      assert.equal(c.headers.Accept, "application/vnd.github+json");
      assert.equal(c.headers["User-Agent"], "chatcoder");
    }
    const upload = mock.calls.find((c) => c.host === "uploads.github.com");
    assert.equal(upload.headers["Content-Type"], "application/octet-stream");
    assert.equal(upload.headers["Content-Length"], String("package-bytes".length));
    assert.equal(upload.body, "package-bytes");

    // 提上去的 issue 正文必须能被 accept.yml 解析并通过校验
    const issueCall = mock.calls.find((c) => c.path === `/repos/${COMMUNITY_REPO}/issues`);
    const issueBody = JSON.parse(issueCall.body);
    assert.equal(issueBody.title, "[upload] demo-app@1.0.0");
    const payload = JSON.parse(extractJsonFence(issueBody.body));
    assert.deepEqual(validatePayloadAsWorkflow(payload), []);
    assert.equal(payload.downloadUrl, ASSET_URL);
    assert.equal(payload.authorId, "usr_abc");
    assert.equal(payload.desc, "画流程图");

    // 进度事件覆盖关键阶段
    for (const stage of ["hashing", "auth", "staging-repo", "release", "uploading", "issue", "waiting", "done"]) {
      assert.ok(stages.includes(stage), `缺少进度阶段 ${stage}`);
    }

    // token 不出现在任何返回值里
    assert.ok(!JSON.stringify(result).includes(TOKEN));
  } finally {
    mock.restore();
    if (prevDir === undefined) delete process.env.CHATCODER_COMMUNITY_DIR;
    else process.env.CHATCODER_COMMUNITY_DIR = prevDir;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("mock：中转 release 删除失败不影响主流程（仍算发布成功，只回一条 warning）", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-cleanup-"));
  const prevDir = process.env.CHATCODER_COMMUNITY_DIR;
  process.env.CHATCODER_COMMUNITY_DIR = dir;
  const pkg = path.join(dir, "demo-app-1.0.0.ccapp");
  fs.writeFileSync(pkg, "bytes");

  const mock = mockHttps((call) => {
    if (call.host === "uploads.github.com") return [201, { browser_download_url: ASSET_URL }];
    if (call.path === "/user") return [200, { login: "alice" }];
    if (call.path === `/repos/${STAGING_REPO_FULL}`) return [200, { owner: { login: "alice" } }]; // 仓库已存在
    if (call.path === `/repos/${STAGING_REPO_FULL}/releases`) return [201, { id: 888 }];
    if (call.path === `/repos/${COMMUNITY_REPO}/issues`) {
      return [201, { number: 13, html_url: `https://github.com/${COMMUNITY_REPO}/issues/13` }];
    }
    if (call.path === `/repos/${COMMUNITY_REPO}/issues/13`) return [200, { state: "closed" }];
    if (call.path === `/repos/${COMMUNITY_REPO}/issues/13/comments?per_page=100`) {
      return [200, [{ body: "已发布 demo-app-1.0.0。" }]];
    }
    if (call.method === "DELETE") return [500, { message: "server error" }]; // 清理失败
    return null;
  });

  try {
    gh.setRepo(COMMUNITY_REPO);
    gh.setToken(TOKEN);
    gh._internals.setRetryDelays([0, 0, 0]);
    const result = await gh.publish({
      appId: "demo-app",
      version: "1.0.0",
      filePath: pkg,
      manifest: SAMPLE_MANIFEST,
      intervalMs: 1,
      maxAttempts: 3,
      sleep: async () => {},
    });

    assert.equal(result.status, "published", "清理失败不能把发布判为失败");
    assert.equal(result.staging.cleanup.attempted, true);
    assert.equal(result.staging.cleanup.ok, false);
    assert.match(result.staging.cleanup.warning, /中转 Release 未删除/);
    assert.ok(!result.staging.cleanup.warning.includes(TOKEN));
    // 已存在的仓库不会再被创建
    assert.ok(!mock.paths().includes("POST /user/repos"));
  } finally {
    gh._internals.setRetryDelays();
    mock.restore();
    if (prevDir === undefined) delete process.env.CHATCODER_COMMUNITY_DIR;
    else process.env.CHATCODER_COMMUNITY_DIR = prevDir;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("mock：PAT 无效（401）立即抛 BAD_TOKEN，不重试", async () => {
  await withTempDir(async (dir) => {
    const pkg = path.join(dir, "demo-app-1.0.0.ccapp");
    fs.writeFileSync(pkg, "bytes");
    const mock = mockHttps(() => [401, { message: "Bad credentials" }]);
    try {
      gh.setRepo(COMMUNITY_REPO);
      gh.setToken(TOKEN);
      await assert.rejects(
        gh.publish({ appId: "demo-app", version: "1.0.0", filePath: pkg, intervalMs: 1, maxAttempts: 2, sleep: async () => {} }),
        (e) => e.code === "BAD_TOKEN" && /Bad credentials/.test(e.message) && !e.message.includes(TOKEN),
      );
      assert.equal(mock.calls.length, 1, "401 不应重试");
    } finally {
      mock.restore();
    }
  });
});

test("mock：5xx 按 2/4/8 重试 3 次后失败（共 4 次请求）", async () => {
  await withTempDir(async (dir) => {
    const pkg = path.join(dir, "demo-app-1.0.0.ccapp");
    fs.writeFileSync(pkg, "bytes");
    const mock = mockHttps(() => [503, { message: "unavailable" }]);
    try {
      gh.setRepo(COMMUNITY_REPO);
      gh.setToken(TOKEN);
      gh._internals.setRetryDelays([0, 0, 0]);
      await assert.rejects(
        gh.publish({ appId: "demo-app", version: "1.0.0", filePath: pkg, intervalMs: 1, maxAttempts: 2, sleep: async () => {} }),
        (e) => e.code === "HTTP_503",
      );
      assert.equal(mock.calls.length, 4, "首次 + 3 次重试");
      assert.ok(mock.paths().every((p) => p === "GET /user"));
    } finally {
      gh._internals.setRetryDelays();
      mock.restore();
    }
  });
});

test("mock：轮询到上限仍未关闭 → status=pending + code=TIMEOUT（不判失败、不清理中转）", async () => {
  await withTempDir(async (dir) => {
    const pkg = path.join(dir, "demo-app-1.0.0.ccapp");
    fs.writeFileSync(pkg, "bytes");
    const mock = mockHttps((call) => {
      if (call.host === "uploads.github.com") return [201, { browser_download_url: ASSET_URL }];
      if (call.path === "/user") return [200, { login: "alice" }];
      if (call.path === `/repos/${STAGING_REPO_FULL}`) return [200, { owner: { login: "alice" } }];
      if (call.path === `/repos/${STAGING_REPO_FULL}/releases`) return [201, { id: 999 }];
      if (call.path === `/repos/${COMMUNITY_REPO}/issues`) {
        return [201, { number: 14, html_url: `https://github.com/${COMMUNITY_REPO}/issues/14` }];
      }
      if (call.path === `/repos/${COMMUNITY_REPO}/issues/14`) return [200, { state: "open" }];
      if (call.path === `/repos/${COMMUNITY_REPO}/issues/14/comments?per_page=100`) return [200, []];
      return null;
    });
    try {
      gh.setRepo(COMMUNITY_REPO);
      gh.setToken(TOKEN);
      const result = await gh.publish({
        appId: "demo-app",
        version: "1.0.0",
        filePath: pkg,
        intervalMs: 1,
        maxAttempts: 2,
        sleep: async () => {},
      });
      assert.equal(result.status, "pending");
      assert.equal(result.code, "TIMEOUT");
      assert.equal(result.timedOut, true);
      assert.match(result.message, /稍后打开该链接查看结果/);
      assert.equal(result.staging.cleanup.attempted, false, "没确认成功就不能删中转包");
      assert.ok(!mock.paths().some((p) => p.startsWith("DELETE")));
    } finally {
      mock.restore();
    }
  });
});

test("mock：withdraw 提单 → 轮询关闭 → withdrawn（不碰任何 Release，D35）", async () => {
  await withTempDir(async () => {
    const mock = mockHttps((call) => {
      if (call.path === `/repos/${COMMUNITY_REPO}/issues` && call.method === "POST") {
        return [201, { number: 21, html_url: `https://github.com/${COMMUNITY_REPO}/issues/21` }];
      }
      if (call.path === `/repos/${COMMUNITY_REPO}/issues/21`) return [200, { state: "closed" }];
      if (call.path === `/repos/${COMMUNITY_REPO}/issues/21/comments?per_page=100`) {
        return [200, [{ body: "已从索引移除 demo-app@1.0.0。Release 里的包保留。" }]];
      }
      return null;
    });
    try {
      gh.setRepo(COMMUNITY_REPO);
      gh.setToken(TOKEN);
      const result = await gh.withdraw({ appId: "demo-app", version: "1.0.0", intervalMs: 1, maxAttempts: 3, sleep: async () => {} });
      assert.equal(result.status, "withdrawn");
      assert.equal(result.issueNumber, 21);
      const issueCall = mock.calls.find((c) => c.path === `/repos/${COMMUNITY_REPO}/issues`);
      const body = JSON.parse(issueCall.body);
      assert.equal(body.title, "[withdraw] demo-app@1.0.0");
      assert.deepEqual(JSON.parse(extractJsonFence(body.body)), { appId: "demo-app", version: "1.0.0", authorId: "", authorName: "", reason: "" });
      assert.ok(!mock.paths().some((p) => p.startsWith("DELETE")), "撤回不应删除 Release（D35）");
    } finally {
      mock.restore();
    }
  });
});

test("mock：Actions 回执为失败 → status=rejected（不当作发布成功）", async () => {
  await withTempDir(async (dir) => {
    const pkg = path.join(dir, "demo-app-1.0.0.ccapp");
    fs.writeFileSync(pkg, "bytes");
    const mock = mockHttps((call) => {
      if (call.host === "uploads.github.com") return [201, { browser_download_url: ASSET_URL }];
      if (call.path === "/user") return [200, { login: "alice" }];
      if (call.path === `/repos/${STAGING_REPO_FULL}`) return [200, { owner: { login: "alice" } }];
      if (call.path === `/repos/${STAGING_REPO_FULL}/releases`) return [201, { id: 1000 }];
      if (call.path === `/repos/${COMMUNITY_REPO}/issues`) {
        return [201, { number: 15, html_url: `https://github.com/${COMMUNITY_REPO}/issues/15` }];
      }
      if (call.path === `/repos/${COMMUNITY_REPO}/issues/15`) return [200, { state: "closed" }];
      if (call.path === `/repos/${COMMUNITY_REPO}/issues/15/comments?per_page=100`) {
        return [200, [{ body: "版本 demo-app-1.0.0 已存在且不允许覆盖，请升版本后重新发布。" }]];
      }
      return null;
    });
    try {
      gh.setRepo(COMMUNITY_REPO);
      gh.setToken(TOKEN);
      const result = await gh.publish({
        appId: "demo-app", version: "1.0.0", filePath: pkg, intervalMs: 1, maxAttempts: 3, sleep: async () => {},
      });
      assert.equal(result.status, "rejected");
      assert.equal(result.staging.cleanup.attempted, false, "被拒时保留中转包，便于用户排查");
      assert.match(result.comment, /不允许覆盖/);
    } finally {
      mock.restore();
    }
  });
});

// ─── 8. networkFailure ──────────────────────────────────────────────────────

test("networkFailure：底层异常包成 NETWORK，已有 code 的原样返回", () => {
  const { networkFailure, communityError: mk } = gh._internals;
  const wrapped = networkFailure(new Error("getaddrinfo ENOTFOUND api.github.com"), "校验 PAT");
  assert.equal(wrapped.code, "NETWORK");
  assert.match(wrapped.message, /无法连接 GitHub/);
  const timeout = mk("TIMEOUT_LOCAL", "请求超时");
  assert.equal(networkFailure(timeout, "校验 PAT"), timeout);
});
