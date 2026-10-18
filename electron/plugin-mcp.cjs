#!/usr/bin/env node
/**
 * 第三方插件的 MCP 桥（stdio）：把 Electron 主进程里插件注册的工具，以标准 MCP 工具的形式提供给后端 Agent。
 *
 * 用法：`<electron> plugin-mcp.cjs <插件根目录>/.bridge.json`（由「第三方插件」连接器配置，ELECTRON_RUN_AS_NODE=1）。
 * 只实现 MCP 的 initialize / tools/list / tools/call 三个方法（与本项目其它内置 MCP 服务的范围一致）。
 * 工具名为 `<插件名安全化>__<工具名>`，每次调用前都向主进程重新取一次工具表，因此插件变更无需重启连接器。
 */
const fs = require("fs");
const http = require("http");
const readline = require("readline");

const bridgeFile = process.argv[2];
const PROTOCOL_VERSION = "2024-11-05";

function readBridge() {
  if (!bridgeFile) throw new Error("缺少 .bridge.json 路径参数");
  return JSON.parse(fs.readFileSync(bridgeFile, "utf-8"));
}

/** 调主进程桥接的一次 HTTP 请求；桥未运行（应用未启动）时返回明确错误。 */
function bridgeRequest(method, urlPath, body) {
  return new Promise((resolve, reject) => {
    let bridge;
    try {
      bridge = readBridge();
    } catch {
      reject(new Error("第三方插件服务未运行，请先启动应用"));
      return;
    }
    const url = new URL(urlPath, bridge.endpoint);
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const req = http.request(url, {
      method,
      headers: {
        "x-chatcoder-token": bridge.token,
        ...(payload ? { "content-type": "application/json", "content-length": Buffer.byteLength(payload) } : {}),
      },
      timeout: 10 * 60 * 1000,
    }, (res) => {
      let data = "";
      res.setEncoding("utf-8");
      res.on("data", (c) => { data += c; });
      res.on("end", () => {
        try {
          const parsed = JSON.parse(data || "{}");
          if (res.statusCode === 401) reject(new Error("插件服务令牌已失效，请重启应用"));
          else resolve(parsed);
        } catch {
          reject(new Error("插件服务返回了无法解析的内容"));
        }
      });
    });
    req.on("timeout", () => req.destroy(new Error("插件工具执行超时")));
    req.on("error", (e) => reject(new Error(`无法连接插件服务：${e.message}`)));
    if (payload) req.write(payload);
    req.end();
  });
}

const safe = (s) => String(s).replace(/^@/, "").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
const toolName = (t) => `${safe(t.plugin)}__${t.name}`;

async function listTools() {
  const res = await bridgeRequest("GET", "/tools");
  return (res.tools || []).map((t) => ({
    name: toolName(t),
    description: t.description,
    inputSchema: t.parameters,
    annotations: { riskLevel: t.risk, title: t.description },
  }));
}

async function callTool(name, args) {
  const res = await bridgeRequest("GET", "/tools");
  const hit = (res.tools || []).find((t) => toolName(t) === name);
  if (!hit) throw new Error(`插件工具不存在或已卸载：${name}`);
  const out = await bridgeRequest("POST", "/call", { plugin: hit.plugin, name: hit.name, args });
  if (!out.ok) throw new Error(out.error || "插件工具执行失败");
  return out.result;
}

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

async function handle(req) {
  const { id, method, params } = req;
  const reply = (result) => { if (id !== undefined) send({ jsonrpc: "2.0", id, result }); };
  const fail = (code, message) => { if (id !== undefined) send({ jsonrpc: "2.0", id, error: { code, message } }); };
  try {
    switch (method) {
      case "initialize":
        return reply({
          protocolVersion: PROTOCOL_VERSION,
          capabilities: { tools: {} },
          serverInfo: { name: "chatcoder-plugins", version: "1.0.0" },
        });
      case "notifications/initialized":
        return;
      case "tools/list":
        return reply({ tools: await listTools() });
      case "tools/call": {
        const result = await callTool(params && params.name, (params && params.arguments) || {});
        const text = result && typeof result.text === "string" ? result.text : JSON.stringify(result ?? null);
        return reply({ content: [{ type: "text", text }], isError: false });
      }
      default:
        return fail(-32601, `不支持的方法：${method}`);
    }
  } catch (e) {
    if (method === "tools/call") {
      return reply({ content: [{ type: "text", text: String((e && e.message) || e) }], isError: true });
    }
    return fail(-32000, String((e && e.message) || e));
  }
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  const text = line.trim();
  if (!text) return;
  let req;
  try {
    req = JSON.parse(text);
  } catch {
    send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "解析失败" } });
    return;
  }
  void handle(req);
});
