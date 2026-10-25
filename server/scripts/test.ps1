# 分级测试入口（plan-549-2281）——解决「全量测试很久、有时一直跑不出来」。
#
# 【为什么需要这个脚本】
#   · 全量 2100+ 用例，其中相当一部分在等真实时间（sleep / 超时 / 子代理唤醒 / 工作流推进）；
#   · 一旦某个 await 永不返回，整个全量就会静默挂死（本机未装 pytest-timeout，pytest 自身不设超时）；
#   · 之前只能「全量跑」，改一行代码也要等几十分钟，实际用起来就是不敢跑、最后不跑。
#
# 【四档】fast（默认，日常）／ file（定向）／ changed（按 git 改动推导）／ full（全量，带超时守护）
#   所有档位都有硬超时，超时不会静默挂死，而是打印「卡在哪个用例」并退出。
#
# 用法（在 server/ 或仓库根均可用，脚本自己定位 server 目录）：
#   pwsh scripts/test.ps1                    # = fast
#   pwsh scripts/test.ps1 fast
#   pwsh scripts/test.ps1 file grep          # 文件名含 grep 的测试
#   pwsh scripts/test.ps1 changed            # 按 git 未提交改动推导
#   pwsh scripts/test.ps1 full
#   pwsh scripts/test.ps1 fast -TimeoutSec 600
param(
    [Parameter(Position = 0)]
    [ValidateSet('fast', 'file', 'changed', 'full')]
    [string]$Mode = 'fast',

    # file 档的匹配关键词（匹配测试文件名）
    [Parameter(Position = 1)]
    [string]$Pattern = '',

    # 硬超时（秒）
    [int]$TimeoutSec = 0,

    # 追加给 pytest 的额外参数
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'

# ── 定位 server 目录（脚本可能在 server/scripts 下）──
$ServerDir = if (Test-Path (Join-Path $PSScriptRoot '..\pyproject.toml')) {
    (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} elseif (Test-Path (Join-Path $PSScriptRoot 'pyproject.toml')) {
    (Resolve-Path $PSScriptRoot).Path
} else {
    throw "找不到 server 目录（未找到 pyproject.toml）"
}

Push-Location $ServerDir
try {
    # 未显式给超时则按档位取默认值
    if ($TimeoutSec -le 0) {
        $TimeoutSec = switch ($Mode) {
            # 实测（2026-10，排除 90 个 >8s 文件后）：fast 档约 6~7 分钟跑完 1400+ 用例。
            # 默认给 600s：既不会正常切断，又能在真卡死时于 10 分钟内收场（旧代码无任何超时，
            # 一挂就是无限等，这才是「一直执行不出来」的直接原因）。
            'fast'    { 600 }
            'file'    { 300 }
            'changed' { 600 }
            'full'    { 2400 }
        }
    }

    # ── 隔离数据库护栏：与 tests/conftest.py 同口径，避免误连真实库 ──
    # conftest 会在 import app 之前设置；这里再设一次，保证从任何目录启动都生效。
    $tmpDb = Join-Path ([System.IO.Path]::GetTempPath()) 'chatcoder_pytest.db'
    $env:DATABASE_URL = "sqlite+aiosqlite:///$($tmpDb -replace '\\', '/')"

    # ── 用真实 Python 解释器，不要用 sys.executable 语义去猜 ──
    $py = $null
    foreach ($cand in @('python', 'py')) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
    }
    if (-not $py) { throw "未找到 python/py，无法运行测试" }

    # ── 组装 pytest 参数 ──
    $targets = @('tests')
    $modeArgs = @()
    $label = ''

    switch ($Mode) {
        'fast' {
            # 实测耗时 >8s 的文件（2026-10 逐文件计时）。这些不是「写了 sleep」那么少：
            # 单文件 8~65s 的主因是**每个测试文件都要导入完整的 app 模块树**（编排+工具+网关+服务，
            # 实测 import app.persistence 就要 1.9s），叠加真实等待（子进程/浏览器/网络/超时）。
            # 快测档排除它们，保证日常改动能在几分钟内拿到反馈；这些文件留给 file / full 档。
            $slowFiles = @(
                'test_agent_loop_empty_response_retry.py'  # 18s
                'test_agent_loop_overflow_classify.py'     # 13.5s
                'test_approval.py'                         # 8.5s
                'test_archive_purge.py'                    # 13.9s
                'test_bg_notify_on_exit.py'                # 10.9s
                'test_bg_process_wait.py'                  # 10.9s
                'test_bg_task_lifecycle.py'
                'test_browser_agent.py'                    # 14.9s
                'test_browser_kit_accuracy.py'             # 48.9s（真实浏览器）
                'test_browser_kit_frames.py'               # 27.4s
                'test_builtin_mcp_database.py'             # 12s
                'test_cancellation_flow.py'                # 12.5s
                'test_codebase_search_incremental.py'      # 9.3s
                'test_commandcode_provider.py'             # 11.8s
                'test_compact_target.py'                   # 16.3s
                'test_compression_service.py'              # 15.3s
                'test_confirm_plan_approval.py'            # 15s
                'test_context_summary_persistence.py'      # 13s
                'test_credential_cooldown_policy.py'        # 9.1s
                'test_credential_rotation.py'              # 16.3s
                'test_database_concurrency.py'             # 11.1s
                'test_degenerate_tool_calls.py'            # 14s
                'test_desktop_accuracy.py'
                'test_desktop_control.py'                  # 16.3s
                'test_desktop_targeting.py'                # 19.4s
                'test_edit_gate.py'                        # 8.3s
                'test_editor_replace_all.py'               # 9.3s
                'test_estimator_calibration.py'            # 8.5s
                'test_executor.py'                         # 17.1s
                'test_extension_injection.py'              # 13.6s
                'test_feature_map.py'                      # 13.3s
                'test_file_reviews.py'                     # 9.1s
                'test_fs_read_window.py'                   # 10.8s
                'test_fs_tools.py'                         # 9.7s
                'test_git_conflict.py'                     # 23s
                'test_git_diff_stat.py'                    # 13.1s
                'test_git_history.py'                      # 12.4s
                'test_git_merge_engine.py'                 # 17s
                'test_git_multirepo.py'                    # 65.4s（最慢）
                'test_git_safety.py'                       # 14.3s
                'test_goal_mode.py'                        # 24.6s
                'test_grep_paging.py'                      # 10.5s
                'test_grok_dedup.py'                       # 9.6s
                'test_inline_thinking.py'                  # 11.6s
                'test_mailbox_interrupt.py'                # 12.9s
                'test_mcp_fetch_signature.py'              # 11.2s
                'test_memory_opt.py'                       # 10.5s
                'test_memory_scope.py'                     # 15.7s
                'test_memory_search_rank.py'               # 17.7s
                'test_memory_service_opt.py'               # 12.9s
                'test_model_service.py'                    # 8.6s
                'test_operator_boost.py'                   # 13.3s
                'test_output_paging.py'                    # 10.1s
                'test_packaged_diagnostics.py'             # 15.5s
                'test_plan_91_394_subagent_resume.py'
                'test_plan_1075_inherit.py'                # 11.6s
                'test_plan_1085_binary.py'                 # 11.1s
                'test_plan_166_767.py'                     # 9.7s
                'test_plan_20261013_wf_order.py'           # 13.1s
                'test_plan_282_1441_fixes.py'              # 17.2s
                'test_plan_330_1648_subagent.py'           # 15.9s
                'test_plan_41_229.py'                      # 15.7s
                'test_plan_546_547.py'                     # 9.8s
                'test_plan_644_lifecycle.py'               # 12.6s
                'test_plan_doc_turn_written.py'            # 12.4s
                'test_plan_rollback.py'                    # 11.5s
                'test_plan_turn_failure.py'                # 10.6s
                'test_plugin_service.py'                   # 10.2s
                'test_prompt_and_output_methodology.py'    # 14.4s
                'test_provider_client_lifecycle.py'
                'test_provider_credential_purge.py'        # 8.4s
                'test_provider_thinking.py'                # 21.4s
                'test_reply_language_resolution.py'        # 13.2s
                'test_reply_language.py'                   # 16.4s
                'test_revert_turn_files.py'                # 16.4s
                'test_review.py'                           # 14.1s
                'test_ripgrep_search.py'                   # 10s
                'test_rollback_attachments.py'              # 13.8s
                'test_rollback_binary.py'                  # 12.5s
                'test_symbol_index_manager.py'
                'test_ta3_session_isolation.py'
                'test_ta3_stream_handling.py'
                'test_terminal_web.py'
                'test_workflow_lifecycle.py'
                'test_workflow_toolcalls.py'
                'test_workflow_wakeup_events.py'
            )
            $ignoreArgs = @()
            foreach ($f in $slowFiles) { $ignoreArgs += @('--ignore', "tests/$f") }
            $modeArgs = $ignoreArgs
            $label = "快测（排除 $($slowFiles.Count) 个实测 >8s 的文件）"
        }
        'file' {
            if (-not $Pattern) {
                throw "file 档需要给出文件名关键词，例如：pwsh scripts/test.ps1 file grep"
            }
            $matched = Get-ChildItem 'tests' -Filter '*.py' -Recurse |
                Where-Object { $_.Name -like "*$Pattern*" } |
                ForEach-Object { 'tests/' + $_.Name }
            if (-not $matched -or $matched.Count -eq 0) {
                throw "没有匹配 '$Pattern' 的测试文件"
            }
            $targets = $matched
            $label = "定向：$Pattern（$($matched.Count) 个文件）"
        }
        'changed' {
            # 按 git 未提交改动推导相关测试。启发式：取改动文件的模块名，匹配同名/相邻测试文件。
            # 做不到精确依赖分析（那需要导入图），所以匹配到就多跑、匹配不到就退化为快测并提示。
            $changedFiles = @()
            try {
                $changedFiles = (git -C .. diff --name-only 2>$null) + (git -C .. diff --name-only --cached 2>$null)
                $changedFiles = $changedFiles | Where-Object { $_ } | Sort-Object -Unique
            } catch { }

            $serverChanged = $changedFiles | Where-Object { $_ -like 'server/*' }
            $clientChanged = $changedFiles | Where-Object { $_ -like 'client/*' }

            if (-not $serverChanged -and -not $clientChanged) {
                Write-Host "[changed] 未检测到未提交改动，退化为快测" -ForegroundColor Yellow
                $Mode = 'fast'
                $modeArgs = @()
                foreach ($f in @('test_bg_task_lifecycle.py', 'test_desktop_accuracy.py',
                                 'test_plan_91_394_subagent_resume.py', 'test_provider_client_lifecycle.py',
                                 'test_symbol_index_manager.py', 'test_ta3_session_isolation.py',
                                 'test_ta3_stream_handling.py', 'test_terminal_web.py',
                                 'test_workflow_lifecycle.py', 'test_workflow_toolcalls.py',
                                 'test_workflow_wakeup_events.py')) {
                    $modeArgs += @('--ignore', "tests/$f")
                }
                $label = '快测（无改动，退化）'
            } else {
                $keys = @()
                foreach ($f in $serverChanged) {
                    $base = [System.IO.Path]::GetFileNameWithoutExtension($f)
                    if ($base -and $base -ne '__init__' -and $base -ne 'conftest') { $keys += $base }
                }
                $keys = $keys | Sort-Object -Unique

                $matched = @()
                foreach ($k in $keys) {
                    $hit = Get-ChildItem 'tests' -Filter '*.py' -Recurse |
                        Where-Object { $_.Name -like "*$k*" } |
                        ForEach-Object { 'tests/' + $_.Name }
                    $matched += $hit
                }
                $matched = $matched | Sort-Object -Unique

                if ($matched.Count -eq 0) {
                    Write-Host "[changed] 改动模块未匹配到测试文件（改动: $($keys -join ', ')），退化为快测" -ForegroundColor Yellow
                    $Mode = 'fast'
                    foreach ($f in @('test_bg_task_lifecycle.py', 'test_desktop_accuracy.py',
                                     'test_plan_91_394_subagent_resume.py', 'test_provider_client_lifecycle.py',
                                     'test_symbol_index_manager.py', 'test_ta3_session_isolation.py',
                                     'test_ta3_stream_handling.py', 'test_terminal_web.py',
                                     'test_workflow_lifecycle.py', 'test_workflow_toolcalls.py',
                                     'test_workflow_wakeup_events.py')) {
                        $modeArgs += @('--ignore', "tests/$f")
                    }
                    $label = '快测（无匹配，退化）'
                } else {
                    $targets = $matched
                    $label = "改动相关（$($matched.Count) 个文件；启发式匹配，可能漏跑）"
                    Write-Host "[changed] 改动模块: $($keys -join ', ')" -ForegroundColor Cyan
                }
                if ($clientChanged) {
                    Write-Host "[changed] 注意：client 有改动（$($clientChanged.Count) 个文件），请另跑 client 的 npm run typecheck" -ForegroundColor Yellow
                }
            }
        }
        'full' {
            $label = '全量'
        }
    }

    Write-Host ""
    Write-Host "== 测试档位: $Mode — $label ==" -ForegroundColor Cyan
    Write-Host "== 超时: ${TimeoutSec}s | 目录: $ServerDir ==" -ForegroundColor Cyan
    Write-Host ""

    $pytestArgs = @('-m', 'pytest', '-q', '--durations=15') + $modeArgs + $targets + $ExtraArgs
    # Start-Process 的 -ArgumentList 是单个字符串：数组会被拼成逗号分隔的假参数（实测
    # `--co,-q` 直接报 file not found）。这里逐个参数加引号后再拼，保证空格与特殊字符安全。
    $argLine = ($pytestArgs | ForEach-Object {
        if ($_ -match '\s') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '

    # ── 带超时守护执行：超时则 kill 子进程并给出定位提示，绝不静默挂死 ──
    $started = Get-Date
    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    $proc = Start-Process -FilePath $py -ArgumentList $argLine `
        -NoNewWindow -PassThru -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr

    $exited = $proc.WaitForExit($TimeoutSec * 1000)
    $elapsed = ((Get-Date) - $started).TotalSeconds

    if (-not $exited) {
        # 超时：打印已产生的输出，让用户看到最后跑到哪个用例
        try { $proc.Kill($true) } catch { }
        Write-Host ""
        Write-Host "== 超时：超过 ${TimeoutSec}s 未结束，已终止 ==" -ForegroundColor Red
        Write-Host "最后 40 行输出（通常能看出卡在哪个用例）：" -ForegroundColor Yellow
        Get-Content $tmpOut -Tail 40 -ErrorAction SilentlyContinue
        Get-Content $tmpErr -Tail 40 -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Host "定位建议：" -ForegroundColor Yellow
        Write-Host "  1) 用 file 档单独跑最后出现的那个测试文件，确认是单例挂死还是批量问题"
        Write-Host "  2) 该文件若在等真实时间（sleep / 超时 / 唤醒），属已知慢测，用 fast 档排除"
        Write-Host "  3) 真挂死在 await 上：用 python -X faulthandler -m pytest <文件> -x 复现取堆栈"
        Remove-Item $tmpOut, $tmpErr -ErrorAction SilentlyContinue
        exit 124
    }

    $stdout = Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue
    $stderr = Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue
    if ($stdout) { Write-Host $stdout }
    if ($stderr) { Write-Host $stderr }

    $code = $proc.ExitCode
    Write-Host ""
    $color = if ($code -eq 0) { 'Green' } else { 'Red' }
    Write-Host ("== 结束：退出码 {0} | 耗时 {1:N1}s ==" -f $code, $elapsed) -ForegroundColor $color

    # 失败时单独摘出失败用例清单，省去在长输出里翻
    if ($code -ne 0 -and $stdout) {
        $failLines = ($stdout -split "`r?`n") | Where-Object { $_ -match '^(FAILED|ERROR)\s' }
        if ($failLines) {
            Write-Host ""
            Write-Host "失败用例清单：" -ForegroundColor Red
            $failLines | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        }
    }

    Remove-Item $tmpOut, $tmpErr -ErrorAction SilentlyContinue
    exit $code
}
finally {
    Pop-Location
}
