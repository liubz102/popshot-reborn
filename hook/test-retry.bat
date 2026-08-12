@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem ==========================================================================
rem  Regression: "GameGuard bypass failed -> bsloader retries automatically"
rem  (V0.2 session 15, FINDINGS 179).  Fully non-interactive.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** (D074 / FINDINGS 135)
rem ==========================================================================

set "SRC=%~dp0"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"
set "TEST_EXE=%TEMP%\popshot_gg_retry_%RANDOM%_%RANDOM%.exe"
set "TEST_OBJ=%TEMP%\popshot_gg_retry_%RANDOM%_%RANDOM%.obj"
set "TEST_OUT=%TEMP%\popshot_gg_retry_%RANDOM%_%RANDOM%.out"

if not exist "%VCVARS%" (
    echo [test] vcvars32.bat not found: "%VCVARS%"
    exit /b 1
)
if not exist "%SRC%bin\bsloader.exe" (
    echo [test] run hook\build.bat first
    exit /b 1
)

call "%VCVARS%" >nul
if errorlevel 1 exit /b 1

cl /nologo /W3 /O2 /MT /utf-8 "%SRC%test_gg_retry.c" /Fo:"%TEST_OBJ%" /Fe:"%TEST_EXE%" /link kernel32.lib user32.lib /BASE:0x00400000 /FIXED /DYNAMICBASE:NO >nul
if errorlevel 1 (
    echo [test] fixture build FAILED
    del /q "%TEST_EXE%" "%TEST_OBJ%" "%TEST_OUT%" >nul 2>&1
    exit /b 1
)

"%SRC%bin\bsloader.exe" "%TEST_EXE%" >"%TEST_OUT%" 2>&1
set "RC=!ERRORLEVEL!"
type "%TEST_OUT%"

set "FAIL="
findstr /C:"GG-RETRY 2/3" "%TEST_OUT%" >nul || set "FAIL=attempt 2 never happened"
findstr /C:"GG-RETRY 3/3" "%TEST_OUT%" >nul || set "FAIL=attempt 3 never happened"
findstr /C:"GG-GIVEUP" "%TEST_OUT%" >nul || set "FAIL=no give-up line"
if "!RC!"=="0" set "FAIL=bsloader reported success, expected failure"

del /q "%TEST_EXE%" "%TEST_OBJ%" "%TEST_OUT%" >nul 2>&1

if defined FAIL (
    echo [test] FAILED: !FAIL!
    exit /b 1
)
echo [test] PASS: bypass failure detected, retried, final attempt reported failure
exit /b 0
