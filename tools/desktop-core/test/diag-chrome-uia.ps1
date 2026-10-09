# 诊断脚本（plan-365-1774）：Chrome 窗口的 UIA 快照到底返回什么、不同时间预算的代价，
# 以及 OCR 冷/热对比。用于决定「Chromium 无 CDP 时」元素采集该走 UIA 还是 OCR。
#
# 只读：截屏 + 遍历 + 命中测试，不注入任何输入。
# 用法（务必 -NoProfile）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File diag-chrome-uia.ps1

param([string]$Exe = "tools\desktop-core\bin\desktop-core.exe")

$ErrorActionPreference = 'Stop'
$Exe = (Resolve-Path $Exe).Path
$work = Join-Path $env:TEMP ('cc-diag-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$stem = (Join-Path $work 'diag').Replace('\', '/')

$pipe = 'cc_diag_' + (Get-Random -Maximum 999999)
$outLog = Join-Path $work 'k.out.txt'
$errLog = Join-Path $work 'k.err.txt'
$proc = Start-Process -FilePath $Exe -ArgumentList @('serve', $pipe) -PassThru -NoNewWindow `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Start-Sleep -Milliseconds 700
for ($i = 0; $i -lt 40; $i++) {
    if (Test-Path $outLog) {
        $ready = Get-Content $outLog -Raw -ErrorAction SilentlyContinue
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
function Invoke-Timed([string]$json) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $writer.WriteLine($json)
    $line = Read-Line
    $sw.Stop()
    $script:lastMs = [int]$sw.ElapsedMilliseconds
    return ($line | ConvertFrom-Json)
}

try {
    $w = Invoke-Timed '{"op":"windows","limit":120}'
    $chrome = $null
    foreach ($it in @($w.data.items)) {
        if (($it.title -like '*Chrome*') -and ($it.cls -eq 'Chrome_WidgetWin_1')) { $chrome = $it; break }
    }
    if (-not $chrome) {
        Write-Output '未找到 Chrome 窗口，跳过'
    } else {
        Write-Output ('Chrome 窗口: ' + $chrome.title + '  handle=' + $chrome.h)

        foreach ($b in @(700, 350, 200)) {
            $r = Invoke-Timed ('{"op":"snapshot","handle":' + $chrome.h +
                ',"interactiveOnly":false,"budget":400,"depth":12,"timeBudgetMs":' + $b + '}')
            $items = @($r.data.items)
            Write-Output ('--- budget=' + $b + 'ms 返回 ' + $items.Count + ' 个元素，耗时 ' +
                $script:lastMs + ' ms（visited=' + $r.data.visited + ' truncated=' + $r.data.truncated + '）')
            $n = 0
            foreach ($it in $items) {
                if ($n -ge 30) { break }
                Write-Output ('    [' + $it.ct + '] ' + $it.n)
                $n++
            }
        }

        # 命中测试：窗口客户区中心（看 UIA 能不能看到网页内容）
        $r2 = Invoke-Timed ('{"op":"hit","x":1280,"y":800}')
        Write-Output ('--- hit(1280,800): ' + $script:lastMs + ' ms  ' + $r2.data.n +
            ' [' + $r2.data.ct + ']')
        $chain = @($r2.data.chain)
        $n = 0
        foreach ($c in $chain) {
            if ($n -ge 6) { break }
            Write-Output ('    ^ ' + $c.n + ' [' + $c.ct + ']')
            $n++
        }
    }

    # OCR 对比：同一张整屏图的冷/热识别
    $s = Invoke-Timed ('{"op":"shot","maxDim":1600,"format":"png","path":"' + $stem + '-full.png"}')
    Write-Output ('--- shot: ' + $script:lastMs + ' ms  scale=' + $s.data.scale +
        '  src=' + $s.data.srcW + 'x' + $s.data.srcH)
    $imgJson = $s.data.path.Replace('\', '/')
    $r2 = Invoke-Timed ('{"op":"ocrboxes","image":"' + $imgJson + '","scale":' + $s.data.scale +
        ',"srcX":' + $s.data.srcX + ',"srcY":' + $s.data.srcY + ',"maxDim":1600,"maxBoxes":300}')
    $boxes = @($r2.data.boxes)
    Write-Output ('--- ocrboxes(冷): ' + $script:lastMs + ' ms  ' + $boxes.Count + ' 块')
    $n = 0
    foreach ($bx in $boxes) {
        if ($n -ge 25) { break }
        Write-Output ('    ' + $bx.t)
        $n++
    }
    $r3 = Invoke-Timed ('{"op":"ocrboxes","image":"' + $imgJson + '","scale":' + $s.data.scale +
        ',"srcX":' + $s.data.srcX + ',"srcY":' + $s.data.srcY + ',"maxDim":1600,"maxBoxes":300}')
    Write-Output ('--- ocrboxes(热): ' + $script:lastMs + ' ms')
}
finally {
    try {
        $writer.WriteLine('{"op":"shutdown"}')
        $null = Read-Line 5000
    } catch { }
    $client.Dispose()
    if (-not $proc.HasExited) { $proc.WaitForExit(3000) | Out-Null }
    if (-not $proc.HasExited) { $proc.Kill() }
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
