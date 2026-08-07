@echo off
setlocal EnableExtensions

rem ==========================================================================
rem  Double-click entry point for build-portable.ps1.
rem
rem  Double-click : shows an interactive menu, output goes to
rem                 dist\PopShot-portable-win64
rem  Command line : every argument is passed straight through, e.g.
rem                 build-portable.bat -Zip
rem                 build-portable.bat -Zip -IncludeSave
rem                 build-portable.bat -OutputDirectory D:\...\dist\try1
rem
rem  Keep this file pure ASCII and do NOT add "chcp 65001".
rem  cmd.exe re-reads a .bat by byte offset after every command, so UTF-8
rem  Chinese lines shift that offset and silently chop the following commands
rem  in half. All Chinese prompts therefore live in build-portable-menu.ps1,
rem  which PowerShell decodes correctly.
rem ==========================================================================

set "MENU=%~dp0build-portable-menu.ps1"
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
