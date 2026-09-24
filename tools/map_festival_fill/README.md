# map_festival_fill —— 补齐庆典系列地图缺失的贴图和特效

原版庆典三张对战图（`Festival00` / `Festival01` / `Festival02`）的 `.map` 引用了 22 张 PNG 和 28 个 `.efx`，
客户端包里从来没有过这些文件；碰撞是烘好的，所以玩家会撞到看不见的地板、箱子、绳桥和灯笼。
这个目录的脚本把它们生成出来，**不改任何 `.map` / `map.ini`**。

```text
mapscan.py   --report   看缺什么（也能 --all 扫全部地图）；--diag 出诊断图和轮廓图
build.py               生成到 logs\map_festival_fill\out\（看效果）；--install 写进 Pack_develop\Maps\Festival\
preview.py             把三张图离线合成成「补前 / 补后」整图 + 局部对照，到 logs\map_festival_fill\preview\
effects.py             特效的捐赠表：缺失名 -> 库里哪一个 efx、改哪几个参数
art.py                 像素工具
ingame.py              实机看图：login / room / start / tour <地图>，按点位传送截图拼表到 logs\map_festival_fill\ingame\
```

用开发 Python（`C:\Python314\python.exe`，要 numpy + Pillow）。实装后跑 `tools\build-pack.bat` 重打
`Maps~Festival.pkn`（先 `stop.bat`，卷被客户端占着时打包会拦）。
