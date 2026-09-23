# chatcoder 内置浏览器准备脚本：把 Playwright Chromium 下载到 server/vendor/ms-playwright
# 该目录由 chatcoder-server.spec 作为 datas 打进后端产物（_internal/ms-playwright），
# 运行时 app/core/browser_env.py 通过 PLAYWRIGHT_BROWSERS_PATH 指向它，
# 使浏览器工具开箱可用，用户无需手动执行 playwright install chromium。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File server/prepare-playwright-browsers.ps1
#   powershell -ExecutionPolicy Bypass -File server/prepare-playwright-browsers.ps1 -Proxy http://127.0.0.1:7897
param(
    [string]$Proxy = ""
)
$ErrorActionPreference = 'Stop'
$serverDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$staging = Join-Path $serverDir "vendor\ms-playwright"
$playwright = Join-Path $serverDir ".venv\Scripts\playwright.exe"
if (-not (Test-Path $playwright)) { throw "未找到 $playwright，请先在 server/.venv 安装 playwright" }

New-Item -ItemType Directory -Force -Path $staging | Out-Null
# 浏览器装到仓库内的 staging 目录，而不是 %LOCALAPPDATA%，保证可被打包
$env:PLAYWRIGHT_BROWSERS_PATH = $staging
if ($Proxy) { $env:HTTPS_PROXY = $Proxy; $env:HTTP_PROXY = $Proxy }

Write-Host "=== 准备内置 Chromium -> $staging ===" -ForegroundColor Cyan
# --no-shell: 只装完整版 Chromium（内置版无头模式通过 channel=chromium 复用完整版），
# 不装额外的 chromium-headless-shell（约 270MB），安装包体积少一半。
# playwright install 自带幂等：已装好则跳过下载。
& $playwright install --no-shell chromium
if ($LASTEXITCODE -ne 0) {
    throw "playwright install 失败（可加 -Proxy http://127.0.0.1:7897 走代理重试）"
}

# 历史遗留清理：早期脚本装过 headless shell，产物中不需要，避免被打进安装包
Get-ChildItem $staging -Directory -Filter "chromium_headless_shell-*" | ForEach-Object {
    Write-Host "清理无需内置的 $($_.Name)" -ForegroundColor DarkGray
    Remove-Item $_.FullName -Recurse -Force
}

# 守门：staging 必须含带 INSTALLATION_COMPLETE 标记的完整 chromium
# （缺该标记时 Playwright 会视作未安装并再次要求 playwright install）
$chromium = Get-ChildItem $staging -Directory -Filter "chromium-*" |
    Where-Object { Test-Path (Join-Path $_.FullName "INSTALLATION_COMPLETE") } |
    Select-Object -First 1
if (-not $chromium) { throw "准备失败：$staging 下没有完整可用的 chromium 目录" }
$size = (Get-ChildItem $chromium.FullName -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("内置 Chromium 就绪: {0} ({1:N0} MB)" -f $chromium.Name, ($size / 1MB)) -ForegroundColor Green
