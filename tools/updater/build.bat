@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  Build the auto-update bootstrap stub (tools\updater\updater.c) and put it
rem  where the client's upgrade branch looks for it:
rem      game_patched\BsPatcherChn.exe
rem
rem  Also embeds, into the exe:
rem    * updater.manifest  -- asInvoker. MANDATORY: the file NAME contains
rem      "Patcher", which trips Windows UAC installer-detection heuristics
rem      for manifest-less exes (auto-elevation -> the client's CreateProcess
rem      fails with ERROR_ELEVATION_REQUIRED). Original NGM shipped a manifest,
rem      so this restores parity.
rem    * updater.rc        -- VERSIONINFO so the UAC prompt / Task Manager
rem      show a proper Chinese display name instead of "BsPatcherChn.exe".
rem      (rc reads the .rc as UTF-8 via the #pragma code_page(65001) inside.)
rem
rem  The original BsPatcherChn.exe (Nexon NGM bootstrap, dead platform.tiancity
rem  chain) is replaced IN PLACE; git history keeps the original binary.
rem
rem  ASCII ONLY in this file (D074 / FINDINGS 135): under `chcp 65001`
rem  cmd.exe counts characters but seeks bytes -- see hook\build.bat for the
rem  full story. Keep this file plain ASCII.
rem
rem  Subsystem: WINDOWS (WinMain, no console flash). The consent dialog and
rem  the elevated worker console are created by the stub itself.
rem ==========================================================================

set "SRC=%~dp0"
set "OUT=%~dp0bin"
set "GAME=%SRC%..\..\game_patched"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"

rem VCVARS contains "(x86)"; always quote it, cmd expands the whole block first.
if not exist "%VCVARS%" (
    echo [updater] vcvars32.bat not found: "%VCVARS%"
    exit /b 1
)

if not exist "%OUT%" mkdir "%OUT%"

call "%VCVARS%" >nul
if errorlevel 1 (
    echo [updater] vcvars32 init failed
    exit /b 1
)

pushd "%OUT%"

echo [updater] compiling version resource ^(updater.rc^) ...
rc /nologo /fo"updater.res" "%SRC%updater.rc"
if errorlevel 1 (
    echo [updater] rc FAILED
    popd
    exit /b 1
)

echo [updater] compiling BsPatcherChn.exe ^(updater stub^) ...
cl /nologo /W3 /O2 /MT /utf-8 "%SRC%updater.c" updater.res /Fe:BsPatcherChn.exe /link /SUBSYSTEM:WINDOWS /MANIFEST:EMBED /MANIFESTINPUT:"%SRC%updater.manifest" kernel32.lib user32.lib shell32.lib
if errorlevel 1 (
    echo [updater] FAILED
    popd
    exit /b 1
)

del /q *.obj >nul 2>&1
del /q updater.res >nul 2>&1
popd

copy /y "%OUT%\BsPatcherChn.exe" "%GAME%\BsPatcherChn.exe" >nul
if errorlevel 1 (
    echo [updater] copy to game_patched failed
    exit /b 1
)

echo [updater] done: game_patched\BsPatcherChn.exe replaced by our updater stub.
exit /b 0
