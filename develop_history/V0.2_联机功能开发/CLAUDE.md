# 炮炮火枪手 · V0.2 联机功能开发

复活 2007 年的《炮炮火枪手》（NEXON《Big Shot: Kaska Tournament》，世纪天成代理，2009 停服）。

**V0.1（基础单机）已完工并归档**：客户端能在 Win10 上启动，localhost 假服务端撑起了
登录 → 大厅 → 建房 → 闯关 → 结算 → 回房间的完整单机闭环。

**V0.2 要做的事**：把假服务端变成**真正的多人服务端** —— 多账号、网页注册、
局域网/云端联机对战与合作闯关，同时**保留单机游玩**（单机连本机假服务器，联机连云服务器，
两者共用同一套服务端代码）。

这是一个**跨多会话、多 agent、全程由 AI 实施的长工程**。任何一个新会话都可能是接手方。

---

## 🔴 开工前必读（每个会话第一件事）

按顺序读这三个文件，**不要重新推导已知结论**：

1. `.claude/PROGRESS.md` —— 现在在哪、正在做什么、下一步、当前卡点、**⏳ 待用户验证**
2. `.claude/FINDINGS.md` —— 已经查明的硬事实，以及**已经试过但不行的路子**（别重复踩坑）
3. `.claude/DECISIONS.md` —— 为什么这么选，别无意义地推翻已定方案

总计划在 `.claude/PLAN.md`。

### ★ V0.1 的结论在哪（**只读参考，不要重新推导**）

```text
develop_history/V0.1_基础单机功能开发/CLAUDE.md
develop_history/V0.1_基础单机功能开发/.claude/PLAN.md
develop_history/V0.1_基础单机功能开发/.claude/PROGRESS.md      ← 单机侧的完整现状
develop_history/V0.1_基础单机功能开发/.claude/FINDINGS.md      ← §1~§121，5517 行，协议真源
develop_history/V0.1_基础单机功能开发/.claude/DECISIONS.md     ← D001~D060
develop_history/V0.1_基础单机功能开发/.claude/sessions/        ← 会话 01~23 的日志和截图
```

**引用方式**：写「V0.1 §119」「V0.1 D057」，指的就是上面那两个文件里的条目。
`.claude/FINDINGS.md` 开头有一张 **V0.1 关键结论速查表**，先看它再决定要不要翻原文。

### ★ 编号规则

| 东西 | 规则 |
|---|---|
| `FINDINGS.md` 的 §号 | **接着 V0.1 往下排，从 §122 起**（全局唯一，跨版本引用不会歧义） |
| `DECISIONS.md` 的 D号 | **接着 V0.1 往下排，从 D061 起**（同上） |
| `sessions/` 的会话号 | **V0.2 重新从 01 起**，文件名 `YYYY-MM-DD-NN.md`（和 V0.1 相互独立） |

## 🔴 收工规则

- **每完成一个可验证小步就立刻更新 `.claude/PROGRESS.md`**，不要攒到会话结束
  —— 上下文随时可能被打断，攒着就丢了。
- 新查明的硬事实立刻追加进 `.claude/FINDINGS.md`（**失败的尝试也要记**）。
- 做了方案选择就记进 `.claude/DECISIONS.md`（记「为什么」，不记「是什么」）。
- 会话结束时在 `.claude/sessions/YYYY-MM-DD-NN.md` 写一份日志：做了什么、结果、遗留。
- `FINDINGS.md` 记「是什么」，`DECISIONS.md` 记「为什么」，`PROGRESS.md` 只保留当前状态
  （不做流水账，流水账进 `sessions/`）。

## 🔴 ★ 需要用户实机操作时，停下来问用户

有些验证 agent 自己做不了 —— 真通关一局、**局域网另一台电脑上登录**、
**两个账号同时进同一个房间**、在手机/别的浏览器上打开注册页、把服务端包丢到云主机上跑。

**做法**：

1. 一次把话说全：**要做什么操作 / 期望看到什么 / 怎么把结果告诉我**
   （截图路径？日志片段？还是一句「行」「不行」）。
2. 把这条登记进 `PROGRESS.md` 的 **「⏳ 待用户验证」** 区，写清「怎么做 / 期望结果 / 当前状态」。
3. **然后停下来等**，不要假装验证过了，也不要用「大概能行」代替实测结论。
4. 在等的同时，把**不依赖这条结论**的活儿继续做完。
5. 用户回话后**立刻**更新 `PROGRESS.md`，结论进 `FINDINGS.md`。

同一个道理反过来也成立：**没实机跑过的就明说没跑过**。V0.1 的教训是
「单元测试全绿 ≠ 客户端认」，客户端只认字节和时序。

---

## 目录

| 路径 | 用途 |
|---|---|
| `game_org/` | **原始安装结果，只读，永不修改** |
| `原版安装包/` | **原始安装包（368MB NSIS），只读** |
| `game_patched/` | 工作副本 —— 客户端实际跑这个，所有改动只碰它 |
| `Pack_decrypt/` | **解开的 `Pack\*.pkn` 资源树，只读参考**（`Data/` 的 ini、`Maps/`、`Models/`、`Images/`）。没有回写工具，改资源只能靠运行时 patch |
| `tools/` | 便携逆向工具（x64dbg / Scylla / Ghidra / Sysinternals）+ 自写探针、启停脚本、打包脚本 |
| `re/` | 逆向产物：`BigShot_22524.exe`、`packets.txt`、`rtti_types.txt`、`vftables.json` |
| `hook/` | MSVC x86 工程：`bshook.dll`（注入）+ `bsloader.exe`（启动器） |
| `server/` | Python 服务端（**单机假服务器和云端服务端是同一套代码**） |
| `runtime/python/` | 内置 CPython 3.14.3 x64 embeddable（目标机不需要装 Python） |
| `runtime-win7/python/` | **Win7 兼容运行时** CPython 3.8.10 win32 + app-local UCRT。**只进客户端包**（让个别 Win7 玩家能启动游戏；服务端包不带，架服务端不考虑老系统），Win10 以下由 `launch.ps1` 自动启用（§215 / D133）。★ 改服务端代码后顺手 `runtime-win7\python\python.exe server\run_tests.py` 跑一遍，别把 3.8 兼容性弄丢 |
| `runtime-linux/` | 服务端包发给 Linux 的那份 CPython 3.14.7，**故意不解压**（D088/D090）。打包时直接 copy，不联网 |
| `logs/` | 抓包 / 调试日志 |
| `dist/` | 打包产物（**客户端包 + 服务端包，两个**） |
| `develop_history/` | 历史版本的进度文档（只读） |

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
   | `.sh` / `.py` / `.mjs` / `.json` | **LF** | 无 BOM | **服务端包要在 Linux 上跑，`.sh` 带 CR 直接 `bad interpreter: /bin/sh^M`** |

   Write 工具默认写 LF。写完 `.bat` 后转 CRLF：
   ```powershell
   $p='路径.bat'; $c=Get-Content -Raw -Encoding UTF8 $p
   $c=$c -replace "`r`n","`n" -replace "`n","`r`n"
   [System.IO.File]::WriteAllText($p,$c,(New-Object System.Text.UTF8Encoding($false)))
   ```
   `.ps1` 同样转 CRLF，但最后一个参数换成 `UTF8Encoding($true)`（带 BOM）。
   验证：`.bat` 用 `file x.bat` 应显示「with CRLF line terminators」；
   `.ps1` 用 `[System.IO.File]::ReadAllBytes($p)[0..2]` 应是 `239,187,191`。
   含 `pause` 的 bat 这样非交互验证：`cmd --% /c D:\work\popshot\x.bat <nul`
4. **客户端是 32 位。** 编译注入 DLL 必须用 x86 工具链
   （`C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat`）。
5. **不启动 `NMService.exe` / `BsPatcherChn.exe`。** 那是 Nexon 通行证和补丁器，
   连的是早已停机的 `platform.tiancity.com`，游戏本体不需要。
6. **GameGuard 绕过必须使用配套构建的 `bsloader.exe` + `bshook.dll`。**
   DLL 注册 VEH，等主线程退出 `LoadLibrary` APC 后给它设置 DR0；执行到 `0x54b0fc`
   时令 `EAX=0x755 / EIP+=5`。这条链靠命名事件握手，**两个二进制不能只替换一个**。
7. **改完 `server/` 的代码必须重启服务端进程才生效。** V0.1 会话 09 就因为服务端是
   改代码前启动的旧实例，白踩了一整轮。
8. **`server/` 是单机和云端共用的同一套代码。** 不允许出现「单机分支 / 联机分支」两份实现；
   差异只能体现在**配置**（`server.config`）和**监听地址**上。
9. **明文口令**：`server/data/accounts.json` 按需求就是明文存密码，
   `.gitignore` 已排除它；**日志里也不要打印密码**，打印票据即可。

## 环境速查

- Win10 Pro 19045 x64 / RTX 3070；`d3d9.dll` + `d3dx9_43.dll` 齐全；VC++ 2005/2008 x86 运行库已装
- 开发 Python `C:\Python314\python.exe`（3.14.3）；便携运行时
  `runtime\python\python.exe`（官方 CPython 3.14.3 x64 embeddable，启动脚本只用这一份）
- 7-Zip `C:\SSD\Program\7-Zip\7z.exe`
- MSVC x86 `C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools`（MSVC 14.16）
  和 `...\18\BuildTools`（MSVC 14.50）
- Node `C:\SSD\Program\nodejs\node.exe`，Git `C:\SSD\Program\Git\cmd\git.exe`
- **没有** VS IDE、cmake、gcc/clang；pefile / capstone 未安装

## 端口速查（V0.2）

| 端口 | 谁 | 备注 |
|---|---|---|
| `47611` | 认证服 | 客户端写死，不可配置 |
| `27799` | 游戏服 | 客户端写死，不可配置 |
| `27810` | 注册网页 | 可在 `server.config` 里改 |
| `27800` | 调试控制通道 | **只绑 `127.0.0.1`**；服务端包里默认关 |
| `47621` / `27809` | 本地中继（联机模式） | 只在客户端包里，`127.0.0.1` |

监听地址固定 `::`（IPv6 双栈，IPv4 也能连），**不做成配置项**。

---

## V0.2 完工后

把本 `CLAUDE.md` 和 `.claude/` 移进 `develop_history/V0.2_联机功能开发/`，
再为下一个版本建一套新的。
