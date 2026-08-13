<#
    launch.ps1 —— 一键启动：服务端 + 本机中继 + 注入启动客户端

    被 start.bat（正常游玩）和 start-debug.bat（调试）调用，两者只差 -DebugLog。

    做的事，按顺序：
      1. 环境自检（GameGuard.des 已改名 / bshook.dll 存在 / 串流是否在跑）
      2. 服务端：`server\app.py` 一个进程带起认证 47611 + 游戏 27799 + 注册页
         **已经在跑就不重复启动**（按端口的 OwningProcess 判断）
      3. 读 server.config，起本机中继（联机模式下客户端经它连远端）
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
    exit 1
}

$Root       = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $Root 'runtime\python\python.exe'
$LogDir     = Join-Path $Root 'logs'
$ModeFile   = Join-Path $LogDir '.server_mode'
$ConfigPath = Join-Path $Root 'server.config'
$AuthPort   = 47611     # 认证服（客户端写死）
$GamePort   = 27799     # 游戏服（客户端写死）
$CtrlPort   = 27800     # 调试控制通道（只绑 127.0.0.1）
$RelayAuth  = 47621     # 联机模式：客户端 -> 中继 -> 远端 47611
$RelayGame  = 27809     # 联机模式：客户端 -> 中继 -> 远端 27799
$PeerRelay  = 27798     # 原版 TCP 中继（服务端；地址由 0x0210 下发，D078/D079）
$RelayPeer  = 27808     # 联机模式：客户端 -> 中继 -> 远端 27798
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
    #   用户机器上还有别的 Python 活儿，误伤过一次就够了。
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
        # 双击 A/D 出近身攻击的判定窗口（毫秒）。250 = 原版，见 FINDINGS §183。
        double_tap_ms        = '500'
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
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
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
$lastMode = ''
if (Test-Path $ModeFile) { $lastMode = (Get-Content $ModeFile -Raw).Trim() }

if ($running -and $lastMode -eq $Mode) {
    Say "[服务端] 已在运行，跳过启动（pid=$authPid，模式=$Mode）" 'Green'
} else {
    if ($running) {
        Say "[服务端] 已在运行，但模式是 '$lastMode'，本次要 '$Mode' —— 重启它" 'Yellow'
    } elseif ($authPid -or $gamePid) {
        Say '[服务端] 上次没关干净（端口只占了一半），全部重启' 'Yellow'
    }
    Stop-ListenerOn @($AuthPort, $GamePort, $CtrlPort)

    # Windows PowerShell 5.1 会把 -ArgumentList 数组直接用空格拼成命令行，
    # 不会替单个参数补引号。脚本路径必须显式引用，否则目录名里的空格会截断路径。
    $appScript = Join-Path $Root 'server\app.py'
    $appArgs = @("`"$appScript`"")
    if ($DebugLog) { $appArgs += '--verbose' }

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
        Get-Content (Join-Path $LogDir 'server.err') -Tail 20 -ErrorAction SilentlyContinue
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
$remoteUrlHost = if ($remote -like '*:*') { "[$remote]" } else { $remote }

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
$lastSignature = ''
if (Test-Path $relayStamp) { $lastSignature = (Get-Content $relayStamp -Raw).Trim() }
$relayPid = Get-ListenerPid $RelayAuth
if ($relayPid -and $lastSignature -eq $relaySignature) {
    Say "[中继]   已在运行（pid=$relayPid，目标 $remoteUrlHost）" 'Green'
} else {
    if ($relayPid) { Say '[中继]   远程连接配置已改变 —— 重启它' 'Yellow' }
    Stop-ListenerOn @($RelayAuth, $RelayGame, $RelayPeer)
    $relayScript = Join-Path $Root 'server\relay.py'
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
    } else {
        Say '!! 中继没起来，「远程服务器」会连不上；「本机服务器」不受影响。看 logs\relay.err' 'Red'
        Get-Content (Join-Path $LogDir 'relay.err') -Tail 20 -ErrorAction SilentlyContinue
    }
}

if ($NoGame) { Say ''; Say '（-NoGame：不启动客户端）' 'Gray'; exit 0 }

# --- 4. 把配置交给 bshook ---------------------------------------------------
# ★ 走环境变量而不是让 C 去解析 UTF-8 配置文件：bsloader.exe 本来就把环境
#   继承给客户端进程，省掉一整套解析和编码处理（决策 D065）。
$env:POPSHOT_SERVER_ADDRESS    = $remote
$env:POPSHOT_SERVER_REG_PORT   = $remoteReg
$env:POPSHOT_LOCAL_REG_PORT    = $localReg
$env:POPSHOT_RELAY_AUTH_PORT   = "$RelayAuth"
$env:POPSHOT_RELAY_GAME_PORT   = "$RelayGame"
$env:POPSHOT_RELAY_PEER_PORT   = "$RelayPeer"
$env:POPSHOT_PEER_RELAY_PORT   = "$PeerRelay"
$env:POPSHOT_DOUBLE_TAP_MS     = $cfg['double_tap_ms']

# --- 5. 残留客户端 ----------------------------------------------------------
# 互斥体 BigShot_Assa 决定了同时只能有一个实例，残留的会让新实例秒退，
# 而那个现象非常像「注入被检测」—— 骗过我们一次了（V0.1 §9）。
$old = Get-Process BigShot -ErrorAction SilentlyContinue
if ($old) {
    Say "[客户端] 先清掉残留实例 pid=$($old.Id -join ',')" 'Yellow'
    $old | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# --- 6. 拉起客户端 ----------------------------------------------------------
if ($DebugLog) { $env:BSHOOK_VERBOSE_LOG = '1' } else { $env:BSHOOK_VERBOSE_LOG = '0' }

Start-Process -FilePath $loader -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $LogDir 'bsloader.out') `
    -RedirectStandardError  (Join-Path $LogDir 'bsloader.err') `
    -WindowStyle Hidden | Out-Null

Say '[客户端] bsloader 已启动，游戏登录窗口稍后出来，请耐心等待十几秒......' 'Green'
Say ''
Say '--- 登录界面上可以自己选服务器 ---' 'Cyan'
Say '  「本机服务器」          连本机，一个人玩，存档在本机' 'Cyan'
Say "  「远程服务器」          连 $remoteUrlHost（改 server.config 换服务器）" 'Cyan'
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
