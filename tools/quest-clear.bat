@echo off
chcp 65001 >nul
setlocal
rem ---------------------------------------------------------------------------
rem  quest-clear.bat —— 一键「通关并结算」当前这一局（V0.3商店 M6）
rem
rem  干什么：给正在跑的服务端发一条 `clear` 控制命令，等价于
rem          「客户端发了 0x0417 通关标志」+「客户端发了 0x040f 结算」两步。
rem          服务端会按 drops.json 掷一次材料掉落，记进存档，然后按
rem          0x041c（合成材料栏）→ 0x0309（结算数据）→ 0x0411（结束关卡）
rem          的顺序发下去。
rem
rem  ★ 为什么不能用 `endgame`：那条打的是「**未**通关」——「通关」标志只有
rem    客户端自己的 0x0417 才置得上，手工发不出来。而 drops.json 里绝大多数
rem    规则是 cleared_only=true，所以用 endgame 一个材料都掉不出来，
rem    下一个难度也不会解锁。
rem
rem  怎么用：
rem    1) start.bat 起服务端和客户端，登录，建房，**开局进到关卡里**；
rem    2) 在关卡里跑这个脚本（不用打完，随时可以）；
rem    3) 看结算界面左下角「合成材料」那一栏有没有画出图标。
rem
rem  ⚠ 必须在**关卡里**跑：0x041c 和 0x0309 写的都是 GameContext，
rem    关卡一结束它就变 0，那时候再发是空指针（V0.1 §99 / V0.3商店 §3）。
rem
rem  多条连接时指定是谁：tools\quest-clear.bat --user 账号名
rem ---------------------------------------------------------------------------
set "ROOT=%~dp0.."
set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" (
    echo [x] 找不到 Python（试过 C:\Python314 和 %ROOT%\runtime\python）
    exit /b 1
)

echo [*] 通关并结算当前这一局……
"%PY%" "%ROOT%\tools\gs_ctl.py" clear %*
if errorlevel 1 (
    echo [x] 没发成功 —— 服务端在跑吗？关卡开着吗？
    echo     看一眼 logs\server.out 里有没有 [ctl] 那一行
    exit /b 1
)

echo.
echo [ok] 发完了。现在去看两个地方：
echo      1) 游戏里的结算界面，左下角「合成材料」那一栏有没有图标
echo      2) logs\server.out 里的「掉落材料 座位N: ...」和
echo         「← 已结算本局：每人各收到 N 份 gspRewardReceived(0x041c...)」
echo.
echo      材料存量随时可以查：python tools\gs_ctl.py inv
endlocal
