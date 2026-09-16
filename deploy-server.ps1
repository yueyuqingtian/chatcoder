# chatcoder 后端部署脚本：把 PyInstaller 产物同步到实际运行目录并验证
# 用法:
#   powershell -ExecutionPolicy Bypass -File deploy-server.ps1            # 仅部署+验证
#   powershell -ExecutionPolicy Bypass -File deploy-server.ps1 -Restart   # 部署+重启 12973 端口服务
param(
    [string]$TargetDir = "v7\win-unpacked\resources\server\chatcoder-server",
    [switch]$Restart
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$src = "$root\server\dist\chatcoder-server"
$srcExe = "$src\chatcoder-server.exe"
$workerSrc = "$root\server\dist\chatcoder-index-worker"
$workerExe = "$workerSrc\chatcoder-index-worker.exe"
if (-not (Test-Path $srcExe)) { throw "未找到打包产物 $srcExe，请先执行打包（build:backend 或 build-release.ps1）" }
if (-not (Test-Path $workerExe)) { throw "未找到索引 worker $workerExe，请先执行打包" }

# 1. 运行中的服务会锁住部署目录文件且继续跑旧代码，必须先停才能部署
$conn = Get-NetTCPConnection -LocalPort 12973 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    if (-not $Restart) { throw "12973 端口服务运行中（PID $($conn.OwningProcess)），会锁住部署文件。请加 -Restart 参数：先停止、部署后自动重启" }
    $p = Get-Process -Id $conn.OwningProcess
    Write-Host "停止旧进程 PID $($p.Id)（$($p.Path)）" -ForegroundColor Yellow
    Stop-Process -Id $p.Id -Force
    Start-Sleep -Seconds 2
} else {
    Write-Host "12973 端口无运行中的服务" -ForegroundColor DarkGray
}

# 2. 同步产物到运行目录
Write-Host "部署 $src -> $TargetDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
Copy-Item "$src\*" $TargetDir -Recurse -Force
# worker 放在主 exe 同级目录，主服务按 frozen 路径查找
Copy-Item "$workerSrc\*" $TargetDir -Recurse -Force

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
