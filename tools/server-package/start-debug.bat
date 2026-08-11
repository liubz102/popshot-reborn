@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  PopShot dedicated server - start (debug, full logging).
rem
rem  Same as start.bat plus `--verbose`: per-packet hexdump on the server and
rem  one capture file pair per connection.  The log grows by megabytes, so
rem  switch back to start.bat once you are done troubleshooting.
rem
rem  Connection events (logs\online.log) are written in BOTH modes -- that
rem  file is independent of --verbose.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** - see start.bat.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\serverctl.ps1" -Action start -DebugLog
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
