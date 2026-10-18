/**
 * 第三方插件工具的本机桥接（plan-20261017-side-session 第 12 点）。
 *
 * 插件运行在 Electron 主进程（见 cordis-host.cjs），而 Agent 运行在后端进程。
 * 本模块在 127.0.0.1 上开一个带令牌的 HTTP 端口，把插件注册的工具暴露给 MCP 桥（plugin-mcp.cjs），
 * 由后者以标准 MCP 连接器的形式接入 Agent——与内置的数据库 / 调试连接器走同一条工具链路，
 * 因此审批、风险分级与工具展示都与现有连接器一致。
 *
 * 端点与令牌写入插件根目录的 `.bridge.json`，MCP 桥每次列出工具时重新读取，启动顺序与端口变化都不影响连接。
 */
const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const BRIDGE_FILE = ".bridge.json";
const TOKEN_HEADER = "x-chatcoder-token";

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 2 * 1024 * 1024) {
        reject(new Error("请求体过大"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

function sendJson(res, status, payload) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(payload));
}

/**
 * 启动桥接服务。返回 { endpoint, file, close }；关闭时删除 .bridge.json，避免残留的过期端点。
 * @param {{ host: { listTools: Function, callTool: Function }, root: string, log?: Function }} options
 */
function startPluginBridge({ host, root, log = () => {} }) {
  const token = crypto.randomBytes(24).toString("hex");
  const file = path.join(root, BRIDGE_FILE);

  const server = http.createServer(async (req, res) => {
    if (req.headers[TOKEN_HEADER] !== token) {
      sendJson(res, 401, { error: "unauthorized" });
      return;
    }
    try {
      if (req.method === "GET" && req.url === "/tools") {
        sendJson(res, 200, { tools: host.listTools() });
        return;
      }
      if (req.method === "POST" && req.url === "/call") {
        const body = JSON.parse((await readBody(req)) || "{}");
        const result = await host.callTool(body.plugin, body.name, body.args);
        sendJson(res, 200, { ok: true, result });
        return;
      }
      sendJson(res, 404, { error: "not found" });
    } catch (e) {
      sendJson(res, 200, { ok: false, error: String((e && e.message) || e) });
    }
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      const endpoint = `http://127.0.0.1:${port}`;
      fs.mkdirSync(root, { recursive: true });
      fs.writeFileSync(file, JSON.stringify({ endpoint, token, pid: process.pid }), { encoding: "utf-8", mode: 0o600 });
      log(`[plugin-bridge] 已监听 ${endpoint}`);
      resolve({
        endpoint,
        file,
        close: () => {
          try { fs.rmSync(file, { force: true }); } catch { /* 退出路径尽力而为 */ }
          server.close();
        },
      });
    });
  });
}

module.exports = { startPluginBridge, BRIDGE_FILE, TOKEN_HEADER };
