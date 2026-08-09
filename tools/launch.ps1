<#
    launch.ps1 —— 一键启动：假服务端 + 注入启动客户端

    被 start.bat（正常游玩）和 start-debug.bat（调试）调用，两者只差 -DebugLog。

    做的事，按顺序：
      1. 环境自检（GameGuard.des 已改名 / bshook.dll 存在 / 串流是否在跑）
      2. 服务端：**已经在跑就不重复启动**（按端口的 OwningProcess 判断）
      3. 残留的 BigShot.exe 一律先杀（单实例互斥体 BigShot_Assa，见 FINDINGS §9）
      4. 按日志级别设好环境变量，再拉起 bsloader.exe

    参数名用 -DebugLog 而不是 -Verbose：后者是 PowerShell 的公共参数，会被吞掉。
#>
[CmdletBinding()]
param(
    # 打开全量调试日志：客户端逐包 dump + 服务端逐包 hexdump。
    # 速度和精简模式几乎一样（§105），代价是日志 4 MB 起、关键行淹在 hexdump 里。
    [switch]$DebugLog,
    # 只起服务端，不拉游戏。
    [switch]$NoGame
)

$ErrorActionPreference = 'Stop'
$Root       = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $Root 'runtime\python\python.exe'
$LogDir     = Join-Path $Root 'logs'
$ModeFile   = Join-Path $LogDir '.server_mode'
$AuthPort   = 47611
$GamePort   = 27799
$Mode       = if ($DebugLog) { 'debug' } else { 'normal' }

function Say([string]$msg, [string]$color = 'Gray') {
    Write-Host $msg -ForegroundColor $color
}

# 返回监听指定端口的进程 id（没有则返回 $null）。
function Get-ListenerPid([int]$port) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($c) { return ($c | Select-Object -ExpandProperty OwningProcess -Unique) }
    return $null
}

function Stop-ListenerOn([int[]]$ports) {
    # ★ 只停「占着这些端口的进程」。绝不 Get-Process python | Stop-Process ——
    #   用户机器上还有别的 Python 活儿，误伤过一次就够了（PROGRESS「测试前必读」）。
    $ids = @()
    foreach ($p in $ports) {
        $found = Get-ListenerPid $p
        if ($found) { $ids += $found }
    }
    foreach ($id in ($ids | Select-Object -Unique)) {
        try { Stop-Process -Id $id -Force -ErrorAction Stop } catch {}
    }
    if ($ids) { Start-Sleep -Milliseconds 700 }
}

Say ''
Say "=== 炮炮火枪手 —— 启动（日志模式：$Mode）===" 'Cyan'
if ($DebugLog) {
    Say '    调试模式：客户端和服务端都会逐包 dump（日志 4 MB 起）。' 'Yellow'
    Say '    速度和 start.bat 差不多，但关键行会淹在 hexdump 里 —— 平时玩用 start.bat。' 'Yellow'
}
Say ''

# --- 1. 环境自检 -----------------------------------------------------------
$loader = Join-Path $Root 'hook\bin\bsloader.exe'
$dll    = Join-Path $Root 'hook\bin\bshook.dll'
if (-not (Test-Path $loader)) { throw "找不到 $loader —— 先跑 hook\build.bat" }
if (-not (Test-Path $dll))    { throw "找不到 $dll —— 先跑 hook\build.bat" }
if (-not (Test-Path $Python)) {
    throw "找不到内置 Python: $Python —— 请重新解压完整的便携版，不能只复制启动脚本。"
}

# 铁律 2：绝不让 2007 年的 GameGuard 真的跑起来（它会装内核驱动）。
$gg = Join-Path $Root 'game_patched\GameGuard.des'
if (Test-Path $gg) {
    throw "game_patched\GameGuard.des 还在原位！必须改名（见 CLAUDE.md 铁律 2）后再启动。"
}

# 串流**会话进行中**会让 D3D9 HAL 整体不可用，画面出不来（FINDINGS §61）。
#
# ★ 判据是探针，不是进程名。`sunshine.exe` 在这台机器上是常驻后台服务，
#   进程在 ≠ 正在串流 —— 而用户可能正靠它远程连着这台机器，更不能去杀它。
#   唯一放行标准就是下面这行 `hr=00000000`（会话 09 实测确立）。
$probe = Join-Path $Root 'tools\d3d9_probe.exe'
if (Test-Path $probe) {
    # 输入法、显卡覆盖层等注入到 GUI 进程的组件可能往 stderr 写无害警告。
    # Windows PowerShell 5.1 会把原生程序的 stderr 包装成 ErrorRecord；若沿用
    # 全局的 Stop 策略，即使探针成功也会在这里终止整个启动脚本。
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $probeOutput = @(& $probe 2>&1)
        $probeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    $out = ($probeOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    if ($probeExitCode -ne 0 -or
        $out -notmatch 'CheckDeviceType HAL/X8R8G8B8/windowed hr=00000000') {
        Say '!! D3D9 HAL 当前不可用 —— 画面多半出不来。' 'Red'
        Say '   最常见的原因是有串流会话正在进行（Sunshine/Moonlight），断开即恢复。' 'Red'
        Say "   自己看一眼：$probe" 'Red'
        Say ''
    }
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# --- 2. 服务端：已在跑就复用 ------------------------------------------------
$authPid = Get-ListenerPid $AuthPort
$gamePid = Get-ListenerPid $GamePort
$running = ($authPid -and $gamePid)
$lastMode = ''
if (Test-Path $ModeFile) { $lastMode = (Get-Content $ModeFile -Raw).Trim() }

if ($running -and $lastMode -eq $Mode) {
    Say "[服务端] 已在运行，跳过启动（认证 pid=$authPid / 游戏 pid=$gamePid，模式=$Mode）" 'Green'
} else {
    if ($running) {
        Say "[服务端] 已在运行，但模式是 '$lastMode'，本次要 '$Mode' —— 重启它" 'Yellow'
    } elseif ($authPid -or $gamePid) {
        Say '[服务端] 只有一半在跑（上次没关干净），全部重启' 'Yellow'
    }
    Stop-ListenerOn @($AuthPort, $GamePort, 27800)

    $gameArgs = @((Join-Path $Root 'server\gameserver.py'))
    if ($DebugLog) { $gameArgs += '--verbose' }

    Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList @((Join-Path $Root 'server\authserver.py'), '--port', "$AuthPort", '--reply', 'login') `
        -RedirectStandardOutput (Join-Path $LogDir 'authserver.out') `
        -RedirectStandardError  (Join-Path $LogDir 'authserver.err') `
        -WindowStyle Hidden | Out-Null

    Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList $gameArgs `
        -RedirectStandardOutput (Join-Path $LogDir 'gameserver.out') `
        -RedirectStandardError  (Join-Path $LogDir 'gameserver.err') `
        -WindowStyle Hidden | Out-Null

    # 等端口真的起来再往下走，别用固定 Sleep 赌。
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if ((Get-ListenerPid $AuthPort) -and (Get-ListenerPid $GamePort)) { $ok = $true; break }
    }
    if (-not $ok) {
        Say '!! 服务端端口没起来，看 logs\gameserver.err / authserver.err' 'Red'
        Get-Content (Join-Path $LogDir 'gameserver.err') -Tail 20 -ErrorAction SilentlyContinue
        exit 1
    }
    Set-Content -Path $ModeFile -Value $Mode -Encoding utf8
    Say "[服务端] 已启动（认证 $AuthPort pid=$(Get-ListenerPid $AuthPort) / 游戏 $GamePort pid=$(Get-ListenerPid $GamePort)）" 'Green'
}

if ($NoGame) { Say ''; Say '（-NoGame：不启动客户端）' 'Gray'; exit 0 }

# --- 3. 残留客户端 ----------------------------------------------------------
# 互斥体 BigShot_Assa 决定了同时只能有一个实例，残留的会让新实例秒退，
# 而那个现象非常像「注入被检测」—— 骗过我们一次了（FINDINGS §9）。
$old = Get-Process BigShot -ErrorAction SilentlyContinue
if ($old) {
    Say "[客户端] 先清掉残留实例 pid=$($old.Id -join ',')" 'Yellow'
    $old | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# --- 4. 拉起客户端 ----------------------------------------------------------
if ($DebugLog) { $env:BSHOOK_VERBOSE_LOG = '1' } else { $env:BSHOOK_VERBOSE_LOG = '0' }

Start-Process -FilePath $loader -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $LogDir 'bsloader.out') `
    -RedirectStandardError  (Join-Path $LogDir 'bsloader.err') `
    -WindowStyle Hidden | Out-Null

Say '[客户端] bsloader 已启动，游戏窗口马上出来' 'Green'
Say ''
Say '登录任意账号密码即可（假服务端不校验）。关闭请跑 stop.bat。' 'Cyan'
if ($DebugLog) {
    Say "调试日志：logs\bshook_*.log（客户端）、logs\gameserver.out（服务端）" 'Cyan'
}
exit 0
