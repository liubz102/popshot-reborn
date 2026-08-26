@echo off
chcp 65001 >nul
setlocal
rem ---------------------------------------------------------------------------
rem  update-weapondata.bat —— 重新从原版 weapon.ini 提取武器表（V0.3 M3b）
rem
rem  产物：server\bot_weapons.json（进 git、进两个发布包）
rem
rem  bot 开火全靠它：Damage 填 rpExplode、Velocity/GravityFactor 算弹道、
rem  CoolingTime 定开火节奏、SplashRange 定「每发吃掉几个弹体句柄」。
rem  最后一条错了就是「子弹飞过去不炸、一滴血不掉」，而且收方静默丢弃。
rem
rem  ★ 素材 Pack_decrypt\ 太大没进本工作副本，只在 main worktree 里。
rem    找不到就用 --ini 指路，例如：
rem      tools\update-weapondata.bat --ini D:\git\popshot-reborn\main\Pack_decrypt\Data\weapon.ini
rem ---------------------------------------------------------------------------
set "ROOT=%~dp0.."
set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" (
    echo [x] 找不到 Python（试过 C:\Python314 和 %ROOT%\runtime\python）
    exit /b 1
)

echo [*] 提取武器表……
"%PY%" "%ROOT%\tools\weapondata.py" %*
if errorlevel 1 (
    echo [x] 提取失败，产物没有更新
    exit /b 1
)

echo.
echo [*] 跑一遍武器表的测试……
"%PY%" "%ROOT%\server\test_weapondata.py"
if errorlevel 1 (
    echo [x] 测试没过 —— 产物可能是坏的，先别打包
    exit /b 1
)

echo.
echo [ok] 完成。产物：%ROOT%\server\bot_weapons.json
echo      看某几把武器：tools\update-weapondata.bat --dump 1002010
endlocal
