@echo off
chcp 65001 >nul
setlocal
rem ---------------------------------------------------------------------------
rem  update-shopdata.bat —— 重新从原版 ini 提取物品表（V0.3 合成与商店 M1）
rem
rem  产物：server\shop_items.json（进 git、进服务端包）
rem
rem  商店 / 合成 / 仓库全靠它：
rem    PartFlag  装备槽冲突判定的唯一依据（穿两件上衣就是它算错了）
rem    Tag       武器 -> weapon.ini 的弹药 id，带出伤害和换弹速度
rem    加成      商店和仓库里显示「这件加多少」
rem    id 存不存在  ★ 中文版客户端不认识的 id 发下去，界面上就是个空格子
rem
rem  ★ 素材 Pack_decrypt\ 太大没进本工作副本，只在 main worktree 里。
rem    找不到就指路，例如：
rem      tools\update-shopdata.bat --shop-ini D:\git\popshot-reborn\main\Pack_decrypt\Data\ShopItem-Chn.ini
rem ---------------------------------------------------------------------------
set "ROOT=%~dp0.."
set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" (
    echo [x] 找不到 Python（试过 C:\Python314 和 %ROOT%\runtime\python）
    exit /b 1
)

echo [*] 提取物品表……
"%PY%" "%ROOT%\tools\shopdata.py" %*
if errorlevel 1 (
    echo [x] 提取失败，产物没有更新
    exit /b 1
)

echo.
echo [*] 跑一遍物品表的测试……
"%PY%" "%ROOT%\server\test_shopdata.py"
if errorlevel 1 (
    echo [x] 测试没过 —— 产物可能是坏的，先别打包
    exit /b 1
)

echo.
echo [ok] 完成。产物：%ROOT%\server\shop_items.json
echo      看某几件：tools\update-shopdata.bat --dump 1120041 30018
echo      看某一类：tools\update-shopdata.bat --dump-kind material
endlocal
