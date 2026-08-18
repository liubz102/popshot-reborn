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
