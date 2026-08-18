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
rem  PopShot - one-click start (normal play).
rem
rem  Slim logging: no cipher hook in the client, no per-packet dump on the
rem  server.  Use start-debug.bat to troubleshoot.
rem  An already-running server is reused instead of restarted.
rem
rem  *** KEEP THIS FILE ASCII-ONLY ***
rem  Under `chcp 65001` cmd.exe seeks around the batch file by byte offset
rem  while counting characters, so ANY multi-byte (Chinese) text in here --
rem  even inside a rem comment -- drifts that offset and eventually chops a
rem  later command line in half:  "'xx' is not recognized as an internal or
rem  external command".  Every Chinese message is printed by
rem  tools\launch.ps1 instead.  See .claude\FINDINGS.md section 135.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1"
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
