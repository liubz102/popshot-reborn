<#
    probe-death.ps1 —— 「死了不复活」现场取证（bug调查/8）

    ★ **存档件，已不进发布包**。根因在会话 32 查实并**在服务端修掉了**
      （§212 队伍号越界踩战绩 / §213 同步局号分叉），玩家不再需要跑它。
      留在 `tools\` 是因为它仍是**唯一**能从活进程里读出客户端侧
      死亡/重生判据的东西 —— 真要再看现场，就从 `tools\` 里双击
      `probe-death.bat`（三个文件得挨在一起）。

    被 probe-death.bat 调用（双击那个）。做的事：

      1. 自己找 BigShot.exe 的 pid（不用手输；开了两个客户端就每个各采一份）
      2. 给几秒钟让人切回游戏窗口 —— ★ 游戏不在前台时它的主循环基本不跑，
         探针读到的值会「冻住」，那种数据没有意义（probe_death.py 的开头
         就写着这条）
      3. 跑 tools\probe_death.py 盯一段时间，把每一次状态变化写进
         logs\probe_death_<时间>_pid<pid>.log
      4. 把日志路径打出来，让人直接发回来

    ★ 所有中文提示都在这里打，probe-death.bat 保持纯 ASCII —— chcp 65001 下
      cmd 会因为多字节字符把后面的命令行拦腰截断（FINDINGS §135 / D074）。
#>
[CmdletBinding()]
param(
    # 盯多久（秒）。默认 90 秒：客户端的重生倒计时是 5 秒，卡住的话
    # 90 秒足够看清「倒计时到了却没发包」还是「倒计时压根是 -1」。
    [double]$Seconds = 90,
    # 采样间隔（秒）。只有值变了才会往日志里写一行，调密一点不会刷屏。
    [double]$Interval = 0.25,
    # 切回游戏的缓冲时间（秒）。给 0 就立刻开始采。
    [double]$Countdown = 6
)

$ErrorActionPreference = 'Stop'

trap {
    Write-Host ''
    Write-Host "[取证失败] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ''
    exit 1
}

$Root   = Split-Path -Parent $PSScriptRoot
$Probe  = Join-Path $PSScriptRoot 'probe_death.py'
$LogDir = Join-Path $Root 'logs'

Write-Host ''
Write-Host '=== 炮炮火枪手 · 「死了不复活」现场取证 ===' -ForegroundColor Cyan
Write-Host ''

# --- 1. 找 Python ----------------------------------------------------------
# 发布包里自带 runtime\python；仓库里开发时也走这一份。都没有才退到 PATH。
$Python = Join-Path $Root 'runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $onPath) {
        throw "找不到 Python：$Python 不存在，PATH 里也没有 python.exe"
    }
    $Python = $onPath.Source
}
if (-not (Test-Path -LiteralPath $Probe -PathType Leaf)) {
    throw "找不到探针脚本：$Probe"
}

# --- 2. 找游戏进程 ---------------------------------------------------------
# 单实例互斥体（V0.1 §9）保证正常情况下只有一个，但开发机上会开好几个，
# 所以这里不挑，找到几个就采几份。
$targets = @(Get-Process -Name 'BigShot' -ErrorAction SilentlyContinue |
             Sort-Object -Property Id)
if ($targets.Count -eq 0) {
    Write-Host '游戏没在跑（找不到 BigShot.exe）。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '这个脚本要在**卡住的那一刻**用：' -ForegroundColor Yellow
    Write-Host '  1) 保持游戏开着、人还躺在地上没复活；'
    Write-Host '  2) 切出来双击 probe-death.bat；'
    Write-Host '  3) 看到提示后马上切回游戏，别动，等它采完。'
    Write-Host ''
    exit 2
}

Write-Host ("找到 {0} 个游戏进程：{1}" -f $targets.Count,
            (($targets | ForEach-Object { "pid=$($_.Id)" }) -join '  '))
Write-Host ("采样时长 {0} 秒，间隔 {1} 秒" -f $Seconds, $Interval)
Write-Host ''

# --- 3. 提示 + 倒计时 ------------------------------------------------------
Write-Host '★ 现在请立刻切回游戏窗口，并让游戏一直停在最前面。' -ForegroundColor Yellow
Write-Host '  游戏切到后台时它的主循环几乎不跑，探针读到的数值会冻住，'
Write-Host '  那样采出来的日志是没用的。采样期间不用操作，放着就行。'
Write-Host ''
for ($i = [int][Math]::Ceiling($Countdown); $i -gt 0; $i--) {
    Write-Host ("  {0} 秒后开始采样…" -f $i)
    Start-Sleep -Seconds 1
}
Write-Host ''

# --- 4. 逐个进程采样 -------------------------------------------------------
if (-not (Test-Path -LiteralPath $LogDir -PathType Container)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logs  = @()

foreach ($proc in $targets) {
    $log = Join-Path $LogDir ("probe_death_{0}_pid{1}.log" -f $stamp, $proc.Id)
    $logs += $log

    # 现场信息先写进日志头：光有内存快照、没有「哪个包、哪一版」不好对账。
    $header = @()
    $header += "==== 炮炮火枪手 死亡/重生 现场取证 ===="
    $header += "时间      : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff')"
    $header += "pid       : $($proc.Id)"
    try   { $header += "exe       : $($proc.Path)" }
    catch { $header += "exe       : <读不到，可能需要管理员权限>" }
    $header += "启动于    : $($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))"
    $header += "采样      : $Seconds 秒 / 每 $Interval 秒一次（只在数值变化时记一行）"
    # 只摘**有效行**：server.config 里 90% 是注释，全抄进来会把日志淹了。
    foreach ($extra in @('BUILD.ver', 'server-ClientFilter.config', 'server.config')) {
        $path = Join-Path $Root $extra
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $header += "--- $extra ---"
            $header += (Get-Content -LiteralPath $path -Encoding UTF8 |
                        Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') } |
                        Select-Object -First 12)
        }
    }
    $modeFile = Join-Path $LogDir '.server_mode'
    if (Test-Path -LiteralPath $modeFile -PathType Leaf) {
        $header += "服务端模式: $((Get-Content -LiteralPath $modeFile -Raw).Trim())"
    }
    $header += "========================================"
    $header += ''
    Set-Content -LiteralPath $log -Value $header -Encoding UTF8

    Write-Host ("[pid $($proc.Id)] 采样中…（{0} 秒）" -f $Seconds) -NoNewline

    # ★ 让 Python 自己把 UTF-8 写进文件：重定向之后它默认按系统代码页编码，
    #   而探针的输出里有 ✗ ★ 这些 cp936 装不下的字符，不设就当场 UnicodeEncodeError。
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8       = '1'
    # ★ 最外面那一对引号是 `cmd /c` 的老规矩：命令行里同时有「带空格的路径」
    #   和重定向时，不整个再包一层，cmd 会把路径拆断（报「文件名、目录名或
    #   卷标语法不正确」）。
    $cmdline = '/c ""{0}" "{1}" {2} {3} {4} >> "{5}" 2>&1"' -f `
               $Python, $Probe, $proc.Id, $Seconds, $Interval, $log
    $run = Start-Process -FilePath $env:ComSpec -ArgumentList $cmdline `
                         -NoNewWindow -PassThru
    # ★ 摸一下 .Handle：`Start-Process -PassThru` 回来的对象不留句柄，
    #   不摸的话进程结束后 `.ExitCode` 永远是 $null，「$null -ne 0」恒真，
    #   每次都误报失败。
    $null = $run.Handle
    while (-not $run.HasExited) {
        Start-Sleep -Milliseconds 2000
        Write-Host '.' -NoNewline
    }
    $run.WaitForExit()
    Write-Host ''
    $text = Get-Content -LiteralPath $log -Encoding UTF8 -Raw
    if ($run.ExitCode -ne 0 -or $text -match 'OpenProcess 失败') {
        Write-Host ("[pid $($proc.Id)] 读不到游戏内存（探针退出码 $($run.ExitCode)）。" +
                    "请右键 probe-death.bat -> 以管理员身份运行，再来一次。"
                   ) -ForegroundColor Yellow
    }
}

# --- 5. 收尾：把结论行摘出来给人看一眼 -------------------------------------
Write-Host ''
Write-Host '=== 采完了 ===' -ForegroundColor Cyan
foreach ($log in $logs) {
    Write-Host ''
    Write-Host "日志：$log" -ForegroundColor Green
    # probe_death.py 的判据行都以这几个记号开头，摘出来当速览。
    $hits = @(Get-Content -LiteralPath $log -Encoding UTF8 |
              Where-Object { $_ -match '^\s{2,}(★|✗|\?) ' } |
              Select-Object -Last 12)
    if ($hits.Count -gt 0) {
        Write-Host '  最后几条判据：' -ForegroundColor DarkGray
        foreach ($line in $hits) { Write-Host "  $line" }
    }
}
Write-Host ''
Write-Host '把上面这个/这些 .log 文件发回来就行。' -ForegroundColor Yellow
Write-Host '（如果采样时游戏不在最前面，数值会是冻住的，那份不算数，重来一次）'
Write-Host ''
exit 0
