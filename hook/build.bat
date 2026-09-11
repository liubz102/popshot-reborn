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

rem --------------------------------------------------------------------------
rem  Regenerate hook\ports.h from server\config.py before compiling.
rem
rem  server\config.py is the ONE place port numbers are defined.  The C side
rem  reads them through the generated ports.h, the PowerShell/sh launchers ask
rem  `python server\config.py --ports`.  Keeping a second hand-written copy in
rem  bshook.c used to be a "change one, forget the other" trap whose symptom is
rem  a feature that silently stops working rather than an error.
rem
rem  ports.h is committed, so building without Python still works -- we only
rem  warn in that case instead of failing.
rem --------------------------------------------------------------------------
set "GENPORTS=%SRC%..\tools\gen_ports_h.py"
set "PYEXE=%SRC%..\runtime\python\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%GENPORTS%"
if errorlevel 1 (
    echo [build] WARNING: could not regenerate ports.h; using the committed one
)

rem --------------------------------------------------------------------------
rem  Regenerate hook\notice_blob.h from hook\notice.zh.txt before compiling.
rem
rem  That is the login-screen anti-resale notice.  It is obfuscated and baked
rem  into bshook.dll so a reseller cannot just edit a text file to remove it.
rem  The plaintext source (hook\notice.zh.txt) is NOT shipped in any package;
rem  notice_blob.h is committed, so building without Python still works.
rem --------------------------------------------------------------------------
set "GENNOTICE=%SRC%..\tools\gen_notice_h.py"
"%PYEXE%" "%GENNOTICE%"
if errorlevel 1 (
    echo [build] WARNING: could not regenerate notice_blob.h; using the committed one
)

if not exist "%OUT%" mkdir "%OUT%"

call "%VCVARS%" >nul
if errorlevel 1 (
    echo [build] vcvars32 init failed
    exit /b 1
)

pushd "%OUT%"

rem --------------------------------------------------------------------------
rem  /Brepro on BOTH cl and link -- REPRODUCIBLE BUILD.  Do not remove.
rem
rem  Without it link.exe stamps the current time into the PE header
rem  (COFF TimeDateStamp + the copy in the debug directory), so rebuilding
rem  the very same sources yields a different file -- measured: identical
rem  length, exactly 4 differing bytes, brand new SHA-256 every time.
rem
rem  That matters because the packaging scripts now rebuild the hook on every
rem  run (D87) and record the DLL's SHA-256 in server\manifest-hook.json (D85).
rem  A timestamp-only churn would mean: three files dirty in git after every
rem  build, and every previously shipped client of the SAME version suddenly
rem  failing the integrity check against a freshly built server package.
rem  With /Brepro the timestamp becomes a hash of the content, so
rem  same sources == same bytes == same SHA, and none of that happens.
rem  (Pinned by server\test_hookintegrity.py, class ReproducibleBuildTests.)
rem --------------------------------------------------------------------------

echo [build] compiling bshook.dll ...
rem  sha256.c is shared with the updater (CNG / bcrypt).  It is compiled in as a
rem  second translation unit rather than copied, so there is only ONE SHA-256
rem  implementation in the repo.  /I lets its own `#include "sha256.h"` resolve.
cl /nologo /W3 /O2 /MT /utf-8 /Brepro /LD /I "%SRC%..\updater\src" "%SRC%bshook.c" "%SRC%..\updater\src\sha256.c" /Fe:bshook.dll /link /Brepro kernel32.lib user32.lib
if errorlevel 1 (
    echo [build] bshook.dll FAILED
    popd
    exit /b 1
)

echo [build] compiling bsloader.exe ...
cl /nologo /W3 /O2 /MT /utf-8 /Brepro "%SRC%bsloader.c" /Fe:bsloader.exe /link /Brepro kernel32.lib user32.lib
if errorlevel 1 (
    echo [build] bsloader.exe FAILED
    popd
    exit /b 1
)

del /q *.obj >nul 2>&1
del /q *.exp >nul 2>&1
popd

rem --------------------------------------------------------------------------
rem  Build gate: the notice must NOT be findable as plaintext in the DLL.
rem  A DLL that leaks it is a defect, not a warning -- fail the build.
rem  errorlevel 9009 means the interpreter itself is missing; that is the one
rem  case we downgrade to a warning (same policy as the generators above).
rem --------------------------------------------------------------------------
"%PYEXE%" "%GENNOTICE%" --verify-dll "%OUT%\bshook.dll"
if errorlevel 9009 (
    echo [build] WARNING: python not available, skipped the notice plaintext check
) else if errorlevel 1 (
    echo [build] FAILED: the login notice is readable as plaintext in bshook.dll
    exit /b 1
)

rem --------------------------------------------------------------------------
rem  Refresh server\manifest-hook.json with the SHA-256 we just produced.
rem
rem  The server rejects a client whose bshook.dll hash is not the one listed for
rem  its version -- including "version not listed at all".  So on a dev machine,
rem  rebuilding the hook WITHOUT refreshing the manifest would lock you out of
rem  your own server on the next launch.  Version comes from build-ver.config.
rem --------------------------------------------------------------------------
set "GENHOOKMAN=%SRC%..\tools\gen_hook_manifest.py"
rem  Exit code 2 = build-ver.config names a version that is OLDER than the
rem  newest one in tools\update-manifest.json, and the DLL we just built hashes
rem  differently.  Old versions are frozen: their players are already out there
rem  and a new hash would mark every one of them as "tampered".  The newest
rem  version (the one being worked on) is always refreshed freely.
"%PYEXE%" "%GENHOOKMAN%"
if errorlevel 9009 (
    echo [build] WARNING: python not available, manifest-hook.json NOT refreshed
) else if errorlevel 2 (
    echo [build] ********************************************************************
    echo [build] *  manifest-hook.json NOT refreshed: tools\build-ver.config names  *
    echo [build] *  a version OLDER than the newest release and the hook changed.  *
    echo [build] *  Old versions are frozen -- bump build-ver.config and rebuild.  *
    echo [build] *  Until then your own server will reject this DLL ^(D85^).        *
    echo [build] ********************************************************************
) else if errorlevel 1 (
    echo [build] WARNING: could not refresh server\manifest-hook.json
)

echo.
echo [build] done, output: %OUT%
dir /b "%OUT%"
exit /b 0
