# 交接：内置浏览器操作提速与点击准确性（2026-10-15）

> 计划（方案 / 分阶段 / 验收 / 待拍板）：[browser-perf-plan.md](browser-perf-plan.md)，编号 `plan-20261015-browser-perf`。
> 测试脚手架与基线数据：[browser-perf-e2e/](browser-perf-e2e/)。
> `ai/` 目录按 AGENTS.md 约定不入库，所以计划与交接放在 `docs/handoff/`。开工时把计划复制为本机的 `ai/chatcoder-plan-20261015-browser-perf.md` 作为当期计划文档（发版时 CHANGELOG 要与它的验收项逐条对照）。

## 0. 现状一句话

**计划阶段 1–5 已全部实施并验证（2026-10-15 第二次提交），结果见 §11。** §七 取舍按计划默认项执行：
不加 `CalculateNativeWinOcclusion` 开关（改为截图那一刻临时放开主窗口节流，实测最小化时可截）；
自动截图只在报错 / 新对话框时附带，其余靠 `screenshot: true`；歧义文字与旧 ref 一律报错列候选。
下文 §2–§4 的代码行号以改造前的提交 `40c70ec` 为准。

## 1. 用户原始诉求（原话）

> 本地已经完成了内置浏览器的交互开发，我实际测试功能上没有什么问题，但是有一些使用缺陷：
> 1. 操作缓慢，需要进一步降低每次操作的延迟；
> 2. 截图延迟，比如点击某个按钮出现报错后，ai 截图不及时导致截图的时候报错已经消失了，需要我让他检查接口响应它才查到报错；
> 3. 某些操作不准确，存在误点击，点击错位置的情况；
>
> 参照我的这些问题点，对内置浏览器操作进行功能优化，保证操作，点击准确性，重点优化操作速度和操作准确性。

## 2. 链路速览（改代码前先看）

```
模型 → tools/browser.py（17 个工具 schema）→ browser/driver.py（动作编排、结果文案）
     → bridge.py（/ws/global：browser.request / browser.response）
     → 前端 client/src/browserAgent/host.ts（标签路由、可视化、焦点保护、settle 轮询）
     → IPC → electron/browser-agent.cjs（主进程：截图、CDP 网络/弹窗、关 guest 节流）
     → 右侧面板 <webview>；页面内感知与定位统一用 browser/kit.py（注入页面的 JS，两种宿主共用）
无桌面端连接时：driver → playwright_host.py（后台 Chromium，原语与桌面端一一对应）
```

关键位置（文件:行，@40c70ec）：

| 位置 | 说明 |
|---|---|
| `client/src/browserAgent/host.ts:40` | `sleep()` 用 `window.setTimeout`——窗口被遮挡时被节流（R1） |
| `host.ts:249-263` | `settleAfter`：先 `waitNavigationSettled`、再 40 ms 轮询 DOM 静默 120 ms（R2/R7） |
| `host.ts:266-281` | `waitCondition` 轮询（同样受节流） |
| `host.ts:317-355` | `dispatchMouse`：坐标直接当 DIP 派发，未乘缩放（R10） |
| `host.ts:496-539` | mouse/keys/text 分支：`aiWindow`(501) → `watch`(503) → 礼让(506) → 光标 `sleep(150)`(509) → 派发 → `finally sleep(30)`(517) → settle(537)（R3） |
| `host.ts:371-409` | `navigateTab`：等 `loadURL` 完成（= 完整 load），`a.wait` 未使用（R4） |
| `client/src/browserAgent/frames.ts:138-150` | `waitNavigationSettled`：无跳转也至少等 120 ms，`window.setTimeout(40)` 轮询 |
| `electron/browser-agent.cjs:54-87` | `onCdp`：只记请求开始/响应/失败，没有 `loadingFinished` 与时间戳（R7） |
| `browser-agent.cjs:121-169` | `capture`：`capturePage(rect, {stayHidden:true})`(157)，宿主隐藏时卡 15 s（R6） |
| `browser-agent.cjs:183-238` | `exec`：op 分派（新增 `sleep` / `netSince` 放这里，`sleep` 要在 `panelGuest` 检查之前） |
| `server/app/orchestration/browser/driver.py:46-50` | 各类超时常量 |
| `driver.py:154-168` | `_settle_lines`：结果文案只有跳转/DOM 变化数/对话框/焦点（R8） |
| `driver.py:241-247` | `_to_css`：只按截图比例换算，不校验滚动/视口/URL（R14） |
| `driver.py:250-277` | `navigate`：导航后再单独 `kit text` 取摘要 |
| `driver.py:351-387` | `screenshot`：只有视口截图更新 `shot_scale`，整页/元素截图后仍用旧比例（R14） |
| `driver.py:403-435` | `click`：resolve 与 mouse 两次往返；`settle_ms` 1500 |
| `driver.py:666-704` | `run_batch` |
| `server/app/orchestration/browser/kit.py:21` | `KIT_VERSION = 5`（改 kit 必须 +1，已打开的页面靠它重新注入） |
| `kit.py:128-137` | `refOf` / `byRef`：ref 绑定节点，无内容指纹（R12） |
| `kit.py:462-498` | `findByText` / `pick`：同分取第一个、选择器取第一个（R13） |
| `kit.py:509-547` | `resolve`：五点命中检测；接受「命中元素是目标祖先」含 body（R16）；坐标模式不吸附（R15）；`scrollIntoView({inline:'center'})`(527) |
| `kit.py:722-747` | MutationObserver 只计数，`idle_ms` 对任何变化都重置（R2/R8） |
| `server/app/orchestration/browser/playwright_host.py:227-253,388-444` | 兜底宿主的 settle / 输入前准备 / 鼠标 / 截图，新原语要在这里同步实现 |
| `server/app/orchestration/tools/browser.py:173-212,410-431` | 截图、点击、批量的 schema（新参数加在这里） |
| `server/app/orchestration/prompts/main.py:232-252` | `BROWSER_TASK_PROMPT` |
| `server/app/orchestration/agent_loop.py:2257-2267` | 只有 `result.ok` 时才把 `data.base64` 图片送进多模态对话——自动截图必须挂在成功结果上 |
| `client/src/components/panel/BrowserPanel.tsx:112-137` | `syncFrameSize`：`viewportRef` 为空时直接 return，停泊 webview 可能没尺寸（R17 待确认） |
| `electron/main.cjs:967-973` | 主窗口 webPreferences：未关 `backgroundThrottling`（R1 的背景） |
| `client/src/styles/browser.css:95-101` | `.bpa-cursor` 过渡 `--dur-3`（200 ms），与 host.ts 的 `sleep(150)` 配对 |

## 3. 已确认的问题与证据

证据来自 E2E 基准（真实 Electron 宿主链路，见 §6），实验页 `site/lab.html` 每个场景对应一类问题。

### 3.1 后台节流导致「慢」（R1）——影响最大

- 第二轮基线时测试窗口被其它窗口完全遮挡：页面工具包类动作（snapshot / get_text / scroll to top）只要 **4–6 ms**，凡是经过 host.ts `sleep()` 的动作全部量化到整秒：点击 / 悬停 ≈**2000 ms**、输入 / 按键 ≈**1000 ms**、方向滚动 ≈**2000 ms**。
- 裸 CDP 探测宿主页面：`visibilityState: "hidden"`、`hasFocus: false`。说明 Windows 原生遮挡检测把被遮住的窗口判为隐藏，宿主渲染进程定时器被节流。
- 用户场景：AI 长任务期间用户切去别的窗口时，每步浏览器操作会变成 1–2 秒，截图会超时（见 3.6）。
- 注意：Playwright 的 `connect_over_cdp` 接管页面会开启焦点模拟，页面随之变为 visible/focused，掩盖问题——探测请用 `probe_host.py`（裸 CDP）。单次 `setTimeout(40)` 可能恰好落在对齐点附近显得正常（实测一次 56 ms），要看链式多次等待。

### 3.2 报错一闪而过、模型看不到（R7/R8/R9）

| 场景（lab.html） | 页面行为 | 改造前结果 |
|---|---|---|
| 保存 | POST 300 ms 后返回 500 `{"msg":"名称不能为空"}`，弹 `role=alert` 提示 1.2 s 后移除 | 结果只有「已点击 按钮「保存」/ 页面内容有变化（2 处 DOM 更新）/ 焦点在 按钮「保存」」，**没有报错文字、没有 500、没有截图** |
| 提交订单 | 250 ms 后返回 200 `{"code":40001,"msg":"库存不足，无法下单"}`，常驻容器改 display 显示 1 s | 结果**没有「库存不足」** |
| 慢接口 | 450 ms 后更新 DOM | 报告了变化——但只是因为节流把 settle 拖到了 2 s；窗口可见时按代码推算（无跳转 120 ms + 静默 120 ms）会在请求返回前结束，写成「页面没有可见变化」，需在窗口可见时复测确认 |

### 3.3 缩放后点偏（R10）

- 主进程 `setZoom` 把实验页设为 125% 后，`click selector=#far-btn` 返回「已点击 按钮「远处按钮」」，页面实际**命中 0 次**。静默点错。
- 生产中缩放的来源：Electron 默认菜单的 Ctrl+= / Ctrl+-（`autoHideMenuBar` 下快捷键仍有效）在焦点位于 webview 时会改该站点缩放并按站点记忆；webview 未设 partition，与应用共用默认 session。

### 3.4 旧 ref 与歧义文字点错（R12/R13）

- snapshot 取「张三」行的「删除」ref → 点「下一页」（只换单元格文字、复用按钮节点）→ 用旧 ref 点击：**删掉了「李四」**，结果照常报成功。
- `click text=删除`（三行都有）：**直接删了第一行「张三」**，无任何提示。

### 3.5 动画菜单（R11）——未能在基线复现，需复测

- 场景：「打开菜单」后立即点「选项三」（菜单 500 ms 展开动画，CSS transition 不产生 DOM 变化）。第二轮 3/3 选对，但当时宿主被节流，每步多等了 1–2 s，动画早已结束，结果不可信。阶段 1 修好节流后要在窗口可见时复测，预期会出现选错/落空。

### 3.6 截图超时（R6）

- 两轮基线中，测试窗口被遮挡期间所有截图都卡满 15 s 后报「截图超时（页面未在渲染）」，有一次报 `UnknownVizError`。原计划文档记录的「隐藏时 capturePage(stayHidden) 7–12 ms」在本机这种「窗口被遮挡」状态下不成立。

### 3.7 首轮异常：停泊 webview 可能是 1×1 视口（R17，待确认）

- 首轮基线（测试窗口刚启动，右侧面板从未显示）：首次导航后所有点击与悬停报「不在可视区域内且无法滚入」、截图超时；之后通过 CDP 观察时 webview 已是 418×754（停泊容器 1×1、`is-parked`），第二轮正常。推测 `syncFrameSize` 在 `viewportRef` 为空时 return，webview 在一段时间内按 1×1 视口排版。需要在「面板从未展开」的干净启动下复现确认。

### 3.8 其它观察

- 窄视口（418 px）下 `resolve` 的 `scrollIntoView({inline:'center'})` 会把可横向滚动的页面整体横移（实测「打开菜单」按钮 left 从 35 变 −264），使上一张截图的坐标失效。计划 §三 3.3-7 已纳入改为 `'nearest'`。
- E2E 里会话 1 的浏览器标签处于停泊态（面板没在显示会话 1），所以测到的是「不可见」路径（无 150 ms 光标等待）；可见路径的延迟需另外让面板显示该会话的浏览器再测。

## 4. 基线数据（改造前，第二轮，测试窗口被遮挡）

原始数据：[baseline-20261015.json](browser-perf-e2e/baseline-20261015.json)。

| 动作 | 中位耗时 | 备注 |
|---|---|---|
| 导航（同页） | 36 ms | 页面极简，几乎无资源 |
| snapshot | 6 ms | |
| get_text | 4 ms | |
| scroll to=top | 4 ms | 纯 kit 调用 |
| click（ref / selector / 无变化） | ≈2000 ms | 被节流：`sleep(30)` + settle 轮询各对齐 1 s |
| hover | 2000 ms | 同上 |
| type（10 字） | 1004 ms | |
| press（End） | 997 ms | |
| scroll 方向滚动 | 2004 ms | |
| screenshot | 15016 ms（失败） | 截图超时 |

准确性：动画菜单 3/3（不可信，见 3.5）；保存/业务错误未回报；旧 ref 点错行；歧义文字点第一行；125% 缩放命中 0；「坐标偏出图标 5 px」「截图后滚动再按旧坐标点」两项因截图失败未执行，待复测。

## 5. 方案要点（详见计划 §三）

- **速度**：宿主等待改走主进程 `sleep`（不受节流）→ 截图在宿主隐藏时兜底出帧 → settle 改成「导航与静默并行 + 网络感知 + 噪声过滤」→ 准备步骤并行、光标等待缩短 → 导航等 DOM 就绪 → 结果直接附新弹出元素与视口元素，减少往返。
- **截图及时**：kit 在节点插入当下记录提示文字（消失也保留）→ 动作窗口内的接口错误（含业务码、响应体摘要）与控制台错误写进结果 → 错误时同一次调用内自动截图；动作支持 `screenshot: true`。
- **准确**：缩放换算 → 派发前 `aim` 校准（等动画稳定、复核命中）→ ref 行上下文指纹 → 文字歧义报错 → 截图坐标守卫（滚动补偿 / 视口或 URL 变化拒绝）→ 10 px 吸附 → 命中收紧 → `marks` 编号截图。
- 两种宿主（host.ts + browser-agent.cjs / playwright_host.py）必须同步实现新原语，见计划 §三 3.4；改 kit 必须递增 `KIT_VERSION`。

## 6. 如何复现与复测（E2E 基准脚手架）

脚手架复刻了生产链路，但**不用 electron/main.cjs**（它启动时会 taskkill 已安装应用的 chatcoder-server.exe），独立端口、不建库、不碰 12973 的数据。依赖：`server/.venv`（fastapi / uvicorn / websockets / playwright 已在其中）、仓库根 `node_modules/electron`。

| 文件 | 用途 |
|---|---|
| `server.py` | 隔离后端：`/ws/global` 宿主协议 + 真实 `driver.execute`（`/e2e/run`）+ 页面求值（`/e2e/eval_page`）+ 测试站点与三个接口（`/e2e-site/api/fail` 300 ms→500、`/api/biz` 250 ms→业务错误、`/api/slow` 450 ms） |
| `main.cjs` | 测试主进程：加载 `client/dist` + 真实 preload / browser-agent.cjs，开远程调试端口 |
| `site/index.html` | 原 E2E 页（点击 / 输入 / 表单 / 悬停 / 弹窗 / 控制台 / 网络 / 长页面） |
| `site/lab.html` | 准确性实验页（动画菜单、小图标按钮、远处按钮、失败接口提示、业务错误、慢接口、就地重渲染表格、滚动目标） |
| `bench.py` | 延迟基准（中位数）+ 准确性场景，输出 JSON |
| `probe_host.py` | 裸 CDP 探测宿主页面可见性与链式定时器间隔 |
| `baseline-20261015.json` | 改造前基线 |

步骤（Git Bash，仓库根目录；长驻进程放后台并保留日志；日志与结果写进 `tmp_browser_perf/`，该目录已被 .gitignore 忽略）：

```bash
npm --prefix client run build && mkdir -p tmp_browser_perf
```

```bash
server/.venv/Scripts/python.exe docs/handoff/browser-perf-e2e/server.py 22973 > tmp_browser_perf/server.log 2>&1 &
```

```bash
E2E_PORT=22973 E2E_CDP=9339 node_modules/.bin/electron docs/handoff/browser-perf-e2e/main.cjs > tmp_browser_perf/electron.log 2>&1 &
```

```bash
PYTHONIOENCODING=utf-8 server/.venv/Scripts/python.exe docs/handoff/browser-perf-e2e/bench.py 22973 tmp_browser_perf/after.json
```

```bash
server/.venv/Scripts/python.exe docs/handoff/browser-perf-e2e/probe_host.py
```

要点：
- host.ts 改动后必须重新 `npm --prefix client run build`（测试窗口加载的是 `client/dist`）；browser-agent.cjs 改动后要重启测试窗口；server/kit 改动后要重启 `server.py`。
- 「可见」与「后台」分开测：测可见时保证测试窗口没被完全遮挡；测后台时把它遮住或最小化，并用 `probe_host.py` 确认宿主 `hidden`。
- 跑完关掉两个进程（PowerShell：按命令行含 `browser-perf\main.cjs` 的 electron.exe 与 `server.py 22973` 的 python.exe 结束；不要误杀已安装应用的 chatcoder.exe / chatcoder-server.exe）。
- `bench.py` 里 125% 缩放通过 CDP 调主窗口的 `chatcoderAPI.browserAgentExec(wcId, 'setZoom', …)` 实现，结束时会改回 100%。

## 7. 测试与检查命令

```bash
server/.venv/Scripts/python.exe -m pytest server/tests/test_browser_agent.py server/tests/test_browser_kit_frames.py -q
```

```bash
npm --prefix client run typecheck
```

```bash
npm --prefix client run build
```

```bash
node --check electron/main.cjs && node --check electron/browser-agent.cjs && node --check electron/preload.cjs
```

- server 测试共用临时库，**不要并发跑两个 pytest**（会互相 drop 表）。
- `test_browser_kit_frames.py` 需要本机 Playwright Chromium，缺失时自动跳过；新增的真实浏览器测试同样用 async Playwright（pytest-asyncio auto 模式下 sync API 会直接报错）。
- 现有单测里 `_FakeHost` 按原语顺序断言（如 `ops == ["kit", "mouse"]`），改原语流程时要同步更新。

## 8. 约定与注意事项

- AGENTS.md：注释与文案简体中文，注释写「为什么」并引用 `plan-20261015-browser-perf`；不改无关模块；提交前过 typecheck / build / `node --check`。
- 可视化改动遵守设计语言：动效用 `--dur-*`，无装饰描边、无循环动画、固定槽位；marks 编号框只画在截图图片上，不改页面 DOM、不改面板视觉。
- 不要绕过 webview 安全设置：跨域 iframe 仍只用截图 + 坐标；网络/控制台数据只用于回报给模型，不外发。
- 兜底宿主（Playwright）必须与桌面端行为一致，否则同一个 ref / 文案在两条路径上含义不同。

## 9. 待用户决定（计划 §七）

1. 截图兜底不够时，是否加 `--disable-features=CalculateNativeWinOcclusion`（被遮挡时继续渲染，多耗 CPU）。
2. 自动截图的范围（仅错误/对话框 vs 更多），token 成本取舍。
3. 歧义文字 / 旧 ref：报错（准，但多一次往返）还是点击并显著提示（快）。

## 10. CHANGELOG 草稿（实现并验证后再写入 `package.json` 当前版本 0.8.2 区块，不新增版本号）

```markdown
### 优化
- 内置浏览器操作提速：动作后不再固定等待，窗口在后台时也不会变慢
- AI 点击后会等接口返回，报错提示与接口错误直接写进结果并附截图
- 操作结果直接列出新弹出的菜单、对话框里的按钮，减少反复读取页面
### 修复
- 修复网页缩放不是 100% 时 AI 点击位置偏移
- 修复下拉菜单展开动画期间点选项落空或点错
- 修复列表翻页后沿用旧元素点错行、文字有重名时点错元素
- 修复截图后页面滚动再按坐标点击点错位置
- 修复窗口被遮挡或最小化时截图超时
```

## 11. 实施结果（改造后，2026-10-15）

### 11.1 做了什么（按计划 §三 编号）

| 项 | 实现位置 |
|---|---|
| 3.1-1 宿主等待不受节流 | `browser-agent.cjs` 新 op `sleep`（在 guest 检查之前）；`frames.ts` `hostSleep`，host.ts / frames.ts 所有轮询改用它 |
| 3.1-2 截图兜底 | `browser-agent.cjs` `captureViewport`：主窗口处于节流时**临时** `setBackgroundThrottling(false)` 让它出帧，截完恢复；capturePage 2 s → CDP 2 s → 可读报错；最小化后刚失败过则直接报错不空等 |
| 3.1-3 稳定判定重写 | host.ts `settleAfter` 单循环 30 ms：导航与 DOM 静默并行；`netSince` 统计动作起点后的 XHR/Fetch 在途数；kit MutationObserver 按节点频次过滤噪声；最短 60 ms / 静默 100 ms；预算见 driver.py `_SETTLE_*` |
| 3.1-4 准备并行 | `aiWindow` 与 `watch` 并行；光标等待 150 → 100 ms 且与 `aim` 重叠；`sleep(30)` 去掉，焦点守卫覆盖整个稳定观察期；光标过渡 `--dur-2` |
| 3.1-5 导航 | host.ts `navigateTab`：DOM 就绪（frames.ts `domSeq`）+ `quietAfterNav`；`wait_until=load` 才等完整加载；driver 一次 kit `digest` 取摘要 + 视口内元素 |
| 3.1-6 / 3.2 结果增强 | kit `changes`：插入当下记录提示文字（`gone` 标记已消失）、新出现的可操作元素（≤20 项 / 1200 字）、新对话框；`netSince(bodies)`：HTTP ≥400、请求失败、200 业务错误码（附响应体摘要与 request_id）；控制台 error；有错误/新对话框时同一次调用内截图（`settle.shot`），动作参数 `screenshot: true` |
| 3.3-1 缩放换算 | host.ts `zoomOf` × 坐标（点击、拖拽、滚轮、可视化叠加层） |
| 3.3-2 点击前校准 | kit `aim`：等目标矩形连续两帧不变且祖先无运行中的有限动画（上限 800 ms），重算命中点；mouse 原语带 `ref` 时两种宿主都调用 |
| 3.3-3 旧 ref | kit `refOf` 记录所在行文字（tr/li/row/listitem/option/treeitem 或同 tag+class 兄弟），`byRefChecked` 用二元组相似度 < 0.5 报错；每次回报 ref 刷新 |
| 3.3-4 歧义 | kit `findByText` 同分候选（去嵌套、对话框优先，不计视口加分）> 1 报错列前 8 个；`selector` 多匹配在结果里说明 |
| 3.3-5 截图坐标守卫 | 截图返回 `scroll / viewport / page_url`；driver `_to_css` 带 `shot` 元信息，kit `shotToViewport` 补偿滚动、视口或地址变化拒绝、整页截图按文档坐标滚入；元素截图坐标拒绝；坐标超出视口直接报错 |
| 3.3-6 吸附 | kit `nearInteractive`：落点不在可交互元素上时 3/6/10 px 三圈采样，唯一或领先 ≥3 px 才吸附 |
| 3.3-7 命中收紧 | kit `hitPoint`：祖先命中仅限非 body/html 的可交互祖先；`scrollIntoView` 横向改 `nearest` |
| 3.3-8 marks | kit `marks` + host.ts `drawMarks`（OffscreenCanvas）/ playwright_host `_draw_marks`（PIL）；结果列「编号 → 元素 ref」 |
| 3.3-9 R17 | BrowserPanel `syncFrameSize`：视口容器未挂载时仍给停泊 webview 赋估算尺寸（防御性修复；首轮 1×1 现象本次未能复现，未写入 CHANGELOG） |
| 两种宿主同步 | playwright_host.py：`requestfinished` 记完成、`_settle` / `_net_errors` / `_biz_error` / `aim` / 截图元信息 / marks 与桌面端一致；`KIT_VERSION` 5 → 6 |

### 11.2 E2E 复测（真实 Electron 宿主链路）

原始数据：`browser-perf-e2e/after-20261015-visible.json`、`after-20261015-minimized.json`。本机 DPR 1.5、面板宽 418 px。

| 动作 | 改造前（被遮挡） | 改造后·窗口可见 | 改造后·窗口最小化（宿主 hidden，页面定时器 ≈1000 ms） |
|---|---|---|---|
| click（ref / selector / 无变化） | ≈2000 ms | 108–132 ms | 108–132 ms |
| hover | 2000 ms | 108 ms | 139 ms |
| type（10 字） | 1004 ms | 122 ms | 124 ms |
| press（End） | 997 ms | 122 ms | 138 ms |
| scroll 方向滚动 | 2004 ms | 108 ms | 125 ms |
| screenshot | 15016 ms 失败 | 13 ms | 26 ms（成功） |
| navigate（同页） | 36 ms | 153 ms（含 DOM 就绪后的接口/DOM 静默与视口元素摘要） | 165 ms |

准确性（两种窗口状态结果一致）：动画菜单 10/10 选中「选项三」；「保存」结果含提示文字 + HTTP 500 + 响应体 + 自动截图（≈460 ms）；「提交订单」含业务报错「库存不足」+ 截图；慢接口等到返回再报告变化；旧 ref 报「所在行已从张三…变为李四…」未点错；歧义「删除」报错列 3 个候选；坐标偏出图标 5 px 吸附命中；截图后滚动按补偿命中「目标乙」；125% 缩放命中「远处按钮」；marks 截图编号框正确。

单测与真实浏览器测试：`test_browser_agent.py`、`test_browser_kit_frames.py`、新增 `test_browser_kit_accuracy.py`（含经 Playwright 兜底宿主的端到端）共 38 项通过。真实浏览器测试在 Playwright 自带 Chromium 不可用时自动退回本机 Edge / Chrome。

### 11.3 脚手架变更与注意

- `main.cjs`：preload 用 `sendSync` 读的 `window:chrome-sync-get`、`ui-prefs:load` 必须用 `ipcMain.on` 应答，否则测试窗口渲染进程卡死、宿主不登记；`window:minimize` / `window:maximizeToggle` 接到真实窗口。
- 新增 `window_state.py minimize|restore`：测「后台」场景。
- `bench.py` 的「坐标偏出图标」先把图标横向滚入视口（窄面板下图标在视口外，原写法的坐标本身就在视口外）。
- `bench.py` 末尾的缩放步骤用 Playwright `connect_over_cdp` 接管页面；接管后紧接着再跑其它脚本时出现过 webview 不再接收输入事件，重启测试窗口即恢复——属测试工具干扰，生产链路不经过它。需要连续多轮测试时每轮重启测试窗口。

### 11.4 遗留

- 「窗口被其它窗口遮挡（非最小化）」本机无法稳定复现，只验证了最小化；截图兜底对遮挡同样生效（只要主窗口处于节流就临时放开），但未实测。
- R17 未复现，仅做防御性修复。
