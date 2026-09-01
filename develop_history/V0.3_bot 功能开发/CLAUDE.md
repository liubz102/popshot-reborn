# 炮炮火枪手 · V0.3 房间 bot 功能开发

复活 2007 年的《炮炮火枪手》（NEXON《Big Shot: Kaska Tournament》，世纪天成代理，2009 停服）。

**V0.1（基础单机）已完工并归档**：客户端能在 Win10 上启动，localhost 假服务端撑起
登录 → 大厅 → 建房 → 闯关 → 结算 → 回房间的单机闭环。

**V0.2（联机）已完工并归档**：真多人服务端、网页注册、云端联机对战与合作闯关、
原版 TCP 中继、位置数据 UDP 旁路、版本门禁 + 自研自动更新器。

**V0.3 要做的事**：**房间 bot**。人少时房主在聊天窗口敲命令加一个 bot 陪打 ——
房间里占一个座位、显示昵称 `bot N`、能换角色 / 换队伍 / 删除 / 一键准备；
开局后能在地图里跑动、跳跃、开枪，难度适中；对战模式打敌人，
闯关（任务）模式打怪并跟着真人推进。附带做一套**离线地图地形提取**
（服务端本来一点地图几何都没有）。

这是一个**跨多会话、多 agent、全程由 AI 实施的长工程**。任何一个新会话都可能是接手方。

---

## 🔴 开工前必读（每个会话第一件事）

按顺序读这三个文件，**不要重新推导已知结论**：

1. `.claude/PROGRESS.md` —— 现在在哪、正在做什么、下一步、当前卡点、**⏳ 待用户验证**
2. `.claude/FINDINGS.md` —— 已经查明的硬事实，以及**已经试过但不行的路子**（别重复踩坑）
3. `.claude/DECISIONS.md` —— 为什么这么选，别无意义地推翻已定方案

总计划在 `.claude/PLAN.md`（M0 ~ M6 路线图）。

### ★★ 动协议之前的第 4 个文件：`re/packet_api.md`

**客户端 ⇄ 服务端通信包的参数 / 返回值手册**（按 opcode 排，每个字段带
✅实测 / 🔍静态 / 🤔推测 / ❓未知 的可信度标记，并写明「这一发**必须回什么**」）。

- 要**读**某个包的字段、要知道某个 opcode 不回会怎样 → **先查它**；
- 逆出**新**字段、或纠正旧结论 → ★ **写 FINDINGS 的同时把它一起改掉**（见「收工规则」）。

V0.3 会往里面加不少东西：`UdpPacket` 的内层 opcode body（§5 那张表现在只有名字，
没有线格式），全部要补进去。

### ★ V0.1 / V0.2 的结论在哪（**只读参考，不要重新推导**）

```text
develop_history/V0.1_基础单机功能开发/CLAUDE.md + .claude/{PLAN,PROGRESS,FINDINGS,DECISIONS}.md
develop_history/V0.2_联机功能开发/CLAUDE.md    + .claude/{PLAN,PROGRESS,FINDINGS,DECISIONS}.md
```

**引用方式**：写「V0.1 §119」「V0.2 §216」「V0.2 D103」，指的就是那两套文件里的条目。
V0.2 的 `FINDINGS.md` 有 41 万字符，**别整份翻** —— 先在本版的 FINDINGS 里找有没有
转述，再按 §号 `sed -n` 定位过去。

### ★ 编号规则（★ V0.3 全部从 1 重新起，不接 V0.2）

| 东西 | 规则 |
|---|---|
| `FINDINGS.md` 的 §号 | **V0.3 从 §1 起**。引用旧版一律写全「V0.1 §xxx」「V0.2 §xxx」 |
| `DECISIONS.md` 的 D号 | **V0.3 从 D1 起**。同上 |
| `sessions/` 的会话号 | **从 01 起**，文件名 `YYYY-MM-DD-NN.md` |

## 🔴 收工规则

- **每完成一个可验证小步就立刻更新 `.claude/PROGRESS.md`**，不要攒到会话结束
  —— 上下文随时可能被打断，攒着就丢了。
- 新查明的硬事实立刻追加进 `.claude/FINDINGS.md`（**失败的尝试也要记**）。
- ★★ **只要新结论涉及「某个包的某个字段 / 某个 opcode 要回什么」，就必须同时更新
  `re/packet_api.md`**。漏更新的代价：下一个会话要么重复逆一遍已经查明的东西，
  要么照着过期结论写错包。
- 做了方案选择就记进 `.claude/DECISIONS.md`（记「为什么」，不记「是什么」）。
- 会话结束时在 `.claude/sessions/YYYY-MM-DD-NN.md` 写一份日志：做了什么、结果、遗留。

### ★★ V0.3 特有：**只记关键信息，语言要短**

V0.2 的四个文件加起来 84 万字符，后期每个会话光读索引就吃掉大量上下文。
V0.3 立三条硬规矩：

1. **判据是「下一个会话需不需要它」**。需要 → 记；只是「我改了什么」→ 不记。
   改了哪一行 git 有；为什么这么改才要写下来。
2. **FINDINGS 一条一个小节，先写结论再写证据**，不写调查过程、不贴大段反汇编
   （贴地址 + 那一句指令就够，要细节的人自己去 Ghidra 看）。
3. **PROGRESS 只保留当前状态**，做完的事从「正在做」挪走，不留历史；
   流水账进 `sessions/`。

## 🔴 ★ 需要用户实机操作时，停下来问用户

有些验证 agent 自己做不了 —— 真打一局、局域网另一台电脑上登录、两个账号同时进同一个
房间、把服务端包丢到云主机上跑。V0.3 里**尤其**是这一类：
「bot 在别人屏幕上到底动没动、打没打得到人」只有实机看得出来。

**做法**：

1. 一次把话说全：**要做什么操作 / 期望看到什么 / 怎么把结果告诉我**
   （截图路径？日志片段？还是一句「行」「不行」）。
2. 把这条登记进 `PROGRESS.md` 的 **「⏳ 待用户验证」** 区，写清「怎么做 / 期望结果 / 当前状态」。
3. **然后停下来等**，不要假装验证过了，也不要用「大概能行」代替实测结论。
4. 在等的同时，把**不依赖这条结论**的活儿继续做完。
5. 用户回话后**立刻**更新 `PROGRESS.md`，结论进 `FINDINGS.md`。

反过来也成立：**没实机跑过的就明说没跑过**。V0.1 的教训是「单元测试全绿 ≠ 客户端认」，
客户端只认字节和时序。

---

## 目录

| 路径 | 用途 |
|---|---|
| `game_org/` | **原始安装结果，只读，永不修改**（不在本工作副本里，见下方「素材在哪」） |
| `原版安装包/` | **原始安装包（368MB NSIS），只读** |
| `Pack_decrypt/` | **解开的 `Pack\*.pkn` 资源树，只读参考**。★ V0.3 的地图地形数据就从 `Pack_decrypt/Maps/*.map` 提取 |
| `tools/` | 便携逆向工具（x64dbg / Scylla / Ghidra / Sysinternals）+ 自写探针、启停脚本、打包脚本。★ V0.3 新增 `mapdata.py` + `update-mapdata.bat`、`weapondata.py` + `update-weapondata.bat`、`chrprops.py` + `update-chrprops.bat`。★ 逆向工具箱是 **`re_bs.py`**（`xref` / `dis` / `func` / `str` / `callers`，capstone 直接读 `re/BigShot_22524.img`）|
| `re/` | 逆向产物：`BigShot_22524.exe`、`packets.txt`、`rtti_types.txt`、`vftables.json`（机械生成，**别手改**）、★ **`packet_api.md`** |
| `hook/` | MSVC x86 工程：`bshook.dll`（注入）+ `bsloader.exe`（启动器） |
| `updater/` | 自研更新器（C 工程），编译产物是 `game_patched\BsPatcherChn.exe` |
| `server/` | Python 服务端（**单机假服务器和云端服务端是同一套代码**）。★ V0.3 新增 `bot.py` / `botsync.py` / `ballistics.py` / `mapdata.py` / `weapondata.py` / `chrprops.py` / **`asynclog.py`**（日志异步出口，D109），以及三份产物 **`bot_mapdata/`**（地形）、**`bot_weapons.json`**（武器表）、**`bot_chrprops.json`**（角色碰撞圆 + 冲刺招式 + 体力常量）—— 都进 git、进两个包。★ `server/data/` **只装用户数据**（`accounts.json` / `tickets.json`，都 `.gitignore`），别往里塞产物（D22） |
| `runtime/python/` | 内置 CPython 3.14.3 x64 embeddable |
| `runtime-win7/python/` | **Win7 兼容运行时** CPython 3.8.10 win32。★ 改服务端代码后顺手 `runtime-win7\python\python.exe server\run_tests.py`，别把 3.8 兼容性弄丢 |
| `runtime-linux/` | 服务端包发给 Linux 的那份 CPython 3.14.7，**故意不解压** |
| `bug调查/` | 线上问题的抓包和日志。★ `server_logs/*.dec.bin` 是**客户端→服务端的明文流**，V0.3 逆 `UdpPacket` body 的主要语料 |
| `logs/` | 抓包 / 调试日志 |
| `dist/` | 打包产物（**客户端包 + 服务端包，两个**） |
| `develop_history/` | **项目的连续开发记录，不是只读归档。** 每次代码变更 / 新发现都要按「收工规则」同步更新**当前版本**的文件；旧版本只作参考 |

### ★ 原版素材在哪（本工作副本里没有）

`game_org/` `原版安装包/` `Pack_decrypt/` 体积太大，`.gitignore` 掉了，只在 `main`
worktree 里：

```text
D:\git\popshot-reborn\main\Pack_decrypt\Maps\        ← V0.3 的地图源文件在这
D:\git\popshot-reborn\main\Pack_decrypt\Data\        ← ChrProps.ini / map.ini 等
```

本工作副本是 `D:\git\popshot-reborn\develop\3_bot`（分支 `develop/03_bot`）。

## 铁律

1. **`game_org/` 和 `原版安装包/` 只读。** 想改什么都去 `game_patched/`。
   原始件毁了就再也找不回来了（客户端是从已停运 15 年的游戏里抢救出来的）。
2. **不要让 2007 年的 nProtect GameGuard 真的跑起来。** 它会尝试装内核驱动。
   `game_patched/GameGuard.des` 必须保持改名状态。
3. **换行符和编码，三套规矩，别记混**：

   | 类型 | 行尾 | BOM | 理由 |
   |---|---|---|---|
   | `.bat` / `.cmd` | **CRLF** | UTF-8 **无** BOM | LF 的 bat 在中文 Win10 + `chcp 65001` 下会被拦腰截断（报 `'ho' 不是内部或外部命令`） |
   | `.ps1` | **CRLF** | UTF-8 **有** BOM | 无 BOM 的 ps1 会被 PowerShell 5.1 按 CP936 读，中文注释乱码后变成语法错误（V0.1 §106） |
   | `.sh` / `.py` / `.mjs` / `.json` / `.md` | **LF** | 无 BOM | **服务端包要在 Linux 上跑，`.sh` 带 CR 直接 `bad interpreter: /bin/sh^M`** |

   ★★ **写中文文件一律用 Write / Edit 工具，不要用 Bash 的 heredoc**
   —— 这台机器的 Git Bash 把 heredoc 里的中文按 **CP936** 落盘，不是 UTF-8（本版 §1）。

   写完 `.bat` 后转 CRLF：
   ```powershell
   $p='路径.bat'; $c=Get-Content -Raw -Encoding UTF8 $p
   $c=$c -replace "`r`n","`n" -replace "`n","`r`n"
   [System.IO.File]::WriteAllText($p,$c,(New-Object System.Text.UTF8Encoding($false)))
   ```
   `.ps1` 同样转 CRLF，但最后一个参数换成 `UTF8Encoding($true)`（带 BOM）。
   含 `pause` 的 bat 这样非交互验证：`cmd --% /c D:\...\x.bat <nul`
4. **客户端是 32 位。** 编译注入 DLL 必须用 x86 工具链
   （`C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat`）。
5. **不启动 `NMService.exe`；`BsPatcherChn.exe` 已不是 NGM。**
   它现在是**自研更新器**（`updater\src\`）：客户端升级分支拉起它、触发自动更新是
   预期行为。原版 NGM 链（BsPatcherChn→NGMDll→platform.tiancity.com）整条废弃。
6. **GameGuard 绕过必须使用配套构建的 `bsloader.exe` + `bshook.dll`。**
   这条链靠命名事件握手，**两个二进制不能只替换一个**。
7. **改完 `server/` 的代码必须重启服务端进程才生效。**
   V0.1 会话 09 就因为服务端是改代码前启动的旧实例，白踩了一整轮。
8. **`server/` 是单机和云端共用的同一套代码。** 不允许出现「单机分支 / 联机分支」两份实现；
   差异只能体现在**配置**（`server.config`）和**监听地址**上。
9. **明文口令**：`server/data/accounts.json` 按需求就是明文存密码，
   `.gitignore` 已排除它；**日志里也不要打印密码**，打印票据即可。
10. ★ **V0.3 新增：一切判据都要事件驱动，禁止固定次数 / 固定时间的阈值。**
    「跳过头 N 次」「等 XX 毫秒再说」「重试 N 次就当失败」这类常量是拿一台机器上的
    观测值当真理，它**掩盖**竞态而不是消除竞态。bot 的加载完成上报、换图完成上报，
    全部按**收到那一发广播**触发，不许用定时器凑。
    唯一例外是物理上根本等不到事件的地方（重生倒计时、AI 反应延迟），
    这时要在注释里写清「这里为什么等不到事件」。
11. ★ **V0.3 新增：bot 的行为规则只能来自原版，权衡留给 AI**（D50，已经犯过两次）。
    来源只有两个：① 原版代码 / 数据里真实存在的机制（体力、冷却、射程、碰撞组）；
    ② 语料里量得出来的真人打法。两个都没有的**不许自己发明**。
    尤其不许这样推理：「这么做会吃亏 ⇒ 所以不许这么做」—— 吃亏是事实，
    「不许」是**替玩家做的决定**。正确做法是**照做，并把代价如实结算**
    （自伤要掉血），把「值不值得」留给以后真正的 AI 层。
    定规则前先问一句：**真人对局的时候是什么样的？** 答不上来就是凭空造的。
    （已经删掉的两条：D47「保持交战距离」、D50「不往自己爆炸半径开炮」。）

## ★ 启动 / 停止游戏 —— **脚本在项目根目录，agent 自己跑**

用户 2026-08-29：「游戏的启动脚本在根目录下的 `start.bat` / `start-debug.bat`，
结束是 `stop.bat`。」**不用问，要跑就跑。**

| 脚本 | 干什么 |
|---|---|
| `start.bat` | 一键启动：服务端 + 中继 + 客户端。精简日志，正常游玩 / 快速复现用它 |
| `start-debug.bat` | 同上 + 调试日志（客户端密码钩子、服务端逐包 dump）。查 bug 用它 |
| `stop.bat` | 一键停止。按端口的 `OwningProcess` 找进程，**不会误杀别的 python** |

- 用 **PowerShell 的 `Start-Process`** 起，别用 Git Bash —— 这两个 bat 末尾有 `pause`。
  例：`Start-Process -FilePath "…\start-debug.bat" -WorkingDirectory "…"`。
- **改完 `server/` 的代码必须重启服务端才生效**（铁律 7）。
  `start.bat` 会复用已经在跑的服务端，所以要生效就先 `stop.bat`。
- 起完看三个地方：`logs/server.out`（服务端）、`logs/server.err`（该是空的）、
  `logs/bshook_*.log`（客户端探针）。
- ★ **`server.out` 永远是「当前这次运行」**；上一次那份在启动时被改名成
  `server-<那次结束的时刻>.out` 归档，3 天后由 `logcleanup` 自动清掉（D112）。
  `relay.out` / `bsloader.out` 同理，抓包文件名里带本次启动时刻。
  要看上一局就翻归档那份，别以为丢了。
- ★ **日志一律异步写**（D109）：客户端是环形缓冲 + 写盘线程，服务端是
  `server/asynclog.py` 的队列 + 写线程。**别往热路径上加同步 `print` /
  `fh.write`** —— `BotConn.log` / `Conn.log` 都在 `room.sim_lock` 里面，
  一等磁盘整个房间跟着等（§150）。单测里 `asynclog` 没 `start()`，行为和
  同步写逐字一致，所以断言 stdout 的用例照旧能写。
- ★ **弹体逐帧诊断默认跟日志级别走**（D113）：`start.bat` 不装那三个 detour，
  `start-debug.bat` 才装。要在精简模式下查弹体，自己设
  `BSHOOK_PROJ_DIAG=1`（反弹法线是 `BSHOOK_MOVE_DIAG=1`）。

### ⚠ 「启动游戏」≠「操作游戏」

脚本只负责把它跑起来。要 agent **自己点鼠标打一局**，还得 computer-use 授权，
而且有个坑：开始菜单里的「炮炮火枪手」解析到的是**只读原版**
`d:\work\popshot\game_org\popshot\bigshot.exe`，**不是 `game_patched`**。
会话 35 / 36 两次申请都卡在这儿。要让 agent 能实机验，得先有一个指向
`game_patched\bsloader.exe` 的入口。

⇒ 在那之前：**能自己跑的验证（起服务端、看日志、跑测试）自己做完**，
只有「真打一局、看别人屏幕上是什么样」这类才停下来请用户操作。

## 环境速查

- Win10 Pro 19045 x64 / RTX 3070；`d3d9.dll` + `d3dx9_43.dll` 齐全；VC++ 2005/2008 x86 运行库已装
- 开发 Python `C:\Python314\python.exe`（3.14.3）；便携运行时
  `runtime\python\python.exe`（官方 CPython 3.14.3 x64 embeddable，启动脚本只用这一份）
- Win7 兼容运行时 `runtime-win7\python\python.exe`（CPython 3.8.10 win32）
- 7-Zip `C:\SSD\Program\7-Zip\7z.exe`
- MSVC x86 `C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools`（MSVC 14.16）
  和 `...\18\BuildTools`（MSVC 14.50）
- Node `C:\SSD\Program\nodejs\node.exe`，Git `C:\SSD\Program\Git\cmd\git.exe`
- Ghidra + JDK 在 `tools/ghidra` / `tools/jdk`
- **没有** VS IDE、cmake、gcc/clang；pefile 未安装
- ★ **capstone 5.0.7 已装**（`C:\Python314\python.exe`）。想看某个地址的几条指令时，
  不用开 Ghidra —— `re/BigShot_22524.img` 是拉平的内存镜像，
  **文件偏移 = VA − 0x400000**，直接 `Cs(CS_ARCH_X86, CS_MODE_32).disasm(...)` 就行
  （§19 就是这么当场逆出来的）。Pillow 12.1.1 也已装（M4 要用）

## 端口速查

| 端口 | 谁 | 备注 |
|---|---|---|
| `47611` | 认证服 | 客户端写死，不可配置 |
| `27799` | 游戏服 | 客户端写死，不可配置 |
| `27798` | 原版 TCP 中继（rcp） | 服务端在 `0x0210` 里告诉客户端 |
| `27810` | 注册网页 | 可在 `server.config` 里改 |
| `27800` | 调试控制通道 | **只绑 `127.0.0.1`**；服务端包里默认关 |
| `47621` / `27809` / `27808` | 本地中继（联机模式） | 只在客户端包里，`127.0.0.1` |

监听地址固定 `::`（IPv6 双栈，IPv4 也能连），**不做成配置项**。

---

## V0.3 完工后

本版的 `CLAUDE.md` 和 `.claude/` **本来就已经在** `develop_history/V0.3_bot 功能开发/`
里（用户 2026-08-25 的决定：不放项目根），所以完工时不用搬家，
只要把项目根那份 4 行的指针 `CLAUDE.md` 改指向下一个版本即可。
