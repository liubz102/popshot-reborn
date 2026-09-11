<#
    build-server-package.ps1 —— 生成【服务端包】（里程碑 K.2）

    给「开服的人」的包：只有服务端，没有游戏本体、没有 hook。
    解压就能跑，目标机器不需要预装任何东西。

        PopShot-server_V0-2-7/          （目录名带版本号，点转横杠）
        ├─ start.bat / start-debug.bat / stop.bat     Windows
        ├─ start.sh  / start-debug.sh  / stop.sh      Linux
        ├─ tools/serverctl.ps1  tools/serverctl.sh    启停实现（中文都在这里）
        ├─ config/server.config                      注册页端口 / 冷却 / 日志 / 备份 / 崩溃包
        ├─ config/server-ClientFilter.config         允许的最低客户端版本（0=不限制）
        ├─ server/                    和客户端包【同一份】代码（铁律 8）
        ├─ runtime-win/python/        Windows 独立运行时
        ├─ runtime-linux/*.tar.gz     Linux 独立运行时（可选，第一次启动时自解）
        ├─ logs/
        ├─ logs_client_crash/         玩家客户端闪退后自动传上来的崩溃现场
        ├─ BUILD.ver / README.md

    用法：
        build-server-package.ps1                       用项目里的 runtime-linux\（有就带）
        build-server-package.ps1 -Zip                  顺便打 ZIP
        build-server-package.ps1 -TarGz                顺便打 tar.gz（丢给云主机方便）
        build-server-package.ps1 -LinuxRuntime download   项目里没有才联网下（存进项目）
        build-server-package.ps1 -LinuxRuntime none        不带 Linux 运行时
        build-server-package.ps1 -LinuxRuntime D:\x.tar.gz 用指定的那份

    ★ Linux 运行时**只下载一次**，存在项目的 `runtime-linux\` 里（和 `runtime\`
      同一个角色，跟着 git 走）。以后每次打包都是直接 copy，不联网。

    ★ Linux 运行时故意**不解压**进包：打包在 Windows 上做，解开会丢掉
      符号链接（python3 -> python3.14）和可执行位，解出来跑不了。
      压缩包原样放进去，`serverctl.sh` 第一次启动时在 Linux 上自己解。
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [switch]$Zip,
    [switch]$TarGz,
    [switch]$Force,
    [switch]$SkipSmokeTest,
    # none     = 不带
    # auto     = 用项目 runtime-linux\ 里那份（默认，**从不联网**）
    # download = 项目里没有（或校验不过）才联网下载，下完**存进项目**
    # 也可以直接给一个 .tar.gz 的路径。
    [string]$LinuxRuntime = 'auto',
    # 要哪个 Python 大版本的 Linux 运行时。默认 3.14 = 和包里的 Windows 运行时
    # 同一个大版本（CPython 3.14.3），两边行为一致。
    # ★ 上游一个 release 里同时挂着 3.10~3.15，其中可能有 rc —— 绝不要 rc。
    [string]$PythonSeries = '3.14',
    [string]$BuildId
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build-common.ps1')

$Root = Split-Path -Parent $PSScriptRoot
$DistRoot = [System.IO.Path]::GetFullPath((Join-Path $Root 'dist'))
$Template = Join-Path $PSScriptRoot 'server-package'
# ★ Linux 运行时存在**项目里**（`runtime-linux\`），不是 tools\_dl\ 那种可丢弃的
#   下载缓存 —— 它和 `runtime\`（Windows 那份）是同一个角色，跟着 git 走，
#   打包时直接 copy。只有项目里没有的时候才需要联网下载一次。
$LinuxRuntimeDir = Join-Path $Root 'runtime-linux'
if ([string]::IsNullOrWhiteSpace($BuildId)) { $BuildId = New-BuildId }
# ★ 版本号第一批就校验（tools\build-ver.config 手动维护）；成果物名带它。
$Version = Get-BuildVersion -Root $Root

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $DistRoot ("PopShot-server_" + $Version.Suffix)
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
Assert-InsideDist -Path $OutputDirectory -DistRoot $DistRoot

# ★ hook 在**任何破坏性动作之前**重编（D87）。服务端包里没有 bshook.dll，
#   但**有** `server\manifest-hook.json` —— 那份 SHA 必须和客户端包里那份
#   DLL 是同一次编译的产物（两个包本来就必须成对发，D079）。
#   下面 Assert-EmptyTarget -Force 会删掉上一次的成果物，所以编在它前面：
#   编不过就停在这，dist\ 里旧的那份原封不动。
Invoke-HookBuild -Root $Root

Assert-EmptyTarget -Path $OutputDirectory -Force:$Force

if (-not (Test-Path -LiteralPath $Template -PathType Container)) {
    throw "找不到服务端包模板目录：$Template"
}

# ---------------------------------------------------------------------------
#  Linux 运行时
# ---------------------------------------------------------------------------
# 上游 = astral-sh/python-build-standalone。选 install_only_stripped 的
# x86_64-unknown-linux-gnu：基线指令集，云主机上兼容性最好，约 35 MiB。
$LinuxAssetSuffix = 'x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'

# ★ 只认「$PythonSeries.<数字>」这一种，于是 3.15.0rc1 这类**预发布版天然被挡掉**
#   （`0rc1+` 过不了 `\d+\+`）。上游一个 release 里同时挂着 3.10~3.15，
#   按名字倒序排会挑中 rc —— 不能把 release candidate 发去开服。
function Get-LinuxAssetRegex([string]$Series) {
    return '^cpython-' + [regex]::Escape($Series) + '\.(\d+)\+\d+-' +
           [regex]::Escape($LinuxAssetSuffix) + '$'
}

function Get-SidecarPath([string]$ArchivePath) { return "$ArchivePath.sha256" }

function Test-CachedArchive([System.IO.FileInfo]$File) {
    <# 缓存里的文件必须带着一份「当初校验通过」的旁证才敢用。
       没有旁证的（比如上一次下载到一半、或者校验失败前留下的）一律不认 ——
       未经校验的东西绝不能进发布包。 #>
    $sidecar = Get-SidecarPath $File.FullName
    if (-not (Test-Path -LiteralPath $sidecar)) { return $false }
    $expected = (Get-Content -LiteralPath $sidecar -Raw).Trim().ToLowerInvariant()
    if (-not $expected) { return $false }
    return ((Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expected)
}

function Get-ProjectLinuxRuntime([string]$Series) {
    <# 项目 `runtime-linux\` 里那份。**不联网**，只看本地。 #>
    if (-not (Test-Path -LiteralPath $LinuxRuntimeDir)) { return $null }
    $regex = Get-LinuxAssetRegex $Series
    # ★ 排序的脚本块里要**自己再 match 一次**：$Matches 是上一次 -match 留下的，
    #   Sort-Object 是在过滤全做完之后才跑的，那时它只剩最后一个元素的结果。
    $candidates = @(Get-ChildItem -LiteralPath $LinuxRuntimeDir -Filter 'cpython-*.tar.gz' -File |
        Where-Object { $_.Name -match $regex } |
        Sort-Object { if ($_.Name -match $regex) { [int]$Matches[1] } else { -1 } } -Descending)
    foreach ($f in $candidates) {
        if (Test-CachedArchive $f) { return $f }
        Write-Host "        忽略校验不过的文件：runtime-linux\$($f.Name)" -ForegroundColor Yellow
    }
    return $null
}

function Get-LinuxRuntimeArchive([string]$Series) {
    <# **项目里没有才走这条**：联网下载 → 用发布里的 SHA256SUMS 校验 →
       存进项目的 `runtime-linux\` → 写一份 `<文件>.sha256` 旁证。
       以后每次打包都走 `Get-ProjectLinuxRuntime`，不再联网。

       校验和不硬编码在脚本里 —— 上游每个月出新版，写死的哈希只会过期。 #>
    $existing = Get-ProjectLinuxRuntime $Series
    if ($existing) {
        Write-Host "        项目里已有且校验通过：runtime-linux\$($existing.Name)" -ForegroundColor DarkGray
        return $existing
    }
    New-Item -ItemType Directory -Path $LinuxRuntimeDir -Force | Out-Null
    Write-Host '        项目里没有，联网取一次（以后就不用了）…' -ForegroundColor DarkGray
    $rel = (Get-WebText 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest' 30) |
        ConvertFrom-Json

    $regex = Get-LinuxAssetRegex $Series
    $asset = $rel.assets | Where-Object { $_.name -match $regex } |
        Sort-Object { if ($_.name -match $regex) { [int]$Matches[1] } else { -1 } } -Descending |
        Select-Object -First 1
    if (-not $asset) {
        $seen = @($rel.assets | Where-Object { $_.name -like "*$LinuxAssetSuffix" } |
            ForEach-Object { ($_.name -replace '^cpython-', '') -replace '\+.*$', '' } | Sort-Object -Unique)
        throw ("最新发布 $($rel.tag_name) 里没有 $Series 系列的 Linux 运行时。" +
               "它有这些版本：$($seen -join ', ')。用 -PythonSeries 指定一个。")
    }
    $sums = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS' } | Select-Object -First 1
    if (-not $sums) { throw "最新发布 $($rel.tag_name) 里没有 SHA256SUMS，拒绝在不校验的情况下下载" }

    $target = Join-Path $LinuxRuntimeDir $asset.name
    Write-Host ("        下载 $($asset.name)（{0:N1} MiB）…" -f ($asset.size / 1MB)) -ForegroundColor DarkGray
    $tmp = "$target.part"
    # 进度条会让 Invoke-WebRequest 慢一个数量级。
    $savedProgress = $ProgressPreference
    try {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmp -UseBasicParsing -TimeoutSec 900
    } finally {
        $ProgressPreference = $savedProgress
    }

    Write-Host '        校验 SHA-256…' -ForegroundColor DarkGray
    # ★ 一定要走 Get-WebText：SHA256SUMS 是 application/octet-stream，
    #   直接读 .Content 拿到的是 Byte[]，逐字节切出来的「行」永远匹配不上（§164）。
    $sumText = Get-WebText $sums.browser_download_url 120
    $expected = $null
    foreach ($line in ($sumText -split "`n")) {
        $parts = $line.Trim() -split '\s+'
        if ($parts.Count -ge 2 -and $parts[-1] -eq $asset.name) { $expected = $parts[0].ToLowerInvariant(); break }
    }
    # ★ 任何一条校验路径失败都要把下载下来的东西删掉。留着的话
    #   下一次 -LinuxRuntime auto 会把这份没验过的文件直接打进发布包。
    if (-not $expected) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "SHA256SUMS 里没有 $($asset.name) 这一条，已删除下载的文件"
    }
    $actual = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "下载的文件校验不过（期望 $expected，实际 $actual），已删除"
    }

    Move-Item -LiteralPath $tmp -Destination $target -Force
    Set-Content -LiteralPath (Get-SidecarPath $target) -Value $expected -Encoding ascii
    Write-Host "        校验通过 $expected" -ForegroundColor DarkGray
    Write-Host "        已存进项目：runtime-linux\$($asset.name)（跟着 git 走，以后不再下载）" -ForegroundColor DarkGray
    Write-Host '        ★ 记得更新 runtime-linux\README.md 里的版本号和哈希' -ForegroundColor Yellow
    return (Get-Item -LiteralPath $target)
}

$linuxArchive = $null
$linuxNote = ''
switch -Regex ($LinuxRuntime) {
    '^none$' {
        $linuxNote = '未包含（-LinuxRuntime none）'
    }
    '^auto$' {
        $linuxArchive = Get-ProjectLinuxRuntime $PythonSeries
        if (-not $linuxArchive) {
            $linuxNote = "未包含（项目 runtime-linux\ 里没有 $PythonSeries 系列；" +
                         '要带的话加 -LinuxRuntime download 联网取一次，之后就一直有了）'
        }
    }
    '^download$' {
        $linuxArchive = Get-LinuxRuntimeArchive $PythonSeries
    }
    default {
        $p = [System.IO.Path]::GetFullPath($LinuxRuntime)
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
            throw "-LinuxRuntime 给的既不是 none/auto/download，也不是一个存在的文件：$LinuxRuntime"
        }
        $linuxArchive = Get-Item -LiteralPath $p
    }
}

# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
try {
    Write-Host ''
    Write-Host "=== 服务端包（版本 $($Version.Text)，批次 $BuildId）===" -ForegroundColor Cyan
    Write-Host "    输出：$OutputDirectory"
    Write-Host ''

    # --- 1. 启停脚本 ---------------------------------------------------------
    Write-Host '  [1/6] 启停脚本（Windows 3 个 + Linux 3 个）'
    foreach ($name in @('start.bat', 'start-debug.bat', 'stop.bat')) {
        Copy-TextFile -Source (Join-Path $Template $name) -Target (Join-Path $OutputDirectory $name) -Kind 'bat'
        Test-AsciiOnly (Join-Path $OutputDirectory $name)
    }
    foreach ($name in @('start.sh', 'start-debug.sh', 'stop.sh')) {
        Copy-TextFile -Source (Join-Path $Template $name) -Target (Join-Path $OutputDirectory $name) -Kind 'unix'
    }
    Copy-TextFile -Source (Join-Path $Template 'serverctl.ps1') `
                  -Target (Join-Path $OutputDirectory 'tools\serverctl.ps1') -Kind 'ps1'
    # ★ serverctl.ps1 点源的兼容垫片（Win7 / PowerShell 2.0）。它不在
    #   $Template 下，而是和客户端包共用 tools\wincompat.ps1 那一份。
    #   漏了它 serverctl.ps1 会在开头就报「找不到文件」—— 别删这两行。
    Copy-TextFile -Source (Join-Path $Root 'tools\wincompat.ps1') `
                  -Target (Join-Path $OutputDirectory 'tools\wincompat.ps1') -Kind 'ps1'
    Copy-TextFile -Source (Join-Path $Template 'serverctl.sh') `
                  -Target (Join-Path $OutputDirectory 'tools\serverctl.sh') -Kind 'unix'
    Copy-TextFile -Source (Join-Path $Template 'server.config') `
                  -Target (Join-Path $OutputDirectory 'config\server.config') -Kind 'unix'
    Copy-TextFile -Source (Join-Path $Template 'README.md') `
                  -Target (Join-Path $OutputDirectory 'README.md') -Kind 'unix'
    # ★ 把这一版 hook 的 SHA-256 记进 server\manifest-hook.json（D85）——
    #   和 build-portable.ps1 那边**同一件事、同一条命令**，两个包都要做：
    #   清单就放在 server\ 里，跟着下面那次递归拷贝一起进包。
    #   ★ 必须在拷 server\ **之前**跑，否则包里带的是旧清单。
    #   只打服务端包时也要更新 —— 不然「清单里没有这个版本」会被判成
    #   「手改出来的版本号」而强制更新（D85 第二轮收紧）。
    #   hook\bin\bshook.dll 不在（服务端包本来就不需要它）就跳过：那时
    #   清单保持仓库里提交的那份，由客户端包那次负责。
    #   ★ D87 起开头的 Invoke-HookBuild 已经保证它在了（编不出来就中止打包），
    #     这个分支留着只当兜底 —— 别照它推断「服务端包可以不带最新清单」。
    #   ★★ 失败必须中断，理由和 build-portable.ps1 那边一字不差：退出码 2 =
    #   比最新发布还老的版本 hook 变了（老版本冻结，抬版本号）；其它失败 =
    #   这一版打出去校验整项关闭，不能默默发布。
    $hookDll = Join-Path $Root 'hook\bin\bshook.dll'
    if (Test-Path -LiteralPath $hookDll -PathType Leaf) {
        & (Join-Path $Root 'runtime\python\python.exe') `
            (Join-Path $Root 'tools\gen_hook_manifest.py') --version $Version.Text
        if ($LASTEXITCODE -eq 2) {
            throw ("版本 {0} 比 update-manifest.json 里最新的还老，而 hook\bin\bshook.dll 变了 —— 老版本冻结，抬 tools\build-ver.config 再打包（D85）" -f $Version.Text)
        }
        if ($LASTEXITCODE -ne 0) {
            throw '更新 server\manifest-hook.json 失败 —— 这一版打出去将不做 hook 完整性校验，不能默默发布'
        }
    } else {
        Write-Host ('  [warn] 没有 hook\bin\bshook.dll，' +
                    'server\manifest-hook.json 用仓库里提交的那份') `
                   -ForegroundColor Yellow
    }
    # ★ 许可证必须随包走：PolyForm Noncommercial 的 Notices 一节明确要求
    #   「拿到软件的人也要拿到这些条款」。这两份是**仓库根**那份，不是
    #   tools\server-package\ 下的模板 —— 许可证只有一份，不存在「另一边」。
    Copy-TextFile -Source (Join-Path $Root 'LICENSE') `
                  -Target (Join-Path $OutputDirectory 'LICENSE') -Kind 'unix'
    Copy-TextFile -Source (Join-Path $Root 'LICENSE.zh.md') `
                  -Target (Join-Path $OutputDirectory 'LICENSE.zh.md') -Kind 'unix'

    # --- 2. server 代码（和客户端包同一份，铁律 8）--------------------------
    Write-Host '  [2/6] server（和客户端包同一份代码）'
    # ★ 打包**不重跑**原版数据的提取（D53）：那几份产物在仓库里，要更新就自己
    #   跑 tools\update-gamedata.bat（唯一那一个）。Copy-* 仍然会核对条数和
    #   format，对不上就中止打包。
    $serverFiles = Copy-ServerCode -Root $Root -PackageRoot $OutputDirectory
    Write-Host ("        $($serverFiles.Count) 个文件（不含 relay.py —— 那是客户端连远端用的本机一跳）") -ForegroundColor DarkGray

    # --- 3. Windows 运行时 ---------------------------------------------------
    Write-Host '  [3/6] runtime-win（Windows 独立 Python）'
    Copy-TreeFiltered -Source (Join-Path $Root 'runtime') -Target (Join-Path $OutputDirectory 'runtime-win')
    if (-not (Test-Path -LiteralPath (Join-Path $OutputDirectory 'runtime-win\python\python.exe') -PathType Leaf)) {
        throw 'runtime-win\python\python.exe 没拷进去'
    }
    # ★ **服务端包不带 `runtime-win7\`**（用户 2026-08-18 拍板，D133）：
    #   Win7 兼容运行时只是为了让个别 Win7 玩家能**启动游戏**，
    #   架服务端这件事不考虑老系统。少带 16 MB。
    #   —— 想改主意的话，抄 `build-portable.ps1` 里那段 Copy + 三个文件的检查。

    # --- 4. Linux 运行时（可选）---------------------------------------------
    if ($linuxArchive) {
        Write-Host ("  [4/6] runtime-linux：$($linuxArchive.Name)（{0:N1} MiB，第一次启动时在 Linux 上自解）" -f ($linuxArchive.Length / 1MB))
        $linuxDir = Join-Path $OutputDirectory 'runtime-linux'
        New-Item -ItemType Directory -Path $linuxDir -Force | Out-Null
        Copy-Item -LiteralPath $linuxArchive.FullName -Destination (Join-Path $linuxDir $linuxArchive.Name) -Force
        $sha = (Get-FileHash -LiteralPath $linuxArchive.FullName -Algorithm SHA256).Hash
        Write-TextFile -Path (Join-Path $linuxDir 'README.txt') -Kind 'unix' -Text @"
Linux 独立 Python 运行时（未解压）
=================================

文件      $($linuxArchive.Name)
SHA-256   $sha
来源      https://github.com/astral-sh/python-build-standalone
许可      解开后见 python/lib/python*/LICENSE.txt

★ 故意不解压：打包是在 Windows 上做的，在那边解开会丢掉符号链接
  （python3 -> python3.14）和可执行位，解出来跑不了。
  第一次执行 ./start.sh 时 tools/serverctl.sh 会自动在 Linux 上解开它，
  解出 runtime-linux/python/bin/python3 之后就一直用那份。

  手工解也行：  tar -xzf $($linuxArchive.Name) -C .
"@
        $linuxNote = "$($linuxArchive.Name)（未解压，首次启动自解）"
    } else {
        Write-Host "  [4/6] runtime-linux：$linuxNote" -ForegroundColor Yellow
        Write-Host '        Linux 上会退回系统的 python3（要求 3.10+）。' -ForegroundColor Yellow
    }

    New-Item -ItemType Directory -Path (Join-Path $OutputDirectory 'logs') -Force | Out-Null
    # 玩家客户端闪退后自动传上来的崩溃现场落在这儿（server\crashstore.py）。
    # 包里只放一个空目录 —— 里面的东西是运行时才有的，和 logs\ 一个道理。
    New-Item -ItemType Directory -Path (Join-Path $OutputDirectory 'logs_client_crash') -Force | Out-Null

    # --- 5. server-ClientFilter.config + BUILD.ver --------------------------
    Write-Host '  [5/6] server-ClientFilter.config + BUILD.ver'
    # 门禁配置：云服务器按这份「最低允许客户端版本」拦旧客户端（0 = 不限制）。
    # 改它不用重启服务器 —— 每条连接进来都会热重读一次。
    Copy-ClientFilterConfig -Root $Root -PackageRoot $OutputDirectory | Out-Null
    Write-BuildVer -PackageRoot $OutputDirectory -Kind '服务端包' -BuildId $BuildId -Version $Version `
        -Extra @{ linuxRuntime = $linuxNote } `
        -Notes @(
            'Windows：解压后双击 start.bat；停服 stop.bat。',
            'Linux：  解压后 chmod +x *.sh tools/*.sh，然后 ./start.sh；停服 ./stop.sh。',
            '要放行的 TCP 端口：47611 / 27799 / 27798 / 27810（注册页，可改）。',
            'config\server-ClientFilter.config：允许的最低客户端版本（0 = 不限制），改完即生效。',
            '详见 README.md。'
        )
    foreach ($must in @('BUILD.ver', 'config\server.config',
                        'config\server-ClientFilter.config')) {
        if (-not (Test-Path -LiteralPath (Join-Path $OutputDirectory $must) -PathType Leaf)) {
            throw "自检失败：$must 没写进包根"
        }
    }

    # --- 6. 自检 -------------------------------------------------------------
    if ($SkipSmokeTest) {
        Write-Host '  [6/6] 自检已跳过（-SkipSmokeTest）' -ForegroundColor Yellow
    } else {
        Write-Host '  [6/6] 自检：用包里的 Python 跑包里的服务端…'
        $smoke = Invoke-ServerSmokeTest -PackageRoot $OutputDirectory -PythonRelative 'runtime-win\python\python.exe'
        Write-Host ("        OK —— 认证 $($smoke.AuthPort) / 游戏 $($smoke.GamePort) / 中继 $($smoke.RelayPort) / 注册页 $($smoke.WebPort) 全部起来，注册页 200") -ForegroundColor Green
    }
    # 自检跑没跑都验一遍：拷贝那一步同样可能把用户数据带进包。
    # 服务端包里 `server\data\` 必须是**空的**，一个文件都不许有。
    Assert-PackageDataClean -PackageRoot $OutputDirectory
    Write-Host '        server\data 是空的（用户数据不进包）' -ForegroundColor DarkGray

    $size = Get-DirectorySize $OutputDirectory
    Write-Host ''
    Write-Host ("服务端包已生成：$OutputDirectory（$(Format-Size $size)）") -ForegroundColor Green

    if ($Zip) {
        $zipPath = "$OutputDirectory.zip"
        $tool = New-PackageZip -SourceDirectory $OutputDirectory -ZipPath $zipPath -Force:$Force
        Write-Host ("ZIP 已生成：$zipPath（$(Format-Size (Get-Item -LiteralPath $zipPath).Length)，$tool）") -ForegroundColor Green
    }
    if ($TarGz) {
        $tarPath = "$OutputDirectory.tar.gz"
        New-PackageTarGz -SourceDirectory $OutputDirectory -TarPath $tarPath -Force:$Force
        Write-Host ("tar.gz 已生成：$tarPath（$(Format-Size (Get-Item -LiteralPath $tarPath).Length)）") -ForegroundColor Green
    }
    $stale = @()
    if (-not $Zip) { $stale += '.zip' }
    if (-not $TarGz) { $stale += '.tar.gz' }
    if ($stale.Count -gt 0) {
        Show-StaleArchiveWarning -PackageDirectory $OutputDirectory -Extensions $stale
    }
} catch {
    Write-Host ''
    Write-Host "生成失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "已生成的部分没有自动删除，便于检查：$OutputDirectory" -ForegroundColor Yellow
    throw
}
