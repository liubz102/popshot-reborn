@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  Build bshook.dll (injected module) and bsloader.exe (launcher).
rem  x86 toolchain only -- BigShot.exe is a 32-bit program.
rem
rem  ASCII ONLY in this file (D074 / FINDINGS 135): under `chcp 65001`
rem  cmd.exe counts characters but seeks bytes, so every multi-byte character
rem  -- even inside a `rem` comment -- shifts the read cursor and eventually
rem  chops a later command line in half.  That is exactly what happened here:
rem  the second `cl` line silently lost `/utf-8`, so every Chinese string
rem  literal in bsloader.c was re-encoded as CP936 and one `%s` got eaten
rem  (warnings C4819 + C4474).  Keep this file plain ASCII.
rem ==========================================================================

set "SRC=%~dp0"
set "OUT=%~dp0bin"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"

rem VCVARS contains "(x86)"; always quote it, cmd expands the whole block first.
if not exist "%VCVARS%" (
    echo [build] vcvars32.bat not found: "%VCVARS%"
    exit /b 1
)

if not exist "%OUT%" mkdir "%OUT%"

call "%VCVARS%" >nul
if errorlevel 1 (
    echo [build] vcvars32 init failed
    exit /b 1
)

pushd "%OUT%"

echo [build] compiling bshook.dll ...
cl /nologo /W3 /O2 /MT /utf-8 /LD "%SRC%bshook.c" /Fe:bshook.dll /link kernel32.lib user32.lib
if errorlevel 1 (
    echo [build] bshook.dll FAILED
    popd
    exit /b 1
)

echo [build] compiling bsloader.exe ...
cl /nologo /W3 /O2 /MT /utf-8 "%SRC%bsloader.c" /Fe:bsloader.exe /link kernel32.lib user32.lib
if errorlevel 1 (
    echo [build] bsloader.exe FAILED
    popd
    exit /b 1
)

del /q *.obj >nul 2>&1
del /q *.exp >nul 2>&1
popd

echo.
echo [build] done, output: %OUT%
dir /b "%OUT%"
exit /b 0
