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
rem  PopShot - one-click start (debug, full logging).
rem
rem  Turns on: per-packet SnowCipher dump in the client (BSHOOK_VERBOSE_LOG=1)
rem            per-packet hexdump + capture files on the server
rem            (server\app.py --verbose)
rem
rem  The peer-sync probe is NOT attached any more.  It existed for bug 9
rem  ("second match, I hit them but they never die"), which is now fixed
rem  at the root (server-side epoch bookkeeping, FINDINGS 218 / D137 / D138).
rem  tools\probe-sync.bat is kept around: double-click it by hand to
rem  attach the probe to a game that is already running.
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
