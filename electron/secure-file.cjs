/**
 * 私有小文件的读写（0600 权限 + 临时文件 + rename 原子替换）
 * 依据：plan-693-2868 §6.4「PAT 引导与存储」/ D20 / D36
 *
 * 为什么单独抽一个模块（而不是在第三个调用点再手写一遍）：
 *   项目里已经有两处「写敏感/关键小文件」的先例，各自手写了一遍同一套动作：
 *     · electron/plugin-bridge.cjs:75 —— 写 `.bridge.json`（带令牌）用的是 `mode: 0o600`；
 *     · electron/cordis-host.cjs:184-190 —— 写插件注册表用的是 tmp + `renameSync` 原子替换。
 *   第三处（社区 PAT 与社区偏好）如果继续手写，最容易出两种偏差，而且都是「平时看不出来、
 *   出问题时才发现」的那种：
 *     ① 漏掉 `mode: 0o600` —— 文件按默认 umask 落盘，同机其他账户可读；PAT 一旦明文降级就等于公开；
 *     ② 直接 `writeFileSync` 覆盖 —— 写入过程中崩溃/断电会留下半截 JSON，下次启动解析失败，
 *        表现为「用户明明填过 PAT，软件又说没填」，排查成本很高。
 *   合成一处后，mode、临时文件命名、父目录创建、失败时清理半成品都只维护一份。
 *
 * 注意：Windows 上 POSIX 权限位只是部分生效（文件不继承别人可读的 ACL），保留 `mode` 是为了
 * 同一份代码在 Linux/macOS 上也安全，不是 Windows 专属逻辑。
 *
 * 本模块只做文件层，不关心内容语义；调用方负责不把内容回显到日志。
 */
"use strict";

const fs = require("fs");
const path = require("path");

/**
 * 确保目标文件的父目录存在。
 * @param {string} file 目标文件绝对路径
 */
function ensureParent(file) {
  const dir = path.dirname(file);
  if (dir) fs.mkdirSync(dir, { recursive: true });
}

/**
 * 原子写入私有文件。临时文件名带 pid + 随机后缀：同目录并发写（多窗口/重入）不会互相截断。
 * @param {string} file 目标文件绝对路径
 * @param {string|Buffer} data 内容
 * @returns {string} 原样返回 file，便于链式使用
 */
function writePrivateFile(file, data) {
  ensureParent(file);
  const tmp = `${file}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
  try {
    // mode 只在「创建」时生效，所以每次都用新的临时文件名，不能复用固定 .tmp
    fs.writeFileSync(tmp, data, { mode: 0o600 });
    fs.renameSync(tmp, file);
    return file;
  } catch (e) {
    // 失败也要把半截临时文件清掉，否则下次目录里会留垃圾（含敏感内容时更糟）
    try { fs.rmSync(tmp, { force: true }); } catch { /* 清理失败不掩盖原始错误 */ }
    throw e;
  }
}

/**
 * 原子写入私有 JSON（UTF-8，2 空格缩进，便于用户手工查看排查）。
 * @param {string} file 目标文件绝对路径
 * @param {any} obj 可序列化对象
 */
function writePrivateJson(file, obj) {
  return writePrivateFile(file, JSON.stringify(obj, null, 2));
}

/**
 * 读取私有文件原始字节；不存在或读取失败返回 null（不抛错——「没有」是正常状态）。
 * @param {string} file
 * @returns {Buffer|null}
 */
function readPrivateFile(file) {
  try {
    return fs.readFileSync(file);
  } catch {
    return null;
  }
}

/**
 * 读取并解析私有 JSON；不存在、读不到或解析失败一律返回 null。
 * 故意不抛错：这些文件是「可有可无的偏好」，损坏时应当按默认值继续，而不是让主进程启动失败。
 * @param {string} file
 * @returns {any|null}
 */
function readPrivateJson(file) {
  const buf = readPrivateFile(file);
  if (!buf || !buf.length) return null;
  try {
    return JSON.parse(buf.toString("utf8"));
  } catch {
    return null;
  }
}

/**
 * 删除私有文件（不存在视为成功）。
 * @param {string} file
 * @returns {boolean} true 表示已删除/本来就不存在
 */
function removePrivateFile(file) {
  try {
    fs.rmSync(file, { force: true });
    return true;
  } catch {
    return false;
  }
}

module.exports = {
  writePrivateFile,
  writePrivateJson,
  readPrivateFile,
  readPrivateJson,
  removePrivateFile,
  ensureParent,
};
