@echo off
setlocal
rem  Switch the console to UTF-8 only on Windows 8 and newer.
rem  On Windows 7 `chcp 65001` is a known cmd.exe hazard (it garbles or
rem  truncates batch files) and buys nothing: the console there has no
rem  UTF-8 font fallback, while the shipped code page (936 on a Chinese
rem  Windows) already renders the PowerShell output correctly.
rem  Probe = the NetTCPIP module folder, which ships only on Win8+ and is
rem  exactly the capability tools\wincompat.ps1 branches on.  Locale
rem  independent and spawns no extra process.
if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules\NetTCPIP" chcp 65001 >nul

rem ==========================================================================
rem  PopShot dedicated server - stop.
rem
rem  Kills whatever process owns the server ports (47611 / 27799 / 27798 and
rem  the register page port from config/server.config).  Targets are found
rem  the port's OwningProcess, so unrelated Python processes are never
rem  touched.  Players currently online will be disconnected.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** - see start.bat.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\serverctl.ps1" -Action stop
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
