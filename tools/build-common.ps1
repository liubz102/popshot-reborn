<#
    build-common.ps1 —— 两个打包脚本共用的底座（点源引入，不单独执行）。

        tools\build-portable.ps1        客户端包
        tools\build-server-package.ps1  服务端包

    ★ 为什么要有这个文件：CLAUDE.md 铁律 8 —— 单机假服务器和云端服务端是
      **同一套 server/ 代码**。两个包各自维护一份「要拷哪些文件」的清单，
      迟早会漂成两份不一样的实现。所以文件选择、冒烟自检、ZIP、编码转换
      全部收在这里，两个打包脚本只负责各自的目录形状。

    ★ 上一版 build-portable.ps1 就是死在「手写文件清单」上的：V0.2 新增的
      app.py / config.py / relay.py / relayserver.py / lobby.py / tickets.py /
      eventlog.py / netlisten.py / web\ 一个都没进包。所以这里改成
      **反向排除**：server\*.py 默认全收，只剔掉测试和开发工具，
      以后再加模块不用改打包脚本。
#>

Set-StrictMode -Version 2.0

# --- server/ 里【不】进发布包的东西 ----------------------------------------
# 其余 .py 一律进包（见文件头的理由）。
$script:ServerExcludeExact = @(
    'run_tests.py',         # 测试入口，发布包里没有 test_*.py 可跑
    'capture_server.py'     # 阶段 3/4 的抓包骨架，纯开发工具
)
$script:ServerExcludePattern = @(
    'test_*.py'
)

# 任何地方都不拷的目录/文件名。
$script:JunkNames = @('__pycache__', '.pytest_cache', '.mypy_cache')

# ---------------------------------------------------------------------------
#  基础工具
# ---------------------------------------------------------------------------

function New-BuildId {
    <# 打包批次号。客户端包和服务端包在同一次构建里拿到**同一个** id，
       用来在事后核对「这两个包是不是配对的」（D079：必须成对发）。 #>
    return (Get-Date -Format 'yyyyMMdd-HHmmss')
}

function Assert-EmptyTarget([string]$Path, [switch]$Force) {
    if (Test-Path -LiteralPath $Path) {
        if (-not $Force) {
            throw "输出目录已经存在，请先改名/删除，或加 -Force 覆盖：$Path"
        }
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Assert-InsideDist([string]$Path, [string]$DistRoot) {
    <# 输出必须落在本项目的 dist\ 下。打包脚本会 Remove-Item -Recurse，
       写错一个路径就可能删掉别的东西 —— 这道闸不能省。 #>
    $prefix = $DistRoot.TrimEnd('\') + '\'
    if (-not $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "输出目录必须位于 $DistRoot 之下：$Path"
    }
}

function Copy-One([string]$Source, [string]$Target) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "缺少必需文件：$Source"
    }
    $parent = Split-Path -Parent $Target
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $Source -Destination $Target -Force
}

function Copy-TreeFiltered {
    <# 递归复制，途中跳过 __pycache__ 之类的垃圾目录。
       Copy-Item -Recurse -Exclude 只作用于叶子节点，挡不住目录，所以自己走。 #>
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target,
        [string[]]$ExcludeNames = @()
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "缺少必需目录：$Source"
    }
    $skip = @($script:JunkNames + $ExcludeNames)
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        if ($skip -contains $item.Name) { continue }
        $dest = Join-Path $Target $item.Name
        if ($item.PSIsContainer) {
            Copy-TreeFiltered -Source $item.FullName -Target $dest -ExcludeNames $ExcludeNames
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $dest -Force
        }
    }
}

function Get-DirectorySize([string]$Path) {
    $m = Get-ChildItem -LiteralPath $Path -Recurse -File -Force | Measure-Object -Property Length -Sum
    if ($m.Sum) { return [int64]$m.Sum }
    return [int64]0
}

function Format-Size([int64]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GiB' -f ($Bytes / 1GB)) }
    return ('{0:N1} MiB' -f ($Bytes / 1MB))
}

# ---------------------------------------------------------------------------
#  server/ 代码：两个包用的是同一份（铁律 8）
# ---------------------------------------------------------------------------

function Get-ServerSourceFile([string]$Root) {
    <# 返回要进包的 server\*.py（相对 server\ 的文件名），已排序、已过滤。 #>
    $dir = Join-Path $Root 'server'
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
        throw "找不到 server 目录：$dir"
    }
    $files = @()
    foreach ($f in (Get-ChildItem -LiteralPath $dir -Filter '*.py' -File | Sort-Object Name)) {
        if ($script:ServerExcludeExact -contains $f.Name) { continue }
        $skip = $false
        foreach ($pat in $script:ServerExcludePattern) {
            if ($f.Name -like $pat) { $skip = $true; break }
        }
        if ($skip) { continue }
        $files += $f.Name
    }
    if ($files.Count -lt 5) {
        throw "server\*.py 只筛出 $($files.Count) 个文件，明显不对，中止打包"
    }
    # 少了这几个，包一定是废的 —— 与其让别人在另一台电脑上踩，不如在这里炸。
    foreach ($must in @('app.py', 'config.py', 'gameserver.py', 'authserver.py',
                        'account_store.py', 'netlisten.py', 'tickets.py',
                        'eventlog.py', 'lobby.py', 'relayserver.py', 'protocol.py',
                        'simple.py')) {
        if ($files -notcontains $must) { throw "server\$must 没被选中，打包脚本的过滤规则坏了" }
    }
    return $files
}

function Copy-ServerCode {
    <# 把 server\ 拷进包：*.py（去掉测试和开发工具）+ web\ + 空的 data\。
       返回拷了哪些文件（相对 server\ 的路径），给 BUILD.txt 用。 #>
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [switch]$IncludeRelayClient      # 客户端包才要 relay.py（本机中继）
    )
    $srcDir = Join-Path $Root 'server'
    $dstDir = Join-Path $PackageRoot 'server'
    New-Item -ItemType Directory -Path $dstDir -Force | Out-Null

    $copied = @()
    foreach ($name in (Get-ServerSourceFile $Root)) {
        # relay.py 是「客户端连远端服务器」的本机一跳，服务端包用不上它。
        if ($name -eq 'relay.py' -and -not $IncludeRelayClient) { continue }
        Copy-One (Join-Path $srcDir $name) (Join-Path $dstDir $name)
        $copied += $name
    }

    # 注册页：__init__.py / server.py / index.html，同样不拷 __pycache__。
    $webSrc = Join-Path $srcDir 'web'
    $webDst = Join-Path $dstDir 'web'
    Copy-TreeFiltered -Source $webSrc -Target $webDst
    foreach ($must in @('__init__.py', 'server.py', 'index.html')) {
        if (-not (Test-Path -LiteralPath (Join-Path $webDst $must) -PathType Leaf)) {
            throw "注册页缺文件：server\web\$must"
        }
        $copied += "web\$must"
    }

    if ($IncludeRelayClient -and ($copied -notcontains 'relay.py')) {
        throw "客户端包必须带 server\relay.py（联机模式的本机中继），但它没被选中"
    }

    New-Item -ItemType Directory -Path (Join-Path $dstDir 'data') -Force | Out-Null
    return $copied
}

function Get-ServerCodeHash([string]$PackageRoot) {
    <# 对包里 server\ 的**共用**代码算一个总哈希。客户端包和服务端包的这一行
       必须一模一样，否则说明它们不是同一次构建出来的（铁律 8 被破坏了）。

       ★ relay.py 不进这个哈希：它是客户端包独有的「本机一跳」，
         服务端包里根本没有。把它算进去两个包永远对不上，这一行就废了。 #>
    $dir = Join-Path $PackageRoot 'server'
    $lines = @()
    foreach ($f in (Get-ChildItem -LiteralPath $dir -Recurse -File -Force |
                    Where-Object { $_.Extension -in @('.py', '.html') -and $_.Name -ne 'relay.py' } |
                    Sort-Object FullName)) {
        $rel = $f.FullName.Substring($dir.Length).TrimStart('\')
        $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
        $lines += "$rel $h"
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '')
    } finally {
        $sha.Dispose()
    }
}

# ---------------------------------------------------------------------------
#  文本文件的三套编码规矩（CLAUDE.md 铁律 3）
# ---------------------------------------------------------------------------

function Write-TextFile {
    <# Kind: bat = CRLF + UTF-8 无 BOM；ps1 = CRLF + UTF-8 有 BOM；
              unix = LF + 无 BOM（.sh / .py / .md / .config）。 #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][ValidateSet('bat', 'ps1', 'unix')][string]$Kind
    )
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $body = $Text -replace "`r`n", "`n"
    if ($Kind -ne 'unix') { $body = $body -replace "`n", "`r`n" }
    $withBom = ($Kind -eq 'ps1')
    $enc = New-Object System.Text.UTF8Encoding($withBom)
    [System.IO.File]::WriteAllText($Path, $body, $enc)
}

function Copy-TextFile {
    <# 从仓库里拷一个文本文件进包，并**强制**按目标类型转换行尾/BOM。
       仓库里本来就是对的，这里是最后一道保险 —— .sh 带上 CR 会直接
       `bad interpreter: /bin/sh^M`，那种错在别人的机器上很难猜。 #>
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][ValidateSet('bat', 'ps1', 'unix')][string]$Kind
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "缺少必需文件：$Source"
    }
    $text = [System.IO.File]::ReadAllText($Source, [System.Text.Encoding]::UTF8)
    $text = $text -replace "^\uFEFF", ''
    Write-TextFile -Path $Target -Text $text -Kind $Kind
}

function Test-AsciiOnly([string]$Path) {
    <# .bat 必须纯 ASCII：chcp 65001 下 cmd 按字符算偏移、按字节 seek，
       任何多字节字符（连 rem 注释里的）都会把后面的命令行拦腰截断（§135 / D074）。#>
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        if ($bytes[$i] -gt 0x7f) {
            $hex = '{0:x2}' -f $bytes[$i]
            throw "$Path 第 $i 字节是非 ASCII（0x$hex）。.bat 只允许 ASCII，中文放 .ps1 里打（FINDINGS §135）"
        }
    }
}

# ---------------------------------------------------------------------------
#  冒烟自检：包里的服务端到底起不起得来
# ---------------------------------------------------------------------------

function Get-WebText([string]$Uri, [int]$TimeoutSec = 120) {
    <# 取一个 URL 的**文本**内容。

       ★ 必须走这个函数，别直接用 `(Invoke-WebRequest ...).Content`：
         `-UseBasicParsing` 对 `application/octet-stream` 返回的 `.Content`
         是 **Byte[] 而不是 String**（GitHub 的 release 资产就是这么下发的）。
         对字节数组做 `-split "`n"` 会变成**逐字节**切 —— 122082 个单字节字符串，
         永远匹配不上任何一行，症状是「SHA256SUMS 里没有 xxx 这一条」，
         看起来像上游少了文件，其实是解析错了（FINDINGS §164）。 #>
    $resp = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec $TimeoutSec
    if ($resp.Content -is [byte[]]) {
        return [System.Text.Encoding]::UTF8.GetString($resp.Content)
    }
    return [string]$resp.Content
}

function Get-FreeTcpPort([int]$Preferred) {
    for ($p = $Preferred; $p -lt ($Preferred + 60); $p++) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue)) {
            return $p
        }
    }
    throw "$Preferred 起连着 60 个端口都被占用了，找不到空闲端口做自检"
}

function Test-TcpPortOpen([int]$Port, [int]$TimeoutMs = 250) {
    <# 主动连一次端口，而不是只查 Get-NetTCPConnection。

       某些受限运行环境里服务端已经 bind/listen、客户端也能连，但系统网络表对当前
       进程不可见，Get-NetTCPConnection 会静默返回空表。冒烟测试关心的是「包里的
       服务真的可连接」，主动探测正好是更直接的验收。 #>
    $client = New-Object System.Net.Sockets.TcpClient
    $wait = $null
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        $wait = $async.AsyncWaitHandle
        if (-not $wait.WaitOne($TimeoutMs)) { return $false }
        try {
            $client.EndConnect($async)
            return $true
        } catch {
            return $false
        }
    } finally {
        if ($wait) { $wait.Close() }
        $client.Close()
    }
}

function Invoke-ServerSmokeTest {
    <# 用**包里那份** Python 跑**包里那份** server\app.py，确认：
         1. 四个监听器都能起来（认证 / 游戏 / 中继 / 注册页）
         2. 注册页真的能返回 200（顺带证明 web\index.html 进包了）
       全程用另一套端口，不会撞上正在开发用的服务端；账号文件指向临时目录，
       不会往包里写 accounts.json。

       这一步是这次改打包脚本的**重点**：上一版打出来的包缺 app.py，
       而缺什么在本机是完全看不出来的 —— 只有拷到别的电脑上才炸。 #>
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$PythonRelative   # 如 runtime\python\python.exe
    )
    $py = Join-Path $PackageRoot $PythonRelative
    if (-not (Test-Path -LiteralPath $py -PathType Leaf)) {
        throw "自检失败：包里没有 $PythonRelative"
    }
    $app = Join-Path $PackageRoot 'server\app.py'
    if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
        throw "自检失败：包里没有 server\app.py"
    }

    $authPort  = Get-FreeTcpPort 47711
    $gamePort  = Get-FreeTcpPort 27899
    $relayPort = Get-FreeTcpPort 27898
    $webPort   = Get-FreeTcpPort 27910
    $ports     = @($authPort, $gamePort, $relayPort, $webPort)

    $work = Join-Path ([System.IO.Path]::GetTempPath()) ("popshot-smoke-" + [System.Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    $accounts = Join-Path $work 'accounts.json'
    $outFile  = Join-Path $work 'smoke.out'
    $errFile  = Join-Path $work 'smoke.err'

    # --no-log-cleanup：自检不是「一次真的开服」，绝不该顺手把打包机
    # logs\ 里的东西删掉（D113 的清理是挂在服务端启动路径上的）。
    $argList = @(
        "`"$app`"", '--no-control', '--no-online-log', '--no-log-cleanup',
        '--auth-port',  "$authPort",
        '--game-port',  "$gamePort",
        '--relay-port', "$relayPort",
        '--web-port',   "$webPort",
        '--accounts',   "`"$accounts`""
    )

    $proc = $null
    try {
        $proc = Start-Process -FilePath $py -WorkingDirectory $PackageRoot `
            -ArgumentList $argList -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile

        $up = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 250
            if ($proc.HasExited) { break }
            $listening = 0
            foreach ($p in $ports) {
                if (Test-TcpPortOpen $p) { $listening++ }
            }
            if ($listening -eq $ports.Count) { $up = $true; break }
        }

        if (-not $up) {
            $tail = ''
            foreach ($f in @($outFile, $errFile)) {
                if (Test-Path -LiteralPath $f) {
                    $lines = Get-Content -LiteralPath $f -Tail 15 -ErrorAction SilentlyContinue
                    if ($lines) { $tail += "`n--- $(Split-Path -Leaf $f) ---`n" + ($lines -join "`n") }
                }
            }
            throw "自检失败：包里的服务端没能起全四个监听器（认证/游戏/中继/注册页）。$tail"
        }

        # 注册页返回 200 —— 证明 server\web\index.html 真的在包里。
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$webPort/" -UseBasicParsing -TimeoutSec 10
        } catch {
            throw "自检失败：注册页起来了但取不到页面（$($_.Exception.Message)）"
        }
        if ($resp.StatusCode -ne 200) {
            throw "自检失败：注册页返回 $($resp.StatusCode)，期望 200"
        }
        if ($resp.Content -notmatch '注册') {
            throw "自检失败：注册页内容不像注册页，server\web\index.html 可能不完整"
        }

        return [pscustomobject]@{
            AuthPort = $authPort; GamePort = $gamePort
            RelayPort = $relayPort; WebPort = $webPort
        }
    } finally {
        if ($proc -and -not $proc.HasExited) {
            try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch {}
            Start-Sleep -Milliseconds 400
        }
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        # 自检跑过一遍 Python，包里会多出 __pycache__。留着是白占地方 ——
        # 服务端包多半要拷到 Linux 上，那边这些 .pyc 完全用不上。
        Get-ChildItem -LiteralPath $PackageRoot -Recurse -Directory -Force -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Sort-Object { $_.FullName.Length } -Descending |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# ---------------------------------------------------------------------------
#  ZIP
# ---------------------------------------------------------------------------

function New-PackageZip {
    <# 有 7-Zip 就用它（Compress-Archive 压 400 MiB 要好几分钟，7z 快一个量级），
       没有再退回 Compress-Archive。 #>
    param(
        [Parameter(Mandatory = $true)][string]$SourceDirectory,
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [switch]$Force
    )
    if (Test-Path -LiteralPath $ZipPath) {
        if (-not $Force) { throw "ZIP 已存在，请先改名/删除，或加 -Force：$ZipPath" }
        Remove-Item -LiteralPath $ZipPath -Force
    }
    $sevenZip = @(
        'C:\SSD\Program\7-Zip\7z.exe',
        'C:\Program Files\7-Zip\7z.exe',
        'C:\Program Files (x86)\7-Zip\7z.exe'
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

    if ($sevenZip) {
        # -mx=5 是速度和体积的平衡点；-bso0 -bsp0 关掉逐文件刷屏。
        $parent = Split-Path -Parent $SourceDirectory
        $leaf = Split-Path -Leaf $SourceDirectory
        $rc = 1
        Push-Location $parent
        try {
            & $sevenZip a -tzip -mx=5 -bso0 -bsp0 -- "$ZipPath" "$leaf" | Out-Null
            $rc = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        if ($rc -ne 0) { throw "7z 打包失败（退出码 $rc）：$ZipPath" }
        return '7-Zip'
    }
    Compress-Archive -LiteralPath $SourceDirectory -DestinationPath $ZipPath -CompressionLevel Optimal
    return 'Compress-Archive'
}

function Show-StaleArchiveWarning([string]$PackageDirectory, [string[]]$Extensions) {
    <# 这次没打压缩包，但上一次的 .zip / .tar.gz 还躺在 dist 里 ——
       很容易顺手把旧的那个发出去。发出去才发现是旧包的代价太大，所以吼一声。 #>
    foreach ($ext in $Extensions) {
        $p = "$PackageDirectory$ext"
        if (Test-Path -LiteralPath $p) {
            $when = (Get-Item -LiteralPath $p).LastWriteTime.ToString('yyyy-MM-dd HH:mm')
            Write-Host "⚠ 旁边还有一个【旧的】压缩包，别发错了：$p（$when）" -ForegroundColor Yellow
            Write-Host '  要更新它就重新跑一次并加 -Zip（菜单里选带压缩包的那几项）。' -ForegroundColor Yellow
        }
    }
}

function New-PackageTarGz {
    <# 服务端包丢给 Linux 云主机时，tar.gz 比 zip 顺手（scp + tar -xzf 一条命令）。
       用 Windows 10 自带的 bsdtar（C:\Windows\System32\tar.exe，1803 起就有）。
       ⚠ 在 Windows 上打的 tar 保不住可执行位，解压后仍然要 chmod +x *.sh
         —— README 里已经写了这一条。 #>
    param(
        [Parameter(Mandatory = $true)][string]$SourceDirectory,
        [Parameter(Mandatory = $true)][string]$TarPath,
        [switch]$Force
    )
    # ★ 必须写死 System32 的那一个，不能 Get-Command tar：装了 Git for Windows
    #   之后 PATH 里常常是 MSYS 的 tar，它把 `D:\...` 当成远程主机
    #   （报 `Cannot connect to D: resolve failed`）。踩过一次。
    $tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $tarExe)) {
        $fallback = Get-Command tar -ErrorAction SilentlyContinue
        if (-not $fallback) { throw "这台机器上没有 tar.exe，打不了 tar.gz（Windows 10 1803+ 才自带）" }
        $tarExe = $fallback.Source
    }
    if (Test-Path -LiteralPath $TarPath) {
        if (-not $Force) { throw "tar.gz 已存在，请先改名/删除，或加 -Force：$TarPath" }
        Remove-Item -LiteralPath $TarPath -Force
    }
    $parent = Split-Path -Parent $SourceDirectory
    $leaf = Split-Path -Leaf $SourceDirectory
    $rc = 1
    Push-Location $parent
    try {
        & $tarExe -czf "$TarPath" -- "$leaf"
        $rc = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($rc -ne 0) { throw "tar 打包失败（退出码 $rc）：$TarPath" }
}

# ---------------------------------------------------------------------------
#  BUILD.txt
# ---------------------------------------------------------------------------

function Write-BuildInfo {
    <# 包里放一份「这是什么包、什么时候打的、代码哈希是多少」。
       测试的人把问题发回来时，第一句就能问「你那份 BUILD.txt 贴一下」。
       ★ 客户端包和服务端包**必须成对使用**（D079），靠这里的批次号核对。 #>
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$Kind,        # 客户端包 / 服务端包
        [Parameter(Mandatory = $true)][string]$BuildId,
        [string[]]$ExtraLines = @()
    )
    $lines = @(
        '炮炮火枪手 —— 打包信息',
        '=======================',
        "包类型      $Kind",
        "打包批次    $BuildId",
        "打包时间    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "打包机器    $env:COMPUTERNAME",
        "共用服务端代码 $(Get-ServerCodeHash $PackageRoot)"
    )
    $lines += $ExtraLines
    $lines += @(
        '',
        '★ 客户端包和服务端包必须【成对】使用：两边的「打包批次」要一致，',
        '  「共用服务端代码」这一行也要一模一样。批次对不上就先各自重新解压一份。',
        '  （原因：客户端里的端口映射表和服务端的中继端口是配套的，',
        '   老客户端连新服务端会在进房间时被弹回大厅。）',
        ''
    )
    Write-TextFile -Path (Join-Path $PackageRoot 'BUILD.txt') -Text ($lines -join "`n") -Kind 'unix'
}
