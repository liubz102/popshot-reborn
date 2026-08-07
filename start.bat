@echo off
setlocal
chcp 65001 >nul
 
rem ========================================================================== 
rem  炮炮火枪手 —— 一键启动（正常游玩） 
rem 
rem  精简日志模式：客户端不装 cipher hook、服务端不逐包 dump。 
rem  排查问题请改用 start-debug.bat。 
rem   
rem  服务端已经在跑的话会直接复用，不会重复启动。 
rem ========================================================================== 

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [启动失败] 上面是错误信息。
) else (
    echo 这个窗口可以关掉，游戏会继续跑。关闭游戏和服务端请运行 stop.bat。
)
pause
exit /b %RC%
