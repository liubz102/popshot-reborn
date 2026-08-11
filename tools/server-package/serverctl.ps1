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
    exit 1
}

$Root    = Split-Path -Parent $PSScriptRoot
$Python  = Join-Path $Root 'runtime-win\python\python.exe'
$AppPy   = Join-Path $Root 'server\app.py'
$LogDir  = Join-Path $Root 'logs'
$Config  = Join-Path $Root 'server.config'

$AuthPort  = 47611      # 认证服（客户端写死）
$GamePort  = 27799      # 游戏服（客户端写死）
$RelayPort = 27798      # 原版 TCP 中继（战斗内同步走它）

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }

function Get-ListenerPid([int]$port) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($c) { return ($c | Select-Object -ExpandProperty OwningProcess -Unique) }
    return $null
}

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
        $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        if (-not $conn) { continue }
        foreach ($id in ($conn | Select-Object -ExpandProperty OwningProcess -Unique)) {
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
Say ''

if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到内置 Python：$Python —— 请重新完整解压服务端包，不能只拷启动脚本。"
}
if (-not (Test-Path -LiteralPath $AppPy)) {
    throw "找不到 $AppPy —— 服务端包不完整。"
}
if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# 已经在跑就别重复起：三个端口有任何一个被占着，都说清楚是谁占的。
$busy = @()
foreach ($port in $Ports) {
    $owner = Get-ListenerPid $port
    if ($owner) { $busy += "$port (pid=$owner)" }
}
if ($busy.Count -gt 0) {
    Say "[提示] 这些端口已经有人在听：$($busy -join ', ')" 'Yellow'
    Say '       如果是上一次启动的服务端，先运行 stop.bat 再来。' 'Yellow'
    Say '       如果是别的程序占了 27810，改 server.config 的 local_register_port。' 'Yellow'
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
    Get-Content (Join-Path $LogDir 'server.err') -Tail 20 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $LogDir 'server.out') -Tail 20 -ErrorAction SilentlyContinue
    exit 1
}

Say "[服务端] 已启动（pid=$(Get-ListenerPid $GamePort)）" 'Green'
Say ''
Say '  监听端口' 'Cyan'
Say "    $AuthPort   认证服（客户端写死，不可改）"
Say "    $GamePort   游戏服（客户端写死，不可改）"
Say "    $RelayPort   战斗同步中继"
Say "    $WebPort   用户注册页  ->  http://127.0.0.1:$WebPort/"
Say ''

# 把本机地址列出来：玩家要把它填进自己那份 server.config 的 server_address。
$addrs = @()
try {
    $addrs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.PrefixOrigin -ne 'WellKnown' } |
        Select-Object -ExpandProperty IPAddress -Unique)
} catch {}
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
Say ''
Say '  日志' 'Cyan'
Say '    logs\online.log   谁连上、谁断开、从哪个 IP、在线多久（精简模式也照记）'
Say '    logs\server.out   服务端全部输出（每次启动会被覆盖）'
Say '    ★ 玩家说进不去，先看 logs\online.log。'
Say ''
Say '  这个窗口可以关掉，服务端会继续跑。要停请运行 stop.bat。' 'Cyan'
exit 0
