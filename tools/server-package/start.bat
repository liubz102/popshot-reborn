@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  PopShot dedicated server - start (normal, slim logging).
rem
rem  Listens on 47611 auth / 27799 game / 27798 relay / 27810 register page.
rem  The debug control channel is OFF here (app.py --no-control).
rem  Use start-debug.bat only while troubleshooting: it dumps every packet.
rem
rem  *** KEEP THIS FILE ASCII-ONLY ***
rem  Under `chcp 65001` cmd.exe seeks around the batch file by byte offset
rem  while counting characters, so ANY multi-byte (Chinese) text in here --
rem  even inside a rem comment -- drifts that offset and eventually chops a
rem  later command line in half.  All Chinese output comes from
rem  tools\serverctl.ps1 instead.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\serverctl.ps1" -Action start
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
