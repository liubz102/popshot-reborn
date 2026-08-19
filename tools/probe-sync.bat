@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  PopShot - peer-sync probe (bug 9: "I hit them but they never die").
rem
rem  *** You usually do NOT need this file. ***
rem  start-debug.bat already attaches this probe on its own and keeps it
rem  running until the game exits, so just play in debug mode and send back
rem  logs\probe_sync_*.log after you hit the bug.
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
