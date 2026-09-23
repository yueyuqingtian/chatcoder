# chatcoder 一键打包脚本(Windows)
# 产物:v7/chatcoder-Setup-<version>.exe（electron-builder 输出目录见 package.json build.directories.output）
# 用法:powershell -ExecutionPolicy Bypass -File build-release.ps1 [-Publish]
#   -Publish: 打包后自动创建 GitHub Release 并上传产物（需 gh CLI 已登录，见 README 发布章节）
param(
    [switch]$Publish
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "=== [1/6] 准备内置 Chromium（浏览器工具开箱可用）===" -ForegroundColor Cyan
# 幂等：已准备好则跳过下载；浏览器由 chatcoder-server.spec 打进 _internal/ms-playwright
& "$root\server\prepare-playwright-browsers.ps1"

Write-Host "=== [2/6] 构建前端 ===" -ForegroundColor Cyan
Push-Location "$root\client"
npm run build
if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
Pop-Location

Write-Host "=== [3/6] 打包后端(PyInstaller, --clean 全量重建) ===" -ForegroundColor Cyan
Push-Location "$root\server"
$pyi = ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) { throw "未找到 PyInstaller,请先 .venv\Scripts\pip install pyinstaller" }
# --clean 必须保留：增量构建在模块列表不变时不会重建 PYZ，会把旧字节码打进 exe
& $pyi chatcoder-server.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "后端打包失败" }
& $pyi chatcoder-index-worker.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "索引 worker 打包失败" }
Pop-Location

# 产物守门：dist 里必须存在刚生成的主后端与索引 worker
$serverExe = "$root\server\dist\chatcoder-server\chatcoder-server.exe"
$workerExe = "$root\server\dist\chatcoder-index-worker\chatcoder-index-worker.exe"
if (-not (Test-Path $serverExe)) { throw "打包产物缺失: $serverExe" }
if (-not (Test-Path $workerExe)) { throw "索引 worker 产物缺失: $workerExe" }
$serverExeItem = Get-Item $serverExe
Write-Host ("后端产物: {0} ({1:N1} MB, {2})" -f $serverExe, ($serverExeItem.Length/1MB), $serverExeItem.LastWriteTime) -ForegroundColor Yellow

# 产物守门：内置 Chromium 必须随包分发，否则用户端浏览器工具会报"未安装"
$bundledRoot = "$root\server\dist\chatcoder-server\_internal\ms-playwright"
$bundled = Get-ChildItem $bundledRoot -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "INSTALLATION_COMPLETE") } |
    Select-Object -First 1
if (-not $bundled) { throw "后端产物缺少内置 Chromium（$bundledRoot），用户端浏览器工具将不可用" }
if (-not (Test-Path (Join-Path $bundled.FullName "chrome-win64\chrome.exe"))) {
    throw "内置 Chromium 缺少 chrome.exe: $($bundled.FullName)"
}
Write-Host ("内置浏览器: {0}" -f $bundled.Name) -ForegroundColor Yellow

Write-Host "=== [4/6] 部署后端到运行目录 ===" -ForegroundColor Cyan
# 同步产物到目标目录，不重启任何正在运行的进程
& "$root\deploy-server.ps1"

Write-Host "=== [5/6] 打包桌面应用(electron-builder) ===" -ForegroundColor Cyan
& npx electron-builder --win
if ($LASTEXITCODE -ne 0) { throw "electron-builder 打包失败" }

Write-Host "=== [6/6] 完成 ===" -ForegroundColor Green
Get-ChildItem "$root\v7\*.exe" | ForEach-Object {
    Write-Host ("产物: " + $_.Name + " (" + [math]::Round($_.Length/1MB,1) + " MB)") -ForegroundColor Yellow
}

# ── 发布到 GitHub Releases（自动更新源：latest.yml + exe + blockmap）──
if ($Publish) {
    $version = (Get-Content "$root\package.json" | ConvertFrom-Json).version
    $tag = "v$version"
    Write-Host "=== 发布 $tag 到 GitHub Releases ===" -ForegroundColor Cyan
    # electron-updater 检查的是 latest 这个 tag 的动态链接，tag 名不影响检查；
    # 资产名必须与 latest.yml 中 url 一致（package.json nsis.artifactName 已保证无空格）。
    gh release create $tag "$root\v7\chatcoder-Setup-$version.exe" "$root\v7\latest.yml" "$root\v7\chatcoder-Setup-$version.exe.blockmap" --title $tag --notes "ChatCoder $tag"
    if ($LASTEXITCODE -ne 0) { throw "gh release create 失败" }
    Write-Host "发布完成: https://github.com/yueyuqingtian/chatcoder/releases/tag/$tag" -ForegroundColor Green
}
