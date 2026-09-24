@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem ==========================================================================
rem  Regression: GetAdaptersInfo guard (bug diary 27).
rem
rem  Builds hook\test_adapters_guard.c into a temporary 32-bit console program
rem  and runs it.  Self-contained: no game, no injection, no network.  The
rem  fixture includes hook\adapters_guard.h directly, so it exercises the same
rem  det_GetAdaptersInfo() that ships inside bshook.dll.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** (D074 / FINDINGS 135)
rem ==========================================================================

set "SRC=%~dp0"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"
set "TEST_EXE=%TEMP%\popshot_adapters_%RANDOM%_%RANDOM%.exe"
set "TEST_OBJ=%TEMP%\popshot_adapters_%RANDOM%_%RANDOM%.obj"

if not exist "%VCVARS%" (
    echo [test] vcvars32.bat not found: "%VCVARS%"
    exit /b 1
)

call "%VCVARS%" >nul
if errorlevel 1 exit /b 1

rem /Od: the guard reads _ReturnAddress() to tell the game's own calls apart
rem from third-party ones; keep the compiler from inlining the callers away.
cl /nologo /W3 /Od /MT /utf-8 "%SRC%test_adapters_guard.c" /Fo:"%TEST_OBJ%" /Fe:"%TEST_EXE%" /link kernel32.lib >nul
if errorlevel 1 (
    echo [test] fixture build FAILED
    del /q "%TEST_EXE%" "%TEST_OBJ%" >nul 2>&1
    exit /b 1
)

"%TEST_EXE%"
set "RC=!ERRORLEVEL!"

del /q "%TEST_EXE%" "%TEST_OBJ%" >nul 2>&1

if not "!RC!"=="0" (
    echo [test] FAILED rc=!RC!
    exit /b 1
)
echo [test] PASS
exit /b 0
