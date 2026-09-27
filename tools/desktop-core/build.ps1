# 编译 desktop-core：用 Windows 自带的 .NET Framework csc.exe，无需 .NET SDK。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -OutDir ..\..\server\vendor\desktop-core
#   powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -CheckOnly
#
# 产物为单文件 exe，无外部依赖；放在 bin/ 下，由 server/app/core/desktop_env.py 定位。

[CmdletBinding()]
param(
    [string]$OutDir = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root 'bin' }
if (-not [System.IO.Path]::IsPathRooted($OutDir)) { $OutDir = Join-Path $root $OutDir }

# .NET Framework 编译器与 WPF 程序集（UI Automation 的托管封装在这里）
$fw = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$csc = Join-Path $fw 'csc.exe'
$wpf = Join-Path $fw 'WPF'

if (-not (Test-Path $csc)) {
    throw "找不到 csc.exe：$csc（需要 .NET Framework 4.x）"
}

$refs = @(
    (Join-Path $wpf 'UIAutomationClient.dll'),
    (Join-Path $wpf 'UIAutomationTypes.dll'),
    (Join-Path $wpf 'WindowsBase.dll'),
    # proccmd op（读取进程命令行以提取 CDP 调试端口）需要 WMI
    (Join-Path $fw 'System.Management.dll')
)
foreach ($r in $refs) {
    if (-not (Test-Path $r)) { throw "缺少引用程序集：$r" }
}

$sources = @('Interop.cs', 'Sensing.cs', 'Input.cs', 'Apps.cs', 'Ocr.cs', 'Program.cs') | ForEach-Object {
    Join-Path $root "src\$_"
}
foreach ($s in $sources) {
    if (-not (Test-Path $s)) { throw "缺少源文件：$s" }
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$out = Join-Path $OutDir 'desktop-core.exe'

$args = @('/nologo', '/target:exe', '/optimize+')
if ($CheckOnly) { $args += '/target:library'; $args += "/out:$env:TEMP\desktop-core-check.dll" }
else { $args += "/out:$out" }
foreach ($r in $refs) { $args += "/reference:$r" }
$args += '/reference:System.Drawing.dll'
$args += $sources

Write-Host "[desktop-core] csc $($args -join ' ')"
$output = & $csc @args 2>&1
$code = $LASTEXITCODE

if ($output) { $output | ForEach-Object { Write-Host $_ } }

if ($code -ne 0) {
    throw "[desktop-core] 编译失败，exit=$code"
}
if ($CheckOnly) {
    Write-Host "[desktop-core] 语法检查通过"
    exit 0
}

$size = (Get-Item $out).Length
Write-Host "[desktop-core] 编译成功: $out ($size bytes)"

# 编译后自检：exe 必须拒绝非预期参数（这是防自调用的守卫）。
# 注意：PowerShell 5.1 在 $ErrorActionPreference='Stop' 下会把 native 命令的 stderr
# 当作终止性错误（即便已重定向到文件），因此这里局部临时放宽偏好设置。
$probeErr = Join-Path $env:TEMP 'desktop-core-probe.err'
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $out 1>$null 2>$probeErr
$probeCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap

if ($probeCode -ne 2) {
    $msg = if (Test-Path $probeErr) { Get-Content $probeErr -Raw } else { '' }
    throw "[desktop-core] 参数守卫失效：未带参数启动时退出码应为 2，实际为 $probeCode。$msg"
}
Write-Host "[desktop-core] 参数守卫自检通过（无参数启动返回退出码 2）"
