@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

rem ==========================================================================
rem  Regression: inline-hook relative-branch relocation (bug diary 27).
rem
rem  Builds hook\test_insn_reloc.c into a temporary 32-bit console program and
rem  runs it.  Self-contained: no game, no injection, no network.  The fixture
rem  includes hook\insn_reloc.h directly, so it exercises the same
rem  install_inline_hook() that ships inside bshook.dll.
rem
rem  *** KEEP THIS FILE ASCII-ONLY *** (D074 / FINDINGS 135)
rem ==========================================================================

set "SRC=%~dp0"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"
set "TEST_EXE=%TEMP%\popshot_insn_reloc_%RANDOM%_%RANDOM%.exe"
set "TEST_OBJ=%TEMP%\popshot_insn_reloc_%RANDOM%_%RANDOM%.obj"

if not exist "%VCVARS%" (
    echo [test] vcvars32.bat not found: "%VCVARS%"
    exit /b 1
)

call "%VCVARS%" >nul
if errorlevel 1 exit /b 1

rem /Od: the fixture hand-writes machine code and calls through it; keep the
rem compiler from reordering or inlining the checks around those calls.
cl /nologo /W3 /Od /MT /utf-8 "%SRC%test_insn_reloc.c" /Fo:"%TEST_OBJ%" /Fe:"%TEST_EXE%" /link kernel32.lib >nul
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
