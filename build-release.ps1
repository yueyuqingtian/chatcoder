# chatcoder 一键打包脚本(Windows)
# 产物:release/chatcoder Setup <version>.exe
# 用法:powershell -ExecutionPolicy Bypass -File build-release.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "=== [1/4] 构建前端 ===" -ForegroundColor Cyan
Push-Location "$root\client"
npm run build
if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
Pop-Location

Write-Host "=== [2/4] 打包后端(PyInstaller) ===" -ForegroundColor Cyan
Push-Location "$root\server"
$pyi = ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) { throw "未找到 PyInstaller,请先 .venv\Scripts\pip install pyinstaller" }
& $pyi chatcoder-server.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "后端打包失败" }
Pop-Location

Write-Host "=== [3/4] 打包桌面应用(electron-builder) ===" -ForegroundColor Cyan
& npx electron-builder --win
if ($LASTEXITCODE -ne 0) { throw "electron-builder 打包失败" }

Write-Host "=== [4/4] 完成 ===" -ForegroundColor Green
Get-ChildItem "$root\v5\*.exe" | ForEach-Object {
    Write-Host ("产物: " + $_.Name + " (" + [math]::Round($_.Length/1MB,1) + " MB)") -ForegroundColor Yellow
}
