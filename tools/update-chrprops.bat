@echo off
chcp 65001 >nul
setlocal
rem ---------------------------------------------------------------------------
rem  update-chrprops.bat —— 重新从原版 ChrProps.ini 提取角色属性表（V0.3 M5）
rem
rem  产物：server\bot_chrprops.json（进 git、进两个发布包）
rem
rem  服务端要它判命中：ChrSizeHead / ChrSizeBody / ChrSizeLegs 是角色的三个
rem  碰撞圆，打中哪个圆就吃 weapon.ini 里对应的那一档伤害
rem  （HeadDamage / Damage / LegsDamage）。没有它所有角色一样大。
rem  顺带带走冲刺攻击（双击左右方向键）的参数 DashNN-*。
rem
rem  ★ 素材 Pack_decrypt\ 太大没进本工作副本，只在 main worktree 里。
rem    找不到就用 --ini 指路，例如：
rem      tools\update-chrprops.bat --ini D:\git\popshot-reborn\main\Pack_decrypt\Data\ChrProps.ini
rem ---------------------------------------------------------------------------
set "ROOT=%~dp0.."
set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" (
    echo [x] 找不到 Python（试过 C:\Python314 和 %ROOT%\runtime\python）
    exit /b 1
)

echo [*] 提取角色属性表……
"%PY%" "%ROOT%\tools\chrprops.py" %*
if errorlevel 1 (
    echo [x] 提取失败，产物没有更新
    exit /b 1
)

echo.
echo [*] 跑一遍角色属性表的测试……
"%PY%" "%ROOT%\server\test_chrprops.py"
if errorlevel 1 (
    echo [x] 测试没过 —— 产物可能是坏的，先别打包
    exit /b 1
)

echo.
echo [ok] 完成。产物：%ROOT%\server\bot_chrprops.json
echo      看某个角色：tools\update-chrprops.bat --dump 2
endlocal
