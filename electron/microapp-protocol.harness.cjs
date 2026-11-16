"use strict";
/** plan-20261116-review-fixes：隐藏窗口加载真实 app:// handler，验证 vendor 模块、样式与断网策略。 */
const { app, BrowserWindow, protocol, session } = require("electron");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "cc-review-protocol-"));
const source = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
const from = source.indexOf("const MICROAPP_CSP =");
const to = source.indexOf("async function startBackend()", from);
assert.ok(from >= 0 && to > from);
const register = new Function("fs", "path", "protocol", "session", "microApp", "log", "logErr", "__dirname",
  source.slice(from, to) + "\nreturn registerMicroAppProtocol;")(
  fs, path, protocol, session, { resolveMicroAppRoot: () => root }, () => {}, console.error, __dirname,
);

function write(relative, content) {
  const target = path.join(root, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
}
write(".vendor/demo/1.0.0/library.js", "export const answer = 42;");
write(".vendor/demo/1.0.0/font.woff2", fs.readFileSync(path.join(__dirname, "../client/node_modules/@fontsource-variable/inter/files/inter-latin-wght-normal.woff2")));
write(".vendor/demo/1.0.0/style.css", "@font-face { font-family: ReviewVendor; src: url('font.woff2'); } #probe { color: rgb(1, 2, 3); font-family: ReviewVendor; }");
write("demo-app/1.0.0/app/index.html", '<link rel="stylesheet" href="app://vendor/demo/1.0.0/style.css"><div id="probe">验证</div><script type="module" src="main.js"></script>');
write("demo-app/1.0.0/app/main.js", `
import { answer } from 'app://vendor/demo/1.0.0/library.js';
const blocked = await fetch('https://example.com/').then(() => false, () => true);
const fonts = await document.fonts.load('16px ReviewVendor');
window.reviewResult = { answer, blocked, font: fonts[0]?.status, color: getComputedStyle(document.getElementById('probe')).color };
`);

let timer;
app.whenReady().then(async () => {
  register();
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, partition: "persist:microapp", backgroundThrottling: false } });
  win.webContents.on("console-message", (event) => console.error("页面控制台：", event.message));
  timer = setTimeout(() => { console.error("微应用协议验收超时"); app.exit(1); }, 10000);
  await win.loadURL("app://microapp/demo-app/1.0.0/app/index.html");
  const result = await win.webContents.executeJavaScript(`new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = () => window.reviewResult ? resolve(window.reviewResult) : Date.now() - started > 3000 ? reject(new Error('页面脚本未完成')) : setTimeout(poll, 20);
    poll();
  })`);
  assert.deepEqual(result, { answer: 42, blocked: true, font: "loaded", color: "rgb(1, 2, 3)" });
  console.log("通过：真实 Electron 微应用 vendor 模块、样式与字体加载成功，外网连接被 CSP 拦截");
  clearTimeout(timer);
  win.destroy();
  app.exit(0);
}).catch((error) => { console.error(error); clearTimeout(timer); app.exit(1); });

app.on("quit", () => {
  assert.ok(path.resolve(root).startsWith(path.resolve(os.tmpdir()) + path.sep));
  fs.rmSync(root, { recursive: true, force: true });
});
