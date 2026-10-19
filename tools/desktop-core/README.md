# 编译 desktop-core（无需安装 .NET SDK）

用 Windows 自带的 .NET Framework `csc.exe` 编译，因此在一台干净的 Windows 上也能构建。
产物为 `bin/desktop-core.exe`（单文件，无外部依赖）。

## 用法

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1
```

可选参数：

```powershell
# 指定输出目录（默认 bin/）
powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -OutDir ..\..\server\vendor\desktop-core

# 只做语法检查，不产出可执行文件
powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -CheckOnly
```

## 为什么用 csc 而不是 dotnet build

1. .NET Framework 4.x 的 `csc.exe` 随系统提供（`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\`），
   不需要用户装 SDK，也不需要 NuGet 还原。
2. UI Automation 托管程序集（`UIAutomationClient.dll` / `UIAutomationTypes.dll` / `WindowsBase.dll`）
   位于 `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\WPF\`，可直接引用。
3. 冷启动实测约 30 ms（对比 Node 100 ms、PowerShell 5.1 193 ms），适合作为常驻内核。

源码刻意限制在 C# 5 语法范围内（不使用 `var` 之外的现代特性、不使用表达式体成员），
以便该编译器可以直接编译。

## 坐标与缩放（为什么点得准）

内核**全程使用物理像素**，与系统缩放比例（100% / 110% / 125% / 150% / 200%）和分辨率无关：

- **DPI 感知**：`src/app.manifest` 把进程声明为 Per-Monitor-V2（进程创建时即生效，比启动后调 API 可靠；
  `Program.Main` 里的 API 调用只是兜底）。运行时用 `GetThreadDpiAwarenessContext` 读回真实状态，
  写进 ready 行与 `info`（`dpiAwareness`），不是 PerMonitorV2 时后端会在 `desktop_windows` 里告警。
  感知级别不对时系统会按缩放比虚拟化坐标——截图、窗口矩形、点击各偏各的，且没有任何报错，所以必须能被检测出来。
- **落点读回**：`Input.MoveTo` 发出 SendInput 绝对坐标后读回光标位置，不一致就用 `SetCursorPos` 校正，
  偏差随响应上报（`cursor` / `landErr`）；按下前 `EnsureAt` 再确认一次（用户碰鼠标也不会点歪）。
  归一化用 `ceil(x * 65536 / 宽)`（系统反算口径），按键事件不再带 ABSOLUTE 标志。
- **`info` / `calib`**：`info` 给出各显示器的物理矩形、有效 DPI 与缩放百分比；`calib` 把光标依次移到 5 个点并读回，
  设置页「检测」按钮据此显示「光标落点自检」。

坐标的两条路（后端 `app/orchestration/tools/desktop.py`）：

1. **按元素**（首选）：`snapshot` 取无障碍树里屏幕内可见的元素（含 Chrome / Edge / Electron 的网页内容），
   矩形就是物理像素，点中心即可——不经过任何换算与估算；
2. **按坐标**（自绘界面兜底）：截图缩放到模型可见尺寸，坐标由 `app/core/desktop_coords.py` 换算回物理像素。

### 网页元素（Chromium 系窗口）

Chromium 的 UIA 网页树是惰性的：窗口首次被 UIA 客户端访问后才开始构建。`snapshot` 对这类窗口先预热
（`warmupMs`，`focus` 时也会后台触发一次），再用 `FindAll(Descendants, IsOffscreen == false)` + `CacheRequest`
一次取回**屏幕内可见**的节点——Chromium 在服务端按条件过滤，实测 7000 节点的页面只回传 168 个、125 ms
（无条件 FindAll 要 1.3 s，逐节点遍历要 2 s）。不需要 `--remote-debugging-port`；失败或超时回退到旧的 TreeWalker 遍历。
副作用：被读取的浏览器 / Electron 进程会一直保持无障碍模式（到进程结束），略增内存。

### 精度实测

```powershell
server\.venv\Scripts\python.exe tools\desktop-core\test\calibrate-e2e.py            # 跟随系统缩放
server\.venv\Scripts\python.exe tools\desktop-core\test\calibrate-e2e.py --scale 1.25   # 模拟页面 125% 缩放
```

打开带已知元素位置的测试页（临时 Chrome 配置），按元素 / 按坐标（像素与 0–1000 两种口径）各点一遍，
读回页面实际收到的点击坐标，打印偏差（物理像素），超过阈值退出码为 1。运行约 20 秒，期间不要动鼠标。

## 产物部署

`server/chatcoder-server.spec` 会把 `server/vendor/desktop-core/` 作为 `datas` 打进安装包，
运行时 `server/app/core/desktop_env.py` 负责定位：
- 源码运行：`tools/desktop-core/bin/desktop-core.exe`
- 打包运行：`<_MEIPASS>/desktop-core/desktop-core.exe`

与内置 Chromium 的定位逻辑同构（见 `server/app/core/browser_env.py`）。
