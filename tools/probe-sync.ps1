<#
    probe-sync.ps1 —— 「我打不死人」现场取证（bug调查/9 / FINDINGS §216）

    ★★ **存档件**：bug调查/9 已经从根上修掉了（§218 / D137 / D138：局号改成
    服务端权威的换代号），所以 **start-debug.bat 不再自动挂它**。
    留着是因为「每座位收包队列」这一侧只有它看得见 —— 以后真要再看，
    手动双击 tools\probe-sync.bat，给**已经在跑**的游戏挂上。
    （`-WaitForGame` / `-Background` 两个开关也留着，以后要重新自动挂就用它们。）

    和 probe-death 那一对的区别：**它要全程开着跑很多局**，不是卡住那一刻才采
    —— 要看的就是「第一局好好的，换到第二局之后某个座位的收包队列变成什么样」。

    做的事：

      1. 自己找 BigShot.exe（`--wait-game` 时游戏还没起来就等着）
      2. 一直采到游戏进程退出（`-Seconds 0` = 不限时，默认就是它）
      3. 只在**状态变化**时记一行；无变化也每 `-Heartbeat` 秒记一行
      4. 换局单独拉横幅、判据命中打 ★，日志超 `-MaxMB` 自动滚动
      5. 全部写进 logs\probe_sync_<时间>.log

    ★ 怎么用（重要）：
      - 游戏窗口尽量放最前面。切到后台时主循环几乎不跑，读到的值会冻住
        （探针知道这件事，那种时候不会误报）。
      - 打到「打不死人」的时候，心里记一下大概时刻，回头对日志。

    ★ 所有中文提示都在这里打，probe-sync.bat 保持纯 ASCII —— chcp 65001 下
      cmd 会因为多字节字符把后面的命令行拦腰截断（FINDINGS §135 / D074）。
#>
[CmdletBinding()]
param(
    # 盯多久（秒）。**0 = 不限时**，一直采到游戏进程退出 —— 默认就是它，
    # 因为这个 bug 不是每次都复现，得让玩家挂着慢慢玩。
    [double]$Seconds = 0,
    # 采样间隔（秒）。只有值变了才写一行，调密一点不会刷屏。
    [double]$Interval = 0.25,
    # 没有变化时也隔多久记一行（确认探针还活着 + 方便回头对时刻）。
    [double]$Heartbeat = 60,
    # 队列多久不动才算「卡死」。战斗中每次开火都有事件包，15 秒足够保守。
    [double]$Stall = 15,
    # 单个日志上限（MB），超了滚动成 .1 / .2 / .3。
    [double]$MaxMB = 64,
    # 游戏还没起来就等着（探针先挂、客户端后到）。
    [switch]$WaitForGame,
    # 后台挂：起完就返回，不打进度点、不等它结束（留给「自动挂」那种用法）。
    [switch]$Background
)

$ErrorActionPreference = 'Stop'

trap {
    Write-Host ''
    Write-Host "[取证失败] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ''
    exit 1
}

# ★ 不用 $PSScriptRoot（PowerShell 2.0 的脚本里是空的，Win7 SP1 出厂就是 2.0）。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
. (Join-Path $ScriptDir 'wincompat.ps1')

$Root   = Split-Path -Parent $ScriptDir
$Probe  = Join-Path $ScriptDir 'probe_sync.py'
$LogDir = Join-Path $Root 'logs'

if (-not $Background) {
    Write-Host ''
    Write-Host '=== 炮炮火枪手 · 「打不死人」现场取证 ===' -ForegroundColor Cyan
    Write-Host ''
}

# --- 1. 找 Python ----------------------------------------------------------
# ★ 和 launch.ps1 走同一套选择逻辑：老系统（Win7）上 3.14 会弹模态框卡死，
#   必须换 runtime-win7 的 3.8。以前这里写死 runtime\python\python.exe，
#   Win7 玩家挂上探针就等于挂一个卡死的 Python。
$PyChoice = Select-PythonRuntime -Modern (Join-Path $Root 'runtime\python\python.exe') `
                                 -Legacy (Join-Path $Root 'runtime-win7\python\python.exe')
$Python = $PyChoice.Path
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

# --- 2. 游戏进程 -----------------------------------------------------------
# pid 交给 Python 自己找（`auto`）：-WaitForGame 时它会等到游戏起来为止，
# 所以这里只在「手动挂、又不等」的情况下才需要提前劝退。
$targets = @(Get-Process -Name 'BigShot' -ErrorAction SilentlyContinue)
if ($targets.Count -eq 0 -and -not $WaitForGame) {
    Write-Host '游戏没在跑（找不到 BigShot.exe）。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '怎么用：' -ForegroundColor Yellow
    Write-Host '  先把游戏开起来（start.bat / start-debug.bat）、登录、进到房间里，'
    Write-Host '  再双击 tools\probe-sync.bat。'
    Write-Host '  （要「探针先挂、游戏后到」就加 -WaitForGame。）'
    Write-Host ''
    exit 2
}

if (-not (Test-Path -LiteralPath $LogDir -PathType Container)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# --- 3. 拼参数 -------------------------------------------------------------
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log   = Join-Path $LogDir ("probe_sync_{0}.log" -f $stamp)

$spanText = '不限时（游戏退出即收工）'
$secondsArg = 'inf'
if ($Seconds -gt 0) {
    $spanText = "$Seconds 秒"
    $secondsArg = "$Seconds"
}

# ★ 让 Python 自己把 UTF-8 写进文件（同 probe-death.ps1 的理由）：
#   PowerShell 的重定向会按控制台代码页转码，中文出来是问号。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8       = '1'

$probeArgs = @("`"$Probe`"", 'auto', $secondsArg, "$Interval",
               '--log', "`"$log`"",
               '--heartbeat', "$Heartbeat",
               '--stall', "$Stall",
               '--max-mb', "$MaxMB")
if ($WaitForGame) { $probeArgs += '--wait-game' }

$modeFile = Join-Path $LogDir '.server_mode'
if (Test-Path -LiteralPath $modeFile -PathType Leaf) {
    $serverMode = (Read-TextFileRaw $modeFile).Trim()
    if ($serverMode) { $probeArgs += @('--note', "`"服务端模式: $serverMode`"") }
}

# --- 4. 采样 ---------------------------------------------------------------
if ($Background) {
    # 起完就走。探针自己会等游戏起来、也自己会在游戏退出时收工，
    # 所以 launch.ps1 不需要再管它的死活。
    Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList $probeArgs -WindowStyle Hidden | Out-Null
    Write-Host "[探针] 同步取证已挂上（$spanText），日志：$log" -ForegroundColor Gray
    exit 0
}

Write-Host ("采样 {0}，间隔 {1} 秒（只在状态变化时记一行）" -f $spanText, $Interval)
Write-Host ''
Write-Host '★ 现在切回游戏，正常玩，一局接一局。这个窗口放着别关。' -ForegroundColor Yellow
Write-Host '  打到「打不死人」的时候记一下大概时刻，回头好对日志。'
if ($Seconds -le 0) {
    Write-Host '  采样会一直跑到游戏关掉为止；想提前收工，在这个窗口按 Ctrl+C。'
}
Write-Host ''
Write-Host '[采样中]…' -NoNewline

$run = Start-Process -FilePath $Python -WorkingDirectory $Root `
                     -ArgumentList $probeArgs -NoNewWindow -PassThru
$null = $run.Handle
while (-not $run.HasExited) {
    Start-Sleep -Milliseconds 5000
    Write-Host '.' -NoNewline
}
$run.WaitForExit()
Write-Host ''

$text = Read-TextFileRaw $log
if ($run.ExitCode -ne 0 -or $text -match 'OpenProcess 失败') {
    Write-Host ("[探针] 读不到游戏内存（退出码 $($run.ExitCode)）。" +
                "请右键 probe-sync.bat -> 以管理员身份运行，再来一次。"
               ) -ForegroundColor Yellow
}

# --- 5. 收尾 ---------------------------------------------------------------
Write-Host ''
Write-Host '=== 采完了 ===' -ForegroundColor Cyan
Write-Host ''
Write-Host "日志：$log" -ForegroundColor Green
$hits = @(Get-Content -LiteralPath $log -Encoding UTF8 |
          Where-Object { $_ -match '⚠' } |
          Select-Object -Last 12)
if ($hits.Count -gt 0) {
    Write-Host '  探针自己判出来的问题：' -ForegroundColor Yellow
    foreach ($line in $hits) { Write-Host "  $line" }
} else {
    Write-Host '  探针没判出明显问题（也可能是没采到出问题的那一局）。'
}
Write-Host ''
Write-Host '把上面这个 .log 发回来即可。' -ForegroundColor Green
Write-Host ''
exit 0
