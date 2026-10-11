# plan-114-450：把 electron/build/icon.svg 渲染成各尺寸 PNG（用本机 Chrome headless 截图，不引入新依赖）
# 用法：powershell -File tools/render-icon.ps1
# Chrome 会往 stderr 写无关警告，不能用 Stop，否则第一张就中断
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { $chrome = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" }
$svg = Get-Content -Raw (Join-Path $root "electron/build/icon.svg")
$tmp = Join-Path $env:TEMP "cc-icon-render"
New-Item -ItemType Directory -Force $tmp | Out-Null

$targets = @(
  @{ size = 512; out = "electron/build/icon-512.png" },
  @{ size = 512; out = "electron/build/icon.png" },
  @{ size = 256; out = "electron/build/icon-256.png" }
)
foreach ($t in $targets) {
  $s = $t.size
  # 透明背景：页面与 body 均透明，svg 撑满视口
  $html = "<!doctype html><html><head><style>html,body{margin:0;background:transparent}svg{display:block;width:${s}px;height:${s}px}</style></head><body>$svg</body></html>"
  $page = Join-Path $tmp "icon-$s.html"
  Set-Content -Path $page -Value $html -Encoding UTF8
  $png = Join-Path $root $t.out
  $args = @("--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
    "--default-background-color=00000000", "--user-data-dir=$tmp\prof", "--window-size=$s,$s",
    "--screenshot=$png", "file:///$($page -replace '\\','/')")
  Start-Process -FilePath $chrome -ArgumentList $args -Wait -WindowStyle Hidden
  Write-Output "$($t.out) <- ${s}px"
}
Remove-Item -Recurse -Force $tmp
