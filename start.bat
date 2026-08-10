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
    echo ------------------------------------------------------------------ 
    echo  登录界面上可以自己选服务器： 
    echo    「单机游玩」 连本机，一个人玩，存档保存在本机 
    echo    「联机」     连 server.config 里配置的那台服务器 
    echo. 
    echo  联机服务器地址配置在本目录下的 server.config，用记事本打开就能改， 
    echo  IPv4 / IPv6 / 域名都支持。改完重新运行 start.bat 生效。 
    echo. 
    echo  首次使用请先注册账号：点登录框下方的「在服务器…上注册用户」链接。 
    echo ------------------------------------------------------------------ 
    echo. 
    echo 这个窗口可以关掉，游戏会继续跑。关闭游戏和服务端请运行 stop.bat。
)
pause
exit /b %RC%
