@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  PopShot dedicated server - stop.
rem
rem  Kills whatever process owns the server ports (47611 / 27799 / 27798 and
rem  the register page port from server.config).  Targets are found through
rem  the port's OwningProcess, so unrelated Python processes are never
rem  touched.  Players currently online will be disconnected.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** - see start.bat.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\serverctl.ps1" -Action stop
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
