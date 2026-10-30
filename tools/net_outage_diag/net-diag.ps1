<#
炮炮火枪手 · 网络诊断（bug调查/29：打着打着弹「发生服务器障碍」、被踢回大厅）

给出问题的玩家跑，做三件事：
  1. 导出电脑最近几天自己记下的断网相关事件（Wi-Fi / 网线断开、网络切换、DHCP、睡眠）；
  2. 记下网卡、Wi-Fi 信号、到游戏服务器的线路（tracert）；
  3. 一直开着：每秒 ping 一次「路由器」「公网」「游戏服务器」，谁断了、断多久都记下来；
     游戏服务器一断，当场再追一次线路，看断在第几跳。

用法：双击 net-diag.bat，开着它去玩；出过问题之后回到这个窗口按 Q，
把生成的 zip（没有 zip 就把「网络诊断结果-…」整个文件夹）发给服主。

不收集：MAC 地址、Wi-Fi 名称（SSID / BSSID）、账号密码、浏览记录。
#>
param(
    [string]$Server = "",
    [int]$Hours = 72,
    [int]$StopAfterSeconds = 0     # 0 = 一直跑到按 Q；给个秒数就到点自己收尾（服主自己试跑用）
)

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$outDir = Join-Path $here ("网络诊断结果-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$utf8Bom = New-Object System.Text.UTF8Encoding($true)

function Open-Writer([string]$name) {
    $writer = New-Object System.IO.StreamWriter((Join-Path $outDir $name), $false, $utf8Bom)
    $writer.AutoFlush = $true      # 玩家直接点 ✕ 关窗口，写过的也都在盘上
    return $writer
}

function Stamp([datetime]$at) { return $at.ToString("yyyy-MM-dd HH:mm:ss.fff") }

# ---------------------------------------------------------------------------
# 游戏服务器地址：先找游戏目录里的 config\server.config（脚本放在游戏目录或它下面几层都行）
# ---------------------------------------------------------------------------
$serverFrom = "命令行参数"
if (-not $Server) {
    $dir = $here
    for ($i = 0; $i -lt 4 -and $dir; $i++) {
        $cfg = Join-Path $dir "config\server.config"
        if (Test-Path $cfg) {
            foreach ($line in [IO.File]::ReadAllLines($cfg)) {
                $text = $line.Trim().TrimStart([char]0xFEFF)
                if ($text -match '^server_address\s*=\s*(.+)$') {
                    $Server = $matches[1].Trim().Trim('[', ']')
                    $serverFrom = $cfg
                    break
                }
            }
        }
        if ($Server) { break }
        $dir = Split-Path -Parent $dir
    }
}
if (-not $Server) {
    $Server = (Read-Host "没找到游戏的 config\server.config，请输入游戏服务器地址（问服主要）").Trim()
    $serverFrom = "手动输入"
}

# 路由器（默认网关）：WMI 在 Win7 上也有
$adapters = @(Get-WmiObject Win32_NetworkAdapterConfiguration -Filter "IPEnabled=TRUE" -ErrorAction SilentlyContinue)
$gateway = $null
foreach ($adapter in $adapters) {
    foreach ($candidate in @($adapter.DefaultIPGateway)) {
        if ($candidate -match '^\d+\.\d+\.\d+\.\d+$') { $gateway = $candidate; break }
    }
    if ($gateway) { break }
}

Write-Host ""
Write-Host "=== 炮炮火枪手 网络诊断 ===" -ForegroundColor Cyan
Write-Host "结果存在：$outDir"
Write-Host "游戏服务器：$Server（来自 $serverFrom）"
Write-Host "路由器：$(if ($gateway) { $gateway } else { '没找到默认网关' })"
Write-Host ""

# ---------------------------------------------------------------------------
# 1. 这台电脑的网络现状
# ---------------------------------------------------------------------------
Write-Host "[1/3] 记录网卡和 Wi-Fi 信息……"
$info = Open-Writer "电脑信息.txt"
$now = Get-Date
$info.WriteLine("记录时刻：$($now.ToString('yyyy-MM-dd HH:mm:ss zzz'))  时区：$([TimeZoneInfo]::Local.DisplayName)")
try {
    $os = Get-WmiObject Win32_OperatingSystem
    $info.WriteLine("系统：$($os.Caption) $($os.Version)")
} catch { }
$info.WriteLine("游戏服务器：$Server（来自 $serverFrom）")
$info.WriteLine("")
$info.WriteLine("--- 启用的网卡 ---")
foreach ($nic in @(Get-WmiObject Win32_NetworkAdapter -Filter "NetEnabled=TRUE" -ErrorAction SilentlyContinue)) {
    $speed = if ($nic.Speed) { "{0:N0} Mbps" -f ($nic.Speed / 1e6) } else { "?" }
    $info.WriteLine("$($nic.NetConnectionID)：$($nic.Name)，连接速率 $speed")
}
$info.WriteLine("")
$info.WriteLine("--- IP 配置 ---")
foreach ($adapter in $adapters) {
    $v4 = @($adapter.IPAddress) | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' }
    $info.WriteLine("$($adapter.Description)")
    $info.WriteLine("    IPv4 $($v4 -join ', ')  网关 $(@($adapter.DefaultIPGateway) -join ', ')  DNS $(@($adapter.DNSServerSearchOrder) -join ', ')  DHCP $($adapter.DHCPEnabled)")
}
$info.WriteLine("")
$info.WriteLine("--- Wi-Fi（用网线的话这里是空的；已去掉 Wi-Fi 名称和物理地址）---")
try {
    foreach ($line in @(netsh wlan show interfaces 2>$null)) {
        if ($line -match 'SSID|物理地址|Physical address|配置文件|Profile|GUID') { continue }
        if ($line.Trim()) { $info.WriteLine($line) }
    }
} catch { $info.WriteLine("（读不到：$($_.Exception.Message)）") }
$info.Close()

# ---------------------------------------------------------------------------
# 2. 电脑自己记下的断网事件（不用等下次出事，以前那几次也在里面）
# ---------------------------------------------------------------------------
Write-Host "[2/3] 导出最近 $Hours 小时的断网相关系统事件……"
$since = (Get-Date).AddHours(-$Hours)
$events = New-Object System.Collections.ArrayList
$notes = New-Object System.Collections.ArrayList

# ★ 不用 Get-WinEvent：它一碰到「描述资源坏掉」的那一条，整个日志一条都不给
#   （10-25 玩家那台的 System 日志就是这样，报「找不到映像文件中指定的资源类型」）。
#   逐条读、描述读不出来的那条单独记「（描述读不出）」，别的照常。
function Read-Channel([string]$channel, [scriptblock]$keep) {
    $utc = $since.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    $kept = 0
    try {
        $query = New-Object System.Diagnostics.Eventing.Reader.EventLogQuery($channel,
            [System.Diagnostics.Eventing.Reader.PathType]::LogName,
            "*[System[TimeCreated[@SystemTime>='$utc']]]")
        $reader = New-Object System.Diagnostics.Eventing.Reader.EventLogReader($query)
    } catch {
        [void]$notes.Add("${channel}：读不到（$($_.Exception.Message)）")
        return
    }
    try {
        while ($true) {
            try { $record = $reader.ReadEvent() } catch {
                # 读到坏记录就停在这儿（前面读到的照留），不在一条坏记录上原地打转
                [void]$notes.Add("${channel}：读到一半出错，后面的没读（$($_.Exception.Message)）")
                break
            }
            if ($null -eq $record) { break }
            try {
                if (& $keep $record) {
                    $text = $null
                    try { $text = $record.FormatDescription() } catch { $text = "（描述读不出）" }
                    [void]$events.Add((New-Object PSObject -Property @{
                        Time = $record.TimeCreated; Log = $channel; Provider = $record.ProviderName
                        Id = $record.Id; Text = $text }))
                    $kept++
                }
            } finally { $record.Dispose() }
        }
    } finally { $reader.Dispose() }
    if ($kept -eq 0) { [void]$notes.Add("${channel}：这段时间没有记录") }
}

# 事件描述里夹着 Wi-Fi 名称（SSID / 配置文件名 / 网络名）和网卡物理地址，写盘前去掉
function Hide-Private([string]$text) {
    $text = $text -replace '(?i)\((十六进制的 SSID 值|Hex Value of SSID)[^)]*\)', ''
    $text = $text -replace '(?i)(服务集标识符\(SSID\)|with SSID)\s*\S+', '$1 <略>'
    $text = $text -replace '(?i)(BSSID|SSID|配置文件名称|Profile Name|名称|描述|Name|Desc)\s*[:：]\s*\S+', '$1: <略>'
    $text = $text -replace '(?i)\b0x[0-9a-f]{12}\b', '<MAC>'
    $text = $text -replace '(?i)\b([0-9a-f]{2}[-:]){5}[0-9a-f]{2}\b', '<MAC>'
    return $text
}

$takeAll = { param($record) $true }
foreach ($channel in @(
    "Microsoft-Windows-WLAN-AutoConfig/Operational",
    "Microsoft-Windows-Wired-AutoConfig/Operational",
    "Microsoft-Windows-NetworkProfile/Operational",
    "Microsoft-Windows-NCSI/Operational",
    "Microsoft-Windows-Dhcp-Client/Admin",
    "Microsoft-Windows-TCPIP/Operational")) {
    Read-Channel $channel $takeAll
}
# System 日志：网卡驱动 / 网络服务 / 电源这几类来源全留，别的来源只留「错误」「严重」
$netProviders = '(?i)tcpip|ndis|wlan|wired|dhcp|dns|netbt|nlasvc|networkprofile|ncsi|iphlpsvc|kernel-power|power-troubleshooter|vwifi|^netw|^e1|^e2|^rt|^athw|^ath|^qcwlan|^mrvl|^bcm|^b57|^mtk|^mt7|^ixgbe|^igc|^l1c|killer|realtek|qualcomm|atheros|broadcom|mediatek'
Read-Channel "System" { param($record) $record.ProviderName -match $netProviders -or ($record.Level -ge 1 -and $record.Level -le 2) }

$eventFile = Open-Writer "系统事件.txt"
$eventFile.WriteLine("从 $($since.ToString('yyyy-MM-dd HH:mm:ss')) 到现在，共 $($events.Count) 条（本机时间；Wi-Fi 名称和物理地址已去掉）")
foreach ($note in $notes) { $eventFile.WriteLine("（$note）") }
$eventFile.WriteLine("")
foreach ($entry in ($events | Sort-Object Time)) {
    $message = $entry.Text
    if (-not $message) { $message = "（无描述）" }
    $message = Hide-Private (($message -replace '\s+', ' ').Trim())
    if ($message.Length -gt 400) { $message = $message.Substring(0, 400) + "…" }
    $log = ($entry.Log -replace '^Microsoft-Windows-', '')
    # 外面那层括号不能省：方法调用里 `-f a, b` 的逗号会被当成 WriteLine 的多个参数
    $eventFile.WriteLine(("{0}  [{1}] {2} #{3}  {4}" -f (Stamp $entry.Time), $log, $entry.Provider, $entry.Id, $message))
}
$eventFile.Close()
Write-Host "      共 $($events.Count) 条"

# ---------------------------------------------------------------------------
# 3. 一直盯着：每秒 ping 三个点
# ---------------------------------------------------------------------------
# 线路追踪放后台跑（一次十几到几十秒，不能挡着 ping）：开头一次当基准，之后每次游戏服务器
# 从通变断再追一次。同一时刻只跑一个；跑完（任务结束这个事件）才收结果。
$traceFile = Open-Writer "线路追踪.txt"
$script:traceJob = $null
$script:traceWhy = ""
function Start-Trace([string]$why) {
    if ($script:traceJob) { return }
    $script:traceWhy = "$(Stamp (Get-Date))  $why"
    $script:traceJob = Start-Job -ScriptBlock { param($target) & tracert.exe -d -h 20 -w 800 $target } -ArgumentList $Server
}
function Receive-Trace([bool]$wait) {
    if (-not $script:traceJob) { return }
    if ($wait) { [void](Wait-Job $script:traceJob) }
    if ($script:traceJob.State -eq "Running") { return }
    $traceFile.WriteLine("=== $($script:traceWhy) ===")
    foreach ($line in @(Receive-Job $script:traceJob)) { $traceFile.WriteLine($line) }
    $traceFile.WriteLine("")
    Remove-Job $script:traceJob -Force
    $script:traceJob = $null
}

function New-Target([string]$name, [string]$address) {
    return New-Object PSObject -Property @{
        Name = $name; Address = $address; Sent = 0; Lost = 0
        Down = $false; DownSince = $null; Run = 0; Outages = 0; Longest = 0.0
    }
}
$targets = @()
if ($gateway) { $targets += New-Target "路由器" $gateway }
$targets += New-Target "公网(阿里DNS)" "223.5.5.5"
$targets += New-Target "游戏服务器" $Server

$csv = Open-Writer "每秒ping.csv"
$csv.WriteLine("时间,目标,地址,结果,毫秒")
$flips = Open-Writer "断线记录.txt"
function Note([string]$text, [string]$color) {
    Write-Host $text -ForegroundColor $color
    $flips.WriteLine($text)
}

Start-Trace "开始时的基准线路"
$pinger = New-Object System.Net.NetworkInformation.Ping
Write-Host "[3/3] 开始监测：路由器 / 公网 / 游戏服务器 每秒各 ping 一次。" -ForegroundColor Green
Write-Host "      开着这个窗口去玩（可以最小化）。出过问题之后回到这里按 Q 结束。" -ForegroundColor Green
Write-Host ""

$startAt = Get-Date
while ($true) {
    $roundAt = Get-Date
    foreach ($target in $targets) {
        $at = Get-Date
        $ok = $false; $ms = ""; $status = ""
        try {
            # ★ 1 秒等回包：ICMP 丢了不会有任何回音，只能等一个上限（物理上没有事件可等）。
            $reply = $pinger.Send($target.Address, 1000)
            $status = [string]$reply.Status
            if ($reply.Status -eq [System.Net.NetworkInformation.IPStatus]::Success) { $ok = $true; $ms = $reply.RoundtripTime }
        } catch {
            $inner = $_.Exception.InnerException
            $status = "出错:" + $(if ($inner) { $inner.Message } else { $_.Exception.Message })
        }
        $target.Sent++
        if (-not $ok) { $target.Lost++ }
        $csv.WriteLine(("{0},{1},{2},{3},{4}" -f (Stamp $at), $target.Name, $target.Address, ($status -replace ',', ' '), $ms))

        # 只在「通 ⇄ 断」翻转时说话
        if (-not $ok -and -not $target.Down) {
            $target.Down = $true; $target.DownSince = $at; $target.Run = 1
            Note "$(Stamp $at)  ✗ $($target.Name) $($target.Address) 断了（$status）" "Red"
            if ($target.Name -eq "游戏服务器") { Start-Trace "游戏服务器刚断" }
        } elseif (-not $ok) {
            $target.Run++
        } elseif ($target.Down) {
            $seconds = ($at - $target.DownSince).TotalSeconds
            $target.Down = $false; $target.Outages++
            if ($seconds -gt $target.Longest) { $target.Longest = $seconds }
            Note ("{0}  ✓ {1} 恢复了：断了 {2:N1} 秒，连丢 {3} 个" -f (Stamp $at), $target.Name, $seconds, $target.Run) "Yellow"
        }
    }
    Receive-Trace $false
    if ($StopAfterSeconds -gt 0 -and ((Get-Date) - $startAt).TotalSeconds -ge $StopAfterSeconds) { break }
    try {
        if ([Console]::KeyAvailable -and [Console]::ReadKey($true).Key -eq "Q") { break }
    } catch { }
    # ★ 每秒一轮：探测本身就是「没有事件可等」的那一类，节拍只决定记录的细度。
    $left = 1000 - ((Get-Date) - $roundAt).TotalMilliseconds
    if ($left -gt 0) { Start-Sleep -Milliseconds ([int]$left) }
}

# ---------------------------------------------------------------------------
# 收尾：汇总、等线路追踪跑完、打包
# ---------------------------------------------------------------------------
$end = Get-Date
foreach ($target in $targets) {
    if ($target.Down) {
        $seconds = ($end - $target.DownSince).TotalSeconds
        $target.Outages++
        if ($seconds -gt $target.Longest) { $target.Longest = $seconds }
        Note ("{0}  … {1} 到结束都没恢复（已断 {2:N1} 秒）" -f (Stamp $end), $target.Name, $seconds) "Red"
    }
}
$summary = Open-Writer "汇总.txt"
$summary.WriteLine("监测结束：$(Stamp $end)")
foreach ($target in $targets) {
    $rate = if ($target.Sent) { 100.0 * $target.Lost / $target.Sent } else { 0 }
    $line = "{0}（{1}）：发 {2} 个，丢 {3} 个（{4:N1}%），断过 {5} 次，最长 {6:N1} 秒" -f $target.Name, $target.Address, $target.Sent, $target.Lost, $rate, $target.Outages, $target.Longest
    $summary.WriteLine($line)
    Write-Host $line
}
$summary.Close()
Write-Host "正在等最后一次线路追踪跑完……"
Receive-Trace $true
$traceFile.Close(); $csv.Close(); $flips.Close()

$zip = "$outDir.zip"
if (Get-Command Compress-Archive -ErrorAction SilentlyContinue) {
    try {
        Compress-Archive -Path $outDir -DestinationPath $zip -Force
        Write-Host ""
        Write-Host "已打包：$zip" -ForegroundColor Cyan
        Write-Host "把这个 zip 发给服主就行。" -ForegroundColor Cyan
    } catch {
        Write-Host "打包失败（$($_.Exception.Message)），请把这个文件夹整个发给服主：$outDir" -ForegroundColor Cyan
    }
} else {
    Write-Host "请把这个文件夹整个压缩后发给服主：$outDir" -ForegroundColor Cyan
}
if ($StopAfterSeconds -le 0) { Read-Host "按回车关闭" }
