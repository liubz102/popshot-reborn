@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  Build the all-in-one auto updater (updater\src\*.c) and put it where the
rem  client's upgrade branch looks for it:
rem      game_patched\BsPatcherChn.exe
rem
rem  v3: the exe IS the updater -- original-style IE-hosted UI (templates
rem  extracted from NGMResource.dll, embedded as RCDATA) + full update logic
rem  (probe / WinHTTP download / apply). Python no longer takes part in any
rem  update, so the bundled runtime can be stopped and overwritten freely.
rem
rem  Embedded into the exe:
rem    * updater.manifest  -- asInvoker. MANDATORY: the file NAME contains
rem      "Patcher", which trips Windows UAC installer-detection heuristics
rem      for manifest-less exes (auto-elevation -> the client's CreateProcess
rem      fails with ERROR_ELEVATION_REQUIRED).
rem    * updater.rc        -- VERSIONINFO (Chinese display name) + all UI
rem      assets (templates + images) as RCDATA resources.
rem    * vendor\miniz      -- zip extraction (public domain).
rem
rem  Gates: after linking, BsPatcherChn.exe --selftest must pass (cipher
rem  vectors vs server\simple.py, 0xFE frame parsing, version math, sha256,
rem  manifest parsing, protected-path matching, embedded resources).
rem
rem  ASCII ONLY in this file (D074 / FINDINGS 135): under `chcp 65001`
rem  cmd.exe counts characters but seeks bytes. Keep this file plain ASCII.
rem ==========================================================================

set "SRC=%~dp0"
set "OUT=%~dp0bin"
set "GAME=%SRC%..\game_patched"
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

rem --- ports.h: single source of truth is server\config.py (gen_ports_h.py) --
rem Same discipline as hook\build.bat. Best effort: dev machines have python;
rem packaging (Assert-UpdaterStub) runs this script on the dev machine too.
rem If python is missing but the header is committed, use the committed one.
python "%SRC%..\tools\gen_ports_h.py"
if errorlevel 1 (
    if not exist "%SRC%src\ports.h" (
        echo [updater] cannot generate src\ports.h and no committed copy exists
        exit /b 1
    )
    echo [updater] python unavailable - using committed src\ports.h
)

pushd "%SRC%"
if errorlevel 1 (
    echo [updater] pushd failed
    exit /b 1
)

echo [updater] compiling version resource ^(updater.rc^) ...
rc /nologo /fo"%OUT%\updater.res" "updater.rc"
if errorlevel 1 (
    echo [updater] rc FAILED
    popd
    exit /b 1
)

echo [updater] compiling BsPatcherChn.exe ^(updater v3, all-in-one^) ...
cl /nologo /W3 /O2 /MT /utf-8 /DUNICODE /D_UNICODE ^
   /Isrc /Ivendor\miniz ^
   src\main.c src\util.c src\log.c src\config.c src\cipher.c src\sha256.c ^
   src\manifest.c src\net_http.c src\probe.c src\procs.c src\zip.c src\apply.c ^
   src\ui_external.c src\ui_native.c src\ui_window.c src\selftest.c ^
   vendor\miniz\miniz.c ^
   "%OUT%\updater.res" ^
   /Fe:"%OUT%\BsPatcherChn.exe" /Fo:"%OUT%\\" /link /SUBSYSTEM:WINDOWS ^
   /MANIFEST:EMBED /MANIFESTINPUT:"updater.manifest" ^
   kernel32.lib user32.lib gdi32.lib comctl32.lib shell32.lib ole32.lib ^
   oleaut32.lib uuid.lib winhttp.lib ws2_32.lib bcrypt.lib advapi32.lib
if errorlevel 1 (
    echo [updater] FAILED
    popd
    exit /b 1
)

del /q "%OUT%\*.obj" >nul 2>&1
popd

echo [updater] running selftest ^(gate^) ...
"%OUT%\BsPatcherChn.exe" --selftest
if errorlevel 1 (
    echo [updater] SELFTEST FAILED - build rejected
    exit /b 1
)

copy /y "%OUT%\BsPatcherChn.exe" "%GAME%\BsPatcherChn.exe" >nul
if errorlevel 1 (
    echo [updater] copy to game_patched failed
    exit /b 1
)

echo [updater] done: game_patched\BsPatcherChn.exe is the all-in-one updater.
exit /b 0
