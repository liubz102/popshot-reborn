# V0.3 PROGRESS —— 现在在哪

**只保留当前状态。** 做完的事从「正在做」挪走，不留历史；流水账进 `sessions/`。

最后更新：2026-08-27（会话 22）

---

## 现在的位置

### ⏳ **等用户实机验收**（会话 19 ~ 22 攒在一起）

### ★★ 会话 22：用户报的四条 + 一条改主意，全做了

| 用户说的 | 真因 | 改在哪 |
|---|---|---|
| 「2 号角色 2 号武器扔在地上会持续燃烧，现在 bot 的没有燃烧」 | 铺火墙那一发 `rpSetOnFire` 是**射手那台机器**在 `IsMine` 门里发的，bot 没有本机 ⇒ 没人替它铺火 | **§75**。补发 `rpSetOnFire`，并跟着推进 **`2×SpawnCount+1`** 个弹体句柄 |
| 「手雷需要长按鼠标蓄力……现在 bot 仿佛不需要蓄力，直接就扔出去了」 | 服务端根本没有蓄力这回事 | **§73 / D55**。蓄力计数器每 tick `+2`、封顶 80、松手夹到 `[15,80]`，bot 现在老老实实按住；心跳 `+15` 跟着报真实蓄力值 |
| 「『预备』『开始』还没显示完 bot 就开枪了」 | — | **§74 / D55**。`Character::Respawn` 挂的那道 **2000 ms** 锁 |
| 「复活后两三秒不能开枪，bot 复活一瞬间就开枪」 | 同上，**是同一道锁**（`Respawn()` 进图和复活都跑） | 同上 |
| ★ 「我改变主意了，bot 可以选择的只有初期的 3 个角色，商城角色不可以」 | — | **D54**。`/char N M` 的 M 从 1~14 收成 **1~3** |

★ **顺带修了一个没暴露过的错**：`ballistics.max_speed()` 对 `PowerControl=2`
用的是 `MaxVelocity`，而蓄力封顶在 80 ⇒ 真正的上限是 `Velocity × 3.8`。
`ch00-02` 写着 60、实际只有 38 —— 服务端一直以为这两把枪比原版远 1.6 倍（§73）。

**三个基础角色的 9 把武器现在全部可用**：

```text
角色 0（面板 1）  左轮手枪   / 苹果雷（蓄力抛）   / T1 狙击枪
角色 1（面板 2）  双管散弹   / 火焰弹（蓄力抛+火墙）/ 追踪火箭
角色 2（面板 3）  机枪       / 螺旋榴弹（点击即发）/ 火箭筒
```

### ★★ 会话 21：用户报的两个 bug，两条都属实，都改了

| 用户说的 | 真因 | 改在哪 |
|---|---|---|
| 「用 gun 命令，很多角色都无法切换 2 号武器」 | ★★ **D49 / §70 的前提是错的**。它拿语料残差判「分裂弹 / 火墙在收方多吃句柄」，但那份残差量的是**射手自己那台机器** —— 逐指令读下来，那些额外对象的创建点**全部**套在 `IsMine`（`0x50d294`）里，而 bot 的弹体在**任何一台客户端**上 `IsMine` 都是假的 ⇒ 一个都不会造出来、句柄一个都不会多吃 | **§72 / D53**。`usable` 改成白名单，33 → **42 把**武器 |
| 「组队模式加 3 个 bot，只有敌人的两个 bot 会打我；我的 bot 队友不打敌人，敌人也不打我的 bot 队友」 | `_hostile_humans()` 只扫 `room.human_seats()` —— 我方 bot 的对面全是 bot 时它「一个敌人都没有」，敌方 bot 也看不见我方 bot | **D52**。改名 `_hostile_targets()`，扫**所有座位**，判据只剩「位置已知 / 活着 / 不同队」 |

**换枪之后的槽位**（`/gun N` 列出来的就是这个）：

```text
角色 0/1/2/98/100/101/102/103/104/105   槽 1、2、3   ← 10 个角色三个槽全齐
角色 3                                  槽 1、2      （3 号 Damage=0，打不动人）
角色 106/107/108/110                    槽 1、3      （2 号是反弹弹 / 炮台）
角色 109                                槽 1、2      （3 号是等离子炮）
```

★ 代价是要照顾**引信**：`AppleGrenade`（角色 0 / 98 的 2 号）、
`SeedBomb`（角色 3 的 2 号）、`SliceBullet`（角色 110 的 3 号）的弹体在
`SliceTime / 32` 那一 tick 上**在每一台机器上自爆且不带伤害**，
所以服务端把它当**射程**用：飞不到就不开这一枪。实测只有角色 3 的
2 号在 1000 单位那一档会被挡住，别的都够。

### ★★ 会话 20：删掉一条凭空造的规则 + **bot 会自己走位了**

| 用户说的 | 改在哪 |
|---|---|
| 「『不往自己爆炸半径开炮』这一点没必要加，真人对局时也有可能会自杀……真人会自己评估风险与收益。后期加真正的 ai 时也需要让 ai 自己评估这一点。你在定规则的时候需要考虑一下真人对局的时候应该是什么样的。」 | **删了 `_in_own_blast()`**（**D50**）。留了回归钉子 `BotSplashTests.test_the_bot_still_fires_a_splash_weapon_point_blank`。★ D50 顺手把口径写死了：**规则只能来自原版 / 语料，权衡留给以后的 AI** |

**接着推进的**（M5 的第一件事）：**bot 不再只回放真人的轨迹了**
（**§71** / **D51**，新模块 `server/botmove.py`）。

* 走速 `ChrSpeed`/tick、冲刺 ×1.5、蹲 ×1/3、空中按键 ×1.5 —— 全是逆出来的；
* 重力 **1.2/tick²**（和子弹同一个常量）、起跳初速 **20**（语料 33971 段量的，
  顶点 167 vs 语料中位 170）、坡度上限 **2**（语料 p99）；
* 走位规则**只有两条**：**打得到就站住打**（语料 39% 的心跳是站着的）、
  **打不到就朝最近的敌人走**（§48 的距离分布说明真人是主动靠过去的）。
  地形只回答「这一步走不走得成」：坎前会跳、无底洞前会停（先算跳不跳得过去）。

★ **三种情形照旧回放真人轨迹**：没有地形产物的图、第一帧（拿真人的点当锚）、
闯关房（那儿就是要跟着推进）。

### ★ 会话 19 改的四件事 + 一个新功能

用户 2026-08-27 实机反馈：「现在能看见子弹了」（§62 / D41 ✅ 收口），
同一条消息报了三个新问题 + 转了一条 GPT 的发现。**四条全都属实，全都改了**：

| 用户说的 | 真因 | 改在哪 |
|---|---|---|
| 「有的时候我看见自己躲开了，但我身上还会有命中效果，也会掉血」 | ①`rpFire body+1` 被当成「武器槽」写死成 1，实际是**碰撞排除组** ⇒ 个人战里座位 0 的人根本撞不着；②服务端**压根没做命中判定**，`_impact_point()` 一律把爆炸点搬到目标身上并报命中 | **§63 / D42** + **§65 / D43** |
| 「火箭飞了一段打在地上了，但爆炸效果显示在我身上，我也掉血」 | 同上第 ② 条 —— 爆炸点是「目标此刻站的地方」，不是弹体撞上的那一点 | **§65 / D43** |
| 「2 号武器手榴弹扔出去的速度太快了，真人对战时飞得很慢」 | 抛物线武器一律用 `max_speed()` + 低抛解 ⇒ 手雷成了贴地平线的直球 | **§68 / D45** |
| 「有没有做真正的命中判定？似乎都是 100% 命中」 | 没有。现在有了：逐 tick 跑真弹道 + 三个碰撞圆 × 三档伤害 | **§65 / §66 / D43 / D44** |
| GPT：`rpFire body+1` 应该是 `座位+1` 不是 1 | ★ **GPT 是对的**，而且比它说的还多一层：组队房里那一格是**队伍号**，不是座位+1 | **§63** |
| 「双击左右移动键消耗体力触发近距离攻击，bot 还没实现」 | 确实没有。`rpDash`（`0x0007`）整条链逆完并实装 | **§64 / D46** |
| 「『保持交战距离』这一条要删掉」 | 删了 —— 那是我们自己加的、原版没有的设定 | **D47**（PLAN 的 M5 已改） |
| ★ 用户追加：「组队战不能直接伤害友军没错，但溅射可以伤害友军；有些手雷还有燃烧效果，燃烧期间也能伤害队友」 | **他是对的，我第一版写错了** —— 我把碰撞组的口径顺手推广到了溅射上。原版拿武器字段 **`SplashTeam`** 控制，而 **228 个节一个都没填** ⇒ 溅射和火墙恒为「撞所有人」，队友和射手自己都吃（语料 1513 发自伤） | **§69 / D48** |
| ★ 顺带挖出来的：`AppleGrenade` / `FlamingBottle` 那几类武器**多吃弹体句柄** | 公式 `shots × (2 if 溅射 else 1)` 只对 `GeneralBullet` 成立。bot 一旦 `/gun` 切到那几把，从此**子弹照飞、一滴血不掉**（静默）。已把 `usable` 收紧 | **§70 / D49** |

### ★ 实机验收清单（照这个看）

1. **躲得开就不掉血** —— 走位躲开 bot 的子弹，身上不该有命中效果、不该掉血；
2. **爆炸点对得上** —— 火箭打在地上就该炸在地上，不该炸在你身上；
3. **手雷看得见弧线** —— 中近距离扔出来是抛物线、飞得慢（0.5~0.7 秒），不是直球；
4. **打得中的时候照样掉血** —— 别修过头变成谁也打不中；
   打头 / 打身 / 打腿掉的血不一样（角色 2 的 1 号枪是 4 / 3 / 3）；
5. **近身**：贴到跟前时 bot 会冲一下（`rpDash`），伤害 23~36，
   受体力限制大概两三秒一次。
   ★ 带溅射的武器（2/3 号槽）**贴脸照开**（会话 20 / D50 改的）——
   炸到它自己就掉它自己的血，跟真人对射一样。**看到 bot 把自己炸死不是 bug。**
6. **组队房**（如果你测组队战）：bot 的子弹**穿过队友**（直接伤害没有），
   但它的手雷 / 火箭**溅射照样伤队友、也伤它自己**（§69，原版就是这样）。
7b. ★★★ **会话 22 的五条**（照这个顺序看最省事）：
   * **蓄力**：`/char N 1` 或 `/char N 2` 之后 `/gun N 2`。bot 扔手雷之前
     该有明显的**按住不放**（近距离 0.3 秒、远距离 1 秒出头），
     扔出去是**慢弧线**。★ 别人屏幕上还该看得见它在攒力气（心跳 `+15`）。
   * **火墙**：角色 1（面板 2）的 2 号武器炸在地上，该**留一道火**。
     ⚠ **那道火现在只有火、没有伤害**（§75 末尾），站上去不掉血是已知的。
   * **不抢跑**：一进图「预备 / 开始」那两秒 bot 只跑不打；被打死复活之后
     也一样。★ 这两条是同一道锁（原版 2000 ms）。
   * **角色只剩 3 个**：`/char N 4` 起会被拒，提示「只能是 1~3」。
   * ★★ **最要紧的一条**：换角色 1 的 2 号武器（火焰弹）打**几个回合**，
     看**掉不掉血**。火墙那一发在收方吃 9 个弹体句柄（§75），
     数错了的症状是「从那一发起子弹照飞、一滴血不掉」，
     **静默、换枪也救不回来**（要等换图）。

7. ★★ **会话 21 改了：`/gun` 的槽位回来了**（§72 / D53）。要看的是：
   * 角色 0 / 1 / 98 / 100 / 101 / 103 的 **2 号槽**现在能换了，
     换完**打得掉血**（这是最要紧的一条 —— 万一 §72 判错了，
     症状就是「子弹照飞、一滴血不掉」，而且静默）；
   * 还缺槽的只剩 106/107/108/110 的 2 号和 109 的 3 号（反弹弹 / 炮台 /
     等离子炮，飞行服务端还没模型）+ 角色 3 的 3 号（原版伤害就是 0）。
   ⚠ 万一 2 号槽真的「打不掉血」：**换回 1 号枪也救不回来** ——
   句柄一旦错位就一路错到底，要等**换图 / 新一局**才清（§42）。
   报的时候把「换枪之后打了几发才发现不掉血」一起说，那个数能反推出
   每发多吃了几个句柄。
8. ★★ **万一「子弹照飞、一滴血不掉」**：在聊天框敲 **`/dash`** 关掉近身再试。
   `rpDash` 在收方会**吃掉一个弹体句柄**（§64，语料量的），
   万一某个角色不是吃 1 个，症状正好是这个。关掉能当场分清是不是它。
9. ★★★ **会话 20 新增：bot 会自己走位了**（对战房，D51）。要看的是：
   * **不再站到你身上** —— 你站着不动时它不会一路挪过来贴着你；
   * **走起来自不自然** —— 有没有抽搐、一格一格平移、脚陷进地里、悬空；
   * **卡不卡住** —— 遇到跳不上去的台子 / 跳不过去的坑会停下或者原地蹦
     （**这是已知的**：绕路要等 A\*，见「下一步」）；
   * **闯关房照旧跟着你走**（那边一点没改）。
   ⚠ 万一走位很难看，`/hold N` 能当场把它钉住，剩下的开枪逻辑照跑。

### 🔴 用户给的硬事实（留着，验收时照这个看）

1. **弹道预测线 ≠ 子弹**：真人一进游戏就一直看得见**自己的**预测线（别人的看不见），
   随鼠标动；1/3 号武器白直线、2 号武器抛物线；开枪瞬间消失一下再回来。
2. **真正的子弹有模型、看得见在飞**。角色 2：1 号 = 双散弹枪、
   2 号 = **手雷**、3 号 = **火箭弹**（火箭模型 + 末段追踪）。
3. ★ **「保持交战距离」不是原版的设定** —— 多人对战里没有这回事（D47）。
4. ★ 真人**双击左右方向键**能消耗体力打出近身攻击（§64）。
5. ★★ **真人也会把自己炸死** —— 两个人离近了互相开枪，自杀是常态；
   真人只是**自己评估**近处爆炸的风险与收益。所以「不往自己爆炸半径开炮」
   这种禁令不许有（D50）。**定任何规则之前先问：真人对局时是什么样的。**

### ⛔ 禁令（各烧掉一轮以上，仍然有效）

| 禁令 | 为什么 |
|---|---|
| **不许再提「距离」当根因** | 用户否掉**三次**，而且他是对的（§62）|
| **不许再问「你看得见吗」** | 只换回一个布尔值，代价是用户真进游戏挨打；要查就自己跑 |
| **看到疑似目标物先找证伪观察** | §59 就是把预测线当子弹，一路顺着它编下去的 |
| ★ **不许擅自加原版没有的设定** | D47（「保持交战距离」）就是这么来的 |

### ★ 实机自助跑的路子（会话 18 跑通，下次直接照抄）

`tools\launch.ps1`（要诊断就先 `$env:BOT_DIAG_FIRE_ANYWHERE='1'`）→
`tools/gui_probe.py login <pid> testuser test` → `tools/click.py` 点 UI
（关活动弹窗 759,705 / 建立房间 116,650 / 确认 589,556 / 游戏开始 856,627）→
一个 30 行的 `type.py` 往聊天框敲命令（**要按两次回车**，走 `WM_CHAR`）→
`tools/screenshot.py <pid> x.png` 连拍。
⚠ 方向键走不动（DirectInput），**鼠标左键开枪可以**（`click.py <pid> x y`）。
⚠ `/char N M` 的 N 是**0 基座位号**（bot 在 1 号位就写 `/char 1 M`），不是面板序号。

<details>
<summary>会话 16 及以前的推进（已经被 §57 接管，留作记录）</summary>

**「看不见弹体」查到这一步：弹体在收方确实存在，但从来没被画出来。**

五轮实机把五条假设逐个否掉了：§50「等心跳」→ §52「贴太近」→ §54「爆炸太早」
→ §55「包的字节不一样」→ §56「太快没注意到」（**慢 10 倍 + 永不消失，还是看不见**）。
交叉验证也齐了：**开枪动作 / 枪口火焰看得见**（`OnFire` 完整跑完）、
**爆炸动画在自己身上**（坐标对）、**自己的子弹看得见**（参照系有了）。

⚠ 但上一局有个前提没控住：用户**开局前就 `/hold` 了 bot**，它整局站在出生点，
而用户在 500~800 单位外 —— **bot 多半在屏幕外**。⏳ 先排掉这一条再进渲染。

★★ 顺带修掉两个**真 bug**（都是从这次日志里抓出来的，见 §56）：
**bot 在 `0x0400`~`0x0402` 之间就开枪**（拿着上一局的弹体句柄，8 秒窗口）、
**`/gun` 换枪后模型要等下一次开火才变**。

★★ 意外收获：`/noboom` 实验把 **§46 的悬案定死了** ——
带溅射的武器那个额外句柄是**爆炸那一刻**分配的（用户关掉开关后永久打不中 =
句柄错位）。D34 那道闸门因此从「两种假设的同解」升级成**唯一正解**。

用户 2026-08-26 实机复验 D37（爆炸多等一发心跳）：**「还是看不到。而且这不是根本
原因的修复吧？和真人玩的时候并没有看不到子弹的问题，为什么真人玩的时候不用多等
一个事件？」** —— 这一问是对的，顺着它把整条链推翻重查：

| 会话 15 以为 | 实际（§52，逐指令 + 语料 + 实机日志三面证据）|
|---|---|
| 事件包要等一发带更大 N 的心跳才被收方执行（旧 §50）| ★ **错的**。收方**每帧**都 flush（`0x405810` 在 `GameSession::Tick` 里），而且包**一入队就把队列上界抬到 `seq+1`**（`0x54bb8c` → `0x54bb66`）。心跳的 N 只是**丢包时的兜底上界**（抬过空洞 → 发现缺号 → 讨重传）|
| 所以「看不见子弹」是 `rpFire`/`rpExplode` 挤在同一个 N 里 | ⚠ 会话 16 一度改判成「bot 贴到 20 个单位、子弹没有可见行程」，**也被用户否掉**（2026-08-27：拉开一屏照样看不见，而真人贴脸的手雷看得见）⇒ **真因未明，见 §53** |
| 「原版也一样，真人贴脸也看不见」（旧 §50 末尾那句圆场）| ★ **用户直接否掉**：真人贴脸扔的手雷看得见。语料量出来的真人交战距离（打中人的中位 **559**、≤20 单位只占 0.2%）**仍然有效**，但它不是看不见子弹的原因 |

★ 顺带查实的一条（**和这个 bug 无关，但 M5 要用**）：`trail_point()` 沿真人
轨迹回退的是**路程**（`BOT_FOLLOW_DISTANCE = 120`），真人站着不动时轨迹 64 个点
全在原地 ⇒ 路程累不到 120 ⇒ 返回最老那点 ⇒ **bot 站到人身上**（实机量到 20 个
单位）。这是 D16 的固有缺陷。★ **「保持交战距离」这条已经作废**（D47）——
要解的是「bot 自己决定站哪」，归 M5 的寻路。

**这一轮改了什么**（代码已落盘）：

| 文件 | 改动 |
|---|---|
| `server/bot.py` | ★ 撤掉 D37 那道 `announced` 闸门 —— `_explosion_ready()` 回到**只看「飞到了没有」**；★ 新增：`shot.ticks < 1.0`（收方连一个逻辑步长都推不完）的**当场结算**，不占用下一帧（**D38**）|
| `server/botsync.py` | 删掉 `BotSyncStream.announced`（没有用户了）；`heartbeat()` 的文档改成 N 的**真实**作用 |
| `server/test_botsync.py` | `BotVisibleBulletTests` → **`BotPointBlankTests`**（验贴脸当场结算 + 真要飞的照旧等）；新夹具 `approach_far()`（`approach()` 只有 40 单位，那是贴脸）；`BotBallisticFireTests` 的 6 个用例改用它 |
| `re/packet_api.md` | §1.3 补一张「队列什么时候被执行」的表；§5 那句「收方拿 N 做 `FlushTo(N)`」加反向说明 |
| `server/bot.py`（再） | ★ 新增诊断命令 **`/noboom`**（`_cmd_noboom` + `BotConn.no_explode`）：只发 `rpFire` 不发 `rpExplode`，让弹体一直飞 —— 用来分清「弹体没造出来」还是「爆炸发得太早」（§53）。**M3b 收口后删** |
| `server/test_botsync.py`（再） | 新增 `BotNoBoomCommandTests`（4 个）+ `HumanFireLogTests`（3 个）|
| `server/bot.py`（三） | ★ `/noboom` 开着时**句柄步进降成 `weapon.shots`** —— 溅射那一格是爆炸时才分配的（§54），不改的话关掉开关后永久打不中（用户实机踩到）|
| `server/gameserver.py` | ★ 诊断 **`note_human_fire()`**：真人本图第一发 `rpFire` / `rpExplode` 原样打进日志，格式和 bot 那行 `开火:` 一致，好并排比（§54 末尾）。`human_fire_logged` 跟着 `reset_sync_trails` 清。**M3b 收口后删** |
| `server/bot.py`（四） | ★ `开火:` / `爆炸:` 日志改成按 **(武器 id, 飞行 tick 取整)** 翻转去重 + **带完整包 hex**；★ 新增诊断命令 **`/slow`**（`_slow_shot`，初速降 1/10 —— 改的是包里 `+18`，收方的初速就是 `power × Velocity`）。**M3b 收口后删** |
| `server/test_botsync.py`（三） | 新增 `BotSlowBulletTests`（3 个）|
| `server/bot.py`（五） | ★★ **修 bug**：`_tick_bot()` 加 `_battle_started(room)` 闸门 —— `0x0400`~`0x0402` 之间一发不发（§56）；★ **修 bug**：`_cmd_gun` 当场发 `rpChangeWeapon`，不再等下一次开火 |
| `server/test_botsync.py`（四） | 新增 `BotLoadingWindowTests`（3 个）+ `BotGunDeclaresAtOnceTests`（2 个）|

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1365 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样全绿**。
★ 新用例验证过「把 `instant` 那一行去掉就会红」。

★★ **真正该修的是 bot 站位**，归 **M5** 的寻路。★ 当时写的「保持交战距离」
**已经被用户否掉了**（D47）—— 那是原版没有的设定。

</details>

**会话 17 改了什么**（代码已落盘）：

| 文件 | 改动 |
|---|---|
| `hook/bshook.c` | ★ 新增**弹体诊断**三件套（§58）：`FIRE>`（`OnFire` 0x491f12 的参数）、`PROJ+`（`ProjectileMgr::Add` 0x473e7c 的全字段快照）、`PROJ.`（每帧 tick 0x47de6a，按整数位置翻转打轨迹）。`BSHOOK_PROJ_DIAG=0` 可关。**M3b 收口后删** |
| `hook/bin/bshook.dll` | 已重编（MSVC 2017 x86），`bshook.c` 零警告；三个 naked detour 的栈偏移用 `dumpbin /disasm` 核过 |
| `server/bot.py` | ★ 临时诊断 **`BOT_DIAG_FIRE_ANYWHERE`**（环境变量，**默认关**）：开着时 bot 无视交战距离和地形遮挡、瞄准点强按到「自己 + 400」，并逐帧报 `_diag_why_not_firing()`「为什么不开枪」。取证专用，**M5 那条实机验完就删**（D40）|

★ 这一轮**没有修 bug**，只是把根因钉死了。修复归 M5。

**会话 22 改了什么**（代码已落盘，**没有提交**）：

| 文件 | 改动 |
|---|---|
| `server/ballistics.py` | ★★ 蓄力（§73）：`POWER2_CHARGE_STEP` / `POWER2_MAX` / `POWER2_MIN` / `POWER2_FLOOR` + **`charge_power()`** / **`charge_ticks()`**；★ `max_speed()` 对模式 2 改成 `speed_for_power(80)`（原来错用 `MaxVelocity`）|
| `server/bot.py` | ★★ `BOT_ACTION_LOCK_S` + `_note_action_lock()` / `_may_act()`（§74）；★★ `_charge_ready()` / `_hold_trigger()` / `_charge_value()` / `_snap_charge()`（§73）；★★ `_fire_wall_of()` / `_set_ground_on_fire()`（§75）；`BotConn` 加 `act_lock_until` / `was_lying` / `charge_at`（`reset_battle_frame` 跟着清）；★ `BOT_CHARACTER_PANEL_IDS` + `_cmd_char` 收成 1~3（D54）|
| `server/botsync.py` | ★ `OP_SET_ON_FIRE` + `set_on_fire_body()` + **`fire_wall_handles()`** + `BotSyncStream.set_on_fire()`（组包 + 句柄 `+2n+1` 在同一次加锁里）|
| `tools/weapondata.py` / `server/weapondata.py` | `_FIELDS` 加 `SliceId` / `SpawnCount` / `SpawnLifeTime` / `SpawnInterval` / `SpawnDistance`；`FORMAT` 4 → **5** |
| `server/bot_weapons.json` | 重新提取（`format` 5，80 KB）|
| `server/test_botsync.py` | 新增 `BotFireWallTests`（5）/ `BotActionLockTests`（6）/ `BotChargeTests`（7）；`BotFrameRoom` 加 `action_lock` 开关 + `unlock_bots()`，`BotFireRoom` 加 `charge()` |
| `server/test_ballistics.py` | 蓄力那三条钉子（合法档 / 上限 / 按住几个 tick）|
| `server/test_bot.py` | 「bot 只能用初期三个角色」的钉子；两个用例的面板序号跟着改 |
| `re/packet_api.md` | §5.2 `+18` 补三种模式的取值；§5.4d 补 **`2×SpawnCount+1`** 的句柄消耗；§5.5 `+15` 从 🔍 改成 ✅**蓄力计数器** |
| `develop_history/.../{FINDINGS,DECISIONS,PROGRESS}.md` | §73 / §74 / §75 / D54 / D55 |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1489 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样全绿**。
**实机**：★ 这一轮**没有实机验证**。

**会话 21 改了什么**（代码已落盘，**没有提交**）：

| 文件 | 改动 |
|---|---|
| `server/bot.py` | ★★ `_hostile_humans()` → **`_hostile_targets()`**（D52）：扫所有座位、位置走 `_seat_body()`、返回 `(座位, x, y, 蹲着没有)`；五个调用点跟着改（`_enemy_spot` / `_own_step` / `_fire_target` / `_dash_target` / `_diag_why_not_firing`）。★ 新增 **`_outlives_fuse()`**（§72 的引信）+ `_fire_target()` 加这一条；`_shell_max_ticks()` 多收一个 `weapon` 参数、按引信收上界；`_cmd_gun` 的文档改成新的槽位表 |
| `tools/weapondata.py` | ★★ `PLAIN_BULLET_CLASS` → **`SAFE_CLASSES`** 白名单 + `FUSE_CLASSES` + `fuse_ticks_of()`（D53）；`_FIELDS` 加 `SliceTime`；`FORMAT` 3 → **4** |
| `server/weapondata.py` | `FORMAT` 3 → **4**；新增 `fuse_ticks` 属性 |
| `server/bot_weapons.json` | **重新提取**：可用武器 33 → **42**，`format` 4 |
| `server/test_botsync.py` | 新增 `BotVersusBotTests`（5，验 bot 互为敌人）+ `BotFuseTests`（6，验引信） |
| `server/test_weapondata.py` | `test_only_plain_bullets_are_usable` → **`test_only_classes_we_have_a_flight_model_for_are_usable`**；新增「每个角色都有 2 号槽（除了那 4 个）」和「带引信的必须算得出引信」两条钉子 |
| `re/packet_api.md` | §5.8 加 `IsMine` 那道门的**全部**位置；§5.9 的句柄公式加「和 `CreatingClass` 无关」+ 引信；`owner 编码` 那一行的怪 / 中立**从 20 勘误成 30**；句柄分配点从 `0x484920` 改成 `0x49172e` / `0x484924` |
| `develop_history/.../{FINDINGS,DECISIONS,PROGRESS}.md` | §72（★ 推翻 §70）/ D52 / D53；§70 和 D49 都加了「已被推翻」的横幅 |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1468 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样全绿**。
★ 两条钉子都验证过「改回旧行为就会红」（`BotVersusBotTests` 5 条里红 4 条）。
**实机**：★ 这一轮**没有实机验证**。

**会话 20 改了什么**（代码已落盘，**没有提交**）：

| 文件 | 改动 |
|---|---|
| `server/bot.py` | ⛔ **删** `_in_own_blast()` 和它的调用点（D50）；`_fire_target()` 的文档写清「这里没有那一条，别再加回来」。★ 新增**自己走位**：`BOT_MOVE_MAX_TICKS` / `_character_of()` / `_enemy_spot()` / **`_move_intent()`** / **`_own_step()`**，`BotConn` 加 `body` + `move_at`（`reset_battle_frame()` 跟着清），`_tick_bot()` 的位置分支改成「先自己走，走不了才回放轨迹」 |
| `server/botmove.py` | **新增**。人的运动模型（§71）：`Body` / `walk_speed()` / `jump()` / `tick()` / `advance()` / `settle()` / `blocked()` / `leaves_ground()` / `drop_below()` / **`jump_lands()`**（真跑一遍跳跃弧线）|
| `server/test_botmove.py` | **新增** 31 个用例（合成地形 6 类 + 真产物走 240 tick 的回归）|
| `server/test_botsync.py` | 删掉 `_in_own_blast` 那个用例，换成**回归钉子**「贴脸照开」；新增 `BotOwnMovementTests`（6）/ `BotCoopMovementTests`（1）+ `synth_terrain()` / `TerrainMixin` |
| `server/run_tests.py` | 挂上 `test_botmove` |
| `tools/build-common.ps1` | 必选文件清单加 `botmove.py`（漏了的话包一定是废的）|
| `develop_history/.../{FINDINGS,DECISIONS,PLAN,PROGRESS}.md` | §71 / D50 / D51；D48 第 2 条划掉 |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1455 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样全绿**。
★ 3.8 上先红了两条，根因是 `int(0.128 × 31.25)` 在两个版本上分别取到 3 和 4
—— 顺手把 `_own_step()` 改成**余数留到下一帧**（不然 bot 稳定比真人慢 23%）。
**实机**：★ 这一轮**没有实机验证**。

**会话 19 改了什么**（代码已落盘，**没有提交**）：

| 文件 | 改动 |
|---|---|
| `server/botsync.py` | ★★★ `FIRE_SLOT_DEFAULT` → **`fire_group(seat, team)`**（§63 / D42）；`fire_body(slot=)` → `group=`。★ 新增 `OP_DASH` + `dash_body()`（§64）、`OP_SPLASH_DAMAGED` + `splash_body()`（§67）、`BotSyncStream.dash()`（**组包 + 句柄 +1 在同一次加锁里**）|
| `server/bot.py` | ★★★ **真命中判定**（§65 / D43）：新增 `Shell` / `_advance_shells()` / `_shell_step()` / `_segment_circle_t()` / `_terrain_stop_t()` / `_resolve_shell()` / `_splash_targets()`，**删掉** `_impact_point()` / `_explosion_ready()` / `_flush_explosions()` / `BOT_AIM_HEIGHT`。★ `_seat_group()` / `_battle_bodies()`（碰撞组过滤，和收方同一口径）。★ `_lob_speed()`（§68 / D45）+ `_aim_point()`（瞄身体圆心）。★ **近身冲刺**：`DashSwing` / `_try_dash()` / `_advance_dash()` / `_dash_hits()` / `_dash_target()` / `_regen_stamina()` + `/dash` 命令（§64 / D46）|
| `server/chrprops.py` | **新增**。角色三个碰撞圆 + 冲刺招式 + `GameProps.ini` 体力常量的加载器（§66） |
| `tools/chrprops.py` | **新增**。从 `ChrProps.ini` + `GameProps.ini` 离线提取 → `server/bot_chrprops.json` |
| `tools/update-chrprops.bat` | **新增**（CRLF + UTF-8 无 BOM，已非交互跑通） |
| `server/bot_chrprops.json` | **新增产物**，17 个角色，14.8 KB |
| `server/weapondata.py` | 加 `head_damage` / `legs_damage` / `damage_for(region)` / `size` / `splash_damage`；★ `FORMAT` 2 → **3** |
| `tools/weapondata.py` | ★★ `_is_usable()` 加第 6 条 **`CreatingClass == GeneralBullet`**（§70 / D49）；`FORMAT` 2 → 3。产物已重新提取：可用武器 48 → **33** |
| `server/bot.py`（再） | ★★ **修第一版写错的地方**（§69 / D48）：`_battle_bodies()` 加 `group=None` / `include_self`，**溅射不按碰撞组过滤**（队友和自己都吃）；~~新增 `_in_own_blast()`~~ ⛔ **会话 20 删了（D50）** |
| `server/test_weapondata.py` | 「三个槽位全可用」改成「至少有 1 号枪」+ 新增「只放行 GeneralBullet」的回归钉子 |
| `server/test_chrprops.py` | **新增** 22 个用例 |
| `server/test_botsync.py` | `BotHitDetectionTests`（8）/ `BotSplashTests`（4）/ `BotLobTests`（3）/ `BotDashTests`（9）+ `EventBodyTests` 加 5 个；`REAL_FIRE_SEAT1` 真包；`arrive()` 改成「把出膛时刻往回拨」；★ `BotFireRoom.melee = False`（那批用例验的是开枪，近身会抢在前面）|
| `server/run_tests.py` | 挂上 `test_chrprops` |
| `tools/build-common.ps1` / `build-portable.ps1` / `build-server-package.ps1` | `Copy-ChrProps` / `Update-ChrProps` + 必选文件清单加 `chrprops.py` |
| `re/packet_api.md` | §5.2 `rpFire +1` 从「武器槽 🤔」改成「碰撞排除组 ✅」；新增 **§5.4b `rpDash`**、**§5.4c `rpSplashDamaged`**；opcode 一览表跟着改 |
| `server/bot_mapdata/*.json`（175 个）| ★ **只是行尾**：仓库里那批是 CRLF（更早版本的提取器留的，违反铁律 3），这一轮跑 `Update-MapData` 重新提取成 LF。**逐文件比对过：175 个全部只差行尾，内容一个字节没变。** 工作区里那 175 个「M」就是这件事 |
| `tools/build-common.ps1`（再） | ★ 修一个**没暴露过的**坑：`$script:WeaponDataUpdated` 从来没声明过，而文件开头是 `Set-StrictMode -Version 2.0` ⇒ `Update-WeaponData` 第一次被调就抛 `VariableIsUndefined`。整包回归还没跑过（M6）所以一直没炸。三个标志现在一起在开头声明 |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1417 全绿**；
`runtime-win7\python\python.exe`（CPython 3.8）→ **同样全绿**。
**实机**：★ **这一轮没有实机验证** —— 见上面那份验收清单，等用户。

M4 已完成并通过实机核对。

| # | 里程碑 | 状态 |
|---|---|---|
| M0 | 进度管理体系 | ✅ 完成（会话 01） |
| M1 | 房间内的 bot（`/bot` `/char` `/tm` `/del` `/ready` `/h`） | ✅ **完成**（实机验证通过）。★ M3b 追加战斗中的 `/hold` `/gun`，会话 19 追加 `/dash` |
| M2 | 开局链路（bot 进图、会死会复活、进结算） | ✅ **完成**（死亡 / 复活 / 不换角色 / 闯关 3 条命 / **进度条**全部实机确认） |
| M3a | bot **会动会跳** | ✅ **完成**：会动会跳、跳跃流畅、身体朝向、走路动画、**冲刺、蹲下**全部实机通过 |
| M3b | bot **会开枪** | ✅ **完成**：打得中、换得了枪、**子弹看得见**（会话 12~18 实机通过，§62 / D41）|
| M4 | 地图地形数据 | ✅ **完成**（会话 06，实机核对通过）：174/174 提取、加载器 + 测试 + 打包钩子 |
| M5 | bot AI（寻路 / 追敌 / 瞄准 / 难度） | 🟡 **进行中**。★ 会话 19 落盘：**真命中判定**（§65）、**碰撞排除组**（§63）、**三个碰撞圆 × 三档伤害**（§66）、**抛物线按刚好够得着扔**（§68）、**近身冲刺攻击**（§64）。★ 会话 20 落盘：**自己走位**（§71 / D51，`botmove.py`）—— ⏳ 全部等实机。★ 凭空造的两条规则**都已删**（D47 / D50）。**下一件事是绕路（A\*）** |
| M6 | 测试 · Win7 兼容 · 文档 · 打包回归 | ⬜ |

详细内容见 `PLAN.md`。

★ **git 工作区是脏的，我没有提交** —— 用户 2026-08-25 明确要求
「任何情况下都不要对我的 git 进行 commit / push / revert」。改完停在工作区，
提交由用户自己来。

---

## ⏳ 下一步

### 1. 先等实机（上面那份验收清单）

命中判定改的是**最容易「修过头」**的一块：以前百发百中，现在可能变成
「怎么都打不中」。这件事单测判不了 —— 它取决于三个圆的尺寸、瞄准点和
真人走位的相互关系，只有实机看得出来。

### 2. 然后：**绕路（可达性搜索 A\*）**（M5 现在的第一件事）

会话 20 把**运动**做完了（`botmove.py` / §71 / D51）：bot 按地形自己走、
自己跳，不再只回放真人轨迹。剩下的是**绕路** —— 现在的走位只会
「朝敌人那个方向走，前面有坎就跳」，遇到跳不上去的高台 / 跳不过去的坑
就停在那儿（16 张真图跑「从这头走到那头」：12 张到得了，3 张卡住）。

要做的是拿 M4 那份逐像素碰撞位图建一张**可达图**（站立面之间的边 =
走得过去 / 跳得过去 / 掉得下去，`botmove.jump_lands()` 已经能算弧线），
再 A\* 找路。⚠ **不是**「离敌人远一点」那种规则（D47 / D50 已经否掉了）。

### 3. 顺带（不挡实机）

- **提前量**：现在瞄的是目标**此刻**的位置，真人打移动目标会往前带一点。
  判据是弹道飞行时间 × 目标速度，不是「故意打偏几度」。
- **闯关房打怪**：怪的句柄服务端手里没有，得先接上控制格那条路。
- ✅ **地面燃烧（火墙）** —— 会话 22 做完了（§75）。⬜ 剩**伤害**：
  火墙现在只有火，站上去不掉血。要补得先逆清火焰对象的碰撞节奏
  （语料里 13160 发 `rpSplashDamaged` 的源句柄全 ≥ 100000，分不出哪几发是火）。
- ⬜ **苹果雷的分裂**（角色 0 的 2 号，`SliceCount=4`）：那 4 片是
  **只在射手那台机器上本地创建**的（`0x47c97b` 那一段在 `IsMine` 门里，
  而且**不走网络包**）—— 和火墙不一样，没有现成的 opcode 可以补。
  要做得先弄清「原版里别人到底看不看得见那几片」。§70 量到的残差 +3
  也和「4 片各占 1~2 个句柄」对不上，机制没吃透。
- ⬜ **追踪火箭**（角色 1 的 3 号，`HomingRange=200 / HomingAngle=30`）：
  服务端的弹道是直线，收方会让它拐弯（`0x47e2cc` 选目标）。
  影响只在「擦边球」上 —— bot 打得中的照样中，只是没有真人那点追踪加成。
- ⬜ **剩下 3 类武器**（商城角色的，D54 之后 bot 用不到了，降级为可选）：
  `BounceBullet` / `RasTurret` / `PlasmaCannon`，都是**飞行**没模型。

### 验完就删的诊断脚手架（D40 的收口清单，D41 / D43 沿用）

`/noboom`、`/slow`、`gameserver.note_human_fire()`、
`bot.py` 的 `BOT_DIAG_FIRE_ANYWHERE` + `_diag_why_not_firing()`、
`bshook.c` 的三个 `PROJ*` detour。
★ `/dash` **不在这个清单里** —— 它是玩法开关兼排除手段，留着。

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

## 会话 15 又做了什么（★ 其中「看不见子弹」那半边**已被会话 16 撤掉**）

| 文件 | 改动 |
|---|---|
| `server/bot.py` | ★ `_reload_after_shot()` —— 完整的弹匣模型（D36）；`BotConn.rounds_left`（换枪 / 换图跟着清）；🔴 ~~`_explosion_ready()` 的第二道闸门（D37 / §50）~~ —— **建立在错的事实上，会话 16 已删**（§52 / D38）；`pending_shots` 每条多带一个 `fire_seq`；`_flush_explosions()` 顺手丢掉换代残留；开火日志加上弹匣节奏 |
| `server/botsync.py` | 🔴 ~~`BotSyncStream.announced`~~ —— 同上，**已删** |
| `server/weapondata.py` | 新增 `magazine` / `cooling_ms` / `reload_ms` 属性；`fire_interval_ms` 的 docstring 标明「有弹匣的不能只看它」|
| `server/test_botsync.py` | 新增 `BotMagazineTests`（5 个）+ ~~`BotVisibleBulletTests`~~（会话 16 换成 `BotPointBlankTests`）；夹具拆出 `arrive()` |

**测试**：`C:\Python314\python.exe server\run_tests.py` → **1365 全绿**；
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
| ★★★ **贴脸时看不见子弹** | ✅ **查明了，不是时序是距离**（§52）：bot 贴到目标身上（实机日志量到 **20 个单位**），弹体在收方**连一个 tick 都推不完**，没有可见行程。真人打中人的中位距离是 **559**。⇒ **原版物理，不是 bug**；该修的是 **bot 站位**（M5）。⏳ 等实机确认「走开就看得见」|
| ~~事件包要等下一发心跳才被执行~~ | 🅿️ **推翻了，别再按这条想**（§52）：flush **每帧**跑，包**一入队就抬上界**。心跳的 N 只是丢包时的兜底上界 |
| bot 在道具模式里要不要捡道具 | 🅿️ 暂不做（PLAN「明确不做的事」） |
| ★ `server/bot_mapdata/` 的 175 个 json 是 **CRLF**（违反铁律 3）| 🟡 **工具已修好、产物还没重跑**。`tools/mapdata.py` 写文件时少了 `newline="\n"`，Windows 的文本模式把 `\n` 转成了 `\r\n`（M4 就有，`index.json` 1280 个 CR）。功能上无害（JSON 允许 `\r\n` 当空白），但规矩上不对。**跑一次 `tools\update-mapdata.bat` 就全好**，代价是 git 里会出现 175 个「只改了行尾」的文件 —— 什么时候跑由用户定 |
| bot 的等级固定 `4`（`BOT_LEVEL`），显示上会不会突兀 | 🤔 只影响玩家列表那一格，实机看一眼再说 |

---

## 不要重做的事

- **客户端版本号 / 升级提示链** —— V0.2 已完成并实机验证，见 `FINDINGS.md` §12。
- **`UdpPacket` 校验和** —— 已逆出且本版重新实测 25091/25091 全中，
  直接抄 `tools/fakeclient.py` 的 `udp_checksum()`，见 `FINDINGS.md` §4。
- **§7 那个「只剩一人时同步被关掉」的对策** —— 已作废，见 `FINDINGS.md` §13。
