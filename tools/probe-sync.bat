@echo off
setlocal
rem  Switch the console to UTF-8 only on Windows 8 and newer -- on Win7
rem  `chcp 65001` is a known cmd.exe hazard and buys nothing there.
rem  Same probe as start.bat: the NetTCPIP module folder (Win8+ only).
if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules\NetTCPIP" chcp 65001 >nul

rem ==========================================================================
rem  PopShot - peer-sync probe (bug 9: "I hit them but they never die").
rem
rem  *** Archive: you normally do NOT need this file. ***
rem  It was written for bug 9, which is now fixed at the root (the server
rem  keeps the per-connection epoch itself -- FINDINGS 218 / D137 / D138),
rem  so start-debug.bat does NOT attach the probe any more.  Kept around
rem  in case the per-seat receive queues ever need looking at again.
rem
rem  Use this one only to attach to a game that is ALREADY running: it finds
rem  BigShot.exe on its own (no pid to type), samples the per-seat receive
rem  queue state until the game exits (or -Seconds N for a fixed window) and
rem  writes everything to logs\probe_sync_<time>.log.  Keep the window open
rem  and play as many matches as it takes; Ctrl+C stops early.
rem
rem  All Chinese text is printed by probe-sync.ps1.
rem  *** KEEP THIS FILE ASCII-ONLY *** - see start.bat / FINDINGS.md 135.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0probe-sync.ps1" %*
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
