@echo off
chcp 65001 >nul
setlocal
rem ---------------------------------------------------------------------------
rem  update-shopicons.bat —— 重新把原版物品图标拼成图集（V0.3 合成与商店 M8）
rem
rem  产物：server\web\itemicons.png + itemicons.json（都进 git、进服务端包）
rem
rem  管理页靠它给每一件东西画图标 —— 664 个图标拼成一张 PNG8，
rem  0.62 MB / 一次请求，比 664 个小文件小 5 倍。
rem
rem  ★ 这个脚本要 Pillow：C:\Python314 里有，便携运行时 runtime\python 里
rem    **没有**（服务端只用标准库，它不需要 Pillow —— 只有这个离线脚本要）。
rem    所以下面只认带 Pillow 的解释器，探测不到就直接说清楚，不硬跑。
rem
rem  ★ 素材 Pack_decrypt\ 太大没进本工作副本，只在 main worktree 里。
rem    找不到就指路，例如：
rem      tools\update-shopicons.bat --src D:\git\popshot-reborn\main\Pack_decrypt\Images\Shop
rem
rem  ⚠ 往上面加中文注释时当心（V0.3bot FINDINGS 135 / start.bat 顶上那段）：
rem    `chcp 65001` 下 cmd.exe 按字节偏移在文件里来回跳、却按字符数数，
rem    多字节文本会让偏移漂移，最后把下面那个 for 块拦腰截断。
rem    现在这个长度实测没问题；再加就先跑一遍确认。
rem ---------------------------------------------------------------------------
set "ROOT=%~dp0.."
set "PY="
for %%P in ("C:\Python314\python.exe" "%ROOT%\runtime\python\python.exe") do (
    if not defined PY (
        if exist %%P (
            %%P -c "import PIL" >nul 2>&1
            if not errorlevel 1 set "PY=%%~P"
        )
    )
)
if not defined PY (
    echo [x] 找不到装了 Pillow 的 Python。
    echo     试过：C:\Python314\python.exe 和 %ROOT%\runtime\python\python.exe
    echo     装一个：C:\Python314\python.exe -m pip install pillow
    exit /b 1
)

echo [*] 用 %PY%
echo [*] 拼图集……
"%PY%" "%ROOT%\tools\shopicons.py" %*
if errorlevel 1 (
    echo [x] 生成失败，产物没有更新
    exit /b 1
)

echo.
echo [*] 跑一遍管理页的测试（图集接口要能把它吐出去）……
"%PY%" "%ROOT%\server\test_web_admin.py"
if errorlevel 1 (
    echo [x] 测试没过 —— 产物可能是坏的，先别打包
    exit /b 1
)

echo.
echo [ok] 完成。产物：
echo      %ROOT%\server\web\itemicons.png
echo      %ROOT%\server\web\itemicons.json
echo      只检查素材齐不齐：tools\update-shopicons.bat --check
endlocal
