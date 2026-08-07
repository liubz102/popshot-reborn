@echo off
setlocal
chcp 65001 >nul

rem ==========================================================================
rem  编译 bshook.dll（注入模块）和 bsloader.exe（启动器）
rem  必须用 x86 工具链 —— BigShot.exe 是 32 位程序
rem ==========================================================================

set "SRC=%~dp0"
set "OUT=%~dp0bin"
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat"

rem 注意：VCVARS 路径含 (x86)，在 if(...) 块里直接展开会把块提前闭合，
rem 所以引用时必须加引号（cmd 是在读取整个块时就做展开的）。
if not exist "%VCVARS%" (
    echo [build] 找不到 vcvars32.bat: "%VCVARS%"
    exit /b 1
)

if not exist "%OUT%" mkdir "%OUT%"

call "%VCVARS%" >nul
if errorlevel 1 (
    echo [build] vcvars32 初始化失败
    exit /b 1
)

pushd "%OUT%"

echo [build] 编译 bshook.dll ...
cl /nologo /W3 /O2 /MT /utf-8 /LD "%SRC%bshook.c" /Fe:bshook.dll /link kernel32.lib user32.lib
if errorlevel 1 (
    echo [build] bshook.dll 编译失败
    popd
    exit /b 1
)

echo [build] 编译 bsloader.exe ...
cl /nologo /W3 /O2 /MT /utf-8 "%SRC%bsloader.c" /Fe:bsloader.exe /link kernel32.lib user32.lib
if errorlevel 1 (
    echo [build] bsloader.exe 编译失败
    popd
    exit /b 1
)

del /q *.obj >nul 2>&1
del /q *.exp >nul 2>&1
popd

echo.
echo [build] 完成，输出目录: %OUT%
dir /b "%OUT%"
exit /b 0
