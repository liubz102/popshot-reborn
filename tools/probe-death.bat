@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  PopShot - death/respawn probe (bug 8: "died and never respawns").
rem
rem  *** ARCHIVED - no longer shipped in the client package. ***
rem  The root cause was found in session 32 (FINDINGS 212/213) and fixed on
rem  the server side, so players no longer need to run this. Kept here in
rem  tools\ because it is the only way to read the client-side death/respawn
rem  state out of a live process; run it from here if that is ever needed
rem  again (it must sit next to probe-death.ps1 / probe_death.py).
rem
rem  Double-click this WHILE the character is stuck dead and the game is still
rem  running. It finds BigShot.exe on its own (no pid to type), samples the
rem  client-side death/respawn state for a while and writes everything to
rem  logs\probe_death_<time>_pid<pid>.log.
rem
rem  All Chinese text is printed by probe-death.ps1.
rem  *** KEEP THIS FILE ASCII-ONLY *** - see start.bat / FINDINGS.md 135.
rem ==========================================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0probe-death.ps1" %*
set "RC=%ERRORLEVEL%"

pause
exit /b %RC%
