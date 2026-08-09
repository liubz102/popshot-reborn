# 炮炮火枪手 · 原版客户端复活工程

复活 2007 年的《炮炮火枪手》（NEXON《Big Shot: Kaska Tournament》，世纪天成代理，2009 停服）：
**修好原版客户端让它在 Win10 上启动 + 做一套跑在 localhost 的假服务端**，
目标是能玩单机内容（闯关模式、训练场）。不做联网对战。

这是一个**跨多会话、全程由 AI 实施的长工程**。任何一个新会话都可能是接手方。

---

## 🔴 开工前必读（每个会话第一件事）

按顺序读这三个文件，**不要重新推导已知结论**：

1. `.claude/PROGRESS.md` —— 现在在哪、正在做什么、下一步、当前卡点
2. `.claude/FINDINGS.md` —— 已经查明的硬事实（地址、偏移、opcode、算法、命令），
   以及**已经试过但不行的路子**（别重复踩坑）
3. `.claude/DECISIONS.md` —— 为什么这么选，别无意义地推翻已定方案

总计划在 `.claude/PLAN.md`。

## 🔴 收工规则

- **每完成一个可验证小步就立刻更新 `.claude/PROGRESS.md`**，不要攒到会话结束
  —— 上下文随时可能被打断，攒着就丢了。
- 新查明的硬事实立刻追加进 `.claude/FINDINGS.md`（**失败的尝试也要记**）。
- 做了方案选择就记进 `.claude/DECISIONS.md`（记「为什么」，不记「是什么」）。
- 会话结束时在 `.claude/sessions/YYYY-MM-DD-NN.md` 写一份日志：做了什么、结果、遗留。
- `FINDINGS.md` 记「是什么」，`DECISIONS.md` 记「为什么」，`PROGRESS.md` 只保留当前状态
  （不做流水账，流水账进 `sessions/`）。

---

## 目录

| 路径 | 用途 |
|---|---|
| `game_org/` | **原始安装结果，只读，永不修改** |
| `原版安装包/` | **原始安装包（368MB NSIS），只读** |
| `game_patched/` | 工作副本 —— 客户端实际跑这个，所有改动只碰它 |
| `Pack_decrypt/` | **解开的 `Pack\*.pkn` 资源树，只读参考**（`Data/` 的 ini、`Maps/`、`Models/`、`Images/`）。查「客户端到底有哪些内容、被什么条件挡住」先翻它 —— 会话 21 的角色/关卡解锁就是靠 `Data/map.ini` 的 `OpenLocale` 和 `Data/ChrProps.ini` 定案的。**没有回写工具，改资源只能靠运行时 patch（D005/D057）** |
| `tools/` | 便携逆向工具：x64dbg / Scylla / Ghidra / Sysinternals |
| `re/` | 逆向产物：`BigShot_dump.exe`、Ghidra 工程、`protocol.md` |
| `hook/` | MSVC x86 工程：`bshook.dll`（注入）+ `bsloader.exe`（启动器） |
| `server/` | Python 假服务端 |
| `logs/` | 抓包 / 调试日志 |

## 铁律

1. **`game_org/` 和 `原版安装包/` 只读。** 想改什么都去 `game_patched/`。
   原始件毁了就再也找不回来了（客户端本身是从已停运 15 年的游戏里抢救出来的，
   见 `.claude/FINDINGS.md` 的溯源记录）。
2. **不要让 2007 年的 nProtect GameGuard 真的跑起来。** 它会尝试装内核驱动。
   `game_patched/GameGuard.des` 必须保持改名状态。
3. **`.bat` / `.cmd` 一律 CRLF + UTF-8 无 BOM。**
   LF 结尾的 .bat 在中文 Win10 + `chcp 65001` 下会被拦腰截断（报 `'ho' 不是内部或外部命令`）。
   Write 工具默认写 LF，写完必须转换：
   ```powershell
   $p='路径.bat'; $c=Get-Content -Raw -Encoding UTF8 $p
   $c=$c -replace "`r`n","`n" -replace "`n","`r`n"
   [System.IO.File]::WriteAllText($p,$c,(New-Object System.Text.UTF8Encoding($false)))
   ```
   含 `pause` 的 bat 在 PowerShell 工具里这样非交互验证（`--%` 让 PowerShell
   别去解析 `<`）：`cmd --% /c D:\work\popshot\x.bat <nul`
   `.py` / `.sh` / `.mjs` 相反，保持 LF。
3b. **`.ps1` 一律 CRLF + UTF-8 **有** BOM** —— 和上面的 `.bat` **正好相反**，别记混。
   无 BOM 的 `.ps1` 会被 PowerShell 5.1 按 CP936 读，中文注释乱码后混进引号，
   直接变成 `The string is missing the terminator` 语法错误（FINDINGS §106）。
   ```powershell
   $p='路径.ps1'; $c=[System.IO.File]::ReadAllText($p,(New-Object System.Text.UTF8Encoding($false)))
   $c=$c -replace "`r`n","`n" -replace "`n","`r`n"
   [System.IO.File]::WriteAllText($p,$c,(New-Object System.Text.UTF8Encoding($true)))
   ```
   验证：`[System.IO.File]::ReadAllBytes($p)[0..2]` 应是 `239,187,191`。
4. **客户端是 32 位。** 编译注入 DLL 必须用 x86 工具链
   （`C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat`）。
5. **不启动 `NMService.exe` / `BsPatcherChn.exe`。** 那是 Nexon 通行证和补丁器，
   连的是早已停机的 `platform.tiancity.com`，游戏本体不需要。
6. **GameGuard 绕过必须使用配套构建的 `bsloader.exe` + `bshook.dll`。**
   会话 23 起不再于 +2.5 秒改 `0x54b0fc`：DLL 注册 VEH，等主线程退出
   `LoadLibrary` APC 后给它设置 DR0；执行到 `0x54b0fc` 时直接令
   `EAX=0x755 / EIP+=5`。这条链靠命名事件握手，两个二进制不能只替换一个。
   +2.5 秒门槛仍用于地区锁、挂机计时器等**其它**游戏代码 patch（D060 / §121）。

## 环境速查

- Win10 Pro 19045 x64 / RTX 3070；`d3d9.dll` + `d3dx9_43.dll` 齐全；VC++ 2005/2008 x86 运行库已装
- 开发 Python `C:\Python314\python.exe`（3.14.3）；便携运行时
  `runtime\python\python.exe`（官方 CPython 3.14.3 x64 embeddable，启动脚本只用这一份）
- 7-Zip `C:\SSD\Program\7-Zip\7z.exe`
- MSVC x86 `C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools`（MSVC 14.16）
  和 `...\18\BuildTools`（MSVC 14.50）
- Node `C:\SSD\Program\nodejs\node.exe`，Git `C:\SSD\Program\Git\cmd\git.exe`
- **没有** VS IDE、cmake、gcc/clang；pefile / capstone 未安装
