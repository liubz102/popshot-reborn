@echo off
setlocal
chcp 65001 >nul

set "SRC=%~dp0"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"
set "TEST_EXE=%TEMP%\popshot_gg_watchdog_%RANDOM%_%RANDOM%.exe"
set "TEST_OBJ=%TEMP%\popshot_gg_watchdog_%RANDOM%_%RANDOM%.obj"
set "TEST_OUT=%TEMP%\popshot_gg_watchdog_%RANDOM%_%RANDOM%.out"

if not exist "%VCVARS%" (
    echo [test] 找不到 vcvars32.bat: "%VCVARS%"
    exit /b 1
)
if not exist "%SRC%bin\bsloader.exe" (
    echo [test] 请先运行 hook\build.bat
    exit /b 1
)

call "%VCVARS%" >nul
if errorlevel 1 exit /b 1

cl /nologo /W3 /O2 /MT /utf-8 "%SRC%test_gg_watchdog.c" /Fo:"%TEST_OBJ%" /Fe:"%TEST_EXE%" /link kernel32.lib /BASE:0x00400000 /FIXED /DYNAMICBASE:NO >nul
if errorlevel 1 (
    echo [test] 回归夹具编译失败
    del /q "%TEST_EXE%" "%TEST_OBJ%" "%TEST_OUT%" >nul 2>&1
    exit /b 1
)

"%SRC%bin\bsloader.exe" "%TEST_EXE%" >"%TEST_OUT%" 2>&1
type "%TEST_OUT%"
findstr /C:"exit code = 0 " "%TEST_OUT%" >nul
if errorlevel 1 (
    echo [test] FAILED: DR0 被清除后没有恢复
    del /q "%TEST_EXE%" "%TEST_OBJ%" "%TEST_OUT%" >nul 2>&1
    exit /b 1
)

del /q "%TEST_EXE%" "%TEST_OBJ%" "%TEST_OUT%" >nul 2>&1
echo [test] PASS: DR0 被清除后已恢复，VEH 返回 0x755
exit /b 0
