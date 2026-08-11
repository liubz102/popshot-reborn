@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem ==========================================================================
rem  Double-click entry point for building the release packages.
rem
rem  Double-click : interactive menu (client package / server package / both),
rem                 output goes to dist\
rem  Command line : every argument is passed straight through, e.g.
rem                 build.bat -Client -Zip
rem                 build.bat -Server -Zip -LinuxRuntime download
rem                 build.bat -Client -Server -Zip -Force
rem
rem  *** KEEP THIS FILE ASCII-ONLY ***
rem  Under `chcp 65001` cmd.exe seeks around the batch file by byte offset
rem  while counting characters, so ANY multi-byte (Chinese) text in here --
rem  even inside a rem comment -- drifts that offset and eventually chops a
rem  later command line in half.  All Chinese prompts therefore live in
rem  build-menu.ps1, which PowerShell decodes correctly.
rem  See .claude\FINDINGS.md section 135 and start.bat.
rem ==========================================================================

set "MENU=%~dp0build-menu.ps1"
if not exist "%MENU%" goto :missing

powershell -NoProfile -ExecutionPolicy Bypass -File "%MENU%" %*
set "RC=%ERRORLEVEL%"
goto :end

:missing
echo [ERROR] file not found: %MENU%
set "RC=1"

:end
echo.
pause
exit /b %RC%
