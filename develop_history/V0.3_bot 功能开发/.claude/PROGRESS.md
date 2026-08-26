# V0.3 PROGRESS —— 现在在哪

**只保留当前状态。** 做完的事从「正在做」挪走，不留历史；流水账进 `sessions/`。

最后更新：2026-08-26（会话 15）

---

## 现在的位置

**★★ M3b-2 第二轮修完，⏳ 等实机。** 用户 2026-08-26 实机确认
**「现在能换枪了」** ⇒ §45（大小写 bug）/ §47（弹道模型）/ D35（放宽 usable）
成立，三个槽位都能选了。同一轮报的两条新问题都修完了：

| 用户报的 | 查下来是什么 | 改法 |
|---|---|---|
| 1 号武器**没有 CD**，开枪太频繁，一会儿就把我秒死 | ★ **弹匣整段漏了**（§51）：会话 12 的 `fire_interval_of()` 只取 `CoolingTime`，没算「打空 `MagazineCount` 发要停 `ReloadTime`」。角色 1 的 1 号枪因此是原版的 **3.5 倍**输出（还是三连散弹）| 补完整个弹匣模型（**D36**）⇒ 18 把武器的持续 DPS 收敛到 **7.5~22** |
| **看不见弹体**，有开枪动画但没有子弹，凭空被打中 | ★★★ **根本不在弹道上，在收方的可靠队列**（§50）：事件包要等一发带更大 N 的心跳 `FlushTo(N)` 才执行。`rpFire` 和 `rpExplode` 挤在同一个 N 里 ⇒ 收方同一瞬间造出弹体又炸掉。近距离（跟在真人身后 120 单位、飞行 38 ms）**每发必中** | 爆炸多等一个**事件**：那一发 `rpFire` 被心跳报出去过（**D37**）⇒ 弹体至少活一整个心跳周期 |

★ §50 是这一轮**最值钱**的一条，而且是通用的：
**任何两发需要在画面上分开的事件包，中间必须夹一发心跳** ——
发包的时间差传不过去，传过去的只有 N。

★ 换弹匣**动画**查过了、做不了，见 §51 末尾（bit4 被语料否掉，没有网络字段）。

M4 已完成并通过实机核对。

| # | 里程碑 | 状态 |
|---|---|---|
| M0 | 进度管理体系 | ✅ 完成（会话 01） |
| M1 | 房间内的 bot（`/bot` `/char` `/tm` `/del` `/ready` `/h`） | ✅ **完成**（实机验证通过）。★ M3b 追加战斗中的 `/hold` `/gun`（D31）|
| M2 | 开局链路（bot 进图、会死会复活、进结算） | ✅ **完成**（死亡 / 复活 / 不换角色 / 闯关 3 条命 / **进度条**全部实机确认） |
| M3a | bot **会动会跳**（`botsync.py` 合成器 + 帧驱动） | ✅ **完成**：会动会跳、跳跃流畅、身体朝向、走路动画、**冲刺、蹲下**全部实机通过 |
| M3b | bot **会开枪** | 🟢 **打得中 + 换得了枪，都实机通过**（会话 12~15）。★ M3b-2 全部落盘：弹道模型（§47）、延后爆炸（D34）、三个槽位全可用（D35）、语料口径的射程（§48）、**弹匣节奏**（D36）、**弹体看得见**（D37）。⏳ 等实机 |
| M4 | 地图地形数据 | ✅ **完成**（会话 06，实机核对通过）：174/174 提取、加载器 + 测试 + 打包钩子；值 1 = 单向平台已查实（§29） |
| M5 | bot AI（寻路 / 追敌 / 瞄准 / 难度） | ⬜ **前置齐了**：M4 的地形数据 + M3b-2 的弹道模型（按距离选武器、提前量、难度旋钮现在都算得出来了）|
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
## ✅ M2 + M3a 实机结论（用户 2026-08-25 ~ 08-26，四轮）

**已经确认没问题的**：进游戏、跟着走跳且落点正确、可以打死、可以复活、
任务模式换房不卡墙、房主退出后正确转移 / 局中退出 / 最后一个真人退房结束、
闯关怪正常刷新、结算能看到 bot 收益、第二局照样能打。
★ 第二轮加上：**跳跃流畅不卡**、**复活不再换角色**、**闯关 3 条命死完就不再复活**。
★ **第三轮加上：进度条正常了（D26 ✅）、身体朝向正常了（§36 / §37 ✅，
`FACING_RIGHT = +1` 这个正负也一并确认没反）**。
★★ **第四轮加上：走路动画好了（§39 ✅ / D27 ✅）** —— M3a 的动作部分收口。

⇒ §5 / §6 / §26 / §32 / §33 / §34 / §35 / §36 / §37 / §38 / §39 /
D4 / D14 / D25 / D26 / D27 **全部被实机确认有效**。

★★ **走路动画那三轮的教训**：会话 07 / 08 各改了一版，两版都是**从字段值的
统计相关性倒推**，而**没有去读选动画那段代码**（会话 08 自己把这条写进了
卡点表）。会话 09 第一件事就是把 `0x507c50` 那个动画状态机读完，
一读就看见 `Stand%02d` / `Run-F%02d` / `Run-B%02d` 三个分支的判据 ——
开关是心跳里那个「六位掩码」，它是**方向键状态**（§39）。
**「症状在画面上」的 bug，先去读画那一帧的代码。**

**第四轮之后按「移动能力」逐项补的两条（会话 10 / 11）**：

| 用户的话 | 查下来是什么 | 改法 |
|---|---|---|
| 按右键加速跑时，**bot 脚下没有扬尘** | 拆成两件事：① **扬尘特效原版就只有自己看得见** —— `CH_Common/efx/FastRun00.efx` 全镜像只有 `0x515d29` 一处创建点，在**本地输入处理**里，远端角色（真人也一样）从来不播它；② 但**冲刺位 `bit3` 我们确实漏发了**，它管的是「收方把这个角色整帧 `dt` × `FastRunRate`」= 位移速度 + 腿的动画速率（语料实测 1.5 倍，**§40**） | `SyncTrailPoint` 加一格 `fast_run`，bot 回放时原样抄（同 D25 口径）；★ 只在**踩地且真的在走**时置起 |
| 按下键**蹲下**并加快体力恢复，实装了吗 | **没实装。** 而且它和前几格都不一样：蹲**在心跳里一个位都没有**，只有事件包 `rpCrouch`(0x000b) 说得着（body = 座位号 + 0/1，语料 394 发）。`[char+0x2b5]` 在收方管三件事：姿势 `Crouch*`、**移动速度 × 1/3**（`0x507607`）、**体力恢复 × 2**（`0x507250`），**§41** | `Conn.sync_crouch` 记状态 → 进轨迹点；bot **按状态翻转**补 `rpCrouch`（铁律 10），换图时两边一起清零 |

★ 用户同一轮的提醒（**M5 的需求，已记进 `PLAN.md`**）：这个游戏的朝向跟
**准星**走，「一边后退一边朝身后开枪」是合法姿势。现在 `aim_point()` 把准星
摆在 bot 正前方，所以它永远朝前走；M5 有目标之后把那个函数换成敌人坐标即可，
`Run-F` / `Run-B` 会自动跟着变。

---

## ✅ 会话 10 + 11 的实机复验（用户 2026-08-26 确认）

> 「我试了下，下蹲和加速跑都正常了」

⇒ **§40（冲刺位 bit3）和 §41（`rpCrouch` 状态包）两条全部实机确认**，
连带 D25（运动状态整套抄真人）/ D27（按键掩码自己算）的口径再次成立。
M3a 的动作部分**全部收口**，不再有待验项。

## ✅✅ M3b-1 实机通过（用户 2026-08-26）

> 「bot 可以打死我」

⇒ ★★★ **句柄预测是对的** —— §42 / §43 的整条链（`rpFire` + `rpExplode` 成对发、
弹体句柄 = `座位×100000+100002+已发弹数`、伤害取自 `weapon.ini` 的 `Damage`）
**全部被实机确认**。D28 / D29 成立。

这是整个 M3b 里唯一会**静默失败**的地方，过了就意味着后面只剩「好不好看」
和「聪不聪明」的问题。

**用户同一轮报的三条，会话 13 全部处理完**：

| 报的 | 查下来是什么 | 改法 |
|---|---|---|
| bot 跟得太近，**没法试隔墙** | 测试手段缺口 —— bot 走的是真人趟过的路（D16），中间**根本不可能有墙** | 新命令 **`/hold [N]`** 让 bot 站住（D31）|
| bot **只会用 1 武器，不会换** | 拆成两件事：① ★ **真 bug** —— bot 从来没发过 `rpChangeWeapon`，别人看到的枪和它打出来的子弹对不上；② 战斗中自动换枪是 **AI 决策** | ① 修了（按状态翻转发一发）；② 先给房主 **`/gun N M`**，自动换留 M5（D30）|
| 闯关房里 bot 不开火 ⇒ **换图没法测** | 原版只有闯关会换图，而闯关里 bot 一枪不开（怪的句柄服务端没有）| 用控制通道的 **`nextmap`** 在对战房里强制换图。★★ **那条命令本身是坏的**（只发给自己、不清记账），一并修成真路径（D32）|

★★ 顺带查实一条「**我以为知道其实不知道**」的事（**§44**）：
**原版根本没有「射程」这个字段**。会话 12 拿 `LockonRange` 当射程是错的 ——
那是**自动瞄准**的作用距离，而且**只有 1 号轻武器有**。`Velocity` 倒是每把都有，
但它的**尺度还没逆清楚**（弹速 100 会比人走路还慢），算不出飞行时间。
⇒ 当前射程是个**明确标注的近似**，M5 第一个该动的旋钮。

## ✅ 会话 13 那一轮的部分结论（用户 2026-08-26）

> 「hold 住以后，距离远了 bot 就不开枪了，站在身边才开枪。」
> 「刚才 bot 用 2 号角色，用 /gun 命令切换武器，只有 3 号武器能用，1 和 2 都不能用。」

⇒ **`/hold` 本身好使**（D31 ✅）、**`/gun` 的列表和换枪链路好使**（D30 ✅），
但**射程**（§44 的近似）和**可用武器表**（§45 的大小写 bug + 抛物线被排除）
两条不对，会话 14 已经修掉。

★ 那一轮里**还没验到**的三条挪进下面的新清单：隔墙、换到重武器打不打得掉血、
换图之后还打不打得中。

---

## ✅ 会话 14 那一轮的结论（用户 2026-08-26）

> 「现在能换枪了，但是 2 号角色，1 号武器没有 CD，开枪太频繁了，一会儿就把我秒死了。
>  这个游戏里所有武器都有 CD 才对，过程中有换弹匣动画。
>  所有武器现在看不见弹体，bot 有开枪动画，但是看不见子弹，
>  过一会儿我就凭空被打中了。」

⇒ ★★ **换枪那条链全通了**：§45 / §47 / D35 被实机确认，三个槽位都选得出来、
换得过去、打得出子弹（不然不会「被秒死」）。
两条新问题 = §51（弹匣）和 §50（可靠队列的 N），会话 15 都修完了。

★ **射程（§48 / D33）这一轮没单独验到** —— 用户是在近距离试换枪，
「被秒死」只说明近处打得中。挪进下面的新清单。

---

## ⏳ 待用户实机验证：M3b-2 第二轮（子弹 / 节奏 / 射程 / 隔墙 / 换图）

★ **先重启服务端**（铁律 7）。仍然要开**对战房**（个人战或组队战都行；
组队战记得 `/tm N` 把 bot 拨到对面）。战斗中敲 `/h` 能看到 `/hold` 和 `/gun`。

### 一、看得见子弹了吗（这一轮最要紧，验 §50 / D37）

| # | 步骤 | 期望看到 |
|---|---|---|
| 1 | ★★★ 就站在 bot 跟前让它用 **1 号枪**打你 | **看得见子弹飞过来**（哪怕很快），不再是「凭空掉血」|
| 2 | ★★ `/gun N 2` 换**抛物线武器**再站远一点 | **看得见一条弧飞过来**、落在身上炸 |
| 3 | 挨打的瞬间往旁边跑 | 爆炸出现在**你现在的位置**，不是刚才站的地方 |

★ 第 1 条近距离时子弹会**飞过你一点点**再炸（多飞不超过一发心跳的距离）——
那是 D37 的已知代价，**不算 bug**。

### 二、开火节奏（验 §51 / D36）

| # | 步骤 | 期望看到 |
|---|---|---|
| 4 | ★★ 2 号角色 + 1 号武器，站着挨打 | **打两发停一下**（换弹匣 1.2 秒），不再是突突突；掉血速度大概是原来的 **1/3.5** |
| 5 | `/gun N 3` 换 3 号武器 | 节奏更慢（那一把是打一发装一次）|
| 6 | 从满血挨到死 | 大概 **8 秒以上**（原来 2 秒出头）|

⚠ **换弹匣动画看不到是正常的** —— 查过了，原版那个动画是本地播的，
网络上没有任何一发包说得着（§51 末尾）。别拿它当 bug。

### 三、射程（会话 14 那一轮没验到，验 §48 / D33）

| # | 步骤 | 期望看到 |
|---|---|---|
| 7 | ★★ 走到 bot 跟前敲 **`/hold`**，然后**走开半个屏幕** | bot 照样朝你开枪、你照样掉血 |
| 8 | 继续往远走，走到大半张图之外 | bot 停火（上限 1000 个单位 ≈ 大半个屏幕宽）|

### 四、隔墙（验 `line_blocked`，§29 —— 一直没验到）

| # | 步骤 | 期望看到 |
|---|---|---|
| 9 | `/hold` 住 bot，你绕到一堵**实心墙 / 地形**后面 | bot **停火** |
| 10 | 从墙后探出来 | bot **重新开火** |
| 11 | 站到一根**白线**（单向平台）后面 | bot **照打不误** —— 白线挡人**不挡子弹**（§29 逆出来的，没实机验过）|

### 五、换图（验句柄记账跟着清零，D28 硬约束 2 —— 一直没验到）

原版只有闯关会换图，所以走**调试控制通道**（服务端跑在本机才有）：

```bash
python tools\gs_ctl.py "nextmap 地图名"
```

| # | 步骤 | 期望看到 |
|---|---|---|
| 12 | 在对战房里打一会儿，然后跑上面那条命令 | 全房间一起换图、正常加载进新图 |
| 13 | ★★★ 换图之后再让 bot 打你 | **照样掉血** |

★ 地图名不知道写什么就先 `python tools\gs_ctl.py rooms` 看当前这张图叫什么，
填同一个名字也行（等于原地重载）。控制通道只绑 `127.0.0.1`。

**怎么告诉我结果**：第 1 / 4 / 7 / 9 / 11 / 13 条各回一句就够。
不对的话贴 `logs\server.out` 里带 **`开火:`** 的那一行（本图第一发才打，好找）
—— 那一行现在把**弹道**（角度 / 力度 / 飞行 tick 数）和**弹匣节奏**都打出来了。

**当前状态**：等用户。

---


## ✅ 上一轮的验证清单（已完成，留作参考）：M3b-1「bot 打得中吗」

★ **先重启服务端**（铁律 7）。

★★ **必须开「对战」房，不能开闯关房** —— 闯关里大家是队友，bot 一枪都不会开
（怪的句柄服务端手里没有，那是 M5 的事）。个人战和组队战都行；组队战记得
用 `/tm N` 把 bot 拨到**对面**那一队。

房主是你，`/bot` 加 1 个 bot，`/ready`，开始。走到 bot 附近让它够得着你
（射程 = 武器的 `LockonRange`，基础枪是 80~120 个单位，大概一屏的三分之一）。

| # | 步骤 | 期望看到 |
|---|---|---|
| 1 | ★★★ **走到 bot 跟前站着别动** | bot 朝你开枪，**你的血在掉** |
| 2 | ★★ 血掉到 0 | 你正常死亡 / 进重生流程（和被真人打死一样）|
| 3 | ★ 躲到墙 / 实心地形后面 | bot **停火**（打不着就不打）|
| 4 | ★ 站到一根**白线**（单向平台）后面 | bot **照打不误** —— 白线挡人不挡子弹（§29）|
| 5 | ★★ **换图 / 打完一局再开一局**，再走到 bot 跟前 | 新一局照样打得掉血（★ 这一条验的是句柄有没有跟着清零）|
| 6 | 身体朝向 | bot **面朝你**开枪；你绕到它背后，它转过来 |
| 7 | 走 / 跳 / 蹲 / 冲刺 | 照旧正常（M3a 回归看一眼）|

**★ 最要紧的判据只有一个：你的血掉不掉。**
子弹飞过去**不炸、不掉血** = 句柄预测错了（`0x492750` 那个静默丢弃，§42）。

**怎么告诉我结果**：第 1 条回一句「掉血 / 不掉血」就够。**不掉血的话**请贴
`logs\server.out` 里带 **`开火:`** 的那一行（只有本图第一发会打，好找），
外加前后十几行。

★ 已知的、**不算 bug** 的几件事（M3b-2 / M5 再管）：

- 子弹几乎**一出膛就炸**（`rpExplode` 紧跟着 `rpFire` 发，还没按飞行时间延后）；
- bot **不会主动靠近你**，它只跟着你走（D16 的轨迹回放），所以得你走过去；
- bot 打得很准、也不换弹匣（难度旋钮是 M5）；
- 角色 3 那类没有可用武器的角色，bot **只跑不打**（日志里不会有 `开火:` 那行）。

**当前状态**：等用户。

---

## ✅ M4 核对结论（用户 2026-08-26 确认「像」）

导出的碰撞位图和游戏里目视一致 ⇒ **坐标系、位序、y 轴方向全部读对了**。

★★ 用户同时给了一条关键信息，直接定死了 §27 里那个 ❓：

> 「原游戏里白线是可以站人的线，站在上面按下键可以穿过白线处掉到下面去。」

⇒ **值 1 = 单向平台**，不是「薄的可站立面」那种描述性的东西。
顺着这条把弹体的地形碰撞逆完了（**§29**）：**单向平台挡人、不挡子弹**。
`server/mapdata.py` 因此拆成两个判据 —— `is_solid()`（挡人）和
`blocks_bullet()`（挡子弹）。★ 原来 `line_blocked()` 用的是前者，**是错的**，已改。

★ 想看别的图：`tools\update-mapdata.bat --verify 地图名`（可以给多个名字）。
可视化在 `logs/mapdata-preview/`：绿=实心、红细线=单向平台、
白线=站立面、蓝/红/黄十字=蓝方出生点/红方出生点/重生点。

---

## M3a 做完了什么（会话 05，代码已落盘）

| 文件 | 改动 |
|---|---|
| `server/botsync.py` | **新增**。校验和 / `UdpPacket` 头 / 心跳 body（31）/ `rpFire`(26) / `rpExplode`(28) / `rpJump`(2) / `rpChangeWeapon`(5) + `BotSyncStream`（序号记账 + D5 三条不变式，违反当场 `SyncInvariantError`） |
| `server/bot.py` | ★ `BotConn.send()` 改成「只跑 `note_epoch_from_frame`」（§26）；`BotConn.sync` / `battle_pos` / `heading` / `last_trail_mark` / `load_progress` / `reset_battle_frame()`；`_align_epoch()`；**帧驱动** `tick_room()` / `_tick_bot()` / `trail_point()` / `_lying_dead()`；进度条 `report_bots_loaded()`；把两个钩子挂到 `gameserver.BOT_ROOM_TICK` / `BOT_ROOM_LOADED` |
| `server/gameserver.py` | `Conn.sync_trail` / `sync_jumped` / `sync_trail_seq` / `sync_load_progress` + 类级默认；`note_sync_position()`（在 `forward_peer_data` 里记位置和起跳）；`reset_sync_trails()`（换图 / 新一局各清一次）；`Conn.is_bot_conn()`；`_relay_battle_tick` 对 bot 直接 return + 调 `BOT_ROOM_TICK`；常量 `SYNC_TRAIL_POINTS` / `PEER_OP_JUMP` / `PEER_OP_LOAD_PROGRESS` / `BOT_ROOM_TICK` |
| `server/udpsync.py` | `heartbeat_position()` + `PEER_HEARTBEAT_STATE_OFFSET` |
| `server/test_botsync.py` | **新增** 79 个用例（线格式 + 不变式 + 轨迹回放 + 战斗帧 + 走路动画 + 加载进度） |
| `server/test_room.py` / `test_battle.py` | 两个 `make_conn` 夹具补 `sync_trail` / `sync_jumped` |
| `server/run_tests.py` / `tools/build-common.ps1` | 挂上 `test_botsync` / 必选文件加 `botsync.py` |

**bot 现在怎么动**（★ 会话 08 修订）：判据是「**它跟的那个真人报了一个新位置**」
（`Conn.sync_trail_seq` 变了，§32）—— 转发路上跑的不只有心跳，靠这个事实分流，
节奏和真人逐发对齐。落脚点 = 那个真人的轨迹上**往回退 120 的那一点**
（第 N 个 bot 退 N×120），**在两个采样点之间插值**，所以 bot 每帧走的距离
恒等于真人这一帧走的距离。那一段里真人跳过的话先补一发 `rpJump` 再发心跳。

★★ **心跳里的运动状态整套抄真人的**（§35 / D25）：轨迹点除了坐标还带
`(on_ground, vx, vy)`，bot 走到哪一段就抄哪一段 —— 踩地时速度**必须是 0**，
腾空时才填那一段真实的抛体速度。**自己反推速度就是「一跳一跳」的抽搐。**
★★★ **方向键掩码（`+23..24`）是走路动画的开关**（§39）：踩地时按
**bot 自己这一帧线上的位移**置起 bit0（左）/ bit2（右），站住就清空
（★ **不抄真人的**，理由见 **D27**）。填 0 = 对方屏幕上一个站着不动的人
被心跳一格一格地拖过去。
★ 准星 / 朝向位 / 角度由 `aim_point()` + `aim_state()` 一起算（§36 / §37），
三个字段永远自洽，身体朝向因此跟着走的方向转。
**位置回放真人走过的点**是因为服务端一点地图几何都没有（D16）。

★★ **冲刺位（位域 bit3）也抄真人这一段的**（§40）：真人按右键快跑时坐标是
1.5 倍步长的，不跟着报这一位收方就只按普通走速替它挪 —— 跟不上 + 拉扯。
只在**踩地且真的在走**时置起。

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1244 全绿**；
`runtime-win7\python\python.exe server\run_tests.py`（CPython 3.8）→ **同样 1244 全绿**。

★ 顺带把 §24 的几格语义改对了（`packet_api.md` §5.5 / §5.6 已同步）：
`+17..18` 是**角度（度）**不是速度、`0x5f895c` 是**朝零截断**不是四舍五入；
头 `+3` 从 ❓ 变成「恒 0」（91526 发）；会话 08 又改了三处 ——
`+11..14` 是**离地速度**不是走路速度、`bit2` 是**在地面**不是静止、
`+25..28` 是**世界坐标**不是屏幕坐标；★ 会话 09 再加一处 ——
`+23..24` 是**方向键状态**（而且发侧取的是每格的 bit0，不是「非 0」）。

`rpAiMsg`(0x0011) 变长那一段仍未逆（组包点只写 8 字节，后面还有一发裸写）。

---

## M3b-1 做完了什么（会话 12，代码已落盘，⏳ 等实机）

**逆向**：`rpFire` / `rpExplode` 的收侧全逆完（**§42**）+ 语料实证（**§43**）。
结论和决定见上面那张表 / **D28** / **D29**。

| 文件 | 改动 |
|---|---|
| `tools/weapondata.py` | **新增**。解 `Pack_decrypt/Data/weapon.ini`（UTF-16LE 的 INI）→ `server/bot_weapons.json`。★ 派生两个关键字段：`handle_step`（每发吃几个句柄，**不确定就返回 `None`**）和 `fire_interval_ms`（`CoolingTime`，没有就退 `ReloadTime`）。只用标准库 |
| `tools/update-weapondata.bat` | **新增**。一键重跑（提取 + 立刻跑一遍测试）。CRLF + UTF-8 无 BOM |
| `server/weapondata.py` | **新增**。运行时加载器：`get()` / `preferred_for()` / `usable()`。**没有产物也不让服务端起不来** —— bot 照样跑跳，只是不开枪。只用标准库，3.8 可跑 |
| `server/bot_weapons.json` | **新增产物**（228 把武器，75 KB，进 git、进两个包）|
| `server/botsync.py` | 句柄换算 `character_handle()` / `projectile_handle()` / `handle_owner()`（逐指令抄 `0x473e65`）；`BotSyncStream.projectiles` + `fire()`（★ 组包和记账**一次加锁**做完）+ `reset_projectiles()`；`explode_body` 的 `radius` 改名 **`damage`**（§42 查实）；`HIT_*` / `FIRE_POWER_FIXED` |
| `server/bot.py` | `BotConn.weapon`（property，跟着 `/char` 走）/ `next_fire_at` / `fire_logged`；`reset_battle_frame()` 多清两样；`_current_map()` / `_hostile_humans()` / `_fire_target()` / `_try_fire()`；`_tick_bot()` 里接上开火，**准星改指向目标**（§37 / §39 自动跟着变朝向和 `Run-F`/`Run-B`）|
| `server/test_botsync.py` | 新增 `HandleTests` / `FireBookkeepingTests` / `BotFireTests` / `BotCoopNoFireTests` / `BotTeamFireTests` / `BotFireHandleResetTests` |
| `server/test_weapondata.py` | **新增** 14 个用例（合成表 + `handle_step` 判据 + 真产物） |
| `server/run_tests.py` / `tools/build-common.ps1` / `build-portable.ps1` / `build-server-package.ps1` | 挂上 `test_weapondata`；必选文件加 `weapondata.py`；新增 `Copy-WeaponData` / `Update-WeaponData`（照 D21 的口径） |

★ **实机排查用**：本图第一次开火会打一行 `开火: 武器 … 本图第一发的弹体句柄 …`
（按状态翻转去重）。句柄错位是整条链上**唯一**会静默失败的地方。

## 会话 13 又做了什么（代码已落盘）

| 文件 | 改动 |
|---|---|
| `server/bot.py` | ★ `_declare_weapon()` —— 头一次开火 / 每次换枪之前发一发 `rpChangeWeapon`（**修 bug**，D30）；`BotConn.declared_weapon` / `weapon_slot` / `holding`；`weapon` property 认 `/gun` 指定的槽位、那一把不可用就退回首选；`_tick_bot` 加 holding 分支（**照常发心跳**，只是坐标不动）；★ 射程口径改对 + 注释写清它是近似（§44）；新命令 `_cmd_hold` / `_cmd_gun` / `_battle_bots`；`/h` 分成房间版和战斗版两套（D31）|
| `server/weapondata.py` | `usable_for()` / `slot_for()`（按角色 / 按槽位查）|
| `server/gameserver.py` | ★★ 控制命令 **`nextmap` 修成走真路径**（原来只发给自己、不清记账，是个陷阱，D32）+ 帮助文本改对 |
| `server/test_botsync.py` | 新增 `BotWeaponDeclarationTests` / `BotGunCommandTests` / `BotHoldCommandTests`，`BotFireHandleResetTests` 加 `nextmap` 那条 |
| `server/test_bot.py` | `/h` 那条用例拆成房间版 + 战斗版 |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1302 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样 1302 全绿**。

---

## M3b-2 做完了什么（会话 14，代码已落盘，⏳ 等实机）

**逆向**：弹道模型（**§47**）、`rpFire` 的 `count`（**§46**）、
`Acceleration`（**§49**）、交战距离（**§48**）、大小写 bug（**§45**）。

| 文件 | 改动 |
|---|---|
| `server/ballistics.py` | **新增**。tick / 重力 / 三种初速模式的常量（全部带出处）；`solve()`（直射一条线、抛物线解发射角、加速弹逐 tick 反解）、`position_at()`、`path_points()`（抛物线切段给地形判定）、`power_for_speed()` / `speed_for_power()`。只用标准库，3.8 可跑 |
| `tools/weapondata.py` | ★ `_SECTION` 加 `re.IGNORECASE`（§45）；新增 `shots_of()`；`handle_step_of()` 改成 `shots × (2 if 溅射 else 1)`（§46）；`_is_usable()` 放行抛物线 + 散射（D35）；`_preference()` 改成按槽位；`_FIELDS` 补 `Acceleration` / `HomingRange`；`FORMAT` → 2 |
| `server/bot_weapons.json` | 重新生成（47 把可用，**14 个玩家角色三个槽全齐**）|
| `server/weapondata.py` | `FORMAT` → 2；新增 `shots` / `max_velocity` / `power_control` / `acceleration` / `splash_range` 属性 |
| `server/botsync.py` | `fire()` 接 `shots` 参数、校验 `handle_step >= shots`、`count` 写进包；新常量 `FIRE_SHOTS_MAX` |
| `server/bot.py` | ★ `BOT_ENGAGE_RANGE = 1000`（取代 `BOT_FIRE_RANGE_FALLBACK`，§48）；`BotConn.pending_shots`（在飞的子弹）；`_path_blocked()`（抛物线分段查遮挡）；`_fire_target()` 改成「解得出弹道才算够得着」；`_try_fire()` 排队而不是立刻炸；`_flush_explosions()` / `_impact_point()` / `_may_fire()`；`_tick_bot()` **最前面**先冲一遍在飞的子弹；`/gun` 列表标「直 / 抛」|
| `server/test_ballistics.py` | **新增** 23 个用例（尺度常量 / 三种模式 / 直射 / 抛物线 / ★ **闭式解 vs 逐 tick 递推对拍** / 真产物全表跑一遍）|
| `server/test_botsync.py` | 新增 `BotBallisticFireTests`（11 个）+ 夹具的 `settle()`；`FireBookkeepingTests` 补散射 / 上限 |
| `server/test_weapondata.py` | 大小写回归钉子、「每个角色三个槽」、`shots` / 步进新口径 |
| `server/run_tests.py` / `tools/build-common.ps1` | 挂上 `test_ballistics`；必选文件加 `ballistics.py` |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1343 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样全绿**。

---

## 会话 15 又做了什么（代码已落盘，⏳ 等实机）

| 文件 | 改动 |
|---|---|
| `server/bot.py` | ★ `_reload_after_shot()` —— 完整的弹匣模型（D36）；`BotConn.rounds_left`（换枪 / 换图跟着清）；★★ `_explosion_ready()` —— 爆炸多一个闸门「那一发 `rpFire` 被心跳报出去过」（D37 / §50）；`pending_shots` 每条多带一个 `fire_seq`；`_flush_explosions()` 顺手丢掉换代残留；开火日志加上弹匣节奏 |
| `server/botsync.py` | `BotSyncStream.announced` = 最近一发心跳报出去的 N（换代时跟着清 0）|
| `server/weapondata.py` | 新增 `magazine` / `cooling_ms` / `reload_ms` 属性；`fire_interval_ms` 的 docstring 标明「有弹匣的不能只看它」|
| `server/test_botsync.py` | 新增 `BotMagazineTests`（5 个）+ `BotVisibleBulletTests`（2 个）；夹具拆出 `arrive()`。★ 两个可见性用例都**验证过「把闸门去掉就会红」** |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1350 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样全绿**。

---

## M4 做完了什么（会话 06，代码已落盘）

★★ **做法和原计划完全不同**：`.map` 的**尾部**烘着一份逐像素碰撞位图
`TerrainData`（§27）—— 和地图同尺寸、每像素 2 bit、RLE 压缩，
**客户端自己的碰撞查询读的就是它**。所以不用去合成地形 PNG 的 alpha（D19），
连 Pillow 都不是硬依赖。

| 文件 | 改动 |
|---|---|
| `tools/mapdata.py` | **新增**。解 `.map`（§17 + §28 补完）→ `TerrainData` → `server/bot_mapdata/`。只用标准库；`--verify` 才用 Pillow 画预览 |
| `tools/update-mapdata.bat` | **新增**。一键重跑：提取 + 立刻跑一遍测试。CRLF + UTF-8 无 BOM |
| `server/mapdata.py` | **新增**。运行时加载器：`cell` / `is_solid`（挡人）/ `blocks_bullet`（挡子弹）/ `is_one_way` / `surfaces` / `ground_below` / `ground_above` / `line_blocked` + 名字解析（`A:NewPvp`、`Quest02_2` → `#Normal`）。**只用标准库**，3.8 可跑 |
| `server/test_mapdata.py` | **新增** 25 个用例（合成小图 19 + 真产物 6） |
| `server/run_tests.py` | 挂上 `test_mapdata` |
| `tools/build-common.ps1` | 必选文件加 `mapdata.py`；新增 `Copy-MapData`（产物缺失或 <150 个就中止打包）和 `Update-MapData`（有素材必须重跑成功、没素材用仓库产物）—— D21 |
| `tools/build-portable.ps1` / `build-server-package.ps1` | `Copy-ServerCode` 之前调一次 `Update-MapData`（一次构建只跑一次） |

**产物**：`server/bot_mapdata/` = `index.json` + 174 个 `<地图名>.json`，
合计 **2.4 MB**，**进 git、进两个发布包**。

★★ **目录约定（D22，用户拍板）**：`server/data/` **只装用户数据**
（`accounts.json` / `tickets.json` —— 运行时生成、每台机器不同、`.gitignore` 掉，
包里只带一个空目录）。**产物不许往里塞**，放 `server/bot_mapdata/`。
（`server/data/bot_maps/` 那个空残留目录已清掉。）

每张图里：

- `cells` —— 原样的 2bit/像素位图（zlib + base64）。**出界返回 2**，照抄客户端。
- `ground_counts` / `ground_ys` —— 每一列的**站立面** y（实心区的上沿）。
- `points` —— 出生点(101/102) / 重生点(108) / 刷怪区(103) 等玩法坐标。

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1195 全绿**（原 1170 + 25）；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样 1195 全绿**。
打包函数也单独验过：拷进去 175 个 json、0 个 png、缺产物时正确抛错。

★ **M5 的前置齐了**：地形能查、站立面能查、弹道遮挡能查、出生点有坐标。

---

## 当前卡点 / 已知未知

| 事 | 状态 |
|---|---|
| `0x4001` 心跳 body 的完整布局 | ✅ **收发两侧逐字段都逆完了**（§24 布局 + §25 语义 / 收侧行为）。★ `+7..10` 才是位置 |
| `rpFire`(26B) / `rpExplode`(28B) / `rpJump`(2B) / `rpChangeWeapon`(5B) | ✅ **已逆出且对穿实包**（§23），组包已实现（`botsync.py`） |
| 位域低 2 位（`[char+0x2d0]`）是不是「朝向」 | ✅ **是**，`+1` 朝右 / `−1` 朝左，而且跟的是**准星在哪一侧**（97.8% / 73.3%，§37）。★ 正负万一反了，实机看一眼、对调两个常量即可 |
| ★ **`rpFire` 的收侧：伤害在哪一台算 / 不发 `rpExplode` 会怎样** | ✅ **全查清了**（§42）：**射手那台算**；不发 `rpExplode` 的话子弹一直飞、一滴血不掉。⇒ 服务端整个当射手（D28） |
| ★★ **弹体句柄能不能预测** | ✅ **能**：`座位×100000 + 100002 + 本图已发弹数`，语料 14/14 文件实证（§43）。**每个 owner 一格计数器**，别人打多少枪都不影响 bot 那一格 |
| ★ 句柄计数器什么时候清零 | ✅ **开局 + 换图**（`ForceReloadTerrain`），语料实测（§43 第 4 条）。★ `gameserver.begin_map_change()` 里那条讲 `max` 合并的注释容易读成「跨图累计」，别被带偏 |
| `rpExplode` `+24` | ✅ **伤害值**（§42，原来标的是「像伤害或半径」）。语料 3.0~74.0 全整数，和 `weapon.ini` 的 `Damage` 对得上 |
| `rpExplode` `+20` 那个位标志、`rpFire` `+1` 的武器槽 | ❓ 只知道取值范围，语义待用时再逆（§23）。bot 填 `flags=0` / `slot=1`（语料里最常见），实机没见到问题 |
| ★ **`SpreadFrags > 1` 的武器每发吃几个句柄** | ✅ **`SpreadFrags` 个**（§46）：收侧内层循环每颗注册一次；语料实测 `1001010` 4 发 → 11 个连号句柄。`rpFire +22` 也**必须**填 `SpreadFrags`（填 1 一颗都造不出来），语料 65/65 |
| bot 的弹道 | ✅ **算了**（`server/ballistics.py`，§47 / D34）：直射一条线、抛物线解发射角、加速弹逐 tick 反解；`rpExplode` 按飞行时间延后发。⏳ 等实机 |
| ★★ `weapon.ini` 的 `Velocity` 是什么尺度 | ✅ **「世界单位 / tick」，tick = 32 ms**（§47）—— `Velocity=100` 就是 3125 单位/秒。逐指令（`0x4920a1` / `0x47f603`）+ 语料回归（8 个不同 `Velocity`）双证。M3b-2 和 M5 的前置就此解除 |
| ★ bot 的「射程」 | ✅ **改成语料量出来的交战距离 1000**（§48 / D33）：247 发真人命中的 p99。真正的「够不够得着」判据是**弹道解得出来吗**。⏳ 等实机 |
| `rpAiMsg`(0x0011) 的变长部分 | ❓ 组包点只写 8 字节，后面还有一发裸写没逆（§23） |
| ~~`VELOCITY_PER_STEP`＝4.111~~ | 🅿️ **不用了，已删**（会话 08）：速度两格根本不是走路速度，是**离地时的抛体速度**，bot 直接抄真人那一段的（§35） |
| 心跳 `+15`（`[char+0x594]`）是什么 | ❓ 语料里 88% 是 0，剩下散在 34~80。**不是**走路 / 站立的区分位（会话 07 对穿过：移动和静止两边分布一样）。bot 填 0 |
| ★ 走路动画到底由哪个量驱动 | ✅ **查明并实机确认：`+23..24` 的方向键掩码**（§39，动画状态机 `0x507c50` / 选择点 `0x507fb5` 已逐指令读完） |
| 掩码的 `bit1`（↑）/ `bit3`（↓）/ `bit4` / `bit5` | 🤔 bit1/bit3 是上下键（收方拿它们设空中速度），语料里几乎只出现在腾空段；bit4/bit5 **一次都没出现过**。bot 全填 0 |
| 位域 `bit3`（`[char+0x4bc]`）| ✅ **冲刺**（§40：`dt × FastRunRate`，实测 1.5 倍）。bot 跟着真人报，**已实机确认** |
| 位域 `bit4`（`[char+0x2dc]`）/ `bit5`（`[char+0x59c]`）| ❓ bit5 几乎不出现、bit4 一半一半看不出规律。bot 全填 0，实机没见到问题 |
| 蹲下 `[char+0x2b5]` | ✅ **查明并实装**（§41：事件包 `rpCrouch`(0x000b)，收方姿势 + 速度 ×1/3 + 体力恢复 ×2）。**已实机确认** |
| 走路速度 `vf+0x128` 的来源（哪张属性表）| ❓ 没跟。目前不影响：冲刺（§40）和蹲走（§41）两档倍率都是收方**自己**乘的，bot 只要把状态位报对就行 |
| ★ 扬尘特效为什么远端看不到 | ✅ **原版就这样**（§40：唯一创建点在本地输入处理里）。🔍 逐指令 + 唯一 xref，**没有双人实机复核** |
| 加载进度 `0x4005` 是不是也在**换图**时发 | 🤔 语料里的 `0x4005` 全出现在开局那一段（`0x0402` 之后、`0x0403` 之前），**没有换图的样本**。不影响 bot：D26 之后它在开局和换图两处**都**报满，不看真人发不发 |
| `.map` 文件 `+14+L` 之后的布局 | ✅ **全部逆完**（§17 + §28 补上最后两处）：174 张逐字段读到文件尾，一个字节不剩 |
| 地形的碰撞几何在哪 | ✅ **在 `.map` 尾部的 `TerrainData`**（§27）：逐像素 2 bit，客户端自己就读它。M4 直接搬（D19），不合成 PNG alpha |
| `HidingObj`(201) 是不是真挡子弹 | 🅿️ **不用查了**：碰撞位图是烘焙好的，挡不挡已经体现在格值里 |
| 版本 < 12 的 21 张图（v7/8/9）字段顺序 | ✅ **已核对**（§28）：7 个 float 确实在外层记录里，顺序和 v≥12 的 blob 一样；type 靠贴图路径判 |
| ★ 碰撞位图里的值 **1** 到底是什么 | ✅ **单向平台**（§29）：挡人不挡子弹。用户实机确认 + 弹体碰撞逆向双证。服务端已分成 `is_solid()` / `blocks_bullet()` 两个判据 |
| bot 用什么武器 | ✅ 默认是**自己角色的 1 号基础枪**（`weapondata.preferred_for()`，D35）。会话 14 之后 **14 个玩家角色三个槽位全可用**（抛物线 + 散射都放行了）；只有角色 3 的 3 号槽（伤害 0）不可用，而它本来就不在玩家可选范围。★ 房主用 **`/gun N M`** 改 |
| ★ bot **战斗中自动换武器** | ⏸ **留在 M5**（D30）。★ 前置现在齐了：`ballistics.solve()` 能告诉你「这把枪够不够得着、飞多久」，按距离选武器不用再硬编阈值 |
| ★ **句柄步进 2**（带溅射的重武器）| 🟡 语料量到了（`1002030` 207 发 → 跨度 427，§46），**实机仍没跑过**。溅射那一格在开火时还是爆炸时分配语料分不出来 ⇒ 加了顺序闸门 `_may_fire()`（D34）让两种假设同解 |
| ★ 抛物线武器 bot 一律**用满蓄力** | 🤔 弹道因此很平（打 600 单位才抬 5°），落点准、飞得快，但看着不太像「扔手雷」。`ballistics.solve()` 接受指定初速，想要高抛只要传小一点的 `speed` —— **难度 / 观感旋钮，留给 M5** |
| ★ 追踪弹（`HomingRange`，4 把）的飞行时间 | 🟡 **近似**：弹道会拐弯，服务端没建模（§49）。伤害不受影响（爆炸点和目标句柄都是服务端写死的），只是爆炸时刻和客户端画的弹体位置会差一点 |
| ★ **换弹匣动画**远端看得到吗 | 🅿️ **看不到，别再查**（§51 末尾）：动画门是 `[char+0x2dc]`（= 心跳位域 bit4），但语料里 bit4 一共只翻转 5 次、和「打空弹匣」对不上；内层 opcode 表里也没有换弹包。多半和扬尘特效一样是本地播的（§40）|
| ★ 近距离弹体会**飞过目标一点点**再爆 | 🟡 D37 的已知代价：爆炸至少要排到下一个 N（否则根本看不见子弹）。多飞的距离 ≤ 一发心跳。远距离不受影响 |
| bot 在道具模式里要不要捡道具 | 🅿️ 暂不做（PLAN「明确不做的事」） |
| ★ `server/bot_mapdata/` 的 175 个 json 是 **CRLF**（违反铁律 3）| 🟡 **工具已修好、产物还没重跑**。`tools/mapdata.py` 写文件时少了 `newline="\n"`，Windows 的文本模式把 `\n` 转成了 `\r\n`（M4 就有，`index.json` 1280 个 CR）。功能上无害（JSON 允许 `\r\n` 当空白），但规矩上不对。**跑一次 `tools\update-mapdata.bat` 就全好**，代价是 git 里会出现 175 个「只改了行尾」的文件 —— 什么时候跑由用户定 |
| bot 的等级固定 `4`（`BOT_LEVEL`），显示上会不会突兀 | 🤔 只影响玩家列表那一格，实机看一眼再说 |

---

## 不要重做的事

- **客户端版本号 / 升级提示链** —— V0.2 已完成并实机验证，见 `FINDINGS.md` §12。
- **`UdpPacket` 校验和** —— 已逆出且本版重新实测 25091/25091 全中，
  直接抄 `tools/fakeclient.py` 的 `udp_checksum()`，见 `FINDINGS.md` §4。
- **§7 那个「只剩一人时同步被关掉」的对策** —— 已作废，见 `FINDINGS.md` §13。
