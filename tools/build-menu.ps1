<#
    build-menu.ps1 —— build.bat 双击之后的中文交互层。

    为什么单独一个文件：cmd.exe 每执行完一条命令就按字节偏移回读 .bat，
    chcp 65001 下 UTF-8 中文行会让偏移错位，把后面的命令拦腰截断
    （实测：菜单整行消失、报 'P' 不是内部或外部命令，FINDINGS §135 / D074）。
    所以 .bat 只留纯 ASCII，中文全放这里。

    不带参数：显示菜单。
    带参数：  直接按参数打包，不打扰。例如
        build.bat -Client -Zip
        build.bat -Server -Zip -LinuxRuntime download
        build.bat -Client -Server -Zip -Force
#>
[CmdletBinding()]
param(
    [switch]$Client,
    [switch]$Server,
    [switch]$Zip,
    [switch]$IncludeSave,
    [switch]$Force,
    [switch]$SkipSmokeTest,
    [string]$LinuxRuntime = 'auto'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$DistRoot = Join-Path $Root 'dist'
$ClientBuilder = Join-Path $PSScriptRoot 'build-portable.ps1'
$ServerBuilder = Join-Path $PSScriptRoot 'build-server-package.ps1'
foreach ($f in @($ClientBuilder, $ServerBuilder)) {
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) {
        Write-Host "[错误] 找不到 $f" -ForegroundColor Red
        exit 1
    }
}
. (Join-Path $PSScriptRoot 'build-common.ps1')

# ★ 版本号第一件事就校验并展示：tools\build-ver.config 是手动维护的，
#   写错了（认不出 / 撞原版 311）要在这里炸，不能等两个包都打完才发现。
$BuildVersion = Get-BuildVersion -Root $Root
Write-Host ''
Write-Host "本次打包版本：$($BuildVersion.Text)（tools\build-ver.config；成果物名带 _V… 后缀）" -ForegroundColor Cyan

$ClientDir = Join-Path $DistRoot ("PopShot-portable-win64_" + $BuildVersion.Suffix)
$ServerDir = Join-Path $DistRoot ("PopShot-server_" + $BuildVersion.Suffix)

function Invoke-Builder([string]$Script, [System.Collections.IDictionary]$BuilderArgs) {
    $shown = @()
    foreach ($k in @($BuilderArgs.Keys)) {
        $v = $BuilderArgs[$k]
        if ($v -is [switch] -or $v -is [bool]) {
            if ($v) { $shown += "-$k" }
        } else {
            $shown += "-$k `"$v`""
        }
    }
    Write-Host ''
    Write-Host (">> $(Split-Path -Leaf $Script) " + ($shown -join ' ')).TrimEnd() -ForegroundColor DarkGray
    try {
        & $Script @BuilderArgs
    } catch {
        Write-Host ''
        Write-Host "[失败] $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

function Clear-Stale([string[]]$Paths, [switch]$Ask) {
    $stale = @($Paths | Where-Object { Test-Path -LiteralPath $_ })
    if ($stale.Count -eq 0) { return $true }
    if ($Ask) {
        Write-Host '[提示] 上次生成的结果还在：' -ForegroundColor Yellow
        $stale | ForEach-Object { Write-Host "       $_" }
        Write-Host ''
        if ((Read-Host '删除它们并重新生成？[y/N]') -notmatch '^\s*[yY]') {
            Write-Host ''
            Write-Host '已取消，什么都没动。'
            return $false
        }
    }
    foreach ($p in $stale) { Remove-Item -LiteralPath $p -Recurse -Force }
    Write-Host '已删除，继续。'
    Write-Host ''
    return $true
}

function Build-Selected([bool]$DoClient, [bool]$DoServer, [bool]$DoZip,
                        [bool]$DoSave, [bool]$NoSmoke, [string]$Linux) {
    # ★ 两个包用同一个批次号：客户端包和服务端包必须成对使用（D079），
    #   BUILD.ver 里的这个号就是事后核对的依据。
    $buildId = Get-Date -Format 'yyyyMMdd-HHmmss'

    if ($DoClient) {
        $a = @{ BuildId = $buildId; Force = $true }
        if ($DoZip) { $a['Zip'] = $true }
        if ($DoSave) { $a['IncludeSave'] = $true }
        if ($NoSmoke) { $a['SkipSmokeTest'] = $true }
        Invoke-Builder $ClientBuilder $a
    }
    if ($DoServer) {
        $a = @{ BuildId = $buildId; Force = $true; LinuxRuntime = $Linux }
        if ($DoZip) { $a['Zip'] = $true; $a['TarGz'] = $true }
        if ($NoSmoke) { $a['SkipSmokeTest'] = $true }
        Invoke-Builder $ServerBuilder $a
    }

    Write-Host ''
    Write-Host '=========================================================================='
    Write-Host '[完成]' -ForegroundColor Green
    if ($DoClient) {
        Write-Host '  客户端包：整个目录拷到对方电脑，双击里面的 start.bat。' -ForegroundColor Green
        Write-Host '            联机时改 config\server.config 的 server_address 指向服务器。' -ForegroundColor Green
    }
    if ($DoServer) {
        Write-Host '  服务端包：Windows 双击 start.bat；Linux 解压后 chmod +x *.sh tools/*.sh 再 ./start.sh。' -ForegroundColor Green
    }
    if ($DoClient -and $DoServer) {
        Write-Host ''
        Write-Host "  两个包的 BUILD.ver 里版本都是 $($BuildVersion.Text)、批次都是 $buildId —— 必须成对发（D079）。" -ForegroundColor Cyan
    } elseif ($DoServer) {
        Write-Host ''
        Write-Host '  ⚠ 只打了服务端包。客户端包如果是旧批次的，玩家进房间会被弹回大厅' -ForegroundColor Yellow
        Write-Host '    （客户端的端口映射和服务端的中继端口是配套的，D079）。' -ForegroundColor Yellow
    }
    Write-Host "  产物都在 $DistRoot"
    Write-Host '=========================================================================='
}

# --- 带参数：不显示菜单 -----------------------------------------------------
if ($PSBoundParameters.Count -gt 0) {
    $doClient = [bool]$Client
    $doServer = [bool]$Server
    if (-not $doClient -and -not $doServer) { $doClient = $true }   # 默认打客户端包
    if (-not $Force) {
        $targets = @()
        if ($doClient) { $targets += @($ClientDir, "$ClientDir.zip") }
        if ($doServer) { $targets += @($ServerDir, "$ServerDir.zip", "$ServerDir.tar.gz") }
        if (-not (Clear-Stale -Paths $targets -Ask)) { exit 1 }
    }
    Build-Selected $doClient $doServer ([bool]$Zip) ([bool]$IncludeSave) ([bool]$SkipSmokeTest) $LinuxRuntime
    exit 0
}

# --- 菜单 -------------------------------------------------------------------
Write-Host ''
Write-Host '=========================================================================='
Write-Host '  炮炮火枪手 —— 打包'
Write-Host '=========================================================================='
Write-Host ''
Write-Host "  产物目录：$DistRoot"
Write-Host ''
Write-Host "  客户端包  PopShot-portable-win64_$($BuildVersion.Suffix)   给玩的人：游戏本体 + 内置"
Write-Host '            Python + 一份完整服务端（单机就连它）。约 390 MiB'
Write-Host "  服务端包  PopShot-server_$($BuildVersion.Suffix)   给开服的人：只有服务端，"
Write-Host '            Windows / Linux 两套启停脚本。约 25 MiB'
Write-Host ''
Write-Host '  （目录名里的版本号取自 tools\build-ver.config，发版前手动改它。）' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  [1] 客户端包（只生成目录）        最快，本机换个位置测试用'
Write-Host '  [2] 客户端包 + ZIP                发给别人测试用这个'
Write-Host '  [3] 服务端包 + ZIP + tar.gz'
Write-Host '  [4] 两个包都要 + 压缩包           客户端和服务端必须成对发'
Write-Host '  [5] 同 [4]，且服务端包带 Linux 运行时  ★ 默认：云主机上连 python3 都'
Write-Host '                                         没有也能开服。取自项目里的'
Write-Host '                                         runtime-linux\，只在它是空的时候'
Write-Host '                                         才联网下一次（约 35 MiB）'
Write-Host '  [6] 客户端包 + ZIP，带上我自己的存档'
Write-Host ''
Write-Host '  [6] 会把 server\data\accounts.json 一起打进去，里面是【明文口令】——' -ForegroundColor Yellow
Write-Host '      只在自己的机器之间搬家时才选它，别发给别人。' -ForegroundColor Yellow
Write-Host ''

$choice = Read-Host '请选择 [1-6]，直接回车 = 5'
if ([string]::IsNullOrWhiteSpace($choice)) { $choice = '5' }

$doClient = $false; $doServer = $false; $doZip = $false
$doSave = $false; $linux = 'auto'
switch ($choice.Trim()) {
    '1' { $doClient = $true }
    '2' { $doClient = $true; $doZip = $true }
    '3' { $doServer = $true; $doZip = $true }
    '4' { $doClient = $true; $doServer = $true; $doZip = $true }
    '5' { $doClient = $true; $doServer = $true; $doZip = $true; $linux = 'download' }
    '6' { $doClient = $true; $doZip = $true; $doSave = $true }
    default {
        Write-Host ''
        Write-Host "[错误] 无效的选择：$choice" -ForegroundColor Red
        exit 1
    }
}

$targets = @()
if ($doClient) { $targets += @($ClientDir, "$ClientDir.zip") }
if ($doServer) { $targets += @($ServerDir, "$ServerDir.zip", "$ServerDir.tar.gz") }
if (-not (Clear-Stale -Paths $targets -Ask)) { exit 1 }

Build-Selected $doClient $doServer $doZip $doSave $false $linux
exit 0
