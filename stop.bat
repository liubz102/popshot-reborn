@echo off
setlocal
chcp 65001 >nul

rem ========================================================================== 
rem  炮炮火枪手 —— 一键关闭 
rem 
rem  关掉 BigShot.exe / bsloader.exe，以及占用服务端和中继端口的进程 
rem  （47611 认证 / 27799 游戏 / 27800 控制 / 注册页 / 47621+27809 中继）。 
rem  按端口的 OwningProcess 精确定位，不会误伤机器上别的 Python 进程。 
rem ========================================================================== 
 
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\shutdown.ps1"
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%
