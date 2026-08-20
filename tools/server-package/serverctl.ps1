<#
    serverctl.ps1 —— 【服务端包】里 Windows 侧的启停实现。

    被包根目录的 start.bat / start-debug.bat / stop.bat 调用。
    那三个 .bat 只允许 ASCII（chcp 65001 下 cmd 会因为多字节字符把后面的
    命令行拦腰截断，FINDINGS §135 / D074），所以**所有中文都在这里打**。

    ★ 这个文件是「服务端包」的模板，由 tools\build-server-package.ps1 拷进包里；
      它在开发目录里是跑不通的（开发目录没有 runtime-win\）。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('start', 'stop')][string]$Action,
    # 逐包 hexdump。日志按 MB 涨，只在排查时开。
    [switch]$DebugLog
)

$ErrorActionPreference = 'Stop'

trap {
    Write-Host ''
    Write-Host "[失败] $($_.Exception.Message)" -ForegroundColor Red
    # 出错位置 + 异常类型由垫片里的 Format-ErrorLocationText 拼好补上
    # （和 launch.ps1 的 trap 同一个套路；Get-Command 兜住垫片还没点源的窗口期）。
    if (Get-Command 'Format-ErrorLocationText' -ErrorAction SilentlyContinue) {
        Write-Host (Format-ErrorLocationText $_) -ForegroundColor Red
    }
    exit 1
}

# ★ 不用 $PSScriptRoot（PowerShell 2.0 的脚本里是空的，Win7 SP1 出厂就是 2.0）。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# 兼容垫片。包里它和本文件同在 tools\ 下；开发目录里本文件在
# tools\server-package\ 下，往上一级找得到 —— 两边都能点源。
$compat = Join-Path $ScriptDir 'wincompat.ps1'
if (-not (Test-Path -LiteralPath $compat)) {
    $compat = Join-Path (Split-Path -Parent $ScriptDir) 'wincompat.ps1'
}
. $compat

$Root    = Split-Path -Parent $ScriptDir
# ★ 服务端包**只有一份运行时**：`runtime-win\`（CPython 3.14）。
#   客户端包里那份 Win7 兼容运行时（`runtime-win7\`，3.8.10）**故意不带** ——
#   它是为了让个别 Win7 玩家能启动游戏，架服务端不考虑老系统（D133）。
$Python  = Join-Path $Root 'runtime-win\python\python.exe'
$AppPy   = Join-Path $Root 'server\app.py'
$LogDir  = Join-Path $Root 'logs'
$Config  = Join-Path $Root 'server.config'

# ★★ 端口号**唯一的源是 server\config.py**，这里向它要，不再各写一份。
#    以前这三个数字在 config.py、bshook.c、launch.ps1、本文件里各有一份 ——
#    那是一类「改了这边没改那边」的故障，而症状通常不是报错，是某个功能
#    悄悄不工作。
$portTable = @{}
foreach ($line in (& $Python (Join-Path $Root 'server\config.py') --ports)) {
    $pair = "$line".Trim() -split '=', 2
    if ($pair.Count -eq 2) { $portTable[$pair[0]] = [int]$pair[1] }
}
if ($portTable.Count -lt 10) {
    throw "读不出端口表（python server\config.py --ports）—— 服务端包不完整？"
}
$AuthPort  = $portTable['AUTH_PORT']
$GamePort  = $portTable['GAME_PORT']
$RelayPort = $portTable['PEER_RELAY_PORT']

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }

# `Get-ListenerPid`（端口 -> 占用它的进程 id）由 wincompat.ps1 提供：
# 新系统走 Get-NetTCPConnection，Win7 这类没有 NetTCPIP 模块的走 netstat。

# 注册页端口写在 server.config 里，解析规则和 server\config.py 一致。
function Get-WebPort {
    $port = 27810
    if (-not (Test-Path -LiteralPath $Config)) { return $port }
    foreach ($line in (Get-Content -LiteralPath $Config -Encoding UTF8)) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#') -or $text.StartsWith(';')) { continue }
        $split = $text.IndexOf('=')
        if ($split -lt 1) { continue }
        if ($text.Substring(0, $split).Trim().ToLowerInvariant() -eq 'local_register_port') {
            $parsed = 0
            if ([int]::TryParse($text.Substring($split + 1).Trim(), [ref]$parsed)) { $port = $parsed }
        }
    }
    return $port
}

$WebPort = Get-WebPort
$Ports = @($AuthPort, $GamePort, $RelayPort, $WebPort)

# ★ 启动前要确认「全部空着」的端口。和上面 $Ports 的区别有两点：
#   1. 多一条 **UDP** —— 位置数据走的是 UDP 27799（和游戏服 TCP 同号，
#      但那是两套独立的端口空间，TCP 空着完全不代表 UDP 空着）；
#   2. 它只用于启动前的检查，停服还是按 $Ports 找进程（服务端进程同时
#      占着 TCP 和 UDP，按 TCP 找到的就是它）。
$PortSpecs = @(
    @{ Port = $AuthPort;  Proto = 'TCP'; Label = '认证服' },
    @{ Port = $GamePort;  Proto = 'TCP'; Label = '游戏服' },
    @{ Port = $GamePort;  Proto = 'UDP'; Label = '位置同步' },
    @{ Port = $RelayPort; Proto = 'TCP'; Label = '战斗同步中继' },
    @{ Port = $WebPort;   Proto = 'TCP'; Label = '注册页' }
)

# ---------------------------------------------------------------------------
#  stop
# ---------------------------------------------------------------------------
if ($Action -eq 'stop') {
    Say ''
    Say '=== 炮炮火枪手服务端 —— 关闭 ===' 'Cyan'
    Say ''
    # ★ 只停「占着我们这几个端口的进程」，绝不 Get-Process python | Stop-Process
    #   —— 这台机器上可能还有别人的 Python 在跑。
    $byPid = @{}
    foreach ($port in $Ports) {
        $owners = Get-ListenerPid $port
        if (-not $owners) { continue }
        foreach ($id in @($owners)) {
            if (-not $byPid.ContainsKey($id)) { $byPid[$id] = @() }
            $byPid[$id] += $port
        }
    }
    $stopped = 0
    foreach ($id in $byPid.Keys) {
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        $who = '?'
        if ($p) { $who = $p.ProcessName }
        Say "[服务端] 停止 pid=$id ($who)，占用端口 $($byPid[$id] -join ', ')" 'Yellow'
        try { Stop-Process -Id $id -Force -ErrorAction Stop; $stopped++ } catch {
            Say "         停不掉 pid=$id : $($_.Exception.Message)" 'Red'
        }
    }
    Start-Sleep -Milliseconds 500
    Reset-ListenerCache          # netstat 那条路有短缓存，复核前必须作废
    $left = @()
    foreach ($port in $Ports) {
        if (Get-ListenerPid $port) { $left += $port }
    }
    Say ''
    if ($left) {
        Say "!! 还有端口没释放：$($left -join ', ')" 'Red'
        exit 1
    }
    if ($stopped -eq 0) {
        Say '本来就没在跑，无事可做。' 'Green'
    } else {
        Say "已关闭（停了 $stopped 个进程）。玩家的连接会一起断开。" 'Green'
    }
    exit 0
}

# ---------------------------------------------------------------------------
#  start
# ---------------------------------------------------------------------------
$mode = 'normal'
if ($DebugLog) { $mode = 'debug' }

Say ''
Say "=== 炮炮火枪手服务端 —— 启动（日志模式：$mode）===" 'Cyan'
if ($DebugLog) {
    Say '    调试模式：逐包 hexdump + 每条连接一对抓包文件，日志按 MB 涨。' 'Yellow'
    Say '    排查完请换回 start.bat，别长期开着。' 'Yellow'
}
$compatNote = Get-CompatBanner
if ($compatNote) { Say $compatNote 'Yellow' }
# ★ 服务端包要求 Win10 及以上：`runtime-win\` 是 Python 3.14，官方只支持
#   Win10+，在更老的系统上会弹「缺少 api-ms-win-core-path-l1-1-0.dll」的
#   **模态框**把脚本卡死。先把话说清楚，别让人对着英文弹窗猜（§215）。
if ((Get-WindowsBuildMajor) -lt 10) {
    Say '!! 这台电脑的 Windows 版本低于 Windows 10 —— 服务端包不支持这么老的系统。' 'Red'
    Say '   包内的 Python 3.14 只支持 Win10 及以上，接下来多半会弹' 'Red'
    Say '   「缺少 api-ms-win-core-path-l1-1-0.dll」并卡住。' 'Red'
    Say '   请换一台 Win10 及以上的机器架服务端（Linux 也行，用 start.sh）。' 'Red'
    Say '   ★ 客户端那边不受影响：客户端包自带 Win7 运行时，Win7 玩家照样能进游戏。' 'Yellow'
}
Say ''

if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到内置 Python：$Python —— 请重新完整解压服务端包，不能只拷启动脚本。"
}
if (-not (Test-Path -LiteralPath $AppPy)) {
    throw "找不到 $AppPy —— 服务端包不完整。"
}
if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# 启动前把每一个要绑的端口都查一遍（TCP + UDP），有一个被占着就不启动。
#
# ★ 为什么是硬失败而不是「警告一下继续」：端口被别人占着时受影响的功能会
#   **静默**坏掉。TCP 那几个还能靠后面的「端口没起全」兜住；UDP 那条位置
#   通道却完全没有回执 —— 服务端照样起来、玩家照样能玩，只是位置数据全部
#   投进了黑洞，最后只能看到「别人卡」却查不出任何原因。宁可现在就说清楚。
Reset-ListenerCache
$busy = Test-PortsFree $PortSpecs
if ($busy.Count -gt 0) {
    Say ''
    Say '!! 端口被占用，服务端无法启动：' 'Red'
    foreach ($line in $busy) { Say "     $line" 'Red' }
    Say '   处理办法：' 'Yellow'
    Say '     * 如果是上一次启动的服务端，先运行 stop.bat 再来；' 'Yellow'
    Say "     * 如果是别的程序占了 $WebPort，改 server.config 的 local_register_port；" 'Yellow'
    Say '     * 其余端口是客户端写死的，只能把占用它的程序关掉。' 'Yellow'
    Say ''
    exit 1
}

# ★ --no-control：调试控制通道（27800）在服务端包里默认关闭。
#   它能直接往任意连接推包，只该在开发机上开。
$appArgs = @("`"$AppPy`"", '--no-control')
if ($DebugLog) { $appArgs += '--verbose' }

Start-Process -FilePath $Python -WorkingDirectory $Root `
    -ArgumentList $appArgs `
    -RedirectStandardOutput (Join-Path $LogDir 'server.out') `
    -RedirectStandardError  (Join-Path $LogDir 'server.err') `
    -WindowStyle Hidden | Out-Null

$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 250
    $up = 0
    foreach ($port in $Ports) { if (Get-ListenerPid $port) { $up++ } }
    if ($up -eq $Ports.Count) { $ok = $true; break }
}
if (-not $ok) {
    Say '[启动失败] 端口没起全，下面是 logs\server.err 的末尾：' 'Red'
    Get-FileTailLines (Join-Path $LogDir 'server.err') 20
    Get-FileTailLines (Join-Path $LogDir 'server.out') 20
    exit 1
}

Say "[服务端] 已启动（pid=$(Get-ListenerPid $GamePort)）" 'Green'
Say ''
Say '  监听端口' 'Cyan'
Say "    $AuthPort   认证服（客户端写死，不可改）"
Say "    $GamePort   游戏服（客户端写死，不可改）"
Say "    $RelayPort   战斗同步中继"
Say "    $GamePort   位置同步（UDP，和游戏服同号但要单独放行）"
Say "    $WebPort   用户注册页  ->  http://127.0.0.1:$WebPort/"
Say ''

# 把本机地址列出来：玩家要把它填进自己那份 server.config 的 server_address。
# Get-LocalIPv4List 来自 wincompat.ps1：有 NetTCPIP 就走 Get-NetIPAddress，
# Win7 这类没有的退回 .NET 2.0 就有的 DNS 查询。
$addrs = @(Get-LocalIPv4List)
if ($addrs.Count -gt 0) {
    Say '  玩家那边 server.config 里填这个地址（局域网）：' 'Cyan'
    foreach ($a in $addrs) { Say "    server_address = $a" }
    Say '  公网/云主机请填公网 IP 或域名。' 'Cyan'
} else {
    Say '  没找到本机的局域网 IPv4 地址，玩家那边填这台机器的实际地址。' 'Yellow'
}
Say ''
Say '  ★ 第一次启动时 Windows 防火墙会弹窗，必须点「允许访问」，' 'Yellow'
Say '    否则别的电脑连不进来。已经点过「取消」的话，用管理员权限执行：' 'Yellow'
Say "      netsh advfirewall firewall add rule name=PopShot dir=in action=allow protocol=TCP localport=$AuthPort,$GamePort,$RelayPort,$WebPort" 'Yellow'
Say "      netsh advfirewall firewall add rule name=PopShot-UDP dir=in action=allow protocol=UDP localport=$GamePort" 'Yellow'
Say '' 'Yellow'
Say "    ⚠ 上面第二条（UDP $GamePort）别漏：位置数据走它，漏了不会报任何错" 'Yellow'
Say '      服务端照常起、游戏照常玩，只是自动退回 TCP，等于这个功能没开。' 'Yellow'
Say "    ⚠ 云主机的【安全组】也要单独加这条 UDP 规则，只在系统防火墙里开不够。" 'Yellow'
Say ''
Say '  日志' 'Cyan'
Say '    logs\online.log   谁连上、谁断开、从哪个 IP、在线多久（精简模式也照记）'
Say '    logs\server.out   服务端全部输出（每次启动会被覆盖）'
Say '    ★ 玩家说进不去，先看 logs\online.log。'
Say ''
Say '  这个窗口可以关掉，服务端会继续跑。要停请运行 stop.bat。' 'Cyan'
exit 0
