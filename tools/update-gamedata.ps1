<#
    update-gamedata.ps1 —— 一键重跑**全部**原版数据的提取（D53）

        [1/5] 地形数据   server\bot_mapdata\        tools\mapdata.py
        [2/5] 武器表     server\bot_weapons.json    tools\weapondata.py
        [3/5] 角色属性   server\bot_chrprops.json   tools\chrprops.py
        [4/5] 物品表     server\shop_items.json     tools\shopdata.py
        [5/5] 图标图集   server\web\itemicons.*     tools\shopicons.py

    这是**唯一**一个更新数据的脚本（以前是五个 update-*.bat，已合并进来）。
    五份产物都进 git、都进发布包；打包时**不会**自动重跑（D53），
    只有改了提取器、或者换了一份原版素材，才需要手动跑一次这个。

    ★ 每一步都是「提取 + 立刻跑对应的测试」。哪一步不过就停下、后面的不跑
      —— 产物可能是坏的，先别打包。
    ★ 顺序有依赖：图标图集要读物品表，[5] 必须排在 [4] 后面。

    入口是 `update-gamedata.bat`（双击那个）：
        tools\update-gamedata.bat
        tools\update-gamedata.bat D:\git\popshot-reborn\main\Pack_decrypt

    ★★ 为什么中文都在这个 .ps1 里、那个 .bat 是纯 ASCII 的：
       cmd.exe 跑批处理时按**字节偏移**回读文件，却在 `chcp 65001` 下把
       「消耗了多少**字符**」当成「消耗了多少**字节**」写回文件指针。中文一个字
       在 UTF-8 里是 3 字节，于是每读一个中文指针就少走 2 字节，偏移越积越多，
       最后在某一行**中间**恢复执行（报 `'xxx' 不是内部或外部命令`），
       甚至**倒退**把前面的命令再跑一遍（实测 `pause` 连弹两次）。
       ⚠ **行尾补空格治不了**：空格是 1 字节 + 1 字符，差值一点没变，
       只是把断点挪个地方 —— 2026-09-09 实测两次都翻。
       PowerShell 自己解码 UTF-8，没有这个毛病，所以中文一律放这边。
       同一套办法见 `tools\build.bat` + `build-menu.ps1`。
#>
[CmdletBinding()]
param(
    # 原版资源目录 `Pack_decrypt`。不给的话，五个提取器各自去
    # `Pack_decrypt\` 和 `..\..\main\Pack_decrypt\` 找素材（口径它们自己一致）。
    [string]$Pack
)

# ★ 故意**不用** 'Stop'：下面全是原生 python 调用，而 unittest 的输出走 stderr，
#   EAP=Stop 时 PowerShell 会把正常的测试输出当成终止错误。
#   这里一律按 $LASTEXITCODE 判成败。
$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $PSScriptRoot

function Get-Python {
    <# 优先用**开发机的** C:\Python314：便携运行时里没有 Pillow，
       而且开发机那份才是平时跑测试的那个。 #>
    foreach ($c in @('C:\Python314\python.exe',
                     (Join-Path $Root 'runtime\python\python.exe'))) {
        if (Test-Path -LiteralPath $c -PathType Leaf) { return $c }
    }
    return $null
}

function Get-PythonWithPillow {
    <# 图标图集那一步要 Pillow。探测用的一行 python **不往 stderr 写东西**
       （find_spec + sys.exit），免得探测本身在控制台上刷一坨红字。 #>
    foreach ($c in @('C:\Python314\python.exe',
                     (Join-Path $Root 'runtime\python\python.exe'))) {
        if (-not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        & $c -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PIL') else 1)"
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    return $null
}

$py = Get-Python
if (-not $py) {
    Write-Host '[x] 找不到 Python（试过 C:\Python314 和 runtime\python）' -ForegroundColor Red
    exit 1
}
$pyPil = Get-PythonWithPillow

# --- 素材指路 ---------------------------------------------------------------
$packArgs = @{ map = @(); wpn = @(); chr = @(); shop = @(); icon = @() }
if ($Pack) {
    if (-not (Test-Path -LiteralPath (Join-Path $Pack 'Data\weapon.ini') -PathType Leaf)) {
        Write-Host "[x] $Pack 不像 Pack_decrypt 目录（底下没有 Data\weapon.ini）" -ForegroundColor Red
        exit 1
    }
    $data = Join-Path $Pack 'Data'
    $packArgs.map  = @('--pack', $Pack)
    $packArgs.wpn  = @('--ini', (Join-Path $data 'weapon.ini'))
    $packArgs.chr  = @('--ini', (Join-Path $data 'ChrProps.ini'))
    $packArgs.shop = @('--shop-ini',      (Join-Path $data 'ShopItem-Chn.ini'),
                       '--bonus-ini',     (Join-Path $data 'EquipBonus-Chn.ini'),
                       '--weapon-ini',    (Join-Path $data 'weapon.ini'),
                       '--promotion-ini', (Join-Path $data 'Promotion-chn.ini'))
    $packArgs.icon = @('--src', (Join-Path $Pack 'Images\Shop'))
    Write-Host "[*] 素材目录：$Pack" -ForegroundColor DarkGray
}

# --- 五步 -------------------------------------------------------------------
$steps = @(
    [pscustomobject]@{ Label = '[1/5] 地形数据';     Out = 'server\bot_mapdata\'
                       Tool  = 'mapdata.py';    Args = @('--verify') + $packArgs.map
                       Test  = 'test_mapdata.py';    Pil = $false },
    [pscustomobject]@{ Label = '[2/5] 武器表';       Out = 'server\bot_weapons.json'
                       Tool  = 'weapondata.py'; Args = $packArgs.wpn
                       Test  = 'test_weapondata.py'; Pil = $false },
    [pscustomobject]@{ Label = '[3/5] 角色属性表';   Out = 'server\bot_chrprops.json'
                       Tool  = 'chrprops.py';   Args = $packArgs.chr
                       Test  = 'test_chrprops.py';   Pil = $false },
    [pscustomobject]@{ Label = '[4/5] 物品表';       Out = 'server\shop_items.json'
                       Tool  = 'shopdata.py';   Args = $packArgs.shop
                       Test  = 'test_shopdata.py';   Pil = $false },
    [pscustomobject]@{ Label = '[5/5] 物品图标图集'; Out = 'server\web\itemicons.png'
                       Tool  = 'shopicons.py';  Args = $packArgs.icon
                       Test  = 'test_web_admin.py';  Pil = $true }
)

foreach ($s in $steps) {
    Write-Host ''
    Write-Host ("===== $($s.Label)  ->  $($s.Out)  =====") -ForegroundColor Cyan

    $exe = $py
    if ($s.Pil) {
        if (-not $pyPil) {
            # ★ 不静默跳过：说清楚「哪一步没做成、前面几步做成了」。
            Write-Host '[x] 这一步要 Pillow，但两个 Python 都没有：' -ForegroundColor Red
            Write-Host '    C:\Python314\python.exe 和 runtime\python\python.exe' -ForegroundColor Red
            Write-Host '    装一个：C:\Python314\python.exe -m pip install pillow' -ForegroundColor Yellow
            Write-Host '    ★ 前面四份已经更新好了，只有图标没重拼（仓库里那份还在，能用）。' -ForegroundColor Yellow
            exit 1
        }
        $exe = $pyPil
    }

    $toolArgs = $s.Args
    & $exe (Join-Path $Root ('tools\' + $s.Tool)) $toolArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host "[x] $($s.Label)：提取失败，产物没有更新，后面的步骤没跑。" -ForegroundColor Red
        exit 1
    }

    & $py (Join-Path $Root ('server\' + $s.Test))
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host "[x] $($s.Label)：测试没过 —— 产物可能是坏的，先别打包。后面的步骤没跑。" -ForegroundColor Red
        exit 1
    }
}

Write-Host ''
Write-Host '[ok] 五份都提取完了，测试也都过了。' -ForegroundColor Green
Write-Host '     改了哪些文件看 git status；打包时 Copy-* 会再核一遍条数和 format。'
Write-Host ''
Write-Host '     要单独跑某一个、或者看细节，直接调 python：' -ForegroundColor DarkGray
foreach ($hint in @('tools\mapdata.py --verify Camel00',
                    'tools\weapondata.py --dump 1002010',
                    'tools\chrprops.py --dump 2',
                    'tools\shopdata.py --dump-kind material',
                    'tools\shopicons.py --check')) {
    Write-Host ("       `"$py`" $hint") -ForegroundColor DarkGray
}
exit 0
