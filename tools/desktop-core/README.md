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

## 产物部署

`server/chatcoder-server.spec` 会把 `server/vendor/desktop-core/` 作为 `datas` 打进安装包，
运行时 `server/app/core/desktop_env.py` 负责定位：
- 源码运行：`tools/desktop-core/bin/desktop-core.exe`
- 打包运行：`<_MEIPASS>/desktop-core/desktop-core.exe`

与内置 Chromium 的定位逻辑同构（见 `server/app/core/browser_env.py`）。
