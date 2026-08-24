# V0.3 PROGRESS —— 现在在哪

**只保留当前状态。** 做完的事从「正在做」挪走，不留历史；流水账进 `sessions/`。

最后更新：2026-08-25（会话 04）

---

## 现在的位置

**M1 全部实机验证通过（用户 2026-08-25 确认），正在做 M2 开局链路。**

| # | 里程碑 | 状态 |
|---|---|---|
| M0 | 进度管理体系 | ✅ 完成（会话 01） |
| M1 | 房间内的 bot（`/bot` `/char` `/tm` `/del` `/ready` `/h`） | ✅ **完成**（实机验证通过） |
| M2 | 开局链路（bot 进图、会死会复活、进结算） | 🟡 代码 + 单测已完成，⏳ **等实机验证** |
| M3 | bot 会动会开枪（逆 `UdpPacket` body + 合成器） | 🟡 **逆向全部完成**（§23 / §24），`botsync.py` 还没写 |
| M4 | 地图地形数据（逆 `.map` + 工具 + 打包钩子）**可并行** | 🟡 **格式已全部逆出**（§17 / §18），工具还没写 |
| M5 | bot AI（寻路 / 追敌 / 瞄准 / 难度） | ⬜ |
| M6 | 测试 · Win7 兼容 · 文档 · 打包回归 | ⬜ |

详细内容见 `PLAN.md`。

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

★ M2 只让 bot **站着**进图、会死会复活会结算。**它还不会动、不会开枪** ——
那是 M3。实机看到 bot 杵在出生点一动不动是**预期**，不是 bug。

---

## ⏳ 待用户实机验证（M2）

★ **先重启服务端**（铁律 7：不重启就还是旧代码）。房主是你，`/bot` 加 1~2 个
bot，`/ready`，然后**按开始**。建议闯关房和对战房各来一局。

| 步骤 | 期望看到 |
|---|---|
| 按开始 | 加载条正常走完、**大家一起进图**（以前会卡在加载界面等 bot） |
| 进图后看 bot | 它站在出生点**不动**（预期，M3 才会动）；模型 / 昵称都在 |
| 打 bot 直到它血空 | bot **倒下、播死亡动画**（以前打不死） |
| 数 5 秒 | bot 自己**站起来**（和真人一个节奏） |
| 闯关房：走到地图最右边 | 全房间一起换图，**加载条不卡住**；新图里 bot 还在 |
| 闯关房：怪 | ★ **重点** —— 怪该刷的都刷、该动的都动，**没有一批怪从开局就杵着不动**（§5 / D14 就是修这个） |
| 打完一局 | 结算界面上 **bot 有自己那一行**（分数 / 生命那几格不是空的）；看完能回房间 |
| 回房间后再开一局 | 第二局照样开得起来（不卡加载） |
| 局中你退房（房里还有别的真人） | 房主转给**那个真人**，那一局继续 |
| 局中最后一个真人退房 | 房间在大厅列表里消失（`python tools\gs_ctl.py` 查不到） |

**怎么告诉我结果**：一条一条回「行 / 不行」。不行的那条请贴
`logs\server.out` 里对应时间的十几行 —— 尤其是带 `0x0414`、`0x0406`、
`0x0419`、`0x0418` 的那几行。

**当前状态**：等用户。

---

## M3 逆向已完成（会话 04）—— 剩下的是写 `botsync.py`

**✅ 做完的（§23 / §24，`re/packet_api.md` §5 已同步）**：

1. **一次扫描定位了全部 24 个内层 opcode 的组包点** —— `UdpPacket` 的构造函数是
   `0x5bbe1b`，内层 opcode 就是紧挨着它的 `push` 立即数。
2. **写原语认出来了**（`0x5d5901`=1 / `0x5d5910`=2 / `0x5d591f`=4 /
   `0x5d592e`=8 / `0x5d593d`=裸 N 字节）⇒ **body 布局 = 组包点后面那串
   `call 写原语` 的顺序**，一个都不用猜。
3. **`rpFire` / `rpExplode` / `rpJump` / `rpChangeWeapon` 字段语义全部对穿实包**
   （7040 / 7118 / 1982 发，座位号那一格 100% 一致、0 例外）。
4. ★★ **心跳 `0x4001` 的 31 字节全部解开**，并**推翻了 §3 的位置字段** ——
   真正的位置是 `+7..8` / `+9..10`，不是 `+25..28`。

**⬜ 还没做的**：

1. 新文件 `server/botsync.py` 合成 `UdpPacket`，**三条不变式写成断言**（D5）。
   校验和直接抄 `tools/fakeclient.py` 的 `udp_checksum()`（§4，25091/25091 实测全中）。
2. 投递走现有 `relayserver.deliver()`，**不新增第二条路**。
3. `rpAiMsg`(0x0011) 是变长的，组包点只写 8 字节、后面还有一发裸写 —— 要用时单独逆。

**开工前先读**：`FINDINGS.md` **§23 / §24**（不是 §3，那节的位置字段是错的）、
§4、`DECISIONS.md` D5、`re/packet_api.md` §5。

---

## 当前卡点 / 已知未知

| 事 | 状态 |
|---|---|
| `0x4001` 心跳 body 的完整布局 | ✅ **已逆出**（§24）。★ `+7..10` 才是位置，§3 那版是错的 |
| `rpFire`(26B) / `rpExplode`(28B) / `rpJump`(2B) / `rpChangeWeapon`(5B) | ✅ **已逆出且对穿实包**（§23） |
| `rpExplode` `+20` 那个位标志、`rpFire` `+1` 的武器槽 | ❓ 只知道取值范围，语义待用时再逆（§23） |
| `rpAiMsg`(0x0011) 的变长部分 | ❓ 组包点只写 8 字节，后面还有一发裸写没逆（§23） |
| `.map` 文件 `+14+L` 之后的布局 | ✅ **已逆出**（§17）：19 类循环 + type 表，174 张全解通。版本是 **7 种不是 2 种**（§9 已勘误） |
| 地形的碰撞几何在哪 | ✅ **不在 `.map` 里，在地形 PNG 的 alpha 通道**（§18）。M4 的做法已相应改掉 |
| `HidingObj`(201) 是不是真挡子弹 | ❓ 未查。名字也可能只是「挡视线的前景」，M4 要确认 |
| 版本 < 12 的 21 张图（v7/8/9）字段顺序 | ❓ 那 7 个 float 在**外层记录**里，要单独核对一遍 |
| bot 用什么武器、伤害怎么算 | ❓ 未查。伤害在**开火者那台**算，所以 bot 开火后真人本地算伤害 —— 大概率不用服务端管，M3 实机确认 |
| bot 在道具模式里要不要捡道具 | 🅿️ 暂不做（PLAN「明确不做的事」） |
| bot 的等级固定 `4`（`BOT_LEVEL`），显示上会不会突兀 | 🤔 只影响玩家列表那一格，实机看一眼再说 |

---

## 不要重做的事

- **客户端版本号 / 升级提示链** —— V0.2 已完成并实机验证，见 `FINDINGS.md` §12。
- **`UdpPacket` 校验和** —— 已逆出且本版重新实测 25091/25091 全中，
  直接抄 `tools/fakeclient.py` 的 `udp_checksum()`，见 `FINDINGS.md` §4。
- **§7 那个「只剩一人时同步被关掉」的对策** —— 已作废，见 `FINDINGS.md` §13。
