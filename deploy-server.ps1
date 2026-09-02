# chatcoder 后端部署脚本：把 PyInstaller 产物同步到实际运行目录并验证
# 背景：electron-builder 输出目录(v6)与实际运行目录(v4\win-unpacked)脱节，
#       server/dist 打包成功不等于运行目录更新，必须显式同步。
# 用法:
#   powershell -ExecutionPolicy Bypass -File deploy-server.ps1            # 仅部署+验证
#   powershell -ExecutionPolicy Bypass -File deploy-server.ps1 -Restart   # 部署+重启 8000 端口服务
param(
    [string]$TargetDir = "v4\win-unpacked\resources\server\chatcoder-server",
    [switch]$Restart
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$src = "$root\server\dist\chatcoder-server"
$srcExe = "$src\chatcoder-server.exe"
if (-not (Test-Path $srcExe)) { throw "未找到打包产物 $srcExe，请先执行打包（build:backend 或 build-release.ps1）" }

# 1. 运行中的服务会锁住部署目录文件且继续跑旧代码，必须先停才能部署
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    if (-not $Restart) { throw "8000 端口服务运行中（PID $($conn.OwningProcess)），会锁住部署文件。请加 -Restart 参数：先停止、部署后自动重启" }
    $p = Get-Process -Id $conn.OwningProcess
    Write-Host "停止旧进程 PID $($p.Id)（$($p.Path)）" -ForegroundColor Yellow
    Stop-Process -Id $p.Id -Force
    Start-Sleep -Seconds 2
} else {
    Write-Host "8000 端口无运行中的服务" -ForegroundColor DarkGray
}

# 2. 同步产物到运行目录
Write-Host "部署 $src -> $TargetDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Copy-Item "$src\*" $TargetDir -Recurse -Force

# 3. 哈希验证：运行目录的 exe 必须与 dist 产物完全一致，防止"部署了旧版"再次发生
$srcHash = (Get-FileHash $srcExe -Algorithm SHA256).Hash
$dstExe = Join-Path $TargetDir "chatcoder-server.exe"
$dstHash = (Get-FileHash $dstExe -Algorithm SHA256).Hash
if ($srcHash -ne $dstHash) { throw "部署验证失败：exe 哈希不一致 src=$srcHash dst=$dstHash" }
Write-Host "部署验证通过 SHA256=$($srcHash.Substring(0,12))... $((Get-Item $dstExe).LastWriteTime)" -ForegroundColor Green

# 4. 可选：重启服务
if ($Restart) {
    Start-Process $dstExe -WorkingDirectory $TargetDir
    Write-Host "服务已从 $dstExe 重启" -ForegroundColor Green
}
