@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem ==========================================================================
rem  Promotion table 0x0F03: chunk assembly + the read-only stream that bshook
rem  hands to the game's own ini loader (X_Mod X23 / D111).
rem
rem  Builds test\hook\test_promo_wire.c into a temporary 32-bit console program
rem  and runs it.  Self-contained: no game, no injection.  The fixture includes
rem  hook\promo_wire.h directly and calls the stream's vtable the way the game
rem  does (thiscall, ecx = this), checking that esp comes back balanced.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** (global rule: cmd.exe re-reads batch
rem  files by byte offset; any CJK text here drifts the read pointer)
rem ==========================================================================

set "SRC=%~dp0"
set "HOOK=%~dp0..\..\hook"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"
set "TEST_EXE=%TEMP%\popshot_promo_wire_%RANDOM%_%RANDOM%.exe"
set "TEST_OBJ=%TEMP%\popshot_promo_wire_%RANDOM%_%RANDOM%.obj"

if not exist "%VCVARS%" (
    echo [test] vcvars32.bat not found: "%VCVARS%"
    exit /b 1
)

call "%VCVARS%" >nul
if errorlevel 1 exit /b 1

cl /nologo /W3 /O2 /MT /utf-8 /I "%HOOK%" "%SRC%test_promo_wire.c" /Fo:"%TEST_OBJ%" /Fe:"%TEST_EXE%" >nul
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
