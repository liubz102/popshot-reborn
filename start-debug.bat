@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  PopShot - one-click start (debug, full logging).
rem
rem  Turns on: per-packet SnowCipher dump in the client (BSHOOK_VERBOSE_LOG=1)
rem            per-packet hexdump + capture files on the server
rem            (server\app.py --verbose)
rem
rem  Speed is about the same as start.bat (15.1 s vs 14.6 s to the lobby) --
rem  V0.1 session 14 stopped flushing the debug log to disk (V0.1 105).
rem  The cost is a 4 MB+ log where the interesting lines drown in hexdump,
rem  so use start.bat for actually playing.
rem
rem  If the server is currently running in normal mode this script restarts
rem  it in debug mode automatically.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** - see start.bat / FINDINGS.md 135.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1" -DebugLog
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
