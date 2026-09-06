@echo off
setlocal
set "ROOT=%~dp0.."
set "OUT=%ROOT%\logs"
call "C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat" >nul
if errorlevel 1 exit /b 1
cl /nologo /W3 /O2 /MT /utf-8 /LD "%~dp0test_bot_motion.c" /Fo:"%OUT%\bot_motion_test.obj" /Fe:"%OUT%\bot_motion_test.dll" /link /INCREMENTAL:NO
if errorlevel 1 exit /b 1
set "PYTHONPATH=%OUT%\investigation-deps;%PYTHONPATH%"
C:\Python314\python.exe "%ROOT%\tools\test_bot_motion_native.py"
exit /b %errorlevel%
