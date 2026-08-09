# PLAN — 总计划（6 个阶段）

> 当前进度看 `PROGRESS.md`。这份文件只描述**要做什么、怎么验收**，不记录状态。

**总目标**：修好 2007 年的《炮炮火枪手》客户端让它在 Win10 上启动，
再做一套跑在 localhost 的 Python 假服务端，让客户端连上后能玩
**闯关模式**和**训练场**等单机内容。不做联网对战。

**关键前提**（见 `FINDINGS.md` §7）：用户报的是 GameGuard 错误框，
说明 ASProtect 壳在 Win10 上能正常解开、游戏代码已在跑。启动的拦路虎只有反作弊。

---

## 阶段 0 — 追踪体系 + 工作副本 + 工具

| # | 任务 | 验收 |
|---|---|---|
| 0.1 | 建 `CLAUDE.md` + `.claude/{PLAN,PROGRESS,FINDINGS,DECISIONS}.md` + `sessions/` | 新会话只读 PROGRESS+FINDINGS 就能说清"现在在哪、下一步做什么" |
| 0.2 | `game_org\Popshot` → `game_patched\`；`GameGuard.des` 改名 `.bak` | `game_patched` 154 文件；`GameGuard.des` 不存在 |
| 0.3 | 便携工具下载到 `tools\`：x64dbg(+Scylla)、Ghidra+JDK17、Sysinternals、Wireshark+Npcap | 各工具能启动 |
| 0.4 | `hook\` MSVC x86 工程骨架（`vcvars32.bat` + `build.bat`） | 能编出 x86 的 `bshook.dll` + `bsloader.exe`，注入记事本能弹出证明 |

## 阶段 1 — 脱壳 `BigShot.exe`

因为 GameGuard 报错框能弹出，说明壳已完全解开、游戏代码在跑 →
**不硬啃 OEP，直接趁错误框挂着时 attach 内存转储**。

1. 跑 `game_patched\BigShot.exe` → 停在 GameGuard 错误框（进程还活着）
2. x32dbg attach → Scylla：`IAT Autosearch` → `Get Imports` → `Dump` + `Fix Dump`
   → `re\BigShot_dump.exe`（**只求 Ghidra 能分析，不要求能运行**）
3. 剔除指向壳段的无效 thunk（ASProtect 的 API 重定向）
4. Ghidra 导入 + auto-analysis，作为后续所有工作的分析底座

**兜底**：若 attach dump 质量差 → x32dbg「最后一次异常法」跟 OEP
（先在设置里忽略所有常见异常，ASProtect 大量用 SEH 反调试）。
VC++ OEP 特征：`push ebp; mov ebp,esp` 或 `push -1 / push <SEH> / mov eax,fs:[0]`。

**止损**：dump 差到 Ghidra 认不出函数 → 放弃脱壳，纯靠 x32dbg 动态调试 + 运行时 hook
逐步逆向（慢，但阶段 2/3 照样能做）。

**验收**：`re\BigShot_dump.exe` 在 Ghidra 里能识别出上千个函数、能看到可读字符串。

## 阶段 2 — 干掉 GameGuard，进登录界面

1. Ghidra 搜 `GameGuard` / `npgl` / `npgg` / `.erl` / `GameMon` / `NPGameLib` 的交叉引用
   → 定位 nProtect 初始化函数与错误分支
2. `hook\bshook.dll`（x86）注册 VEH，捕获主线程在 `0x54b0fc` 的 DR0 执行断点：
   校验真正执行时令 `EAX=0x755 / EIP+=5`，跳过状态取值调用，**不改代码字节**。
3. `hook\bsloader.exe`：`CreateProcess(CREATE_SUSPENDED)` → APC 注入 → `ResumeThread`。
   DLL 内部线程按“主线程已离开 LoadLibrary APC”这个状态设置 DR0，再用命名事件
   向启动器回报已武装；不再用固定 +2.5 秒猜 GameGuard 的安全窗口。
4. 按实测出现的顺序处理其它障碍（**不要预先全做**）：
   - 老 `usp10.dll`(2004) / `dbghelp.dll`(2002) / `unicows.dll` 干扰加载 → 移走
   - `nmcogame.dll` 卡通行证 → 同名 stub DLL 顶替（7 个 `NMCO_*` 返回良性值）
   - `SeData.dll` 同理（`AnInitSet/ImpressAC/protectLoad/protectUnload/DllVersion`）
   - D3D9 独占全屏/分辨率 → 兼容模式(XP SP3)、窗口化、必要时 dgVoodoo2 / DXVK-d3d9

**验收**：双击 `bsloader.exe` → NEXON logo → 登录/主界面，**不再弹 GameGuard 错误框**
（此时必然卡在连不上服务器，正常）。

## 阶段 3 — 网络重定向 + 抓包框架

`bshook.dll` 加网络 hook（MinHook 或手写 inline hook，不引重依赖）：

1. hook `gethostbyname` / `getaddrinfo` / `connect` / `WSAConnect`
   → **目标地址一律改写 `127.0.0.1`，端口不变**，原始目标记日志
   （这样不必先破 `BigShotCN.ini` 的加密就能知道原服务器 IP:Port）
2. hook `send` / `recv` / `WSASend` / `WSARecv` / `closesocket`
   → 按连接落盘 `logs\conn_<seq>_<port>.bin`（原始流）+ `.txt`（带时间戳 hexdump）
3. hook `CreateFileA/W` + `ReadFile` → 看它读哪些 `Pack\*.pkn`，
   顺带定位 pkn/ini 解密函数入口（为可选的资源解包器铺路）
4. Wireshark+Npcap 抓回环做交叉验证（hook 日志为主，抓包为辅）

**验收**：`logs\conn_*.txt` 有内容；`netstat -ano` 看得到客户端连 `127.0.0.1:<port>`。

## 阶段 4 — 协议逆向

1. Ghidra 从 `recv`/`send` 的调用者往上：socket 封装类 → 拆包缓冲
   → **包头结构**（大概率 `[len][opcode]`）→ **分发表/opcode 枚举** → 包体序列化
2. 判定有无加密。若有（常见：握手下发种子的 XOR/流密码）→
   **在加解密函数出入口 hook 直接抓明文**，这样算法没逆完也能先跑通
3. 产出 `re\protocol.md` + `server\packets.py`，opcode 表同步进 `FINDINGS.md`

**止损**：包加密与 GameGuard 强耦合（密钥来自 GameGuard 模块）→
保留密钥派生路径只短路检测，或直接在明文 hook 点旁路。

**验收**：`protocol.md` 能解释 logs 里 ≥90% 的字节；`packets.py` 对 log 往返编解码一致。

## 阶段 5 — Python 假服务端

```
server/
├─ main.py       多端口监听（Auth / Lobby / Game）+ 统一日志
├─ packets.py    包头 + 各 opcode 的 struct 编解码
├─ crypto.py     握手/加密（若存在）
├─ session.py    每连接状态机
├─ handlers/     auth / lobby / room / game
├─ data/         角色、道具、地图、关卡静态表
└─ save.json     本地存档（等级/金币/道具）
```
Python 3.14 + asyncio，优先零外部依赖。

四个里程碑，每个都以**客户端界面上看得见的变化**为准，达成即更新 `PROGRESS.md`：

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **A** | 版本校验 + 登录 + 角色列表 | 客户端不再报连接/登录失败 |
| **B** | 频道列表、角色数据、房间列表 | 看到原版大厅 UI |
| **C** | 建房 → 选模式(训练场/闯关)+地图 → 准备 → 开始 | 加载进战斗场景 |
| **D** | 战斗内包应答、通关/失败结算 | 打完一关，经验金币写入 `save.json` |

**关键风险**：若闯关的怪物配置/剧本由服务端下发而非藏在 `Maps*.pkn` 里，
这部分内容需按原版录像重建。**到里程碑 C 就能判明**，届时单独同步决策。

## 阶段 6 — 打包收尾

- `start.bat`：用项目内 `runtime\python\python.exe` 起服务端 → 起 `bsloader.exe`
  （目标电脑不需要安装 Python）；`stop.bat` 收尾
- `tools\build-portable.ps1`：只收集运行必需文件，排除日志、崩溃转储和开发工具，
  可选生成 ZIP / 携带现有存档
- `README.md`：启动方式、迁移存档、故障排查和安全注意事项
- 可选：`tools\pkn_extract.py` 用阶段 3 拿到的解密逻辑解包 `Pack\*.pkn`，
  导出原版模型/贴图/地图

**验收**：在没有安装 Python / VC++ 附加运行库的干净 Win10/11 x64 上解压便携 ZIP，
双击 `start.bat`，全程无手工步骤即可进游戏打完一关并持久化存档。

---

## 工期与风险的诚实交底

- 阶段 0–3（启动 + 重定向到 localhost）：**可控的工程问题，天级到周级**
- 阶段 4–6（逆协议 + 写服务端）：**以周计的开放式工作**，有真实失败风险
  （包体可能加密、闯关关卡数据可能在服务端）
- 每个阶段都写了止损点和退路。每个里程碑结束如实汇报，不粉饰。
