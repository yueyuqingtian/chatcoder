// 微应用库根解析（plan-693-2868 §6.1「库目录与根统一」/ D6 / D27）
//
// 为什么单独抽一个模块（而不是在 main.cjs 里内联）：
//   1. 库根有两个使用者——主进程的 app:// 协议 handler（读文件）与 spawn 后端的 env
//      `CHATCODER_MICROAPP_ROOT`（Python 侧落库）；必须是**同一套解析链**，否则
//      「协议读的目录」与「后端写的目录」会分裂（方案 §8 风险「双用户根 → 落点不一致」）。
//   2. 方案 §7 P0-3 只要求协议 + CSP，但根解析是它的前置条件，独立成文件便于后面 P3
//      （settings.microappRoot 设置项）在此处扩展，不必再动 main.cjs 的协议代码。
//
// 解析顺序（与方案 §6.1 一致的分级降级）：
//   ① 环境变量 CHATCODER_MICROAPP_ROOT（用户/开发显式指定，最高优先）
//   ② Electron userData 下的 microapps（打包后 %APPDATA%\chatcoder\microapps，
//      与 main.cjs:111/836 等处既有 userData 用法同根）
//   ③ 兜底 <home>/.chatcoder/microapps（方案 §6.1 写的默认位置；非 Electron 环境
//      或 getPath 不可用时走这里，Python 侧缺省也是这个路径，两边天然对齐）
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const DIR_NAME = "microapps";

/**
 * 确保目录存在（递归创建）。失败不抛错——调用方（协议 handler / spawn）随后会因
 * 文件读取失败各自返回 404 或由后端记录日志，启动路径不应因为目录创建失败而中断。
 * @param {string} dir
 * @returns {string} 原样返回 dir，便于链式使用
 */
function ensureDir(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch {
    /* 权限不足/路径非法：留给上层按各自语义处理 */
  }
  return dir;
}

/** 取 Electron 的 app 实例；非 Electron 环境（node 脚本、单测）返回 null。 */
function getElectronApp() {
  try {
    const electron = require("electron");
    // 非 Electron 运行时 require("electron") 返回的是可执行文件路径字符串，不是对象
    return electron && typeof electron === "object" && electron.app ? electron.app : null;
  } catch {
    return null;
  }
}

/** 解析微应用库根；返回绝对路径，并已确保目录存在。 */
function resolveMicroAppRoot() {
  // ① 环境变量优先：显式配置（含 P3 设置页将来写入的值）永远压过默认推导
  const fromEnv = String(process.env.CHATCODER_MICROAPP_ROOT || "").trim();
  if (fromEnv) return ensureDir(path.resolve(fromEnv));

  // ② 默认落用户目录（方案 D6：默认 ~/.chatcoder/microapps，不落安装目录）。
  //    这里刻意**不用** app.getPath("userData")——那是 %APPDATA%\chatcoder，与 Python 侧
  //    读的 ~/.chatcoder 是两个根，走它会让 Electron 与后端各写各的（2866 缺陷 #4 的双根问题）。
  //    getElectronApp() 保留给 P6 独立窗口取 userData 用，当前路径不消费。
  return ensureDir(path.join(os.homedir(), ".chatcoder", DIR_NAME));
}

module.exports = { resolveMicroAppRoot, ensureDir };
