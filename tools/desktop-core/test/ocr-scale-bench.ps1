# OCR 分辨率-耗时曲线实测（plan-365-1774）。
#
# 目的：截图链路的固定成本是「shot（110ms）+ OCR（320ms）」。要压缩总耗时，
# 唯一有量级空间的就是 OCR；而 OCR 耗时与图像像素量相关。
# 本脚本把同一张整屏图缩到不同长边，分别 OCR 并对比「耗时 / 识别块数」，
# 用数据决定能否用更低分辨率换时间（必须同时看识别质量，不能为快牺牲点准率）。
#
# 只读，不注入输入。用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File ocr-scale-bench.ps1

param([string]$Exe = "")

$ErrorActionPreference = 'Stop'
if (-not $Exe) { $Exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'bin\desktop-core.exe' }
$Exe = (Resolve-Path $Exe).Path

Add-Type -AssemblyName System.Drawing

$work = Join-Path $env:TEMP ('cc-ocrscale-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work -Force | Out-Null

# 先截一张真实整屏图作为样张（内核 shot），再在本地缩放出各尺寸副本
$pipe = 'cc_ocrscale_' + (Get-Random -Maximum 999999)
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

function Read-Line([int]$timeoutMs = 60000) {
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
    # 1) 取一张真实整屏截图（163 万像素级）
    $src = Join-Path $work 'base.png'
    $r = Invoke-Timed ('{"op":"shot","maxDim":0,"format":"png","path":"' +
        $src.Replace('\', '/') + '"}')
    $base = [System.Drawing.Image]::FromFile($src)
    Write-Output ("样张: " + $base.Width + "x" + $base.Height +
        "  截屏耗时 " + $script:lastMs + " ms")
    $ratio = [double]$base.Width / $base.Height

    # 2) 各长边尺寸：缩放副本 → 分别 OCR
    foreach ($long in @(2560, 1600, 1280, 1024, 800)) {
        $w = $long
        $h = [int]($long / $ratio)
        $dst = Join-Path $work ("s$long.png")
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($base, 0, 0, $w, $h)
        $g.Dispose()
        $bmp.Save($dst, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()

        $dstJson = $dst.Replace('\', '/')
        # 第一次（冷：不同内容 → 必然真跑 OCR）
        $r1 = Invoke-Timed ('{"op":"ocrboxes","image":"' + $dstJson +
            '","scale":1,"srcX":0,"srcY":0,"maxDim":0,"maxBoxes":500}')
        $cold = $script:lastMs
        $boxes = @($r1.data.boxes)
        $avgH = 0
        if ($boxes.Count -gt 0) {
            $avgH = [int](($boxes | Measure-Object -Property h -Average).Average)
        }
        Write-Host ("长边 {0,5}: {1,5} ms   块数={2,4}  平均字高={3,3}px" -f `
            $long, $cold, $boxes.Count, $avgH)
    }
}
finally {
    try {
        $writer.WriteLine('{"op":"shutdown"}')
        $null = Read-Line 5000
    } catch { }
    $client.Dispose()
    if (-not $proc.HasExited) { $proc.WaitForExit(3000) | Out-Null }
    if (-not $proc.HasExited) { $proc.Kill() }
    $err = if (Test-Path $errLog) { (Get-Content $errLog -Raw) } else { '' }
    if ($err) { Write-Output ("stderr: " + $err.Trim()) }
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
