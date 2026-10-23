# chatcoder 内置 ripgrep 准备脚本：把本机的 rg.exe 复制到 server/vendor/ripgrep。
# 该目录由 chatcoder-server.spec 作为 datas 打进后端产物（_internal/ripgrep），
# 运行时 app/core/ripgrep_env.py 优先使用它，使「增强搜索」开箱即用，不依赖用户自行安装 ripgrep。
# 找不到 rg.exe 时只给出警告（运行时会回退系统 PATH，再不行回退纯 Python 搜索），不阻塞构建。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File server/prepare-ripgrep.ps1
#   powershell -ExecutionPolicy Bypass -File server/prepare-ripgrep.ps1 -Source D:\tools\rg.exe
param(
    [string]$Source = ""
)
$ErrorActionPreference = 'Stop'

$serverDir = $PSScriptRoot
$staging = Join-Path $serverDir "vendor\ripgrep"
$target = Join-Path $staging "rg.exe"

$src = $Source
if (-not $src) {
    $cmd = Get-Command rg -ErrorAction SilentlyContinue
    if ($cmd) { $src = $cmd.Source }
}
if (-not $src -or -not (Test-Path $src)) {
    Write-Warning "未找到 rg.exe（ripgrep）：安装包将不内置增强搜索。可先安装 ripgrep，或用 -Source 指定 rg.exe 路径。"
    exit 0
}

New-Item -ItemType Directory -Force -Path $staging | Out-Null
Copy-Item -Force -Path $src -Destination $target
$version = (& $target --version | Select-Object -First 1)
Write-Host ("已准备 ripgrep：{0}（{1}）" -f $target, $version) -ForegroundColor Green
