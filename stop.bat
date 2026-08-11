@echo off
setlocal
chcp 65001 >nul

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
