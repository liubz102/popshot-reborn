@echo off
setlocal
chcp 65001 >nul

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
