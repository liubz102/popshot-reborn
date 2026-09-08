@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem ==========================================================================
rem  Double-click entry point for re-extracting ALL original game data:
rem  terrain, weapons, character props, shop items, item icon atlas.
rem  Each step also runs that product's tests; the first failure stops it.
rem
rem  Double-click : extract everything, letting each tool find Pack_decrypt
rem  Command line : update-gamedata.bat [path to Pack_decrypt]
rem
rem  *** KEEP THIS FILE ASCII-ONLY ***
rem  Under `chcp 65001` cmd.exe seeks around the batch file by byte offset,
rem  but writes back the count of CHARACTERS consumed instead of BYTES.  A
rem  Chinese character is 3 bytes in UTF-8, so the pointer falls 2 bytes
rem  short each time; the drift accumulates until cmd resumes in the MIDDLE
rem  of a later line ('xxx' is not recognized...) or even seeks BACKWARDS and
rem  re-runs commands it already executed (observed: `pause` firing twice).
rem  Padding Chinese lines with a trailing space does NOT help -- a space is
rem  1 byte AND 1 character, so the drift rate is unchanged; it only moves
rem  where the break lands (tried 2026-09-09, broke both times).
rem  All Chinese therefore lives in update-gamedata.ps1, which PowerShell
rem  decodes correctly.  Same pattern as tools\build.bat + build-menu.ps1.
rem ==========================================================================

set "PS1=%~dp0update-gamedata.ps1"
if not exist "%PS1%" goto :missing

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"
goto :end

:missing
echo [ERROR] file not found: %PS1%
set "RC=1"

:end
echo.
pause
exit /b %RC%
