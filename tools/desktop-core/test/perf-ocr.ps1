# 性能测量（plan-365-1774）：实测内核各 op 耗时，为「操作变慢」定位大头。
#
# 为什么要单独测：用户反馈操作比改动前慢约 4 倍，怀疑 OCR 及感知链路。盲猜没有意义——
# 本机实测 shot / ocrboxes / findtext / snapshot 各自的毫秒数，才能确定优化对象。
# 全部只读（截屏、OCR、枚举），不注入任何输入。
#
# 用法（务必 -NoProfile）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File perf-ocr.ps1

param([string]$Exe = "")

$ErrorActionPreference = 'Stop'
if (-not $Exe) { $Exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'bin\desktop-core.exe' }
$Exe = (Resolve-Path $Exe).Path

$work = Join-Path $env:TEMP ('cc-perf-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$stem = (Join-Path $work 'shot').Replace('\', '/')

$pipe = 'cc_perf_' + (Get-Random -Maximum 999999)
$outLog = Join-Path $work 'kernel.out.txt'
$errLog = Join-Path $work 'kernel.err.txt'
$proc = Start-Process -FilePath $Exe -ArgumentList @('serve', $pipe) -PassThru -NoNewWindow `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Start-Sleep -Milliseconds 700
for ($i = 0; $i -lt 40; $i++) {
    if (Test-Path $outLog) {
        $ready = (Get-Content $outLog -Raw -ErrorAction SilentlyContinue)
        if ($ready -and $ready.Contains('ready')) { break }
    }
    Start-Sleep -Milliseconds 50
}

$client = New-Object System.IO.Pipes.NamedPipeClientStream('.', $pipe,
    [System.IO.Pipes.PipeDirection]::InOut)
$client.Connect(3000)
$writer = New-Object System.IO.StreamWriter($client)
$writer.AutoFlush = $true
$reader = New-Object System.IO.StreamReader($client)

function Read-Line([int]$timeoutMs = 30000) {
    $task = $reader.ReadLineAsync()
    if (-not $task.Wait($timeoutMs)) { throw '内核响应超时' }
    return $task.Result
}

$script:lastMs = 0
function Invoke-Timed([string]$json) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $writer.WriteLine($json)
    $line = Read-Line
    $sw.Stop()
    $script:lastMs = [int]$sw.ElapsedMilliseconds
    return ($line | ConvertFrom-Json)
}

function Stat([string]$label, [object[]]$ms) {
    $vals = @($ms | ForEach-Object { [int]$_ })
    $avg = [int](($vals | Measure-Object -Average).Average)
    $max = ($vals | Measure-Object -Maximum).Maximum
    # Write-Host：不进入函数输出流，否则会被 `$res.x = Stat ...` 赋值捕获、不在控制台显示
    Write-Host ("{0,-42} avg={1,6} ms  max={2,6} ms  runs=[{3}]" -f `
        $label, $avg, $max, (($vals | ForEach-Object { $_.ToString() }) -join ','))
    return $avg
}

try {
    Write-Output '===== 内核性能测量（单位 ms）====='
    $res = @{}

    # ── A. 截图（整屏 1600 jpg）：感知链路的固定成本 ──
    $a = @()
    for ($i = 0; $i -lt 3; $i++) {
        $r = Invoke-Timed ('{"op":"shot","maxDim":1600,"format":"jpg","path":"' + $stem + '-' + $i + '.jpg"}')
        $a += $script:lastMs
    }
    $res.shot = Stat 'A. shot 整屏 1600 jpg' $a
    $fullPath = $r.data.path
    $fullScale = $r.data.scale
    $fullX = $r.data.srcX; $fullY = $r.data.srcY
    $fullJson = $fullPath.Replace('\', '/')

    # ── B. ocrboxes 同一张图 ×3：第 1 次冷、后续热（当前无缓存 → 应都差不多）──
    $b = @()
    for ($i = 0; $i -lt 3; $i++) {
        $r = Invoke-Timed ('{"op":"ocrboxes","image":"' + $fullJson + '","scale":' + $fullScale +
            ',"srcX":' + $fullX + ',"srcY":' + $fullY + ',"maxDim":1600,"maxBoxes":200}')
        $b += $script:lastMs
    }
    $res.ocrboxes = Stat 'B. ocrboxes 复用图 ×3' $b
    Write-Output ("     （块数=" + $r.data.count + "）")

    # ── C. findtext 复用同一张图 ×3 ──
    $probes = @('设置', '文件', '编辑', '登录', '搜索')
    $c = @()
    $hitWord = ''
    foreach ($q in $probes) {
        $r = Invoke-Timed ('{"op":"findtext","text":"' + $q + '","image":"' + $fullJson +
            '","scale":' + $fullScale + ',"srcX":' + $fullX + ',"srcY":' + $fullY + ',"maxDim":1600}')
        $c += $script:lastMs
        if (-not $hitWord -and $r.data.found) { $hitWord = $q }
    }
    $res.findtext_reuse = Stat 'C. findtext 复用图 ×5' $c

    # ── D. findtext 自截图（无 image）×2：截图 + OCR 全链路 ──
    $d = @()
    for ($i = 0; $i -lt 2; $i++) {
        if ($hitWord) {
            $r = Invoke-Timed ('{"op":"findtext","text":"' + $hitWord + '","maxDim":1600}')
        } else {
            $r = Invoke-Timed '{"op":"findtext","text":"a","maxDim":1600}'
        }
        $d += $script:lastMs
    }
    $res.findtext_self = Stat 'D. findtext 自截图（截屏+OCR）×2' $d

    # ── E. snapshot（UIA）：Chrome 窗口遍历成本 ──
    $w = Invoke-Timed '{"op":"windows","limit":120}'
    $chrome = $null
    foreach ($it in @($w.data.items)) {
        if (($it.title -like '*Chrome*') -and ($it.cls -eq 'Chrome_WidgetWin_1')) { $chrome = $it; break }
    }
    if ($chrome) {
        $e = @()
        for ($i = 0; $i -lt 3; $i++) {
            $r = Invoke-Timed ('{"op":"snapshot","handle":' + $chrome.h +
                ',"interactiveOnly":true,"budget":300,"depth":12,"timeBudgetMs":700}')
            $e += $script:lastMs
        }
        $res.snapshot = Stat 'E. snapshot Chrome（depth12/700ms）' $e
        Write-Host ("     （元素数=" + @($r.data.items).Count + "，已用 " + $r.data.elapsedMs + " ms）")
    } else {
        Write-Output 'E. snapshot Chrome：未找到 Chrome 窗口，跳过'
    }

    # ── F. 轻量 op（窗口枚举 / 帧 / 命中）──
    $f = @()
    for ($i = 0; $i -lt 3; $i++) {
        $r = Invoke-Timed '{"op":"windows","limit":120}'
        $f += $script:lastMs
    }
    $res.windows = Stat 'F1. windows 枚举' $f

    $g = @()
    for ($i = 0; $i -lt 5; $i++) {
        $r = Invoke-Timed '{"op":"hit","x":1280,"y":800}'
        $g += $script:lastMs
    }
    $res.hit = Stat 'F2. hit 命中' $g

    Write-Output ''
    Write-Output '===== 合成结论 ====='
    $clickRef = $res.windows + $res.snapshot      # 服务端一次 ref 点击的感知成本（UIA 分支）
    $clickText = $res.windows + $res.findtext_self  # 服务端一次 text_target 点击
    $shotTotal = $res.shot + $res.snapshot          # 一次 desktop_screenshot（UIA 分支）
    Write-Output ("一次「ref 点击」服务端感知成本 ≈ {0} ms（windows+snapshot）" -f $clickRef)
    Write-Output ("一次「text 点击」内核成本   ≈ {0} ms（findtext 自截图）" -f $clickText)
    Write-Output ("一次「截图+编号」内核成本   ≈ {0} ms（shot+snapshot；未含画编号）" -f $shotTotal)
}
finally {
    try { Invoke-Timed '{"op":"shutdown"}' | Out-Null } catch { }
    $client.Dispose()
    if (-not $proc.HasExited) { $proc.WaitForExit(3000) | Out-Null }
    if (-not $proc.HasExited) { $proc.Kill() }
    $err = if (Test-Path $errLog) { (Get-Content $errLog -Raw) } else { '' }
    if ($err) { Write-Output ("stderr: " + $err.Trim()) }
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
