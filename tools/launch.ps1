<#
    launch.ps1 —— 一键启动：服务端 + 本机中继 + 注入启动客户端

    被 start.bat（正常游玩）和 start-debug.bat（调试）调用，两者只差 -DebugLog。

    做的事，按顺序：
      1. 环境自检（GameGuard.des 已改名 / bshook.dll 存在 / 串流是否在跑）
      2. 服务端：`server\app.py` 一个进程带起认证 47611 + 游戏 27799 + 注册页
         **已经在跑就不重复启动**（按端口的 OwningProcess 判断）
      3. 读 config\server.config，起本机中继（联机模式下客户端经它连远端）
      4. 把配置经**环境变量**交给 bshook（登录框文案 / 注册页 URL 都要用）
      5. 残留的 BigShot.exe 一律先杀（单实例互斥体 BigShot_Assa，见 V0.1 §9）
      6. 按日志级别设好环境变量，再拉起 bsloader.exe

    参数名用 -DebugLog 而不是 -Verbose：后者是 PowerShell 的公共参数，会被吞掉。
#>
[CmdletBinding()]
param(
    # 打开全量调试日志：客户端逐包 dump + 服务端逐包 hexdump。
    # 速度和精简模式几乎一样（V0.1 §105），代价是日志 4 MB 起、关键行淹在 hexdump 里。
    [switch]$DebugLog,
    # 只起服务端和中继，不拉游戏。
    [switch]$NoGame
)

$ErrorActionPreference = 'Stop'

# ★ 所有中文提示都在这里打，start.bat 保持纯 ASCII（chcp 65001 下 cmd 会因为
#   多字节字符把后面的命令行拦腰截断，见 FINDINGS §135）。失败路径也一样，
#   所以这里要自己把「[启动失败]」说出来，bat 不再负责这句。
trap {
    Write-Host ''
    Write-Host "[启动失败] $($_.Exception.Message)" -ForegroundColor Red
    # 出错位置 + 异常类型由垫片里的 Format-ErrorLocationText 拼好补上 ——
    # 光有「拒绝访问」这种消息没法远程定位（2026-08-19 Win7 玩家那次只能
    # 靠逐条排除）。垫片点源之前出错的话函数还没在，Get-Command 兜住。
    if (Get-Command 'Format-ErrorLocationText' -ErrorAction SilentlyContinue) {
        Write-Host (Format-ErrorLocationText $_) -ForegroundColor Red
    }
    exit 1
}

# ★ 不用 $PSScriptRoot：它在 PowerShell 2.0（Win7 SP1 出厂自带）的**脚本**里
#   是空的，`Split-Path -Parent ''` 会直接报错。改用 $MyInvocation 求本脚本
#   所在目录 —— 这一句必须自己写死，不能调垫片里的函数（垫片还没点源进来）。
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
. (Join-Path $ScriptDir 'wincompat.ps1')

$Root       = Split-Path -Parent $ScriptDir
# Win10 及以上用 runtime\（3.14），更老的系统用 runtime-win7\（3.8.10）——
# 3.14 在 Win7 上会弹模态框卡死，只能提前按系统版本选，不能「跑跑试试」。
$PyChoice   = Select-PythonRuntime -Modern (Join-Path $Root 'runtime\python\python.exe') `
                                   -Legacy (Join-Path $Root 'runtime-win7\python\python.exe')
$Python     = $PyChoice.Path
$LogDir     = Join-Path $Root 'logs'
$ModeFile   = Join-Path $LogDir '.server_mode'
$ConfigPath = Join-Path $Root 'config\server.config'
# ★★ 端口号**不在这里写死**，见下面「端口表」那一段 —— 它向
#    server/config.py 要，因为要用内置 Python 去读，所以排在
#    「Python 在不在」那个检查之后。
# ★ `$x = if (...) {...} else {...}` 换成显式赋值：老 PowerShell 上这种
#   「把语句当表达式赋值」的写法不保险，一行的事，不冒这个险。
$Mode       = 'normal'
if ($DebugLog) { $Mode = 'debug' }

function Say([string]$msg, [string]$color = 'Gray') {
    Write-Host $msg -ForegroundColor $color
}

# `Get-ListenerPid`（端口 -> 占用它的进程 id）由 wincompat.ps1 提供：
# 新系统走 Get-NetTCPConnection，Win7 这类没有 NetTCPIP 模块的走 netstat。

function Assert-PortsFree([object[]]$specs, [string]$who) {
    <#
        这些端口必须全空，否则**直接报错退出**。

        ★ 为什么是硬失败而不是「警告一下继续」：端口被别人占着时，受影响的
          功能会**静默**坏掉 —— TCP 那几个还能靠「端口没起来」兜住，UDP 那条
          位置通道却完全没有回执，玩家只会看到「别人卡」却查不出任何原因。
          宁可开局就说清楚是哪个端口、被谁占着。
    #>
    $busy = Test-PortsFree $specs
    if ($busy.Count -eq 0) { return }
    Say ''
    Say "!! 端口被占用，无法启动$who：" 'Red'
    foreach ($line in $busy) { Say "     $line" 'Red' }
    Say '   处理办法：' 'Yellow'
    Say '     * 如果是上一次没关干净的本游戏，先运行 stop.bat；' 'Yellow'
    Say '     * 如果是别的程序，关掉它，或在任务管理器里按上面的 pid 找到它。' 'Yellow'
    Say ''
    exit 1
}

function Stop-ListenerOn([int[]]$ports) {
    # ★ 只停「占着这些端口的进程」。绝不 Get-Process python | Stop-Process ——
    #   用户机器上还有别的 Python 活儿，误伤过一次就够了。
    $ids = @()
    foreach ($p in $ports) {
        $found = Get-ListenerPid $p
        if ($found) { $ids += $found }
    }
    foreach ($id in ($ids | Select-Object -Unique)) {
        try { Stop-Process -Id $id -Force -ErrorAction Stop } catch {}
    }
    if ($ids) {
        Start-Sleep -Milliseconds 700
        # netstat 那条路会缓存快照，杀完必须让它作废，否则下一次查询
        # 还看得见刚被杀掉的进程。
        Reset-ListenerCache
    }
}

# 读 server.config。解析规则和 server\config.py 保持一致：
# key = value、# 或 ; 起头是注释、认不出的键忽略、缺键用默认值。
function Read-ServerConfig([string]$path) {
    $cfg = @{
        server_address       = '192.168.1.100'
        server_register_port = '27810'
        local_register_port  = '27810'
        proxy_type           = 'socks5'
        proxy_address        = ''
        proxy_port           = '1080'
        proxy_username       = ''
        proxy_password       = ''
    }
    if (-not (Test-Path -LiteralPath $path)) { return $cfg }
    foreach ($line in (Get-Content -LiteralPath $path -Encoding UTF8)) {
        $text = $line.Trim()
        if (-not $text) { continue }
        if ($text.StartsWith('#') -or $text.StartsWith(';')) { continue }
        $split = $text.IndexOf('=')
        if ($split -lt 1) { continue }
        $key = $text.Substring(0, $split).Trim().ToLowerInvariant()
        $value = $text.Substring($split + 1).Trim()
        if ($cfg.ContainsKey($key) -and $value) { $cfg[$key] = $value }
    }
    # IPv6 可能被写成 [2001:db8::1]，去掉方括号（拼 URL 时再加回去）。
    $addr = $cfg['server_address']
    if ($addr.StartsWith('[') -and $addr.EndsWith(']')) {
        $cfg['server_address'] = $addr.Substring(1, $addr.Length - 2).Trim()
    }
    $proxyAddr = $cfg['proxy_address']
    if ($proxyAddr.StartsWith('[') -and $proxyAddr.EndsWith(']')) {
        $cfg['proxy_address'] = $proxyAddr.Substring(1, $proxyAddr.Length - 2).Trim()
    }
    return $cfg
}

function Get-TextSha256([string]$text) {
    $sha = New-Object System.Security.Cryptography.SHA256Managed
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        # ★ 用 Clear() 不用 Dispose()：.NET 3.5（PowerShell 2.0 的运行时）里
        #   HashAlgorithm.Dispose 是**显式接口实现**，PowerShell 调不到它，
        #   会在 finally 里抛「找不到 Dispose 方法」。Clear() 各版本都是 public。
        $sha.Clear()
    }
}

# --- 版本号：启动横幅要显示 + 开发机缺 BUILD.ver 时才补生成 -----------------
# 客户端上报的版本号来自包根 BUILD.ver（bshook 读它补丁握手版本号）：
#   * BUILD.ver 已存在：一律不碰（发布包里由打包脚本写好；开发机上要么是
#     上次启动生成的、要么是手改过的调试值）—— 手改它就能试版本门禁 /
#     上报版本，启动脚本不会把它刷回去。横幅直接读它的 "version"（bshook
#     读的也是这份文件），排查时不用再翻文件问。
#   * 开发机（仓库里）缺 BUILD.ver：按 tools\build-ver.config 现场生成一份，
#     让开发机上报的版本和「将来打出来的包」一致（版本号源文件不进包）。
# 认不出版本号只黄字提醒不拦启动：缺了它客户端按原版 311 上报（= 旧版）。
$BuildVerText = ''
$verSrc = Join-Path $Root 'tools\build-ver.config'
$verDst = Join-Path $Root 'BUILD.ver'
$verSrcExists = Test-Path -LiteralPath $verSrc -PathType Leaf
if (Test-Path -LiteralPath $verDst -PathType Leaf) {
    $m = [regex]::Match([System.IO.File]::ReadAllText($verDst), '"version"\s*:\s*"([^"]+)"')
    if ($m.Success) {
        $BuildVerText = $m.Groups[1].Value
        if ($verSrcExists) {
            Say ''
            Say "BUILD.ver 已存在（版本 $($BuildVerText)），保持不动 —— 客户端握手会上报这个版本"
        }
    } elseif ($verSrcExists) {
        Say ''
        Say '!! BUILD.ver 已存在但认不出版本号，未改动 —— 客户端将按旧版(311)上报' 'Yellow'
    }
} elseif ($verSrcExists) {
    Say ''
    $verText = ''
    foreach ($line in ([System.IO.File]::ReadAllText($verSrc) -split "`r?`n")) {
        $t = "$line".Trim()
        if ($t.Length -gt 0 -and $t[0] -ne '#' -and $t[0] -ne ';') {
            $verText = $t
            break
        }
    }
    if ($verText -ne '') {
        if ($verText[0] -eq 'v' -or $verText[0] -eq 'V') { $verText = $verText.Substring(1).Trim() }
    }
    if ($verText -match '^\d+(\.\d+){0,2}$') {
        while (($verText -split '\.').Count -lt 3) { $verText = "$($verText).0" }
        $verText = "V$($verText)"
        $json = "{`"version`": `"$($verText)`"}`n"
        [System.IO.File]::WriteAllText($verDst, $json, (New-Object System.Text.UTF8Encoding($false)))
        $BuildVerText = $verText
        Say "BUILD.ver 已生成（版本 $($verText)）—— 客户端握手会上报这个版本"
    } else {
        Say '!! tools\build-ver.config 里认不出版本号，BUILD.ver 没生成 —— 客户端将按旧版(311)上报' 'Yellow'
    }
} else {
    Say ''
    Say '!! 根目录没有 BUILD.ver —— 客户端将按旧版(311)上报，连开了版本门禁的服务器会被要求升级' 'Yellow'
}

Say ''
if ($BuildVerText -ne '') {
    Say "=== 炮炮火枪手 $($BuildVerText) —— 启动（日志模式：$Mode）===" 'Cyan'
} else {
    Say "=== 炮炮火枪手 —— 启动（日志模式：$Mode）===" 'Cyan'
}
if ($DebugLog) {
    Say '    调试模式：客户端和服务端都会逐包 dump（日志 4 MB 起）。' 'Yellow'
    Say '    速度和 start.bat 差不多，但关键行会淹在 hexdump 里 —— 平时玩用 start.bat。' 'Yellow'
}

# 老系统（Win7 这类）走的是兼容路径，说一声，免得用户以为哪里不对。
$compatNote = Get-CompatBanner
if ($compatNote) { Say $compatNote 'Yellow' }
# 选了哪份 Python（老系统上会改用 runtime-win7 的 3.8）。
$pyColor = 'Yellow'
if (-not $PyChoice.IsLegacy) { $pyColor = 'Red' }   # 该用老运行时却没带 = 红字警告
foreach ($line in $PyChoice.Notes) { Say $line $pyColor }

Say ''

# --- 1. 环境自检 -----------------------------------------------------------
$loader = Join-Path $Root 'hook\bin\bsloader.exe'
$dll    = Join-Path $Root 'hook\bin\bshook.dll'
if (-not (Test-Path $loader)) { throw "找不到 $loader —— 先跑 hook\build.bat" }
if (-not (Test-Path $dll))    { throw "找不到 $dll —— 先跑 hook\build.bat" }
if (-not (Test-Path $Python)) {
    throw "找不到内置 Python: $Python —— 请重新解压完整的便携版，不能只复制启动脚本。"
}

# --- 端口表：唯一的源是 server/config.py -----------------------------------
# 以前这个脚本里有 9 个端口字面量，`hook/bshook.c` 里另有 11 个，两边靠一串
# POPSHOT_*_PORT 环境变量在运行时对齐。那既是重复劳动，也是一类「改了这边
# 没改那边」的故障 —— 症状通常不是报错，而是某个功能悄悄不工作（位置数据
# 那条 UDP 通道尤其典型：端口对不上时它一点回声都没有）。
#
# 现在只有一个源：C 那边走生成的 hook/ports.h，这里走 config.py 的 --ports，
# 两条路读的是同一份常量。
$portTable = @{}
foreach ($line in (& $Python (Join-Path $Root 'server\config.py') --ports)) {
    $pair = "$line".Trim() -split '=', 2
    if ($pair.Count -eq 2) { $portTable[$pair[0]] = [int]$pair[1] }
}
if ($portTable.Count -lt 10) {
    throw "读不出端口表（python server\config.py --ports）—— 包不完整？"
}
$AuthPort      = $portTable['AUTH_PORT']
$GamePort      = $portTable['GAME_PORT']
$CtrlPort      = $portTable['CONTROL_PORT']
$RelayAuth     = $portTable['RELAY_AUTH_PORT']
$RelayGame     = $portTable['RELAY_GAME_PORT']
$PeerRelay     = $portTable['PEER_RELAY_PORT']
$RelayPeer     = $portTable['RELAY_PEER_PORT']
$RelayUdpSync  = $portTable['RELAY_UDP_SYNC_PORT']
$ClientUdpPort = $portTable['CLIENT_UDP_PORT']

# 铁律 2：绝不让 2007 年的 GameGuard 真的跑起来（它会装内核驱动）。
$gg = Join-Path $Root 'game_patched\GameGuard.des'
if (Test-Path $gg) {
    throw "game_patched\GameGuard.des 还在原位！必须改名（见 CLAUDE.md 铁律 2）后再启动。"
}

# 串流**会话进行中**会让 D3D9 HAL 整体不可用，画面出不来（V0.1 §61）。
#
# ★ 判据是探针，不是进程名。`sunshine.exe` 在这台机器上是常驻后台服务，
#   进程在 ≠ 正在串流 —— 而用户可能正靠它远程连着这台机器，更不能去杀它。
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
# V0.2 起认证服和游戏服合并成一个进程（决策 D064）：认证服签发的票据要让
# 游戏服查得到，跨进程就得再造一套 IPC。
$authPid = Get-ListenerPid $AuthPort
$gamePid = Get-ListenerPid $GamePort
$running = ($authPid -and $gamePid -and $authPid -eq $gamePid)
$lastMode = (Read-TextFileRaw $ModeFile).Trim()

if ($running -and $lastMode -eq $Mode) {
    Say "[服务端] 已在运行，跳过启动（pid=$authPid，模式=$Mode）" 'Green'
} else {
    if ($running) {
        Say "[服务端] 已在运行，但模式是 '$lastMode'，本次要 '$Mode' —— 重启它" 'Yellow'
    } elseif ($authPid -or $gamePid) {
        Say '[服务端] 上次没关干净（端口只占了一半），全部重启' 'Yellow'
    }
    Stop-ListenerOn @($AuthPort, $GamePort, $CtrlPort)
    # ★ 清完自己的残留之后再查：这时候还占着的一定是**别的程序**。
    #   TCP 27799 和 UDP 27799 是两套端口空间，必须分开查 —— 位置数据走的
    #   是后者，只查 TCP 的话它会在被占时静默失效。
    Reset-ListenerCache
    Assert-PortsFree @(
        @{ Port = $AuthPort; Proto = 'TCP'; Label = '认证服' },
        @{ Port = $GamePort; Proto = 'TCP'; Label = '游戏服' },
        @{ Port = $GamePort; Proto = 'UDP'; Label = '位置同步' },
        @{ Port = $CtrlPort; Proto = 'TCP'; Label = '调试控制通道' }
    ) '本机服务端'

    # Windows PowerShell 5.1 会把 -ArgumentList 数组直接用空格拼成命令行，
    # 不会替单个参数补引号。脚本路径必须显式引用，否则目录名里的空格会截断路径。
    $appScript = Join-Path $Root 'server\app.py'
    $appArgs = @("`"$appScript`"")
    if ($DebugLog) { $appArgs += '--verbose' }
    # 关掉原版 rcp 中继服，降低复杂度，提升稳定性 —— 27798 不再
    #   监听、不回 0x0210，玩家间同步整场走 0x040e/0x040f 回退路径。
    #   启动日志应出现「中继服   已关闭（--no-tcp-relay）」。
    $appArgs += '--no-tcp-relay'

    # ★ 上一次那份先归档，别覆盖（同一天多次重启也留得住）。见 Move-LogAside。
    Move-LogAside (Join-Path $LogDir 'server.out') | Out-Null
    Move-LogAside (Join-Path $LogDir 'server.err') | Out-Null
    Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList $appArgs `
        -RedirectStandardOutput (Join-Path $LogDir 'server.out') `
        -RedirectStandardError  (Join-Path $LogDir 'server.err') `
        -WindowStyle Hidden | Out-Null

    # 等端口真的起来再往下走，别用固定 Sleep 赌。
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if ((Get-ListenerPid $AuthPort) -and (Get-ListenerPid $GamePort)) { $ok = $true; break }
    }
    if (-not $ok) {
        Say '[启动失败] 服务端端口没起来，下面是 logs\server.err 的末尾：' 'Red'
        Get-FileTailLines (Join-Path $LogDir 'server.err') 20
        exit 1
    }
    Set-Content -Path $ModeFile -Value $Mode -Encoding utf8
    Say "[服务端] 已启动（认证 $AuthPort / 游戏 $GamePort，pid=$(Get-ListenerPid $AuthPort)）" 'Green'
}

# --- 3. 读配置 + 起本机中继 -------------------------------------------------
# server.config 由服务端在启动时按模板生成，所以到这里一定已经存在。
$cfg      = Read-ServerConfig $ConfigPath
$remote   = $cfg['server_address']
$remoteReg = $cfg['server_register_port']
$localReg  = $cfg['local_register_port']
$remoteUrlHost = $remote
if ($remote -like '*:*') { $remoteUrlHost = "[$remote]" }   # IPv6 拼 URL 要加方括号

# 目标或任一代理字段变了都要重起。签名里会算账号密码，但磁盘只落 SHA-256，
# 不把第二份明文凭据写进 logs\.relay_target。
$relayConfigText = @(
    $remote,
    $cfg['proxy_type'],
    $cfg['proxy_address'],
    $cfg['proxy_port'],
    $cfg['proxy_username'],
    $cfg['proxy_password']
) -join "`n"
$relaySignature = Get-TextSha256 $relayConfigText
$relayStamp = Join-Path $LogDir '.relay_target'
$lastSignature = (Read-TextFileRaw $relayStamp).Trim()
$relayPid = Get-ListenerPid $RelayAuth
if ($relayPid -and $lastSignature -eq $relaySignature) {
    Say "[中继]   已在运行（pid=$relayPid，目标 $remoteUrlHost）" 'Green'
} else {
    if ($relayPid) { Say '[中继]   远程连接配置已改变 —— 重启它' 'Yellow' }
    Stop-ListenerOn @($RelayAuth, $RelayGame, $RelayPeer)
    Reset-ListenerCache
    Assert-PortsFree @(
        @{ Port = $RelayAuth;    Proto = 'TCP'; Label = '认证中继' },
        @{ Port = $RelayGame;    Proto = 'TCP'; Label = '游戏中继' },
        @{ Port = $RelayPeer;    Proto = 'TCP'; Label = '战斗中继' },
        @{ Port = $RelayUdpSync; Proto = 'UDP'; Label = '位置同步中继' }
    ) '本机中继'
    $relayScript = Join-Path $Root 'server\relay.py'
    Move-LogAside (Join-Path $LogDir 'relay.out') | Out-Null
    Move-LogAside (Join-Path $LogDir 'relay.err') | Out-Null
    Start-Process -FilePath $Python -WorkingDirectory $Root `
        -ArgumentList @("`"$relayScript`"") `
        -RedirectStandardOutput (Join-Path $LogDir 'relay.out') `
        -RedirectStandardError  (Join-Path $LogDir 'relay.err') `
        -WindowStyle Hidden | Out-Null
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if ((Get-ListenerPid $RelayAuth) -and (Get-ListenerPid $RelayGame) -and
            (Get-ListenerPid $RelayPeer)) { $ok = $true; break }
    }
    if ($ok) {
        Set-Content -Path $relayStamp -Value $relaySignature -Encoding ascii
        Say "[中继]   已启动（选「远程服务器」时经 127.0.0.1:$RelayAuth / $RelayGame / $RelayPeer 转发到 $remoteUrlHost）" 'Green'
        Say "         位置数据另走 UDP：127.0.0.1:$RelayUdpSync -> ${remoteUrlHost}:$GamePort/udp" 'Green'
        Say "         游戏从 UDP $ClientUdpPort 收位置数据" 'Green'
        Say '         ⚠ 服务器要放行 UDP —— 没放行会自动退回 TCP，网络不稳定时会比较卡。' 'Gray'
    } else {
        Say '!! 中继没起来，「远程服务器」会连不上；「本机服务器」不受影响。看 logs\relay.err' 'Red'
        Get-FileTailLines (Join-Path $LogDir 'relay.err') 20
    }
}

if ($NoGame) { Say ''; Say '（-NoGame：不启动客户端）' 'Gray'; exit 0 }

# --- 更新善后：静默清掉历次更新「改名让位」的 .update_old ------------------
# 自动更新器覆盖「正在运行的自己」时，会把旧 exe 改名成 *.update_old 让位
# （Windows 不许覆盖运行中的 exe，但允许改名）。更新器进程退出后锁就没了，
# 这里启动成功后顺手扫掉；仍被占用的极少数（更新器刚退出零点几秒内）静默
# 跳过，下次启动再删。绝不弹提示（用户拍板）。
try {
    $oldFiles = @(Get-ChildItem -Path $Root -Recurse -Filter '*.update_old' -ErrorAction SilentlyContinue |
                  Where-Object { -not $_.PSIsContainer })
    foreach ($old in $oldFiles) {
        Remove-Item -LiteralPath $old.FullName -Force -ErrorAction SilentlyContinue
    }
    if ($oldFiles.Count -gt 0) {
        Say ("[更新善后] 已清理上次更新遗留的旧文件 " + $oldFiles.Count + " 个") 'DarkGray'
    }
} catch { }

# --- 4. 把配置交给 bshook ---------------------------------------------------
# ★ 走环境变量而不是让 C 去解析 UTF-8 配置文件：bsloader.exe 本来就把环境
#   继承给客户端进程，省掉一整套解析和编码处理（决策 D065）。
$env:POPSHOT_SERVER_ADDRESS    = $remote
$env:POPSHOT_SERVER_REG_PORT   = $remoteReg
$env:POPSHOT_LOCAL_REG_PORT    = $localReg
# ★ 端口**不再传给 bshook**：它是编译期从 hook/ports.h 拿的，和上面那张表
#   同源（server/config.py），运行时没有对不齐的余地。只有上面三个
#   （服务器地址 + 两个注册页端口）真的来自玩家的 server.config。

# --- 5. 残留客户端 ----------------------------------------------------------
# 互斥体 BigShot_Assa 决定了同时只能有一个实例，残留的会让新实例秒退，
# 而那个现象非常像「注入被检测」—— 骗过我们一次了（V0.1 §9）。
$old = Get-Process BigShot -ErrorAction SilentlyContinue
if ($old) {
    # ★ 别写 `$old.Id -join ','`：数组的成员枚举是 PowerShell 3.0 才有的，
    #   2.0 上 `$数组.Id` 是 $null，日志里 pid 会变成空。
    Say "[客户端] 先清掉残留实例 pid=$(Get-ProcessIdListText $old)" 'Yellow'
    $old | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# --- 6. 拉起客户端 ----------------------------------------------------------
# ★ 游戏【接收】位置数据的那个 UDP 口必须空着：bshook 会让游戏去 bind 它，
#   bind 成功之后才会告诉本机中继「可以往这儿投」。被别的程序占着的话游戏
#   bind 会失败（原版会弹「…(Bind Fail)」），位置数据的下行就静默退回 TCP。
#   残留的 BigShot.exe 上一步已经清掉了，所以这里占着的一定是别人。
Reset-ListenerCache
Assert-PortsFree @(
    @{ Port = $ClientUdpPort; Proto = 'UDP'; Label = '游戏接收位置数据' }
) '游戏客户端'

if ($DebugLog) { $env:BSHOOK_VERBOSE_LOG = '1' } else { $env:BSHOOK_VERBOSE_LOG = '0' }

Move-LogAside (Join-Path $LogDir 'bsloader.out') | Out-Null
Move-LogAside (Join-Path $LogDir 'bsloader.err') | Out-Null
Start-Process -FilePath $loader -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $LogDir 'bsloader.out') `
    -RedirectStandardError  (Join-Path $LogDir 'bsloader.err') `
    -WindowStyle Hidden | Out-Null

Say '[客户端] bsloader 已启动，游戏登录窗口稍后出来，请耐心等待十几秒......' 'Green'

# 同步探针（tools\probe-sync.ps1）**不再自动挂**：它是给 bug调查/9
# 「第二局打不死人」采证用的，而那个 bug 已经从根上修掉了
# （§218 / D137 / D138：局号改成服务端权威的换代号）。
# 脚本留在 tools 里存档 —— 以后真要采证，手动双击 tools\probe-sync.bat
# 给**已经在跑**的游戏挂上即可。

Say ''
Say '--- 登录界面上可以自己选服务器 ---' 'Cyan'
Say '  「本机服务器」          连本机，一个人玩，存档在本机' 'Cyan'
Say "  「远程服务器」          连 $remoteUrlHost（改 config\server.config 换服务器）" 'Cyan'
Say ''
Say "  远程服务器地址配置在：  $ConfigPath" 'Cyan'
Say '  首次使用请先注册账号：  点登录框下方的「在服务器…上注册用户」链接' 'Cyan'
Say "                          本机服务器的注册页 http://127.0.0.1:$localReg/" 'Cyan'
Say ''
if ($DebugLog) {
    Say "调试日志：logs\bshook_*.log（客户端）、logs\server.out（服务端）" 'Cyan'
}
Say '这个窗口可以关掉，游戏会继续跑。关闭游戏和服务端请运行 stop.bat。' 'Cyan'
exit 0
