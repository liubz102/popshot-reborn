@echo off
setlocal
chcp 65001 >nul

rem ========================================================================== 
rem  炮炮火枪手 —— 一键启动（调试，全量日志） 
rem 
rem  打开：客户端 SnowCipher 逐包 dump（BSHOOK_VERBOSE_LOG=1） 
rem        服务端逐包 hexdump + 抓包落盘（server\app.py --verbose） 
rem  
rem  速度和 start.bat 几乎一样（实测登录到大厅 15.1 秒 vs 14.6 秒）——  
rem  会话 14 把详细日志改成不刷盘之后，逐包 dump 不再拖慢启动（V0.1 §105）。 
rem  代价是日志 4 MB 起、关键行淹在 hexdump 里，所以平时玩还是用 start.bat。 
rem  
rem  服务端如果正以「正常」模式在跑，本脚本会自动把它重启成调试模式。 
rem ========================================================================== 

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1" -DebugLog
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [启动失败] 上面是错误信息。
) else (
    echo ------------------------------------------------------------------ 
    echo  登录界面上可以自己选「单机游玩」或「联机」。 
    echo  联机服务器地址配置在本目录下的 server.config。 
    echo  首次使用请先注册账号：点登录框下方的「在服务器…上注册用户」链接。 
    echo ------------------------------------------------------------------ 
    echo. 
    echo 这个窗口可以关掉，游戏会继续跑。关闭游戏和服务端请运行 stop.bat。
)
pause
exit /b %RC%
