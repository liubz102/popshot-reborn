<#
    quest-clear.ps1 —— 一键「通关并结算」当前这一局（V0.3商店 M6）

    被 quest-clear.bat 调用（双击那个）。

    干什么：给正在跑的服务端发一条 `clear` 控制命令，等价于
            「客户端发了 0x0417 通关标志」+「客户端发了 0x040f 结算」两步。
            服务端会按 drops.json 掷一次材料掉落，记进存档，然后按
            0x041c（合成材料掉落）⇒ 0x0309（结算数据）⇒ 0x0411（结束关卡）
            的顺序发下去。

    ★ 为什么不能用 `endgame`：那条打的是「**未**通关」——「通关」标志只有
      客户端自己的 0x0417 才置得上，手工发不出来。而 drops.json 里绝大多数
      规则是 cleared_only=true，所以用 endgame 一个材料都掉不出来，
      下一个难度也不会解锁。

    怎么用：
      1) start.bat 起服务端和客户端，登录，建房，**开局进到关卡里**；
      2) 在关卡里跑这个脚本（不用打完，随时可以）；
      3) 看结算界面左下角「合成材料」那一栏有没有画出图标。

    ⚠ 必须在**关卡里**跑：0x041c 和 0x0309 写的都是 GameContext，
      关卡一结束它就变 0，那时候再发是空指针（V0.1 §99 / V0.3商店 §3）。

    多条连接时指定是谁：tools\quest-clear.bat --user 账号名

    ★★ 为什么中文都在这个 .ps1 里、quest-clear.bat 是纯 ASCII 的：
       cmd.exe 跑批处理时按**字节偏移**回读文件，却在 `chcp 65001` 下把
       「消耗了多少**字符**」当成「消耗了多少**字节**」写回文件指针。中文一个字
       在 UTF-8 里是 3 字节，于是每读一个中文指针就少走 2 字节，偏移越积越多，
       最后在某一行**中间**恢复执行（报 `'xxx' 不是内部或外部命令`），
       甚至**倒退**把前面的命令再跑一遍（实测 `pause` 连弹两次）。
       ★ 老版本的 quest-clear.bat 有 26 行中文，走到第一条真命令
         （`set "ROOT=%~dp0.."`）时已经攒了 **640 字节**偏移、整份 **888 字节**
         —— 这就是「双击跑不完」的根因（2026-09-10 定位）。
       ⚠ **行尾补空格治不了**：空格是 1 字节 + 1 字符，差值一点没变。
       判据是 `len(raw) - len(raw.decode('utf-8'))`，纯 ASCII 时才是 0。
       同一套办法见 `tools\update-gamedata.bat` + `update-gamedata.ps1`。
#>
[CmdletBinding()]
param(
    # 原样转发给 gs_ctl.py 的参数，例如 `--user 账号名`（多人在线时指定操作谁）。
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# ★ 故意**不用** 'Stop'：下面是原生 python 调用，成败一律按 $LASTEXITCODE
#   和服务端回的那一行判，别让 PowerShell 把正常输出当成终止错误。
$ErrorActionPreference = 'Continue'

# 直接跑这个 .ps1（没经过 bat 的 chcp 65001）时，控制台默认是 936，
# 捕获 python 的 UTF-8 输出会变成乱码 —— 这里自己钉死一次。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $PSScriptRoot

function Get-Python {
    <# 和 update-gamedata.ps1 一个口径：先开发机的 C:\Python314，
       再便携运行时里那份。 #>
    foreach ($c in @('C:\Python314\python.exe',
                     (Join-Path $Root 'runtime\python\python.exe'))) {
        if (Test-Path -LiteralPath $c -PathType Leaf) { return $c }
    }
    return $null
}

$py = Get-Python
if (-not $py) {
    Write-Host '[x] 找不到 Python（试过 C:\Python314 和 runtime\python）' -ForegroundColor Red
    exit 1
}

$ctl = Join-Path $Root 'tools\gs_ctl.py'
if (-not (Test-Path -LiteralPath $ctl -PathType Leaf)) {
    Write-Host "[x] 找不到 $ctl" -ForegroundColor Red
    exit 1
}

Write-Host '[*] 通关并结算当前这一局……'

# ★ 这里要**捕获**回复而不是让它直接刷屏：gs_ctl.py 只要连上了控制端口就
#   退出码 0，服务端那句 `err 当前没有活动连接` / `err 当前有多条活动连接`
#   照样是 0 —— 老版本据此打「[ok] 发完了」，等于对着没发出去的包报喜。
#   判据用服务端自己给的那个前缀（ok / err），不猜。
$reply = & $py $ctl clear @Rest
$rc = $LASTEXITCODE
foreach ($line in @($reply)) { Write-Host $line }

$text = (@($reply) -join "`n")
if ($rc -ne 0 -or $text -match '(?m)^\s*(err|!!)') {
    Write-Host ''
    Write-Host '[x] 没发成功 —— 服务端在跑吗？关卡开着吗？' -ForegroundColor Red
    Write-Host '    看一眼 logs\server.out 里有没有 [ctl] 那一行；'
    Write-Host '    要是说「有多条活动连接」，加参数指定是谁：'
    Write-Host '      tools\quest-clear.bat --user 账号名'
    Write-Host '    在线的账号用这个看：python tools\gs_ctl.py who' -ForegroundColor DarkGray
    exit 1
}

Write-Host ''
Write-Host '[ok] 发完了。现在去看两个地方：' -ForegroundColor Green
Write-Host '     1) 游戏里的结算界面，左下角「合成材料」那一栏有没有图标'
Write-Host '     2) logs\server.out 里的「掉落材料 座位N: ...」和'
Write-Host '        「← 已结算本局：每人各收到 N 份 gspRewardReceived(0x041c...)」'
Write-Host ''
Write-Host '     材料存量随时可以查：python tools\gs_ctl.py inv' -ForegroundColor DarkGray
exit 0
