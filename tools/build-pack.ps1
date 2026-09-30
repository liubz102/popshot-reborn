<#
    build-pack.ps1 —— 把明文资源树打成客户端能读的加密卷（增量），必要时顺带更新服务端数据

        game_patched\Pack_develop   明文资源树（改资源改这里，进 git，不进发布包）
            ↓  tools\pkn.py pack    增量：只重写改过的卷，一个子目录一卷，原版格式
        game_patched\Pack_publish   加密卷 + pack-index.json（客户端经 bshook 读这里，进 git、进发布包）
            ↓  明文树变了？          判据：tools\gamedata-stamp.json 记的树哈希 ≠ 当前树哈希
        tools\update-gamedata.ps1   服务端那五份数据（地形 / 武器 / 角色 / 物品 / 图标）跟着重提

    什么都没变 ⇒ 两步都跳过。update-gamedata 失败 ⇒ 卷和清单**不回滚**（它们是对的），
    戳不更新，下次再跑本脚本会自动重试提取；打包脚本 tools\build.bat 在这种状态下会中止。

    入口是 `build-pack.bat`（双击那个）：
        tools\build-pack.bat
        tools\build-pack.bat -Force      全部重打（无视清单）
        tools\build-pack.bat -Check      只判断卷 / 服务端数据有没有过期，不写
        tools\build-pack.bat -Verify     打完把每一卷回读，和明文逐字节比（1~2 分钟）

    ★ start.bat **不**调本脚本（用户拍板）：改完资源自己双击一次；tools\build.bat 打包前
      会自动先跑一遍。
    ★★ 中文为什么都在 .ps1 里、.bat 是纯 ASCII：见 update-gamedata.ps1 开头那段。
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Check,
    [switch]$Verify
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'build-common.ps1')

try {
    if ($Check) {
        $stale = Test-PackStale -Root $Root
        if ($stale) { exit 1 }
        exit 0
    }
    Invoke-PackBuild -Root $Root -Force:$Force -Verify:$Verify
    exit 0
} catch {
    Write-Host ''
    Write-Host "[失败] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
