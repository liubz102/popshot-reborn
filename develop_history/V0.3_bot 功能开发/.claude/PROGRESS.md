# V0.3 PROGRESS —— 现在在哪

**只保留当前状态。** 做完的事从「正在做」挪走，不留历史；流水账进 `sessions/`。

最后更新：2026-08-25（会话 05）

---

## 现在的位置

**M2 和 M3a 的代码都写完了，两块都 ⏳ 等实机验证（可以一起验）。**

| # | 里程碑 | 状态 |
|---|---|---|
| M0 | 进度管理体系 | ✅ 完成（会话 01） |
| M1 | 房间内的 bot（`/bot` `/char` `/tm` `/del` `/ready` `/h`） | ✅ **完成**（实机验证通过） |
| M2 | 开局链路（bot 进图、会死会复活、进结算） | 🟡 代码 + 单测完成，⏳ **等实机验证** |
| M3a | bot **会动会跳**（`botsync.py` 合成器 + 帧驱动） | 🟡 代码 + 单测完成，⏳ **等实机验证** |
| M3b | bot **会开枪** | ⬜ 先要逆 `rpFire` 的收侧「伤害在哪台算」（D18） |
| M4 | 地图地形数据（逆 `.map` + 工具 + 打包钩子）**可并行** | 🟡 **格式已全部逆出**（§17 / §18），工具还没写 |
| M5 | bot AI（寻路 / 追敌 / 瞄准 / 难度） | ⬜ |
| M6 | 测试 · Win7 兼容 · 文档 · 打包回归 | ⬜ |

详细内容见 `PLAN.md`。

★ **git 工作区是脏的，我没有提交** —— 用户 2026-08-25 明确要求
「任何情况下都不要对我的 git 进行 commit / push / revert」。改完停在工作区，
提交由用户自己来。

---

## M1 做完了什么（代码已落盘）

| 文件 | 改动 |
|---|---|
| `server/bot.py` | **新增**。`BotConn`（D1）+ 命令层 + 面板序号换算（D6） |
| `server/lobby.py` | `Seat.is_bot`；`Room.bot_seats/human_seats/human_count/human_members`；`Lobby.add_bot/remove_bot`；房主迁移跳过 bot（D2）；最后一个真人走 → bot 全散 |
| `server/gameserver.py` | `send_system_chat` / `room_system_chat` / `broadcast_seat_leave`（§15）；`on_chat` 插命令层；`after_someone_left` 里 `check_pvp_finished` 改由**真人**发起 |
| `server/app.py` | 显式 `import bot`，让 bot.py 坏掉时**启动就炸**（§14） |
| `server/test_bot.py` | **新增** 44 个用例 |
| `server/test_lobby.py` | 加 `BotSeatTests`（纯模型，不碰协议） |
| `server/run_tests.py` | 挂上 `test_bot` |
| `tools/build-common.ps1` | 必选文件清单加 `bot.py` |

**会话 03 的两个修复**（用户首轮实机报的）：

| 报的问题 | 真因 | 改法 |
|---|---|---|
| `/team` 一点反应都没有 | 客户端把 `/team ` 当**队伍聊天前缀**自己吃掉了，服务端收到的只剩参数（§19） | 换队命令改名 **`/tm N`**；`/team` 留着回一行指路（D12） |
| `/h` 只看得到 4 行 | 房间聊天框一次就只显示 4 行，多发的被顶出去（§20） | 帮助从 8 行压到 **3 行**，多条命令挤一行、`;` + 两空格分隔，每行 ≤ 50 半角宽 |

**测试**：`python server\run_tests.py` → **1080 全绿**；
Win7 运行时（CPython 3.8）`runtime-win7\python\python.exe server\run_tests.py`
→ **同样 1080 全绿**。

**顺带发现**：§7 那个坑（只剩一人时同步被关掉）因为 D1 选了假连接，
**自动消失了，一行都不用改**（见 §13）。M2 的清单里可以划掉这一条。

★ **capstone 5.0.7 其实装着**（CLAUDE.md 的环境速查写错了，已改）——
`C:\Python314\python.exe` 直接 `import capstone` 就能反汇编 `re/BigShot_22524.img`
（文件偏移 = VA − 0x400000），§19 就是这么当场逆出来的。

---

## ✅ M1 实机验证结论（用户 2026-08-25 确认「没问题」）

`/bot` 加座位 + 3D 模型、连加多个角色轮换、`/char N M` 换模型不出韩文、
`/char` 越界报错、`/ready` 标记准备、`/del N` 名字和模型一起消失、
`/h` 三行都看得见、`/tm N` 在组队房换边 / 在个人战房给提示、光杆 `/team` 指路。

⇒ §19（客户端吃 `/team `）和 §20（聊天框只看得见 4 行）两条结论**已被实机确认**，
`/h` 的「3 行 × 50 半角宽」这个口径以后照着用。

### M1 剩下的边角（没单独验，但不挡 M2，随手看到再说）

客户端自带「踢出」按钮点 bot（单测已钉住，§13）、非房主敲命令、
房主退房 / 房主迁移跳过 bot。

---

## M2 做完了什么（会话 04，代码已落盘）

七步全做完了，每一步都有单测钉着（`test_bot.py` 的 `Bot*Tests` 那六个类）：

| 步 | 挂在哪个事件上 | 代码 |
|---|---|---|
| 1 加载完成上报 | **广播 `0x0400` 那一刻**（D4，无定时器） | `broadcast_start_game()` 里对 `room.bot_members()` 逐个 `battle.on_loaded()` |
| 2 控制者交接 | 进 stage 7、`room.quest` 刚建好之后 | `broadcast_start_game()` 对 `room.bot_seats()` 逐个 `handover_controller_slots()`；★ 接管者池收紧成 `room.human_seats()`（D14） |
| 3 死亡放宽 | — | `on_report_hp_zero()` 的幽灵上报判据加 `and not bot_seat`（D3）；去重多走一层时间窗（`record_death(many_reporters=True)`） |
| 4 重生 | 死后 `BOT_RESPAWN_DELAY_S` = **5 秒**（D13，铁律 10 的明文例外） | 复用 `arm_respawn_watchdog` 那把闩，期限由 `respawn_delay_for(seat)` 分流 |
| 5 结算 | — | ★ **一行都没改**：`send_end_game()` 本来就对 `account is None` 全程有守卫（§21） |
| 6 换图 | **广播 `0x0417` 那一刻**（同 D4） | `on_req_change_to_next_map()` 里对 bot 逐个 `quest.map_done()` |
| 7 掉线三种 | — | 现成的路都通（§21），只补了单测 |

| 文件 | 改动 |
|---|---|
| `server/lobby.py` | 加 `Room.bot_members()`（对称于 `human_members()`） |
| `server/gameserver.py` | 上表 1/2/3/4/6 五处；新常量 `BOT_RESPAWN_DELAY_S`；新方法 `Conn.is_bot_seat()` / `Conn.respawn_delay_for()`；`RoomQuest.record_death(many_reporters=)` |
| `server/test_bot.py` | 新增 22 个用例（`BotStartChainTests` / `BotDeathTests` / `BotRespawnTests` / `BotMapChangeTests` / `BotSettlementTests` / `BotPeerRelayTests` / `BotMidGameLeaveTests`） |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1102 全绿**；
`runtime-win7\python\python.exe server\run_tests.py`（CPython 3.8）→ **同样 1102 全绿**。

---

## ⏳ 待用户实机验证（M2 + M3a，★ 可以一起验）

★ **先重启服务端**（铁律 7：不重启就还是旧代码）。房主是你，`/bot` 加 1~2 个
bot，`/ready`，然后**按开始**。建议闯关房和对战房各来一局。

| # | 步骤 | 期望看到 |
|---|---|---|
| 1 | 按开始 | 加载条正常走完、**大家一起进图**（以前会卡在加载界面等 bot） |
| 2 | ★ 进图后**你走两步** | bot **跟着你走**（落在你身后约 120 的地方，慢半拍是正常的 —— 收方按 0.4 插值） |
| 3 | ★ 你**跳一下** | bot 走到那个位置时**也跳一下**（回放你的跳） |
| 4 | ★ 你站着不动 | bot 走到你身后就**停下**，不抽搐、不来回滑 |
| 5 | ★ bot 的位置 | **落在地面上**，不在墙里、不悬空、不在图外 |
| 6 | ★ bot 的朝向 | 走的方向和它面朝的方向**一致**（🤔 朝向那两位的正负是推测的，反了就说一声，改一个常量的事） |
| 7 | 打 bot 直到它血空 | bot **倒下、播死亡动画**（以前打不死） |
| 8 | 数 5 秒 | bot 自己**站起来**，然后继续跟着你走 |
| 9 | 闯关房：走到地图最右边 | 全房间一起换图，**加载条不卡住**；新图里 bot 还在 |
| 10 | ★ 换图刚进去那一下 | bot **不会先在墙里 / 图外闪一下**（换图会把位置轨迹清掉） |
| 11 | 闯关房：怪 | ★ **重点** —— 怪该刷的都刷、该动的都动，**没有一批怪从开局就杵着不动**（§5 / D14 就是修这个） |
| 12 | 打完一局 | 结算界面上 **bot 有自己那一行**（分数 / 生命那几格不是空的）；看完能回房间 |
| 13 | ★ 回房间后**再开一局** | 第二局 bot **照样会动**（换代对不上的话第一局正常、第二局静止，§26 就是修这个） |
| 14 | 两个 bot | 两个不叠在同一个点上，排成一前一后 |
| 15 | 局中你退房（房里还有别的真人） | 房主转给**那个真人**，那一局继续 |
| 16 | 局中最后一个真人退房 | 房间在大厅列表里消失（`python tools\gs_ctl.py` 查不到） |

★ **bot 现在不会开枪**（M3b，D18）。看到它只跑不打是**预期**，不是 bug。

**怎么告诉我结果**：一条一条回「行 / 不行」。不行的那条请贴
`logs\server.out` 里对应时间的十几行 —— 尤其是带 `0x0414`、`0x0406`、
`0x0419`、`0x0418`、`跨代丢弃`、`同代改写局号`、`不变式` 的那几行。

★ 「bot 一动不动」这个症状有**三个**长得一模一样的原因：
换代对不上（`跨代丢弃`）、位置字段填错、包压根没合成出来。
所以真出现了请一定贴日志，别只回「不行」—— 日志里那三行一眼能分开。

**当前状态**：等用户。

---

## M3a 做完了什么（会话 05，代码已落盘）

| 文件 | 改动 |
|---|---|
| `server/botsync.py` | **新增**。校验和 / `UdpPacket` 头 / 心跳 body（31）/ `rpFire`(26) / `rpExplode`(28) / `rpJump`(2) / `rpChangeWeapon`(5) + `BotSyncStream`（序号记账 + D5 三条不变式，违反当场 `SyncInvariantError`） |
| `server/bot.py` | ★ `BotConn.send()` 改成「只跑 `note_epoch_from_frame`」（§26）；`BotConn.sync` / `battle_pos` / `heading` / `last_frame_at`；`_align_epoch()`；**帧驱动** `tick_room()` / `_tick_bot()` / `trail_point()`；把 `tick_room` 挂到 `gameserver.BOT_ROOM_TICK` |
| `server/gameserver.py` | `Conn.sync_trail` / `sync_jumped` + 类级默认；`note_sync_position()`（在 `forward_peer_data` 里记位置和起跳）；`reset_sync_trails()`（换图 / 新一局各清一次）；`Conn.is_bot_conn()`；`_relay_battle_tick` 对 bot 直接 return + 调 `BOT_ROOM_TICK`；常量 `SYNC_TRAIL_POINTS` / `PEER_OP_JUMP` / `BOT_ROOM_TICK` |
| `server/udpsync.py` | `heartbeat_position()` + `PEER_HEARTBEAT_STATE_OFFSET` |
| `server/test_botsync.py` | **新增** 68 个用例（线格式 33 + 不变式 10 + 轨迹回放 7 + 战斗帧 18） |
| `server/test_room.py` / `test_battle.py` | 两个 `make_conn` 夹具补 `sync_trail` / `sync_jumped` |
| `server/run_tests.py` / `tools/build-common.ps1` | 挂上 `test_botsync` / 必选文件加 `botsync.py` |

**bot 现在怎么动**：每当房里**真人的同步包到达**（8 Hz，`_relay_battle_tick`），
bot 各走一帧 —— 取「最近那个真人的轨迹上往回退 120（第 N 个 bot 退 N×120）的
那个采样点」当自己的落脚点，合成一发心跳；那一段里真人跳过的话，
先补一发 `rpJump` 再发心跳。**位置回放真人走过的点**是因为服务端一点地图几何
都没有（D16）。

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1170 全绿**；
`runtime-win7\python\python.exe server\run_tests.py`（CPython 3.8）→ **同样 1170 全绿**。

★ 顺带把 §24 的三格语义改对了（§25 / `packet_api.md` §5.5 已同步）：
`+17..18` 是**角度（度）**不是速度、`+25..28` 是**准星屏幕坐标**、
`0x5f895c` 是**朝零截断**不是四舍五入；头 `+3` 从 ❓ 变成「恒 0」（91526 发）。

### M3b（还没做）：开火

`rpFire` / `rpExplode` 的组包和单测都做全了，**但战斗帧不发它们**（D18）。
先要逆清楚两件事：**伤害到底在哪一台算**、**只发 `rpFire` 不发 `rpExplode`
会怎样**。`rpFire` 包里没有弹体句柄、收方按顺序自己分配 —— 搞错就是
「打不死人」的老病根（V0.2 §216/§217）。

`rpAiMsg`(0x0011) 变长那一段仍未逆（组包点只写 8 字节，后面还有一发裸写）。

---

## 当前卡点 / 已知未知

| 事 | 状态 |
|---|---|
| `0x4001` 心跳 body 的完整布局 | ✅ **收发两侧逐字段都逆完了**（§24 布局 + §25 语义 / 收侧行为）。★ `+7..10` 才是位置 |
| `rpFire`(26B) / `rpExplode`(28B) / `rpJump`(2B) / `rpChangeWeapon`(5B) | ✅ **已逆出且对穿实包**（§23），组包已实现（`botsync.py`） |
| 位域低 2 位（`[char+0x2d0]`）是不是「朝向」 | 🤔 68% / 65% 的倾向，**没到证明**（§25）。反了只是「bot 背对着开枪」，实机看一眼就能定 |
| ★ **`rpFire` 的收侧：伤害在哪一台算 / 不发 `rpExplode` 会怎样** | ❓ **M3b 的前置**（D18）。没搞清之前 bot 不开枪 |
| `rpExplode` `+20` 那个位标志、`rpFire` `+1` 的武器槽 | ❓ 只知道取值范围，语义待用时再逆（§23） |
| `rpAiMsg`(0x0011) 的变长部分 | ❓ 组包点只写 8 字节，后面还有一发裸写没逆（§23） |
| `VELOCITY_PER_STEP`＝4.111 | 🤔 **回归出来的**（10886 组，同号率 95.5%），不是逆出来的。只影响走路动画观感，不影响任何判定（§25） |
| `.map` 文件 `+14+L` 之后的布局 | ✅ **已逆出**（§17）：19 类循环 + type 表，174 张全解通。版本是 **7 种不是 2 种**（§9 已勘误） |
| 地形的碰撞几何在哪 | ✅ **不在 `.map` 里，在地形 PNG 的 alpha 通道**（§18）。M4 的做法已相应改掉 |
| `HidingObj`(201) 是不是真挡子弹 | ❓ 未查。名字也可能只是「挡视线的前景」，M4 要确认 |
| 版本 < 12 的 21 张图（v7/8/9）字段顺序 | ❓ 那 7 个 float 在**外层记录**里，要单独核对一遍 |
| bot 用什么武器 | ❓ 未定。等 M3b 逆完收侧再说 |
| bot 在道具模式里要不要捡道具 | 🅿️ 暂不做（PLAN「明确不做的事」） |
| bot 的等级固定 `4`（`BOT_LEVEL`），显示上会不会突兀 | 🤔 只影响玩家列表那一格，实机看一眼再说 |

---

## 不要重做的事

- **客户端版本号 / 升级提示链** —— V0.2 已完成并实机验证，见 `FINDINGS.md` §12。
- **`UdpPacket` 校验和** —— 已逆出且本版重新实测 25091/25091 全中，
  直接抄 `tools/fakeclient.py` 的 `udp_checksum()`，见 `FINDINGS.md` §4。
- **§7 那个「只剩一人时同步被关掉」的对策** —— 已作废，见 `FINDINGS.md` §13。
