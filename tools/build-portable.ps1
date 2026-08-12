<#
    build-portable.ps1 —— 生成【客户端包】（里程碑 K.1）

    给普通玩家的那个包：客户端 + 内置 Python + 一份完整的服务端（当单机假服务器
    用，也能被局域网里的别人连），启停脚本仍然是 3 个，**不放服务端专用脚本**。

    用法：
        build-portable.ps1                     只生成目录
        build-portable.ps1 -Zip                顺便打 ZIP
        build-portable.ps1 -Zip -IncludeSave   带上自己的 accounts.json（含明文口令！）
        build-portable.ps1 -Force              覆盖上一次的产物
        build-portable.ps1 -SkipSmokeTest      跳过「包里的服务端起不起得来」自检

    ★ 默认不复制 accounts.json —— 里面是明文口令，发给别人等于把密码一起发出去。

    ★ 每次打包最后都会真的用**包里那份** Python 跑**包里那份** server\app.py，
      四个监听器全起来、注册页返回 200 才算成功。上一版打包脚本的文件清单
      漏了大半个 V0.2 服务端（app.py / config.py / relay.py / web\ …），
      而这种漏在本机是看不出来的：本机总能从项目目录里跑起来，
      只有把包拷到别的电脑上才炸。自检就是为了把这种错拦在打包机上。
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [switch]$IncludeSave,
    [switch]$Zip,
    [switch]$Force,
    [switch]$SkipSmokeTest,
    # 由 build-menu.ps1 传进来，让客户端包和服务端包共用同一个批次号（D079）。
    [string]$BuildId
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build-common.ps1')

$Root = Split-Path -Parent $PSScriptRoot
$DistRoot = [System.IO.Path]::GetFullPath((Join-Path $Root 'dist'))
if ([string]::IsNullOrWhiteSpace($BuildId)) { $BuildId = New-BuildId }

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $DistRoot 'PopShot-portable-win64'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
Assert-InsideDist -Path $OutputDirectory -DistRoot $DistRoot
Assert-EmptyTarget -Path $OutputDirectory -Force:$Force

# --- 打包前的环境闸 ---------------------------------------------------------
# 铁律 2：GameGuard 绝不能真的跑起来。它要是还在原位，打出来的包会在
# 别人的电脑上尝试装 2007 年的内核驱动 —— 必须在这里拦住。
$gg = Join-Path $Root 'game_patched\GameGuard.des'
if (Test-Path -LiteralPath $gg) {
    throw "game_patched\GameGuard.des 还在原位！必须先改名（CLAUDE.md 铁律 2）再打包。"
}
foreach ($must in @('hook\bin\bshook.dll', 'hook\bin\bsloader.exe',
                    'game_patched\BigShot.exe', 'runtime\python\python.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $must) -PathType Leaf)) {
        throw "缺少必需文件：$must"
    }
}

# ★★★ UserConfig.ini 是**构建输入**，不是本机杂项（V0.1 §49 / D021）。
#   客户端是「登录成功那一刻」才写出它的，所以全新环境第一次跑时它不存在；
#   文件不在就用内置默认值 = 全屏 + 16 位色 -> 走进 D3D 模式枚举分支 ->
#   现代 N 卡不报告任何 16 位色模式，GetAdapterModeCount 返回 0 ->
#   `dec eax` 下溢成 0xFFFFFFFF -> 循环上界 42.9 亿 -> **开局卡死约 40 分钟，
#   而且没有任何报错**。发出去才发作的代价太大，在这里拦住。
$userCfg = Join-Path $Root 'game_patched\UserConfig.ini'
if (-not (Test-Path -LiteralPath $userCfg -PathType Leaf)) {
    throw @"
缺少 game_patched\UserConfig.ini —— 不能打包。
这个文件是客户端登录成功时才生成的，缺了它玩家第一次启动会**卡死**（V0.1 §49）。
先在本机启动一次并成功登录让客户端生成它，或手工建一份，内容至少要有：
    FullScreen=1
    ColorDepth=1
（注意这个老客户端的 FullScreen 语义和 D3D Windowed 相反，1 才是窗口模式。）
"@
}
$userCfgText = Get-Content -LiteralPath $userCfg -Raw
if ($userCfgText -notmatch '(?m)^\s*ColorDepth\s*=\s*1\s*$') {
    throw ("game_patched\UserConfig.ini 里 ColorDepth 不是 1（16 位色）。" +
           "现代显卡上会触发 D3D 模式枚举下溢，玩家开局卡死 40 分钟（V0.1 §49）。改成 1 再打包。")
}
if ($userCfgText -notmatch '(?m)^\s*FullScreen\s*=\s*1\s*$') {
    Write-Host '⚠ game_patched\UserConfig.ini 的 FullScreen 不是 1（1 才是窗口模式）。' -ForegroundColor Yellow
    Write-Host '  ColorDepth=1 已经能避开 §49 的死循环，所以只是提醒；但两条一起才最保险。' -ForegroundColor Yellow
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
try {
    Write-Host ''
    Write-Host "=== 客户端包（批次 $BuildId）===" -ForegroundColor Cyan
    Write-Host "    输出：$OutputDirectory"
    Write-Host ''

    # --- 1. 根目录的脚本和配置 ---------------------------------------------
    Write-Host '  [1/6] 启停脚本 + server.config'
    foreach ($file in @('start.bat', 'start-debug.bat', 'stop.bat')) {
        Copy-TextFile -Source (Join-Path $Root $file) -Target (Join-Path $OutputDirectory $file) -Kind 'bat'
        Test-AsciiOnly (Join-Path $OutputDirectory $file)
    }
    Copy-One (Join-Path $Root 'README.md') (Join-Path $OutputDirectory 'README.md')

    # server.config：本机那份是**玩家自己填的地址**，`.gitignore` 里排掉了，
    # 所以新 clone 下来的仓库根本没有它 —— 那种情况下照 `server\config.py` 的
    # DEFAULT_CONFIG_TEXT 现生成一份，让「clone 完就能打包」成立。
    # 用 LF：Win10 1809 起记事本认 LF，模板本来也是 LF。
    $cfgSrc = Join-Path $Root 'server.config'
    $cfgDst = Join-Path $OutputDirectory 'server.config'
    if (Test-Path -LiteralPath $cfgSrc -PathType Leaf) {
        Copy-TextFile -Source $cfgSrc -Target $cfgDst -Kind 'unix'
    } else {
        Write-Host '        根目录没有 server.config，照 config.py 的模板生成一份' -ForegroundColor DarkGray
        $serverDir = Join-Path $Root 'server'
        & (Join-Path $Root 'runtime\python\python.exe') -c `
            "import sys; sys.path.insert(0, r'$serverDir'); import config; config.ensure_exists(r'$cfgDst')"
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $cfgDst -PathType Leaf)) {
            throw '生成 server.config 失败（server\config.py 的模板出问题了？）'
        }
    }

    # --- 2. 运行时 / 注入件 / 探针 ------------------------------------------
    Write-Host '  [2/6] runtime + hook\bin + tools'
    Copy-TreeFiltered -Source (Join-Path $Root 'runtime') -Target (Join-Path $OutputDirectory 'runtime')
    Copy-TreeFiltered -Source (Join-Path $Root 'hook\bin') -Target (Join-Path $OutputDirectory 'hook\bin')
    Copy-TreeFiltered -Source (Join-Path $Root 'readmeResource') -Target (Join-Path $OutputDirectory 'readmeResource')
    # ★ 只带启停要用的三个：launch.ps1 / shutdown.ps1 / d3d9_probe.exe。
    #   fakeclient.py、gs_ctl.py、各种 probe_*.py 都是开发工具，不进发布包。
    foreach ($file in @('tools\launch.ps1', 'tools\shutdown.ps1')) {
        Copy-TextFile -Source (Join-Path $Root $file) -Target (Join-Path $OutputDirectory $file) -Kind 'ps1'
    }
    Copy-One (Join-Path $Root 'tools\d3d9_probe.exe') (Join-Path $OutputDirectory 'tools\d3d9_probe.exe')

    # --- 3. 服务端代码（和服务端包同一份，铁律 8）---------------------------
    Write-Host '  [3/6] server（单机假服务器 = 云端服务端的同一套代码）'
    $serverFiles = Copy-ServerCode -Root $Root -PackageRoot $OutputDirectory -IncludeRelayClient
    Write-Host ("        $($serverFiles.Count) 个文件") -ForegroundColor DarkGray
    if ($IncludeSave) {
        Copy-One (Join-Path $Root 'server\data\accounts.json') `
                 (Join-Path $OutputDirectory 'server\data\accounts.json')
        Write-Host '        已带上 accounts.json（内含明文口令，别发给别人）' -ForegroundColor Yellow
    }

    # --- 4. 游戏本体 ---------------------------------------------------------
    Write-Host '  [4/6] game_patched（排除 Debug / Dump，约 250 MiB 的崩溃转储）'
    $sourceGame = Join-Path $Root 'game_patched'
    $targetGame = Join-Path $OutputDirectory 'game_patched'
    New-Item -ItemType Directory -Path $targetGame -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $sourceGame -Force) {
        if ($item.Name -in @('Debug', 'Dump', 'BigShot.rpt')) { continue }
        Copy-Item -LiteralPath $item.FullName -Destination $targetGame -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Join-Path $OutputDirectory 'logs') -Force | Out-Null

    # --- 5. BUILD.txt --------------------------------------------------------
    Write-Host '  [5/6] BUILD.txt'
    $dllHash = (Get-FileHash -LiteralPath (Join-Path $OutputDirectory 'hook\bin\bshook.dll') -Algorithm SHA256).Hash
    $ldrHash = (Get-FileHash -LiteralPath (Join-Path $OutputDirectory 'hook\bin\bsloader.exe') -Algorithm SHA256).Hash
    Write-BuildInfo -PackageRoot $OutputDirectory -Kind '客户端包' -BuildId $BuildId -ExtraLines @(
        "bshook.dll  $dllHash",
        "bsloader.exe $ldrHash",
        '',
        '怎么用：整个目录拷到目标电脑，双击 start.bat。',
        '联机：登录界面选「远程服务器」，地址改 server.config 的 server_address。',
        '首次使用先点登录框下方的注册链接注册账号。'
    )

    # --- 6. 自检 -------------------------------------------------------------
    if ($SkipSmokeTest) {
        Write-Host '  [6/6] 自检已跳过（-SkipSmokeTest）' -ForegroundColor Yellow
    } else {
        Write-Host '  [6/6] 自检：用包里的 Python 跑包里的服务端…'
        $smoke = Invoke-ServerSmokeTest -PackageRoot $OutputDirectory -PythonRelative 'runtime\python\python.exe'
        Write-Host ("        OK —— 认证 $($smoke.AuthPort) / 游戏 $($smoke.GamePort) / 中继 $($smoke.RelayPort) / 注册页 $($smoke.WebPort) 全部起来，注册页 200") -ForegroundColor Green
    }

    $size = Get-DirectorySize $OutputDirectory
    Write-Host ''
    Write-Host ("客户端包已生成：$OutputDirectory（$(Format-Size $size)）") -ForegroundColor Green

    if ($Zip) {
        $zipPath = "$OutputDirectory.zip"
        Write-Host '正在压缩…（几百 MiB，要等一会）'
        $tool = New-PackageZip -SourceDirectory $OutputDirectory -ZipPath $zipPath -Force:$Force
        $zipSize = (Get-Item -LiteralPath $zipPath).Length
        Write-Host ("ZIP 已生成：$zipPath（$(Format-Size $zipSize)，$tool）") -ForegroundColor Green
    } else {
        Show-StaleArchiveWarning -PackageDirectory $OutputDirectory -Extensions @('.zip')
    }
} catch {
    Write-Host ''
    Write-Host "生成失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "已生成的部分没有自动删除，便于检查；确认后手工删掉即可：$OutputDirectory" -ForegroundColor Yellow
    throw
}
