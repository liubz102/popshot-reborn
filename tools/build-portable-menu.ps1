<#
    build-portable-menu.ps1 —— build-portable.bat 双击之后的中文交互层。

    为什么单独一个文件：cmd.exe 每执行完一条命令就按字节偏移回读 .bat，
    UTF-8 中文行会让偏移错位，把后面的命令拦腰截断（实测：菜单整行消失、
    报 'P' 不是内部或外部命令）。所以 .bat 只留纯 ASCII，中文全放这里。

    不带参数：显示菜单，让人选打包方式。
    带参数：  不打扰，直接按参数转发给 build-portable.ps1。
    参数表和 build-portable.ps1 保持一致，这样 .bat 里一句 %* 就能透传。
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [switch]$IncludeSave,
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'

$builder = Join-Path $PSScriptRoot 'build-portable.ps1'
if (-not (Test-Path -LiteralPath $builder -PathType Leaf)) {
    Write-Host "[错误] 找不到 build-portable.ps1：$builder" -ForegroundColor Red
    exit 1
}

# 用 hashtable splat 转发：数组 splat 只按位置传参，会把 -Zip 当成输出目录。
# 只把失败原因说清楚，不刷整坨 PowerShell 调用栈；注意 build-portable.ps1
# 开头那两个校验（输出目录不在 dist 下 / 已存在）是在它自己的 try 之外
# throw 的，这里不打印就什么提示都没有了。
function Invoke-Builder([System.Collections.IDictionary]$BuilderArgs) {
    $shown = @()
    $keys = @($BuilderArgs.Keys)
    if ($keys -contains 'OutputDirectory') { $shown += "-OutputDirectory `"$($BuilderArgs['OutputDirectory'])`"" }
    if ($keys -contains 'Zip') { $shown += '-Zip' }
    if ($keys -contains 'IncludeSave') { $shown += '-IncludeSave' }

    Write-Host ''
    Write-Host ('>> build-portable.ps1 ' + ($shown -join ' ')).TrimEnd() -ForegroundColor DarkGray
    Write-Host ''
    try {
        & $builder @BuilderArgs
    } catch {
        Write-Host ''
        Write-Host "[失败] $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

function Show-Done {
    Write-Host ''
    Write-Host '[完成] 便携目录整个拷到目标电脑就行，在那边双击里面的 start.bat 开玩。' -ForegroundColor Green
}

if ($PSBoundParameters.Count -gt 0) {
    Invoke-Builder $PSBoundParameters
    Show-Done
    exit 0
}

$root = Split-Path -Parent $PSScriptRoot
# 变量别叫 $zip：param 里已经有 [switch]$Zip，同名会被当成给参数变量赋值，
# 直接报「无法将 String 转换为 SwitchParameter」。
$outDir = Join-Path $root 'dist\PopShot-portable-win64'
$zipPath = "$outDir.zip"

# build-portable.ps1 拒绝覆盖已有产物，双击场景先问一句要不要清掉上一次的。
$stale = @($outDir, $zipPath) | Where-Object { Test-Path -LiteralPath $_ }
if ($stale.Count -gt 0) {
    Write-Host '[提示] 上次生成的结果还在，build-portable.ps1 不会覆盖它们：' -ForegroundColor Yellow
    $stale | ForEach-Object { Write-Host "       $_" }
    Write-Host ''
    if ((Read-Host '删除它们并重新生成？[y/N]') -notmatch '^\s*[yY]') {
        Write-Host ''
        Write-Host '已取消，什么都没动。也可以指定别的输出目录（必须在 dist 下面）：'
        Write-Host "    build-portable.bat -OutputDirectory $root\dist\另一个名字"
        exit 1
    }
    foreach ($path in $stale) { Remove-Item -LiteralPath $path -Recurse -Force }
    Write-Host '已删除，继续。'
    Write-Host ''
}

Write-Host '=========================================================================='
Write-Host '  炮炮火枪手 —— 生成便携版'
Write-Host '=========================================================================='
Write-Host ''
Write-Host "  输出目录：$outDir"
Write-Host '  内容：game_patched + runtime + server + hook\bin + start/stop 脚本'
Write-Host '  体积：约 390 MiB，已排除 Debug 和约 195 MiB 的 Dump'
Write-Host ''
Write-Host '  [1] 只生成便携目录            默认，最快'
Write-Host '  [2] 生成便携目录 + ZIP        压缩到约 363 MiB，要好几分钟'
Write-Host '  [3] 在 2 的基础上带上存档     server\data\accounts.json'
Write-Host ''
Write-Host '  accounts.json 里可能有明文登录口令，只在自己的机器之间搬家时才选 [3]。' -ForegroundColor Yellow
Write-Host ''

$choice = Read-Host '请选择 [1/2/3]，直接回车 = 1'
if ([string]::IsNullOrWhiteSpace($choice)) { $choice = '1' }

switch ($choice.Trim()) {
    '1' { $buildArgs = @{} }
    '2' { $buildArgs = @{ Zip = $true } }
    '3' { $buildArgs = @{ Zip = $true; IncludeSave = $true } }
    default {
        Write-Host ''
        Write-Host "[错误] 无效的选择：$choice" -ForegroundColor Red
        exit 1
    }
}

Invoke-Builder $buildArgs
Show-Done
exit 0
