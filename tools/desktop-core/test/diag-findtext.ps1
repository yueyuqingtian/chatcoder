# 定位 findtext 复用图片与自截图两条路径的坐标分歧（plan-365-1757 排查用）。
#
# 目的：把两条路径的**原始返回**并排打出来，并比对两张全屏截图的哈希与真实尺寸，
# 以区分「换算错误」与「两次截图内容不同」。
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File diag-findtext.ps1

param([string]$Exe = "", [string]$Query = "e")

$ErrorActionPreference = 'Stop'
if (-not $Exe) { $Exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'bin\desktop-core.exe' }
$Exe = (Resolve-Path $Exe).Path

$stem = (Join-Path $env:TEMP 'cc-diag-365').Replace('\', '/')
$pipe = 'cc_diag_365_' + (Get-Random -Maximum 999999)
$outLog = Join-Path $env:TEMP 'cc-diag-365.out.txt'
$errLog = Join-Path $env:TEMP 'cc-diag-365.err.txt'
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath $Exe -ArgumentList @('serve', $pipe) -PassThru -NoNewWindow `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Start-Sleep -Milliseconds 700

$client = New-Object System.IO.Pipes.NamedPipeClientStream('.', $pipe,
    [System.IO.Pipes.PipeDirection]::InOut)
$client.Connect(3000)
$writer = New-Object System.IO.StreamWriter($client)
$writer.AutoFlush = $true
$reader = New-Object System.IO.StreamReader($client)

function Read-Line([int]$timeoutMs = 15000) {
    $task = $reader.ReadLineAsync()
    if (-not $task.Wait($timeoutMs)) { throw '内核响应超时' }
    return $task.Result
}
function Op([string]$json) { $writer.WriteLine($json); return (Read-Line) }
function OpJson([string]$json) { return ((Op $json) | ConvertFrom-Json) }

try {
    # ① 全屏截图 P1（与工具层用法一致）
    $p1 = OpJson ('{"op":"shot","maxDim":1600,"format":"png","path":"' + $stem + '-p1.png"}')
    Write-Output ('P1 shot 返回: ' + $p1.data.width + 'x' + $p1.data.height + ' src=' + $p1.data.srcW +
        'x' + $p1.data.srcH + ' scale=' + $p1.data.scale + ' srcXY=' + $p1.data.srcX + ',' + $p1.data.srcY)

    # ② 复用 P1 做 OCR
    $p1Json = $p1.data.path.Replace('/', '\').Replace('\', '\\')
    Write-Output '--- 复用图片 findtext 原始返回 ---'
    Write-Output (Op ('{"op":"findtext","text":"' + $Query + '","image":"' + $p1Json +
        '","scale":' + $p1.data.scale + ',"srcX":' + $p1.data.srcX + ',"srcY":' + $p1.data.srcY +
        ',"maxDim":1600,"maxCandidates":2}'))

    # ③ 自截图做 OCR（同一时刻、同一区域）
    Write-Output '--- 自截图 findtext 原始返回 ---'
    Write-Output (Op ('{"op":"findtext","text":"' + $Query + '","maxDim":1600,"maxCandidates":2}'))

    # ③b 文字行框（plan-365-1757 A2：自绘界面编号的来源），同样支持复用图片
    Write-Output '--- ocrboxes（复用 P1）---'
    $boxes = OpJson ('{"op":"ocrboxes","image":"' + $p1Json + '","scale":' + $p1.data.scale +
        ',"srcX":' + $p1.data.srcX + ',"srcY":' + $p1.data.srcY + ',"maxDim":1600,"maxBoxes":5}')
    if ($boxes.data.error) {
        Write-Output ('ocrboxes 错误: ' + $boxes.data.error)
    }
    else {
        Write-Output ('行框数=' + $boxes.data.count + ' src=' + $boxes.data.src +
            ' scaleUsed=' + $boxes.data.scaleUsed + ' imageSize=' + ($boxes.data.imageSize -join 'x'))
        foreach ($b in @($boxes.data.boxes)) {
            Write-Output ('  ' + $b.x + ',' + $b.y + ' ' + $b.w + 'x' + $b.h + '  "' + $b.t + '"')
        }
        # 行框必须落在截图区域内，否则说明坐标换算与 findtext 不一致
        $vw = [int]($p1.data.srcX + $p1.data.srcW)
        $vh = [int]($p1.data.srcY + $p1.data.srcH)
        $bad = @($boxes.data.boxes | Where-Object { $_.x -lt 0 -or $_.y -lt 0 -or $_.x -gt $vw -or $_.y -gt $vh })
        Write-Output ('行框越界数量=' + $bad.Count + '（应为 0）')
    }

    # ④ 再截一张 P2，与 P1 比对：内容是否真的没变
    $p2 = OpJson ('{"op":"shot","maxDim":1600,"format":"png","path":"' + $stem + '-p2.png"}')
    $h1 = (Get-FileHash ($p1.data.path.Replace('/', '\')) -Algorithm SHA256).Hash
    $h2 = (Get-FileHash ($p2.data.path.Replace('/', '\')) -Algorithm SHA256).Hash
    Write-Output ('P1 sha=' + $h1.Substring(0, 16) + '  P2 sha=' + $h2.Substring(0, 16) +
        '  字节相同=' + ($h1 -eq $h2))

    Add-Type -AssemblyName System.Drawing
    foreach ($path in @($p1.data.path, $p2.data.path)) {
        $local = $path.Replace('/', '\')
        $img = [System.Drawing.Image]::FromFile($local)
        Write-Output ('文件 ' + $local + ' = ' + $img.Width + 'x' + $img.Height +
            ' (' + (Get-Item $local).Length + ' bytes)')
        $img.Dispose()
    }
}
finally {
    try { Op '{"op":"shutdown"}' | Out-Null } catch { }
    $client.Dispose()
    if (-not $proc.HasExited) { $proc.WaitForExit(3000) | Out-Null }
    if (-not $proc.HasExited) { $proc.Kill() }
    $err = if (Test-Path $errLog) { (Get-Content $errLog -Raw) } else { '' }
    if ($err) { Write-Output ('stderr: ' + $err.Trim()) }
}
