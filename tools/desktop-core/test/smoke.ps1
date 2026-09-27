# 冒烟自测：验证常驻内核能启动、响应各类 op、并在结束后干净退出。
#
# 安全约定：
#   * 由本脚本启动服务端（内核自身不含任何自调用逻辑）。
#   * 只读 op（ping / info / windows / snapshot / hit / shot）全部测试。
#   * 写 op（click / type / keys）**不测**：它们会真的操作鼠标键盘，
#     测试环境里可能打到用户窗口。写路径由人工在受控窗口下验证。
#   * 结束时确保进程归零，不留残余。

[CmdletBinding()]
param(
    [string]$Exe = "",
    [string]$PipeName = "chatcoder_desktop_core_smoke"
)

$ErrorActionPreference = 'Stop'

# 注意：$PSScriptRoot 在 param() 默认值求值阶段为空（PS 5.1 已知行为），
# 因此路径解析必须放在主体里，不能用 param 默认值。
$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Exe) { $Exe = Join-Path (Split-Path $root -Parent) 'bin\desktop-core.exe' }
if (-not (Test-Path $Exe)) { throw "找不到内核：$Exe（先运行 build.ps1）" }

$pass = 0
$fail = 0
# 在 try 之外预置：finally 里会清理它，若定义在 try 内部且异常提前抛出，
# finally 引用未定义变量会抛 ParameterArgumentValidationError 掩盖真实错误。
$shotPath = Join-Path $env:TEMP 'dc-smoke-shot.jpg'

function Assert([string]$name, [bool]$ok, [string]$detail) {
    if ($ok) {
        $script:pass++
        Write-Host "  PASS  $name"
    } else {
        $script:fail++
        Write-Host "  FAIL  $name  -> $detail"
    }
}

Write-Host "[smoke] 内核: $Exe"
Write-Host "[smoke] 管道: $PipeName"

# 启动常驻服务端
$outLog = Join-Path $env:TEMP "dc-smoke-out.log"
$errLog = Join-Path $env:TEMP "dc-smoke-err.log"
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath $Exe -ArgumentList @('serve', $PipeName) -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Write-Host "[smoke] 服务端 pid=$($proc.Id)"

Start-Sleep -Milliseconds 900
if ($proc.HasExited) {
    $err = if (Test-Path $errLog) { Get-Content $errLog -Raw } else { '' }
    throw "[smoke] 服务端启动即退出，exit=$($proc.ExitCode) $err"
}

try {
    # 建立持久连接（整个测试复用同一条连接，这正是常驻模式的意义）
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', $PipeName,
        [System.IO.Pipes.PipeDirection]::InOut)
    $pipe.Connect(8000)
    $pipe.ReadMode = [System.IO.Pipes.PipeTransmissionMode]::Byte

    $writer = New-Object System.IO.StreamWriter($pipe, (New-Object System.Text.UTF8Encoding($false)), 65536)
    $writer.AutoFlush = $true
    $reader = New-Object System.IO.StreamReader($pipe, [System.Text.Encoding]::UTF8, $false, 65536)

    function Call([string]$json) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $writer.WriteLine($json)
        $line = $reader.ReadLine()
        $sw.Stop()
        [pscustomobject]@{ Ms = $sw.Elapsed.TotalMilliseconds; Raw = $line }
    }

    Write-Host "`n--- 只读操作 ---"

    # ping
    $r = Call '{"op":"ping"}'
    Assert 'ping' ($r.Raw -match '"pong":true') $r.Raw

    # info
    $r = Call '{"op":"info"}'
    $info = $r.Raw | ConvertFrom-Json
    Assert 'info 返回版本' ($null -ne $info.data.version) $r.Raw
    Assert 'info 返回物理分辨率' ($info.data.screen[0] -gt 0) $r.Raw
    Write-Host ("        分辨率={0}x{1}  DPI感知=物理像素" -f $info.data.screen[0], $info.data.screen[1])

    # windows
    $r = Call '{"op":"windows","limit":10}'
    $win = $r.Raw | ConvertFrom-Json
    Assert 'windows 返回窗口列表' ($win.data.count -gt 0) $r.Raw
    Write-Host ("        窗口数={0}  前台={1}" -f $win.data.count, $win.data.foregroundTitle)

    # snapshot（对一个真实窗口）
    $target = $win.data.items | Where-Object { $_.rect[2] -gt 300 -and $_.rect[3] -gt 200 } | Select-Object -First 1
    if ($target) {
        $r = Call ("{{`"op`":`"snapshot`",`"handle`":{0},`"budget`":200," -f $target.h + '"interactiveOnly":true}')
        $snap = $r.Raw | ConvertFrom-Json
        Assert 'snapshot 返回节点' ($null -ne $snap.data.nodes) $r.Raw
        Write-Host ("        目标='{0}'  交互元素={1}" -f $target.title, $snap.data.nodes)
    } else {
        Assert 'snapshot 找到可用目标窗口' $false '没有足够大的可见窗口'
    }

    # hit（ElementFromPoint，最便宜的原语）
    $cx = [int]($info.data.screen[0] / 2)
    $cy = [int]($info.data.screen[1] / 2)
    $r = Call ("{`"op`":`"hit`",`"x`":$cx,`"y`":$cy,`"chain`":true}")
    $hit = $r.Raw | ConvertFrom-Json
    Assert 'hit 命中元素' ($hit.data.found -eq $true) $r.Raw
    if ($hit.data.found) {
        Write-Host ("        命中 ct={0}  祖先链={1} 个" -f $hit.data.ct, @($hit.data.chain).Count)
    }

    # shot（截图 + 缩放 + JPEG）
    $r = Call ("{`"op`":`"shot`",`"maxDim`":1280,`"format`":`"jpg`",`"path`":`"" + ($shotPath -replace '\\','\\') + "`"}")
    $shot = $r.Raw | ConvertFrom-Json
    $exists = Test-Path $shotPath
    Assert 'shot 产出文件' $exists $r.Raw
    if ($exists) {
        $kb = [math]::Round((Get-Item $shotPath).Length / 1KB, 1)
        Write-Host ("        尺寸={0}x{1}  体积={2} KB  scale={3}" -f $shot.data.width, $shot.data.height, $kb, $shot.data.scale)
    }

    Write-Host "`n--- 未知 op 应返回错误而非崩溃 ---"
    $r = Call '{"op":"nonexistent"}'
    Assert '未知 op 返回 error' ($r.Raw -match '"error"') $r.Raw

    Write-Host "`n--- 常驻连接复用（连续 20 次 ping）---"
    $times = @()
    for ($i = 0; $i -lt 20; $i++) {
        $r = Call '{"op":"ping"}'
        $times += $r.Ms
    }
    $sorted = $times | Sort-Object
    $p50 = [math]::Round($sorted[[int]($sorted.Count / 2)], 3)
    Write-Host ("        ping p50 = {0} ms  (进程启动对比：C# exe ~30ms / PowerShell ~430ms)" -f $p50)
    Assert '常驻往返 < 5ms' ($p50 -lt 5) "p50=$p50 ms"

    Write-Host "`n--- 服务端仍在运行（验证不会因请求而重启）---"
    Assert '服务端存活' (-not $proc.HasExited) '服务端已退出'

    # 优雅关闭
    Write-Host "`n--- 优雅关闭 ---"
    $null = Call '{"op":"shutdown"}'
    Start-Sleep -Milliseconds 700
    Assert 'shutdown 后进程退出' $proc.HasExited '进程未退出'

} finally {
    $writer = $null; $reader = $null; $pipe = $null
    if (-not $proc.HasExited) {
        $proc.Kill()
        Start-Sleep -Milliseconds 300
    }
    Remove-Item $shotPath -ErrorAction SilentlyContinue
    $left = @(Get-Process -Name 'desktop-core' -ErrorAction SilentlyContinue).Count
    Write-Host "`n[smoke] 残留 desktop-core 进程: $left"
}

Write-Host ""
Write-Host "[smoke] 结果: $pass 通过 / $fail 失败"
if ($fail -gt 0) { exit 1 }
exit 0
