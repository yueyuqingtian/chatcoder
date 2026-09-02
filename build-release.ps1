# chatcoder 一键打包脚本(Windows)
# 产物:v6/chatcoder-Setup-<version>.exe（electron-builder 输出目录见 package.json build.directories.output）
# 用法:powershell -ExecutionPolicy Bypass -File build-release.ps1 [-Publish]
#   -Publish: 打包后自动创建 GitHub Release 并上传产物（需 gh CLI 已登录，见 README 发布章节）
param(
    [switch]$Publish
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "=== [1/5] 构建前端 ===" -ForegroundColor Cyan
Push-Location "$root\client"
npm run build
if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
Pop-Location

Write-Host "=== [2/5] 打包后端(PyInstaller, --clean 全量重建) ===" -ForegroundColor Cyan
Push-Location "$root\server"
$pyi = ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) { throw "未找到 PyInstaller,请先 .venv\Scripts\pip install pyinstaller" }
# --clean 必须保留：增量构建在模块列表不变时不会重建 PYZ，会把旧字节码打进 exe
& $pyi chatcoder-server.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "后端打包失败" }
Pop-Location

# 产物守门：dist 里必须存在刚生成的 exe，防止静默复用旧产物
$serverExe = "$root\server\dist\chatcoder-server\chatcoder-server.exe"
if (-not (Test-Path $serverExe)) { throw "打包产物缺失: $serverExe" }
$serverExeItem = Get-Item $serverExe
Write-Host ("后端产物: {0} ({1:N1} MB, {2})" -f $serverExe, ($serverExeItem.Length/1MB), $serverExeItem.LastWriteTime) -ForegroundColor Yellow

Write-Host "=== [3/5] 部署后端到运行目录并重启服务 ===" -ForegroundColor Cyan
# electron-builder 输出目录(v6)与实际运行目录(v4\win-unpacked)脱节，必须显式同步，
# 否则打包成功但跑的还是旧版（本次事故根因之一）
& "$root\deploy-server.ps1" -Restart

Write-Host "=== [4/5] 打包桌面应用(electron-builder) ===" -ForegroundColor Cyan
& npx electron-builder --win
if ($LASTEXITCODE -ne 0) { throw "electron-builder 打包失败" }

Write-Host "=== [5/5] 完成 ===" -ForegroundColor Green
Get-ChildItem "$root\v6\*.exe" | ForEach-Object {
    Write-Host ("产物: " + $_.Name + " (" + [math]::Round($_.Length/1MB,1) + " MB)") -ForegroundColor Yellow
}

# ── 发布到 GitHub Releases（自动更新源：latest.yml + exe + blockmap）──
if ($Publish) {
    $version = (Get-Content "$root\package.json" | ConvertFrom-Json).version
    $tag = "v$version"
    Write-Host "=== 发布 $tag 到 GitHub Releases ===" -ForegroundColor Cyan
    # electron-updater 检查的是 latest 这个 tag 的动态链接，tag 名不影响检查；
    # 资产名必须与 latest.yml 中 url 一致（package.json nsis.artifactName 已保证无空格）。
    gh release create $tag "$root\v6\chatcoder-Setup-$version.exe" "$root\v6\latest.yml" "$root\v6\chatcoder-Setup-$version.exe.blockmap" --title $tag --notes "ChatCoder $tag"
    if ($LASTEXITCODE -ne 0) { throw "gh release create 失败" }
    Write-Host "发布完成: https://github.com/yueyuqingtian/chatcoder/releases/tag/$tag" -ForegroundColor Green
}
