@echo off
chcp 65001 >nul
setlocal
rem ---------------------------------------------------------------------------
rem  update-gamedata.bat —— 一键重跑**全部**原版数据的提取（D53） 
rem
rem    [1/5] 地形数据   server\bot_mapdata\        tools\mapdata.py 
rem    [2/5] 武器表     server\bot_weapons.json    tools\weapondata.py 
rem    [3/5] 角色属性   server\bot_chrprops.json   tools\chrprops.py 
rem    [4/5] 物品表     server\shop_items.json     tools\shopdata.py 
rem    [5/5] 图标图集   server\web\itemicons.*     tools\shopicons.py 
rem
rem  这是**唯一**一个更新数据的脚本（以前是五个 update-*.bat，已合并进来）。 
rem  五份产物都进 git、都进发布包；打包时**不会**自动重跑（D53）， 
rem  只有改了提取器、或者换了一份原版素材，才需要手动跑一次这个。 
rem
rem  ★ 每一步都是「提取 + 立刻跑对应的测试」。哪一步不过就停下、后面的不跑 
rem    —— 产物可能是坏的，先别打包。 
rem  ★ 顺序有依赖：图标图集要读物品表，[5] 必须排在 [4] 后面。 
rem
rem  用法： 
rem    tools\update-gamedata.bat
rem    tools\update-gamedata.bat D:\git\popshot-reborn\main\Pack_decrypt
rem
rem  不给参数时，提取器自己去 Pack_decrypt\ 和 ..\..\main\Pack_decrypt\ 找素材； 
rem  只有素材在别处（本工作副本里没有）才需要指路。 
rem ---------------------------------------------------------------------------
set "ROOT=%~dp0.."
set "PACK=%~1"
set "RC=0"
set "STEP="

set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" goto :nopython

rem 图标那一步要 Pillow：开发机的 C:\Python314 有，便携运行时里没有 
rem （服务端只用标准库，它不该有）。这里探一个能 import PIL 的解释器。 
set "PYPIL="
for %%P in ("C:\Python314\python.exe" "%ROOT%\runtime\python\python.exe") do (
    if not defined PYPIL (
        if exist %%P (
            %%P -c "import PIL" >nul 2>&1
            if not errorlevel 1 set "PYPIL=%%~P"
        )
    )
)

set "MAPARG="
set "WPNARG="
set "CHRARG="
set "SHOPARG="
set "ICONARG="
if not "%PACK%"=="" if not exist "%PACK%\Data\weapon.ini" goto :badpack
if not "%PACK%"=="" set MAPARG=--pack "%PACK%"
if not "%PACK%"=="" set WPNARG=--ini "%PACK%\Data\weapon.ini"
if not "%PACK%"=="" set CHRARG=--ini "%PACK%\Data\ChrProps.ini"
if not "%PACK%"=="" set SHOPARG=--shop-ini "%PACK%\Data\ShopItem-Chn.ini" --bonus-ini "%PACK%\Data\EquipBonus-Chn.ini" --weapon-ini "%PACK%\Data\weapon.ini" --promotion-ini "%PACK%\Data\Promotion-chn.ini"
if not "%PACK%"=="" set ICONARG=--src "%PACK%\Images\Shop"
if not "%PACK%"=="" echo [*] 素材目录：%PACK% 

echo.
echo ===== [1/5] 地形数据 -^> server\bot_mapdata\ ===== 
set "STEP=地形数据" 
"%PY%" "%ROOT%\tools\mapdata.py" --verify %MAPARG%
if errorlevel 1 goto :extfail
"%PY%" "%ROOT%\server\test_mapdata.py"
if errorlevel 1 goto :testfail

echo.
echo ===== [2/5] 武器表 -^> server\bot_weapons.json ===== 
set "STEP=武器表" 
"%PY%" "%ROOT%\tools\weapondata.py" %WPNARG%
if errorlevel 1 goto :extfail
"%PY%" "%ROOT%\server\test_weapondata.py"
if errorlevel 1 goto :testfail

echo.
echo ===== [3/5] 角色属性表 -^> server\bot_chrprops.json ===== 
set "STEP=角色属性表" 
"%PY%" "%ROOT%\tools\chrprops.py" %CHRARG%
if errorlevel 1 goto :extfail
"%PY%" "%ROOT%\server\test_chrprops.py"
if errorlevel 1 goto :testfail

echo.
echo ===== [4/5] 物品表 -^> server\shop_items.json ===== 
set "STEP=物品表" 
"%PY%" "%ROOT%\tools\shopdata.py" %SHOPARG%
if errorlevel 1 goto :extfail
"%PY%" "%ROOT%\server\test_shopdata.py"
if errorlevel 1 goto :testfail

echo.
echo ===== [5/5] 物品图标图集 -^> server\web\itemicons.png ===== 
set "STEP=物品图标图集" 
if not defined PYPIL goto :nopillow
"%PYPIL%" "%ROOT%\tools\shopicons.py" %ICONARG%
if errorlevel 1 goto :extfail
"%PY%" "%ROOT%\server\test_web_admin.py"
if errorlevel 1 goto :testfail

echo.
echo [ok] 五份都提取完了，测试也都过了。 
echo      改了哪些文件看 git status；打包时 Copy-* 会再核一遍条数和 format。 
echo.
echo      要单独跑某一个、或者看细节，直接调 python： 
echo        "%PY%" tools\mapdata.py --verify Camel00
echo        "%PY%" tools\weapondata.py --dump 1002010
echo        "%PY%" tools\chrprops.py --dump 2
echo        "%PY%" tools\shopdata.py --dump-kind material
echo        "%PY%" tools\shopicons.py --check
goto :end

:nopython
echo [x] 找不到 Python（试过 C:\Python314 和 %ROOT%\runtime\python） 
set "RC=1"
goto :end

:badpack
echo [x] "%PACK%" 不像 Pack_decrypt 目录（底下没有 Data\weapon.ini） 
set "RC=1"
goto :end

:nopillow
echo [x] 图标图集这一步要 Pillow，但两个 Python 都没有： 
echo     C:\Python314\python.exe 和 %ROOT%\runtime\python\python.exe 
echo     装一个：C:\Python314\python.exe -m pip install pillow 
echo     ★ 前面四份已经更新好了，只有图标没重拼（仓库里那份还在，能用）。 
set "RC=1"
goto :end

:extfail
echo.
echo [x] %STEP%：提取失败，产物没有更新，后面的步骤没跑。 
set "RC=1"
goto :end

:testfail
echo.
echo [x] %STEP%：测试没过 —— 产物可能是坏的，先别打包。后面的步骤没跑。 
set "RC=1"

:end
echo.
pause
exit /b %RC%
