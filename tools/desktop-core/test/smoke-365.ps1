# 冒烟测试 plan-365-1757 新增的内核 op：frame / waitstable / zoom / findtext（复用图片）。
#
# 为什么单独测这几个：它们是「点准」链路新增的本地能力，全部只读、不注入任何输入，
# 因此可以在不影响用户桌面的前提下直接验证；一旦这里返回结构不对，工具层的换算就会跟着错。
#
# 用法（务必 -NoProfile，本机 profile 会拖慢启动）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File smoke-365.ps1

param([string]$Exe = "")

$ErrorActionPreference = 'Stop'
if (-not $Exe) { $Exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'bin\desktop-core.exe' }
$Exe = (Resolve-Path $Exe).Path

# 路径一律用正斜杠写进 JSON：内核的扁平解析器把反斜杠当转义符，
# "C:\Users\x" 会被解析成 "C:Usersx"（实测踩到的坑，表现为 GDI+ 保存失败）。
$stem = (Join-Path $env:TEMP 'cc-smoke-365').Replace('\', '/')

$pipe = 'cc_smoke_365_' + (Get-Random -Maximum 999999)
$outLog = Join-Path $env:TEMP 'cc-smoke-365.out.txt'
$errLog = Join-Path $env:TEMP 'cc-smoke-365.err.txt'
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath $Exe -ArgumentList @('serve', $pipe) -PassThru -NoNewWindow `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Start-Sleep -Milliseconds 700

# ready 行写在进程 stdout（不在管道里），因此从日志轮询；管道一旦连上即说明服务已就绪。
$ready = ''
for ($i = 0; $i -lt 40; $i++) {
    if (Test-Path $outLog) {
        $ready = (Get-Content $outLog -Raw -ErrorAction SilentlyContinue)
        if ($ready -and $ready.Contains('ready')) { break }
    }
    Start-Sleep -Milliseconds 50
}
Write-Output ("ready: " + ($ready -replace "`r?`n", ' ').Trim())

$client = New-Object System.IO.Pipes.NamedPipeClientStream('.', $pipe,
    [System.IO.Pipes.PipeDirection]::InOut)
$client.Connect(3000)
$writer = New-Object System.IO.StreamWriter($client)
$writer.AutoFlush = $true
$reader = New-Object System.IO.StreamReader($client)

# NamedPipeClientStream 不支持 ReadTimeout（会抛 "Timeouts are not supported on this stream"），
# 改用异步读取 + 等待，任何一步无响应都快速失败，不让脚本挂住。
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

function Get-FileHashOf([string]$path) {
    if (-not (Test-Path $path)) { return '' }
    return (Get-FileHash $path -Algorithm SHA256).Hash
}

$fail = 0
function Assert([bool]$cond, [string]$what) {
    if ($cond) { Write-Output ("  [ok] " + $what) }
    else { Write-Output ("  [FAIL] " + $what); $script:fail++ }
}

try {
    # ① windows：取一个真实窗口，供 frame / 区域截图使用
    $w = Invoke-Op '{"op":"windows","limit":20}'
    Write-Output ("windows: count=" + $w.data.count)
    Assert ($w.data.count -ge 1) 'windows 返回至少一个窗口'

    # 屏幕尺寸来自 info op（windows 响应里没有 virtual 字段——踩过：$null 索引会直接抛错）
    $info = Invoke-Op '{"op":"info"}'
    $vw = [int]$info.data.virtualScreen.w
    $vh = [int]$info.data.virtualScreen.h
    Write-Output ("info: screen=" + ($info.data.screen -join 'x') + " virtual=" + $vw + "x" + $vh +
        " origin=" + $info.data.virtualScreen.x + "," + $info.data.virtualScreen.y)

    $items = @($w.data.items)
    foreach ($it in $items) {
        $r = @($it.rect)
        Write-Output ("  - " + $it.title + "  h=" + $it.h + "  cls=" + $it.cls + "  rect=" + ($r -join ','))
    }

    # 选一个完全落在虚拟屏内的窗口（有些窗口 rect 为负或越界，不适合做区域取样）
    $pick = $null
    foreach ($it in $items) {
        $r = @($it.rect)
        if ($r.Count -lt 4) { continue }
        if ($r[0] -lt 0 -or $r[1] -lt 0) { continue }
        if (($r[0] + $r[2]) -gt $vw -or ($r[1] + $r[3]) -gt $vh) { continue }
        if ($r[2] -le 200 -or $r[3] -le 150) { continue }
        $pick = $it
        break
    }

    $handle = if ($pick) { $pick.h } else { $items[0].h }
    $title = if ($pick) { $pick.title } else { $items[0].title }
    $rect = if ($pick) { $pick.rect } else { $items[0].rect }
    Write-Output ("取样窗口: " + $title + "  handle=" + $handle + "  rect=" + ($rect -join ','))

    # ② frame：DWM 可见边界 vs GetWindowRect
    $f = Invoke-Op ('{"op":"frame","handle":' + $handle + '}')
    Assert (-not $f.data.error) 'frame 正常返回'
    if (-not $f.data.error) {
        Write-Output ("  frame=[" + ($f.data.frame -join ',') + "]  window=[" + ($f.data.window -join ',') + "]")
        Assert ($f.data.frame.Count -eq 4 -and $f.data.window.Count -eq 4) 'frame/window 均为 4 元素矩形'
        Assert ($f.data.frame[2] -gt 0 -and $f.data.frame[3] -gt 0) 'frame 宽高为正'
    }

    # ③ waitstable：先用两次 shot 判定取样区域本身是否在变化，再验证 waitstable 的语义
    $rx = [int]$rect[0]; $ry = [int]$rect[1]
    $rw = [int][Math]::Min(500, $rect[2]); $rh = [int][Math]::Min(360, $rect[3])
    $region = '' + $rx + ',' + $ry + ',' + $rw + ',' + $rh
    $args1 = '{"op":"shot","x":' + $rx + ',"y":' + $ry + ',"w":' + $rw + ',"h":' + $rh +
             ',"maxDim":480,"format":"png","path":"' + $stem + '-a.png"}'
    $args2 = '{"op":"shot","x":' + $rx + ',"y":' + $ry + ',"w":' + $rw + ',"h":' + $rh +
             ',"maxDim":480,"format":"png","path":"' + $stem + '-b.png"}'
    $sa = Invoke-Op $args1
    Start-Sleep -Milliseconds 150
    $sb = Invoke-Op $args2
    $ha = Get-FileHashOf $sa.data.path
    $hb = Get-FileHashOf $sb.data.path
    $regionStatic = ($ha -ne '' -and $ha -eq $hb)
    Write-Output ("  取样区 " + $rx + "," + $ry + " " + $rw + "x" + $rh +
        " 两次截图一致=" + $regionStatic)

    $st = Invoke-Op ('{"op":"waitstable","x":' + $rx + ',"y":' + $ry + ',"w":' + $rw +
        ',"h":' + $rh + ',"timeout":2000,"settle":60}')
    Write-Output ("  waitstable: stable=" + $st.data.stable + " elapsedMs=" + $st.data.elapsedMs +
        " checks=" + $st.data.checks)
    Assert ($null -ne $st.data.stable) 'waitstable 结构完整'
    # 区域在取样期间没变时，waitstable 必须判为稳定（否则说明实现有问题）
    if ($regionStatic) { Assert ([bool]$st.data.stable) '静止区域被判为稳定' }
    else { Write-Output "  （取样区域本身在变化，此项不做强断言）" }

    # ④ zoom：围绕窗口中心放大截图，并校验坐标换算元数据自洽
    $cx = [int]($rect[0] + $rect[2] / 2)
    $cy = [int]($rect[1] + $rect[3] / 2)
    $z = Invoke-Op ('{"op":"zoom","x":' + $cx + ',"y":' + $cy +
        ',"radius":160,"maxDim":900,"path":"' + $stem + '-zoom.jpg"}')
    Assert (-not $z.data.error) 'zoom 正常返回'
    if (-not $z.data.error) {
        Write-Output ("  zoom: " + $z.data.width + "x" + $z.data.height + " src=" + $z.data.srcW +
            "x" + $z.data.srcH + " scale=" + $z.data.scale + " bytes=" + $z.data.bytes +
            " srcXY=" + $z.data.srcX + "," + $z.data.srcY)
        Assert (Test-Path $z.data.path) 'zoom 落盘成功'
        # 裁剪区必须夹在虚拟桌面内
        Assert ($z.data.srcX -ge 0 -and $z.data.srcY -ge 0) 'zoom 原点非负（已夹取）'
        Assert ($z.data.srcW -le (2 * 160) -and $z.data.srcH -le (2 * 160)) 'zoom 裁剪不超过 2×radius'
        # 窄裁剪应当在放大后更清晰：scale 必须 >= 全屏缩放比（约 1600/2560）
        Assert ($z.data.scale -ge 0.99) 'zoom 未发生降采样（长边 320 < maxDim 900）'
    }

    # ⑤ findtext 复用图片：先在一张全屏截图里找一个真实存在的文字，再断言「复用路径 ≡ 自截图路径」。
    #    这是坐标换算闭环的强验证——两条路径看同一张画面必须给出同一个点。
    #    注：文字没搜到时内核按既有约定只返回 found/hint/visible，不返回 count/imageSize，
    #    所以断言必须放在 found=true 的分支里（踩过：对空字段取下标会直接抛错）。
    $full = Invoke-Op ('{"op":"shot","maxDim":1600,"format":"png","path":"' + $stem + '-full.png"}')
    Assert (-not $full.data.error) '全屏截图成功'
    $fullJson = $full.data.path.Replace('/', '\').Replace('\', '\\')   # 生产端 Python 的写法

    $picked = $null
    $reused = $null
    foreach ($q in @('微信', 'e', 'a', '的', '设置', '文件')) {
        $probe = Invoke-Op ('{"op":"findtext","text":"' + $q + '","image":"' + $fullJson +
            '","scale":' + $full.data.scale + ',"srcX":' + $full.data.srcX + ',"srcY":' + $full.data.srcY +
            ',"maxDim":1600,"maxCandidates":3}')
        Assert (-not $probe.data.error) ('findtext 复用图片（"' + $q + '"）无错误')
        if (-not $probe.data.error -and $probe.data.found) { $picked = $q; $reused = $probe; break }
    }

    if ($null -eq $picked) {
        Write-Output '  （当前屏幕上找不到可用于验证的文字，复用路径闭环比对跳过）'
    } else {
        Write-Output ('  验证文字: "' + $picked + '" → 复用路径 (' + $reused.data.x + ',' + $reused.data.y +
            ') imageSize=' + (@($reused.data.imageSize) -join 'x'))
        Write-Output ('  复用路径自描述: src=' + $reused.data.src + ' scaleUsed=' + $reused.data.scaleUsed +
            ' origin=' + (@($reused.data.origin) -join ',') + ' rawXY=' + (@($reused.data.rawXY) -join ','))
        Assert (@($reused.data.imageSize).Count -ge 2) '复用图片时能回显图片尺寸'
        $inScreen = ($reused.data.x -ge 0) -and ($reused.data.x -le $vw) -and
                    ($reused.data.y -ge 0) -and ($reused.data.y -le $vh)
        Assert $inScreen '复用路径坐标落在屏幕范围内'

        # 判定性门槛：先连跑两次「自截图」路径。若两次结果本身就差很多，说明取样窗口内
        # 画面在变（测试环境问题），此时任何与复用路径的比对都不具判定力——如实标注并跳过，
        # 避免把「屏幕在动」误报成「换算错误」。
        $f1 = Invoke-Op ('{"op":"findtext","text":"' + $picked + '","maxDim":1600,"maxCandidates":3}')
        Start-Sleep -Milliseconds 120
        $f2 = Invoke-Op ('{"op":"findtext","text":"' + $picked + '","maxDim":1600,"maxCandidates":3}')
        Assert (-not $f1.data.error) 'findtext 自截图路径可用'
        if ($f1.data.error -or -not $f1.data.found -or -not $f2.data.found) {
            Write-Output '  （自截图路径未找到同一文字，闭环比对跳过）'
        }
        else {
            $sdx = [Math]::Abs([int]$f1.data.x - [int]$f2.data.x)
            $sdy = [Math]::Abs([int]$f1.data.y - [int]$f2.data.y)
            Write-Output ('  自截图两次: (' + $f1.data.x + ',' + $f1.data.y + ') / (' +
                $f2.data.x + ',' + $f2.data.y + ')  自身Δ=' + $sdx + ',' + $sdy)
            if (($sdx -gt 20) -or ($sdy -gt 20)) {
                Write-Output '  （屏幕在变化，本次比对不具判定性，仅记录数据）'
            }
            else {
                $dx = [Math]::Abs([int]$reused.data.x - [int]$f1.data.x)
                $dy = [Math]::Abs([int]$reused.data.y - [int]$f1.data.y)
                Write-Output ('  复用 vs 自截图 Δ=' + $dx + ',' + $dy)
                Assert (($dx -le 20) -and ($dy -le 20)) '复用与自截图两条路径坐标一致（缩放换算闭环）'
            }
        }
    }

    # ⑦ 复用不存在的图片：必须给出可读错误而不是抛异常
    $bad = Invoke-Op '{"op":"findtext","text":"a","image":"Z:/nope/missing.png","scale":0.5}'
    Assert ($bad.data.error -like '*不存在*') '图片不存在时给出可读错误'

    if ($fail -eq 0) { Write-Output 'SMOKE-365: PASS' } else { Write-Output ("SMOKE-365: FAIL (" + $fail + ")") }
}
finally {
    try { Invoke-Op '{"op":"shutdown"}' | Out-Null } catch { }
    $client.Dispose()
    if (-not $proc.HasExited) { $proc.WaitForExit(3000) | Out-Null }
    if (-not $proc.HasExited) { $proc.Kill() }
    foreach ($f in @('-a.png', '-b.png', '-zoom.jpg')) {
        Remove-Item ($stem.Replace('/', '\') + $f) -ErrorAction SilentlyContinue
    }
    $err = if (Test-Path $errLog) { (Get-Content $errLog -Raw) } else { '' }
    if ($err) { Write-Output ("stderr: " + $err.Trim()) }
}
