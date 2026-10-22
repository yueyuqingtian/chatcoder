# chatcoder 一键打包脚本(Windows；本地与 GitHub Actions 共用同一份)
# 产物目录取 package.json 的 build.directories.output（下文 $outDir），不再在脚本里写死。
# 用法:
#   powershell -ExecutionPolicy Bypass -File build-release.ps1            本地打包
#   powershell -ExecutionPolicy Bypass -File build-release.ps1 -Publish   本地打包并发布到 GitHub Releases（需 gh 已登录）
#   pwsh -File build-release.ps1 -CI                                      GitHub Actions：跳过「部署到本地运行目录」，发布由工作流负责
# 正式发布推荐走 tag：git tag v<version> && git push origin v<version>，由 .github/workflows/release.yml 自动打包发布。
param(
    [switch]$Publish,
    [switch]$CI
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pkg = Get-Content "$root\package.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$version = $pkg.version
$outDir = Join-Path $root $pkg.build.directories.output
$exeName = "chatcoder-Setup-$version.exe"
Write-Host ("版本 v{0}，产物目录 {1}" -f $version, $outDir) -ForegroundColor Cyan

Write-Host "=== [1/7] 内置能力预检（Chromium / 桌面操控内核）===" -ForegroundColor Cyan
# 幂等：已准备好则跳过下载；浏览器由 chatcoder-server.spec 打进 _internal/ms-playwright
& "$root\server\prepare-playwright-browsers.ps1"

# 桌面操控内核：用系统自带 csc.exe 编译，产物落到 server/vendor/desktop-core，
# 由 chatcoder-server.spec 打进 _internal/desktop-core。
& "$root\tools\desktop-core\build.ps1" -OutDir "$root\server\vendor\desktop-core"

Write-Host "=== [2/7] 构建前端 ===" -ForegroundColor Cyan
Push-Location "$root\client"
npm run build
if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
Pop-Location

Write-Host "=== [3/7] 打包后端(PyInstaller, --clean 全量重建) ===" -ForegroundColor Cyan
Push-Location "$root\server"
$pyi = ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) { throw "未找到 PyInstaller,请先 .venv\Scripts\pip install -r requirements-build.txt" }
# --clean 必须保留：增量构建在模块列表不变时不会重建 PYZ，会把旧字节码打进 exe
& $pyi chatcoder-server.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "后端打包失败" }
& $pyi chatcoder-index-worker.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "索引 worker 打包失败" }
Pop-Location

# 产物守门：dist 里必须存在刚生成的主后端与索引 worker
$serverExe = "$root\server\dist\chatcoder-server\chatcoder-server.exe"
$workerExe = "$root\server\dist\chatcoder-index-worker\chatcoder-index-worker.exe"
if (-not (Test-Path $serverExe)) { throw "打包产物缺失: $serverExe" }
if (-not (Test-Path $workerExe)) { throw "索引 worker 产物缺失: $workerExe" }
$serverExeItem = Get-Item $serverExe
Write-Host ("后端产物: {0} ({1:N1} MB, {2})" -f $serverExe, ($serverExeItem.Length/1MB), $serverExeItem.LastWriteTime) -ForegroundColor Yellow

# 产物守门：内置 Chromium 必须随包分发，否则用户端浏览器工具会报"未安装"
$bundledRoot = "$root\server\dist\chatcoder-server\_internal\ms-playwright"
$bundled = Get-ChildItem $bundledRoot -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "INSTALLATION_COMPLETE") } |
    Select-Object -First 1
if (-not $bundled) { throw "后端产物缺少内置 Chromium（$bundledRoot），用户端浏览器工具将不可用" }
if (-not (Test-Path (Join-Path $bundled.FullName "chrome-win64\chrome.exe"))) {
    throw "内置 Chromium 缺少 chrome.exe: $($bundled.FullName)"
}
Write-Host ("内置浏览器: {0}" -f $bundled.Name) -ForegroundColor Yellow

# 产物守门：桌面操控内核必须随包分发，否则用户端「电脑操控」会报"内核未找到"
$coreExe = "$root\server\dist\chatcoder-server\_internal\desktop-core\desktop-core.exe"
if (-not (Test-Path $coreExe)) {
    throw "后端产物缺少桌面操控内核（$coreExe），用户端电脑操控将不可用"
}
Write-Host ("桌面操控内核: {0} ({1:N0} KB)" -f (Split-Path $coreExe -Leaf), ((Get-Item $coreExe).Length/1KB)) -ForegroundColor Yellow

if ($CI) {
    Write-Host "=== [4/7] 部署后端到本地运行目录（CI 跳过）===" -ForegroundColor DarkGray
} else {
    Write-Host "=== [4/7] 部署后端到本地运行目录 ===" -ForegroundColor Cyan
    # 同步产物到目标目录，不重启任何正在运行的进程
    & "$root\deploy-server.ps1" -TargetDir (Join-Path $outDir "win-unpacked\resources\server\chatcoder-server")
}

Write-Host "=== [5/7] 生成更新说明（CHANGELOG 当前版本区块）===" -ForegroundColor Cyan
# 更新说明与 CHANGELOG 同源：同一份文本既是 GitHub Release 正文，也由 electron-builder 写进 latest.yml 的
# releaseNotes——三条更新线路（自建源 / 镜像 / GitHub）的客户端都能直接看到「更新了什么」。
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$notesFile = Join-Path $outDir "release-notes.md"
$changelog = Get-Content "$root\CHANGELOG.md" -Raw -Encoding UTF8
$notesText = ""
if ($changelog -match "(?ms)^## v$([regex]::Escape($version))\s*\r?\n(.*?)(?=^## |\z)") {
    $notesText = $matches[1].Trim()
}
if (-not $notesText) {
    if ($CI -or $Publish) { throw "CHANGELOG.md 缺少 ## v$version 区块，发布前请先写好本版本的更新内容" }
    $notesText = "ChatCoder v$version"
    Write-Host "CHANGELOG.md 没有 v$version 区块，更新说明暂用版本号占位（本地打包不阻塞）" -ForegroundColor Yellow
}
[IO.File]::WriteAllText($notesFile, ($notesText -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
Write-Host ("更新说明: {0}（{1} 字）" -f $notesFile, $notesText.Length) -ForegroundColor Yellow

Write-Host "=== [6/7] 打包桌面应用(electron-builder) ===" -ForegroundColor Cyan
# --publish never：上传统一由下方 -Publish 或 GitHub Actions 负责，避免 electron-builder 隐式发布
& npx electron-builder --win --publish never "--config.releaseInfo.releaseNotesFile=$notesFile"
if ($LASTEXITCODE -ne 0) { throw "electron-builder 打包失败" }

Write-Host "=== [7/7] 安装包自检 ===" -ForegroundColor Cyan
& node "$root\tools\verify-package.cjs" (Join-Path $outDir "win-unpacked")
if ($LASTEXITCODE -ne 0) { throw "安装包自检未通过" }
$assets = @(
    (Join-Path $outDir $exeName),
    (Join-Path $outDir "$exeName.blockmap"),
    (Join-Path $outDir "latest.yml")
)
foreach ($a in $assets) {
    if (-not (Test-Path $a)) { throw "发布三件套缺失: $a" }
}
$ymlPath = Join-Path $outDir "latest.yml"
$yml = Get-Content $ymlPath -Raw -Encoding UTF8
if ($yml -notmatch "(?m)^version:\s*$([regex]::Escape($version))\s*$") { throw "latest.yml 的版本号与 package.json（$version）不一致" }
if ($yml -notmatch "(?m)^releaseNotes:") {
    # 兜底：electron-builder 没写入时自己追加块标量（顶层键，放在文件末尾即可）
    $lines = ($notesText -replace "`r`n", "`n").Split("`n") | ForEach-Object { ("  " + $_).TrimEnd() }
    $block = "releaseNotes: |`n" + ($lines -join "`n")
    [IO.File]::WriteAllText($ymlPath, ($yml.TrimEnd() + "`n" + $block + "`n"), (New-Object Text.UTF8Encoding $false))
    Write-Host "latest.yml 未带更新说明，已补写 releaseNotes" -ForegroundColor Yellow
}
foreach ($a in $assets) {
    Write-Host ("产物: {0} ({1:N1} MB)" -f (Split-Path $a -Leaf), ((Get-Item $a).Length/1MB)) -ForegroundColor Yellow
}

# ── 本地发布到 GitHub Releases（自动更新源：latest.yml + exe + blockmap）──
# 正式发布推荐推送 tag 交给 GitHub Actions；此处保留本地发布能力，便于 Actions 不可用时兜底。
if ($Publish) {
    $tag = "v$version"
    Write-Host "=== 发布 $tag 到 GitHub Releases ===" -ForegroundColor Cyan
    # 先建草稿、传完三件套再公开：公开的瞬间资产就是齐的，
    # 自建源的定时同步不会抓到「有 release 没安装包」的半成品。
    gh release create $tag @assets --title $tag --notes-file $notesFile --draft
    if ($LASTEXITCODE -ne 0) { throw "gh release create 失败" }
    gh release edit $tag --draft=false --latest
    if ($LASTEXITCODE -ne 0) { throw "gh release edit（公开草稿）失败" }
    Write-Host "发布完成: https://github.com/yueyuqingtian/chatcoder/releases/tag/$tag" -ForegroundColor Green

    # 自建更新源已改为服务器定时从 GitHub 拉取（tools/server-sync-updates.sh，见 docs/release.md），
    # 这里只是立即触发一次，省去最多 5 分钟的等待；失败不影响已完成的发布。
    Write-Host "=== 触发自建更新源同步（服务器直接从 GitHub 拉取）===" -ForegroundColor Cyan
    python "$root\tools\sync-updates.py" --version $version
    if ($LASTEXITCODE -ne 0) {
        Write-Host "立即同步失败：服务器的定时同步仍会在 5 分钟内从 GitHub 拉取新版本" -ForegroundColor Yellow
    } else {
        Write-Host "更新源就绪: https://service.guyueyu.asia/updates/latest.yml" -ForegroundColor Green
    }
}
