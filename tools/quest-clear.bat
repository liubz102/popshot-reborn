@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem ==========================================================================
rem  quest-clear.bat -- one-click "clear the current quest AND settle it"
rem  (V0.3 shop, milestone M6).  Sends the server a single `clear` control
rem  command, which is the 0x0417 "quest cleared" flag plus the 0x040f
rem  settlement in one go; `endgame` alone always settles as NOT cleared, and
rem  almost every rule in drops.json is cleared_only, so it drops nothing.
rem
rem  Run it WHILE a quest is running -- 0x041c and 0x0309 both write into
rem  GameContext, which goes to 0 the moment the quest ends.
rem
rem  Double-click : acts on the only live connection
rem  Command line : tools\quest-clear.bat --user <account>   (several online)
rem
rem  *** KEEP THIS FILE ASCII-ONLY ***
rem  Under `chcp 65001` cmd.exe seeks around the batch file by byte offset,
rem  but writes back the count of CHARACTERS consumed instead of BYTES.  A
rem  Chinese character is 3 bytes in UTF-8, so the pointer falls 2 bytes
rem  short each time; the drift accumulates until cmd resumes in the MIDDLE
rem  of a later line ('xxx' is not recognized...) or even seeks BACKWARDS and
rem  re-runs commands it already executed (observed: `pause` firing twice).
rem  The previous version of this file had 26 Chinese lines and was already
rem  640 bytes adrift by its first real command (888 over the whole file) --
rem  that is why double-clicking it never ran to the end.
rem  Padding Chinese lines with a trailing space does NOT help -- a space is
rem  1 byte AND 1 character, so the drift rate is unchanged; it only moves
rem  where the break lands (tried 2026-09-09, broke both times).
rem  All Chinese therefore lives in quest-clear.ps1, which PowerShell decodes
rem  correctly.  Same pattern as tools\update-gamedata.bat + .ps1.
rem ==========================================================================

set "PS1=%~dp0quest-clear.ps1"
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
