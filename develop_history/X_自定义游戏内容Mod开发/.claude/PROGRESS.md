# X_Mod PROGRESS —— 现在在哪

**只保留当前状态，全文 ≤ 150 行。** 做完的事从「正在做」挪走，不留历史；流水账进 `sessions/`。

最后更新：2026-09-19（会话 09）

---

## 现在在哪

**X3「9 把自定义黄金武器 + GM 页可调属性 / 说明文」代码和资源全部做完、两套运行时 3950 项全绿、
hook 编译过并在实机装上了钩子；剩下两件事等用户：① 选撞色（效果图已交）② 实机 V26~V30。**
X1 / X2 的实机项（V15~V25）仍在等，见下面那张表。

| X3 里程碑 | 状态 |
|---|---|
| 原版 `weapon.ini` 逐字节恢复 + 守卫（`test_weaponini`）| ✅ |
| 资源生成链 `tools/goldwp/{spec,palette,preview,build}.py` + `tools/amftool.py`（186 个文件，占位配色 A）| ✅ |
| 效果图三张（模型 / 精灵图标 / 特效贴图 × 黑曜 A / 宝蓝 B / 银白 C）| ✅ 已交，⏳ 等选 |
| 服务端：`weaponcfg.py` 第七份配置、说明文 / 名字、`0x0F01` 登录后发 + 保存后广播、管理页两发接口 | ✅ |
| 前端：物品库武器卡片「自定义属性」钮 + 左 PVE / 右 PVP 弹窗 | ✅ |
| hook：吞 `0x0F01` / Load 返回点重施加 / 主线程补写（`bshook.c` 新段）| ✅ 编译过、实机装上 |
| 打包：16 卷重写、五份产物重生成；版本仍是开发版 **0.4.1**（用户定的，别抬），`manifest-hook.json` 的 0.4.1 刷成新 DLL 哈希 | ✅ |
| 文档：packet_api §3.5 `0x0F01` + §6.7 武器记录；FINDINGS §36~§40；DECISIONS D26~D30；PLAN X3 | ✅ |

**本轮改的文件**（全部停在工作区，没有提交）：`game_patched/Pack_develop/Data/{weapon.ini,ShopItem-Chn.ini,default.amf}` +
186 个新资源 + `Pack_publish/` 16 卷 + `pack-index.json`；`tools/goldwp/*`、`tools/amftool.py`、`tools/shopdata.py`、
`tools/gamedata-stamp.json`、`tools/build-ver.config`；`server/{weaponcfg,shopcfg,shopdata,shopdefaults,versioning,gameserver,
shop_items.json,bot_weapons.json,manifest-hook.json}`、`server/web/{admin.py,admin.html,admin.js,admin.css,itemicons.*}`；
`hook/bshook.c` + `hook/bin/{bshook.dll,bsloader.exe}`；`BUILD.ver`；`test/{test_weaponcfg,test_weaponini,test_amftool,
test_patchsites_wtab}.py` + `test_web_admin` / `test_shopcfg` / `test_weapondata` 改动；`re/packet_api.md`；本套进度文件。

## 下一步

0. ★★ **版本号由用户定，别擅自抬**（用户 2026-09-19 原话：「0.4.1 还没发呢，这个就是现在的开发版本」；线上是 V0.4.0）。
   我一度抬到 0.4.2，用户全部改回；顺手加的 `WEAPON_TABLE_MIN_VERSION` 门控也**删了**（用户：版本门控只有
   `server-ClientFilter.config` 一道，别重复加）。清单里 0.4.1 = 新 DLL 的哈希。
1. 撞色**已选 C 银白**（2026-09-19），`build.py --variant C` 已重跑、资源包重打（火墙按同一规则改色）。
2. **实机 V26~V30**（下表）。V27 前先在管理页「金币 / 经验」页把一把自定义武器发给自己（出厂不上架）。
   ★ 服务端代码在用户登着时又改过（浮窗两套数值、提示语缩短），要 `stop.bat` / `start.bat` 一次才生效。
3. 发版：客户端包 + 服务端包都要重打（资源 + `bshook.dll` + 服务端代码）。
4. 可选（用户没要）：2 号武器的碎片 / 火墙子弹药数值也开放；hook 强制刷新仓库提示框（D29 否掉，除非用户改口）。

## 当前卡点

**没有卡点。** 两条线都等用户：选颜色、实机。

DLL 已按最新源码重编（2026-09-19 02:28，哈希 `fc1e698e…`），清单 0.4.1 那条 = 它。

★ 实机已经闭环一次（2026-09-19 02:12，用户登着 `testuser1`）：管理页保存 → 服务端「推给 1 位在线玩家」→
hook「收到自定义武器表 v2：9 条（主线程）」「按【对战】写入 9/9 条」。登录那一刻武器表还没加载（0/9），
启动那次 Load 在进大厅时才跑，返回点上补写成功 —— 这是预期，不是 bug（§36）。

## ⏳ 待用户验证

**X3（本轮，★ 先重启客户端：`bshook.dll` 和资源包都换了）**：

| # | 怎么做 | 期望看到 | 状态 |
|---|---|---|---|
| V26 | 看三张效果图 `tools/goldwp/preview_*.png` | 选 A / B / C（或每把不同）；火墙留原色还是改金 | ⏳ |
| V27 | 管理页「金币 / 经验」页给自己发一把 `1920001`（左轮 自定义）；进仓库看、装备、进图开枪 | 仓库有金色图标、名字「左轮手枪 自定义」，能装备；手里是金枪，子弹 / 拖尾 / 命中特效金色 | ⏳ |
| V28 | 对战房里，管理页物品库 → 这把武器「自定义属性」→ 右栏 PVP 身体伤害改 1 → 保存 → 打 bot | **不用重登**，下一枪掉血明显变少；改回参考值就恢复。再开一局照样生效 | ⏳ |
| V28b | 闯关房里同一把枪，左栏 PVE 身体伤害改成明显不同的数 | 打怪掉血按 PVE 那套；`logs/bshook_*.log` 里对战房出现「按【对战】…来源 00497CBB」、闯关房「按【任务】…来源 004A3B81」；训练场 / 教程各进一次看落在哪一套 | ⏳ |
| V29 | 看提示框：商店（若上架）/ 仓库 | 自定义武器第一行「仅显示PVP属性，PVE的请看管理页」**不折行**，数值是 PVP 那套；改说明文后商店提示框即时、仓库提示框重登后更新 | ⏳ |
| V30 | 一台仍是 V0.4.1 的客户端连本地服务端 | 被版本门禁拒（预期）；服务端日志里对它**没有**「发 0x0F01」 | ⏳ |

★ 看结果的地方：`logs/server.out` 搜「发 0x0F01」「自定义武器表已推给」；`logs/bshook_*.log` 搜「WTAB」——
「收到自定义武器表 vN：9 条（线程 …）」那一行会写明**收包在不在主线程**（不在的话会在下一次出站包时补写，也是对的）。

**X1 / X2 遗留（2026-09-18 起等）**：V15 声线 / V16 `Jab00` 特效 / V18 低等级号能选爱琳 / V20 蝴蝶不绕尸体 /
V21 蝴蝶打中 bot 减速 / V22 商店没有爱琳 / V23 bot 蹭图腾 / V24 「♥HEAL」不崩 / V25 Alt+Tab 不崩 ——
做法和期望见 `sessions/2026-09-18-03.md` 与上一版 PROGRESS（git 里）。

**出问题时一次给全这 4 样**：① 卡在哪一条；② `game_patched/BigShot.rpt` 的**最后一段**；
③ `logs/bshook_*.log` 的**最后 50 行**；④ 崩溃地址（我拿 `tools/re_bs.py dis <地址>` 定位）。

---

## 给接手会话的十句话

1. **原版武器一个字节不许改**（用户 2026-09-19）：`test_weaponini` 钉着 `weapon.ini` 前 221519 字节的 sha256。
   想调数值去管理页物品库的「自定义属性」弹窗，只有 9 把自定义武器能调。
2. ★★ **自定义武器的数值链**：`server/data/weapons.json` → `weaponcfg.effective()` → `0x0F01`（登录后 + 保存后广播）→
   bshook `wtab_*` 写内存；`weapon.ini` **每次进图重读**，所以 hook 挂在 `WeaponTable::Load` 返回点上再写一遍（§36 / D26）。
3. PVE / PVP 由 hook 按 Load 的返回地址挑（D27），别改成服务端按房间推。
4. 自定义资源全部由 `tools/goldwp/build.py` 从爆裂 3 母本生成，**幂等**；改色规则在 `palette.py`，清单在 `spec.py`。
   手改生成物会在下次重跑时被盖掉。
5. `default.amf` 只许经 `tools/amftool.py` 改（定长记录，写错一格整张表读歪，§37）。
6. `shop_items.json` **FORMAT 2**、`bot_weapons.json` 13：改字段必须和重生成的产物同一次落地，对不上就是空表。
7. 部位码 `92` = 自定义武器（`tools/shopdata.PART_KIND`）；物品 id `X92000S`、武器 Id `100C9S0`（D28）。
8. `_` 前缀（全年龄）资源键在自定义小节里全指黄金资源，别给它们另配气泡版（D28）。
9. **版本号只由用户定**：`build-ver.config` 里的号是尚未发布的开发版本（现在 0.4.1，线上是 V0.4.0），
   改 hook 直接 `hook\build.bat` 刷它那一条清单就行；别自己抬号，也别把它当成已发布版本。
10. ★ 跑测试：`runtime\python\python.exe test\run_tests.py`（3.14）和 `runtime-win7\python\python.exe test\run_tests.py`（3.8），
    全量名单现扫 `test/test_*.py`。
