<#
    wincompat.ps1 —— 老版 Windows / 老版 PowerShell 兼容垫片

    被 tools\launch.ps1、tools\shutdown.ps1（客户端包）和 tools\serverctl.ps1
    （服务端包）点源（dot-source）加载。**它自己不做任何事**，只提供几个
    「新系统走新写法、老系统走老写法」的函数。
    （★ 例外：挑运行时的 `Select-PythonRuntime` **只有客户端包用** ——
      服务端包不带 Win7 运行时，见该函数的注释和 D133。）

    ## 为什么需要它

    用户 2026-08-17 反馈：把包发给一台 **Windows 7** 的电脑，启动脚本直接报错。
    原因是脚本里用了几样 Windows 8 / PowerShell 3.0 才有的东西，而 Win7 SP1
    出厂只带 **PowerShell 2.0**，也没有 NetTCPIP 模块：

    | 原来的写法 | 最低要求 | Win7 上的表现 |
    |---|---|---|
    | `Get-NetTCPConnection` | Win8 / Server 2012（NetTCPIP 模块） | 「无法识别为 cmdlet」——★ 而且 `-ErrorAction SilentlyContinue` **压不住**「命令找不到」，配上 `$ErrorActionPreference='Stop'` 直接终止整个脚本 |
    | `Get-NetIPAddress`     | 同上 | 同上 |
    | `$PSScriptRoot`（脚本里）| PowerShell 3.0 | 为空 → `Split-Path -Parent ''` 报错，`$Root` 直接错 |
    | `Get-Content -Raw`     | PowerShell 3.0 | 参数不存在 |
    | `Get-Content -Tail`    | PowerShell 3.0 | 参数不存在 |
    | `$数组.属性`（成员枚举）| PowerShell 3.0 | 返回 $null（日志里 pid 列表变空） |
    | `HashAlgorithm.Dispose()` | .NET 4.0 | .NET 3.5 上是显式接口实现，PowerShell 调不到 |

    ## 判据用「能力」不用「版本号」

    一律 `Get-Command xxx` 探一下有没有这个 cmdlet，而不是去比 Windows 版本号
    —— 装了 WMF 3.0/4.0/5.1 的 Win7 也能用一部分新写法，按版本号一刀切会白白
    退化；而且版本号的取法本身就有坑（未加清单的进程拿到的是 6.2）。

    ## 铁律：不要在这里写「杀掉所有 python」这种粗暴逻辑

    端口 -> 占用进程的映射无论走哪条路，语义都必须和原来一致：
    **只认监听我们那几个端口的进程**。
#>

# ---------------------------------------------------------------------------
#  能力探测（模块加载时算一次）
# ---------------------------------------------------------------------------

# Get-Command 找不到命令时写的是**非终止**错误，SilentlyContinue 压得住，
# 所以这里不会被 $ErrorActionPreference='Stop' 带走。
$script:HasNetTcpCmdlet = [bool](Get-Command 'Get-NetTCPConnection' -ErrorAction SilentlyContinue)
$script:HasNetUdpCmdlet = [bool](Get-Command 'Get-NetUDPEndpoint'   -ErrorAction SilentlyContinue)
$script:HasNetIpCmdlet  = [bool](Get-Command 'Get-NetIPAddress'    -ErrorAction SilentlyContinue)
$script:PsMajor = 2
if ($PSVersionTable -and $PSVersionTable.PSVersion) {
    $script:PsMajor = $PSVersionTable.PSVersion.Major
}

# ★ POPSHOT_FORCE_LEGACY=1 -> 在新系统上强行走老路。
#   手上没有 Win7 机器时，这是唯一能真跑一遍兼容分支的办法 —— 没有它，
#   netstat 那条路就只能靠「看着像对」发出去，而这次出问题的正是这种东西。
#   用法：`set POPSHOT_FORCE_LEGACY=1` 之后照常双击 start.bat / stop.bat。
if ($env:POPSHOT_FORCE_LEGACY -and $env:POPSHOT_FORCE_LEGACY -ne '0') {
    $script:HasNetTcpCmdlet = $false
    $script:HasNetUdpCmdlet = $false
    $script:HasNetIpCmdlet  = $false
}

function Test-LegacyWindows {
    # 这台机器是不是要走兼容路径（缺 NetTCPIP 或 PowerShell < 3.0）。
    return (-not $script:HasNetTcpCmdlet) -or ($script:PsMajor -lt 3)
}

function Get-CompatBanner {
    <#
        走兼容路径时给用户看的一行说明；不需要兼容就返回空串。
        调用方自己决定打不打（正常机器上不该多这一行噪音）。
    #>
    if (-not (Test-LegacyWindows)) { return '' }
    if ($env:POPSHOT_FORCE_LEGACY -and $env:POPSHOT_FORCE_LEGACY -ne '0') {
        return '[兼容] POPSHOT_FORCE_LEGACY 已设 —— 强行走老版 Windows 那条路（自测用）。'
    }
    $why = @()
    if (-not $script:HasNetTcpCmdlet) { $why += '没有 NetTCPIP 模块' }
    if ($script:PsMajor -lt 3) { $why += "PowerShell $($script:PsMajor).0" }
    return "[兼容] 检测到老版 Windows（$($why -join '、')）—— 端口查询改走 netstat，功能不变。"
}

# ---------------------------------------------------------------------------
#  端口 -> 占用它的进程 id
# ---------------------------------------------------------------------------

# netstat 一次要几十到几百毫秒（本机实测 ~190 ms），而启动脚本的等端口循环
# 一轮要查好几个端口。缓存一小会儿，让同一轮里的多次查询共用一次 netstat。
#
# ★ TTL 是**自适应**的，不是写死的常数：一开始拿 200 ms 试，之后取
#   `max(200 ms, 上一次 netstat 实际耗时)`。写死 200 ms 踩过坑 —— netstat
#   本身就要 191 ms，缓存刚存进去就快过期了，等端口的循环几乎每次查询都在
#   重跑 netstat，冷启动被拖成几十秒。让 TTL 跟着实际耗时走，最坏情况也只有
#   一半时间花在 netstat 上，而快的机器照样保持 200 ms 的新鲜度。
$script:NetstatMap     = $null
$script:NetstatUdpMap  = $null
$script:NetstatTakenAt = [DateTime]::MinValue
$script:NetstatTtlMs   = 200

function Reset-ListenerCache {
    # 刚杀完进程之类的场合，强制下一次查询重新跑 netstat。
    $script:NetstatMap = $null
    $script:NetstatUdpMap = $null
    $script:NetstatTakenAt = [DateTime]::MinValue
}

function Get-NetstatListenerMap {
    <#
        解析 `netstat -a -n -o`，返回**哈希表** `@{ 端口 = @(进程id, ...) }`。

        ★ 为什么是哈希表而不是「一串 (端口, 进程id) 对」：PowerShell 的管道
          会把数组**拆开**输出，`return @(@(47611,1234))` 在只有一行时到了调用方
          就变成两个标量 47611 和 1234，`foreach` 一遍就错得看不出来。
          哈希表不会被拆，是这里唯一不用小心翼翼的返回形状。

        ★ **不按「状态」那一列的文字判监听**：那一列在部分语言版本里会被翻译。
          改看「外部地址」是不是 `0.0.0.0:0` / `[::]:0` —— 只有监听行长这样，
          这个判据不随语言变。为保险起见也认英文的 LISTENING。

        ★ **绝不给 netstat 加 `2>&1` / `2>$null`**：Windows PowerShell 会把
          原生程序的 stderr 包成 ErrorRecord，配 $ErrorActionPreference='Stop'
          会把调用方整个带走（launch.ps1 里 d3d9 探针那段吃过这个亏）。
    #>
    $now = Get-Date
    if ($null -ne $script:NetstatMap -and
        ($now - $script:NetstatTakenAt).TotalMilliseconds -lt $script:NetstatTtlMs) {
        return $script:NetstatMap
    }
    $started = Get-Date
    $map = @{}
    $udp = @{}
    try {
        foreach ($line in (& netstat.exe -a -n -o)) {
            $text = "$line".Trim()
            $fields = @($text -split '\s+')
            # ★ UDP 行没有「状态」那一列：`UDP  0.0.0.0:27799  *:*  1234`
            #   —— 4 列，进程 id 在最后一列。它没有「监听」这个概念，
            #   只要绑着就占着，所以不需要那套状态判据。
            if ($text.StartsWith('UDP')) {
                if ($fields.Count -lt 4) { continue }
                $local = $fields[1]
                $colon = $local.LastIndexOf(':')
                if ($colon -lt 0) { continue }
                $port = 0
                if (-not [int]::TryParse($local.Substring($colon + 1), [ref]$port)) { continue }
                $owner = 0
                if (-not [int]::TryParse($fields[$fields.Count - 1], [ref]$owner)) { continue }
                if (-not $udp.ContainsKey($port)) { $udp[$port] = @() }
                if ($udp[$port] -notcontains $owner) { $udp[$port] += $owner }
                continue
            }
            if (-not $text.StartsWith('TCP')) { continue }
            if ($fields.Count -lt 5) { continue }
            if (-not ($fields[2].EndsWith(':0') -or $fields[3] -eq 'LISTENING')) { continue }
            $local = $fields[1]
            $colon = $local.LastIndexOf(':')
            if ($colon -lt 0) { continue }
            $port = 0
            if (-not [int]::TryParse($local.Substring($colon + 1), [ref]$port)) { continue }
            $owner = 0
            if (-not [int]::TryParse($fields[4], [ref]$owner)) { continue }
            if (-not $map.ContainsKey($port)) { $map[$port] = @() }
            if ($map[$port] -notcontains $owner) { $map[$port] += $owner }
        }
    } catch {
        $map = @{}
        $udp = @{}
    }
    # 快照的时刻按 netstat **跑完**算：它自己那 190 ms 里的信息本来就是旧的，
    # 从开始时刻算会让缓存刚存进去就过期（见上面 TTL 那段注释）。
    $finished = Get-Date
    $costMs = ($finished - $started).TotalMilliseconds
    if ($costMs -gt $script:NetstatTtlMs) { $script:NetstatTtlMs = [int]$costMs }
    $script:NetstatMap = $map
    $script:NetstatUdpMap = $udp
    $script:NetstatTakenAt = $finished
    return $map
}

function Get-NetstatUdpMap {
    <# `@{ 端口 = @(进程id, ...) }`，UDP 版。和 TCP 那份共用**同一次** netstat。 #>
    $null = Get-NetstatListenerMap        # 顺带把 UDP 那份也填好（或命中缓存）
    if ($null -eq $script:NetstatUdpMap) { return @{} }
    return $script:NetstatUdpMap
}

function Get-UdpListenerPid {
    <#
        绑着这个 UDP 端口的进程 id；没有就返回 $null。

        ★ 返回值语义和 `Get-ListenerPid` 一致（标量 / 数组 / $null）。

        ★ UDP 没有「监听状态」—— 一个 socket 只要 bind 了就占着这个端口，
          所以判据比 TCP 简单：查得到就是有人占。
    #>
    param([Parameter(Mandatory = $true)][int]$Port)

    if ($script:HasNetUdpCmdlet) {
        $ep = Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue
        if ($ep) { return ($ep | Select-Object -ExpandProperty OwningProcess -Unique) }
        return $null
    }
    $map = Get-NetstatUdpMap
    if (-not $map.ContainsKey($Port)) { return $null }
    $ids = @($map[$Port])
    if ($ids.Count -eq 0) { return $null }
    return ($ids | Select-Object -Unique)
}

function Get-PortOwnerName {
    <# 占用者的进程名，查不到就返回 '?'。只用来把报错说清楚。 #>
    param($OwnerPid)
    $first = @($OwnerPid)[0]
    if (-not $first) { return '?' }
    try {
        $p = Get-Process -Id ([int]$first) -ErrorAction Stop
        return $p.ProcessName
    } catch {
        return '?'
    }
}

function Test-PortsFree {
    <#
        检查一组端口是否**全部空着**。返回值是「占用说明」的数组，
        空数组 = 全空。

        入参形如：
            @( @{ Port = 27799; Proto = 'TCP'; Label = '游戏服' },
               @{ Port = 27799; Proto = 'UDP'; Label = '位置同步' } )

        ★ 为什么 TCP 和 UDP 要分开查：它们是两套独立的端口空间，
          27799/tcp 空着完全不代表 27799/udp 空着。只查 TCP 的话，
          位置数据那条 UDP 通道会在「端口被别的程序占了」时**静默失效** ——
          而 UDP 没有回执，在外面根本看不出来。
    #>
    param([Parameter(Mandatory = $true)][object[]]$Specs)

    $busy = @()
    foreach ($spec in $Specs) {
        $port = [int]$spec.Port
        $proto = "$($spec.Proto)".ToUpper()
        if ($proto -eq 'UDP') { $owner = Get-UdpListenerPid $port }
        else { $owner = Get-ListenerPid $port }
        if (-not $owner) { continue }
        $name = Get-PortOwnerName $owner
        $label = if ($spec.Label) { "（$($spec.Label)）" } else { '' }
        $busy += "$proto $port$label 被 pid=$(@($owner) -join ',') ($name) 占用"
    }
    return $busy
}

function Get-ListenerPid {
    <#
        监听这个端口的进程 id；没有就返回 $null。

        返回值语义和原来那份基于 `Get-NetTCPConnection` 的实现**一模一样**：
        一个进程时是标量，多个时是数组，没有时是 $null。调用方不用改。
    #>
    param([Parameter(Mandatory = $true)][int]$Port)

    if ($script:HasNetTcpCmdlet) {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
        if ($conn) { return ($conn | Select-Object -ExpandProperty OwningProcess -Unique) }
        return $null
    }

    $map = Get-NetstatListenerMap
    if (-not $map.ContainsKey($Port)) { return $null }
    $ids = @($map[$Port])
    if ($ids.Count -eq 0) { return $null }
    return ($ids | Select-Object -Unique)
}

# ---------------------------------------------------------------------------
#  本机的局域网 IPv4 地址（服务端包启动后要告诉玩家往 server.config 里填什么）
# ---------------------------------------------------------------------------
function Get-LocalIPv4List {
    $addrs = @()
    if ($script:HasNetIpCmdlet) {
        try {
            $addrs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.PrefixOrigin -ne 'WellKnown' } |
                Select-Object -ExpandProperty IPAddress -Unique)
        } catch {
            $addrs = @()
        }
    }
    if ($addrs.Count -eq 0) {
        # 老系统的退路：.NET 2.0 就有的 DNS 查询。拿到的是本机所有 IPv4，
        # 环回和 169.254 自动配置地址（没拿到 DHCP 时才有）都不该给玩家看。
        try {
            $addrs = @([System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
                Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
                ForEach-Object { $_.IPAddressToString } |
                Where-Object { $_ -ne '127.0.0.1' -and -not $_.StartsWith('169.254.') } |
                Select-Object -Unique)
        } catch {
            $addrs = @()
        }
    }
    return @($addrs)
}

# ---------------------------------------------------------------------------
#  文件读取（替掉 PowerShell 3.0 才有的 -Raw / -Tail）
# ---------------------------------------------------------------------------
function Read-TextFileRaw {
    # 整个文件读成一个字符串；文件不在或读不了都返回空串（替 `Get-Content -Raw`）。
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        # ReadAllText 自己认 BOM，比 Get-Content -Raw 还稳（.server_mode 是带
        # BOM 的 UTF-8，Set-Content -Encoding utf8 写的）。
        $text = [System.IO.File]::ReadAllText($Path)
        # 保险：万一哪个 .NET 版本没吃掉 BOM，这里补一刀。留着 BOM 会让
        # `(Read-TextFileRaw $ModeFile).Trim() -eq 'normal'` 恒为假 —— .NET 4+
        # 的 Trim() 不把 U+FEFF 当空白，服务端会被白白重启一次。
        if ($text.Length -gt 0 -and [int]$text[0] -eq 0xFEFF) {
            $text = $text.Substring(1)
        }
        return $text
    } catch {
        return ''
    }
}

function Move-LogAside {
    # 启动前把上一次那份日志改名归档，**同一天多次重启也不会互相覆盖**
    # （用户 2026-09-01：「server.out、relay.out、连接抓包等日志我希望不要被
    #  覆盖、只清理过期日志」）。
    #
    # 为什么非得改名不可：`Start-Process -RedirectStandardOutput` 是 `>` 语义
    # （截断新建），PowerShell 没有「追加」模式。所以在起进程**之前**把旧的
    # 挪走，新进程照旧写 `server.out` —— 文档、`launch.ps1` 读 server.err 尾巴
    # 的失败诊断、`logcleanup` 的白名单全都不用改。
    #
    # 归档名用**文件自己的最后修改时间**，不是「现在」：
    #   1. 名字标的是那次运行**结束**的时刻，比标归档时刻有意义；
    #   2. `Move-Item` 保留 mtime ⇒ `logcleanup` 按 mtime 老化立刻就对，
    #      不会因为「刚归档过」而白白多留 3 天（`eventlog.py` 开头讲的就是
    #      这个坑）。
    #
    # 挪不动（正被别的进程开着、目标重名）就**原样返回**，让调用方照旧覆盖 ——
    # 归档不值得为它冒「服务端起不来」的险，和 `eventlog._rotate_unlocked`
    # 是同一个取舍。
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        # 0 字节的（上次启动就没写出东西的 .err）不留，免得攒一堆空文件。
        if ($item.Length -le 0) { return $false }
        $stamp  = $item.LastWriteTime.ToString('yyyyMMdd-HHmmss')
        $stem   = [System.IO.Path]::GetFileNameWithoutExtension($item.Name)
        $ext    = $item.Extension
        $target = Join-Path $item.DirectoryName ("{0}-{1}{2}" -f $stem, $stamp, $ext)
        if (Test-Path -LiteralPath $target) { return $false }
        Move-Item -LiteralPath $Path -Destination $target -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Get-FileTailLines {
    # 文件末尾 N 行（替 `Get-Content -Tail N`）。文件不在就返回空数组。
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Count = 20
    )
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    try {
        return @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue |
                 Select-Object -Last $Count)
    } catch {
        return @()
    }
}

# ---------------------------------------------------------------------------
#  杂项
# ---------------------------------------------------------------------------
function Get-ProcessIdListText {
    <#
        一串进程对象 -> "1234,5678"。

        ★ 别写 `$procs.Id -join ','`：数组的成员枚举是 PowerShell 3.0 才有的，
          PowerShell 2.0 上 `$数组.Id` 是 $null，日志里的 pid 会变成空。
    #>
    param($Processes)
    if (-not $Processes) { return '' }
    return ((@($Processes) | ForEach-Object { $_.Id }) -join ',')
}

function Format-ErrorLocationText {
    <#
        错误记录 -> 「位置 文件:行  语句」+「异常 类型」两行说明，给 launch.ps1 /
        serverctl.ps1 的 trap 用。

        ★ 为什么要有它：trap 里光打 `$_.Exception.Message` 时，「拒绝访问。」这种
          三个字的消息没法远程定位（2026-08-19 Win7 玩家那次，只能靠逐条排除
          才 narrowing 到 Start-Process）。带上文件、行号和出错语句，玩家截个
          图就能看出炸在哪。

        InvocationInfo 在 PowerShell 2.0 就有；但 .NET 直接抛、没有脚本上下文的
        错它是空的 —— 那种就只剩「异常 类型」一行。
    #>
    param($ErrorRecord)
    $lines = @()
    $info = $ErrorRecord.InvocationInfo
    if ($info -and $info.ScriptLineNumber -gt 0) {
        # ScriptName 可能为空（-Command 方式运行的错不在任何文件里），
        # Split-Path -Leaf '' 会炸，给个兜底标签。
        $where = '（命令行）'
        if ("$($info.ScriptName)") { $where = Split-Path -Leaf "$($info.ScriptName)" }
        $stmt  = "$($info.Line)".Trim()
        if ($stmt.Length -gt 100) { $stmt = $stmt.Substring(0, 100) + '…' }
        $lines += "位置 ${where}:$($info.ScriptLineNumber)  $stmt"
    }
    $lines += "异常 $($ErrorRecord.Exception.GetType().Name)"
    return ($lines -join [Environment]::NewLine)
}

function Get-WindowsBuildMajor {
    <#
        Windows 主版本号（Win7 = 6，Win10/11 = 10）。

        取的是 kernel32.dll 的文件版本，**不是** [Environment]::OSVersion ——
        后者对没有兼容性清单的进程会谎报 6.2。拿不到就退回 OSVersion。

        ★ `POPSHOT_FORCE_LEGACY=1` 时直接返回 6（假装 Win7）。开关下沉到这里，
          是为了让**所有**按版本分支的地方（挑运行时、serverctl 的「不支持老系统」
          提示）在 Win10 上都能真跑一遍 —— 开关只挂在调用方的话，测得到一处
          就漏得掉另一处，而漏掉的那处正是玩家第一眼会看到的东西。
    #>
    if ($env:POPSHOT_FORCE_LEGACY -and $env:POPSHOT_FORCE_LEGACY -ne '0') { return 6 }
    try {
        $k32 = Join-Path $env:SystemRoot 'System32\kernel32.dll'
        if (Test-Path -LiteralPath $k32) {
            return (Get-Item -LiteralPath $k32).VersionInfo.ProductMajorPart
        }
    } catch {
    }
    try {
        return [Environment]::OSVersion.Version.Major
    } catch {
        return 0
    }
}

function Select-PythonRuntime {
    <#
        挑这台机器该用哪份 Python，返回**哈希表**
        `@{ Path = 'python.exe 的路径'; IsLegacy = $true/$false; Notes = @('要打的话') }`。

        ★ 为什么必须挑：主力运行时是 **CPython 3.14**，官方**只支持 Windows 10 及以上**
          （3.9 起砍掉 Win7，3.13 起砍掉 Win8.1）。在更老的系统上它一跑就弹
          「缺少 api-ms-win-core-path-l1-1-0.dll」，而且是**模态框**，会把启动脚本
          直接卡死 —— 所以绝不能「先跑跑试试」，只能按系统版本提前决定。

        ★ 判据是 kernel32.dll 的**文件版本**（`Get-WindowsBuildMajor`），
          不是 `[Environment]::OSVersion` —— 后者对没有兼容性清单的进程谎报 6.2。

        `runtime-win7\` 里那份是 **CPython 3.8.10 win32**（最后一个支持 Win7 的版本，
        797 项服务端测试在它上面全绿，见 `runtime-win7\README.md`）。

        返回哈希表而不是裸字符串，是因为 PowerShell 的管道会把数组拆开 ——
        这里要同时带回「路径」「走没走老路」「要打的话」三样。

        ★ **只有客户端包用它。** 服务端包故意不带 `runtime-win7\`（架服务端不
          考虑老系统，D133），`serverctl.ps1` 直接用 `runtime-win\` 那一份，
          并在老系统上打一句「不支持」了事。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Modern,   # runtime\python\python.exe
        [Parameter(Mandatory = $true)][string]$Legacy    # runtime-win7\python\python.exe
    )
    $result = @{ Path = $Modern; IsLegacy = $false; Notes = @() }

    # `Get-WindowsBuildMajor` 自己认 POPSHOT_FORCE_LEGACY，这里不用重复判。
    if ((Get-WindowsBuildMajor) -ge 10) { return $result }

    if (Test-Path -LiteralPath $Legacy) {
        $result.Path = $Legacy
        $result.IsLegacy = $true
        $result.Notes = @('[兼容] 老版 Windows —— 改用包内的 Python 3.8 运行时（runtime-win7）。')
        return $result
    }

    # 该走老路却没带老运行时：多半是拿了旧包。说清楚，别让他对着模态框猜。
    $result.Notes = @(
        '!! 这台电脑的 Windows 版本低于 Windows 10，而这个包里**没有** runtime-win7\。',
        '   主力运行时 Python 3.14 只支持 Win10 及以上，接下来多半会弹',
        '   「缺少 api-ms-win-core-path-l1-1-0.dll」并卡住 ——',
        '   请换用带 runtime-win7\ 的新包（BUILD.ver 里 win7Runtime 会写「已包含」）。'
    )
    return $result
}
