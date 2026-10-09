# 验证 OCR 定位精度（plan-365-1773）：合成菜单行 + 内核 findtext / ocrboxes。
#
# 为什么需要它：菜单栏这类「一行多个可点项」的界面，旧实现把整行合并成一个框，
# 搜索「收费」返回的是**整行中心**（落点常常偏到别的菜单项上），编号框也盖住整行。
# 本脚本用一张合成图把这个场景固定下来：修复前后各跑一次即可看到差异。
#
# 中文字面量一律用码点构造：.ps1 文件在 Windows 上可能被按 GBK 解码，
# 直接写中文会变成乱码（本机踩过的坑），码点构造与文件编码无关。
#
# 用法（务必 -NoProfile）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File verify-ocr-prec.ps1

param([string]$Exe = "")

$ErrorActionPreference = 'Stop'
if (-not $Exe) { $Exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'bin\desktop-core.exe' }
$Exe = (Resolve-Path $Exe).Path

function U([int[]]$cp) { -join ($cp | ForEach-Object { [char]$_ }) }

# 合成行的菜单项（探针选「首页」：OCR 对该词识别稳定，且距整行中心足够远）
$items = @(
    (U @(0x6536, 0x8D39)),              # 收费
    (U @(0x4F1A, 0x5458, 0x65E7)),      # 会员旧
    (U @(0x9996, 0x9875)),              # 首页
    (U @(0x6302, 0x53F7)),              # 挂号
    (U @(0x95E8, 0x8BCA)),              # 门诊
    (U @(0x53D1, 0x836F)),              # 发药
    (U @(0x7BA1, 0x7406)),              # 管理
    (U @(0x836F, 0x623F))               # 药房
)
$probe = $items[2]                                   # 「首页」
$probeSpaced = (U @(0x9996)) + ' ' + (U @(0x9875))   # 「首 页」（针里带空格）

$work = Join-Path $env:TEMP ('cc-ocr-prec-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$png = Join-Path $work 'menu.png'
$pngJson = $png.Replace('\', '/')                    # 内核的扁平解析器把反斜杠当转义符

# ── 1) 画合成图：每项间距 60px（远大于字高的一半，等价于「不同控件」）──
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap(1600, 240)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::White)
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$font = New-Object System.Drawing.Font('Microsoft YaHei', 30)
$brush = [System.Drawing.Brushes]::Black
$fmt = [System.Drawing.StringFormat]::GenericTypographic

$x = 60
$expect = @{}                                        # 项 → 中心 x
foreach ($it in $items) {
    $sz = $g.MeasureString($it, $font, (New-Object System.Drawing.PointF(0, 0)), $fmt)
    $w = [int][Math]::Ceiling($sz.Width)
    $expect[$it] = [int]($x + $w / 2)
    $g.DrawString($it, $font, $brush, [float]$x, [float]90, $fmt)
    if ($it -eq $items[0]) { $firstW = $w }
    $x += $w + 60
}
$g.Dispose()
$bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()

$lineRight = $x - 60
$lineCenter = [int](($lineRight + 60) / 2)
Write-Output ("合成图: " + $png)
Write-Output ("  期望「" + $probe + "」中心 x ≈ " + $expect[$probe] + "，整行右端 x ≈ " + $lineRight +
    "，整行中心 x ≈ " + $lineCenter)

# ── 2) 起内核 ──
$pipe = 'cc_ocr_prec_' + (Get-Random -Maximum 999999)
$outLog = Join-Path $work 'kernel.out.txt'
$errLog = Join-Path $work 'kernel.err.txt'
$proc = Start-Process -FilePath $Exe -ArgumentList @('serve', $pipe) -PassThru -NoNewWindow `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Start-Sleep -Milliseconds 700
$ready = ''
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

function Read-Line([int]$timeoutMs = 15000) {
    $task = $reader.ReadLineAsync()
    if (-not $task.Wait($timeoutMs)) { throw '内核响应超时' }
    return $task.Result
}
function Invoke-Op([string]$json) {
    $writer.WriteLine($json)
    $line = Read-Line
    if (-not $line) { throw ('内核无响应: ' + $json) }
    return ($line | ConvertFrom-Json)
}

$fail = 0
function Assert([bool]$cond, [string]$what) {
    if ($cond) { Write-Output ("  [ok] " + $what) }
    else { Write-Output ("  [FAIL] " + $what); $script:fail++ }
}

try {
    # ── 3) findtext：命中位置必须是「收费」自己，而不是整行中心 ──
    $ft = Invoke-Op ('{"op":"findtext","text":"' + $probe + '","image":"' + $pngJson +
        '","scale":1,"srcX":0,"srcY":0,"maxDim":1600,"maxCandidates":5}')
    Assert (-not $ft.data.error) 'findtext 无错误'
    Write-Output ("  findtext: found=" + $ft.data.found + " x=" + $ft.data.x + " y=" + $ft.data.y +
        " text=" + $ft.data.text)
    if ($ft.data.found) {
        $near = [Math]::Abs([int]$ft.data.x - [int]$expect[$probe])
        Write-Output ("  距「" + $probe + "」实际中心 " + $near + "px；距整行中心 " +
            ([Math]::Abs([int]$ft.data.x - $lineCenter)) + "px")
        Assert ($near -le 60) '命中落在目标项上（误差 ≤ 60px）'
        Assert ([Math]::Abs([int]$ft.data.x - $lineCenter) -gt 150) '命中不是整行中心（定位到词级）'
    }
    else { Assert $false 'findtext 应能找到目标项' }

    # ── 4) 带空格针：「收 费」也应按「收费」命中（中文 OCR 常把词拆开）──
    $ft2 = Invoke-Op ('{"op":"findtext","text":"' + $probeSpaced + '","image":"' + $pngJson +
        '","scale":1,"srcX":0,"srcY":0,"maxDim":1600,"maxCandidates":5}')
    Write-Output ("  findtext(带空格针): found=" + $ft2.data.found + " x=" + $ft2.data.x)
    Assert ($ft2.data.found -eq $true) '针里带空格时仍能命中'

    # ── 5) ocrboxes：每个菜单项应各自成块，不能合并成整行 ──
    $bx = Invoke-Op ('{"op":"ocrboxes","image":"' + $pngJson +
        '","scale":1,"srcX":0,"srcY":0,"maxDim":1600,"maxBoxes":50}')
    $boxes = @($bx.data.boxes)
    Write-Output ("  ocrboxes: count=" + $boxes.Count)
    $maxW = 0
    $probeBox = $null
    foreach ($b in $boxes) {
        if ([int]$b.w -gt $maxW) { $maxW = [int]$b.w }
        Write-Output ("    x=" + $b.x + " y=" + $b.y + " w=" + $b.w + " h=" + $b.h + " t=" + $b.t)
        if (($b.t -replace '\s', '') -eq $probe) { $probeBox = $b }
    }
    $minCx = 99999
    foreach ($b in $boxes) { $c0 = [int]($b.x + $b.w / 2); if ($c0 -lt $minCx) { $minCx = $c0 } }
    Write-Output ('  最左块中心 x=' + $minCx)
    Assert ($minCx -le 200) '最左块中心落在首项附近（未整行合并）'
    Assert ($boxes.Count -ge 5) '至少切出 5 个块（不是整行一块）'
    Assert ($maxW -le 320) '最大块宽 ≤ 320px（未把整行合并成一块）'
    Assert ($null -ne $probeBox) ('存在等于「' + $probe + '」的块')
    if ($probeBox) {
        $near2 = [Math]::Abs([int]($probeBox.x + $probeBox.w / 2) - [int]$expect[$probe])
        Write-Output ("  「" + $probe + "」块中心距期望 " + $near2 + "px")
        Assert ($near2 -le 60) '块中心落在目标项上'
    }

    if ($fail -eq 0) { Write-Output 'VERIFY-OCR-PREC: PASS' }
    else { Write-Output ("VERIFY-OCR-PREC: FAIL (" + $fail + ")") }
}
finally {
    try { Invoke-Op '{"op":"shutdown"}' | Out-Null } catch { }
    $client.Dispose()
    if (-not $proc.HasExited) { $proc.WaitForExit(3000) | Out-Null }
    if (-not $proc.HasExited) { $proc.Kill() }
    $err = if (Test-Path $errLog) { (Get-Content $errLog -Raw) } else { '' }
    if ($err) { Write-Output ("stderr: " + $err.Trim()) }
}
