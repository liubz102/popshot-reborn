@echo off
chcp 65001 >nul
setlocal
rem ---------------------------------------------------------------------------
rem  update-mapdata.bat —— 重新从原版 .map 提取地图地形数据（V0.3 M4）
rem
rem  产物：server\bot_mapdata\index.json + 每张图一个 .json（进 git、进两个发布包）
rem  可视化：logs\mapdata-preview\*.png（人工核对地面线用）
rem
rem  ★ 素材 Pack_decrypt\ 太大没进本工作副本，只在 main worktree 里。
rem    找不到就用 --pack 指路，例如：
rem      tools\update-mapdata.bat --pack D:\git\popshot-reborn\main\Pack_decrypt
rem ---------------------------------------------------------------------------
rem  ★ 优先用**开发机的** Python：只有它装了 Pillow，--verify 的可视化才出得来。
rem    便携运行时里没有 Pillow（也不该有 —— 服务端运行时不许依赖它）。
set "ROOT=%~dp0.."
set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" (
    echo [x] 找不到 Python（试过 C:\Python314 和 %ROOT%\runtime\python）
    exit /b 1
)

echo [*] 提取地图地形数据……
"%PY%" "%ROOT%\tools\mapdata.py" --verify %*
if errorlevel 1 (
    echo [x] 提取失败，产物没有更新
    exit /b 1
)

echo.
echo [*] 跑一遍地形数据的测试……
"%PY%" "%ROOT%\server\test_mapdata.py"
if errorlevel 1 (
    echo [x] 测试没过 —— 产物可能是坏的，先别打包
    exit /b 1
)

echo.
echo [ok] 完成。可视化在 %ROOT%\logs\mapdata-preview\
endlocal
