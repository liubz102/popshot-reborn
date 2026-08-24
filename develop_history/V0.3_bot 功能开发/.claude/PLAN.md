# V0.3 总计划 —— 房间 bot

**目标**：人少时房主敲一条聊天命令就能加 bot 陪打，bot 在房间里像个正常玩家，
进图后会走会跳会开枪，难度适中。

**每个里程碑都以「实机能看到的现象」收尾，不以「代码写完了」收尾。**

---

## 里程碑

| # | 名字 | 交付 | 状态 |
|---|---|---|---|
| M0 | 进度管理体系 | 本套文件 + 根目录指针 CLAUDE.md | ✅ 完成 |
| M1 | 房间内的 bot | `/bot` `/char` `/team` `/del` `/ready` `/help`，bot 占座、显示、可增删改 | ⬜ 未开始 |
| M2 | 开局链路 | bot 跟着进图（站着不动）、会被打死、会复活、进结算、能回房间 | ⬜ 未开始 |
| M3 | bot 会动 | 逆 `UdpPacket` 内层 body，服务端合成心跳 / 开火 / 跳跃 | ⬜ 未开始 |
| M4 | 地图地形数据 | 逆 `.map`、`tools/mapdata.py` + 一键 bat + 打包钩子 | ⬜ 未开始 |
| M5 | bot AI | 寻路、追敌 / 跟随、瞄准与遮挡判定、难度旋钮 | ⬜ 未开始 |
| M6 | 测试 · 兼容 · 收尾 | 单测、Win7(3.8) 绿、文档、打包回归 | ⬜ 未开始 |

M1 → M2 → M3 有严格依赖；**M4 可以和 M1~M3 并行**（纯离线，不碰服务端运行时）。
M5 依赖 M3 + M4。

---

## M1 · 房间内的 bot（不进游戏）

新文件 `server/bot.py`。纪律和 `lobby.py` 一样：**模型和策略在这里，组包发包留在
`gameserver.py`** —— 这样它能脱离 socket 单独测。

### 要写的东西

- **`BotConn`** —— 假连接。继承 `Conn` 但**不调 `Conn.__init__`**（那里面全是 socket、
  密码流、文件句柄），自己的 `__init__` 只设必要字段；覆盖 `send()` / `send_batch()` /
  `close_now()` / `kill_stream()` 为无副作用版本。
  ★ **继承是为了兜底**：任何没覆盖到的方法被调到时不会 `AttributeError` 炸掉真人那条线程。
- **`BotManager`** —— 房间级。座位表、昵称、角色、队伍、难度。
- **命令层** —— `Conn.on_chat()` 解析出 `text` 之后、广播之前插一层
  `bot.handle_command(conn, text)`，返回 `True` = 已消费（不再广播原文）。

### 命令表（★ 只有房主有效；别人发的原样当聊天广播出去，不要吞）

| 命令 | 行为 |
|---|---|
| `/bot` | 最小空座加一个 bot，昵称 `bot N`（N = **服务端座位号 0..5**，和日志、协议一致） |
| `/char N M` | N = 座位号，M = **人物选择面板序号 1..14**（1/2/3 → 角色 id 0/1/2；4..14 → 100..110） |
| `/team N` | 切换 bot 队伍（1↔2）。个人战 / 闯关房无效，提示原因 |
| `/del N` | 删除 bot |
| `/ready` | 所有 bot 置 ready |
| `/h` `/help` `/?` | 回显完整命令表 + 角色序号对照表 |

失败时**必须说清具体原因**（满座 / 不是房主 / 不在房间 / 游戏中 / 座位上不是 bot /
序号越界），用 `broadcast_system_chat()` 那条无前缀的系统提示行发给房主。

### 要改的现成代码

- `lobby._leave_unlocked()` 的房主迁移循环 → **跳过 bot 座**（D2）。
- `Lobby.leave()` 之后判「房里还有没有真人」→ 没有就摘掉所有 bot、房间照常解散。
  ★ 这条**在房间里和游戏中都要生效**。

### 验收（实机）

真人建房 → `/bot` → 客户端房间里出现新座位、有 3D 模型、昵称 `bot 1`；
`/char 1 7` 模型跟着变；`/team 1` 在组队房里换边、在个人战房里给出提示；
`/del 1` 模型消失；真人退房后房间在服务端消失（`tools/gs_ctl.py` 查得到）。

---

## M2 · 开局链路（bot 进图，站着不动）

- **加载完成上报**：bot 在**收到房间广播的 `0x0400 gspPrepareGame` 那一刻**
  同步标记 loaded 并触发 `RoomStartGame.on_loaded(bot_conn, members)`。
  ★ **事件驱动，不许用「延迟 X 毫秒后上报」**（CLAUDE.md 铁律 10）。
- **控制者交接**（§4 坑 1）：进 stage 7 后立刻 `handover_controller_slots()`，
  把 bot 的控制格交给真人，否则闯关模式里一部分怪从开局就没人模拟。
- **死亡**（§5 坑 2）：`on_report_hp_zero()` 对 bot 座位放宽「只认本人上报」。
- **重生**：广播 `0x0406` 之后服务端主动发 `0x0419`，走
  `RoomQuest.arm_respawn_watchdog` / `respawn_point_for()` 那条现成的路。
- **同步开关**（§6 坑 3）：`sync_peer_relay()` 的判据从「房里几条连接」
  改成「房里几个**会动的座位**（真人 + bot）」，否则 1 真人 + N bot 时通道 A 被关掉。
- **结算**：`settlement_seats()` / `send_end_game()` / `build_rep_game_result()`
  能处理 `account is None` 的座位；bot 的奖励算完丢弃。
- **换图**（闯关）：`0x0412` 新图加载完，bot 同样在收到换图广播时立刻上报。
- 中途真人掉线、房主掉线、游戏中最后一个真人掉线，各走一遍。

### 验收（实机）

真人 + 1 bot 开一局：bot 站在出生点，能被打死、几秒后复活、结算表里有它、
看完结算能回房间再开一局（局号换代正确、房间不黑）。

---

## M3 · bot 会动、会开枪

- 逆完 `0x4001` 心跳 body 剩余字段（本版 §3 已有大半）。
- 逆 `rpFire`(0x0002, 26B) / `rpExplode`(0x0003, 28B) / `rpJump`(0x0006, 2B) /
  `rpChangeWeapon`(0x0001, 5B)。语料：`bug调查/server_logs/game_003_27799.dec.bin`
  （心跳 11925 发、rpFire 4394 发、rpExplode 4390 发，本版 §2）。
- 新文件 `server/botsync.py` —— 合成 `UdpPacket`。**三条不变式写成断言**（D5）：
  1. 事件包序号（头 `+8`）从 0 **严格连续递增**；
  2. 心跳的 N（body `+0..1`）**恒等于已发出的事件包数**，绝不越过；
  3. 每个收件人的局号（头 `+4`）按**收件人自己的** epoch 重新盖章。
- 投递走现有 `relayserver.deliver()` / `battle_broadcast()` 的路径，**不新增第二条**。

### 验收（实机）

bot 在真人屏幕上走动、跳跃、开枪；子弹能打到真人身上并扣血；
`tools/probe_sync.py` 里 bot 座位的收包队列 base 正常推进，无「打不死人」现象。

---

## M4 · 地图地形数据（可与 M1~M3 并行）

- 逆 `.map` 的 **v8 / v18 两种布局**（`D:\git\popshot-reborn\main\Pack_decrypt\Maps\`，
  174 张 `.map`，头部见本版 §7）。
  **只要两样东西**：站立面（能走的地面）和弹道遮挡体。别贪多。
- `tools/mapdata.py`：批量解析 → `server/data/mapdata/`。
  带 `--verify` 输出可视化 PNG，人工核对几张。
- `tools/update-mapdata.bat`（**CRLF + UTF-8 无 BOM**）：一键重跑。
- 打包接入：`tools/build-common.ps1` 的 `Copy-ServerCode` 加拷 `server/data/mapdata/`；
  `build-menu.ps1` / `build-portable.ps1` / `build-server-package.ps1` 打包前自动跑一次，
  **产物缺失或解析失败就中止打包**（照 `Get-ServerSourceFile`「明显不对就炸」的风格）。

### 验收

174 张全部解析成功；抽 3~5 张（含对战图和闯关图）导出可视化，地面线和游戏里目视一致；
`tools/build.bat` 跑一遍，产物里有 mapdata。

---

## M5 · bot AI

- **移动**：地形上的可达性搜索 + 跳跃弧线。
  对战 = 靠近最近敌人到射程内；闯关 = 跟随最近真人推进 + 就近打怪。
- **瞄准**：抛物线解算 + 用 M4 的遮挡体判「这一发会不会被墙挡住」。
- **难度**：反应延迟、瞄准角误差、开火间隔**三个连续量**，默认「中等」。
  ★ 不许用「每 N 次故意打偏」这种计数阈值（铁律 10）。
- 可选：`server.config` 加 bot 难度项（沿用 `config.py` 的宽容解析 + fail-open）。

### 验收（实机）

1v1 和 2v2 各打几局，主观判定「打得过但不轻松」；
闯关模式 bot 会跟着真人推进、不卡在地形里。

---

## M6 · 测试 · 兼容 · 收尾

- `server/test_bot.py` / `test_botsync.py` / `test_mapdata.py`，挂进 `run_tests.py`。
- **Win7 兼容**：`runtime-win7\python\python.exe server\run_tests.py` 必须绿（CPython 3.8）。
- `re/packet_api.md` §5 补齐所有新逆出的内层 opcode body；README 更新。
- 打包 + 实机跑一轮完整回归。

---

## 明确不做的事

- **客户端版本号 / 升级提示链** —— V0.2 已完整实现并实机验证（本版 §8），V0.3 不动它。
- **bot 进大厅 / 跨房间** —— bot 只活在一个房间里。
- **bot 玩道具模式的道具** —— M6 之后视情况再说，不进主线。
- **bot 占座时给真人让位** —— 用户 2026-08-25 决定：**照常挡住**，房主得先 `/del`。
