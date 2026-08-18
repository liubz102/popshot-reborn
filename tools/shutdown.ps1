<#
    shutdown.ps1 —— 一键关闭：客户端 + 假服务端

    被 stop.bat 调用。

    ★ 只停「占着我们那几个端口的进程」，不做 Get-Process python | Stop-Process。
      用户这台机器上还有别的 Python 活儿，全杀会误伤（PROGRESS「测试前必读」）。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# ★ 不用 $PSScriptRoot（PowerShell 2.0 的脚本里是空的，Win7 SP1 出厂就是 2.0）。
#   点源兼容垫片，端口查询等等由它按系统能力自动挑实现。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
. (Join-Path $ScriptDir 'wincompat.ps1')

$Root     = Split-Path -Parent $ScriptDir
$LogDir   = Join-Path $Root 'logs'
$ModeFile = Join-Path $LogDir '.server_mode'
$RelayStamp = Join-Path $LogDir '.relay_target'
# 认证服 / 游戏服 / 调试控制通道 / 注册页 / 中继两个口。
# 注册页端口可配，所以从 server.config 里读；读不到就用默认的 27810。
$Ports    = @(47611, 27799, 27798, 27800, 47621, 27809, 27808)
$ConfigPath = Join-Path $Root 'server.config'
$webPort = 27810
if (Test-Path -LiteralPath $ConfigPath) {
    foreach ($line in (Get-Content -LiteralPath $ConfigPath -Encoding UTF8)) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#') -or $text.StartsWith(';')) { continue }
        $split = $text.IndexOf('=')
        if ($split -lt 1) { continue }
        if ($text.Substring(0, $split).Trim().ToLowerInvariant() -eq 'local_register_port') {
            $parsed = 0
            if ([int]::TryParse($text.Substring($split + 1).Trim(), [ref]$parsed)) { $webPort = $parsed }
        }
    }
}
$Ports += $webPort

function Say([string]$msg, [string]$color = 'Gray') {
    Write-Host $msg -ForegroundColor $color
}

Say ''
Say '=== 炮炮火枪手 —— 关闭 ===' 'Cyan'
$compatNote = Get-CompatBanner
if ($compatNote) { Say $compatNote 'Yellow' }
Say ''

$stopped = 0

# --- 1. 客户端 --------------------------------------------------------------
foreach ($name in @('BigShot', 'bsloader')) {
    $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
    if ($procs) {
        # ★ 别写 `$procs.Id -join ','`（数组成员枚举是 PowerShell 3.0 才有的）。
        Say "[客户端] 停止 $name pid=$(Get-ProcessIdListText $procs)" 'Yellow'
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        $stopped += @($procs).Count
    }
}

# --- 2. 服务端（按端口精确定位）--------------------------------------------
# ★ 先把「pid → 它占着哪些端口」收齐再统一停。gameserver.py 一个进程同时持有
#   27799 和 27800，边遍历边杀的话第二个端口会撞上「找不到进程」的假错误。
$byPid = @{}
foreach ($port in $Ports) {
    # Get-ListenerPid 来自 wincompat.ps1：新系统走 Get-NetTCPConnection，
    # Win7 这类没有 NetTCPIP 模块的走 netstat，语义一样。
    $owners = Get-ListenerPid $port
    if (-not $owners) { continue }
    foreach ($id in @($owners)) {
        if (-not $byPid.ContainsKey($id)) { $byPid[$id] = @() }
        $byPid[$id] += $port
    }
}
foreach ($id in $byPid.Keys) {
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    $who = '?'
    if ($p) { $who = $p.ProcessName }
    Say "[服务端] 停止 pid=$id ($who)，占用端口 $($byPid[$id] -join ', ')" 'Yellow'
    try { Stop-Process -Id $id -Force -ErrorAction Stop; $stopped++ } catch {
        Say "         停不掉 pid=$id : $($_.Exception.Message)" 'Red'
    }
}

foreach ($stamp in @($ModeFile, $RelayStamp)) {
    if (Test-Path $stamp) { Remove-Item $stamp -Force -ErrorAction SilentlyContinue }
}

# --- 3. 复核 ----------------------------------------------------------------
Start-Sleep -Milliseconds 500
Reset-ListenerCache          # netstat 那条路有短缓存，复核前必须作废
$left = @()
foreach ($port in $Ports) {
    if (Get-ListenerPid $port) { $left += $port }
}
if (Get-Process BigShot -ErrorAction SilentlyContinue) { $left += 'BigShot.exe' }

Say ''
if ($left) {
    Say "!! 还有没停干净的：$($left -join ', ')" 'Red'
    exit 1
}
if ($stopped -eq 0) {
    Say '本来就没有在跑的东西，无事可做。' 'Green'
} else {
    Say "已全部关闭（停了 $stopped 个进程）。" 'Green'
}
exit 0
