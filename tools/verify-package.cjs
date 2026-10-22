#!/usr/bin/env node
"use strict";
/**
 * 打包产物自检（build-release.ps1 在 electron-builder 之后调用；本地与 GitHub Actions 同一份）。
 *
 * 为什么需要：package.json 的 build.files 是白名单，新增的 electron 模块忘了登记时，
 * 打包照样成功，装好的应用却在启动时报 Cannot find module——0.9.1 的 update-source-policy.cjs
 * 就差点这样漏掉。这里直接读 app.asar，把「主进程模块的相对 require 都能解析」
 * 「随包资源都在」变成发布前的硬性检查。
 *
 * 用法: node tools/verify-package.cjs [win-unpacked 目录]   （缺省读 package.json 的输出目录）
 */
const fs = require("fs");
const path = require("path");
const asar = require("@electron/asar");

const root = path.resolve(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const outDir = (pkg.build && pkg.build.directories && pkg.build.directories.output) || "dist";
const unpacked = path.resolve(process.argv[2] || path.join(root, outDir, "win-unpacked"));
const resources = path.join(unpacked, "resources");
const archive = path.join(resources, "app.asar");

const problems = [];
const fail = (msg) => problems.push(msg);

if (!fs.existsSync(archive)) {
  console.error(`[verify-package] 找不到 ${archive}，请先完成 electron-builder 打包`);
  process.exit(1);
}

// asar 内的路径统一成 a/b/c 形式
const entries = new Set(asar.listPackage(archive).map((p) => p.replace(/\\/g, "/").replace(/^\//, "")));
const unpackedDir = archive + ".unpacked";
const exists = (rel) => entries.has(rel) || fs.existsSync(path.join(unpackedDir, rel));

/** 解析相对 require：按 Node 规则补全扩展名 / index */
function resolveRel(fromRel, spec) {
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(fromRel), spec));
  const candidates = [base, `${base}.cjs`, `${base}.js`, `${base}.json`, `${base}/index.js`, `${base}/index.cjs`];
  return candidates.find(exists) || null;
}

// 1. 主进程模块：每个静态相对 require 都必须在包里
const mainModules = [...entries].filter((p) => /^electron\/[^/]+\.cjs$/.test(p));
if (!mainModules.includes("electron/main.cjs")) fail("app.asar 缺少 electron/main.cjs");
for (const rel of mainModules) {
  if (/\.(test|harness)\.cjs$/.test(rel)) fail(`测试文件被打进了安装包：${rel}`);
  const src = asar.extractFile(archive, rel).toString("utf8");
  for (const m of src.matchAll(/require\(\s*["'](\.{1,2}\/[^"']+)["']\s*\)/g)) {
    if (!resolveRel(rel, m[1])) fail(`${rel} 依赖的 ${m[1]} 不在安装包里（检查 package.json build.files）`);
  }
}

// 2. 运行期必需的第三方模块
for (const mod of ["electron-updater", "builder-util-runtime"]) {
  if (!exists(`node_modules/${mod}/package.json`)) fail(`安装包缺少依赖 ${mod}`);
}

// 3. 随包资源（extraResources）
const required = [
  ["frontend/index.html", "前端页面"],
  ["server/chatcoder-server/chatcoder-server.exe", "后端主程序"],
  ["server/chatcoder-server/chatcoder-index-worker.exe", "索引 worker"],
  ["server/chatcoder-server/_internal/desktop-core/desktop-core.exe", "桌面操控内核"],
  ["CHANGELOG.md", "更新历史"],
  ["icon.png", "应用图标"],
];
for (const [rel, label] of required) {
  if (!fs.existsSync(path.join(resources, rel))) fail(`缺少${label}：resources/${rel}`);
}
const browsers = path.join(resources, "server", "chatcoder-server", "_internal", "ms-playwright");
const chromium = fs.existsSync(browsers)
  ? fs.readdirSync(browsers).find((d) => d.startsWith("chromium-")
      && fs.existsSync(path.join(browsers, d, "INSTALLATION_COMPLETE"))
      && fs.existsSync(path.join(browsers, d, "chrome-win64", "chrome.exe")))
  : null;
if (!chromium) fail("缺少内置 Chromium（resources/server/chatcoder-server/_internal/ms-playwright）");

if (problems.length) {
  console.error("[verify-package] 安装包自检未通过：\n" + problems.map((p) => "  - " + p).join("\n"));
  process.exit(1);
}
console.log(`[verify-package] 自检通过：${mainModules.length} 个主进程模块的相对依赖齐全，内置浏览器 ${chromium}`);
