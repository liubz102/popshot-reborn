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
rem  PopShot - one-click stop.
rem
rem  Kills BigShot.exe / bsloader.exe plus whatever process owns the server
rem  and relay ports (47611 auth / 27799 game / 27800 control / register page
rem  / 47621 + 27809 relay).  Targets are found through the port's
rem  OwningProcess, so unrelated Python processes are never touched.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** - see start.bat / FINDINGS.md 135.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\shutdown.ps1"
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
