<#
    build-portable.ps1 —— 从开发目录生成可直接复制到另一台电脑的精简便携版。

    默认不复制 accounts.json，因为其中可能含明文登录口令。
    -IncludeSave 用于把自己的现有存档一起迁移。
    -Zip 会在便携目录旁生成同名 ZIP。
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [switch]$IncludeSave,
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$DistRoot = [System.IO.Path]::GetFullPath((Join-Path $Root 'dist'))

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $DistRoot 'PopShot-portable-win64'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

# 输出必须位于本项目的 dist 目录里，避免脚本误写或误清理其它位置。
$distPrefix = $DistRoot.TrimEnd('\') + '\'
if (-not $OutputDirectory.StartsWith($distPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "输出目录必须位于 $DistRoot 之下：$OutputDirectory"
}
if (Test-Path -LiteralPath $OutputDirectory) {
    throw "输出目录已经存在，请先改名或删除后重试：$OutputDirectory"
}

function Copy-RequiredFile([string]$RelativePath) {
    $source = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "缺少便携版必需文件：$source"
    }
    $target = Join-Path $OutputDirectory $RelativePath
    $parent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $target
}

function Copy-RequiredDirectory([string]$RelativePath) {
    $source = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        throw "缺少便携版必需目录：$source"
    }
    $target = Join-Path $OutputDirectory $RelativePath
    $parent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $target -Recurse
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
try {
    foreach ($file in @('start.bat', 'start-debug.bat', 'stop.bat', 'README.md')) {
        Copy-RequiredFile $file
    }

    Copy-RequiredDirectory 'runtime'
    Copy-RequiredDirectory 'hook\bin'
    Copy-RequiredDirectory 'readmeResource'

    foreach ($file in @(
        'tools\launch.ps1',
        'tools\shutdown.ps1',
        'tools\d3d9_probe.exe'
    )) {
        Copy-RequiredFile $file
    }

    foreach ($file in @(
        'server\authserver.py',
        'server\gameserver.py',
        'server\account_store.py',
        'server\protocol.py',
        'server\simple.py',
        'server\snow.py'
    )) {
        Copy-RequiredFile $file
    }
    New-Item -ItemType Directory -Path (Join-Path $OutputDirectory 'server\data') -Force | Out-Null
    if ($IncludeSave) {
        Copy-RequiredFile 'server\data\accounts.json'
    }

    # 游戏目录保持当前已经实机验证过的内容，只排除运行生成的调试日志和崩溃转储。
    $sourceGame = Join-Path $Root 'game_patched'
    $targetGame = Join-Path $OutputDirectory 'game_patched'
    New-Item -ItemType Directory -Path $targetGame -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $sourceGame -Force) {
        if ($item.Name -in @('Debug', 'Dump')) { continue }
        Copy-Item -LiteralPath $item.FullName -Destination $targetGame -Recurse
    }

    New-Item -ItemType Directory -Path (Join-Path $OutputDirectory 'logs') -Force | Out-Null

    $size = (Get-ChildItem -LiteralPath $OutputDirectory -Recurse -File |
        Measure-Object -Property Length -Sum).Sum
    Write-Host ("便携目录已生成：{0}（{1:N1} MiB）" -f $OutputDirectory, ($size / 1MB)) -ForegroundColor Green

    if ($Zip) {
        $zipPath = "$OutputDirectory.zip"
        if (Test-Path -LiteralPath $zipPath) {
            throw "ZIP 已存在，请先改名或删除后重试：$zipPath"
        }
        Compress-Archive -LiteralPath $OutputDirectory -DestinationPath $zipPath -CompressionLevel Optimal
        Write-Host "ZIP 已生成：$zipPath" -ForegroundColor Green
    }
} catch {
    Write-Host "生成失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "未自动删除可能已生成的目录，便于检查；确认后可手工删除：$OutputDirectory" -ForegroundColor Yellow
    throw
}

