/**
 * dsh 第三方插件宿主（plan-20261017-side-session 第 12 点，按用户收窄的范围：只兼容第三方插件协议）。
 *
 * 协议依据（对照 DeepSeek Harness 源码核实）：
 *   - 插件是 npm 包，`package.json` 含 `name`、`version`；
 *   - 宿主（host）半：`dsh.bundle.patch` 为一个或有序列表的 Cordis 组合文件（YAML，相对包根）；
 *   - 客户端（client）半：`dsh.client = { platform: 'web', inject?, immediately? }`，配套 `exports["./client"]`；
 *     本软件运行宿主半；客户端半依赖 dsh 自己的 slot / boot 体系，不在此运行，状态里明确标注；
 *   - `dsh.profile.bundles` 是 dsh profile 自身的层列表，第三方包无需声明；
 *   - `dsh.manifestVersion` 在 dsh 中不存在（历史字段），这里仅保留可选兼容；
 *   - 组合文件中的 `name` 为插件模块（相对路径或包名，包名从插件目录的 node_modules 解析）。
 *
 * 运行时：vendor/ 下的 Cordis 4.0.4 及其 loader / include 插件（MIT，原样拷贝，见 vendor/README.md），
 * 与 dsh 的加载语义一致（`inject`、`insert`、`!!js` 表达式等由 Cordis 负责）。
 * 每个插件包使用独立的根 Context，单个包失败只影响它自己。
 *
 * 安装目录：`~/.chatcoder/plugins/<包名安全化>/`；启停状态记录在同目录的 `registry.json`。
 */
const fs = require("fs");
const path = require("path");
const os = require("os");
const { pathToFileURL } = require("url");
const { spawn } = require("child_process");

/** 本软件实现的 dsh 协议版本：插件 `engines.dsh` 不满足时拒绝安装 */
const HOST_DSH_VERSION = "0.2.0-rc.2";
/** 支持的清单格式版本（`dsh.manifestVersion`，dsh 已无此字段，仅兼容旧包） */
const MANIFEST_VERSION = 1;
const VENDOR_DIR = path.join(__dirname, "vendor", "node_modules");
const REGISTRY_FILE = "registry.json";
const TOOL_NAME_RE = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;
const TOOL_RISKS = new Set(["low", "medium", "high"]);

function defaultRoot() {
  return process.env.CHATCODER_PLUGIN_ROOT || path.join(os.homedir(), ".chatcoder", "plugins");
}

/** 懒加载 vendor 的 Cordis 运行时（同一进程只加载一次）。 */
let runtimePromise = null;
function loadRuntime() {
  if (!runtimePromise) {
    const url = (rel) => pathToFileURL(path.join(VENDOR_DIR, rel)).href;
    runtimePromise = Promise.all([
      import(url("@deepseek-ai/cordis/lib/index.js")),
      import(url("@deepseek-ai/cordis-plugin-loader/lib/index.js")),
      import(url("@deepseek-ai/cordis-plugin-include/lib/index.js")),
    ]).then(([cordis, loader, include]) => ({
      Context: cordis.Context,
      Loader: loader.default || loader.Loader,
      Include: include.default || include.Include,
    }));
  }
  return runtimePromise;
}

function semver() {
  return require(path.join(VENDOR_DIR, "semver"));
}

/**
 * 找出组合文件中声明了 inject、但宿主与插件根上下文都不提供的服务名（插件会因此不会运行）。
 * 只做静态检查（解析组合文件 + 检查根上下文上的服务），不依赖 loader 的内部结构。
 * 局限：由同一插件包内其它插件提供的服务，在根上下文上不可见，会被报告为缺失。
 */
function findMissingServices(patchFiles, ctx) {
  const yaml = require(path.join(VENDOR_DIR, "js-yaml"));
  const required = new Set();
  const ids = new Set();
  const visit = (node) => {
    if (Array.isArray(node)) { node.forEach(visit); return; }
    if (!node || typeof node !== "object") return;
    if (typeof node.id === "string") ids.add(node.id);
    const inj = node.inject;
    if (typeof inj === "string") required.add(inj);
    else if (Array.isArray(inj)) inj.forEach((n) => typeof n === "string" && required.add(n));
    if (node.insert) visit(node.insert);
  };
  for (const file of patchFiles) {
    try {
      // 组合文件中的 !!js 表达式不在此求值，只读取声明结构
      visit(yaml.load(fs.readFileSync(file, "utf-8"), { schema: yaml.JSON_SCHEMA }));
    } catch {
      // 解析失败会在加载阶段以插件错误的形式报告，这里不重复
    }
  }
  return [...required].filter((n) => !ids.has(n) && ctx[n] === undefined).sort();
}

/** 包名 → 安装目录名（scope 与分隔符替换为双下划线，避免路径穿越） */
function safeDirName(pkgName) {
  return String(pkgName).replace(/^@/, "").replace(/[\\/]+/g, "__").replace(/[^A-Za-z0-9._-]/g, "_");
}

/**
 * 校验 dsh 第三方插件的包清单，返回组合文件的绝对路径列表。
 * 任一项不满足都抛出中文错误，安装与加载共用同一套规则。
 */
function validatePackage(pkg, dir) {
  if (!pkg || typeof pkg !== "object") throw new Error("package.json 不是有效的对象");
  if (typeof pkg.name !== "string" || !pkg.name.trim()) throw new Error("package.json 缺少 name");
  if (typeof pkg.version !== "string" || !pkg.version.trim()) throw new Error("package.json 缺少 version");

  const range = pkg.engines && pkg.engines.dsh;
  if (range !== undefined) {
    if (typeof range !== "string" || !semver().validRange(range)) {
      throw new Error(`engines.dsh 不是有效的版本范围：${range}`);
    }
    if (!semver().satisfies(HOST_DSH_VERSION, range, { includePrerelease: true })) {
      throw new Error(`插件要求 dsh ${range}，本软件兼容 dsh ${HOST_DSH_VERSION}`);
    }
  }

  const dsh = pkg.dsh || {};
  if (dsh.manifestVersion !== undefined && dsh.manifestVersion !== MANIFEST_VERSION) {
    throw new Error(`不支持的插件清单版本 ${dsh.manifestVersion}（本软件支持 ${MANIFEST_VERSION}）`);
  }

  const patch = dsh.bundle && dsh.bundle.patch;
  if (patch === undefined) throw new Error("缺少 dsh.bundle.patch（Cordis 组合文件）");
  const list = Array.isArray(patch) ? patch : [patch];
  if (list.length === 0) throw new Error("dsh.bundle.patch 不能为空");
  const root = path.resolve(dir);
  return list.map((rel) => {
    if (typeof rel !== "string" || !rel.trim()) throw new Error("dsh.bundle.patch 含无效路径");
    const abs = path.resolve(root, rel);
    if (abs !== root && !abs.startsWith(root + path.sep)) throw new Error(`组合文件超出插件目录：${rel}`);
    if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) throw new Error(`找不到组合文件：${rel}`);
    return abs;
  });
}

function readPackage(dir) {
  const file = path.join(dir, "package.json");
  if (!fs.existsSync(file)) throw new Error("目录中没有 package.json");
  return JSON.parse(fs.readFileSync(file, "utf-8"));
}

/**
 * 读取并校验 `dsh.client`（dsh 的客户端半声明）。
 * dsh 的合法形态：`{ platform: 'web', inject?: string[], immediately?: boolean }`，
 * 且必须配套 `exports["./client"]`（字符串或带字符串 default 的对象）。
 * 返回 null 表示该包没有客户端模块；声明不合法时抛错，与 dsh 的安装期校验一致。
 */
function readClientDecl(pkg) {
  const decl = pkg && pkg.dsh && pkg.dsh.client;
  if (decl === undefined) return null;
  if (!decl || typeof decl !== "object" || Array.isArray(decl)) throw new Error("dsh.client 必须是对象");
  if (typeof decl.platform !== "string" || !decl.platform.trim()) {
    throw new Error("dsh.client.platform 缺失（dsh 当前只支持 web）");
  }
  if (decl.inject !== undefined && (!Array.isArray(decl.inject) || decl.inject.some((n) => typeof n !== "string"))) {
    throw new Error("dsh.client.inject 必须是字符串数组");
  }
  if (decl.immediately !== undefined && typeof decl.immediately !== "boolean") {
    throw new Error("dsh.client.immediately 必须是布尔值");
  }
  const exp = pkg.exports && pkg.exports["./client"];
  const hasExport = typeof exp === "string" || Boolean(exp && typeof exp === "object" && typeof exp.default === "string");
  if (!hasExport) throw new Error('dsh.client 需要配套 exports["./client"]（客户端模块入口）');
  return {
    platform: decl.platform,
    inject: Array.isArray(decl.inject) ? decl.inject : [],
    immediately: Boolean(decl.immediately),
  };
}

function listPackageDirs(root) {
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root, { withFileTypes: true })
    .filter((d) => d.isDirectory() && fs.existsSync(path.join(root, d.name, "package.json")))
    .map((d) => path.join(root, d.name))
    .sort();
}

function readRegistry(root) {
  try {
    const data = JSON.parse(fs.readFileSync(path.join(root, REGISTRY_FILE), "utf-8"));
    return { disabled: Array.isArray(data.disabled) ? data.disabled.map(String) : [] };
  } catch {
    return { disabled: [] };
  }
}

function writeRegistry(root, registry) {
  fs.mkdirSync(root, { recursive: true });
  const file = path.join(root, REGISTRY_FILE);
  const tmp = file + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(registry, null, 2), "utf-8");
  fs.renameSync(tmp, file);
}

/** 工具执行结果统一为 { text, data }：字符串直接作为文本，对象若带 text 则保留，其余序列化为 JSON。 */
function normalizeToolResult(out) {
  if (typeof out === "string") return { text: out };
  if (out && typeof out === "object" && typeof out.text === "string") return { text: out.text, data: out.data };
  return { text: JSON.stringify(out === undefined ? null : out) };
}

function createHost(options = {}) {
  const log = options.log || (() => {});
  const logErr = options.logErr || log;
  const sendToRenderer = options.sendToRenderer || (() => {});

  /** 已加载（或失败、停用）的插件包状态，供设置页展示；_fibers 仅内部用于卸载。 */
  let loaded = [];
  /** 插件注册的工具：pluginName -> Map(toolName -> spec)。卸载时整体清空，随加载重建。 */
  let toolRegistry = new Map();

  /** 注入给插件的宿主服务（服务名 chatcoderHost）：界面通知与消息推送。 */
  function hostServices(pluginName) {
    return {
      name: pluginName,
      notify(message, level = "info") {
        sendToRenderer("cordis:notify", { plugin: pluginName, level, message: String(message) });
      },
      send(channel, payload) {
        sendToRenderer("cordis:message", { plugin: pluginName, channel: String(channel), payload });
      },
      /** 注册一个模型可调用的工具（经连接器「第三方插件」暴露给 Agent）。
       *  spec: { name, description, parameters (JSON Schema), risk: low|medium|high, execute(args) => string | {text, data} }
       *  返回注销函数。风险默认 medium（需要审批），与内置 MCP 工具的默认一致。 */
      registerTool(spec) {
        if (!spec || typeof spec !== "object") throw new Error("registerTool 需要传入对象");
        const name = String(spec.name || "");
        if (!TOOL_NAME_RE.test(name)) throw new Error(`工具名不合法：${name}（需字母开头，仅含字母、数字与下划线，最长 48）`);
        if (typeof spec.execute !== "function") throw new Error(`工具 ${name} 缺少 execute 函数`);
        const bucket = toolRegistry.get(pluginName) || new Map();
        bucket.set(name, {
          name,
          description: String(spec.description || name),
          parameters: spec.parameters && typeof spec.parameters === "object" ? spec.parameters : { type: "object", properties: {} },
          risk: TOOL_RISKS.has(spec.risk) ? spec.risk : "medium",
          execute: spec.execute,
        });
        toolRegistry.set(pluginName, bucket);
        return () => { toolRegistry.get(pluginName)?.delete(name); };
      },
    };
  }

  /** 当前已注册的全部工具（供桥接层给连接器列出）。 */
  function listTools() {
    const out = [];
    for (const [plugin, bucket] of toolRegistry) {
      for (const t of bucket.values()) {
        out.push({ plugin, name: t.name, description: t.description, parameters: t.parameters, risk: t.risk });
      }
    }
    return out;
  }

  /** 执行一个插件工具；插件抛错原样返回给调用方（由桥接层转为工具错误）。 */
  async function callTool(plugin, name, args) {
    const tool = toolRegistry.get(String(plugin))?.get(String(name));
    if (!tool) throw new Error(`插件工具不存在：${plugin}/${name}`);
    return normalizeToolResult(await tool.execute(args && typeof args === "object" ? args : {}));
  }


  async function loadPackage(dir, registry) {
    const dirName = path.basename(dir);
    const st = { id: dirName, dir: dirName, name: dirName, version: null, description: "", status: "loading", error: null, warnings: [], hasClient: false, clientPlatform: null, _fibers: [] };
    loaded.push(st);
    try {
      const pkg = readPackage(dir);
      st.name = pkg.name || dirName;
      st.version = pkg.version || null;
      st.description = typeof pkg.description === "string" ? pkg.description : "";
      const patches = validatePackage(pkg, dir);
      // 客户端半只做识别与告知：它依赖 dsh 自己的 slot / boot 体系，本软件不运行它
      const client = readClientDecl(pkg);
      st.hasClient = Boolean(client);
      st.clientPlatform = client ? client.platform : null;
      if (client) {
        st.warnings.push(
          `含 dsh 客户端模块（platform: ${client.platform}）：本软件只运行其宿主半，dsh 的界面扩展不会显示；命令与工具不受影响。`,
        );
      }
      if (registry.disabled.includes(st.name)) {
        st.status = "disabled";
        return st;
      }
      const { Context, Loader, Include } = await loadRuntime();
      const ctx = new Context();
      ctx.baseUrl = pathToFileURL(dir + path.sep).href;
      ctx.provide("chatcoderHost", hostServices(st.name));
      const loaderFiber = ctx.plugin(Loader, { baseUrl: ctx.baseUrl });
      await loaderFiber;
      st._fibers.push(loaderFiber);
      for (const abs of patches) {
        // 组合文件以绝对 file URL 传入：include 会把自身目录设为后续的解析基准，必须每次都写绝对地址
        const fiber = ctx.plugin(Include, { path: pathToFileURL(abs).href, initial: [] });
        await fiber;
        st._fibers.push(fiber);
      }
      await ctx.loader.await();
      const missing = findMissingServices(patches, ctx);
      if (missing.length) st.warnings.push(`缺少服务：${missing.join("、")}（依赖它们的插件功能不会生效）`);
      st.status = "loaded";
      log(`[cordis] 已加载插件 ${st.name}@${st.version}`);
    } catch (e) {
      st.status = "failed";
      st.error = String(e && e.message ? e.message : e);
      logErr(`[cordis] 插件 ${st.name}（${dirName}）加载失败：${st.error}`);
    }
    return st;
  }

  /** 卸载全部插件：逆序销毁各包的插件树，清空状态。 */
  function unload() {
    for (const st of loaded.slice().reverse()) {
      for (const fiber of (st._fibers || []).slice().reverse()) {
        try { fiber.dispose(); } catch (e) { logErr(`[cordis] 卸载 ${st.name} 失败：${e && e.message}`); }
      }
    }
    loaded = [];
    toolRegistry = new Map();
  }

  /** 扫描根目录并加载所有已安装的插件包（设置页「重新加载」与启动时调用）。 */
  async function load(root = defaultRoot()) {
    unload();
    const registry = readRegistry(root);
    for (const dir of listPackageDirs(root)) {
      await loadPackage(dir, registry);
    }
    return status();
  }

  function publicView({ _fibers, ...rest }) {
    return rest;
  }

  function status() {
    return loaded.map(publicView);
  }

  /** 把 npm pack 的 tgz 解到临时目录，返回包根（npm 约定为 package/ 子目录）。 */
  function extractTarball(file) {
    const stage = fs.mkdtempSync(path.join(os.tmpdir(), "chatcoder-plugin-"));
    return new Promise((resolve, reject) => {
      const p = spawn("tar", ["-xzf", file, "-C", stage], { windowsHide: true });
      let err = "";
      p.stderr.on("data", (d) => { err += d; });
      p.on("error", (e) => reject(new Error(`无法解压插件包：${e.message}`)));
      p.on("close", (code) => {
        if (code !== 0) return reject(new Error(`无法解压插件包：${err.trim() || "tar 退出码 " + code}`));
        const inner = path.join(stage, "package");
        resolve({ stage, pkgDir: fs.existsSync(path.join(inner, "package.json")) ? inner : stage });
      });
    });
  }

  function npmInstall(cwd) {
    return new Promise((resolve, reject) => {
      const isWin = process.platform === "win32";
      const p = spawn(isWin ? "npm.cmd" : "npm", ["install", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], { cwd, windowsHide: true, shell: false });
      let out = "";
      p.stdout.on("data", (d) => { out += d; });
      p.stderr.on("data", (d) => { out += d; });
      p.on("error", (e) => reject(new Error(`无法运行 npm 安装插件依赖：${e.message}`)));
      p.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`插件依赖安装失败：${out.trim().slice(-400) || "npm 退出码 " + code}`))));
    });
  }

  /**
   * 安装一个第三方插件：源可以是插件目录，也可以是 `npm pack` 生成的 .tgz。
   * 先校验清单，通过后复制到插件根目录；声明了依赖而包内没有 node_modules 时运行 npm 安装（不执行脚本）。
   */
  async function installPackage(root, src) {
    const abs = path.resolve(String(src || ""));
    if (!fs.existsSync(abs)) throw new Error(`找不到插件：${abs}`);
    const isTar = /\.tgz$|\.tar\.gz$/i.test(abs) && fs.statSync(abs).isFile();
    const extracted = isTar ? await extractTarball(abs) : null;
    try {
      const stageDir = extracted ? extracted.pkgDir : abs;
      if (!fs.statSync(stageDir).isDirectory()) throw new Error("插件源必须是目录或 .tgz 包");
      const pkg = readPackage(stageDir);
      validatePackage(pkg, stageDir);
      readClientDecl(pkg);
      const target = path.join(root, safeDirName(pkg.name));
      // 同名插件视为更新：先卸载旧版本，再替换目录
      unload();
      fs.rmSync(target, { recursive: true, force: true });
      fs.mkdirSync(root, { recursive: true });
      fs.cpSync(stageDir, target, { recursive: true });
      const deps = Object.keys(pkg.dependencies || {});
      if (deps.length && !fs.existsSync(path.join(target, "node_modules"))) {
        try {
          await npmInstall(target);
        } catch (e) {
          fs.rmSync(target, { recursive: true, force: true });
          throw e;
        }
      }
      await load(root);
      const hit = loaded.find((s) => s.name === pkg.name);
      return hit ? publicView(hit) : null;
    } finally {
      if (extracted) fs.rmSync(extracted.stage, { recursive: true, force: true });
    }
  }

  /** 卸载插件：销毁运行中的插件并删除安装目录。 */
  async function uninstallPackage(root, name) {
    const dir = listPackageDirs(root).find((d) => {
      try { return readPackage(d).name === name; } catch { return false; }
    });
    if (!dir) throw new Error(`未安装插件：${name}`);
    unload();
    fs.rmSync(dir, { recursive: true, force: true });
    const registry = readRegistry(root);
    writeRegistry(root, { disabled: registry.disabled.filter((n) => n !== name) });
    await load(root);
    return true;
  }

  /** 启用或停用插件（停用后保留安装目录，重新加载时跳过）。 */
  async function setPackageEnabled(root, name, enabled) {
    const registry = readRegistry(root);
    const disabled = new Set(registry.disabled);
    if (enabled) disabled.delete(name); else disabled.add(name);
    writeRegistry(root, { disabled: Array.from(disabled).sort() });
    await load(root);
    const hit = loaded.find((s) => s.name === name);
    return hit ? publicView(hit) : null;
  }

  return {
    load,
    unload,
    status,
    listTools,
    callTool,
    root: defaultRoot,
    install: (src, root = defaultRoot()) => installPackage(root, src),
    uninstall: (name, root = defaultRoot()) => uninstallPackage(root, name),
    setEnabled: (name, enabled, root = defaultRoot()) => setPackageEnabled(root, name, enabled),
  };
}

module.exports = { createHost, validatePackage, readClientDecl, safeDirName, defaultRoot, HOST_DSH_VERSION, MANIFEST_VERSION };
