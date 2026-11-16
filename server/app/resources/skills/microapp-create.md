# 微应用创建

用这套协议为当前任务造一个「专用界面 + 标准方法面」的微应用，让用户看得见、点得动，而不是只读一段 JSON。

## 何时该造微应用

- 用户要看**结构**（流程图、时序、关系图、看板、表格）而不是一屏文字；
- 需要**反复调整**某个可视化结果（拖节点、改参数、看 diff）；
- 原生工具表达不了（画布、图表、自定义表单）。

不该造的时候：一次性的文本输出、命令行能直接给出结果、只读一个文件。

## 目录与文件

一个微应用就是一个目录，落在微应用库里：

```
<appId>/<version>/
├── manifest.json        # 必须：方法面与元信息
├── app/
│   ├── index.html       # 入口（manifest.entry 指向它）
│   ├── app.js           # 你的逻辑
│   └── app.css          # 你的样式
└── assets/              # 图片、字体等（可选）
```

`appId` 只能是 `^[a-z0-9][a-z0-9._-]{1,62}$`（小写字母/数字开头，允许点、下划线、连字符）。

## manifest.json

```json
{
  "appId": "pipeline-canvas",
  "name": "管线画布",
  "version": "1.0.0",
  "description": "把管线协议画成时序图，支持逐步高亮",
  "entry": "app/index.html",
  "methods": [
    {
      "name": "drawTimeline",
      "description": "按步骤数组绘制时序图",
      "params": {
        "steps": { "type": "array", "description": "步骤列表，每项含 label 与 detail" }
      }
    },
    {
      "name": "highlight",
      "description": "高亮某一步",
      "params": { "index": { "type": "integer" } }
    },
    {
      "name": "exportPng",
      "description": "导出当前画布为 PNG",
      "params": {}
    }
  ],
  "permissions": [],
  "runtime": { "vendors": [] }
}
```

- `methods[].name` 就是 AI 之后能调的方法名，**声明了才允许调用**，没声明会返回 `NOT_FOUND`。
- `params` 是给 AI 看的参数说明（JSON Schema 子集即可，不必写满）。
- `permissions` 是**提示性声明**：宿主按维护者决策完全放行，不做运行时拦截，空数组也能用宿主能力。写清楚用途即可（给人看）。
- `runtime.vendors` 只有在用第三方库时才写。

## 界面规范（必须遵守）

1. **单页、无构建**：`index.html` 直接引 `app.js` / `app.css`，不引入打包器，不写 TypeScript。
2. **禁止任何外链**：不能引 CDN、不能 `fetch` 外部地址。页面跑在 `app://` 协议下，CSP 是 `connect-src 'none'`，任何网络请求都会被拦掉。
3. **第三方库本地化**：要用 antd / element-plus 等，走宿主共享库 `app://vendor/<name>/<version>/<file>`，并在 `manifest.runtime.vendors` 里声明。没有对应 vendor 时用原生 DOM 或自己写小样式，**不要**试图从网络加载。
4. **样式**：允许内联 `style`、允许 `<style>` 标签（CSP 保留 `style-src 'unsafe-inline'`）。颜色、圆角、间距尽量贴近宿主：圆角用 8px / 12px，字体 13px 上下，主色克制。
5. **禁止**：左侧装饰细竖线、彩色边框条、满屏渐变色块这类"AI 味"装饰；层次用留白与字号字重表达。
6. **必须可空**：没有数据时给一个清楚的空态（一句话说明"调用某个方法后会出现在这里"）。

## 最小骨架

`app/index.html`：

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <link rel="stylesheet" href="app.css" />
</head>
<body>
  <div id="root"><p class="empty">调用 drawTimeline 后，时序图会出现在这里。</p></div>
  <script src="app.js"></script>
</body>
</html>
```

`app/app.js`——方法注册（宿主调用入口）：

```js
// 宿主通过这个对象调用 manifest 里声明的方法。
// 每个方法返回任意可 JSON 序列化的值，会原样回给 AI。
const methods = {
  drawTimeline({ steps }) {
    const root = document.getElementById("root");
    root.innerHTML = "";
    (steps || []).forEach((s, i) => {
      const row = document.createElement("div");
      row.className = "step";
      row.dataset.index = String(i);
      row.innerHTML = `<b>${i + 1}</b><span>${s.label ?? ""}</span><small>${s.detail ?? ""}</small>`;
      root.appendChild(row);
    });
    return { ok: true, count: (steps || []).length };
  },
  highlight({ index }) {
    document.querySelectorAll(".step").forEach((el) => el.classList.remove("is-on"));
    document.querySelector(`.step[data-index="${index}"]`)?.classList.add("is-on");
    return { ok: true };
  },
  exportPng() {
    // 真实实现可用 canvas；这里返回占位，避免假装成功
    return { ok: false, reason: "尚未实现导出" };
  },
};

window.__MICROAPP_METHODS__ = methods;
```

`app/app.css`：

```css
:root { color-scheme: light dark; }
body { margin: 0; padding: 16px; font: 13px/1.6 system-ui, "Microsoft YaHei", sans-serif; }
.empty { color: #888; }
.step { display: flex; gap: 10px; align-items: baseline; padding: 8px 10px; border-radius: 8px; }
.step.is-on { background: rgba(120, 140, 255, 0.14); }
.step b { color: #6b7cff; }
```

## 主动操作宿主（window.microapp）

上面的骨架只写了「AI 调页面」这一个方向。如果界面需要**用户点一下就真跑起来**（执行命令、读文件、实时看日志），必须用宿主桥；靠 `__MICROAPP_METHODS__` 做不到——那个方向只有宿主能调页面。

```js
// 起一条命令：立即返回，输出走流、不阻塞
const { streamId } = await window.microapp.invoke("exec.start", { command: "java -version" });

// 订阅流式输出（宿主按 100ms 或 200 行一批推）
const off = window.microapp.on("stream.log", ({ streamId: id, lines }) => {
  if (id !== streamId) return;
  appendLog(lines.join("\n"));
});
window.microapp.on("stream.state", ({ streamId: id, state, code }) => {
  if (id !== streamId) return;
  if (state === "exited") { appendLog(`\n[退出码 ${code}]`); off(); }
});

// 读工作区文件
const { content } = await window.microapp.invoke("fs.read", { path: "src/main/java/App.java" });
```

**可用 op**（协议 v3）：`exec.start` / `exec.stdin` / `exec.stop` / `exec.status` /
`fs.read` / `fs.list` / `workspace.info` / `host.capabilities`。

**不要手抄这份清单来判断能力**：启动时调一次 `host.capabilities`，按它的 `ops` / `features`
决定显示哪些控件——老宿主可能没有 `exec.stdin`，新版可能又多了 op：

```js
const cap = await window.microapp.invoke("host.capabilities", {});
// { protocol: 3, ops: [...], features: { stdin: true, env: false, net: false },
//   limits: { maxConcurrentExec: null, maxOutputCharsPerCall: 4000 } }
if (!cap.features.stdin) {
  // 把「交互输入」置灰并写明原因，而不是给一个点了没反应的按钮
  setInputDisabled("当前宿主版本不支持向进程输入");
}
```

**约定**

- `invoke` 返回 Promise；失败时 `error.message` 以 `microapp-host-` 开头，按前缀给用户看得懂的提示，不要把原始英文错误直接喷到界面上。
- `streamId` 就是后端后台命令的 id（`bg_` 前缀），**原样用**，不要做前缀或格式校验。
- `stream.log` 的 `stream` 字段恒为 `stdout`（后端把 stderr 合并了），按单流渲染即可。
- `exec.start` 的 `env` 目前不生效（返回里会给 `warnings`），不要依赖它。
- 页面起的命令**不会**唤醒 AI 会话，这是有意行为，不要以此判断失败。
- `exec.start` 起的进程**保留 stdin**：可以用 `exec.stdin` 往里写数据、用 `eof: true` 关流。
  AI 那边的后台命令没有这个管道，别指望页面能写进去。
- `exec.stdin` 的 `written` 是**字符数**，不是字节数；宿主不会替你加换行，`\n` 自己拼。
- 进程已退出 / 想写一个不是这里起的 stream 时，`exec.stdin` 报
  `microapp-host-stdin-closed` / `microapp-host-stream-unknown`：置灰输入框，不要重试。
- `window.microapp` 可能不存在（旧版宿主）：用 `if (!window.microapp)` 做降级提示，别白屏。
- **CSP 仍然封网**：`window.microapp` 是唯一的对外通道，`fetch` 外部地址依旧会被拦。

**两种能力怎么选**：AI 要来操控界面 → 写 `__MICROAPP_METHODS__`；用户要直接操作并看见真实结果 → 用 `window.microapp`。多数好用的微应用两者都有。

## 能力边界（生成微应用前先按这张表核对）

**写界面之前先把要做的功能逐条对一遍这张表**，再决定界面长什么样。尤其这两条——**页面不能直连端口**、**页面不能联网**——不是风格建议，而是宿主 CSP 的硬边界（`connect-src 'none'`），照错设计出来的按钮，用户点开就是报错或白屏。

| 想做的事 | 能否 | 正确做法 |
|---|---|---|
| 跑一条命令、看输出 | ✅ | `exec.start` + 订阅 `stream.log` |
| 交互式命令行（jdb / psql / REPL） | ✅（协议 v3） | `exec.start` + `exec.stdin`（见下一节） |
| JVM 断点 / 步进 / 变量 | ✅ 间接 | 页面自带一个 JDI 控制器进程，用 `exec.stdin` 驱动它 |
| 页面直接连 JDWP 端口 | ❌ | CSP 封网；只能走上面的间接路径（控制器进程自己连） |
| 页面联网 / 拉 CDN | ❌ | 只用 `window.microapp`；第三方库走 `app://vendor/...` |
| 页面读文件 | ⚠️ 限工作区 | `fs.read` / `fs.list`，越界返回 `microapp-host-fs-outside-workspace` |
| 页面写文件 | ⚠️ 当前无写 op | 用 `exec.start` 起命令间接写，或另提需求 |
| 页面起子进程 | ❌ | 必须经 `exec.start`（否则无法审计、无法终止） |
| 页面感知 AI 调用了自己 | ⚠️ 无事件 | 在 `__MICROAPP_METHODS__` 的实现体里顺手更新 UI |

## 交互式进程（协议 v3：`exec.stdin`）

需要「用户点一下、进程答一句」的界面（调试步进、REPL、数据库终端）时用这个模式：页面起一个**自己写的控制器进程**，两边用 **JSON 行协议**对话——页面往 stdin 写一行 JSON，从 `stream.log` 按行读回 JSON。宿主只负责转字节，不理解你的协议。

```js
// 1) 起控制器进程。连接性由它自己负责（例如 JDI 去连 :5005），页面不碰端口
const { streamId } = await window.microapp.invoke("exec.start", {
  command: "java -cp . DebugController",
});

// 2) 按行读回：宿主按 100ms / 200 行聚批推，lines 已经是整行（不含 \n）
window.microapp.on("stream.log", ({ streamId: id, lines }) => {
  if (id !== streamId) return;
  for (const line of lines) {
    if (!line.trim()) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { appendRaw(line); continue; } // 控制器自己打的普通日志
    if (msg.event === "paused") renderPaused(msg.line, msg.frames ?? []);
  }
});

// 3) 发一行 JSON（换行自己加，宿主不会替你补）
async function send(cmd) {
  try {
    await window.microapp.invoke("exec.stdin", {
      streamId,
      data: JSON.stringify(cmd) + "\n",
    });
  } catch (err) {
    const msg = String(err?.message ?? "");
    if (msg.startsWith("microapp-host-stdin-closed")) setInputDisabled(true); // 进程已退出
    else if (msg.startsWith("microapp-host-stream-unknown")) showExpired();     // 流不属于本会话
    else report(err);
  }
}
await send({ cmd: "stepOver" });

// 4) 结束输入：关掉 stdin，控制器读到 EOF 自然退出（比直接 kill 干净）
await window.microapp.invoke("exec.stdin", { streamId, eof: true });
```

要点：**每行一个完整 JSON**、自己拼 `\n`、`stream.log` 里混着进程日志时用 `try/catch JSON.parse` 兜住、进程退出后别继续写。`streamId` 只在**起它的那个页面**里用，不要跨页面传递。

## 三种常见形态

- **画布式**：`<canvas>` 或 SVG，方法面是 `draw*` / `moveNode` / `exportPng`。
- **列表式**：表格或卡片列表，方法面是 `setRows` / `sortBy` / `filter`。
- **表单式**：输入控件，方法面是 `fill` / `read` / `validate`。

## 交付前自检

- [ ] `manifest.json` 可解析，`appId` 符合正则；
- [ ] `entry` 指向的文件真实存在；
- [ ] `methods` 非空，每个方法在 `app.js` 里都有对应实现，且注册进 `window.__MICROAPP_METHODS__`；
- [ ] 界面里需要「点一下真跑」的按钮走的是 `window.microapp.invoke`（不是假进度条或写死的日志），且对 `window.microapp` 缺失做了降级；
- [ ] **已按「能力边界」表逐条核对**：没有试图直连端口 / 联网 / 自己起子进程；交互式输入走的是 `exec.stdin`；
- [ ] 需要在启动时按 `host.capabilities` 判能力的地方，`features.stdin === false` 时有置灰说明（不是死按钮）；
- [ ] 没有任何外链（`http://` / `https://` / `//cdn`）；
- [ ] 用到第三方库时已在 `runtime.vendors` 声明，且路径是 `app://vendor/...`；
- [ ] 有清楚的空态；
- [ ] 调一次 `microapp_validate` 全部通过。

违反以上任一条，用户点开就是白屏或报错——宁可少一个方法，也不要交付跑不起来的界面。

## 交付话术

创建完成后告诉用户：应用名、版本、可用的方法列表、体积、用到的第三方库，并给两个动作：「放进输入框」和「去拓展里看」。
