# FINDINGS — 已查明的硬事实

> 只记「**是什么**」：地址、偏移、字节序列、opcode、算法、能直接复制粘贴的命令。
> 「为什么这么选」记在 `DECISIONS.md`。
> **试过但不行的路子也要记在这里**（见文末「死路清单」），别让下个会话重复踩坑。

最后更新：2026-08-08（会话 19，最新一节是 §117）

---

## 1. 客户端来源与完整性

- 安装包：`原版安装包\PopShot_Setup_311.exe`
  - **NSIS 2.29**（`Nullsoft.NSIS.exehead`），368,413,903 字节，**156 个文件**
  - 列内容：`& "C:\SSD\Program\7-Zip\7z.exe" l "D:\work\popshot\原版安装包\PopShot_Setup_311.exe"`
  - 7-Zip 能直接解（`Type = Nsis`, `SubType = NSIS-2`, Deflate）
- `game_org\Popshot\` = 安装后结果，**154 个文件 / 366.84 MB**，与安装包内容一致
- 版本线索：`.url` 指向 `http://popshot.tiancity.com`（世纪天成国服）
- 资源语言 ID = **1042（韩语 ko-KR）** —— 韩国原版的残留

## 2. PE 文件全景（`game_org\Popshot\`）

| 文件 | 大小 | 编译时间(UTC) | 关键点 |
|---|---|---|---|
| `BigShot.exe` | 1,612,800 | 2007-10-05 18:20:30 | **游戏本体，ASProtect 2.x 加壳** |
| `NMService.exe` | 1,458,176 | 2007-10-05 11:31:04 | Nexon Messenger 服务，未加壳。**不需要** |
| `BsPatcherChn.exe` | 110,592 | 2007-06-28 09:22:46 | NGM 补丁器。**不需要** |
| `GameGuard.des` | 165,569 | 2007-06-19 09:09:16 | **nProtect GameGuard，加壳 EXE（非 DLL）** |
| `nmcogame.dll` | 241,664 | 2007-10-05 11:22:06 | **被 BigShot.exe 静态导入**，Nexon 通行证游戏侧 |
| `nmconew.dll` | 1,040,384 | 2007-10-05 11:18:05 | NMService 用，游戏本体不用 |
| `NGMDll.dll` | 499,712 | 2007-09-28 07:26:36 | Nexon Game Manager，导出 `NGMMain`/`NGMSetupMain` |
| `NGMResource.dll` | 352,256 | 2007-08-13 10:06:49 | 纯资源 DLL |
| `SeData.dll` | 118,784 | 2007-05-21 10:57:00 | 第二套反作弊，**非静态导入**（动态 LoadLibrary） |
| `DevIL.dll` | 864,256 | 2004-05-27 | 图像库，122 个导出，**被静态导入** |
| `fmod.dll` | 161,280 | 2004-10-18 | 音频，**UPX 加壳**，230 导出，**被静态导入** |
| `dbghelp.dll` | 489,984 | **2002-08-29** | 微软老版，只用 `MiniDumpWriteDump` |
| `usp10.dll` | 406,528 | **2004-08-04** | Uniscribe 老版，**Win10 上可疑** |
| `unicows.dll` | 258,352 | 2004-12-07 | Win9x Unicode 层，507 导出，Win10 上无用 |
| `uninst.exe` | 60,562 | 2007-07-14 | NSIS 卸载器 |

### 2.1 `BigShot.exe` — ASProtect 2.x

```
ImageBase       0x00400000      EntryPoint RVA  0x00001000
Subsystem       2 (GUI)         PE hdr offset   0x138
节表（前 4 个节名为空 = ASProtect 特征）:
  <blank>  VA=00001000 VS=00236000 RAW=00000400 RSZ=000CF800  entropy 8.00
  <blank>  VA=00237000 VS=00092000 RAW=000CFC00 RSZ=00027400  entropy 8.00
  <blank>  VA=002C9000 VS=00071000 RAW=000F7000 RSZ=00008800  entropy 7.99
  <blank>  VA=0033A000 VS=00001000 RAW=000FF800 RSZ=00001000  entropy 0.00
  .rsrc    VA=0033B000 VS=00077000 RAW=00100800 RSZ=00077000  entropy 5.08
  .data    VA=003B2000 VS=00013000 RAW=00177800 RSZ=00012200  entropy 7.79
  .adata   VA=003C5000 VS=00002000 RAW=00189A00 RSZ=00000200  entropy 1.35   ← ASProtect 标志节
数据目录: IMPORT=0x3B2A38(0x3D4)  RESOURCE=0x33B000  TLS=0x3B29CC  DEBUG=0x237540
入口字节 (文件偏移 0x400):
  68 01 20 7B 00     push 0x7B2001
  E8 01 00 00 00     call $+5
  C3                 ret
  C3                 ret            ← ASProtect 2.x 经典壳入口
```

导入表（被壳精简，每 DLL 只留 1 个函数）：
`kernel32(GetProcAddress/GetModuleHandleA/LoadLibraryA)`, `advapi32`, `user32`, `gdi32`,
`shell32`, `winmm`, `iphlpapi(GetAdaptersInfo)`, `ole32`, `oleaut32`, **`fmod.dll`**,
**`devil.dll`**, **`ws2_32.dll`**, `shlwapi`, **`nmcogame.dll(NMCO_MemoryFree)`**,
`dbghelp(MiniDumpWriteDump)`, `imm32`, **`d3d9.dll(Direct3DCreate9)`**, `oleaut32`

**含义**：代码/字符串/协议全被加密（熵 8.0），**静态改不动**，必须运行时处理。
渲染是 **D3D9**。`iphlpapi!GetAdaptersInfo` = 取网卡 MAC 做机器码。

资源很少（都在 `.rsrc`，语言 1042）：BITMAP×2、ICON×3、DIALOG×2（其中一个仅 64 字节）、
GROUP_ICON×2。**没有版本信息、没有清单、没有可用字符串表** —— 别指望从资源里挖东西。

### 2.2 `GameGuard.des` — 是 EXE 不是 DLL

```
Characteristics 0x010F  → IMAGE_FILE_DLL 位没置，是可执行程序
节表:
  .text   VA=00001000 VS=00034000 RAW=00000400 RSZ=00000000   ← 原始大小 0，运行时自解
  .rdata  VA=00035000 VS=00015000 RAW=00000400 RSZ=00014200
  .rsrc   VA=0004A000 VS=00001000 RAW=00014600 RSZ=00000E00
无导出表。导入极少:
  KERNEL32: LoadLibraryA, GetProcAddress, ExitProcess
  ADVAPI32: RegCloseKey   GDI32: GetDeviceCaps   USER32: GetDC
  ole32: CoCreateInstance   OLEAUT32/COMCTL32: (序号导入)
内部含明文字符串 "GameGuard.des"
```

配套：`GameGuard\GameGuard.ver` = `Tue Jun 19 17:55:51 2007\0`（明文），
`GameGuard\npgg.erl`(19,260B) / `npgl.erl`(23,842B) = **加密**的错误消息资源。

### 2.3 `nmcogame.dll` 导出（写 stub 时要全部实现）

```
NMCO_CallNMFunc, NMCO_MemoryFree, NMCO_SetLocale, NMCO_SetPatchOption,
NMCO_SetUseFriendModuleOption, NMCO_SetUseNGMOption, NMCO_SetVersionFileUrl
```
只有 `NMCO_MemoryFree` 被 `BigShot.exe` 静态导入，其余靠 GetProcAddress。

### 2.4 `SeData.dll` 导出

```
AnInitSet, DllVersion, ImpressAC, protectLoad, protectUnload
```
版本串 `1.1.0.1`。MFC 程序（含 `CNotSupportedException`）。**非静态导入**。

## 3. 已死的服务器地址（全部无用，仅供识别）

从 `NMService.exe` / `nmcogame.dll` / `BsPatcherChn.exe` 提取：

```
http://platform.tiancity.com/NGM/Bin/NGMDll.dll      ← 国服 NGM
http://ngm.nexon.net/ngm/Messenger/version.xml
http://platform.nexon.com/Messenger/version.xml
http://webdown.nexon.co.jp/NexonJapan/Auth/...
IP: 63.251.217.190 / 222.73.209.45 / 210.51.40.237 / 218.153.7.115
    203.141.248.1 / 218.145.45.51 / 218.145.45.11
注册表: SOFTWARE\Nexon\NGM,  Software\Nexon\NexonPlug
配置文件: %s\newserver.cfg, %s\chnserver.cfg
```

**注意**：这些都是 NGM/信使的地址，**不是游戏服务器地址**。
游戏服务器地址在加密的 `BigShotCN.ini` 里（见下），或由启动参数传入。

## 4. 加密容器格式（`BigShotCN.ini` 与 `Pack\*.pkn` 同一套）

`BigShotCN.ini` 共 318 (0x13E) 字节，是唯一一个**小到能看清结构**的样本：

```
0x0000..0x00DF  (224B)  密文
0x00E0..0x00ED  (14B)   明文 ASCII "BigShotCN.ini\0"      ← 文件名不加密
0x00EE..0x012F  (66B)   密文
0x0130..0x013D  (14B)   尾部:
     81 32                       ← 疑似前一字段结尾
     0E 00 00 00                 ← 14 = strlen("BigShotCN.ini")+1
     40 00 00 00                 ← 64 = ？（原始数据大小或偏移）
     21 26 81 32                 ← 魔数 0x32812621
```

- **推论**：真正的 ini 正文可能只有 64 字节，形如 `[Server] IP=... Port=...`
- `Pack\*.pkn`（6 组共 87 个分卷，~350MB）：**从第 0 字节起就是高熵密文，尾部无明文索引、无魔数**
  —— 与 ini 的布局不同，索引可能在 `Data0000.pkn` 里，或用哈希代替文件名
- 分卷规律：每组按 ~4MB 切片
  `Data0000` / `Effects0000-0011` / `Images0000-0017` / `Maps0000-0023` /
  `Models0000-0021` / `Sounds0000-0004`
- **密钥在 ASProtect 壳里** → 想解包必须先脱壳或运行时 hook 解密函数

## 5. 明文资源（唯一不加密的游戏内容）

`game_org\Popshot\Sounds\` 共 49 个 **OGG**，文件名直接印证了单机内容的存在：

```
BGM-Game-Tutorial.ogg / Tutorial01 / Tutorial02 / Tutorial-Intro   ← 训练场
BGM-Game-Quest02-Esperan / Quest03-boss / Quest03-Esperan / Quest03-Forest
BGM-Game-Quest04-boss(+Intro) / Quest05-boss(+Intro) / Quest05-Stage(+Intro)
BGM-Game-Quest06-boss / Quest06-stage / Quest07-boss / Quest07-intro / Quest07-out
                                                                    ← 闯关模式 7 关 + Boss
BGM-ChallengeMissionBgm / BGM-Game-TimeAttack-OneMinute             ← 挑战/计时
地图 BGM: camel00 / Desert00 / Domir00 / Esperan00 / Forest00 / Garden00 /
          Iceria00 / Megatron00 / festival
其它: Lobby / Room / Logo / NexonLogo / Loading / shop / Victory / Lose /
      StageClear / ClearResult / Failed / Boss / Boss01 / Boss-Intro / Boss-Outro
```

## 6. 本机环境（已核实）

```
OS      Windows 10 专业版 19045 x64
GPU     NVIDIA GeForce RTX 3070 Laptop（另有 GameViewer / Meta 虚拟显示器适配器）
D3D     C:\Windows\SysWOW64\d3d9.dll 有, d3dx9_43.dll 有
运行库  VC++ 2005 / 2008(9.0.30729) / 2010 / 2012 / 2013 / 2022  x86+x64 均已装
Python  C:\Python314\python.exe (3.14)   ※ pefile / capstone 未装
7-Zip   C:\SSD\Program\7-Zip\7z.exe (19.00)
Node    C:\SSD\Program\nodejs\node.exe
Git     C:\SSD\Program\Git\cmd\git.exe
MSVC    C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools  MSVC 14.16.27023
        C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools    MSVC 14.50.35717
        两者 Hostx64\x86 与 Hostx86\x86 的 cl.exe 都在，vcvars32.bat 都在
WinKits C:\Program Files (x86)\Windows Kits\10\bin\{10.0.14393.0 ... 10.0.26100.0}
缺失    VS IDE / cmake / gcc / clang / zig / rust / dumpbin(PATH) / x64dbg / Ghidra
测试签名 未开启（bcdedit 无 testsigning / nointegritychecks）
注册表  HKLM\SOFTWARE\Nexon 等游戏相关键 **全部不存在**（用户是从别处拷来的安装结果）
```

## 7. 用户报告的启动失败现象（关键）

> "GameGuard 启动过程中发生错误，请把游戏文件夹下 GameGuard 文件夹中全部的 `*.erl` 文件发送至 xxxxx"

**这条信息的价值**：这是游戏侧 nProtect 模块（npgl）弹的框，意味着
**ASProtect 壳在 Win10 上能完整解开、游戏自己的初始化代码已经在跑**。
启动的唯一拦路虎是反作弊，不是壳、不是 DirectX、不是缺运行库。

## 8. 可复用的分析命令

PE 结构 / 导入导出 / 熵值（本次会话用的脚本思路，用 Bash 工具 heredoc 喂给 python）：

```bash
python - <<'PYEOF'
import struct,math,collections
b=open(r"D:\work\popshot\game_org\Popshot\BigShot.exe","rb").read()
pe=struct.unpack_from("<I",b,0x3c)[0]
nsec=struct.unpack_from("<H",b,pe+6)[0]; optsz=struct.unpack_from("<H",b,pe+20)[0]
secs=[]
for i in range(nsec):
    o=pe+24+optsz+i*40
    nm=b[o:o+8].rstrip(b"\0").decode("latin1")
    vs,va,rs,ro=struct.unpack_from("<IIII",b,o+8); secs.append((nm,va,vs,ro,rs))
def r2o(rva):
    for nm,va,vs,ro,rs in secs:
        if va<=rva<va+max(vs,rs): return ro+(rva-va)
# 导入表: 目录项 1；导出表: 目录项 0；资源: 目录项 2
PYEOF
```

**注意**：PowerShell 工具会把带引号的 python `-c` 字符串搅乱（`r"..."` 里的引号被吃掉），
**用 Bash 工具的 heredoc**（`python - <<'PYEOF'`）喂 stdin 最稳。

列安装包内容：
```
& "C:\SSD\Program\7-Zip\7z.exe" l "D:\work\popshot\原版安装包\PopShot_Setup_311.exe"
```

---

## 死路清单 —— 试过不行，别再试

| 尝试 | 结果 |
|---|---|
| 从 `BigShot.exe` 提取字符串找服务器地址/协议 | **无效**，代码段熵 8.0 全加密，`.data` 也是 7.79 |
| 从 `BigShot.exe` 资源里找线索 | **无效**，只有 2 图 3 图标 2 对话框，无版本信息无字符串表 |
| 直接读 `BigShotCN.ini` / `Pack\*.pkn` | **无效**，加密容器，密钥在壳里 |
| 从 `NMService.exe` / `nmcogame.dll` 的地址列表找游戏服 | **无效**，那些是 NGM/信使地址，不是游戏服 |
| `python -c "..."` 经 PowerShell 传含引号的脚本 | **会被搅乱**，改用 Bash heredoc |
| 查注册表 `HKLM\SOFTWARE\Nexon` 等找安装信息 | **不存在**，本机从未真正安装过 |
| （历史）从网上找原版客户端下载 | 已在更早的会话穷尽，见全局记忆 `popshot-client-sources`；**客户端现已从用户处获得，此项作废** |

---

# 第二批发现 —— 脱壳成功后（2026-08-06 会话 01 后半段）

## 9. 启动失败的真正原因链（已实测确认）

原版客户端在 Win10 上**跑得比预想远得多**。实测时间线（`logs\bshook_*.log`）：

```
+0.0s   进程启动，ASProtect 壳开始解
+0.5s   壳解完（base+0x1000 从 68 01 20 7B 00 变成 E9 15 02 00 00）
+0.7s   静态导入全部加载：fmod / devil / nmcogame / d3d9 / ws2_32 / dbghelp
+1.5s   dxdiagn + WMI + wbem（硬件信息采集）、setupapi、dsound
+2.0s   NVIDIA D3D 驱动 DLL 全部载入（nvd3dum / nvwgf2um / nvgpucomp32）
+5.0s   ddraw / dsound / AUDIOSES / wdmaud / midimap（音频初始化完成）
+5.3s   FirewallAPI / fwbase / FWPolicyIOMgr（注册防火墙例外）
+5.5s   ★ 主窗口创建：class=[MoleWnd] title="炮炮火枪手"（隐藏状态）
+5.6s   ★ GameGuard 错误框弹出：class=[#32770] title="公告"
```

**错误框原文**（用 `GetWindowTextW` 抓的，非乱码）：
> Game guard启动过程中发生错误，请把游戏文件夹下Gameguard文件夹中全部的*.erl文件发送至Game2@inca.co.kr

即：**D3D9、音频、资源都初始化成功了，只卡在反作弊这一步**。

**全程没有加载任何 GameGuard 模块** —— 没有 npgg/npgl/GameMon 被 map 进进程。
说明游戏侧的 nProtect 代码是**静态链进 BigShot.exe** 的（见 §10 的
`NPGameLib.lib` 字符串），它在还没加载 GameGuard.des 之前就失败了。

### ⚠ 单实例互斥体 `BigShot_Assa`

`\Sessions\1\BaseNamedObjects\BigShot_Assa`。**同时只能跑一个实例**，
第二个会立刻以 `ExitProcess(0xFFFFFFFF)` 退出。

**这曾经骗过我一次**：以为是注入被 ASProtect 检测、或是 GameGuard.des 改名导致的，
其实只是用户手上还开着一个实例。**每次测试前先确认没有残留的 BigShot.exe 进程。**

## 10. 脱壳（阶段 1 已完成）

**方法**：不用 x64dbg+Scylla（GUI 无法脚本化）。自写 `tools\dump_process.py`：
等客户端跑到 GameGuard 错误框（此时壳已完全解开、进程空闲），用
`OpenProcess + ReadProcessMemory` 把主模块内存原样倒出，再把节表的
`PointerToRawData` 改成 `VirtualAddress`、`SizeOfRawData` 改成 `VirtualSize`，
得到 Ghidra 可直接打开的 PE。

```
python tools/dump_process.py <pid>        # 输出到 re/
```

**产物**（`re\`）：
- `BigShot_<pid>.exe` — PE 化转储，3,957,248 字节 ← **给 Ghidra 用这个**
- `BigShot_<pid>.img` — 同样内容的裸内存镜像（基址 0x400000）
- `BigShot_<pid>.map.txt` — 748 个已提交内存区域清单
- `packets.txt` / `rtti_types.txt` — 见下

**质量验证**：熵值从原文件的全 8.0 降到
`.text 6.478 / 5.304 / 2.083`，`.adata 0.198` —— 解开了。

## 11. ★ 二进制里带完整 RTTI —— 协议是"读名字"不是"猜字段"

脱壳镜像里有 **879 个 MSVC RTTI 类型名**（`.?AU...@@` / `.?AV...@@`），
其中 **120 个是网络包结构体**。完整清单见 `re\packets.txt` 和 `re\rtti_types.txt`。

命名规则：
- `Packet_gcp*` = **客户端 → 服务端**（game client packet），53 个
- `Packet_gsp*` = **服务端 → 客户端**（game server packet），65 个
- `Packet_rcp*` = 中继（relay），2 个：`rcpRegister` / `rcpRepPing`
- `Req`/`Rep` = 请求/应答配对

登录大厅相关：`gcpReqChannelList`/`gspRepChannelList`、`gcpReqListSession`、
`gcpReqCreateSession`、`gcpReqMoveInto`/`gspRepMoveInto`、`gcpReqMyInfo`/`gspRepMyInfo`、
`gcpReqUserList`/`gspRepUserList`、`gspRepLogin`、`gspRepMoney`、`gspRepShopItemList`、
`gspRepEquippedList`、`gspSlotEquippedList`、`gcpReqQuickJoinSession`

开局/战斗相关：`gspPrepareGame`、`gspRepCountDown`、`gspCreateObject`、
`gspRespawnCharacter`、`gspEndGame`、`gspRepGameResult`、`gspCannotStartGame`、
`gcpReqChangeToNextMap`/`gspRepChangeToNextMap`、`gspTriggerCountGame`

闯关模式相关：`gcpEndQuest`、`gcpMarkQuestSuccess`、`gcpUpdateQuestScore`、
`gcpReqQuestRecord`、`gspRepQuestRecord`、`gspRepQuestRecordInPvp`、
`gspUpdateQuestScore`、`gspQuestReachedDifficulty`、`gspUpdateQuestDifficulty`

反作弊相关：`gspReqGameGuard`/`gcpRepGameGuard`、`gspReqHackShieldCheck`/`gcpRepHackShieldCheck`、
`gcpReportHack`

## 12. ★ 闯关模式和训练场的逻辑在客户端

RTTI 里有这些**客户端 C++ 类**：

```
GameContextQuest        GameContextQuest01 .. GameContextQuest07
GameContextPromotionQuest
GameContextTraining     GameContextTraining00 / 01 / 02
Training00AirShip       TrainingGrenade
DlgCreateQuestRoom      DlgSelectQuestMap
```

**这消灭了本项目原先最大的风险**（"关卡剧本可能在服务端，需要照录像重建"）。
服务端在闯关里只负责：开房间、开局、收分数、发结算。关卡内容本身是客户端的。

关卡 Lua 回调签名（从 boost::bind 模板参数还原）：
`void GameContextQuestNN::f(str::TString<wchar_t>&, int)` —— 说明关卡脚本
按「事件名 + 整数参数」回调进 C++。

## 13. ★ 内嵌 Lua 5.0.2 + lua_tinker

```
$Lua: Lua 5.0.2 Copyright (C) 1994-2004 Tecgraf, PUC-Rio $
LUA_PATH  /  ?;?.lua
lua_tinker::dostring()
RegisterLuaObject / RegisterLuaMethod / UnRegisterLuaMethod / GetRegisteredLuaMethodCode
```

绑定到 Lua 的对象类型（`lua_tinker::ptr2user<T>`）：
`MyCharacter, Character, GameSession, GameObject, TerrainObj, BreakableObj,
DramaticPoint, HidingObj, JumpingObj, LayerObj, MapEffObj, MapObject, RespawnPointObj, VignettingObj`

暴露给 Lua 的函数（部分）：`GetQuestDifficulty`, `GetQuestId`

**推论**：关卡脚本大概率是 `Pack\*.pkn` 里的 `.lua` 文件。解包后可直接读甚至改。

## 14. ★★ 客户端支持命令行指定服务器地址

字符串（`re\BigShot_<pid>.exe` 偏移 0x25f9d0 附近，连续排布）：

```
ServerMultiPort   ServerMultiIp
MoleServerPort    MoleServerIp        ← "Mole" 是内部代号，和窗口类 MoleWnd 对应
/serverport:      /serverip:          ← ★ 命令行开关
ServerAddr        ServerPort          ServerIp
10.10.165.49                          ← 开发期内网 IP 残留
```

**含义**：`BigShot.exe /serverip:127.0.0.1 /serverport:XXXX` 很可能直接生效，
**阶段 3 的网络重定向也许根本不需要 hook `connect`**（DECISIONS D008 可能要修正）。
`BigShotCN.ini` 里的键名也应该就是 `ServerIp` / `ServerPort` / `MoleServerIp` 这些。

其它地址：`218.145.45.32`（韩国段）、
`http://gamepopshot.tiancity.com/launcher/notice.html`（公告页，已死）、
`http://member.tiancity.com/Registration/PopshotReg.aspx`（注册页，已死）。

启动器传参格式：`-patchurl:'%s' -patchdir:'%s' -patchcmd:'%s' -patchimg:'...' -use_local_dll`

## 15. 客户端自带的日志/调试设施（可能免费送我们抓包能力）

字符串里有这些输出文件名：

```
Net.txt          ← ★ 网络日志
Sync.txt         ← ★ 同步日志
Debug.txt        Die.txt
Log/GM_%04d-%02d-%02d.txt
_Debug/%04d-%02d-%02d.txt
Dump\LastCrashReport.txt     %sLastCrashReport.txt     CrashLog.txt
```

还有 `Packet_gcpReqDbgLogin` / `gspRepDbgLogin` / `gcpRepDbgLoginId` / `gspReqDbgLoginId`
和 `ServerConnectionDbg` 类 —— **存在一条调试登录通道**，可能比正式登录流程简单得多。
另有 `ServerConnectionJpn` + `Packet_gcpReqJpnLogin`（日服分支）。

## 16. 包加密 = SNOW 流密码

RTTI：`?$CipherStream@VSnowCipher@@` → `CipherStream<SnowCipher>`。
**SNOW 是公开算法**（SNOW 1.0/2.0），不是自研黑盒 —— 只需从代码里找出密钥/IV 来源即可。
配套类：`BufferStream`, `FileStream`, `BasicSession`, `RawPacket`, `NetAddress`,
`ServerConnection`, `GameSession`, `UDPBinder`。

`Pack\*.pkn` 和 `BigShotCN.ini` 的加密**很可能也是同一套 SnowCipher**。

## 17. 其它架构事实

- C++ 运行库用 **STLport**（`_STL` 命名空间），不是 MSVC STL
- 大量 **boost::bind**（`boost::_bi::bind_t`）
- 界面状态机：`GameStage`, `LoadingStage`, `RoomStage`, `ShopStage`
- 主窗口类名 `MoleWnd`，标题「炮炮火枪手」
- 资源路径前缀 `Images/NewUI2/`、`Images/Chinese/`、`GPack/`、`Pack/*.pkn`
- 字符串类：`str::TString<wchar_t, str::TCharTraits<wchar_t>>`（宽字符）

---

## 死路清单追加

| 尝试 | 结果 |
|---|---|
| 把 `GameGuard.des` 改名后运行，期望游戏跳过反作弊 | **没用**，照样弹同样的错误框（错误来自静态链接的 NPGameLib，不是那个文件） |
| 用 x64dbg + Scylla 做脱壳 | **没必要**，自写 `tools/dump_process.py` 更快更可靠，见 §10 |
| 以为注入被 ASProtect 检测导致秒退 | **误判**，真凶是单实例互斥体 `BigShot_Assa` |

---

# 第三批发现 —— 阶段 2 起步（会话 01 末）

## 18. ★ GameGuard 校验失败是硬阻断（已实测）

实验：客户端跑到「公告」错误框后，用 `SendMessageW(btn, BM_CLICK)` 点掉「确定」。
**结果：进程立即退出**（不是隐藏错误框继续走登录）。

结论：阶段 2 **不能只吞错误框**，必须让 GameGuard 初始化逻辑本身返回成功。
主窗口 `MoleWnd` 虽然在错误框之前就建好了，但那只是预创建，校验没过就销毁退出。

## 19. GameGuard 相关代码位置（脱壳镜像 re\BigShot_22524.exe，基址 0x400000）

> 转储做了 PE 化，**文件偏移 == RVA**（PointerToRawData 已设成 VirtualAddress）。
> 即：绝对地址 va → 文件偏移 = va - 0x400000。

字符串位置：
```
"InitNPGameMon: %lu"                          va=0x6ca098  (off 0x2ca098)
"--- NPGameLib.lib version %d : %hs ---"      va=0x6ca6a4  (off 0x2ca6a4)
```

引用点：
```
va=0x56164c  push 0x6ca098 ("InitNPGameMon: %lu") → call 0x567f80(sprintf类)
             ↑ 这是个日志格式化函数，位于 va=0x561640 起的小函数，不是校验主体
va=0x561d13  引用 "NPGameLib.lib version"（同属日志/版本打印）
```

**注意**：这两个引用都只是**日志**，不是校验判定点。真正的 GameGuard 初始化
（调用 nProtect API、检查返回值、失败则弹框退出）还没定位。
下个会话用 Ghidra 全量分析后，从这两个日志函数的**调用者**往上找校验主体。

nProtect(INCA) 的典型 API 名（Ghidra 里搜这些交叉引用）：
`_NPGameMonInitialize`, `NPGameMonInitialize`, `InitNPGameMon`, `NPGetKeyState`,
`NPGameMonStartCallback`, `_AhnLibInitialize`。校验主体通常是：
调 Init → 若返回非 0 → 走错误分支（弹「公告」框 + `ExitProcess`）。
patch 策略：让 Init 调用点直接 `mov eax,0` 或跳过错误分支的条件跳转。

## 20. 错误框「公告」的字符串不在明文镜像里

`"Game guard启动过程中发生错误..."` 这串中文**在转储里搜不到**（UTF-16 和 GBK 都没有）。
可能：① 从 `npgl.erl`（加密错误资源）解密而来；② 从 pkn 里的文本表取的；
③ 运行时拼装。定位错误框调用点时以窗口类 `#32770` + 标题「公告」为准，
或找 `MessageBox`/`DialogBox` 系列 API 的调用。

---

# 第四批发现 —— GameGuard 校验点精确定位 + patch（2026-08-06 会话 02）

## 21. ★★★ GameGuard 校验点已精确定位（动态 hook 抓到的，铁证）

工具：`tools/re_bs.py`（capstone+pefile 自写反汇编工具箱，**capstone/pefile 其实已装**，
FINDINGS §6「未装」作废）。方法：在 `bshook.dll` 里内联 hook `MessageBoxW`，
错误框弹出时抓到 `_ReturnAddress` + EBP 链回溯。

**错误框调用**（游戏自有代码，非 nProtect 线程）：
```
MessageBoxW(hwnd, "Game guard启动过程中发生错误…", "公告", MB_OK)
调用点 va=0x40f4bf: call dword ptr [0x6e6290]   ← [0x6e6290] = 游戏 IAT 的 MessageBoxW 槽
返回地址 0x40f4c5，所在函数是通用「弹提示框」封装
```

**EBP 链**（外→内）：`0x5f7b9e → 0x40a68e(虚调用) → 0x40d039 → 0x54b042 → 弹框封装`

**校验判定点 va=0x40d034**（GameStage 状态机里，经 vtable 虚调用进来）：
```
0040d02b: push dword ptr [edi+0x40]
0040d02e: push dword ptr [0x72e28c]
0040d034: call 0x54b042        ; GameGuard 校验函数，返回 al(1=过/0=失败)
0040d039: test al, al
0040d03b: je   0x40d545        ; al==0 → 跳到失败清理/退出
```

**校验函数 0x54b042 内部**（关键在这）：
```
0054b060: inc ebx              ; ebx=1（成功时 al=bl 用）
...
0054b0fc: call 0x5611d0        ; ★ 取 GameGuard 状态（内部读 [[0x6e6624]+0x10]，
                               ;   并打印 "InitNPGameMon: %lu"）。因 nProtect 从未真初始化，
                               ;   [0x6e6624]=0 → 返回 0 → 非成功码
0054b101: mov esi, eax
0054b103: cmp esi, 0x755       ; ★ 0x755(1877) = GameGuard 成功码
0054b109: je  0x54b572         ; 成功路径 → 0x54b58f: mov al,bl(=1); ret 8
                               ; 否则按错误码 switch → 弹「公告」框 → 返回 al=0
```

`0x5611d0` 只是**状态取值器+日志**（`jmp 0x561640` 格式化 InitNPGameMon），
**不是真正的 nProtect 初始化**，跳过它无副作用（只少一行日志）。

## 22. ★ 当时采用的 5 字节 patch（历史；会话 23 已被 §121 的 DR0 + VEH 取代）

**在 0x54b0fc 把 `call 0x5611d0` 替换成 `mov eax,0x755`**（都恰好 5 字节）：
```
原:  E8 CF 60 01 00   call 0x5611d0
新:  B8 55 07 00 00   mov eax, 0x755
```
效果：esi=0x755 → `je 0x54b572` 成功路径 → al=1 → 调用者 `je 0x40d545` 不跳 → 继续启动。
**nProtect 状态取值器被完全跳过，错误框根本不会被调用。**

patch 时机：`bshook.dll` 起一个轮询线程，等 0x54b0fc 的字节从密文变成 `E8 CF 60 01 00`
（= ASProtect 解壳到该区域）后再写。校验点 0x40d034 在启动 ~5s 时才执行，解壳 ~0.7s 完成，
时间窗充裕。

## 23. 内联 hook 事实（bshook.dll 现有能力）

- Win10 user32 的 `MessageBoxW`/`MessageBoxA` 序言就是经典热补丁 5 字节 `8b ff 55 8b ec`
  （mov edi,edi; push ebp; mov ebp,esp），可干净偷 5 字节做蹦床，已实测成功。
- `bshook.dll` 现内置：极简 x86 长度反汇编器 `insn_len` + 通用内联 hook `install_inline_hook`
  + EBP 链回溯 `log_ebp_chain`。阶段 3 hook ws2_32 可直接复用。
- 主模块 SizeOfImage = 0x3C6200，范围 0x400000..0x7C6200。
- **内联 hook 的坑**：`_ReturnAddress()` 在 detour 里拿到的 caller 有时落在 bshook.dll
  自己（因为 hook 的是 ws2_32!connect，而游戏经 WSOCK32→ws2_32 转发过来，且 EBP 顶帧
  会串到 detour/log 函数自身）。**caller 值仅供参考，sockaddr 目标才是可靠数据**。

---

# 第五批发现 —— 阶段 3：认证服务器地址（2026-08-06 会话 02）

## 24. ★★ 游戏认证服务器 = 222.73.1.42:47611（动态抓到）

`bshook.dll` 内联 hook `ws2_32!connect`（Win10 序言均为热补丁 5 字节 `8b ff 55 8b ec`，
`connect`/`WSAConnect`/`gethostbyname`/`getaddrinfo` 都干净可 hook）。
用 `tools/gui_probe.py`（ctypes 直接操作登录框）填账号点「开始」触发连接，抓到：

```
★WS2 connect s=4544 -> 222.73.1.42:47611
```
- 222.73.x = 上海电信段（世纪天成国服所在），与登录界面选的「炮火连天(电信)」分区对应。
- 连接失败 ~10s 后，游戏自有代码（va=0x00423E1F）弹 MessageBoxW：
  标题「登录失败」正文「认证服务器失败 (20000)」。**20000 = 认证服连接失败码。**
- **这是登录流程的第一跳（认证服）。** 认证通过后大概率还会连大厅/游戏服（可能别的端口），
  但认证不过就看不到，需等假认证服跑起来后才能观测下一跳。

**登录框控件 ID**（`tools/gui_probe.py enum <pid>` 抓的，#32770 "PopShot" 对话框）：
```
Edit  id=1004 用户名     Edit  id=1005 密码
Button id=1006 开始       id=1007 记住帐号
Button id=1011 炮火连天(电信)  id=1012 枪林弹雨(网通)
+ 内嵌 IE（MPlay.Control.MiniBrowser / Internet Explorer_Server）= 公告页，走 wininet 不走 ws2
```

## 25. 登录连接经 WSOCK32（winsock 1.1）转发

`connect` 的 caller 落在 bshook.dll（转发链干扰），但进程里 **WSOCK32.dll（winsock 1.1
兼容层）被加载**且游戏静态导入 `ws2_32`。登录网络码大概率经 `WSOCK32!connect`→`ws2_32!connect`。
hook `ws2_32!connect` 能抓到全部（WSOCK32 内部转发到 ws2_32）。
**待查**：认证走的是 Nexon 通行证协议还是游戏自有 gcp/gsp(SnowCipher) —— 要靠抓首包字节判定。

## 26. ★★ 登录首包已抓到 + 帧格式初步解出（阶段4 起步）

做法：`bshook.dll` 把 connect 重定向到 127.0.0.1（端口不变），
`server/capture_server.py` 在 127.0.0.1:47611 监听并落盘。用 `gui_probe.py login` 填不同长度
账号触发，抓到两个样本（`logs/conn_001_47611.bin` / `conn_003`）：

```
样本A user="testuser"(8) pass="testpass"(8)  共 70 字节:
  00 42 00 0f 18 00 00 3e 02 00 00 | 32 5e 90 54 98 7d 4b b4 35 ... 87 80 00 00
样本B user="abc"(3)      pass="z"(1)         共 46 字节:
  00 2a 00 0f 18 00 00 26 02 00 00 | 1a 5e 92 43 b1 7d 4b b4 35 ... 6f 14 00 00
                                    ↑ off 0x0b 起是 SnowCipher 密文
```

**长度差 = 70-46 = 24 字节；账号+密码字符差 = (8+8)-(3+1) = 12；24/12 = 2 字节/字符
→ 凭据是 UTF-16 宽字符**（印证 §17 的 `str::TString<wchar_t>`）。

**帧头格式（前 11 字节明文，之后是密文体）**：
```
off 0  [u16 BE] = 总长-4      (0x42=66 / 0x2a=42)   外层长度(从 off4 到结尾)
off 2  [u16]    = 00 0f       两样本相同 → 消息类型/opcode 0x0f
off 4  [byte]   = 18          常量(=24?)
off 5  [u16]    = 00 00       常量
off 6  实际上 [u16 BE @off6] = 总长-8 (0x3e=62 / 0x26=38)  内层(密文)长度
off 8  [3字节]  = 02 00 00    常量(02 疑似加密方式标记)
off 0x0b 起     SnowCipher 密文，长度 = off6 的值；含 UTF-16 的用户名/密码
```
（注：off4~off8 的精确字段边界还需更多样本确认；但「off0=总长-4、off6=总长-8、
off2=0x0f 固定、密文从 0x0b 起」已由两样本坐实。）

**卡点/下一步（阶段4 主线）**：密文体要解密才能读到明文登录结构。两条路：
1. **静态**：从 `re/BigShot_22524.exe` 找 `CipherStream<SnowCipher>` 的 key/IV 来源
   （RTTI 已知类名，§16）——SNOW 是公开算法，拿到 key 就能自己解。
2. **动态（更快，PLAN 阶段4 止损方案）**：在客户端内 hook SnowCipher 加/解密函数出入口
   直接抓明文，算法没逆完也能先跑通。
另：`conn_002` 抓到 0 字节（客户端可能开了第二个 socket 或重试），忽略。

## 27. 阶段3 工具链（本会话新增，可复用）

- `server/capture_server.py`：监听指定端口，accept 后 hexdump 落盘 `logs/conn_*.{bin,txt}`。
  **只收不发**（抓首包用）。要让客户端走完登录，得让它按协议回包（阶段5）。
- `tools/gui_probe.py`：ctypes 直接操作登录框。`enum <pid>` 列控件；
  `login <pid> <user> <pass>` 选分区(id=1011)+填账号(1004/1005)+点开始(1006)。
- `bshook.dll`：GameGuard patch + MessageBox 抑制 + ws2_32(connect/WSAConnect/
  gethostbyname/getaddrinfo) hook + connect 重定向 127.0.0.1（`g_redirect`）。

---

# 第六批发现 —— 阶段 4：包加密体系（2026-08-06 会话 03）

## 28. ★★★ SnowCipher = 标准 SNOW 2.0（已完整还原并逐位验证）

RTTI `.?AVSnowCipher@@`（TD 0x6e6028）→ COL 0x6a47c0 → **vftable 0x692f28**，4 个虚函数：

| 槽 | va | 作用 |
|---|---|---|
| [0] | 0x5bc4f1 | scalar deleting dtor |
| [1] | 0x4eaf4b | `push 4; pop eax; ret` = **块大小 4 字节** |
| [2] | 0x5dd200 | **Encrypt**：`dst[i] = src[i] + ks[i]`（32 位**加法**，不是异或） |
| [3] | 0x5dd242 | **Decrypt**：`dst[i] = src[i] - ks[i]` |

配套：
```
0x5dc7bc  loadkey(key, keysize, iv0,iv1,iv2,iv3)   ecx=state   ret 0x18
0x5dbf85  一次生成 16 个密钥流字（写到 state+0x50）
0x5dd1e1  取下一个密钥流字（索引在 state+0x90，==16 时先生成）
```
state 布局（ecx 指向 SnowCipher 对象 +4）：
`+0x00..0x3c` = LFSR 16 字（`m[k]` 对应论文里的 `s(15-k)`）、`+0x48`=R1、`+0x4c`=R2、
`+0x50..0x8c` = 16 字输出缓冲、`+0x90` = 索引。

**6 张常量表**（各 256 个 dword，转储里已初始化）：
```
T0 alpha      0x6d8aa8    T0[1]=0xe19fcf13  ← SNOW 2.0 参考实现的标准常量
T1 alpha^-1   0x6d8ea8    T1[1]=0x180f40cd  ← 同上
S0..S3        0x6d92a8 / 0x6d96a8 / 0x6d9aa8 / 0x6d9ea8   （AES S 盒 T-table 的 4 个旋转）
```

**调用约定**：客户端固定 `keysize=128`、`IV=(0,0,0,0)`（见 0x560427 `push 0x80` + 4 个 `push ebx(=0)`）。

**两个必须如实复刻的实现细节**：
1. `Encrypt/Decrypt` 只处理 `len//4` 个整字，**尾部 `len%4` 字节原样不加密**（无余数处理）。
2. loadkey 取 key 字时用的是 `movsx`（原版把 key 声明成了 `char*`）——
   key 字节 ≥0x80 时结果**不等于**普通大端取字。`server/snow.py` 里的 `_w32` 已复刻。

**Python 实现 `server/snow.py` 已用客户端实测数据逐位验证通过**（见 §29 的基准数据）。

## 29. ★★★ pkn / ini 容器的密钥 = 文件名本身

派生函数 **0x5608b0**：
```
key[j] = (宽字符串[j % len] 的低字节 + j) & 0xff ,  j = 0..15
```
实测：客户端解 `Pack\Data0000.pkn` 时 loadkey 的 key =
`50 62 65 6e 33 49 67 7b 69 39 3a 3b 3c 3b 7e 7a`，逐字节减下标 → **"Pack/Data0000.pk"**
（即 `"Pack/Data0000.pkn"` 的前 16 字节，注意是**正斜杠**）。
`key_from_wstr("Pack/Data0000.pkn")` 与之完全一致。

**这解开了 FINDINGS §4 的谜**：容器里那段明文文件名不是疏忽，**它就是密钥**。

**算法基准数据**（客户端 hook 抓的入/出配对，key 如上、IV=0、同一个 cipher 对象连续调用）：
```
in 0d6b96a0 -> out 00000000      密钥流字 k0 = a0966b0d
in 3e199e0d -> out 805ad763              k1 = a9c6bebe
in 0d916005 -> out 82000000              k2 = 0560908b
in cded7814 -> out a2010000              k3 = 1478ec2b
in cb9848fc -> out 04000000              k4 = fc4898c7
in 028d2f6c625dc159 -> out "D\0a\0t\0a\0" k5 = 6bce8cbe, k6 = 59605cee
```
→ Data0000.pkn 索引头 = `[u32 0][u32 0x63d75a80][u32 0x82][u32 0x1a2][u32 4]["Data" UTF-16]`。
解完这一层后客户端会用**第二把密钥**（如 `dc 42 c2 5f 94 c5 66 97 8c f1 2d 1b 6a 4e 9a d1`，
非 ASCII、但每次运行**恒定**）继续解内层，接着解出 `"Chinese.ini"` 等条目名。

**含义**：`Pack\*.pkn` 的解包现在是可做的（PLAN 阶段 6 的可选项已不再受阻）。

## 30. SimpleCipher（ICipher 的另一个实现，登录没用到但记下来）

RTTI `.?AVSimpleCipher@@`（TD 0x6d85b0）→ COL 0x693e4c → **vftable 0x64dd54**：
```
[1] 0x4f167c  块大小 = 1 字节
[2] 0x5bc449  Encrypt: dst[i] = src[i] + tblA[i1] + tblB[i2]
[3] 0x5bc49d  Decrypt: dst[i] = src[i] - tblA[i1] - tblB[i2]
     每字节后 i1=(i1+1)%49, i2=(i2+1)%24 ; i1 在 this+4, i2 在 this+8（跨调用保持）
```
```
tblA @0x64dd64 (49 字节) = 经典 Aleph One 的 Linux x86 execve("/bin/sh") shellcode：
  eb 1a 5e 31 c0 88 46 07 8d 1e 89 5e 08 89 46 0c b0 0b 89 f3 8d 4e 08 8d 56 0c
  cd 80 e8 e1 ff ff ff "/bin/sh#AAAABBBB"
tblB @0x64dd98 (24 字节) = 38 31 57 31 4e 31 4f 31 34 31 4f 31 47 31 62 31 00 ac 48 31 57 31 31 31
```

## 31. ★★ 登录包外层 = 一次一密的 4 字节 XOR，密钥明文写在包里

用**同一账号、两次不同运行**抓的两个 70 字节登录包做差分，得到严格周期 4 的
`61 91 f4 03`（从 off 20 起一直到 off 67），off 12..15 的差分则是它的**逆序** `03 f4 91 61`。

由此定死帧格式：
```
off 0..1    u16 BE = 总长-4          (0x42=66 / 0x2a=42)
off 2..3    00 0f                    固定（消息类型/opcode）
off 4       18                       固定
off 5..6    00 00                    固定
off 7       总长-8                   (0x3e / 0x26)
off 8       02                       固定
off 9..10   00 00                    固定
off 11      u8 = 总长-20             = 加密体长度（含末尾 2 字节）
off 12..15  ★ 4 字节会话密钥，明文传输，每次运行随机
off 16..19  7d 4b b4 35              固定常量（跨运行、跨账号都一样）
off 20..末-2 XOR 加密体（48 / 24 字节）
off 末-2..末 00 00                   明文尾
```
解密：`plain[i] = cipher[i] ^ key[3 - ((i-20) % 4)]`，其中 `key = 包[12:16]`。
**验证**：同账号两次不同运行的包，解出的 48 字节体**完全相同** —— 一次一密那层已彻底消除。

**尚未解决**：解出的体里还有一层**固定**变换，不是可读明文。已排除的可能见死路清单。
已知的体差分（testuser/testpass vs abc/z，前 24 字节）：
```
A体: 70 15 88 40 72 6c 3c 11 08 ca 8f 83 df 6c 09 e2 88 14 fd 74 60 15 c6 4a ...
C体: 7b 15 9d 40 7e 6c 39 11 7a ca 90 83 d9 6c 60 c0 88 15 c5 56 67 14 fd 4a
```
（奇数位 1,3,5,7,9,11,13 相同、偶数位不同 —— 强烈提示底下是 UTF-16。）

## 32. ★★ ASProtect 有后台完整性校验线程（此前一直在赌运气）

现象：弹 `MessageBoxA(cap="Protection Error", text="Error: 15")` 后卡在模态框上，进程不退。
调用链（EBP 回溯）全在 ASProtect 的动态代码区 `0x0295xxxx..0x0296xxxx`，
最外层 `ret=0`，即**它自己的独立线程**，与游戏代码无关。

- 不注入直接跑 `BigShot.exe`：**不报**（只报 GameGuard 那个「公告」框）→ 确认是我们的 patch 触发的。
- **GameGuard patch（0x54b0fc）打在 +0.7s 时，实测 4 次中 3 次被抓**（成功率约 25%）。
  会话 01/02 的成功是运气好，不是稳定行为。
- **把 patch 延后到 +2.5s 后，实测 5/5 全部通过**并进到登录界面。
  校验点 0x40d034 约 +5s 才执行，2.5s→5s 就是唯一的安全窗口。
- SnowCipher 那三个 hook（0x5dc7bc/0x5dd200/0x5dd242）在 +2.5s 之后装，实测也安全。

## 33. 登录包**不是** SnowCipher 也不是 SimpleCipher 加密的（已排除）

两条独立证据：
1. **静态**：搜整个镜像对 6 张 SNOW 表和 2 张 Simple 表的引用 ——
   SNOW 表 32 处引用**全部**落在 `0x5dbf85..0x5dd1de` 之内，
   Simple 表 4 处引用全部落在 `0x5bc449/0x5bc49d` 之内。**没有任何内联副本。**
2. **动态**：5 个 cipher 函数全部 hook 上之后完整跑一遍登录，
   从 connect 到「认证服务器失败」全程**一次都没被调用**。


---

## 死路清单追加（会话 03）

| 尝试 | 结果 |
|---|---|
| 枚举镜像里全部 37596 个字符串当密钥源，SNOW 解登录包 | **无效**。后来查明登录包压根不是 SnowCipher 加的（§33） |
| 拿连接前最后 16 把 loadkey 密钥（含 4 把非文件名派生的）SNOW 解登录包 | **无效**，同上 |
| SimpleCipher 穷举全部起始状态（i1 0..48 × i2 0..23 × 偏移 0..15 × 加/减） | **无效**，同上 |
| 把解出的 48 字节体再用常量 `7d 4b b4 35`（4 种旋转 × xor/加/减）解一次 | **无效** |
| 把 SnowCipher hook 装在解壳刚完成时（+0.7s） | **会被 ASProtect 完整性校验抓**，见 §32；必须晚于 +2.5s |
| bshook 里对每次加解密都 `bslog`（含 FlushFileBuffers） | **把游戏拖死**：启动加载 pkn 会产生上万次调用，40s 都进不了登录界面。必须等 connect 之后再开闸 |

---

## 34. ★★★ 登录包已完全解开（会话 03 收尾，`server/protocol.py` 可直接编解码）

§31 只解到外层。完整答案是**两层**，都是异或、都对合：

### 第一层：一次一密（4 字节会话密钥，明文写在包里）
```
sessionkey = pkt[12:16]          每次连接随机
ct[i] = pkt[20+i] ^ sessionkey[3 - (i % 4)]      i = 0 .. len-22
（注意是**逆序**用密钥字节；off 16..19 和末尾 2 字节不参与）
```

### 第二层：64 字节固定密钥流 F + 4 字节明文反馈
```
plain[i] = ct[i] ^ plain[i-4] ^ F[i % 64]        plain[i<0] 视为 0
ct[i]    = plain[i] ^ plain[i-4] ^ F[i % 64]
```
```
F (64 字节，周期已由 F[64..67] == F[0..3] 确认):
  78 15 fc 40 1f 6c 3b 11 19 ca 89 83 d8 6c 19 e2
  89 14 90 74 66 15 ab 4a a0 12 8c 7b cd ff 18 00
  4b 70 ab cc 0f 8c 5a 7b 91 b8 13 aa 07 98 41 de
  ae bc ff 12 34 ba 5f 5f 99 ac f5 10 01 dd c1 b1
```
F **不在镜像里**（整表和各种前缀都搜不到），是运行时生成的。当前这份是用已知明文反解的。

### 完整帧格式（opcode 0x0f = 登录）
```
off 0..1    u16 BE = 总长-4
off 2..3    u16 BE = 0x000f      opcode
off 4..5    18 00                固定
off 6..7    u16 BE = 总长-8
off 8..10   02 00 00             固定
off 11      u8    = 总长-20      加密体长度（含末尾 2 字节）
off 12..15  4 字节会话密钥（明文，每次随机）
off 16..19  7d 4b b4 35          固定常量，不参与加密，含义未知
off 20..末-2 密文体
off 末-2..末 00 00
```

### 登录包体结构（opcode 0x0f）
```
u16 LE   用户名字符数
UTF-16LE 用户名
u16 LE   密码字符数
UTF-16LE 密码
12 字节  00 00 03 22 01 01 56 00 00 00 00 00   ← 4 个样本完全一致，尚未逐字段解释
```

### 验证
4 个样本全部解出正确凭据、且尾部一致：
`testuser/testpass`（两次不同运行）、`abc/z`、`abcdefghijklmnop/0123456789abcdef`。
`server/protocol.py` 自检：`pack(build_login("testuser","testpass"), skey=5e905498)`
**与真实抓包逐字节相同**。

### 待办
- F 的生成算法还没找（当前 64 字节够用；若服务端回包需要更长的流，或 F 与 opcode/方向有关，
  就必须找出生成器）。入手点：hook `ws2_32!send` 拿 EBP 回溯 → 打包代码。
- 12 字节尾部的字段含义（疑似版本号/区服/机器码，`03 22` 可能是版本 3.34）。

---

# 第七批发现 —— 认证协议整条打通（2026-08-06 会话 04）

## 35. ★★★ 登录协议根本不是游戏自己的，是 Nexon 通行证（NMCO），代码在**未加壳**的 `nmconew.dll` 里

会话 03 花大力气用差分反解出来的东西，其实在磁盘上就是明文可读的。

**证据链**：
1. 会话 03 反解出的 64 字节固定流 F，在 `game_org/Popshot/nmconew.dll` 文件偏移
   `0xef548`（VA `0x100ef548`）处**64/64 逐字节精确匹配**，是一张硬编码常量表。
2. `bshook` 日志显示：客户端点「开始」connect 的同一刻，
   **`nmconew.dll` 被动态加载进 BigShot 进程**（base=0x09180000）。
   —— 这解释了为什么 F 表在 `BigShot.exe` 的转储里一个 dword 都搜不到（§37）。
3. 用从 DLL 静态读出的结构去解真实抓包，**逐字节复现**，包括重新加密后与原密文相同。

**含义**：认证服方向的协议**不需要再逆 BigShot.exe**。`nmconew.dll` / `NMService.exe`
都未加壳，请求和应答的字段表可以直接读。

## 36. ★★★ NMCO 帧格式（全部从静态代码推导，非拟合）

关键函数（`nmconew.dll`，ImageBase 0x10000000）：

| VA | 作用 |
|---|---|
| `0x1008bdb0` | 收包外层：校验首字节必须是 `0x18`，读 u24 长度，调下面那个 |
| `0x1008c180` | 解 12 字节头 |
| `0x1008cbb0` | 组 16 字节头（含 `0x18` tag） |
| `0x100981d0` | 最外层：写 `[u16 BE 长度][u16 BE opcode]`，opcode 取自 `this+0x10` |
| `0x1009f270` | **加密** `C[i] = P[i] ^ P[i-1] ^ key ^ Fw[i&15]` |
| `0x1009f2f0` | **解密** `P[i] = C[i] ^ P[i-1] ^ key ^ Fw[i&15]` |
| `0x100ef548` | 64 字节常量表 F（16 个**小端** dword） |
| `0x1008ba60` | ReadInt32（直接取 dword，**小端**） |
| `0x1008ba00` | ReadUInt16 |
| `0x1008bb80` / `0x1008c0c0` | 读字符串（u16 小端字符数 + UTF-16LE） |
| `0x1008ca80` | 写字符串（同上编码） |

```
[0..1]  u16 BE  = NMCO 消息长度（= 总长-4）
[2..3]  u16 BE  = opcode
[4..]   NMCO 消息：
    [0]      0x18          固定 tag，客户端硬校验（0x1008bdb0）
    [1..3]   u24 BE = 载荷长 + 12
    [4]      flags         bit1(0x02)=加密  bit2(0x04)=分片续传
    [5..7]   u24 BE = 载荷长
    [8..11]  u32 BE 会话密钥（发送方随机；nmconew 里是 GetTickCount ^ 0x5f54ca13）
    [12..15] u32 BE 消息 ID
    [16..]   载荷
```

**加密只处理载荷前 `len//4*4` 字节**（`shr eax,2` 后按 dword 循环），尾部余数原样明文。
—— 会话 03 记的「末尾 2 字节明文尾部字段」其实不是字段，就是这个余数。

**注意**：分片阈值 1400 字节（`cmp [ecx+8], 0x578`）。超过要拆包，目前用不到。

## 37. `BigShot.exe` 的转储里没有 F 表，是因为 ASProtect 按页惰性解密

登录后重新转储进程（`re/BigShot_6852.*`），F 表**依然搜不到**，`.data` 熵仍是 7.567
（原始文件 7.79，几乎没变），零字节仅 10.9%。
即 ASProtect 2.x 的页级保护会在页用完后**重新加密**，靠"事后转储"拿不到。

**但这条路已经不需要了**（见 §35）：登录代码根本不在 BigShot.exe 里。

## 38. ★★★ NMCO 完整 opcode 表（16 个消息类，已由客户端实跑三次验证）

**还原方法**（`tools/nmco_opcodes.py`）：
基类构造 `CNMFunc::CNMFunc(int opcode)` 在 `va=0x100980f0`，把 opcode 写进 `this+0x10`；
派生类构造形状固定 `push <opcode>; call 0x100980f0; ...; mov [this], <派生虚表>`。
枚举全部 30 个派生构造 → 16 个不同 opcode。

**类名对应关系**：`.data` 0xef154 起有 16 个工厂函数类型描述符
（`.P6APAVCU<类名>Packet@@XZ`），其排列顺序与「按构造函数地址排序的 16 个 opcode」
**一一对应**；且 7 组 Request/Reply 全部落成相邻的 `n / n+1`，
两个没有 Reply 兄弟的类（`Login2` / `UpdateAndAttachSession`）
正好落在两个落单的 opcode（0x0f / 0x17）上。

| opcode | 类 | 虚表 | 构造 |
|---|---|---|---|
| 0x0b | CULoginPacket | 100d2ec0 | 1002e141 |
| **0x0c** | **CULoginReplyPacket** | 100d2ee4 | 1002e691 |
| 0x0d | CULogoutPacket | 100d2f08 | 1002ebf1 |
| 0x0e | CULogoutReplyPacket | 100d2f2c | 1002efc1 |
| **0x0f** | **CULogin2Packet**（客户端实际发的登录包） | 100d2e9c | 1002d991 |
| 0x15 | CUUpdateSessionPacket | 100d304c | 10031381 |
| 0x16 | CUUpdateSessionReplyPacket | 100d3070 | 10031751 |
| 0x17 | CUUpdateAndAttachSessionPacket | 100d3028 | 10030fb1 |
| 0x1f | CUCheckSessionPacket | 100d2e54 | 1002bdb1 |
| 0x20 | CUCheckSessionReplyPacket | 100d2e78 | 1002d371 |
| 0x29 | CURequestKeyPacket | 100d2f98 | 1002fe5c |
| 0x2a | CURequestKeyReplyPacket | 100d2fbc | 1003011c |
| 0x2b | CURequestAppServerListPacket | 100d2f50 | 1002f3bc |
| 0x2c | CURequestAppServerListReplyPacket | 100d2f74 | 1002f691 |
| 0x2d | CURequestOldFashionInfoPacket | 100d2fe0 | 10030441 |
| 0x2e | CURequestOldFashionInfoReplyPacket | 100d3004 | 10030811 |

每个包类虚表 3 槽：`[0]` 析构 / `[1]` **Serialize（写）** / `[2]` **Deserialize（读）**。
虚表间距 0x24 字节。

**⚠ 注意 0x0f 是 `CULogin2Packet` 不是 `CULoginPacket`** ——
它没有自己的 Reply 类，复用 `CULoginReplyPacket`(0x0c)。实测客户端确实接受 0x0c。

## 39. ★★★ 认证握手的完整报文（实测跑通，`server/authserver.py --reply login`）

```
客户端 → 0x0f CULogin2Packet
    u16+UTF-16LE 用户名 / u16+UTF-16LE 密码 / u16+UTF-16LE 空串
    + 12 字节 = 03 22 01 01 | 56 00 00 00 | 00 00 00 00（3 个 int32，含义待定）
服务端 → 0x0c CULoginReplyPacket        （反序列化 va=0x1002e910）
    int32 结果码 / string / string / int32 / int32 / int32
客户端 → 0x2d CURequestOldFashionInfoPacket   （载荷 2 字节 = 一个空串）
服务端 → 0x2e CURequestOldFashionInfoReplyPacket  （反序列化 va=0x10030c10）
    int32 / string / string / string / int32 / u16 / u16 / int32 / int64 / int64
客户端 → 0x0d CULogoutPacket                  （载荷 2 字节 = 一个空串）
服务端 → 0x0e CULogoutReplyPacket             （反序列化 va=0x1002f160）
    int32 结果码 / string
```
**全部字段填 0 / 空串，客户端照单全收**（本会话实测三轮，每轮都往前推进一步）。
每个请求各用一条**新 TCP 连接**，服务端回完客户端就主动关闭。

## 40. ★★ 下一跳服务器 = 222.73.209.12:27799

认证握手走完后，客户端立刻 connect **`222.73.209.12:27799`**（bshook 日志实测）。
这才是游戏本体的服务器（大厅/游戏服），`Packet_gcp*` / `Packet_gsp*`（120 个 RTTI 名，
见 `re/packets.txt`）应该走这个端口，而且**那一层才是 `CipherStream<SnowCipher>`**。

`bshook` 已把它重定向到 `127.0.0.1:27799`，但当前没人监听 → 连接失败。
**下个会话从这里接手。**

---

## 死路清单追加（会话 04）

| 尝试 | 结果 |
|---|---|
| 在 `BigShot.exe` 的内存转储里找登录协议的代码/常量 | **白费**。登录由动态加载的 `nmconew.dll` 干，主模块里根本没有 |
| 登录后重新转储进程，指望 ASProtect 把 `.data` 解开 | **无效**，熵值一点没变（页级保护用完会重新加密） |
| 用 RTTI（`.?AV`/`.?AU`）枚举 `nmconew.dll` 的包类 | **没有**。这些包类不带类 RTTI，只有工厂函数指针的类型描述符 |
| 扫 `.rdata`/`.data` 找「opcode + 函数指针」静态分发表 | **没有**，注册是运行时建 map |
| 从 `CNMLoginAuthFunc` 那条线找线上格式 | **走偏了**。`CNM*Func` 是本地 IPC 层（`_NMCONEW_FuncCall_Event_`），不是线上包 |

## 41. 游戏服 27799 的首包 = 4 字节高熵数据，**不是** NMCO

`server/authserver.py --port 47611,27799` 实测：认证握手走完后客户端连上 27799，
**主动发来 4 字节就停住等应答**：

```
logs/auth_003_27799.bin :  53 72 8f 7f
```

- 没有 NMCO 的 `0x18` tag，也不符合 `[u16 BE 长度][u16 BE opcode]`
  （若按 NMCO 解，长度字段会要求 21366 字节）。
- 4 字节 = **`CipherStream<SnowCipher>` 的块大小**（FINDINGS §28 虚表槽[1] 返回 4）。
- 高熵 → 大概率是握手种子 / 密钥协商的第一个字，或者已经是加密过的首字。

**结论：27799 走的是游戏自有的 `gcp`/`gsp` + SnowCipher，和认证服完全是两套。**
`server/snow.py` 已经复刻并逐位验证过，缺的是**密钥来源**。

另注：本轮客户端在连 27799 **之前**没有发 `0x0d CULogoutPacket`（上一轮发了），
说明登出是超时/清理路径，不是必经步骤。

---

# 第八批发现 —— 游戏服 27799 协议整层打通（2026-08-06 会话 05）

## 42. ★★★ 27799 那一层用的是 **SimpleCipher**，不是 SnowCipher

会话 04 的推测（§41「27799 走 gcp/gsp + SnowCipher」）**只对了一半**：
opcode 体系确实是 gcp/gsp，但流加密是 `SimpleCipher`（FINDINGS §30），
**不是** `CipherStream<SnowCipher>`。

证据：会话 04 那份日志里其实就有答案，只是当时没往下看
（`logs/bshook_20260806_134427_pid24392.log:409`）：

```
★WS2 重定向 -> 127.0.0.1:27799
SNOW    ★Simple::Encrypt#1 this=1F974D2C dst=0019F908 src=0019F908 len=4
SNOW    入  (4 bytes)   37 01 00 00
```

即 27799 的首包 `53 72 8f 7f`（`logs/auth_003_27799.bin`）的明文是 `37 01 00 00`
= **311**，正是版本号（安装包就叫 `PopShot_Setup_311.exe`，`UserConfig.ini` 里也是 `Ver=311`）。

### 两个方向各一把 cipher，初始状态不同

构造函数 `0x5bc798` = `TcpConnection::TcpConnection`：

```
this+0x87c  发送方向 SimpleCipher   i1=0, i2=1     ← 客户端 -> 服务端
this+0x888  接收方向 SimpleCipher   i1=5, i2=3     ← 服务端 -> 客户端
```

表基址（从镜像取，与 §30 抄录一致）：`tblA @0x64dd64` 49 字节 / `tblB @0x64dd98` 24 字节。
`i1 % 49`（`idiv 0x31`）、`i2 % 24`（`idiv 0x18`），
**状态跨调用保持 —— 整条 TCP 流是一个连续流**。

`server/simple.py` 已复刻并自检通过（`37 01 00 00` 与 `53 72 8f 7f` 逐字节互转）。

## 43. ★★★ `TcpConnection` 的两种帧格式（接收循环 `0x5bcb19`）

收到的字节**先整体解密，再分帧**（`0x5bcb81` 调 cipher 虚表槽[3] Decrypt，
dst = 接收缓冲尾部，src = recv 原始缓冲 `this+0x74`，recv 一次最多 0x800 字节）。

```
0xFE 控制帧（4 字节头）—— 判定 0x5bcb95，要求 buf[0]==0xFE
    [0]     0xFE
    [1]     未用
    [2..3]  u16 LE 载荷长度      总长 = 载荷长 + 4
    [4..]   载荷                 -> 虚表槽12 (+0x30)

0xFF 游戏帧（10 字节头）—— 判定 0x5bcc02，要求 buf[0]==0xFF
    [0]     0xFF
    [1]     未用
    [2..3]  u16 LE 载荷长度      总长 = 载荷长 + 10
    [4..5]  u16 = 0
    [6..7]  u16 = 0              （TcpConnection::Send 0x5bc9c1 固定传 0）
    [8..9]  u16 = opcode ★       （RawPacket::SetType 0x5bba0a 写这里）
    [10..]  载荷                 -> 虚表槽13 (+0x34)
```

出站头由 `0x5bb9e7` 写（magic FF + 长度 + [6]），`0x5bba0a` 写 opcode。
接收循环先试 0xFE 再试 0xFF，两个都不匹配就等更多数据。

载荷里的基本类型（RawPacket 读写原语）：

```
0x5d59ff ReadInt32(4字节小端)   0x5d5984 ReadInt32 返回值
0x5d596f ReadUInt16            0x5d59de ReadBool(读int32, !=0 存 1 字节)
0x5d5b3a ReadString / 0x5d5a5a WriteString  <- u16 字符数 + UTF-16LE（与 NMCO 同）
0x5d591f WriteInt32            0x5d5a4c WriteByte-as-int32
0x5d5d05 Seek(off, whence)     0x5d5ce7 Resize
```

## 44. ★★★ 27799 的握手序列（实测跑通）

```
客户端 connect 27799
客户端 -> 4 字节 int32 = 311   （裸的，不带帧头；0x54d965 = ServerConnection 虚表槽7 OnConnect）
服务端 -> 0xFE 控制帧，载荷 = int32 结果码
        虚表槽12 = 0x54dbf6：读 int32
            == 0 -> 0x54d67c：(1) 调槽15 0x54d520 发 opcode 0x0100 登录包
                              (2) 有 Dump\LastCrashReport.txt 就发出去
                              (3) 发 opcode 0x0103（崩溃报告标志）
                              (4) 置 [conn+0x894] = 1
            != 0 -> 再读一个字符串，走升级/报错分支（GetComputerName + 弹框）
客户端 -> 0xFF opcode 0x0100  Packet_gcpReqLogin
客户端 -> 0xFF opcode 0x0103  （载荷 int32(0) + int32(0)）
服务端 -> 0xFF opcode 0x0100  Packet_gspRepLogin
        结果码 0 -> 0x54f4af：置 [conn+0x898] = 2，new(0x4a8) 建大厅对象存进全局 [0x72e29c]，
                              向状态机 [0x72e320] post 一个事件
        1 -> 0x54f468   2 -> 0x54f416   3 -> 0x54f3cf（断开）
```

**实测结果**：客户端接受了全部三步，**把 `LastLoginId=testuser` 写进了
`game_patched/UserConfig.ini`** —— 游戏服登录确实成功了。

### `Packet_gcpReqLogin`（0x0100，客户端发，34 字节，实测）

```
00 00                    string 长度 0（空串 = 认证服给的票据，我们回的是空串）
c0 a8 0b d7              本机内网 IP（192.168.11.215），按字节存
00 00 00 00
d8 bb c1 21 7e 89        疑似网卡 MAC（对应 §2.1 的 iphlpapi!GetAdaptersInfo）
ef 80 c7 8e 00 00 00 00
0d 57 01 00              0x1570d
00 00 00 00  00 00
```

序列化函数 `0x558d34`：`SetType(0x100)` -> `WriteString` -> 循环 `WriteInt32`（一个 int32 vector）
-> `WriteInt32(0)` -> 再调一个 functor（`this+0x10`，vftable 0x6916a4）写尾部。

### `Packet_gspRepLogin`（0x0100，服务端发）字段表

`Deserialize` = vftable `0x6915d8` 槽[1] = `0x54c35c`，读取顺序：

```
int32   结果码      (+0x04)   0=成功 1 2 3=断开
string              (+0x08)
string              (+0x0c)
int32               (+0x10)   -> 全局 0x72e338
int32               (+0x14)   -> [conn+0x89c]
int32               (+0x18)   -> [conn+0x8a0]
int32               (+0x1c)   -> 全局 0x72e33c
int32               (+0x20)   -> 全局 0x72e340
int32               (+0x24)   -> 全局 0x72e344
int32               (+0x28)   -> [0x72e2a4]+0x64
int32               (+0x2c)   -> 全局 0x72e354
string              (+0x30)   -> 全局 0x72e37c
int32               (+0x34)   -> 全局 0x72e378
byte                (+0x38)   -> 全局 0x72e358
byte                (+0x39)   -> 全局 0x72e380
```

全填 0 / 空串共 48 字节，客户端照单全收（D019 再次成立）。

## 45. ★★★ 客户端 -> 服务端 opcode 表（静态全量提取）

方法：`RawPacket::SetType(u16)` = `0x5bba0a`，109 个调用点，
每个点往前找 `push <imm>` 拿 opcode，往附近找 vftable 赋值拿类名。
94 个拿到 opcode，45 个带类名。工具：`tools/gcp_opcodes.py`。分组规律很清楚：

| 段 | 含义 |
|---|---|
| 0x00xx | RelayConnection 用（0x0000/0x0001/0x0003） |
| 0x01xx | 系统：登录/ping/反作弊上报/调试登录 |
| 0x02xx | 频道内会话（房间列表、快速加入、用户列表） |
| 0x03xx | 房间内（聊天、踢人、开局、闯关记录） |
| 0x04xx | 战斗内（复活、换图、闯关分数、结算） |
| 0x05xx | 伤害统计 |
| 0x06xx | 道具/商店/礼物/合成 |
| 0x07xx | 个人信息 |
| 0x08xx | 频道切换/频道列表 |
| 0x09xx | 邀请 |
| 0x0axx | 全服公告 |
| 0x0bxx | GameGuard / HackShield |

已确认类名的（其余见 `server/gameserver.py` 的 `GCP_NAMES`）：

```
0x0100 gcpReqLogin          0x0104 gcpRepPing          0x0105 gcpStopPing
0x0106 gcpReportHack        0x0109 gcpReqDbgLogin      0x010a gcpRepDbgLoginId
0x0200 gcpReqListSession    0x0205 gcpReqQuickJoinSession   0x0208 gcpReqMaster
0x020b gcpReqMoveChannelByGameType  0x020d gcpReqUserList   0x020e gcpReqNotAcceptWhisper
0x0305 gcpSendChatMsg       0x030b gcpKickOut          0x030f gcpReqFirstUserResult
0x0310 gcpStartTcpRelay     0x0311 gcpReqQuestRecord
0x040f gcpEndQuest          0x0410 gcpUpdateQuestScore 0x0411 gcpReqChangeToNextMap
0x0413 gcpRespawnCharacter  0x0414 gcpRepFirstAidBox   0x0417 gcpMarkQuestSuccess
0x0505 gcpAccumulatedWeaponDamage
0x0606 gcpReqComposeItem    0x0607 gcpReqGiftList      0x0609 gcpReqGiftAction
0x0705 gcpReqMyInfo         0x0800 gcpReqMoveChannel   0x0802 gcpMoveChannelTest
0x0803 gcpReqChannelList    0x0900 gcpReqUserInfo      0x0901 gcpInviteCancel
0x0902 gcpReqInvite         0x0903 gcpReqInviteStart   0x0a01 gcpNoticeAllServer
0x0b01 gcpRepGameGuard      0x0b02 gspReqHackShieldCheck
```

## 46. ★★ 服务端 -> 客户端的分发（`ServerConnection` 虚表槽13 = `0x54e036`）

opcode 是 `u16 @ [RawPacket->buf + 8]`（`0x54e094: movzx eax, word ptr [eax+8]`）。
**收发共用同一套 opcode 编号**（0x0100 既是登录请求也是登录应答）。

分发前先给三个全局对象过一遍（谁先 `return true` 谁吃掉）：
`[0x72e29c]`（登录后新建的大厅对象）-> `0x4061e2`；`0x409f0e()` -> `[vft+0xc4]`；
`[0x72e28c]` -> `0x54b634`。都没吃才进 `0x54e036` 自己的 switch：

```
0x0002 -> 0x553b89        0x0100 -> 0x54f2cc (RepLogin)   0x0102 -> 槽17 0x54f23a
0x0103 -> 槽16 0x54f025   0x0104 -> 0x55304c (ping)
0x0200 -> 0x54f596   0x0201 -> 0x54f6fb   0x0202 -> 0x54fd07   0x0203 -> 0x54fffe
0x0204 -> 0x5500a5   0x0205..0x020c -> 跳表 @0x54e56a   0x020d -> 0x553c5f
0x02e0 -> 0x54e281   ...   0x0404 -> 0x54e300
```

`[0x72e29c]` 的处理器 `0x4061e2` 吃 0x0300..0x0311（跳表 @0x406332）和 0x0401。

## 47. ★ ServerConnection 关键虚表槽（vft `0x6916fc`，对象 >= 0x8a4 字节）

| 槽 | +off | va | 作用 |
|---|---|---|---|
| 6 | 0x18 | 0x553031 | SendPacket |
| 7 | 0x1c | 0x54d965 | **OnConnect** —— 发 4 字节版本号 311 |
| 8 | 0x20 | 0x54da2e | OnDisconnect（置 [+0x898]=0） |
| 9 | 0x24 | 0x5bcb19 | 接收循环（继承自 TcpConnection） |
| 12 | 0x30 | 0x54dbf6 | **收 0xFE 控制帧** |
| 13 | 0x34 | 0x54e036 | **收 0xFF 游戏帧（主分发）** |
| 15 | 0x3c | 0x54d520 | 组并发 opcode 0x0100 登录包 |
| 16 | 0x40 | 0x54f025 | 收 0x0103 |
| 17 | 0x44 | 0x54f23a | 收 0x0102 |

对象字段：

```
+0x054  接收缓冲(vector)      +0x05c 缓冲数据指针     +0x060 发送缓冲
+0x074  recv 原始缓冲(0x800)  +0x87c 发送 cipher      +0x888 接收 cipher
+0x894  byte  「登录流程已走完」标志（0x54d8f6 置 1）
+0x898  dword 连接状态 0=断开 1=已连接 2=已登录
+0x89c / +0x8a0  来自 gspRepLogin 的两个 int32
```

全局：`[0x72e30c]` = 主 ServerConnection，`[0x72e29c]` = 登录后建的大厅对象，
`[0x72e320]` = 应用状态机，`[0x72e2a4]` = 另一个全局上下文（+0x60/+0x64/+0x68 是等待标志）。

## 48. 登录成功后客户端的等待循环 `0x410950`

```
循环体（每轮 Sleep(50)）退出条件，满足任一即 return 1：
    (1) [0x72e30c] != 0 且 [conn+0x894] != 0 且 [conn+0x898] == 2
    (2) [0x72e2a4]+0x60 != 0
    (3) [0x72e2a4]+0x68 != 0
    (4) arg[0x18] == 2  -> return 0
```

`[0x72e2a4]+0x60 = 1` 由**收到 opcode 0x0103** 触发（0x54f025 里 `0x409f0e()` 返回 0 那条路）。

## 49. ★★★ 登录成功后卡死的真凶 = D3D 显示模式枚举整数下溢（已解决，与协议无关）

现象：登录成功（`UserConfig.ini` 已写 `LastLoginId=testuser`），登录对话框消失，
但主窗口 `MoleWnd` 一直 `IsWindowVisible == false`，进程稳定吃满一个核、磁盘读为 0，
从外部 `ShowWindow` 会阻塞（主线程根本没在泵消息）。

**定位手法（新工具 `tools/thread_stacks.py`，不用重编 bshook）**：
64 位 Python 用 `Wow64GetThreadContext` 拿 32 位线程的 EIP/EBP，再走 EBP 链。
主线程栈固定为：

```
d3d9.dll 内部  <- BigShot+0x1bfb85 <- BigShot+0xd4c9 <- BigShot+0xa68e <- BigShot+0x1f7b9e
```

即反复调用 `IDirect3D9::EnumAdapterModes`（`0x5bfb82: call [ecx+0x1c]`，虚表槽7）。

**根因**（`0x5bfad4` = 建/重建 D3D 设备的函数）：

```
0x5bfb3c: eax = [ebp+0x10]        ; 参数3 = ColorDepth
0x5bfb3f: neg eax / sbb eax,eax / add eax,0x17
                                  ; ColorDepth!=0 -> 0x16 = D3DFMT_X8R8G8B8(32位)
                                  ; ColorDepth==0 -> 0x17 = D3DFMT_R5G6B5 (16位)
0x5bfb4d: cmp byte [ebp+0x14],0 / je 0x5bfbb0
                                  ; 参数4 = FullScreen；窗口模式直接跳过枚举
0x5bfb56: call [edx+0x18]         ; GetAdapterModeCount(Adapter, Format) -> eax
0x5bfb59: dec eax                 ; ★ 没判 0
0x5bfb5a: cmp eax, 0 / jbe 跳过   ; ★ 无符号比较
0x5bfb67..0x5bfbae                ; 循环 EnumAdapterModes，i < [ebp-0x18]
```

现代 NVIDIA 驱动**不再报告任何 16 位色（R5G6B5）显示模式**，
`GetAdapterModeCount` 返回 **0** -> `dec eax` 下溢成 `0xFFFFFFFF` ->
`jbe`（无符号）不成立 -> 循环上界 = **4,294,967,295**，每轮还调一次 D3D API。
实测活进程里读到的循环变量（`probe_modeloop.py`）：

```
i = 228,007,655 -> 232,518,464   （每 0.4 秒才 +64 万）
总数 = 4294967295    目标 = 1024x768   当前最佳刷新率 = 0
```

按这个速度要跑 **40 多分钟**，看起来就是死机。

**为什么第一次运行才会中招**：`game_patched/UserConfig.ini` 是客户端**登录成功那一刻才写出来**的。
首次运行没有这个文件，用的是内置默认值（全屏 + 16 位色），于是走进枚举分支。

### ★ 修复：改配置，不用打补丁

`game_patched/UserConfig.ini`：

```
FullScreen=0      # 窗口模式 -> 0x5bfb4d 的 je 直接跳过整个枚举
ColorDepth=1      # 32 位色 -> 即使全屏也会查 X8R8G8B8，模式数不为 0
```

两条各自都能避开，一起改最保险。改完实测：主窗口 1024x768 显示、
D3D 设备创建成功（多出一个 `D3DProxyWindow`）、大厅完整渲染。

**这条留给以后**：真要支持全屏 + 16 位色，就 patch `0x5bfb65` 的 `76 49`（jbe）成
`eb 49`（jmp），等于「模式数为 0 时不进循环」。本会话没打这个补丁 —— 配置能解决就不动代码。

## 50. ★★★ 里程碑 B 达成：原版大厅 + 训练场都跑起来了（实测截图）

服务端只回了「版本 OK + gspRepLogin(result=0，其余全 0)」，客户端就一路走到底：

1. **游戏大厅**完整渲染（`logs/shot_lobby.png`）：
   「对战 / 任务」标签页、房间列表、建立房间、金币 0、
   底部 主菜单/后退/商店/合成/仓库/任务/礼物箱/情报/设定/查询/好友
2. 弹出「活动」公告窗（韩文原版素材）+ 「新手向导」：
   「欢迎来到炮炮火枪手的世界！我们已经为你准备了射击教学课程。」
3. 点「确认」-> **直接开始加载训练场**（画面显示「正在载入 26%」）
4. 加载完成后**进入训练场场景**（`logs/shot_training3.png`）：
   角色带血条站在地图上、NPC 教官对白、WASD 移动 + 鼠标「攻击/奔跑」键位教学、
   教到哪个键哪个键就高亮

**结论：闯关/训练场的内容确实在客户端本地**（印证 §12），
服务端在单机内容里几乎不需要做事。

进大厅后客户端的稳态行为（每 10 秒轮询一次，我们目前一个都没回，不影响）：

```
0x0200 gcpReqListSession  载荷 12 字节   房间列表
0x020d gcpReqUserList     载荷  5 字节   频道用户列表
0x0700 (?)                载荷  0 字节
```

## 51. 操作游戏内 UI 只能真点鼠标

大厅和游戏里的按钮是 **D3D 自绘**，不是 Win32 控件 ——
`tools/gui_probe.py` 那套 `EnumChildWindows` + `BM_CLICK` 到这里完全失效
（`EnumWindows` 只看得到 `MoleWnd` 和 d3d9 自建的 `D3DProxyWindow`）。

`tools/click.py`：`SetCursorPos` + `mouse_event`，坐标用窗口客户区坐标（窗口在 0,0 时等于屏幕坐标）。
`tools/screenshot.py`：BitBlt 屏幕 DC（**不能用 PrintWindow**，D3D 画面会抓成全黑），
并且要先把窗口拉到前台 —— 光 `SetForegroundWindow` 不够，
得叠 `SwitchToThisWindow` + `SetWindowPos(HWND_TOPMOST)`，
而且游戏窗口**失去焦点时会自己最小化**，抓图前要先 `IsIconic` 判断 + `SW_RESTORE`。

---

## 52. ★★ 大厅两个轮询包的应答（已实测被接受）

进大厅后客户端每 3~10 秒轮询。两个已经能正确应答（`server/gameserver.py`）：

### `0x0200` 房间列表

请求载荷 12 字节。应答反序列化 `0x559009`：

```
u16 房间数 n       ← n <= 0 时 0x559023 的 jle 直接跳过整个循环
n 次 { 每项 new(0x30) ... }
u16                ← 循环之后还读一个，存到 [ebx+0x38]
```

**空列表 = 4 字节 `00 00 00 00`**。实测客户端接受，且回了之后轮询周期从 10 秒缩到 3 秒
（说明它认为自己在「房间列表页」并在主动刷新）。

### `0x020d` 频道用户列表

请求载荷 5 字节。应答反序列化 `0x54d0d3`（构造在 `0x54d10c`）：

```
int32              ← == 0 时 0x553c80 的 je 跳过后面整段列表处理
string  (u16 字符数 + UTF-16LE)
string
int32（当 bool 用，0x5d59de）
```

**空列表 = 12 字节** `00000000 0000 0000 00000000`。实测接受。

### 还没回的

`0x0700`（请求载荷 0 字节）—— 不回也不影响，客户端照常进大厅、进训练场。

**另外**：客户端**不会因为服务端长时间不应答而断线**（实测挂了 30+ 分钟，连接一直在）。

## 53. ★ 新手向导会强制进训练场

大厅弹的「新手向导」对话框，**点「确认」和点右上角 X 都会进射击教学（训练场）**。
想留在大厅里做别的，得先让它进一次教学再退出来，或者找到那个「已看过教学」的标志位。

登录 → 大厅完全渲染大约需要 **100 秒**（读 `Pack\*.pkn`，`bshook` 日志里能看到
上千次 `SnowCipher::loadkey`）。中间会停在原版标题画面
（`Version 311, Build Date 03:17:29 Oct 6 2007`）显示「正在载入」。
**测试脚本里等待时间要给足，别以为卡住了。**

> ⚠ **会话 14 更正**：那 100 秒**不是解 pkn 慢**，是 `bshook` 的日志在等
> `FlushFileBuffers`（每条 2 毫秒 × 上万条）。修掉之后**同一条链路只要 14.6 秒**，
> 连开着逐包 dump 也只要 15.1 秒。见 §105。上面这句「等待时间要给足」已作废。

## 54. ★★★ 新手教程完成状态已定位：`gspRepLogin +0x28`，完成值 = 3

用户提出的「由假后端返回 flag，让客户端自己跳过教程」可直接实现，不需要改客户端。

`Packet_gspRepLogin::Deserialize`（`0x54c35c`）读取的 8 个连续 `int32` 中，
第 7 个（包对象 `+0x28`）在登录处理器里写入全局上下文 `[0x72e2a4]+0x64`。
大厅初始化函数 `0x43b11b` 对它做硬判断：

```asm
0043b34f  mov eax, [0x72e2a4]
0043b354  mov eax, [eax+0x64]       ; 新手教程状态
0043b357  cmp eax, 3
0043b35a  jge 0x43b475              ; >=3：跳过新手教程弹窗
                                       <3：构造并显示下列教程弹窗
```

`<3` 分支引用的资源/文本进一步钉死了语义：

```text
0x665194  Images/NewUI/FirstUserIcon.png
0x665188  "튜토리얼"（教程）
0x6651d8  "欢迎来到 BigShot 的世界……已为大家准备射击战课程……"
```

该分支最后调用 `0x40f4df`，正是点「确认」或 X 后强制进入教学的路径。
因此假后端应使用易编辑的 `tutorial_completed` 布尔值作为存档字段，并编码为：

```text
tutorial_completed = false  -> gspRepLogin 第 7 个业务 int32 = 0
tutorial_completed = true   -> gspRepLogin 第 7 个业务 int32 = 3
```

不要误用登录包最后一个 bool（对象 `+0x39` / 全局 `0x72e380`）：它控制的是
`Images/Event/Itemx2SaleEvent.png` 活动弹窗，不是教程状态。

实现已落在：

```text
server/account_store.py       两个服务进程共享的原子 JSON 读写层
server/data/accounts.json     可直接编辑的本地账号存档
server/authserver.py          登录时自动建档并记录 active_account
server/gameserver.py          每次游戏服登录重读存档，组 gspRepLogin
server/test_account_store.py  持久化与协议编码测试（3 项通过）
```

当前只支持一个 localhost 客户端，因此用根级 `active_account` 在认证服和游戏服之间交接身份；
未知账号默认 `tutorial_completed=false`，预置 `testuser` 为 `true`，便于直接推进闯关主线。

## 55. 教程 flag 实机验证途中遇到的是系统级 D3D9 HAL 暂时不可用，不是协议失败

服务端实机日志已经确认：登录时读取 `testuser`，只把 `gspRepLogin` 第 7 个业务 int32
写成 `3`，其余未知业务 int32 继续保持 `0`；客户端正常接受登录包并进入图形初始化。
当前进程随后退出的直接原因是客户端弹「图像引擎初始化失败」。

为避免把图形环境问题误判成教程字段问题，`bshook` 新增了只读诊断：

```text
RendererInit(1024, 768, ColorDepth=1, modeFlag=0/1)
IDirect3D9::CreateDevice 每次调用的参数和 HRESULT
GetAdapterDisplayMode / CheckDeviceType
```

已测得：

```text
适配器模式：1920x1080@60，D3DFMT_X8R8G8B8(22)，1 个 adapter
MoleWnd：有效顶层窗口，client=1024x768
HAL + mixed VP (0x44)      -> 0x8876086C D3DERR_INVALIDCALL
HAL + software VP (0x24)   -> 0x8876086A D3DERR_NOTAVAILABLE
SW device type 3 (0x24)    -> 0x8876086C D3DERR_INVALIDCALL
CheckDeviceType(HAL, X8R8G8B8, windowed) -> D3DERR_NOTAVAILABLE
```

`tools/d3d9_probe.c/.exe` 用一个与游戏无关的全新可见窗口做了 32 位 D3D9 最小复现；
即使 `BackBufferFormat=UNKNOWN`、不建深度缓冲、flags=0，HAL 仍返回同样的
`D3DERR_NOTAVAILABLE`。因此该时刻的失败是**当前 Windows 控制台会话/驱动层不提供
D3D9 HAL**，不是 BigShot 参数、注入钩子、账号 JSON 或教程状态造成的。

同一台机器 15:27 的已成功日志里出现过可见 `D3DProxyWindow` 并完整渲染大厅，说明这是
当前图形环境的回归/暂态；下一步测试 D3D9 REF 参考光栅器作为兼容回退，恢复画面后再完成
`tutorial_completed=true` 的 UI 验收。

另外，当前二进制的配置名语义是反的（动态参数已确认）：

```text
UserConfig.ini FullScreen=0 -> D3DPRESENT_PARAMETERS.Windowed=0
UserConfig.ini FullScreen=1 -> D3DPRESENT_PARAMETERS.Windowed=1
```

两种模式在当前 HAL 不可用状态下都失败，所以这不是本次根因；但后续窗口模式应预置
`FullScreen=1`，`ColorDepth=1` 仍用于避开 16 位显示模式枚举下溢。

## 56. `0x0201 gcpReqCreateSession` 的最小成功应答 = 两个 int32

客户端在三个建房对话框路径中都会发送 opcode `0x0201`。服务端同 opcode 的处理入口
`0x54f6fb` 先调用 `0x5590bb`，后者只读取：

```text
int32 result
int32 session_id
```

`result != 0` 走错误弹窗；`result == 0` 才进入 `0x54f7da` 的成功路径，创建/切换
`RoomStage`。第二个值写入 `LobbyStage+0x1c8`，作为 session id。

已在 `server/gameserver.py` 接入：

```text
0x0201 -> gspRepCreateSession { result=0, session_id=1 }
```

`server/test_gameserver.py` 覆盖包体和 `0xFF` 帧往返；连同账号测试当前共 5 项通过。

## 57. `0x0311 gcpReqQuestRecord` 的应答是固定至少六项的 int32 数组

大厅包分发器 `0x4061e2` 的跳表项 `0x0311` 落到处理器 `0x408a1c`。该处理器实际构造的是
`Packet_gspRepQuestRecordInPvp`（vft `0x65e14c`），不是名字相近但结构更复杂的
`Packet_gspRepQuestRecord`（vft `0x66a37c`）。

反序列化槽 `0x404fb9` 调用通用 `vector<int32>` 读取器 `0x408fc9`，精确包体为：

```text
int32 n
int32 records[n]
```

读取后，`0x408a52..0x408a7c` 固定遍历六种游戏类型，并在对应类型启用时直接读取
`records[0]` 到 `records[5]`，没有检查 `n`。所以不能按“空记录”直觉发 `n=0`；
**最小安全应答必须包含六项**。各项语义尚未查明，按 D019 暂填 0：

```text
06 00 00 00  00 00 00 00 × 6     （总计 28 字节）
```

`server/gameserver.py` 已对 0x0311 回同 opcode 的六项零记录，并在构造器中强制长度必须为 6；
`server/test_gameserver.py` 覆盖包体、帧往返和错误长度拒绝。连同账号测试当前共 **8 项通过**。

## 58. ★ 开局握手已闭合：`0402 → 0401 → 0402 → 0412 → 0400`

此前把 `Packet_gspPrepareGame` 误记成 opcode `0x0401`，原因是读取主分发器
`0x54e28c..0x54e2c6` 的连续减法分支时少还原了一步。重新逐条回算后，准确映射是：

```text
0x0400 -> 0x551605 -> Packet_gspPrepareGame (vft 0x69165c)
0x0401 -> 大厅 0x4074de -> Packet_gspTriggerCountGame (vft 0x65e0e0)
0x0402 -> 0x5517d0 -> 无包类、空载荷的同步确认
0x0412 -> 游戏/房间上下文 0x493755 -> Packet_gspRepCountDown (vft 0x670c88)
```

三个有类的包都只反序列化一个 `int32`。客户端侧完整因果链可以静态闭合：

1. 普通房主按钮在 `0x469561..0x46956c` 经 `0x468b8b(0)` 发送空载荷 `0x0402`。
2. 收到 `0x0401 { int32 result }` 后，大厅处理器 `0x4074de` 对 result=2/3 显示天梯维护错误；
   正常值 0 调 `0x468b8b(1)`，客户端会再发一次空 `0x0402`，作为房间成员确认。
3. 收到空 `0x0402` 的主处理器 `0x5517d0` 记录当前时间/同步状态。
4. 收到 `0x0412 { int32 state=0 }` 后，`0x493755` 调 `0x468e78(0)`，在房间 UI 中启动
   约 6 秒倒计时；state=1 会停止/复位。
5. 房间 UI 更新函数 `0x4690e3` 在倒计时归零后，经 `0x468d0a` 发送空 `0x0400`。
6. 服务端再下发 `0x0400 { int32 seed }`，主处理器 `0x551605` 切换到加载态。

`PrepareGame.seed` 最终作为 `0x40b6e5` 的第 5 个参数：`-1` 使用线程随机源，其他值构造
确定性随机源，从候选地图/出生数据中选同一项。假后端只有一个客户端，使用 seed=0 既合法又可复现。

`server/gameserver.py` 已实现 `StartGameHandshake`：第一次 `0x0402` 回正常
`TriggerCountGame`；第二次 `0x0402` 回空同步确认并启动倒计时；倒计时后的空 `0x0400`
回 `PrepareGame(seed=0)`。重复、提前或非空异常包不会推进状态。真实服务器需要等待房间内所有玩家，
当前按项目范围只实现一个 localhost 客户端。包构造、帧往返、完整状态迁移和异常顺序均有测试；
连同账号测试当前共 **12 项通过**。受 FINDINGS §55 的系统级 D3D9 故障影响，真实 UI 开局尚待恢复画面后验收。

## 59. ★ 房间类型枚举已确认：`1=普通 / 2=闯关 / 5=天梯`，`0x0416` 不是闯关开局

`Packet_gcpReqCreateSession`（vft `0x664e94`）的序列化槽 `0x43abc1` 依次写：

```text
string text_1
string text_2
string text_3
int32 option
SessionDescriptor descriptor
```

最后的 `SessionDescriptor` 使用 vft `0x65e09c`，序列化槽 `0x557374` 先写 `int32 type`，
再按 type 写 1～3 个 int32 参数。三条建房路径的静态赋值把类型枚举闭合了：

| 建房入口 | 对话框 RTTI | descriptor.type | 证据 |
|---|---|---:|---|
| `0x43b8e6` | `DlgCreateRoom` | 1 | `0x43bba7..0x43bbbe` |
| `0x43be28` | `DlgCreateQuestRoom` | 2 | `0x43be78` 令 `ebx=2`，随后写到 descriptor+4 |
| `0x43c18f` | `DlgCreateLadderRoom` | 5 | `0x43c2b3 / 0x43c35e` 令 `ebx=5`，随后写到 descriptor+4 |

大厅建房按钮的选择器 `0x4410ee..0x441107` 也独立对上三条路径：模式变量默认走普通，
值 1 调 `DlgCreateQuestRoom`，值 2 调 `DlgCreateLadderRoom`。建房成功后这份 descriptor 被保存在
`LobbyStage+0x18`，所以房间代码读取的 `[LobbyStage+0x1c]` 正是 `descriptor.type`。

因此 `0x469356` 的开始按钮分支含义已经确定：

```text
[LobbyStage+0x1c] == 5  -> 0x468b3e -> 客户端发空 0x0416（天梯专用）
其它类型（包括 2=闯关） -> 0x468b8b(0) -> 客户端发空 0x0402
```

同号的服务端→客户端 `0x0416` 却由 `0x553a38` 构造
`Packet_gspUpdateQuestDifficulty`（vft `0x691674`），反序列化槽 `0x54cfbf` 读取两个 int32。
它与客户端发出的空天梯请求是**方向不对称的两种包**，不能回显，也不能拿来启动闯关。

实现结果：`server/gameserver.py` 现在精确解析 0x0201 请求并记录
`normal / quest / ladder`，每次建房同时重置 `StartGameHandshake`；日志表把 `0x0416`
修正为 `rawLadderStartGame`，不再把 `0x0401` 误标为客户端特殊开局。闯关 type=2 继续使用
FINDINGS §58 的普通倒计时链。解析、截断/尾随拒绝、枚举区分和状态重置均有测试；
连同账号测试当前共 **15 项通过**。

## 60. D3D9 故障不是 32 位驱动特例；当前主输出同时挂着物理内屏与虚拟目标

`tools/d3d9_probe.c` 已增加 D3D9 adapter identifier 与 `EnumDisplayDevices` 输出，并分别编译
32 位和 64 位版本做对照。两者均直接枚举同一块实体显卡，而不是把虚拟适配器当成 D3D9 adapter：

```text
x86: NVIDIA GeForce RTX 3070 Laptop GPU / nvldumd.dll  / \\.\DISPLAY1
x64: NVIDIA GeForce RTX 3070 Laptop GPU / nvldumdx.dll / \\.\DISPLAY1
```

但两种位数的最小窗口、HAL/REF、默认 D3D9On12 都同样在 `CheckDeviceType` 或 `CreateDevice`
返回 `0x8876086A D3DERR_NOTAVAILABLE`；显式创建 D3D12 WARP device/queue 则成功，接到
D3D9On12 后仍无法创建 D3D9 device。因此可排除“只有旧客户端需要的 32 位 NVIDIA 组件损坏”，
也再次排除 BigShot 自身参数。

当前 Win32 显示枚举给出了一个异常且可操作的相关项：主适配器 `DISPLAY1` 的 flags 为
`0x00080005`，其下两个 monitor **同时**为 active/attached（flags `0x00000003`）：

```text
MONITOR\SHP14EC  — 笔记本物理内屏
MONITOR\XMD009A  — 当前虚拟显示目标
```

系统还枚举到多组未附着桌面的 `GameViewer Virtual Display Adapter` 与 `Meta Virtual Monitor`；
WMI 的视频控制器信息同时报告 `Meta Virtual Monitor` 正在使用 1920×1080@60。结合本机更早同日
曾成功创建 D3D9 并渲染大厅，**最高概率解释**是当前虚拟显示/镜像拓扑使 legacy D3D9 路径回归为
不可用。不过这仍是待验证因果：必须在退出虚拟显示软件、`Win+P → 仅电脑屏幕`（必要时手动临时
禁用虚拟显示设备）后重跑探针；只有 HRESULT 恢复为 `00000000` 才算坐实。

按 D027 不自动改动系统显示设备，避免误禁 NVIDIA 或令用户失去画面。探针恢复后再跑完整客户端，
依次验收 `tutorial_completed=true` 是否跳过弹窗、闯关 type=2 建房、倒计时和加载后的下一批请求。


## 61. ★★★ D3D9 HAL 故障的根因坐实 = 串流软件（Sunshine/Moonlight）占用显示路径

用户退出 Sunshine / Moonlight 远程画面后，**不做任何其它改动**（没有 `Win+P`、没有禁用
任何显示设备、`XMD009A` 仍然 active/attached），重跑 `tools/d3d9_probe.exe`，
全部 HRESULT 立刻恢复为 `00000000`：

```text
CheckDeviceType HAL/X8R8G8B8/windowed  hr=00000000
minimal unknown format  type=1 behavior=00000024 hr=00000000
BigShot exact           type=1 behavior=00000024 hr=00000000
BigShot exact           type=1 behavior=00000044 hr=00000000
BigShot exact REF       type=2 behavior=00000024 hr=00000000
9On12 / WARP 9On12 各组合                hr=00000000
```

因此 FINDINGS §60 的推断要**精确化**：

- 起因**不是**「虚拟显示器附着在 `DISPLAY1` 上」这个静态拓扑本身 ——
  探针恢复成功时 `MONITOR\XMD009A` 依然是活动/附着状态（flags `0x00000003`），
  `GameViewer Virtual Display Adapter` × 10 和 `Meta Virtual Monitor` × 7 也依然被枚举到。
- 真正的开关是**串流软件是否正在运行并接管画面输出**。Sunshine 在会话中捕获桌面时，
  legacy D3D9 的 HAL 设备创建整条路径（含 REF 与 D3D9On12）都会返回
  `0x8876086A D3DERR_NOTAVAILABLE`。

### 操作结论（后续会话直接照做）

> **实机验收画面前，必须先退出 Sunshine / Moonlight 串流。**
> 判据只有一条：`tools/d3d9_probe.exe` 的
> `CheckDeviceType HAL/X8R8G8B8/windowed hr=00000000`。
> 不需要 `Win+P → 仅电脑屏幕`，不需要在设备管理器里禁用任何虚拟显示设备（D027 的
> 「必要时手动临时禁用」这步实测**不必要**）。

代价是「远程串流」与「游戏画面」二者只能取其一，所以涉及画面的验收必须安排在
串流关闭的时段。截图不受影响：`tools/screenshot.py` 走 BitBlt 屏幕 DC，
不依赖串流也能取到画面。


## 62. ★★★ 大厅标签页切换 = `0x020b` 请求 + `0x0701 gspRepMoveInto` 应答（不是回 `0x020b`）

点大厅「任务」标签页时，客户端发 `0x020b gcpReqMoveChannelByGameType`，载荷是**一个 int32**
（实机抓到 `02 00 00 00`），然后**什么都不做地等服务端**。会话 06 之前没回这个包，
所以标签页一直切不过去 —— 表现为「点了没反应」，很容易误判成点击坐标不对。

### 同号包方向不对称：`0x020b` 服务端包只用来报失败

`Packet_gspRepMoveChannelByGameType`（vft `0x6917c0`）只有一个 int32，
序列化/反序列化就是 `0x404ee8` / `0x404ef7`。但处理器 `0x54fbfa` 的**两条分支都是错误提示**
（韩文原版素材）：

```text
result != 0 -> 0x692380 "해당 게임을 즐길 수 있는 채널로의 이동에 실패하였습니다. 에러코드 %d"
                        （移动到能玩该游戏的频道失败。错误码 %d）
result == 0 -> 0x6923d4 "해당 게임을 즐길 수 있는 채널로 이동하지 못했습니다."
                        （未能移动到能玩该游戏的频道。）
```

**所以切频道成功时绝对不能回 `0x020b`**，连 `result=0` 都会弹错误框。

### 成功应答 = `0x0701 Packet_gspRepMoveInto`

分发器 `0x54e4b7` 起的连续减法：`0x0609 -> 0x552593`、`0x0700 -> 0x552a73`、
**`0x0701 -> 0x552b47`**。`0x552b47` 反序列化 vft `0x6915c0` = `Packet_gspRepMoveInto`，
反序列化槽 `0x54c891` 依次读三个字段，**线格式全是 4 字节**：

```text
int32 ok             0x5d59de：先读 4 字节，再 setne 折成 1 字节 bool 存进对象
int32 channel_code   -> [conn+0x89c]
int32 channel_index  -> [conn+0x8a0]
```

处理器随后：`[conn+0x898] = 2`（状态=已登录）→ `0x5545ec` 把频道码翻成游戏类型
→ `0x43b63b` 切换大厅标签页。所以**服务端只要回这 12 字节，标签页就会切**。

### 频道码 ↔ 游戏类型映射（`0x5545ec`，逐分支抄全）

```text
code < 0            -> -1（不在任何可玩频道）
code 0,1,2,3,4      -> 1   普通
code 6              -> 1   普通
code 7              -> 2   ★ 闯关
code 9              -> 5   天梯
code 10 (0xa)       -> 1   普通
其它                -> -1
```

大厅标签页索引 → 游戏类型（`0x43b61a`）：`0 -> 1 普通`、`1 -> 2 闯关`、`2 -> 5 天梯`、`3 -> 6`。

**注意类型 6 是死的**：`0x5545ec` 不会为任何频道码返回 6，所以服务端没有办法把客户端
移进类型 6 的频道，只能不回包（回失败包只会弹原版韩文错误框）。

### 客户端是否发包的判定（`0x43b64d` 起）

```asm
0043b656  mov eax, [0x72e30c]        ; 主 ServerConnection
0043b65b  mov eax, [eax+0x89c]       ; 当前频道码（来自 gspRepLogin 第 2 个业务 int32）
0043b661  call 0x5545ec              ; -> edx = 当前游戏类型
0043b668  call 0x43b61a              ; -> eax = 请求的游戏类型（由标签索引来）
0043b66d  cmp edi, 2                 ; edi = 标签索引；2 = 天梯
0043b670  jne 0x43b729               ; 非天梯标签，跳过等级检查
0043b676  cmp dword ptr [0x72e338], 6
0043b67d  jge 0x43b729
          ; < 6：弹 0x6650d0 "레벨 %d이상만 플레이하실 수 있습니다."（等级 %d 以上才能玩）
          ; 然后直接返回，**不发包**
0043b729  cmp eax, -1  / je 本地路径      ; 请求类型无效
0043b72e  cmp edx, eax / je 本地路径      ; 当前类型 == 请求类型，不用发包
0043b732  push eax / call 0x55395a       ; 发 0x020b，然后返回等服务端
```

**等级 6 的门槛只卡「天梯」标签，不卡「任务」标签。** 闯关不需要提升账号等级。
`[0x72e338]` = `gspRepLogin` 的**第 1 个**业务 int32（包 `+0x10`，见 §44），即玩家等级。

### 另一条会发 `0x020b` 的路径（登录后自动切频道）

`0x54dab0`（登录建大厅对象之后）按 `[conn+0x89c]` 预置一个待切换类型到 `[0x6dc678]`：

```text
0x89c == 7 -> [0x6dc678] = 2      0x89c == 8 -> 6      0x89c == 9 -> 5      其它 -> edi
```

`0x0200` 房间列表应答的处理器 `0x54f596` **开头**会先看 `[0x6dc678]`：不等于 -1 就
发一次 `0x020b` 并把它清成 -1，**同时整包房间列表都不处理**（`jmp 0x54f672`）。

### 服务端实现

```text
server/gameserver.py
    OP_MOVE_CHANNEL_BY_GAME_TYPE = 0x020b / OP_REP_MOVE_INTO = 0x0701
    CHANNEL_CODE_GAME_TYPES / GAME_TYPE_CHANNEL_CODES / LOBBY_TAB_GAME_TYPES
    parse_move_channel_by_game_type() / build_rep_move_into()
    连接对象记 channel_code / channel_index（登录时都是 0 = 普通频道 0）
```

游戏类型 → 规范频道码取 `{1: 0, 2: 7, 5: 9}`；类型 6 不回包。
`server/test_gameserver.py` 覆盖载荷解析、截断/尾随拒绝、12 字节包体、帧往返、
标签 1 → 类型 2 → 频道码 7 → 类型 2 的闭环，以及「类型 6 不可达」这条约束。
连同账号测试当前共 **22 项通过**。


## 63. ★★★ `gspRepLogin` 第 1 个业务 int32 = 玩家等级；等级 0 会让闯关关卡列表整个空掉

「建立房间(任务)」对话框（`DlgCreateQuestRoom`，vft `0x664804`）能打开，但「任务」下拉框是空的
——点箭头只高亮不展开。**根因在服务端**：登录包等级填 0。

### 对话框初始化 `0x4365e1` 里的四个下拉框全部按等级过滤

```asm
00436833  mov ebx, dword ptr [0x72e338]   ; ★ ebx = 玩家等级
00436839  mov [ebp+0xc], ebx
; 1) 房间加密  表 0x72e4e0 × 2 条 -> 控件 [this+0x570]
; 2) 人数      表 0x72e4f8 × 4 条 -> 控件 [this+0x574]
; 3) 任务      见下                -> 控件 [this+0x578]
; 4) 难度      表 0x72e528 × 3/4 条 -> 控件 [this+0x57c]
;    三张静态表的条目都是 12 字节 {名字指针, 值, 要求等级}，
;    循环体统一是 `cmp ebx, [esi+8] / jl 跳过`
```

三张静态表的**要求等级都是 0**，所以它们照常填充（截图里「人数=3」「难度=简单」正常）。
关卡那一路不同：

```asm
004368b3  and dword ptr [ebp+0x10], 0
004368b7  mov eax, [ebp+0x10]
004368ba  push dword ptr [eax + 0x6dc52c]  ; 关卡 id 表
004368c0  call 0x40b6a2                    ; 按 id 在目录里找记录
004368c5  test eax, eax / je 跳过
004368ca  cmp ebx, dword ptr [eax + 0x28]  ; ★ 等级 >= 关卡要求等级
004368cd  jl 跳过
004368cf  mov ecx, [0x72e320] / mov ecx, [ecx]   ; 区域号（活进程实测 = 2）
004368d7  mov esi, [eax + 0x48]            ; 关卡的区域掩码
004368dd  shl edx, cl / test esi, edx / je 跳过
004368e3  ... call 0x435cd0                ; 加进「任务」下拉框
00436901  cmp dword ptr [ebp+0x10], 0x1c / jb   ; 固定 7 轮
```

`0x6dc52c` 的关卡 id 表 = **`3, 2, 1, 4, 5, 6, 7`**（就是这个顺序）。

**活进程实测**（`tools/probe_quest_list.py`，在对话框开着的时候读的）：

```text
[0x72e320] 首字段（掩码位）= 2
目录 [0x72e3d8] 共 32 条记录，名字含 "Quest" 的 6 条：
   id=1 要求等级=1 掩码=0x7      id=2 ×2 要求等级=1 掩码=0x7
   id=3 ×2 要求等级=1 掩码=0x7   id=4 要求等级=1 掩码=0x7
```

即：**目录里有关卡、掩码也通过，只有等级这一关卡住**（`0 >= 1` 不成立）。
把等级填成 1 就能解锁 id 1/2/3/4；id 5/6/7 在当前目录里还没有记录。

### 关卡目录 `[0x72e3d8]` 的结构

是一棵红黑树（STLport set/map），节点 `+0x04 parent / +0x08 left / +0x0c right / +0x14 记录指针`，
`[树头+8]` = 最左节点 = `begin()`，`0x5e4040` 就是标准的 `operator++`。

记录字段：

```text
+0x10  名字（宽字符串对象，第 1 个 dword 是字符指针）
       类型判定 `0x40b1fb(rec, 2)` = 名字里含 "Quest"（宽串 @0x65ebc0）
+0x28  要求等级
+0x40  关卡 id
+0x48  区域掩码，要求 `mask & (1 << [[0x72e320]])`
```

`0x40b6a2(questId)` = 遍历目录，取第一条「类型 2 且 `[rec+0x40]==questId`」的记录。

### `[0x72e338]` 就是等级，另有一处硬门槛

同一个全局在 `0x43b676` 被拿来卡**天梯标签页**：`cmp [0x72e338], 6 / jge`，
不够就弹 `0x6650d0` 的「레벨 %d이상만 플레이하실 수 있습니다」（等级 %d 以上才能玩）。
两处合起来足以确认它是玩家等级，不是别的计数。见 §62 的完整判定链。

### 服务端实现

```text
server/account_store.py   新增 player_level()，容忍缺失/非法值，负数夹到 0
server/gameserver.py      build_gsp_rep_login 第 1 个业务 int32 改用账号等级
server/data/accounts.json testuser 的 level 已经是 1，够解锁闯关列表
```

其余 6 个业务 int32 仍按 D019 保持 0。`server/test_account_store.py` 增加了
「等级与教程状态各自编码到正确位置、其余位置仍为 0」「新账号默认等级 >= 1」
「player_level 对缺失/字符串/负数的处理」三组断言；连同游戏服测试当前共 **24 项通过**。

### 记一笔

**「点了没反应」在这个客户端里经常不是点击坐标问题，而是过滤条件把内容全滤掉了。**
分辨方法：先用 `tools/probe_quest_list.py` 那样直接读活进程的数据源，
确认「数据在不在」，再去查过滤条件。


## 64. ★★ 闯关房建成后客户端崩溃：`DlgSelectPvpMap` 拿 -1 去索引模式名表

等级修好之后，「建立房间(任务)」→「确认」能把请求发出来，服务端也按 §56 回了
`gspRepCreateSession {result=0, session_id=1}`，客户端进入房间，
**约 5 秒后崩溃退出**。

### 抓到的真实建房请求（type=2 闯关）

```text
opcode=0x0201 (gcpReqCreateSession) 载荷 54 字节
  texts = ('想和做朋友吗?', 'Quest03_1', '')
  option = 3
  descriptor.type = 2 (quest)
  descriptor.args = (3, 1)          ← (关卡 id=3, 难度=1)
```

关卡 id 3 就是下拉框里的「神秘岛」（`0x6dc52c` 表的第 0 项），地图名 `Quest03_1`。
**`server/gameserver.py` 现有的解析器逐字段解对了**，type=2 走两个参数这一点得到实机确认。

### 崩溃报告（客户端自带，`game_patched/Dump/LastCrashReport.txt`）

```text
Exception code: C0000005 ACCESS_VIOLATION
Fault address:  0040197F
EAX:00000000  EBX:00000001  ESI:FFFFFFFF  EDI:3BFF5380
Call stack:
  0040197F   <- TString::TString(const wchar_t*) 里对空指针做 wcslen
  004655FA   <- DlgSelectPvpMap 的虚函数（vft 0x66a01c 槽 +0x94 = 0x4652b3）
  00465234   <- call dword ptr [eax+0x94]（虚调用）
  00466F69
  0040BD09
  00426742
  0040A6B9
  005F7B9E   <- 主循环
```

**这个客户端会自己写崩溃报告**（`Dump/LastCrashReport.txt` + `.mdmp`），
带异常码、故障地址、寄存器和调用栈 —— 以后客户端一退就先看这个文件，
比外挂调试器省事得多。

### 崩溃机制（三个描述符访问器对 type=2 全部返回 -1）

`0x4652b3`（房间 UI 刷新）连续取三个标签，各自用 `LobbyStage+0x18` 的
`SessionDescriptor` 去查一张名字表：

```asm
0046556c  cmp ecx, 5                 ; ecx = [LobbyStage+0x1c] = descriptor.type
00465572  je  0x465632               ; ★ 只给天梯(5)开了旁路
0046557b  call 0x409dd9 -> 0x5570f9  ; 表 0x6dc6c8
004655b3  call 0x409df1 -> 0x5570ca  ; 表 0x6dc6c0
004655ec  call 0x409e0a -> 0x55709b  ; 表 0x6dc6a8   ← 崩在这
```

三个访问器的逻辑一模一样：

```text
0x409dd9 / 0x409df1 / 0x409e0a:
    type == 5 -> 0 / 1 / 5
    type == 1 -> descriptor[+0x10] / [+0x8] / [+0xc]
    其它      -> -1              ★ 包括 type 0 和 type 2
```

而 `0x55709b` 这类查表函数是 `push [eax*4 + 表基址]` **不做范围检查**，
索引 -1 就是读表基址前 4 字节：

```text
0x6dc6c4 (表 0x6dc6c8 的 -1) = 0x00664ad4 '팀  전'   ← 越界但非空，侥幸不崩
0x6dc6bc (表 0x6dc6c0 的 -1) = 0x00692c68 '래   더'  ← 同上
0x6dc6a4 (表 0x6dc6a8 的 -1) = 0x00000000            ← ★ 空指针，TString 崩
```

### 已经排除的解释

- **不是**掩码或等级过滤问题：等级修好后关卡列表已经正常出「神秘岛」。
- **不是**建房参数解析错：服务端解出的 type/args 与客户端 UI 完全对得上。
- **不是**「打开了对话框却点不到」：崩溃栈明确是每帧 UI 刷新时发生的。

### 关键疑点（下个会话从这里接）

崩的是 **`DlgSelectPvpMap`**，闯关房本该开 **`DlgSelectQuestMap`**（vft `0x66a19c`，
构造在 `0x465dfc` / `0x465e88`）。二选一发生在建房成功处理器里：

```asm
0054f875  mov eax, [0x72e29c]
0054f87a  mov eax, [eax + 0x1c]     ; descriptor.type
0054f87d  cmp eax, esi              ; esi = 2
0054f884  jne 0x54f8e1              ; != 2 -> PvP 分支
                                    ; == 2 -> 闯关分支
```

既然实际走了 PvP 分支、且三个访问器都返回 -1，**最可能的解释是此刻
`[LobbyStage+0x1c]` 根本不是 2，而是 0**（`LobbyStage` 构造函数 `0x40533a`
只把 `+0x18` 设成 `SessionDescriptor` 的 vftable，type 保持 0）。
也就是说：**客户端的 `LobbyStage` 描述符要靠某一步才会被填上，而我们漏了这一步。**

已经查过、可以排除的填充来源：

- `0x406a92` 是描述符拷贝函数（`ecx` 源 → `eax` 目标），全部 14 个调用点
  都是**从** `LobbyStage+0x18` 拷到栈上临时对象，没有一个是写进去的。
- `Packet_gcpChangeSession`（vft `0x691808`）确实带一个 `SessionDescriptor`
  在 `+0x14`，但它在客户端只有一个构造点 `0x54c228`，唯一调用者 `0x54e5e9`
  是**发送**辅助函数 —— 是客户端发给服务端的，不是我们能下发的。

因此下一步要查的是：**建房成功后，`LobbyStage+0x18` 的 type 到底由谁写成 2**，
是客户端自己在发 `0x0201` 时就该写好（那就要看 `0x43be28` 的 OK 处理器有没有
被我们的应答时序打断），还是要等某个我们还没实现的服务端包。

`SessionDescriptor` 的反序列化槽是 `0x557401`（vft `0x65e09c` 槽 1），
按 type 读 1～3 个 int32，可以直接用来构造服务端下发的描述符。


## 65. ★★★ §64 崩溃的答案：房间的 `SessionDescriptor` 由 opcode `0x0303` 下发

### 实测：描述符从头到尾是 -1，不是 0 也不是 2

新工具 `tools/probe_session_desc.py` 在建闯关房的整个窗口里连续采样
`[0x72e29c]`（LobbyStage）：

```text
[19:54:44] LobbyStage=0x1fb008d0 desc.vft=0x0065e09c✓ type=-1 session_id=0 me=0 host=0
[19:55:00] LobbyStage=0x1fb008d0 desc.vft=0x0065e09c✓ type=-1 session_id=1 me=0 host=0
[19:55:08] LobbyStage 还没建（[0x72e29c] = 0）   ← 崩溃后对象已销毁
```

三条信息一次性坐实：

1. **`session_id` 从 0 变成 1**，说明 §56 的 `gspRepCreateSession` 应答确实生效了。
2. **`me == host == 0`**，说明客户端认为自己是房主，座位分配也正常。
3. **`descriptor.type` 始终是 `-1`** —— 不是 §64 猜的 0。`LobbyStage` 构造函数
   `0x4052ff` 就是把 `+0x18` 设成 `SessionDescriptor` 的 vftable、`+0x1c` 置 `-1`
   （`or dword ptr [esi+0x1c], 0xffffffff`）。**客户端自己永远不会把它填成 2。**

于是 `0x409e0a(type=-1)` 返回 -1 → 越界索引 → 空指针 → §64 的崩溃，完全对上。

### `LobbyStage` 和「房间列表条目」是同一套布局

房间列表条目的构造函数就是 `0x4052ff`（`0x0200` 反序列化里 `new(0x30)` 之后调它），
和 `LobbyStage` 用的是同一个 —— 所以两者 `+0x04..+0x3c` 的字段完全一致，
读取器也共用。

### `0x0303` = 把整个 Session（含描述符）灌进 `LobbyStage`

大厅包分发器 `0x4061e2` 的跳表 `@0x406332` 覆盖 `0x0300..0x0311`，
**索引 3（opcode `0x0303`）→ `0x406756`**：

```asm
00406765  call 0x409f1c / cmp eax,5 / sete [ebp-0xd]   ; 先记下「原来是不是天梯」
0040676d  lea eax, [edi+0x10] ... call 0x402cbe        ; 备份旧地图名
0040677f  lea ecx, [edi+0x18] / lea eax, [ebp-0x34]
00406788  call 0x406a92                                 ; 备份旧描述符
0040678d  mov eax, [ebp+8] / mov ecx, edi
00406792  call 0x556ed1                                 ; ★ Session::Deserialize(this=LobbyStage)
00406797  ...                                           ; 新旧对比，检测换图
```

`0x556ed1` = `0x556e80` 再加一个 `u16`。**完整线格式**（原语宽度已逐个确认）：

```text
int32                    -> +0x04     (0x5d5984 读 4 字节)
string                   -> +0x08     (0x5d5b3a = u16 字符数 + UTF-16LE)
int32                    -> +0x0c
string                   -> +0x10     ★ 地图名（后面拿来比对换图）
int32                    -> +0x14     (0x5d5956 读 4 字节，存成 1 字节)
SessionDescriptor        -> +0x18     (虚槽 0x557401：int32 type + 按 type 读 1~3 个 int32)
u16                      -> +0x3c     (0x556ed1 追加的)
```

原语宽度速查（这套协议里反复出现）：

```text
0x5d5942  读 1 字节        0x5d5956  读 4 字节（存成 byte）
0x5d596f  读 2 字节        0x5d5984  读 4 字节
0x5d59de  读 4 字节折成 bool          0x5d59ff  读 4 字节 int32
0x5d5b3a  字符串 = u16 字符数 + UTF-16LE
```

`SessionDescriptor::Deserialize`（`0x557401`）按 type 分支：
type 1 / 5 / 6 读 3 个 int32；type 0 读 1 个；type 2 走 `0x557462` 分支
（客户端**发送**侧 `0x557374` 对 type 2 写 2 个参数，与实机抓到的
`args=(3, 1)` = (关卡 id, 难度) 一致）。

### 下一步该做什么

在 `gspRepCreateSession` 之后补发一个 **`0x0303`**，包体按上表组，
`SessionDescriptor` 直接回显客户端 `0x0201` 请求里那一份
（`type=2, args=(关卡 id, 难度)`），地图名回显 `Quest03_1` 那个字段。
这样 `[LobbyStage+0x1c]` 就是 2，`0x54f884` 会走闯关分支开
`DlgSelectQuestMap` 而不是 `DlgSelectPvpMap`，§64 的崩溃自然消失。

`0x0300..0x0311` 里其余 17 个分支的语义还没查，但闯关主线只差这一个。

---

# 第九批发现 —— 里程碑 C 的崩溃已解除，闯关房进得去了（2026-08-06 会话 08）

## 66. ★★★ `0x0303` 必须排在 `0x0201` 应答**之前**（§65 的下一步顺序是反的）

§65 写的是「在 `gspRepCreateSession` 之后补发一个 `0x0303`」。**这个顺序是错的**，
按它做修不好崩溃。

决定开哪个房间面板的 `cmp type, 2`（`0x54f875`）就在 **`0x0201` 应答自己的
处理器里**（`0x54f6fb` → 共用处理器 `0x54f747`）：

```asm
0054f6fb  call 0x5590bb          ; 读两个 int32：result、session_id
0054f717  call 0x54f747          ; 共用处理器（0x54f721 是同形状的另一个入口，flag=1）
...
0054f75f  cmp [ebp+0xc], edi     ; result != 0 -> 0x54f764 错误弹窗
0054f7da  mov esi, [0x72e29c]    ; LobbyStage
0054f7e4  mov [esi+0x1c8], eax   ; session_id 落位
0054f820  call 0x40e47f          ; edx=5 -> 切到 RoomStage（在地图名判断之前）
0054f875  mov eax, [0x72e29c]
0054f87a  mov eax, [eax+0x1c]    ; ★ descriptor.type，此刻必须已经是 2
0054f884  jne 0x54f8e1           ; != 2 -> PvP 分支
```

所以 `0x0303` 必须先到，客户端处理 `0x0201` 时描述符才是 2。
**实测按这个顺序发，§64 的崩溃当场消失，闯关房正常进入。**

## 67. ★★★ `0x0303` 里的地图名在建房那一步**必须留空**

同一个处理器在 `0x54f82e` 拿 `LobbyStage+0x10`（地图名）和 `L""`（`0x65dafc`）比一次：

```asm
0054f825  mov edi, 0x65dafc      ; L"" —— 就是一个 u16 0
0054f82a  push edi
0054f82b  lea ecx, [esi+0x10]    ; 地图名
0054f82e  call 0x4040f5          ; TString 比较，相等返回 0
0054f833  test eax, eax
0054f835  jne 0x54fb0c           ; ★ 地图名非空 -> 直接跳到函数的 ret
```

`0x54fb0c` 就是 `0x54f747` 的收尾（`ret 0x10`）。**地图名一旦非空，客户端收到
建房应答后什么都不做**，房间面板根本不会建起来。

`0x4040f5` 的返回语义（此前没查明，容易搞反）：它算出参数的宽字符长度，再把
自己的缓冲和参数一起交给 `0x402b7e`（先比长度再比内容的三路比较），
**相等返回 0**。所以 `test eax,eax; jne` = 「不相等就跳」。

## 68. ★★ 地图名是客户端随后用 `0x0302 gcpChangeSession` 提交上来的

`0x54f747` 走完闯关分支后，在 `0x54fae9` 调发送辅助 `0x54e5e9`，
后者在 `0x54e639` 用 `push 0x302` 打 opcode。**所以 `gcpChangeSession` = `0x0302`。**

`gcpChangeSession::Serialize`（vft `0x691808` 槽 0 = `0x54c18c`）的线格式：

```text
int32              (+0x04)   自由座位数，来自 0x556f40（数 6 个座位里的空位）
string             (+0x08)   房间标题
string             (+0x0c)   ★ 地图名
int32              (+0x10)   由 1 字节经 0x5d5a4c 零扩展而来
int32              (+0x11)   同上
SessionDescriptor  (+0x14)   房间类型 + 参数
```

**实机抓到的 60 字节载荷逐字段解对**：
`free_slots=6, texts=('想和做朋友吗?', 'Quest03_1'), flags=(0, 121), type=2, args=(3, 1)`。
（4 + (2+14) + (2+18) + 4 + 4 + 12 = 60 ✓）

即闯关房的地图名走的是「建房时留空 → 客户端提交 `0x0302` → 服务端广播 `0x0303`」这个回环。

## 69. ★ `Session::Deserialize` 与 `SessionDescriptor` 的线格式（逐条复核过）

`0x556ed1` = `0x556e80` 再追加一个 u16，写进 `LobbyStage`：

```text
int32   -> +0x04   (0x5d5984 读 4 字节)
string  -> +0x08   房间标题
int32   -> +0x0c
string  -> +0x10   ★ 地图名
int32   -> +0x14   (0x5d5956 读 4 字节，存成 1 字节)
descr   -> +0x18   (虚槽 1 = 0x557401)
u16     -> +0x3c   (0x5d596f 读 2 字节，movsx 后按 4 字节写)
```

它**不碰** `+0x1c8`（session id）和 `+0x34`（房主座位），所以下发 `0x0303`
不会把 `0x0201` 已经设好的 session id 冲掉。

`SessionDescriptor` 的**收发两侧参数个数不对称**，必须分开两张表：

| type | Serialize `0x557374` 写 | Deserialize `0x557401` 读 |
|---|---|---|
| 0 | 1 | 1 |
| 1 | 3 | 3 |
| 2 | **2** | **2** |
| 3 / 4 | 1 | **2**（而且第一个写回 `+0x04`，把 type 自己覆盖掉） |
| 5 / 6 | 3 | 3 |

3 / 4 是客户端自己的 bug。`server/gameserver.py` 因此不下发这两种类型
（`DESCRIPTOR_READ_ARGUMENT_COUNTS` 里没有它们）。

## 70. ★ `DlgSelectQuestMap` 的每帧刷新槽不是崩溃的那一个

```text
DlgSelectPvpMap    vft 0x66a01c  [+0x94] = 0x4652b3   ← §64 崩在这
DlgSelectQuestMap  vft 0x66a19c  [+0x94] = 0x4661c6   ← 完全不同的函数
```

`0x4661c6` 不调用 `0x409dd9 / 0x409df1 / 0x409e0a` 那三个对 type≠1/5 返回 -1
的访问器，所以闯关面板不会重蹈越界索引。

## 71. `0x0303` 处理器在旧描述符不是天梯时只做「灌数据 + 刷 UI」

`0x406756` 开头 `call 0x409f1c; cmp eax,5; sete [ebp-0xd]` 记下**旧** type 是否为 5，
反序列化之后 `cmp [ebp-0xd], bl(=0); je 0x406a5d` —— **旧 type 不是 5 就整段换图检测都跳过**，
直接走到 `0x406a5d`：`call 0x405a74`（LobbyStage 刷 UI）+ 可选的 `0x469344`。
我们的旧 type 是 -1 或 2，所以这条路径一直很短，没有额外副作用。

## 72. 实机确认的新 opcode（语义待查）

| opcode | 方向 | 时机 | 载荷 |
|---|---|---|---|
| `0x0103` | C→S | 登录应答之后立刻 | 8 字节全 0 |
| `0x0700` | C→S | 大厅首屏渲染完 | 0 |
| `0x0409` | C→S | 进闯关房约 6 秒后 | 0 |
| `0x0203` | C→S | 房间里点「F5 游戏开始」 | 0 |

**注意：闯关房点「游戏开始」发的是 `0x0203`，不是 §58 开局握手链的 `0x0402`。**
§58 那条 `0402 → 0401 → 0402 → 0412 → 0400` 是在别处观测到的，
闯关房的开局入口是另一个包，别直接套用。

## 73. ★★ 当前卡点：房间里点开始弹「等级太低，无法选择任务」

进房后点「F5 游戏开始」，客户端发 `0x0203`，然后弹确认框
**「等级太低，无法选择任务。」**（截图 `logs/shot_s08_after_f5.png`）。
此时「选择地图」面板里「任务」那一行是空的 —— 因为那一轮测试还没实现
`0x0302 → 0x0303` 回环，`LobbyStage+0x10` 的地图名一直是空串。

检查点在 `0x465935` 起的函数里，**等级和地图名是一起用的**：

```asm
00465948  mov eax, [0x72e338]    ; 玩家等级（§63 的那个全局）
0046594d  mov [ebp-0x1c], eax
00465950  mov eax, [0x72e29c]
00465955  add eax, 0x10          ; ★ LobbyStage 的地图名
00465958  push eax
00465959  lea ecx, [ebp-0x24]
0046595c  call 0x402cbe          ; 拷贝地图名
00465974  call 0x464995          ; 两者一起送进去判定
```

所以「等级太低」很可能是**地图名为空导致查不到关卡记录**的连带表现，
而不是真的等级门槛。要先补上回环再复判。

## 75. ★★ 「等级太低」**不是**等级问题：等级 30 一样报（已排除）

把 `accounts.json` 的 `level` 从 1 改到 30 重跑一整轮：

- 大厅左上角变成 **Lv.30**、军衔图标也跟着变 —— 说明等级确实下发到位了；
- 建房、进房、地图面板全部正常；
- 点「F5 游戏开始」**仍然弹同一个「等级太低，无法选择任务。」**
  （截图 `logs/shot_s08_lv30_start.png`）

**所以 `gspRepLogin` 的等级字段与这个提示无关**，测完已把 `level` 改回 1。

### 真正可疑的线索：房间座位里的等级从来没被下发过

同一张截图里，右上角个人信息栏是 **Lv.30**，而左下「玩家列表」里我自己那个座位
显示的是 **Lv. 1**。两个数字来自不同的地方：

- 个人信息栏读全局 `[0x72e338]`（登录包下发的等级）；
- 座位读的是 `LobbyStage` 的座位数组 —— **服务端从来没发过任何房间成员包**。

座位数组的位置由 `0x556f40` 反推得到：从 `LobbyStage+0x40` 起，**每项 0x3c 字节、共 6 项**
（`add ecx,0x41` 起步、`add ecx,0x3c` 步进、数 `[+0x41]==0` 的空位）。
建房成功时客户端只自己写了三处：

```asm
0054f807  call 0x405a1f      ; edx=0 -> 设置「我」的座位号
0054f80e  mov [esi+0x34], edi ; 房主座位 = 0
0054f812  mov [esi+0x40], bl  ; 座位 0 的占用标记 = 1
0054f815  mov [esi+0x4c], edi ; 座位 0 的 +0xc = 0
```

**座位里的等级/昵称等字段一个都没填**，要靠服务端下发的房间成员包。
下个会话从这里查：找哪个 opcode 往 `LobbyStage+0x40..+0x1a8` 写。

## 76. ★ `0x0409`（服务端→客户端）是「开始失败」提示包，不是准入应答

房间/游戏分发器是 `0x54e036`（一棵比较树，不是单一跳表）。
`0x0405..0x0413` 段用跳表 `@0x54e5ae`：

```text
0x0405 -> 0x54e34d   0x0408 -> 0x54e358   0x0409 -> 0x54e363 -> 0x551f53
0x040a -> 0x54e36e   0x040b -> 0x54e385   0x040c -> 0x54e391
0x040d -> 0x54e37a   0x0411 -> 0x54e39d   0x0413 -> 0x54e342
0x0406/0x0407/0x040e/0x040f/0x0410/0x0412 -> 0x54e546（未处理）
```

`0x0409` 的处理器 `0x551f53` 构造 vft `0x6918b0` 的包，
反序列化 `0x404ef7` **只读一个 int32**（写进 `+0x04`），然后：

```asm
00551f76  cmp [ebp-0x20], ebx   ; 包体第一个 int32
00551f89  je  0x551ff3
```

**但两条分支都是弹提示框**，只是文案 id 不同：

```text
0x691dec  '시작 실패'                                  = 「开始失败」（标题）
0x691d98  '게임을 시작할 수 없습니다.'                   = 「无法开始游戏。」      ← 非 0 分支
0x691db8  '네트워크 문제로 게임을 시작할 수 없습니다.'     = 「因网络问题无法开始游戏。」 ← 0 分支
```

所以**服务端不该主动发 `0x0409`** —— 发什么值都只会弹「开始失败」。
客户端进房后自己发的那个空 `0x0409` 是同号反方向的另一个包（D028 的老规律），
语义仍待查。

## 74. UI 文案是中文但镜像里只有韩文原串

`"无法选择任务"` / `"等级太低"` 在 `re/BigShot_22524.img` 里 UTF-16 搜**搜不到**，
而韩文串（`레벨` `등급` `선택` 等）搜得到。中文本地化文案在 `Pack\*.pkn` 的文本表里，
运行时替换。**想按中文提示定位代码，得先把韩文原串找出来，或者直接从行为反推。**

---

## 死路清单追加（会话 05）

| 尝试 | 结果 |
|---|---|
| 以为 27799 是 SnowCipher（会话 04 的推测） | **错**。是 SimpleCipher，会话 04 自己的日志里就有反证 |
| 从外部 `ShowWindow` 强行显示主窗口 | **会阻塞**（目标线程不泵消息），且窗口仍不可见 |
| PowerShell `Add-Type` 内联 C# 调 Win32 | **很慢/会挂**（要现编译）。用 Python ctypes，见 `tools/screenshot.py` |
| Bash heredoc 往 `.claude/*.md` 追加大段中文 Markdown | **会被 shell 解析炸掉**。用 Write 写临时文件 + PowerShell `Add-Content` |

## 死路清单追加（会话 06）

| 尝试/现象 | 结果 |
|---|---|
| 用 `cmd start /b ... > game_live.out` 后客户端每 0.2 秒重连 27799，产生数百个空连接 | **不是协议错**。Python 在中文 Windows 后台重定向时 stdout=GBK；游戏服打印 `✓` 触发 `UnicodeEncodeError`，处理线程在版本握手后立刻退出。两个服务现已启动时把 stdout/stderr 固定为 UTF-8。 |
| `tools/screenshot.py` 在主窗口仍隐藏时抓图 | 会误选 `SplashWindow`，得到 451×350 黑图；必须先确认游戏服登录成功、`MoleWnd` 已可见再抓。 |
| 在 `RendererInit` hook 里提前 `ShowWindow(MoleWnd)` | 能让隐藏窗口变为 visible，但独立 D3D9 探针和客户端仍返回相同的 `D3DERR_NOTAVAILABLE`，不是设备创建失败的修复。该行为已撤回，只保留只读诊断 hook。 |
| 把房间类型 5 / 空 `0x0416` 当成闯关开局 | **错**。类型 5 是 `DlgCreateLadderRoom` 写入的天梯类型；闯关明确是类型 2，仍发空 `0x0402`。同号服务端 0x0416 还是两个 int32 的任务难度更新包，绝不能直接回显。 |

## 死路清单追加（会话 07）

| 尝试/现象 | 结果 |
|---|---|
| 以为 D3D9 挂掉是「虚拟显示器附着在 DISPLAY1 上」这个静态拓扑造成的 | **不精确**。退出 Sunshine/Moonlight 后 `XMD009A` 依旧 active/attached，探针却全部恢复成功。真正的开关是**串流软件是否在跑**，不是拓扑（§61）。 |
| 点大厅「任务」标签页没反应，先怀疑点击坐标不对 | **不是坐标**。客户端确实发了 `0x020b`，只是我们没回应答（§62）。**这个客户端里「点了没反应」几乎都是缺应答或被过滤，不是点歪了。** |
| 想按「同 opcode 回显」的思路回 `0x020b` 当作切频道成功 | **会弹错误框**。`0x54fbfa` 两条分支都是失败提示，`result=0` 也一样。成功必须回 `0x0701 gspRepMoveInto`（§62）。 |
| 关卡下拉框空着，先怀疑要靠服务端下发已解锁关卡（`0x020c gspQuestReachedDifficulty`） | **不是**。那张 map（`0x72e35c`）全镜像只有两处引用，都在 `0x020c`/`0x0416` 自己的处理器里，没有任何地方读它来填列表。真凶是登录包等级填 0（§63）。 |
| 打开建房对话框时期待客户端向服务端要关卡列表 | **不会发任何包**。关卡目录纯客户端（`[0x72e3d8]` 红黑树，来自 pkn 数据）。 |
| 客户端在建闯关房后退出，先怀疑是注入/GameGuard/D3D | **都不是**。客户端自带崩溃报告 `game_patched/Dump/LastCrashReport.txt`，直接给出 `C0000005 @ 0x40197F` 和完整调用栈（§64）。**客户端一退先看这个文件。** |

## 死路清单追加（会话 08）

| 尝试/现象 | 结果 |
|---|---|
| 按 §65 的字面建议「在 `gspRepCreateSession` **之后**补发 `0x0303`」 | **修不好崩溃**。决定房间面板类型的 `cmp type,2` 就在 `0x0201` 自己的处理器里，`0x0303` 必须**先**到（§66）。 |
| 想在建房的 `0x0303` 里顺手回显地图名 `Quest03_1`（「反正客户端已经告诉我们了」） | **会让客户端什么都不做**。`0x54f835` 见地图名非空就直接 `jne` 到函数的 `ret`，房间面板根本不建（§67）。地图名要等客户端发 `0x0302` 再回。 |
| 把闯关房的「游戏开始」当成 §58 的 `0x0402` 开局链入口 | **不是**。实机点 F5 发的是 `0x0203`（载荷 0），§58 那条链是别处观测的（§72）。 |
| 想按中文提示「等级太低，无法选择任务」在镜像里搜字符串定位代码 | **搜不到**。中文文案在 pkn 文本表里，镜像里只有韩文原串（§74）。 |

---

# 第十批发现 —— 「等级太低」解除，开局握手真的跑起来了（2026-08-06 会话 09）

## 77. ★★★ 「等级太低，无法选择任务。」读的是**房主座位里的等级**，不是账号等级

韩文原串 `0x66a7fc` = `레벨이 낮아서 퀘스트를 선택할 수 없습니다`
（中文本地化就是那句「等级太低，无法选择任务。」）。全镜像只有 **一处** 引用：`0x4682d6`。

它所在的函数 `0x468176` 是「能不能开局」的准入校验，返回一个错误码：

```text
0 = 通过
2 = '맵의 최대 인원(%d명)을 초과하였습니다'      地图最大人数超了     (0x66a7cc)
3 = '레벨이 낮아서 퀘스트를 선택할 수 없습니다'  ★ 就是我们卡的这个    (0x66a7fc)
    （同一分支的另一条文案 0x66a82c '…맵을 선택할 수 없습니다'，由 0x5f5192 选）
5 = '플레이할 수 없는 난이도 입니다…'            难度太高             (0x66a758)
6 = '인원이 너무 많거나 레벨이 낮아서 랜덤맵…'   随机图模式不可用      (0x66a858)
```

判定链（`0x468242` 起）：

```asm
00468242  mov edi, [esi+0x34]          ; esi = LobbyStage，+0x34 = 房主座位号
00468249  call 0x4045f9                ; 座位占用？
00468256  call 0x404d42                ; -> 座位指针（+0x40 + idx*0x3c）
00468261  test edi, edi
00468266  je  0x468316                 ; 座位指针为 0 -> 直接放行
00468272  call 0x464848                ; -> eax = 关卡要求等级 = [关卡记录+0x28]
00468277  movzx ecx, word ptr [edi+0x10]   ; ★★★ 座位里的等级（u16）
0046827b  cmp ecx, eax
0046827d  jge 0x468316                 ; 够 -> 通过
                                       ; 不够 -> 弹 0x66a7fc
```

**和 `gspRepLogin` 下发、存在全局 `0x72e338` 的那个等级完全无关** —— 这就是
§75 里「等级改成 30 照样弹框」的原因：座位数组从来没被下发过，`seat[+0x10]` 是 0。

通过之后还有两关，闯关房都能过：
- `0x46485c()` = `[关卡记录+0x34]` = 地图最大人数（找不到记录时默认 6），
  与 `0x4059fc(LobbyStage)`（当前人数）比；
- 描述符 type==2 走 `0x4683ba`：`0x40119f(0x72e328, 关卡 id)` 取「已达成难度」+1
  夹到 4，和 `LobbyStage+0x24`（描述符第 2 个参数 = 难度）比，小于等于就放行。
  所以难度「简单」(1) 永远过。

## 78. ★★★ `SessionSlot`（房间座位）的线格式

反序列化 `0x556d9d`（`this` = 座位指针，`eax` = 流）。座位数组在 `LobbyStage+0x40`
起、每项 **0x3c** 字节、共 **6** 项，取值器是 `0x404d42(ecx=下标, edx=LobbyStage)`
（`imul ecx,ecx,0x3c / lea ecx,[ecx+edx+0x40]`，下标越界返回 0）。

```text
int32(bool) 占用       -> +0x00   0x5d5956 读 4 字节折成 bool
占用时:
    string  昵称       -> +0x04   0x5d5b3a = u16 字符数 + UTF-16LE
    u8      ?          -> +0x08   ★ 0x5d5942 读 1 字节，不是 4
    int32   角色 id    -> +0x0c   0x0301 的 action 4 拿它去 0x557128 查角色名
    int32×n + int32 0  -> +0x1c   0 结尾的 int32 列表（推测是装备/道具）
    int32   ?          -> +0x28
    u16     ?          -> +0x2c
    u16     ★ 等级     -> +0x10   §77 的准入校验读的就是它
    int32(bool) ?      -> +0x2e
    u16     ?          -> +0x12
    string  ?          -> +0x30
    int32(bool) ?      -> +0x34
不占用时:
    （无字段，客户端调 0x556c55 把整个座位清零）
int32(bool) 关闭       -> +0x01   ★ 无论占用与否都要读
```

`+0x01` 是**「关闭」**不是「占用」：`0x556f40` 数的空位是 `[+0x41] == 0` 的座位
（`add ecx,0x41` 起步、`add ecx,0x3c` 步进、6 轮），`0x406416` 拼调试串时也是
`[seat+1] != 0 -> 'closed'`。所以实机 `0x0302` 里 `free_slots=6`（座位 0 已占用）
是对的 —— 它数的是「没被房主关掉的位子」。

座位其它已知字段（非线格式，运行时填）：`+0x14` = 对端 IP（`0x0100007f`），
`+0x18` u16 = 端口，`+0x38` = 变更通知回调链表。

## 79. ★★★ `0x0300` = 整个房间的座位快照（服务端 -> 客户端）

大厅跳表 `@0x406332` 索引 0 → `0x406232` → **`0x40637a`**，包体交给
`0x556eec`（`this` = `LobbyStage`）：

```text
int32   房主座位号   -> LobbyStage+0x34
int32   ?            -> LobbyStage+0x38
SessionSlot × 6      -> LobbyStage+0x40 起
```

处理器随后：
1. `[LobbyStage+0x1c4] = 1`；
2. 逐座位：`0x405a5d(i)`（i 是我 或 `[LobbyStage+0x1e8+i]==1`）为真就把
   `seat[+0x14]` 写成 `0x0100007f`（127.0.0.1），否则清 `[LobbyStage+0x284+i*4]`；
3. `0x405d8c(1)`：**逐座位建「角色对象」**（`0x405e1c` new 0x7c4/0x744，落到
   `[LobbyStage+0x1d0+i*4]`）+ `0x406f42`；
4. `0x405a74` 刷房间 UI；
5. 末尾拼 `"Slot %d : %s, open/closed"` 的循环是**死调试代码**，字符串建完就析构。

**必须排在 `0x0201` 建房应答之后**：`0x54f815` 会把 `[LobbyStage+0x4c]`
（= 座位 0 的 +0x0c，角色 id）清零，先发就被冲掉。

### 实机验收（会话 09）

`0x0303`(空图名) → `0x0201`(ok) → **`0x0300`(host_seat=0, 座位 0 = testuser/Lv1)**
之后：

- 房间左下「玩家列表」出现 **`testuser` + 「房主」徽标 + Lv.1**（此前是空的「等待」）；
- 「人物选择」栏出现三个角色头像（此前全空）；
- 点「F5 游戏开始」**不再弹「等级太低」**，客户端直接发 `0x0402` 进开局握手。
  截图 `logs/shot_s09_room.png`。

## 80. ★★ 闯关房点「F5 游戏开始」发的是 `0x0402`，不是 `0x0203`（§72 记错了）

会话 09 实测：座位数据补齐后点 F5，客户端发的是 **`0x0402`**，
走的正是 §58 那条链 —— 服务端回 `0x0401` 后客户端 14ms 内再发一个 `0x0402`，
服务端回 `0x0402` + `0x0412`。

`0x0203` 是**离开房间/取消**：发送函数 `0x406191`（`push 0x203` + RawPacket，载荷 0），
四个调用点里 `0x46739c` 那个在「离开房间」逻辑里，同一函数会弹
`0x66aa60 '카운트 다운 중에는 방을 나갈 수 없습니다.'`（倒计时中不能离开房间），
另一个 `0x54be4c` 在连接清理里。§72 观测到的 0x0203 是弹框之后玩家/客户端
退房的动作，不是「请求开始」。

## 81. 大厅包分发跳表的完整处理器表（`0x4061e2`，跳表 `@0x406332`）

```text
0x0300 -> 0x40637a   房间座位快照（§79）
0x0301 -> 0x40648d   单座位更新（byte action + int32 座位号 + SessionSlot）
0x0302 -> 0x4066e2
0x0303 -> 0x406756   整份 Session（§65/§69）
0x0305 -> 0x406adb   0x0306 -> 0x406c78   0x0308 -> 0x406d49
0x030b -> 0x406ea1   0x030c -> 0x40740b   0x030d -> 0x4083a4
0x030e -> 0x40899a   0x030f -> 0x4089c3   0x0311 -> 0x408a1c
0x0304 / 0x0309 / 0x030a / 0x0310 -> 0x4062ea（未处理，返回 0）
0x0307 -> 0x40632d（吞掉）
0x0401 -> 0x4074de（`cmp eax,0x401 / je` 走的单独分支）
0x040e -> 0x40741c   0x040f -> 0x4086b5   0x0410 -> 0x408703
0x0417 -> 0x408526   0x0418 -> 只置 [this+0x3fa]=1
0x041f -> 0x4088b7   0x0420 -> 0x4088fe
```

`0x0301` 的载荷：`u8 action`（`0x5d5942` 读 1 字节）+ `int32 座位号`（校验 0..5）
+ `SessionSlot`。action 分支：

```text
0 -> 0x406691  进房：清 IP/端口、0x405e1c 建角色对象、0x406f42、0x4089fa
1/2 -> 0x406676 清 IP/端口 + 0x405f8f（**销毁**角色对象并把 +0x1d0 槽清 0）
3 -> 0x406628  离开/踢出
4 -> 0x406520  换角色：用 [seat+0x0c] 查名字，播 0x65e898 '%s님이 %s 캐릭터로 선택되었습니다.'
```

## 82. ★★ 倒计时开始后渲染线程空指针崩溃（`0x50a368`）—— **已解决，答案在 §84/§85**

`0x0412 gspRepCountDown(state=0)` 发出后同一秒内客户端崩溃
（`game_patched/Dump/LastCrashReport.txt`，`BigShotV0311N003.mdmp`）：

```text
Exception code: C0000005    Fault address: 0050A368    EBX = 00000000
0050a368  mov eax, dword ptr [ebx]        ← 崩在这
Call stack: 0050A368 <- 00496F84 <- 00497040 <- 00490F31 <- 00424F18
            <- 0042B5B7 <- 0042647B <- 0040E6C4 <- 0040DF96 <- 0040A6B9 <- 005F7B9E(主循环)
```

`ebx` 来自 `0x496f78: call 0x409f39`：

```asm
00409f39  mov esi, [0x72e29c]           ; LobbyStage
00409f42  jne 0x409f48 ; 否则返回 0
00409f48  call 0x409e20
    00409e20  mov edi, [esi+0x1cc]      ; 我的座位号
    00409e2b  call 0x4045f9             ; 座位占用？不占用 -> 返回 0
    00409e34  mov eax, [esi + edi*4 + 0x1d0]   ; ★ 我这一格的「角色对象」指针
```

即：**`[LobbyStage+0x1d0 + 我的座位*4]` 是 NULL**（或我的座位标记成空了）。
按 §79 第 3 步，`0x0300` 的处理器里 `0x405d8c(1)` 本该把它建出来 ——
所以下个会话要么是「它压根没建成」，要么是「倒计时/换场景时被销毁了」。

**下一步实测手段**：进房后用活进程探针直接读
`[[0x72e29c]+0x1cc]`（我的座位）和 `[[0x72e29c]+0x1d0 .. +0x1e8]`（6 个角色对象指针），
就能一次分清这两种情况。工具照 `tools/probe_session_desc.py` 改。

若确实没建成，最直接的补法是在 `0x0300` 之后再补一个
**`0x0301` action=0**（座位 0），它会显式调 `0x405e1c` + `0x406f42`；
注意它同时会把 `seat[+0x14]/[+0x18]`（IP/端口）清 0，顺序要考虑。

## 83. ★★ 大厅点「对战」标签弹「不符合等级要求，无法连接」= 等级**恰好等于 1** 的新手保护

用户实机观察（会话 09）：大厅在「对战」和「任务」标签间切换时，点「对战」立刻弹
「不符合等级要求，无法连接」。

韩文原串 `0x6658f8` = `레벨이 맞지 않아 접속 하실 수 없습니다.`，两处引用
（`0x440b57` / `0x440d2a`）都在同一个频道准入校验 `0x440af7` 里：

```asm
00440af7  ; bool CanEnterChannel(this=ecx, targetChannel=edx)
00440b05  xor ebx, ebx / inc ebx           ; ebx = 1
00440b08  cmp dword ptr [0x72e338], ebx    ; ★ 账号等级（gspRepLogin 下发的那个）
00440b0e  jne 0x440b9c                     ; ★ 等级 != 1 就完全不拦
00440b14  mov eax, [ecx+0xd4]              ; 该标签页对应的频道号
00440b1a  cmp edx, eax
00440b1c  jne 0x440b9c
          ; 两条都命中 -> MessageBox('알림', '레벨이 맞지 않아 접속 하실 수 없습니다.')
00440b9c  ; 第二道拦截：[conn+0x89c] == 0xa 且 edx == [ecx+0x128] 时另一个提示
```

**判定是 `== 1` 而不是 `< 2`** —— 这是给 1 级新号的引导性拦截（配套的频道说明串
`0x665560 '2~3레벨에 해당하는 유저만을 위한 대전 채널입니다.'` = 对战频道给 2~3 级用）。

### 这条现象的真正价值：反证 §77

它和「等级太低，无法选择任务」是**两套完全独立的判定，连等级来源都不同**：

| 提示 | 读哪个等级 | 条件 | 位置 |
|---|---|---|---|
| 不符合等级要求，无法连接 | 全局 `[0x72e338]`（`gspRepLogin` 下发） | `== 1` | `0x440b08` |
| 等级太低，无法选择任务 | 座位 `seat+0x10`（u16，`0x0300`/`0x0301` 下发） | `< 关卡要求等级` | `0x468277` |

所以这条现象恰好证明**账号等级确实下发到位并被客户端用上了**，
§75「等级改成 30 照样弹『等级太低』」不是因为等级没传到，而是那个框根本不看它。

### 顺手可做

把 `server/data/accounts.json` 里 `level` 从 1 改成 **2**，「对战」标签页就不再弹框
（判定是 `== 1`，2 就直接跳过）。座位等级跟着变 2，闯关关卡要求等级是 1，照样通过。
天梯标签仍需 >= 6（§62），但不影响闯关主线。
**本项目只做单机闯关+训练场，不做联网对战，所以这个框不阻塞任何东西**，改不改都行。

---

## 死路清单追加（会话 09 前半段，§77–§83）

| 尝试/现象 | 结果 |
|---|---|
| 按 §73 从 `0x465935` 那条「等级 + 地图名一起送进 `0x464995`」的链去找「等级太低」 | **找错函数了**。`0x465935` 所在的是房间 UI 按钮回调（`gameModeRBtn` / `teamModeLBtn` 那一堆），跟提示框无关。正确入口是搜韩文原串 `레벨이 낮아서 퀘스트를…`（`0x66a7fc`），全镜像唯一引用 `0x4682d6`（§77）。 |
| 想按中文提示搜串 —— 换成搜韩文 | **有效**。`레벨` / `미션` / `선택할 수 없습니다` 三个词就把 20 多条相关文案全捞出来了。§74 说的「中文搜不到」成立，但**韩文搜得到**，这是定位 UI 提示的标准手段。 |
| 以为「等级太低」是 `gspRepLogin` 的等级字段 | **不是**，§75 已排除，§77 给出真凶（座位里的 u16，§77）。反证见 §83。 |
| 以为 `0x0300` 建了角色对象就万事大吉 | **没跑完**。座位数据补齐后 F5 能进开局握手，但倒计时一开始就在渲染路径空指针崩溃（§82），`[LobbyStage+0x1d0+我的座位*4]` 是 NULL。 |

## 84. ★★★ `0x0400` / `0x0402`（服务端→客户端）不是应答，是**「换 stage」命令**

§82 的崩溃根因。房间/游戏分发器 `0x54e036` 是一棵比较树（不是单一跳表），
`0x0400` 和 `0x0402` 各自的处理器**最后一句都是切 stage**：

```asm
; 0x0400 gspPrepareGame -> 0x551605
00551628  mov eax, [0x72e29c]            ; LobbyStage
0055162d  mov cl, [eax+0x14]
00551632  je  0x5517a3                   ; [+0x14]==0 -> 直接跳到汇合点
00551638  mov ecx, [eax+0x1c]            ; descriptor.type
0055163e  cmp ecx,1 / je  ...            ; 只有 type 1（普通）和 5（天梯）
00551642  cmp ecx,5 / jne 0x5517a3       ; 才走中间那段；★ 闯关 type=2 直接到汇合点
; --- 汇合点 ---
005517a3  inc dword ptr [eax+0x3c]
005517a6  mov dword ptr [eax+4], 4
005517ad  call 0x407678
005517b7  push 6 / pop edx
005517ba  call 0x40e47f                  ; ★★ 切到 stage 6（准备/加载）

; 0x0402 -> 0x5517d0
005517d0  call 0x409ff3                  ; now(ms)
005517db  mov [ecx+0x3dc], eax           ; ecx = LobbyStage，记时间戳
005517ea  or dword ptr [eax+0xf7c], -1   ; 清 [0x72e2cc] 里的倒计时状态
005517f9  cmp dword ptr [eax+0x54], 7    ; 已经在 stage 7 就什么都不做
005517fe  jmp 0x40e47f                   ; ★★ 否则切到 stage 7（游戏），edx=7
```

`0x40e47f` 就是 `0x54f820` 建房时用来切 RoomStage(5) 的那个 stage 切换函数
（`edx` = 目标 stage，`dec ecx` 链分派）。

### 会话 09 的错误与修正

服务端原先在第二个 `0x0402` 到达时一口气回 `0x0402` + `0x0412`：

- `0x0402` 把客户端**直接踢进 stage 7**，跳过了 stage 6 的准备/加载；
- 房间对象随之拆除，座位的角色对象 `[LobbyStage+0x1d0+i*4]` 被销毁；
- 渲染路径 `0x496f78 → 0x409f39 → 0x409e20` 拿到 NULL 再解引用 → §82 的崩溃。

**探针实拍**（`logs/probe_s09b_seats.txt`，`tools/probe_room_seats.py`）：

```text
[00:12:05.181] 座位0 ★我 占用=1 昵称='testuser' 等级=1 IP=127.0.0.1 对象=0x20144020
[00:12:15.019] 座位0 ★我 占用=1 昵称='testuser' 等级=1 IP=127.0.0.1 对象=0x0  ← 被销毁
[00:12:17.629] LobbyStage 还没建（[0x72e29c] = 0）                            ← 崩溃后
```

`0x0300` 确实把角色对象建出来了（`0x20144020`），是**我们自己发的 `0x0402` 把它拆掉的**。
所以 §82 里「压根没建成 / 建了又被销毁」这个二选一，答案是**后者**。

**修正**：第二个 `0x0402` 只回 **`0x0400`**，一次只推进一个 stage（先 6 后 7），
stage 7 等客户端加载完自己开口再说。

### `0x0412` 不要发

`0x54e036` 的 `0x0405..0x0413` 段跳表 `@0x54e5ae` 实测内容：

```text
0x0405 -> 0x54e34d   0x0406 -> 0x54e546✗  0x0407 -> 0x54e546✗  0x0408 -> 0x54e358
0x0409 -> 0x54e363   0x040a -> 0x54e36e   0x040b -> 0x54e385   0x040c -> 0x54e391
0x040d -> 0x54e37a   0x040e -> 0x54e546✗  0x040f -> 0x54e546✗  0x0410 -> 0x54e546✗
0x0411 -> 0x54e39d   0x0412 -> 0x54e546✗  0x0413 -> 0x54e342
```

`0x54e546` = 未处理。**`0x0412` 在这里收到就丢**，发它纯属噪音。
（`build_rep_count_down` 暂时留着，等确认它属于哪个 stage 的分发器再定去留。）

### 比较树里 0x0400 段的完整分派（`0x54e28c` 起）

```text
opcode == 0x2e1 -> 0x550640    0x2e2 -> 0x550a72    0x309 -> 0x55210d
           0x30a -> 0x552880    0x400 -> 0x551605    0x402 -> 0x5517d0
           0x403 -> 0x5518fb    其它 -> 0x54e546（未处理）
```

## 85. ★★★ 闯关开局的完整包序列（会话 09 实机走通到加载 100%）

```text
C→S  0x0402  (载荷 0)        点「F5 游戏开始」
S→C  0x0401  gspTriggerCountGame(result=0)
C→S  0x0402  (载荷 0)        约 15ms 后，确认
S→C  0x0400  gspPrepareGame(seed)    ★ 切 stage 6 = 准备/加载（§84）
     ...客户端在 stage 6 加载关卡，画面是关卡插画 + 「正在载入 %d%%」+
        操作向导键位表 + 左下角每个玩家一条加载进度条...
C→S  0x0403  (载荷 0)        加载到 100% 之后**每 5.02 秒一发**的轮询
S→C  0x0402  (载荷 0)        ★ 切 stage 7 = 游戏本体（§84）
```

**关键点：`0x0402` 是服务端手里的「开闸」包，但必须等客户端先报 `0x0403`。**
提前发就是 §82 的崩溃：客户端还没经过 stage 6 的重建流程，
座位角色对象正处在 NULL 窗口期。

### 探针实拍的角色对象生命周期（`logs/probe_s09c_seats.txt`）

```text
[00:20:42.698] 座位0 ... 对象=0x3d1c8038     ← 0x0300 房间座位快照建的
[00:20:52.535] 座位0 ... 对象=0x0            ← 0x0400 切 stage 6，房间对象拆除
[00:20:54.142] 座位0 ... 对象=0x3d1c8038     ← ★ stage 6 的加载流程又重建了（同地址）
```

所以「对象变 NULL」本身是**正常的过渡态**，问题只在于会话 09 早先把
`0x0402` 发在了这个窗口里。**下发顺序错了一步，症状是空指针崩溃，
不是协议字段错** —— 这类 bug 只能靠这种带时间戳的活进程采样定位。

### `0x0403` 的服务端方向

比较树 `0x54e28c` 里 `0x0403 -> 0x5518fb`（服务端→客户端方向的处理器，语义未查）。
客户端发的这个空 `0x0403` 是同号反方向的另一个包（D028 的老规律），
服务端**不要回显 `0x0403`**，要回 `0x0402`。

### ★★★★ 实机验收：里程碑 C 达成（会话 09）

按上面的序列改完之后跑完整一轮，客户端**真的进了关卡**：

```text
00:26:33.422  C→S 0x0402   → S→C 0x0401 result=0
00:26:33.436  C→S 0x0402   → S→C 0x0400 seed=0        ★ 切 stage 6
00:26:58.012  C→S 0x0403   → S→C 0x0402               ★ 切 stage 7
```

- `logs/shot_s09c_after_f5.png` —— 关卡加载画面，「正在载入 26%」+ 关卡插画
  + 操作向导键位表 + 左下角 `testuser` 的加载进度条
- `logs/shot_s09d_ingame.png` —— **剧情演出**：「卡希尔： 对，还是进去看看会比较好！」，
  角色带血条站在海滩场景里，右上「Esc 跳过剧情」
- `logs/shot_s09d_battle.png` —— **可玩的战斗画面**：「神秘岛 简单」、
  关卡计时器 12:31 在倒数、右上战绩栏（生命 ♥♥♥ / 分数 0）、
  左下角色头像 + HP/SP 条、右下武器栏（火箭筒 / 道具 / 枪）、准星

即 §12 的判断得到最终证实：**闯关关卡的内容完全在客户端**，
服务端只要把 stage 按顺序推过去就行。

---

## 死路清单追加（会话 09 后半段，§84–§85）

| 尝试/现象 | 结果 |
|---|---|
| 按 §58 的链把 `0x0402` + `0x0412` 一起当「倒计时开始」回给客户端 | **会崩**。`0x0402` 是「切 stage 7」命令，跳过了 stage 6 的加载与对象重建；`0x0412` 在 `0x54e036` 里根本没有处理器（§84）。 |
| 以为「座位角色对象变 NULL」本身就是 bug | **不是**。它是 stage 切换的正常过渡态，stage 6 的加载流程会把它重建回同一地址。错的是我们**发包的时机**（§85）。 |
| 想靠静态反汇编推断「对象为什么是 NULL」 | **推不出来**。同一段代码在两种时序下表现完全不同。`tools/probe_room_seats.py` 带时间戳采样一次就定案了 —— **时序类 bug 必须用活进程采样**。 |
| 等客户端先发 `0x0400` 再回 `0x0400` | **等不到**。客户端从头到尾没发过 `0x0400`；它在 stage 6 加载完之后发的是 `0x0403`。 |

## 86. ★★★ 战斗中客户端发的 opcode，以及「死不掉 / 不结算」的根因

用户实机反馈两个现象，根因都是**服务端从没回过任何战斗包**：

1. 人物血量归零后不死，站着不动无法控制
2. 关卡倒计时归零后不结束，不进结算页

### 一局实测收到的战斗包（`logs/gameserver.out`，会话 09）

```text
00:27:44.929  0x040c  4B   00 00 00 00
00:27:52.995  0x0415  4B   00 00 00 00
00:28:50.270  0x0406  32B  da 27 00 00 | 00 90 49 45 | 00 c0 1e 44 | 00 00 00 00 ...
                           ★ 后两个 dword 是 IEEE754 float 3225.0 / 635.0 = 坐标
00:28:53~00:29:02  0x0410 ×9  4B  4,12,20,24,28,32,44,56,64   ★ 累计分数，单调递增
00:29:00~00:29:03  0x0408 ×4  18B 06 c9 10 00 ff 00 00 00 ...  疑似开火/命中
00:40:04.089  0x040f  0B    ★ gcpEndQuest —— 倒计时（12:30）归零那一刻
00:40:04.095  0x0417  4B   00 00 00 00   ★ gcpMarkQuestSuccess(0)
```

`0x040f` 的时间戳 00:40:04 = 进游戏 00:27:34 + 12:30，**与关卡计时器精确吻合**。
所以客户端把「关卡结束」如实报上来了，是服务端没接。

### 服务端方向的应答 opcode（从 RTTI vftable 反查分发树，全部确认）

| 类 | vftable | 处理器 | **opcode** |
|---|---|---|---|
| `Packet_gspEndGame` | `0x691874` | `0x551804` | **`0x0411`** |
| `Packet_gspRespawnCharacter` | `0x6916b0` | `0x553ecc` | **`0x0419`** |
| `Packet_gspRepGameResult` | `0x691898` | 待定（`0x5520cb` 附近） | 待定 |
| `Packet_gspRewardPopup` | `0x6917d8` | `0x54d11d` 构造 | 待定 |
| `Packet_gspUpdateQuestScore` | `0x673afc` | `0x4a3f0b` 构造 | 待定 |

反查方法（可复用）：RTTI 名 → `TD = name_va - 8` → 找引用 TD 的 dword 得 COL
→ 找引用 COL 的 dword `+4` 得 vftable → `refs_to(vftable)` 得构造点
→ 构造点落在哪个处理器函数体内，就查 `0x54e036` 分发树里谁跳过去。

分发树补全（`0x54e30b` / `0x54e3be` 段）：

```text
0x040d -> 0x551dfb   0x040b -> 0x55206b   0x040c -> 0x552089   0x0411 -> 0x551804
0x0416 -> 0x553a38   0x0419 -> 0x553ecc   0x041a -> 0x553fe2   0x041b -> 0x554006
0x041e -> 0x551ddb   0x0501 -> 0x554136
```

### 两个包的线格式（已读出）

**`0x0419 gspRespawnCharacter`** —— 反序列化 `0x54c5d0`，4 个 int32：

```text
int32 -> +0x04   角色/座位 id
int32 -> +0x08   X 坐标   ★ 客户端用 fild 转 float，所以线上是**整数**
int32 -> +0x0c   Y 坐标   ★ 同上
int32 -> +0x10   语义未查
```

处理器 `0x553ecc` 读完直接 `call [stage_vft+0xd4](id, floatX, floatY, [+0x10])`。

**`0x0411 gspEndGame`** —— 反序列化 `0x54cea3`，栈对象 0x70 字节，字段很多：

```text
int32   -> +0x04   (0x5d59ff)
bool32  -> +0x08   (0x5d59de 读 4 字节折 bool)
int32   -> +0x0c / +0x10 / +0x14 / +0x18 / +0x1c / +0x20 / +0x24 / +0x28 / +0x2c / ...
```

**还没读完**（`0x54cf19` 之后仍在继续）。下个会话把 `0x54cea3` 读到 `ret`，
数清字段总数和宽度，再看 `0x551804` 处理器里哪些字段被真正用到
（`0x551854` 起有一大段 `[ebp-0x68]`.. 的搬运，对应包体各字段）。

### 禁忌（服务端方向未处理，回显只会被丢弃或出错）

`0x0406` / `0x040f` / `0x0410` / `0x0415` 在 `0x54e5ae` 跳表里都指向
`0x54e546` = 未处理。**客户端发它们，但服务端不能回同号包** —— 这是 D028 的老规律，
要回的是上表里对应的反方向 opcode。

## 87. ★★★ `gspEndGame`(0x0411) 与 `gspRespawnCharacter`(0x0419) 的线格式

### `0x0411 gspEndGame` —— 14 个 4 字节字段（56 字节）

反序列化 `0x54cea3`（全部 `0x5d59ff` / `0x5d59de`，逐条数到 `ret @0x54cf47`）：

```text
int32   -> +0x04   ★ 座位号
bool32  -> +0x08   （0x5d59de 读 4 字节折成 bool）成功 / 失败
int32   -> +0x0c   ┐
int32   -> +0x10   │  ★ [0x72e33c] = 它
int32   -> +0x14   │  ★ [0x72e344] = 它
int32   -> +0x18   │  ★ [0x72e340] = 它
int32   -> +0x1c   │  ★★ [0x72e330] **+=** 它（累加！填 0 最安全）
int32   -> +0x20   │  12 个业务值，整体交给结算 UI
int32   -> +0x24   │
int32   -> +0x28   │
int32   -> +0x2c   │
int32   -> +0x30   │
int32   -> +0x34   │
int32   -> +0x38   ┘
```

处理器 `0x551804`（分发树 `0x54e39d`）：

```asm
0055189f  push 0xd / rep movsd      ; 把 +0x0c 起的 13 个 dword 搬成一个紧凑结构
005518af  call 0x4a4096             ; eax = +0x04（座位号），交给结算 UI
005518b4  call 0x409f7d             ; 我的座位号
005518b9  cmp ebx, eax / jne        ; ★ 座位号 == 我的，才更新下面四个全局
005518c0  add [0x72e330], pkt+0x1c  ; ★★ 累加
005518c9  mov [0x72e33c], pkt+0x10
005518d1  mov [0x72e340], pkt+0x18
005518d9  mov [0x72e344], pkt+0x14
005518de  ...
005518e4  call 0x4913fc             ; 结算界面（无论座位号对不对都会走）
005518ef  call 0x4087f0
```

**`+0x1c` 是 `+=` 不是 `=`** —— 未查明语义前必须填 0，否则每结算一次玩家数据就涨一次。

### `0x0419 gspRespawnCharacter` —— 4 个 int32（16 字节）

反序列化 `0x54c5d0`：

```text
int32 -> +0x04   角色 / 座位 id
int32 -> +0x08   ★ X 坐标
int32 -> +0x0c   ★ Y 坐标
int32 -> +0x10   语义未查
```

处理器 `0x553ecc`（分发树 `0x54e3f6`）读完直接调
`[stage_vft+0xd4](id, (float)X, (float)Y, +0x10)`。

★ **坐标在线上是整数**：`0x553ee4` / `0x553ef5` 用 `fild` 把它们转 float，
所以服务端写 int32，不要写 IEEE754 float。

### ★ 死亡时客户端**什么包都不发**

会话 09 那一局的完整证据：

```text
00:29:03  C→S 0x0408      ← 最后一个包
          （玩家在这前后血量归零，人物站着不动、无法控制）
   ...    整整 11 分钟一个包都没有...
00:40:04  C→S 0x040f      ← 倒计时归零，gcpEndQuest
```

活着时客户端持续发 `0x0406`（位置同步）/ `0x0408`（疑似开火）/ `0x0410`（分数），
死后完全静默。**说明死亡判定本来在服务端** —— 真服务器按 `0x0408` 的伤害
自己算血量，然后主动下发 `0x0419` 让角色重生。

本地假服务端算不了血量，所以 `server/gameserver.py` 用「**战斗中静默超过
`RESPAWN_IDLE_SECONDS`(8 秒) 就补一发 `0x0419`**」当替代触发
（`Conn.maybe_respawn()`，靠 `sock.settimeout(1.0)` 定期回到循环顶端检查）。
**这个触发条件是推断的**，误判的代价只是角色被传送回出生点，不会崩。

重生坐标默认用实测 `0x0406` 里带的 `float 3225.0 / 635.0` → `int 3225 / 635`
（`DEFAULT_RESPAWN_X/Y`）。**真正的关卡出生点还没查到。**

### 顺带：`0x0403` 的服务端方向 = 回房间

`0x5518fb`（就在 `0x551804` 下面）：`mov [LobbyStage+4], 2` + `push 5 / call 0x40e47f`
→ **切回 stage 5（RoomStage）**，并清 `[LobbyStage+0x1e8..]`。
结算看完之后要回房间，多半就是发这个。

### 实机验收（会话 09 末）：`0x0419` 格式对，但触发条件和坐标都还不对

```text
[00:57:03.165] 战斗中静默 8.1s，判定角色死亡卡住 (第 1 次)   ← 剧情演出期间就误触发
[00:57:03.167] ← 回 gspRespawnCharacter(id=0, x=3225, y=635)
   ...客户端没崩，正常进入战斗，持续发 0x0408 / 0x0410...
[00:59:39.096] 战斗中静默 8.1s，判定角色死亡卡住 (第 10 次)
```

截图 `logs/shot_s09e_state.png`（关卡第二个场景，计时器 10:13）：

- ✅ **包格式正确**：客户端接受并执行了，全程不崩。
- ❌ **坐标不对**：角色被传送到**地图最左边缘**卡住。
  `DEFAULT_RESPAWN_X/Y = 3225/635` 是上一局在**另一张场景**记录的
  `0x0406` 坐标，闯关关卡是**多场景推进**的（神秘岛第一幕海滩 → 第二幕森林），
  同一个绝对坐标换了场景就没有意义。
- ❌ **触发条件误判**：截图里右上角战绩栏「生命 ♥♥♥」**一颗没少**，
  角色根本没死，只是挂机不操作 → 静默 → 被反复传送。

所以「静默 8 秒 = 死亡」这个替代信号**站不住**：客户端在剧情演出、
挂机、场景切换时都会静默。

**下一步方向**（按性价比排序）：

1. 找真正的死亡信号。入手点：`0x0408`（18 字节）的服务端方向处理器 `0x551e44`
   —— 如果它是伤害包，服务端就能自己算血量。
   另可查 `Packet_gspCreateObject` / `gspEventValueChanged` 等是否携带血量。
2. 重生坐标不能写死。应该从关卡当前场景取出生点，
   或者干脆回显客户端最近一次 `0x0406` 里的坐标（它自带 float X/Y）。
3. 在没解决 1 之前，**建议把 `RESPAWN_IDLE_SECONDS` 调大或直接关掉**
   —— 误传送比不重生更影响体验。

## 88. ★★★ 乱发 `0x0419` 会被客户端判成作弊：`0x0106 gcpReportHack` 是硬反馈信号

会话 09 末，用户报「提示『15秒内没有向前移动将强制退出』，15 秒后没真退出，
但会被传送到边缘位置无法移动」。日志给出了完整因果链：

```text
01:04:51.534  服务端：静默 8.1s → 发 gspRespawnCharacter(id=0, x=3225, y=635)
01:04:51.557  客户端：★ 0x0106 gcpReportHack（46 字节）—— 仅 23 毫秒之后
01:04:53.555  客户端：0x040d（8 字节）
   ...每 8 秒重复一次，实测重复到第 41 次...
```

**`0x0106 gcpReportHack` 紧跟在每一发 `0x0419` 后 23ms** —— 客户端把服务端
下发的这次传送判定成了非法位移。

### 完整因果链（全部由服务端的错误下发引起）

```text
静默误判（剧情/挂机也静默）
  → 发 0x0419 把角色传到 3225/635（上一场景的坐标，本场景非法）
  → 角色卡在地图边缘无法移动
  → 触发闯关模式的反挂机机制：「15秒内没有向前移动将强制退出」
  → 客户端 0x0106 gcpReportHack 报告异常
  → 强制退出流程同样要服务端配合，我们没接 → 15 秒后并不会真的退出，卡死
```

### 两条可复用的教训

1. **`0x0106 gcpReportHack` 是免费的正确性检查器。**
   服务端下发的位置/状态类包只要不合法，客户端会在几十毫秒内报上来。
   以后加任何影响角色状态的下发，都应该盯着日志里有没有冒出 `0x0106`
   —— 它比截图快得多，也比「客户端没崩」这个标准严格得多。
2. **闯关模式有强制推进机制，「挂机等超时」这条测试路线走不通。**
   不向前推进 15 秒就会被警告。想抓关卡结束的 `0x040f gcpEndQuest`，
   要么真的把关打完/打输，要么找到跳过关卡的手段，
   **不能靠让角色站着等 12:30**（会话 09 因此一直没能实机验证 `0x0411`）。

### 处置

`RESPAWN_IDLE_SECONDS` 默认值改成 **0（关闭）**，
另加 `--respawn-idle <秒>` 供临时试验。`0x0419` 的**包格式是验证过的**
（客户端接受并执行传送、不崩），缺的是「什么时候发」和「发哪个坐标」，
见 §87 末的下一步方向。

**注意进程版本**：`server/gameserver.py` 改完必须重启服务端进程才生效。
本次就是因为服务端是 00:53 启动的旧实例，01:01 改的默认值没生效，
用户又多踩了一轮。

---

# 第十一批发现 —— 战斗包的两条错误线索被推翻（2026-08-07 会话 10）

## 89. ★★★ `0x0408` 的服务端方向**不是伤害包**，是「网络状态不佳」告警

PROGRESS / §87 留的下一步是「查 `0x0408` 的服务端方向处理器 `0x551e44`，
看能不能反推出伤害/血量结构，让服务端自己算死亡」。**这条线索是错的。**

处理器地址本身没记错（跳表 `@0x54e5ae` 里 `0x0408 -> 0x54e358 -> call 0x551e44`），
但 `0x551e44` 里根本没有反序列化调用，它做的是：

```asm
00551e58  循环 esi = 0..5                       ; 遍历 6 个座位
00551e62  call 0x4045f9                         ; 座位是否占用
00551e71  call 0x54bfe3                         ; 某个状态检查
00551ea2  push 0x691df8                         ; ★ 格式串
00551f33  call [[0x72e30c]+0x18]                ; 播到聊天框
```

`0x691df8`（UTF-16 韩文）= `!! %s 님과의 네트워크 상태가 좋지 않습니다 !!`
（「与 %s 的网络状态不佳」）。**它是延迟告警的广播包，服务端千万不要发。**

所以「让服务端自己算血量」这条路在 `0x0408` 上没有入口。
客户端方向的 18 字节 `0x0408` 是什么仍未知（`GCP_NAMES` 里也没有它）。
注意 `gcpAccumulatedWeaponDamage` 是 **`0x0505`**，不是 `0x0408`。

## 90. ★★★ 闯关模式下角色死亡**确实什么包都不发**——这是客户端代码写死的

§87 观察到「死亡后客户端静默 11 分钟」，当时只当是现象。现在有了代码依据。

### 客户端有 `gcpRespawnCharacter`，opcode 是 `0x0413`

`Packet_gcpRespawnCharacter`（td `0x6e5ab8` / vft `0x6917cc`）唯一构造点
`0x553e5b`，所在函数 `0x553e48`：

```asm
00553e48  函数入口 (id, float X, float Y, arg4)
00553e55  mov eax, [ebp+8]              ; +0x04 = id
00553e58  fld  dword [ebp+0xc]          ; X 是 float 参数
00553e5b  mov [ebp-0x20], 0x6917cc      ; vft = Packet_gcpRespawnCharacter
00553e65  call 0x5f895c                 ; ★ ftol：float -> int
00553e6d  mov [ebp-0x18], eax           ; +0x08 = (int)X
00553e70  call 0x5f895c
00553e75  mov [ebp-0x14], eax           ; +0x0c = (int)Y
00553e7e  mov [ebp-0x10], eax           ; +0x10 = arg4
00553e8a  push 0x413                    ; ★★★ opcode = 0x0413
```

**结构与服务端方向的 `0x0419 gspRespawnCharacter` 完全一致**（4 个 int32，
坐标同样是「float 转 int」上线）。服务端方向的 `0x0413`（跳表 `0x54e342`
→ `0x55195d`）用的甚至是同一个反序列化器 `0x54c5d0`。

### 但闯关模式走不到那一步

发送点 `0x4fe8d7` 的唯一调用者是 `0x4fe78f`（角色重生逻辑，`this` = 角色对象，
由每帧状态机 `0x4fe343` 在 `[char+0x2b4] != 0` 时调用）：

```asm
004fe7aa  or  [ebp-8], 0xffffffff       ; 重生点索引，初值 -1
004fe7b9  cmp [LobbyStage+0x1c], 4      ; ★ SessionDescriptor.type == 4 ?
004fe7c0  mov byte [ebp-1], 1           ;   [ebp-1] = 1 表示「要发网络包」
004fe7c4  jne 0x4fe7d9                  ; 闯关是 type 2 -> 走这边
004fe7d9  call [[0x72e2dc]+0xa4](charId); GameContext 的虚函数
004fe7e8  test eax,eax / je 0x4fe7fa    ; ★ 返回 0 -> 落到下面
004fe7fa  cmp [LobbyStage+0x1c], 0 / je ret
004fe80f  cmp [char+0x614], 0 / jne ret
004fe81a  cmp [ebp-8], -1 / je 0x4fe90e ; ★★ 索引还是 -1 -> 直接返回
```

只有走 `0x4fe7ce → call 0x4fe70e` 那条路 `[ebp-8]` 才会被填；
`0x4fe7fa` 这条分支根本没调它，于是 `0x4fe81a` 必然跳走 —— **函数什么都不做**。
最后那句 `cmp byte [ebp-1], 1 / call 0x553e48`（发 `0x0413`）永远到不了。

**结论**：闯关模式（`GameContextQuest`）里，角色死亡是客户端内部状态，
既不上报也不请求重生。「服务端下发 `0x0419` 让角色复活」这个机制属于对战模式
（type 4 / `[GameContext+0xa4]` 返回非 0 的那些），**闯关主线用不上**。

§87「真服务器按 0x0408 的伤害自己算血量」的推测，两头都不成立。

### 推论

- `0x0419` 的实现留着（格式已验证），但**别再找「什么时候发」了** ——
  闯关里就不该发。§88 那串 `0x0106 gcpReportHack` 是这个判断的旁证。
- 玩家死亡后「站着不动无法控制」，要么是关卡脚本自己的等待状态，
  要么缺的是别的包。等有了控制通道（§91）可以逐个试，不必再静态硬推。

## 91. ★★ 调试控制通道：不用打完关卡也能验证战斗应答

`server/gameserver.py --control-port 27800`（默认开）+ `tools/gs_ctl.py`。
一行一命令的纯文本 TCP，服务端把命令翻成真正的游戏包推给当前客户端：

```text
python tools/gs_ctl.py status            # 连接 / 开局状态 / 座位 / 分数 / 最后坐标
python tools/gs_ctl.py endgame-probe     # 发 0x0411，12 个业务值 = 101..112
python tools/gs_ctl.py endgame 0 1 …     # 发 0x0411，自己指定每个值
python tools/gs_ctl.py respawn           # 发 0x0419，坐标用客户端自报的
python tools/gs_ctl.py raw 0411 <hex>    # 发任意 opcode + 任意载荷
python tools/gs_ctl.py back-to-room      # 发 0x0403（服务端方向 = 切回 stage 5）
```

**动机**：§88 证明「挂机等 12:30」这条测试路线走不通（闯关有强制推进机制），
会话 09 因此始终没能实机验证 `0x0411`。有了这个通道，验证一个战斗应答的成本
从「打完一整关」降到一行命令。

两个实现要点：

- `Conn.send` 必须加锁。SimpleCipher 是**逐字节推进的流密码**，
  控制线程和收包线程交错加密会让整条流永久错位。
- `endgame-probe` 把 12 个业务值填成 **101..112**，这样结算界面上
  哪一格显示哪个字段可以一眼看出来 —— 比从 UI 代码静态反推快得多。
  （静态那条路试过：`0x4a4096` 只是把 13 个 dword 存进
  `LobbyStage + 0x3ec + seat*0x34`，真正读它的 `0x4a4b02` 是玩家标签渲染，
  绕得很远。）

## 92. `gspEndGame` 的 13 个 dword 在客户端的落位（`0x551854` 逐条核对）

`0x551804` 把包体搬成一个 13 dword 的紧凑结构再交给 `0x4a4096`：

```text
紧凑结构        <- 包字段
+0x00 (1 字节)  <- pkt+0x08 的低字节（成功 bool）  ★ 只写 1 字节，高 3 字节是栈垃圾
+0x04           <- pkt+0x0c
+0x08           <- pkt+0x10
+0x0c           <- pkt+0x14
+0x10           <- pkt+0x1c        ← 注意：跳过了 pkt+0x18
+0x14           <- pkt+0x20
+0x18           <- pkt+0x24
+0x1c           <- pkt+0x28
+0x20           <- ★ 从来没被赋值（未初始化的栈内存）
+0x24           <- pkt+0x2c
+0x28           <- pkt+0x30
+0x2c           <- pkt+0x34
+0x30           <- pkt+0x38
```

- **`pkt+0x18` 不进结算结构**，只落到全局 `[0x72e340]`（`0x5518ce`）。
- **紧凑结构 `+0x20` 是客户端自己的 bug**：13 个 dword 里有一个从没写过。
- `0x4a4096` 的存放位置 = `LobbyStage + 0x3ec + seat*0x34`（`0x4a40b2`），
  座位有效性由 `0x4045f9` 检查（`[LobbyStage + seat*0x3c + 0x40]`，
  顺带确认 **`[0x72e29c]` 就是 LobbyStage**）。

## 93. ★★★★ `0x0411 gspEndGame` 实机验证通过（会话 10，控制通道推的）

会话 09 一直没能验证它（要打完整关）。用 §91 的控制通道在战斗中直接推一发，
**一次就验完了**。

### 实验

关卡「神秘岛 简单」，计时器 12:59，角色站在第一幕海滩上（`logs/shot_s10e_ingame.png`）。

```text
python tools/gs_ctl.py endgame-probe
→ ok 已发 0x0411 seat=0 success=True values=[101,102,…,112]
```

### 结果 1：客户端立刻走关卡结束流程（`logs/shot_s10f_endgame.png`）

一秒内，画面上**全部战斗 HUD 消失**：顶部关卡计时器、右上角战绩栏
（生命/分数）、左下角色头像 + HP/SP 条、右下武器栏，光标变成沙漏。
关卡场景本身还在（角色仍站在沙滩上）。**客户端不崩。**

### 结果 2：四个全局逐字段对上 —— 包格式完全正确

`tools/probe_endgame.py`（本会话新增）读客户端内存：

```text
[0x72e330] =  105   <- pkt+0x1c = values[4]   ★ 累加，初值 0，加了一次 105
[0x72e33c] =  102   <- pkt+0x10 = values[1]
[0x72e340] =  104   <- pkt+0x18 = values[3]
[0x72e344] =  103   <- pkt+0x14 = values[2]
```

四个值和 §87 的字段表**一个不差**。`0x0411` 的 14 个 int32 线格式就此坐实，
`build_end_game` 不用再改。

### 结果 3：结算界面被创建了，但没显示出来

`[GameContext+4] == 1`。`0x4913fc` 开头是
`cmp byte [esi+4], 0 / jne <ret>` + `mov byte [esi+4], 1`（防重入），
所以这个 1 证明**结算界面构造函数真的执行了**。但画面上没有它。

随后 `[0x72e2dc]`（GameContext）变成 **0** —— 关卡上下文被销毁了，
关卡是真的结束了，不是卡住。

**推测**：结算界面属于 stage 5（RoomStage）的 UI，要先把客户端切回房间才可见。
`0x0403` 的服务端方向处理器 `0x5518fb` 正好是
`mov [LobbyStage+4],2` + `push 5 / call 0x40e47f` = 切回 stage 5（§87 末）。
控制通道的 `back-to-room` 就是发这个，**下一轮实机先试它**。

### ★ 结算数据的基址不是 LobbyStage

`0x4a4096` 把 13 个 dword 存到 **`GameContextQuest + 0x3ec + seat*0x34`**。

容易看错的地方：`0x4a4096` 开头 `mov ecx,[0x72e29c]` 只是拿 LobbyStage 去做
座位有效性检查（`0x4045f9` 读 `[LobbyStage + seat*0x3c + 0x40]`）；
真正的基址是 `[esp+8]`，来自调用方 `0x5518a7: push eax`，
而那个 eax 是 `0x551844` 的 `dynamic_cast<GameContextQuest*>([0x72e2dc])`
（两个 TypeDescriptor：`0x6dfdd4 = GameContext`、`0x6e0710 = GameContextQuest`）。

**副作用**：`0x55184e` 的 `je 0x5518de` 意味着——非闯关模式下 cast 失败，
13 个 dword 根本不搬，但结算界面照样弹。

### 未完成

12 个业务值**分别显示在结算界面哪一格**还没看到（界面没显示出来）。
下一轮：`endgame-probe` → `back-to-room` → 截图，再用
`probe_endgame.py <pid> 0` 读 `GameContextQuest+0x3ec` 核对落位表（§92）。

### `0x0309`（302 字节）是遥测，不是结算数据

实机 hexdump 一眼看穿：

```text
00 00                                    u16 0
08 00  "testuser"(UTF-16LE)              wstr 玩家名
8c 00  "|Boss00Fps|MinFPS:196,FPS:232,CPU:11th Gen Intel(R) Core(TM)..."
                                         wstr 诊断串
```

即 `0x0309` = **性能/环境统计上报**（FPS、CPU 型号），关卡结束时随手报给服务端的遥测。
**它不需要应答**，也不含任何结算数值。

所以结算数据要到 `0x030a`（6 字节）和 `0x0505 gcpAccumulatedWeaponDamage`（48 字节）里找 ——
下个会话从这两个入手，`0x030a` 只有 6 字节，成本最低，先解它。

---

# 第十一批发现 —— 账户数据下发链 + 教程回环（2026-08-07 会话 11）

## 95. ★★★★ `gspRepLogin` 的 8 个业务 int32 **全部**落位查明了（处理器 `0x54f2cc`）

以前只知道第 1 个是等级、第 7 个是教程状态，其余 6 个按 D019 填 0。
其实处理器把包对象的每一个字段都搬了出去，一条不落，读一遍就全有了：

```asm
0054f2cc  ; Packet_gspRepLogin 的处理器，[ebp-0x60] = 栈上的包对象(+0x00=vft)
0054f2f7  mov edi, [ebp-0x5c]              ; pkt+0x04  结果码
0054f302  lea eax, [ebp-0x58] ... 0x402cd7 ; pkt+0x08  string
0054f326  mov eax, [ebp-0x50]
0054f329  mov [0x72e338], eax              ; pkt+0x10  ★ 等级（§63）
0054f314  mov eax, [ebp-0x4c]
0054f317  mov [esi+0x89c], eax             ; pkt+0x14  ★ 频道码
0054f31d  mov eax, [ebp-0x48]
0054f320  mov [esi+0x8a0], eax             ; pkt+0x18  ★ 频道序号
0054f32e  mov eax, [ebp-0x44]
0054f331  mov [0x72e33c], eax              ; pkt+0x1c  ★★ 总经验
0054f336  mov eax, [ebp-0x40]
0054f339  mov [0x72e340], eax              ; pkt+0x20  ★★ 本级起始总经验
0054f33e  mov eax, [ebp-0x3c]
0054f341  mov [0x72e344], eax              ; pkt+0x24  ★★ 下一级所需总经验
          ; pkt+0x28 -> [0x72e2a4]+0x64      新手教程状态（§54）
0054f356  mov [0x72e378], eax              ; pkt+0x2c  语义未查
0054f346  mov [0x72e354], eax              ; pkt+0x34
0054f34e  mov [0x72e358], al               ; pkt+0x38 (byte)
0054f370  mov [0x72e380], al               ; pkt+0x39 (byte) 活动弹窗，§54 末
```

即 `build_gsp_rep_login` 的 8 个业务 int32 依次是：

```text
[0] 等级  [1] 频道码  [2] 频道序号  [3] 总经验  [4] 本级起点  [5] 下一级所需
[6] 教程状态  [7] ?（唯一还没查的，-> 0x72e378）
```

经验三件套落在 `gspEndGame` 用的同一组全局，所以编码规则一样：**三个都是绝对累计值**，
右上角数据栏自己做减法（§94）。

### ★ 金币不在登录包里 —— 这就是「重登金币归零」的原因

`0x54f2cc` 从头到尾**没有碰 `0x72e330`**（金币）。全镜像写 `0x72e330` 的只有三处：
`0x5518c1`（`gspEndGame` 的 `+=`）、`0x5523e8`、`0x553871`。金币只能靠下面这个包下发。

## 96. ★★★★ `0x0600 gspRepMoney` = 整份玩家数据栏（金币 + 经验 + 等级）

**opcode 是 `0x0600`**，从游戏分发树 `0x54e40c` 的减法链读出来：

```asm
0054e40c  sub eax, 0x507 / je 0x54e437   -> 0x553dc9      (0x0507)
0054e413  sub eax, 0xf9  / je 0x54e42c   -> ★ 0x553855    (0x0600)
0054e41a  dec eax        / jne 未处理    -> 0x55420e      (0x0601)
```

RTTI：`vft 0x69185c` = `.?AUPacket_gspRepMoney@@`（同类还有另一张 vft `0x6150c4`）。

反序列化 `0x54c7c3`（vft 槽 1）+ 处理器 `0x553855`，共 **30 字节**：

| 包偏移 | 宽度 | 落到全局 | 语义 |
|---|---|---|---|
| `+0x04` | int32 | `0x72e334` | 未查（另一种货币？`0x45814e` 读它）|
| `+0x08` | int32 | `0x72e330` | ★ **金币**（绝对总额，直接 `mov`）|
| `+0x0c` | int32 | `0x72e33c` | ★ 总经验 |
| `+0x10` | int32 | `0x72e340` | ★ 本级起始总经验 |
| `+0x14` | int32 | `0x72e344` | ★ 下一级所需总经验 |
| `+0x18` | **u16** | `0x72e338` | ★ 等级（`0x5d59f1` 读 **2 字节**，`movsx` 扩展）|
| `+0x1c` | int32 | `0x72e390` | 未查（全镜像只有这一处写，无读）|
| `+0x20` | int32 | `0x72e394` | 同上 |

★ **等级那一格是 u16**，写成 int32 后面两个字段就整体错位 2 字节。

### 实机验证通过（会话 11）

登录后补发一发 0x0600（`money=1234, experience=250, level=3`），大厅右上角立刻显示
**「金币: 1234」+ 经验进度条 50%**（`(250-200)/(300-200)`），截图
`logs/shot_s11b_money.png`。服务端侧实现见 `build_rep_money` / `Conn.send_rep_money`，
控制通道加了 `sync-account` 命令（改完 JSON 不用重登就能刷新画面）。

另外两个也写这组全局的处理器（都还没接）：`0x5523e8`（同时 `inc word [seat+0x10]`
= 有人升级的广播）、`0x553871` 就是本节这个。

## 97. ★★★★ `0x030f gcpReqFirstUserResult` = 客户端上报「新手教程通过了」

发送点只有一个，`0x4f22c1`，三条指令把整件事说得明明白白：

```asm
004f22c1  mov ecx, [0x72e29c]      ; LobbyStage
004f22c7  call 0x4054fa
004f22cc  mov eax, [0x72e2a4]
004f22d1  push 4 / pop edx
004f22d4  call 0x40e47f            ; ★ 切回 stage 4（大厅）—— 客户端自己切，服务端不用管
004f22d9  mov eax, [0x72e2a4]
004f22de  push esi
004f22df  mov [eax+0x64], esi      ; ★★ 写本地教程状态，正是大厅 0x43b354 拿去
                                   ;    `cmp 3 / jge` 的那一个（§54）
004f22e2  call 0x5538f2            ; ★★ 把同一个 esi 用 0x030f 发给服务端
```

**客户端先自己记下新状态，再把同一个值原样告诉服务端** —— 服务端只要存下来，
不用推断也不用换算。三个调用点传的值：

```text
0x4f3aa1  push 5 / pop esi   -> 上报 5
0x4f41c9  push 4 / pop esi   -> 上报 4   （前置 call 0x4f31c2 返回真才走）
0x4f41fd  push 4 / pop esi   -> 上报 4
```

都 >= 3，都算完成。载荷 = **1 个 int32**（序列化 `0x404ee8` 只写 `pkt+0x04`，
`0x5d591f` 写 4 字节）。

服务端方向的同号包有处理器 `0x4089c3`（大厅跳表 §81），但它**不读包体**，
只是清 `[LobbyStage+0x49c]`、置 `[LobbyStage+4]=2`、加载两份 UI 资源。
客户端已经自己切回大厅了，所以按 D028 **不回显**，只记账。

实现：`parse_first_user_result` + `Conn.on_first_user_result` →
`AccountStore.set_tutorial_progress`（同时置 `tutorial_completed=true`）。

## 98. ★★★★ `0x0309` 的**服务端方向**是 `gspRepGameResult` —— §93「不需要应答」下早了

§93 只看了客户端**发**的 0x0309（302 字节 FPS/CPU 遥测），就判定这个 opcode 不用回。
实际上服务端方向的 0x0309 在游戏分发树里有处理器 **`0x55210d`**（§85 已记录但没查），
而它构造的是 `vft 0x691898` = **`.?AUPacket_gspRepGameResult@@`**。

### 线格式（反序列化 `0x54c6b4`）

```text
+0x04  int32    ★ 座位号（处理器拿它做座位有效性检查）
+0x08  int32
+0x0c  bool32   （0x5d59de 读 4 字节存 1 字节）
+0x10  int32
+0x14  int32
+0x18  int32
+0x1c  int32
+0x20  int32
+0x24  bool32
+0x25  bool32
+0x28  int32
+0x2c  int32
+0x30  int32
+0x34  int32 n + int32[n]     ← 0x408fc9，和 gspRepQuestRecordInPvp 用的是同一个数组读取器
```

**13 个字段，正好 0x34 字节** —— 与 §93 里 `0x4a4096` 把 13 个 dword 存到
`GameContextQuest + 0x3ec + seat*0x34` 的那个 0x34 完全对上，也和 §92 的落位表同宽。
即 `0x0411 gspEndGame` 和 `0x0309 gspRepGameResult` 携带的是**同一组 13 个 dword**，
后者多一个尾部数组。

### 处理器 `0x55210d` 做的事 —— 它才是真正写结算数据、判胜负的那个

```asm
0055211f  [ebp-0x60] = 0x691898        ; 栈上建包
00552138  call [0x69189c]              ; Deserialize
0055213e  esi = [0x72e29c]             ; LobbyStage，为 0 就退出
0055214c  ebx = pkt+0x04               ; 座位号
00552156  call 0x4045f9                ; 座位有效性检查，假就退出
00552163  cmp [esi+0x1cc], ebx         ; 是不是我的座位
005521bb  ebx = [0x72e2dc]             ; ★★ GameContext（关卡上下文）
005521d2  循环 6 次把 pkt 里的一段搬到 [GameContext+0x184]
005521ec  call 0x493dd4(GameContext, pkt+0x25)
005521f6  [GameContext + seat*4 + 0x2c] = pkt+0x1c
005521fd  [GameContext + seat*4 + 0x44] = pkt+0x24
00552204  [GameContext + seat*4 + 0x5c] = pkt+0x20
0055220b  [GameContext + seat   + 0x164] = pkt+0x0c
00552231  call [GameContext_vft+0x10c](我的座位)   ; 名次？
0055223f  setge al                                  ; >=0 -> 赢
00552242  cmp [LobbyStage+0x1c], 2                  ; ★ 房间类型 2 = 闯关，分支选不同文本
00552291  call 0x5c14dc                             ; 胜/负表现
```

### ★★ 由此得到的下一步假设（**还没实机验证**）

真服务器的结算顺序应该是 **先 `0x0309` 后 `0x0411`**：
`0x0309` 把每个座位的结算数据写进 GameContext 并判胜负，`0x0411` 才结束关卡、
更新经验金币、弹结算界面。我们只发了 `0x0411`，界面因此拿不到数据 ——
这正好解释 §93「构造函数执行过但画面上没有」。

**硬约束**：`0x55210d` 直接解引用 `[0x72e2dc]`（GameContext），
关卡结束后它会变成 0（§93 实测），那时再发就是空指针崩溃。
**`0x0309` 必须在战斗中、`0x0411` 之前发。**

验证方法（控制通道，成本一行命令）：进关卡 → `raw 0309 <载荷>` → `endgame` → 截图。

## 99. ★★★★★ 结算界面弹出来了 —— 顺序是 `0x0309` → `0x0411`（会话 11 实机）

§98 的假设**一次就验证通过**。实验（关卡「神秘岛 简单」，计时器 11:00，战斗中）：

```bash
python tools/gs_ctl.py raw 0309 <80 字节全 0，n=6>
python tools/gs_ctl.py endgame
```

### 结果：`logs/shot_s11n_endgame.png`

「**游戏结果**」整页显示出来了，左栏逐项：

```text
未完成                    ← 红黄标签
[Lv.3 星标] testuser + 角色立绘
分数 / 生命    0 / 3
经验值         +0
竞技场分数     +0
金币           +0
合成材料       （空）
称号卡片       （空）
```

右侧是 5 列 × 十几行的空表格（多人对战的名次表，单机只有一行）。

### ★ 两个包分工不同 —— 这是这次最有价值的一条

发 `0x0411` 时走的是**真结算路径**（`experience=250 / next=300 / start=200`，
绝对值），但结算界面显示的是「经验值 **+0**」「金币 **+0**」，也就是**增量**。

所以：

| 包 | 数据去向 | 数值语义 |
|---|---|---|
| `0x0411 gspEndGame` | 右上角玩家数据栏的四个全局 | **绝对累计值**（金币那格是 `+=` 本局所得）|
| `0x0309 gspRepGameResult` | `GameContext` 里的结算表 → **结算界面** | **本局增量**（经验 / 金币 / 竞技场分数…）|

**只发 `0x0411` 界面拿不到数据，所以构造了也不显示** —— §93 观察到的
「`[GameContext+4]==1` 但画面上没有」就是这么来的。

### 硬约束

- **`0x0309` 必须在 `0x0411` 之前发**：`0x55210d` 直接解引用 `[0x72e2dc]`
  （GameContext），关卡一结束它就变 0（§93 实测），那时再发就是空指针。
- **尾部数组至少要有 6 个元素**：`0x5521d2` 的循环无条件读 `pkt+0x34` 那个
  vector 的 6 个 dword 搬到 `[GameContext+0x184]`。数组为空时 `[ebp-0x2c]`
  还是构造时置的 0，读 NULL 直接崩。
- 全 0 载荷（80 字节）实测**不崩、不触发 `0x0106 gcpReportHack`**，
  战斗继续正常进行 —— 线格式（13 个 int32 + `n` + `n` 个 int32）就此坐实。

### 线上字节流（注意两个 bool 各占 4 字节）

`0x5d59de` 读 **4 字节**再折成 1 字节 bool，所以线上是 int32：

```text
int32  座位号        ← 必须等于客户端认为的自己的座位（[LobbyStage+0x1cc]）
int32  ?
int32  ?  (bool)     -> [GameContext + seat + 0x164]
int32  ?
int32  ?
int32  ?             ← 非 0 时走 0x552170 的提示分支
int32  ?
int32  ?
int32  ?  (bool)
int32  ?  (bool)     -> 0x493dd4 的第 2 个参数
int32  ?             -> [GameContext + seat*4 + 0x2c]
int32  ?             -> [GameContext + seat*4 + 0x5c]
int32  ?             -> [GameContext + seat*4 + 0x44]
int32  n
int32  values[n]     ← 前 6 个 -> [GameContext+0x184]
```

n=6 时载荷 **80 字节**。

### 未完成

13 个字段分别对应界面上哪一格还没认（这一发全填 0）。方法同 D038：
发特征值（`gameresult-probe`）再对着截图认。「未完成」这个标签也说明
成功/失败标志在 `0x0309` 里，不是 `0x0411` 的 `success`——
那一发 `success=True` 但界面照样写「未完成」。

## 100. ★★★ 结算之后：`0x0405` 是「结算看完了」，回房间会把金币清 0

### `0x0405`（客户端 -> 服务端，载荷 0）= 结算界面看完了

会话 11 实测：`0x0411` 之后客户端先报三个包（`0x0309` 遥测 292B / `0x030a` 6B /
`0x0505` 48B），**再过 9 秒整发一个空的 `0x0405`**，然后停在结算界面上等。

服务端收到它就发 `0x0403`（切回 stage 5 房间），实测客户端干净回到「待机房间」，
玩家列表、地图、F5 按钮都正常。**「结算 → 回房间」这条链就此闭合。**

⚠ 服务端方向的同号 `0x0405` 是完全不同的东西：跳表 `@0x54e5ae` 指向 `0x551d35`，
它读**两个 int32**（`0x5590bb`）再拿座位号查角色对象、调 `vft+0xd4`。
所以判断要严格 —— 只有本局已经发过结算包时才把收到的 `0x0405` 当「看完了」。

### ★ 回房间后金币会变成 0（经验和等级不受影响）

实测：结算前金币 1234，走完 `0x0309` → `0x0411` → `0x0403` 回到房间后，
右上角变成「金币: 0」，而「Lv.3」「经验值 50/100」原封不动。
再发一发 `0x0600 gspRepMoney` 就恢复成 1234。

所以**回房间后必须补一发 `0x0600`**。已实现在 `Conn.leave_game_result`。

### ⚠⚠ 死路：`gameresult-probe` 的特征值会让客户端**主动断开连接**

发 `0x0309` 的 12 个业务值 = 201..212、尾部数组 = 301..306 之后，
客户端 **20 毫秒**内就 `close()` 了 socket（服务端日志「对端关闭」）。
**进程没崩、没有崩溃报告**，纯粹是主动断链，等于协议层面的「我不接受这个包」。

对比：同一位置发**全 0** 的 80 字节载荷完全正常（战斗继续、不断链、
后续 `0x0411` 照常弹结算界面）。所以是某个具体字段的值触发的，不是长度或形状问题。

嫌疑最大的三个（下次逐个试，别再一次全填）：

```text
values[4] -> pkt+0x18   非 0 会走 0x552170 的分支（构造 0x691d58 的字符串 ->
                        0x558916 -> 0x40f304），全 0 时这条分支根本不进
values[7] -> pkt+0x24   bool，线上 4 字节
values[8] -> pkt+0x25   bool，交给 0x493dd4 当第 2 个参数
```

**下次的做法**：保持前 9 个值为 0，只让 `values[9..11]`
（-> `[GameContext + seat*4 + 0x2c / 0x5c / 0x44]`）非 0 ——
这三个最像结算界面上的「经验值 / 竞技场分数 / 金币」三格。

### 顺手修掉的坑：`tools/click.py --key` 要十六进制

实现原来是 `int(x, 0)`，不带 `0x` 前缀的 `74` 会被当十进制 74 = VK `0x4a`，
**静默按错键**（这次找不到为什么 F5 没反应就是它）。已改成 `int(x, 16)`，
`74` 和 `0x74` 现在都对。房间里点「F5 游戏开始」按钮不如直接按键可靠：

```bash
python tools/click.py <pid> --key 0x74
```

另外房间挂久了会弹「90秒无任何动作，返回至游戏大厅。」的提示框。

> ⚠ **这段话在会话 12 被推翻了一半，见 §101。**
> 「点它自己的确认也点不掉」是**坐标算错**（截图像素比客户区坐标高 29 像素），
> 而且那不是一个框、是每 90 秒叠一个的一摞。
> 现在 `bshook` 已经把挂机计时器 patch 成 12.4 天，正常不会再见到它。

## 101. ★★★★ 「90秒无任何动作，返回至游戏大厅。」的完整机制（会话 12）

用户报的两个现象——**任务退出回房间就立刻弹**、**点确认关不掉**——是同一条链上的
两截。查法还是 D034：中文串在镜像里搜不到，韩文原串搜得到。

```text
0x0066a9e0  '1분 30초 이상 동작이 없어 로비로 돌아갑니다.'   ← 1分30秒 = 90 秒
0x0066aa18  '빅샷 알림'                                       ← 标题（中文版渲染成「提示」）
```

唯一引用点 `0x46767a`，在 **`RoomStage::Update`**（`RoomStage` vftable `0x66a38c`，
COL `0x698e24`）里。

### 判定链（`0x46760f`）

```text
eax = [0x72e29c]                 ; LobbyStage，NULL 就跳过
ecx = eax + 0x3e8                ; ★ 挂机计时器（Timer 对象）
call 0x5d5ecc                    ; Timer::IsExpired() = (now - start) >= duration
al == 0                 -> 跳过
[RoomStage+0xf80] >= 0  -> 跳过   ; 倒计时进行中（-1 = 没在倒计时）
[0x72e354] & 0x4f != 0  -> 跳过
------------------------------------------------------------------
0x46769f  call 0x424b96          ; 弹提示框（非模态，函数立刻返回）
0x4676cb  call 0x4082ae          ; ★ 重置挂机计时器 -> 90 秒后会再弹一个
0x4676d4  call [RoomStage_vft+0xac] = 0x467303
              -> 0x46738d: [+0xf7c]=[+0xf80]=-1; call 0x406191
              -> ★ 发 gcpLeaveSession(0x0203)，然后干等服务端应答
```

### Timer 布局（`0x5d5e37 Timer::Start(ms)` 逐条读出）

| 偏移 | 含义 |
|---|---|
| +0x00 | vptr（实机 `0x65e10c`），vtable[1] = GetTick |
| +0x04 | duration，**0 = 停用**，`IsExpired` 恒 false |
| +0x08 | start tick |
| +0x0c | start + duration |

tick 的时基 = `GetTickCount() - [[0x6d8a18]+4]`（**进程相对毫秒**，不是系统 uptime）。
`[0x6d8a18]+0xc` 是 float 缩放系数，实机 1.0。

### ★ 计时器只有四个重置点，**没有一个是收包触发的**

`0x4082ae LobbyStage::ResetIdleTimer()` = `push 0x15f90(90000); ecx+=0x3e8; Timer::Start`。
调用方全部找齐：

| 调用点 | 场合 |
|---|---|
| `0x40ee3d` | 窗口过程收到 **WM_LBUTTONUP(0x202) / WM_MBUTTONUP(0x208) / WM_RBUTTONUP(0x205) / WM_KEYUP(0x101)** |
| `0x40563b` | `LobbyStage::ResetSession` (`0x4054fa`) 的尾部 |
| `0x4676cb` | 提示框弹完自己重置（于是 90 秒后再弹一次） |
| `0x4906ac` | 战斗中 `GameContext::vft+0xa4(我的座位) <= 0` 时 |

**服务端够不着这个计时器** —— `0x4054fa` 在包处理器里的唯一入口是
`0x552943`（`0x0203` 的成功应答），而那条路本身就要把客户端送回大厅。
所以「回房间时把计时器揉一下」只能改客户端。

### 现象一：任务退出回房间立刻弹

`RoomStage::Update` 只在 stage 5 跑。打关卡那十几分钟里计时器照走不误，
回到房间的**第一帧**就已经 `IsExpired`，于是立刻弹。

### 现象二：点确认关不掉 —— 其实是**弹了一摞**

`0x4676cb` 每弹一次就重置 90 秒，`0x467303` 发出去的 `0x0203`
**服务端从来没有应答过**，客户端就永远留在房间里 ——
于是每 90 秒叠一个一模一样的提示框。会话 12 实测：一个挂了 ~35 分钟的客户端，
连点 25 次「确认」才把整摞点完（`logs/shot_s12_ok25.png`）。

⚠ 顺带纠正 §100 末尾那条：「点它自己的确认也点不掉」是**坐标算错了**。
`tools/screenshot.py` 抓的是**窗口矩形**（1030x797，含标题栏和边框），
而 `tools/click.py` 用的是**客户区坐标**（1024x768）。
两者差 **(x-3, y-29)**。按截图像素直接去点会低 29 像素，正好落在按钮外面。
PROGRESS 里那张坐标表（确认 = (622, 429)）本来就是对的。

### `0x0203` 的服务端方向 = 只读一个 int32

处理器 `0x54fffe`：

```text
eax = 0x5d5984(stream)      ; 读 1 个 int32
eax == 0 -> 0x552943:
              LobbyStage::ResetSession 0x4054fa
              ecx = [[0x72e2a4]+0x54]        ; 当前 stage
              ecx == 5 || ecx == 7 -> ChangeStage(4)   ; 4 = 大厅
eax != 0 -> 弹 '퇴장 실패' / '방에서 나갈수 없습니다.'（退场失败 / 无法离开房间）
```

`0x0203`（客户端方向）**只有一个发送点** `0x406191`，四个调用方共用：

| 调用点 | 场合 |
|---|---|
| `0x46739c` | 90 秒挂机提示框 |
| `0x4a50f4` / `0x4a5a85` | 房间里的退出 / ESC |
| `0x54be4c` | 网络层状态处理 |

**所以「挂机踢出」和「玩家主动退房」发的是同一个空包，服务端区分不了也不用区分。**
在这之前服务端根本没实现 `0x0203`，**玩家其实压根没法退出房间**。

### 采取的修法（两条一起，缺一不可）

1. **服务端**：`Conn.leave_session()` 收到 `0x0203` 就回 `0x0203 result=0`，
   并把房间相关状态（`room` / `settled` / `quest_score` / `start_game`）清干净。
2. **客户端 patch**（`bshook.c` `try_patch_afk_timer`）：
   `0x4082ae` 的 `push 0x15f90`(90000ms) → `push 0x40000000`(约 12.4 天)。
   5 字节换 5 字节，机制原样保留，`deadline = start + 时长` 也不会溢出成负数。
   设 `BSHOOK_KEEP_AFK_KICK=1` 可保留原版行为。

单机没有挂机踢人的必要，而计时器又只认真实鼠标/键盘消息 ——
两条都做才既治标（提示框不再堆）又治本（根本不弹）。

### 新工具 `tools/probe_idle_timer.py`

```bash
python tools/probe_idle_timer.py <pid> --watch 2
```

打印 stage、duration、start、deadline。**盯 `start` 变没变**就知道计时器有没有被重置。

### 关卡里计时器什么时候会被重置（实测 + 反汇编）

`0x4906ac` 那条战斗内重置的判据是 `GameContextQuest` 虚表 `+0xa4`：

```text
0x4913c6  GameContextQuest::vf_a4(seat):
    eax = this + 0x384
    [eax] == 0 -> return 0x7fffffff          ; 没有那个对象就返回大数 = 不重置
    ecx = [eax]; jmp [[ecx]+0x10](seat)      ; 转发给 [this+0x384] 的 vf+0x10
调用方 0x4906a4:  返回值 > 0 -> 不重置；<= 0 -> 重置
```

实测（会话 12，分数一直是 0 的一局）：

* 关卡里按方向键（VK 0x27）**会**重置：`start` 486930 → 579018。
* 关卡里一动不动也**偶尔**被重置（579018 → 633828）—— 就是上面这条
  「`vf_a4(座位) <= 0`」的路，分数为 0 时成立。
* **结算 → 回房间这一段完全不重置**：`0x0405`(10:53:14) 之后回到 stage 5，
  40 秒后读到 `start` 还是战斗里最后那次的 633828，已闲置 **108 秒**。

也就是说「打关卡时计时器一直在走」并不绝对，但**关卡尾段 + 结算界面 +
切回房间这一整段肯定在走**，玩家一旦有分数（`vf_a4 > 0`）战斗内那条重置也停了。
所以打得越久越容易一回房间就超时 —— 和用户报的「似乎每次都弹」一致。

### 会话 12 实机验收（`logs/shot_s12_*.png`，`logs/gameserver.out`）

| 步骤 | 结果 |
|---|---|
| 房间里挂 150 秒不碰鼠标键盘 | **一个提示框都没弹**，服务端没收到 `0x0203`；`duration` 读出来是 `1073741824`(0x40000000)，patch 生效 |
| F5 → 关卡 → `gs_ctl.py endgame` → 结算 → `0x0405` → 回房间 | 回到房间时累计闲置 **108 秒**，**没弹框**（原版这时早该弹了） |
| 房间里点「后退」 | `C→S 0x0203` → `S→C 0x0203 result=0` → **stage 5 → 4**，客户端立刻恢复大厅轮询（`0x0200`/`0x020d`） |
| 回大厅后再点「建立房间」 | 房间正常重建，座位/地图面板都在 |

---

# 第十三批发现 —— 待机房间的场景与换角色（2026-08-07 会话 13）

## 102. ★★★★★ 「待机房间背景纯黑」的真凶：`Session+0x04`（房间状态）我们一直发 0

用户报的现象：**建房进去是纯黑背景、角色不能走动；打完一关回房间就有蓝天、能走动**。
两半都由同一个字段解释，而且**是我们发错的**，不是客户端 bug。

### `Session+0x04` = 房间状态，**2 = 待机中**

判据在房间列表的渲染里（`0x43e5de`）：

```asm
0043e5de  cmp dword ptr [session+4], 2
0043e5ea  jne 0x43e612
0043e5ec  push 0x665700       ; '대기중'  = 待机中
0043e612  push 0x6656f8       ; '게임중'  = 游戏中
```

### 它同时决定房间里建**哪个 3D 场景**

`RoomStage` 的构造函数 `0x466979`（`new(0xfd0)` @ `0x40bce2`，stage 5 的工厂）
在 `0x466a88` 用三个参数调游戏上下文工厂 `0x494509`：

```asm
00466a74  mov esi, [LobbyStage+4]        ; ★ 房间状态  -> callee [ebp+8]
00466a7a  lea ecx, [LobbyStage+0x18]     ; 描述符(0x14 字节，按值传)
00466a82  call 0x406a92                  ;   -> callee [ebp+0x0c..0x18]
00466a61  lea eax, [LobbyStage+0x10]     ; 地图名 TString(按值传) -> [ebp+0x20]
00466a88  call 0x494509
```

工厂开头就按**状态**分流：

```asm
00494526  cmp [ebp+8], 2  -> je 0x4948c2      ; 2 / 5 / 6
0049453c  cmp [ebp+8], 5  -> je 0x4948c2      ;   -> new(0x3a8) + 0x494b69
00494546  cmp [ebp+8], 6  -> je 0x4948c2      ;   = GameContextWaitingRoom
0049454c  否则按 descriptor.type 建**战斗**上下文
          (type 2 -> 0x49466f -> GameContextQuestNN，闯关房就是 GameContextQuest03)
```

也就是说：**状态不是 2，客户端就在房间里建了一个战斗上下文**。
战斗上下文的场景要靠 stage 6（准备/加载）那条流程铺，直接在 RoomStage 里建出来
既没地形也没天空 —— 就是一片黑，角色也没有地面可走。

### `GameContextWaitingRoom::Init`（`0x494b9f`）加载的是固定地图

```asm
00494bbf  push 0x671528      ; L"room"                 -> [World+0x1d7c]（BGM/场景标签）
00494c00  push 0x671500      ; L"Maps/ReadyRoom.map"
00494c14  call 0x558916      ; 文本表格式化
00494c23  call 0x47496a      ; ★ World::LoadMap
00494c4d  call 0x405d8c      ; LobbyStage 逐座位建/换角色对象
```

**和房间选的关卡地图无关**。`World::LoadMap`（`0x47496a`）成功后会把去掉 `.map`
的名字写进 `[World+0x1c6c]`，实机读出来是 **`room-06`**（所以文本表把
`Maps/ReadyRoom.map` 映射成了 `room-06.map`）。

### 为什么打完一关回来就好了

`0x0403`（服务端方向 = 结算完回房间）的处理器 `0x5518fb` 里有一句
**硬写**：

```asm
00551900  inc dword ptr [LobbyStage+0x3c]
00551904  mov dword ptr [LobbyStage+4], 2      ; ★ 状态强制成「待机中」
00551918  call 0x40e47f (edx=5)                ; 切回 stage 5
```

所以走这条路进房间的状态永远是 2，场景永远对。**只有建房那一路是我们发的 0。**

### 修法

`build_update_session` 的第一个 int32 从 `w_i32(0)` 改成
`w_i32(SESSION_STATUS_WAITING)`（= 2）。一行。

实机验收（`logs/shot_s13_room_fixed.png`）：建房进去**直接就是蓝天 + 中式塔楼 +
沙地的待机房间**，角色站在左边，`〈向导〉窗口化模式：F11 准备/开始：F5` 提示正常。
探针读到 `GameContext.vft = 0x6713ec (GameContextWaitingRoom)`、
`[World+0x1c6c] = 'room-06'`、`[LobbyStage+4] = 2`。

### 探针

`GameContext = [0x72e2dc]`、`World = [0x72e2d4]`、`App = [0x72e2a4]`
（`+0x54` 当前 stage、`+0x5c` 待切 stage）。
按 vftable 认上下文类型：`0x6713ec` = WaitingRoom、`0x6739ac` = Quest、
`0x674f14` = Quest03。**stage 切换是延后到主循环执行的，而游戏窗口不在前台时
主循环基本不跑** —— 发完切 stage 的包要先把窗口拉到前台再读，否则只看到
`+0x5c` 挂着待切值。

## 103. ★★★★ 换角色：客户端方向的 `0x0301` 要服务端广播回来才生效

房间右下角「人物选择」点头像**完全没反应**，是因为服务端没实现这个包。

### 客户端方向的 `0x0301`（序列化点 `0x558dcb`）

```asm
00558dd1  push 0x301                ; opcode
00558dd8  call 0x5bba0a
00558ddd  push [esi]  / 0x5d591f    ; int32 座位号
00558de6  esi = [esi+4]
00558de9  call 0x556ccc             ; SessionSlot（与 0x0300 里每一项同格式）
```

**没有 action 字节** —— 服务端方向的同号包才有（`0x40648d` 先读 1 字节 action）。
实机 59 字节载荷逐字段解对：`seat=0, occupied=1, nickname='testuser',
character_id=0/1/2, level=1`，连点三个头像时**只有 `character_id` 在变**。

### 客户端自己什么都不改，纯等服务端

服务端方向 `0x0301` 的处理器 `0x40648d` **无论 action 是几**都先把座位
反序列化进去（`0x556d9d`），再按 action 分支。action 4（`0x406520`）：

```text
0x406520  用 seat+0x04(昵称) 和 0x557128(seat+0x0c) 查到的角色名拼一行聊天
          '%s님이 %s 캐릭터로 선택되었습니다.'  -> 0x40605d 加进聊天框
0x406628  （与 action 3 共用的收尾）edi = [LobbyStage + seat*4 + 0x1d0]
          -> 0x4045f9 / 0x404d9e / 0x405fba 重建座位的角色对象
0x4066ca  -> 0x405a74 刷 UI
```

**中下那个 3D 预览和房间里站着的模型都是在这里换的。** 不回这一发，点头像
就是彻底没反应。

### 修法

服务端收到客户端方向 `0x0301` → 解出 `character_id` 存进 `accounts.json`
（新字段 `character`）→ 用 `action=4` 把座位广播回去。
`build_session_member_update(seat, 4, ...)` 已有，只差 action 4 的放行和解析侧。

实机验收：点第 2 个头像 → 房间里的角色和预览一起变成**卡希尔**（白发少女），
聊天框出现韩文原串；点第 3 个 → 变成**布洛克**（机器人）；
`accounts.json` 写进 `"character": 2`；F5 进关卡后战斗里的角色也是布洛克。

### 角色 id ↔ 头像

```text
0 = 泰尔（默认，男）   1 = 卡希尔（白发少女）   2 = 布洛克（机器人）
```

### 顺带确认的两条

- 「F5 游戏开始」按钮在**刚进房间的头几秒是暗的**，过一会儿自己亮
  —— 对应原版那句 `'대기방에 입장 후 또는 다른사람 입장 후 3초가 지나야
  시작할수 있습니다.'`（进房 3 秒后才能开始）。不是 bug。
- 换角色的聊天提示显示成韩文，说明中文文本表里没有这条 —— 原版就这样，
  不是我们的问题。

## 104. ⚠ 客户端跑久了会丢贴图（不是我们的 bug，但会误导排查）

会话 13 一开始接手的那个客户端进程（跑了约 50 分钟、中途机器上有
RDP/串流虚拟显示器进出）出现**全局贴图丢失**：房间/大厅的装饰边框、
「F5 游戏开始」按钮全变成纯色块，房间场景也黑着。同一个进程 45 分钟前
（`logs/shot_s12_back_in_room.png`）还是正常的。

**症状特征**：不是某一处黑，而是**一大批 UI 贴图同时变白/变黑**，
文字和 3D 模型照常。这时候读到的画面**不能用来判断渲染类问题**——
会话 13 就是被它骗了一轮（修好 `Session+0x04` 之前先在这个进程上
试了 `back-to-room`，上下文已经换成 WaitingRoom、`room-06` 也加载了，
画面却还是黑的）。

**做法**：怀疑贴图丢失就**重启客户端**再验一次，别在老进程上下结论。

---

# 第十四批发现 —— 启动慢 / 战斗卡的根因（2026-08-07 会话 14）

## 105. ★★★★★ 「启动慢、玩着卡」= `bslog` 的 `FlushFileBuffers`，每条 2 毫秒

用户报「游戏启动很慢，玩的时候有时候很卡」。**跟渲染、跟服务端、跟机器都没关系**
—— 是我们自己的调试日志。

### 实测数据（`logs/bshook_20260807_114523_pid13448.log`，会话 13 留下的）

```text
文件大小        8,411,355 字节
总行数             96,333
其中 SNOW 行       24,229（加上它们的 hexdump 行占了全部行数的 ~99%）
相邻两行时间差   中位数 2.0 ms / 均值 2.51 ms
间隔 < 20ms 的累计     197.5 秒（94,144 条）
```

**每条日志 2 毫秒**，因为 `bslog` 在 `WriteFile` 之后无条件调
`FlushFileBuffers`（同步刷盘），外加一次 `OutputDebugStringA`（没有调试器时
也要走 `RaiseException`）。

算一笔账就明白为什么卡：SnowCipher 的 detour 每次加解密记 1 行头 +
最多 16 行 hexdump（`SNOW_MAX_LOG_BYTES=256`）= **17 × 2ms ≈ 34 毫秒**。
战斗中每个网络包都要解密 —— 一个包就是两三帧的卡顿。

启动慢同理：进大厅要解 `Pack\*.pkn`，上万次解密全打在日志上。
PROGRESS 里那句「登录 → 大厅完全渲染要 ~100 秒（读 pkn）」，**100 秒里绝大部分
是在等 flush**，不是在解密。

### 会话 03 其实已经撞到过一次

死路清单里那条「bshook 里对每次加解密都 `bslog`（含 FlushFileBuffers）
→ **把游戏拖死**，40s 都进不了登录界面」就是同一件事。当时的处理是
**把日志推迟到 connect 之后**（`g_snow_log_on` 开闸），治了「进不了登录界面」，
但 connect 之后的**全部游戏过程**仍然背着这个开销 —— 而那时协议还没解完，
逐包 dump 是刚需，所以没人再往下追。现在协议解完了，它就是纯负担。

### 修法（会话 14）

日志分成两级，默认精简：

| | 精简（默认） | 详细（`BSHOOK_VERBOSE_LOG=1`）|
|---|---|---|
| SnowCipher hook | **根本不装** | 装，逐包 dump |
| 关键事件（PATCH/HOOK/MSGBOX/WS2/D3D）| 记，且 flush | 同左 |
| 详细日志写法 | —— | `bsvlog`：写了**不 flush**、不 OutputDebugString |

- `bshook.c` 新增 `bsvlog` / `bsvlog_hex`，级别在 `DllMain` 里由
  `read_log_level()` 读环境变量决定（必须排在 patch 线程之前起，
  因为那个线程要按级别决定装不装 cipher hook）。
- 详细模式也快了很多：不 flush 让每条从 2ms 降到微秒级。
  进程退出时 OS 会把文件缓存写回，只有整机断电才会丢尾巴。
- `gameserver.py` 加 `--verbose`：默认不打逐包 hexdump、不打「试解」、
  不写 `game_*.raw.bin` / `.dec.bin`，并对 `NOISY_OPCODES`
  （`0x0406` / `0x0408` / `0x0410` / `0x0104`）**只报第一次**。
  静音只影响日志，应答逻辑一行没动（有测试守着，见
  `test_gameserver.py::NoisyPacketLoggingTests`）。

### 效果（同一台机器、同一条链路，两种模式都实机跑了一遍）

```text
                       改之前     精简(start.bat)   详细(start-debug.bat)
登录 → 大厅第一个包      ~100 秒       14.6 秒            15.1 秒
bshook 日志（进大厅时）   8.4 MB       18.7 KB            4.2 MB
战斗中连续 30 秒的增量      ——      bshook +44 字节     bshook +44 字节
关卡加载 0x0402→0x0403     ——         8.8 秒              ——
```

**★ 意外收获：详细模式也不慢了。** 原来那 100 秒里绝大部分是 `FlushFileBuffers`，
只要 `bsvlog` 不刷盘，逐包 dump 照写不误也只多花 0.5 秒。
两个改动的分工是：

- **`bsvlog` 不 flush** —— 让**详细模式**从 ~100 秒降到 15 秒（提速的大头在这）
- **精简模式不装 cipher hook** —— 让日志从 4.2 MB 降到 18.7 KB
  （省的是磁盘、格式化 CPU 和**日志可读性**，不再是启动时间）

所以现在选详细模式的代价不是「慢」，而是「日志几 MB、关键行淹在 hexdump 里、
SNOW 的 8000 次配额在加载 pkn 阶段就烧光」。

验收截图：`logs/shot_s14_lobby_fast.png`（大厅）、`shot_s14_room.png`（待机房间）、
`shot_s14_battle.png`（神秘岛战斗，HUD 齐全）、
`shot_s14_battle_debug2.png`（同一条链路在详细模式下复跑一遍）。

### ⚠ 排查时怎么用

要看逐包 dump 就跑 `start-debug.bat`（它同时开客户端和服务端的详细日志）。
既然它已经不拖慢启动了，逆协议时可以放心用 —— 但**别把它当默认**：
日常游玩每分钟往磁盘写几 MB 没有任何收益，而且下次真要查问题时，
关键事件会淹在几万行 hexdump 里。

## 106. PowerShell 5.1 读 `.ps1` 必须是 **UTF-8 with BOM**

跟 CLAUDE.md 铁律 3（`.bat` 要 UTF-8 **无** BOM）**正好相反**，很容易记混。

无 BOM 的 `.ps1` 会被 PowerShell 5.1 按系统 ANSI（中文机器上是 CP936）读，
中文注释和字符串全部乱码，而且乱码字节里常常混进引号/大括号，直接变成**语法错误**：

```text
Say "宸插叏閮ㄥ叧闂紙鍋滀簡 $stopped 涓繘绋嬶級銆? 'Green'
                                              ~
The string is missing the terminator: '.
```

写完 `.ps1` 要转（注意 `UTF8Encoding($true)` = 带 BOM，和 `.bat` 的 `$false` 相反）：

```powershell
$p='路径.ps1'; $c=[System.IO.File]::ReadAllText($p,(New-Object System.Text.UTF8Encoding($false)))
$c=$c -replace "`r`n","`n" -replace "`n","`r`n"
[System.IO.File]::WriteAllText($p,$c,(New-Object System.Text.UTF8Encoding($true)))
```

**验证**：`[System.IO.File]::ReadAllBytes($p)[0..2]` 应是 `239,187,191`。

## 107. `GameGuard.des` 之前一直没按铁律改名

会话 14 加启动脚本时才发现：`game_patched\GameGuard.des` **一直躺在原位**
（165,569 字节），和 CLAUDE.md 铁律 2 / DECISIONS D006 要求的「必须保持改名状态」不符。

之所以没出事，是因为当时 bshook 在 `0x54b0fc` 的旧 patch 让 GameGuard 校验函数直接
返回成功码 `0x755`，**nProtect 的状态取值器根本不执行**，那个文件从头到尾没被
加载过（§9 的模块日志可以复核：全程没有任何 GameGuard 模块被 map 进进程）。
会话 23 起用 §121 的 DR0 + VEH 在执行瞬间给出同一个返回值，语义不变、代码不再修改。

已改名为 `GameGuard.des.bak`，并**实机复验**改名后全链路照常
（登录 → 大厅 → 建房 → 待机房间 → F5 → 战斗，见上面几张截图）。
`tools/launch.ps1` 现在会在启动前检查这一条，文件在原位就拒绝启动。

---

# 第十六批发现 —— 死亡/重生链路（2026-08-07 会话 15）

## 108. ★★★★★ 「血量归零不死、掉岩浆不死、卡住不能操作」的完整机制

用户实机反馈：**角色血量归零后不会死、也不会重生，只是变成无法操作的状态卡住**；
岩浆关卡掉进岩浆同样不死。§90 曾判定「闯关模式下角色死亡确实什么包都不发」，
**那条结论是错的** —— 错在把 `GameContext::vf_a4` 读成「返回 0」（见文末勘误）。

真相是：**闯关模式下的死亡判定本来就在服务端**，而我们从来没接过这条链。

### 完整链路（每一环都在会话 15 实机验证过）

```text
① 伤害落地      0x50f778   HP = [char+0x150]; HP -= 伤害
                           HP<=0 -> HP=0, call [vft+0xd8]
② HP 归零       0x4ffab0   Character::OnHpZero（虚表槽 54 / +0xd8）
                           if ([this+0x2b4]) return;              // 已经死了
                           if (GameContext::vf_1c())  -> 本地直接 Die()
                           else                       -> 上报服务端    ★ 走这条
③ 上报          0x493855   GameContext::ReportDeath
                           -> 0x558f16 序列化，`push 0x408` = **opcode 0x0408**
                    ────────  服务端必须回 0x0406  ────────
④ 死亡广播      0x4938d2   GameContext 包分发表 0x493808 的 0x406 那一格
                           World::Find(句柄) -> vf_c4() -> 战绩表记一次死亡
                           -> call [vft+0xdc] = Character::Die()
⑤ 倒下          0x4ffbb7   Character::Die（虚表槽 55 / +0xdc，超长函数）
                           [char+0x150]=0（HP）、[char+0x2b4]=1（死亡标记）
                           0x501967 if ([LobbyStage+0x1c]==0)     -> [char+0x2d8]=-1
                           0x501976 if (ctx->vf_a4(charId)==0)    -> [char+0x2d8]=-1  生命耗尽
                           0x5019a8 else [char+0x2d8] = now + 5000/timescale  ★ 5 秒后重生
⑥ 每帧检查      0x4fe338   Character::Update: [char+0x2b4]!=0 -> call 0x4fe78f
⑦ 请求重生      0x4fe78f   descriptor.type==4 ? 本地重生(不发包) : **发 0x0413**
                           0x4fe70e 先选重生点，任何一条不满足就当帧不重生：
                             [LobbyStage+0x3da]!=0 / [char+0x2ac]!=[LobbyStage+0x1cc]
                             / [GameContext+0x04]!=0 / [char+0x2d8]<0 / now<[char+0x2d8]
                           0x4fe8d7 -> 0x553e48 序列化，`push 0x413`
                    ────────  服务端必须回 0x0419  ────────
⑧ 复活          0x553ecc   反序列化 0x54c5d0（4 个 int32）
                           -> GameContext::vf_d4(id, (float)X, (float)Y, 重生点索引)
```

**服务端一个字都没回过 ③ 和 ⑦，所以角色停在 ② 之后**：HP 归零、`[char+0x2b4]`
永远是 0，既不播倒地动画、也不进 5 秒重生倒计时 —— 就是用户说的「活死人」。
岩浆只是另一个伤害源，走的是同一条 ①。

### `GameContext::vf_1c` 恒为假 —— 客户端**永远不会自己判死**

```asm
0046e188  xor al, al
0046e18a  ret
```

`GameContextQuest / Quest01..07 / Pvp / WaitingRoom` 的 `+0x1c` **全部**是它。
只有 `GameContextTraining*`（`0x46f801`）和 `GameContextPromotionQuest`
（`0x46f801`）不同。所以「闯关是单机、客户端自己算死亡」这个直觉是错的。

### `0x0408`（客户端 -> 服务端，18 字节）的线格式

序列化 `0x558f16` 逐条读出来：

```text
int32 -> +0x00   角色对象句柄 = [char+0xd0]（World::Find 0x474225 用它）
u8    -> +0x04   座位号 = [char+0x2ac]；★ 怪物/NPC 是 0xff = -1
u8    -> +0x05   传给 Character::Die() 的参数 = [char+0x158]（凶手 id？实测恒 0xff）
int32 -> +0x08   [char_vft+0xc0](x, y) 的返回值，收侧交给 vf_c4
float -> +0x0c   X
float -> +0x10   Y
```

实机对上了（`logs/gameserver.out` 15:19:03 起）：

```text
HP 归零上报: NPC/怪物 (座位=-1) 句柄=0x0010c8fb 参数=255 extra=0 位置=(1571, 644)
HP 归零上报: 玩家座位 0      句柄=0x000186a1 参数=255 extra=0 位置=(2137, 640)
```

★ **怪物死亡走的是同一个包** —— 所以在这之前，玩家打死的怪其实也没「死」。
§86 那串「`0x0408 ×4` 18B `06 c9 10 00 ff 00 00 00`」正是四只怪的死亡上报
（`ff` = 座位 -1 就在第 5 个字节上，当时没认出来）。

### 服务端方向 `0x0406` = 死亡广播（**和位置同步同号，语义相反**）

读侧 `0x4938d2` 只读 **10 字节**：`u32 句柄 + u8 座位 + u8 参数 + u32`。
写侧是 18 字节。**服务端原样回显收到的 18 字节即可**（多出来的 X/Y 不读），
这也最贴近真服务器「广播给房间里所有人」的原意。

⚠ **客户端方向的 `0x0406` 是位置同步（32 字节），绝不能回显** —— 回显等于
拿位置包当死亡广播，随机杀角色。收发同号语义不同，又一次印证 D028。

### 为什么这个包能到 `GameContext`：分发器的优先级

```asm
0054e036  ServerConnection::OnPacket（vft 0x691730 +0x34）
0054e04a    if (LobbyStage) call 0x4061e2 ────┐
004061e2      LobbyStage 分发器              │
004061f5        call [GameContext_vft+0xe0]  │ ★ 一进门先问 GameContext
004061fb        if (handled) return          │
00406203        ...大厅跳表 @0x406332 ...     │
0054e091    ...游戏跳表 @0x54e5ae ...     ────┘
```

`GameContext::vf_e0` = `0x493808`（基类）/ `0x4a40ca`（`GameContextQuest*`，
它先调基类、不认再自己处理 `0x0415`）。基类分发表：

| opcode | 处理器 | 语义 |
|---|---|---|
| `0x0406` | `0x4938d2` | **死亡广播** |
| `0x0407` | —— | 认下但什么都不做 |
| `0x0412` | `0x493755` | 有处理器 |
| `0x0414` | `0x493780` | 有处理器 |
| `0x041c` | `0x493ed1` | 有处理器 |

★ **这修正了 PROGRESS 的一条禁忌**：「不要发 `0x0412`，`0x54e036` 的跳表里
没有处理器」—— 游戏跳表里确实没有，但 **GameContext 分发表里有**。
（本次没试发 `0x0412`，禁忌先降级成「语义未查」，别当成「收到就丢」。）

### `0x0413` / `0x0419` 是同一份结构，服务端**原样回显**就对

两边共用反序列化器 `0x54c5d0`：

```text
int32 +0x00  角色 id = [char+0x2ac]
int32 +0x04  X   ★ 客户端已用 ftol 把 float 截成整数
int32 +0x08  Y
int32 +0x0c  重生点索引 = [char+0x2b0]
```

**坐标是客户端自己选好的**（`0x4fe70e` 按当前场景挑重生点），所以原样回显
一定落在本场景的合法位置。会话 09「写死 3225/635 把角色传到地图边缘、
23 毫秒后收到 `0x0106 gcpReportHack`」那个坑（§88）从根上没了 ——
本次实机全程 **0 个 `0x0106`**。

### `[GameContext+0x384]` = `QuestVictoryCondition`（生命/战绩管理器）

探针实机读出来的类名。它就是「生命 ♥♥♥」的真身：

- `ctx->vf_a4(charId)` = `[ctx+0x384] ? [ctx+0x384]->vf10(charId) : 0x7fffffff`
  = **剩余生命**。`Die()` 拿它判「还能不能重生」。
- `0x48c942(1, seat)`（`0x4938d2` 在 0<=座位<6 时调）
  = `[表 + seat*0x2c + 0x60] += 1` = **记一次死亡**。
- 待机房间里 `[ctx+0x384]` 是 **NULL**（`GameContextWaitingRoom` 没有胜负条件），
  这时 `vf_a4` 返回 `0x7fffffff` = 无限命。

### 实机验收（会话 15，`logs/gameserver.out` + 截图）

神秘岛，3 条命：

```text
15:16:26.790  [ctl] 手动发 0x0406 死亡广播(handle=0x000186a1)
              -> 角色倒地，聊天框「你自己击倒了自己」(shot_s15_dead.png)
15:16:31.782  C→S 0x0413  重生请求 id=0 坐标=(664,611) 索引=1   ★ 正好 5.0 秒后
15:16:31.784  S→C 0x0419  原样回显 -> 角色站起、HP 满 (shot_s15_respawn.png)
15:19:03.311  C→S 0x0408  怪物死（座位=-1）-> S→C 0x0406 -> 怪物真的死了
15:19:04.296  C→S 0x0408  玩家死（座位=0）  -> S→C 0x0406 -> 倒地
15:19:09.281  C→S 0x0413  -> S→C 0x0419 -> 复活
15:19:20.403  第 3 次死亡（生命耗尽，[char+0x2d8] 被写成 -1，不再重生）
15:19:26.488  C→S 0x040f  ★ 关卡自动结束 -> 结算 -> 回房间
```

**「生命耗尽 -> 自动进结算」也是原来完全没有的行为** —— 以前角色卡死，
关卡只能干等 12:30 倒计时。岩浆关（`Boss00` 机械青蛙，满屏岩浆背景）复验同样通过
（15:24:08 死 -> 15:24:13 在关卡出生点 (270,809) 复活）。

### 新工具 `tools/probe_death.py`

一条命令把上面链路里的每个判据都读出来：descriptor.type / `[LobbyStage+0x3da]` /
`[GameContext+0x04]` / `[GameContext+0x384]` 的类名 / 每个角色的
HP·句柄·死亡标记·重生时刻·重生点索引·坐标，并按 `0x4fe70e` 的条件逐条给结论。
`handle` 那一列可以直接喂给 `gs_ctl.py kill <handle>`。

配套：`tools/click.py --hold <VK> <秒>`（走路要按住，单次 keybd_event 只挪一帧）。

### ★ 战斗操作（自动化跑关卡要用）

| 操作 | 输入 | 工具 |
|---|---|---|
| 左右移动 | **`A` / `D`**（VK 0x41 / 0x44）—— **不是方向键**，方向键按住没反应 | `click.py <pid> --hold 44 5` |
| 开火 | **鼠标左键**，**光标位置就是瞄准点** | `click.py <pid> <x> <y> [<x2> <y2> ...]` |
| 跳过剧情 | `ESC`（0x1b） | `click.py <pid> --key 0x1B` |
| 房间里开局 | `F5`（0x74） | `click.py <pid> --key 0x74` |

`click.py` 本来就是 `SetCursorPos` + 左键按下弹起，正好等于「瞄准 + 开火」，
一条命令给多组坐标就是连打几发。这样不用真人上手也能把关卡打出分数来。

### ⚠ 对 §87 / §89 / §90 的勘误

| 旧结论 | 实际 |
|---|---|
| §87「死亡时客户端什么包都不发」 | **发 `0x0408`**。当时把它当成了开火/命中遥测 |
| §87「静默 8 秒 = 死亡」这个替代信号 | 从来就不需要。真信号是 `0x0408`，已删掉那段启发式 |
| §89「`0x0408` 的服务端方向是网络告警」 | 没错，但那说的是**服务端方向**（`0x551e44`）。**客户端方向**是死亡上报，两码事 |
| §90「闯关模式死亡什么包都不发，客户端代码写死」 | **错**。错在把 `GameContext::vf_a4`（`0x4913c6`）读成「返回 0」—— 它在 `[ctx+0x384]==0` 时返回 **0x7fffffff**，非 0 时返回剩余生命，`test eax,eax` 那一跳几乎从不成立 |
| §90「`0x0419` 属于对战模式，闯关主线不该发」 | **正好反了**。`descriptor.type==4` 才是**本地重生不发包**；闯关（type 2）反而必须走服务端 |

**教训**：`test eax,eax / je` 这种跳转，光看跳转本身推不出走哪边 ——
必须把被调用的函数（尤其是虚函数的每个实现）读到 `ret`。§90 只读了调用点。

## 109. ★★★★ HUD 的「心形生命」和「分数」都是服务端字段，客户端自己不算

用户实机反馈（§108 修好之后）：**「死 3 次判负」正常，但 HP 上方和右上角战绩面板
的心形一颗都不减少。** 顺带查出「分数」列恒为 0 是同一类问题。

### 两处心形用的是同一个公式

右上角战绩面板 `0x4a49a4`：

```asm
004a49a4  mov  ecx, [0x72e2dc]            ; GameContext
004a49a9  push esi                        ; 座位
004a49aa  call [ctx_vft+0xa0]             ; ★ 最大生命
004a49b6  mov  [ebp-0x24], eax
004a49b9  call 0x404ff6                   ; [LobbyStage + 座位*4 + 0x1d0] = 角色对象
004a49c2  call [char_vft+0xc0]            ; ★ 已死次数
004a49c8  mov  ecx, [ebp-0x24]
004a49cb  sub  ecx, eax                   ; ★★ 实心心形数 = 最大生命 - 已死次数
   ...ecx 个实心 (sprite id 4)，补到 max(3, 最大生命) 个空心 (sprite id 5)
```

左下角 `UiMyStatusPanel` `0x4724fa` 是同一份公式，只是画法不同
（`for (i = 剩余生命; i < 槽位总数; i++) 心形数组[i] = 0;`）。

- `GameContext::vf_a0(座位)` = `0x4913aa` = `[ctx+0x384] ? [ctx+0x384]->vf_0c(座位) : 0x7fffffff`；
  `QuestVictoryCondition::vf_0c` = `0x55e095` = **`[vc + 座位*4 + 0x198]`**，构造时填 3。
- `Character::vf_c0()` = `0x4facd4` = **`[char+0x600]`**。

### `[char+0x600]` 只有服务端能改

`Character::vf_c4(n)` = `0x4ff1fd`：`if ([this+0x600] != n) 0x508412(this); [this+0x600] = n;`
（`0x508412` 只是记个时间戳 `[char+0x620] = now`，无副作用。）

**唯一的调用点是死亡广播 `0x0406` 的处理器 `0x4938d2`**，参数就是包里的第 4 个字段。
客户端从头到尾不会自己 +1。所以：

| 计数 | 存在哪 | 谁改 | 用途 |
|---|---|---|---|
| 已死次数 | `[char+0x600]` | **只有服务端的 `0x0406` 第 4 格** | **HUD 心形** |
| 死亡次数 | `[QuestVictoryCondition + 座位*0x2c + 0x60]` | `0x48c942`，`0x4938d2` **本地** +1 | 判负（`vf10` 算剩余生命） |

两份计数**分家**：会话 15 一开始把 `0x0408` 原样回显，第 4 格照抄客户端报上来的旧值，
于是 `[char+0x600]` 永远是 0（心形不动），而战绩表那份照常 +1（死 3 次照样判负）
—— 正好是用户描述的组合。**修法：回显时把这一格改成「客户端报的值 + 1」。**

`0x0408` 里这一格客户端填的是 `Character::vf_c0()`，即「我死之前已经死过几次」；
服务端回的是「你现在死了几次」。真服务器在这里是权威方，我们只是纯转发 + 1。

### ⚠ 线偏移 ≠ 客户端结构体偏移（本会话踩的坑，代价是两次客户端崩溃）

`0x558f16` 是**逐字段紧凑写**的，所以线上布局是：

```text
线 +0x00  u32    句柄
线 +0x04  u8     座位
线 +0x05  u8     凶手 id
线 +0x06  i32    ★ 死亡次数        ← 客户端**结构体**里它在 +0x08（差两字节对齐填充）
线 +0x0a  f32    X
线 +0x0e  f32    Y                  合计 18 字节
```

按 `+0x08` 去就地改包，改到的是「死亡次数的高半边 + X 的低半边」，
死亡次数变成六万多 → `剩余生命 = 最大生命 - 死亡次数` 是**大负数** →
左下角状态面板 `0x472527  mov [eax + esi*4], ebx` 拿它当数组下标，**当场 C0000005**：

```text
Fault address: 0047252D   ESI: FFFF1AE6   ← 剩余生命 = -58650
Call stack: 0047252D <- 00472033 <- 004A4344 <- 004781FE <- ...（UI 渲染树）
```

（第一次崩在右上角面板的同一条链 `0x5CCF29`，`[动画数组 + id*4]` 取到 NULL。）

**教训**：改一个抓来的包时，不要用「客户端结构体偏移」去 `pack_into`。
`server/gameserver.py` 现在统一用一个 `DEATH_REPORT_FORMAT = "<IBBiff"`
收发（Python 的 `<` 前缀本来就是紧凑无填充的，解析侧一直是对的，
错的只有那个手写的 `DEATH_COUNT_OFFSET`），并加了一条回归测试
「把回包重新解析一遍，除死亡次数外每个字段都必须原封不动」。

### 「分数」列同理：`0x0410 -> 0x0415`

战绩面板 `0x4a4a86` 读的是 **`[GameContextQuest + 座位*4 + 0x3b8]`**，
唯一写它的是 `0x0415 gspUpdateQuestScore` 的处理器 `0x4a3efe`（两个 int32：座位 + 分数）。

客户端加分的路径 `0x4a40f8` 和死亡是同一个套路：

```asm
004a40fc  call [ctx_vft+0x1c]        ; 恒返回 0（§108）
004a4101  je   0x4a410f              ; -> 不本地加分
004a4107  add  [esi+0x3b8], eax      ; （只有 vf_1c 为真才走这里）
004a410f  ...  500 毫秒节流后发 0x0410 ...
```

★ **`0x0410` 的载荷是累计分数**，不是增量：`0x4a414a` 先算 `[ctx+0x3b4] + 增量`
再把**新的累计值**交给 `0x4a40f8`，节流掉的那次不会丢（下一发带最新累计值）。
所以服务端把收到的数加个座位号原样发回去就对。

实机：控制通道推 `raw 0415 00000000 39300000`，战绩面板「分数」立刻显示 **12345**。

### 实机验收（会话 15 后半段，`logs/shot_s15e_heart2.png` / `shot_s15e_score.png`）

```text
19:26:38  0x0408 死亡次数=0 -> 回 0x0406 死亡次数=1   心形 ♥♥♡（左下角 2 颗）
19:26:55  0x0408 死亡次数=1 -> 回 0x0406 死亡次数=2   心形 ♥♡♡（左下角 1 颗）
19:27:09  0x0408 死亡次数=2 -> 回 0x0406 死亡次数=3   生命耗尽，不再重生
19:27:15  0x040f 关卡自动结束 -> 结算 -> 19:27:30 回房间
```

全程无崩溃、无 `0x0106 gcpReportHack`。
★ 第二次死亡时客户端报的是 `死亡次数=1` —— 这本身就证明第一发回包写对了位置。

### ★ 自然流程复验（岩浆巨龙 `GameContextQuest02`，真开枪打怪）

上面那轮分数是控制通道推的。查明「开火 = 鼠标左键」之后又跑了一遍**全自然**的：

```text
20:48:07  C→S 0x0410 累计分数           -> S→C 0x0415 -> 战绩「分数」= 73
20:48:10  C→S 0x0408 怪物死（座位=-1，凶手=0，句柄 0x0010c8fa）
          S→C 0x0406 死亡次数->1        -> 怪真的倒下（一局打死 5 只）
20:48:34  C→S 0x0408 玩家死（凶手=255）  -> S→C 0x0406 -> 心形 ♥♥♡
20:48:39  C→S 0x0413 (2327, 536) 索引=2  -> S→C 0x0419 -> 复活
```

截图 `logs/shot_s15f_score.png`：战绩栏 `生命 ♥♥♡ / 分数 73`，左下角 2 颗心，
NPC 台词「小心不要掉在熔岩上！」。全程 **0 个 `0x0106`**。

★ 注意探针里有**两个分数字段**，别看错：`战绩[n] 分数` 是
`[vc + 座位*0x2c + 0x5c]`（本地那份，客户端在闯关模式下**从不写它**，恒 0）；
面板读的是 `0x0415分数` / `GameContext+0x3b8` 那一份。

## 110. 「15秒内没有向前移动将强制退出」是原版反挂机机制，不是 bug

用户在岩浆巨龙图掉岩浆、死亡重生正常之后，过一会弹出这个提示。**这是原版行为。**

韩文原串 `퀘스트 진행방향으로 이동이 없을시 15초 후 자동으로 방을 나갑니다.`
@ `0x673c18`（「若不朝任务前进方向移动，15 秒后自动退出房间」），唯一引用点 `0x4a5a2b`。

判定逻辑（`0x4a59a2` 起，每 5 秒一跳）：

```asm
004a59a2  cmp [ctx+0x3ac], [ctx+0x3a8]   ; 关卡进度变了没
004a59b0  jne -> 记下新进度并返回          ; ★ 有推进就不计数
004a59b9  cmp byte [char+0x2b4], 0
004a59c0  jne -> 返回                     ; ★ 角色死着的时候不计数
004a59c6  取角色 X，和阈值比               ; 已经站在前面就不计数
004a59e7  inc [ctx+0x3a4]                 ; 计数器 +1
004a59f9  cmp eax, 3  -> 弹这句提示
004a5a72  cmp [ctx+0x3a4], 6 -> 0x406191 = 发 0x0203（离开房间）
```

- **计到 3 弹提示，计到 6 客户端自己发 `0x0203` 真的退出。**
  `0x0203` 我们从会话 12 起就实现了（§101），所以现在是**干净退出回大厅**，
  不是 §88 那时的卡死。实测 16:47:11 客户端就是这样退出去的。
- **重生之后特别容易触发**：角色被放回关卡出生点（岩浆图是 (3379, 359)），
  在进度线**后面**，这时站着不动计数器就开始走。
- 计数器只在 `0x4a3695`（换场景/初始化）清零，向前推进只是**不再增加**、不会回退。

**不需要修**。真嫌烦的话可以照 §101 patch 掉 `0x4a59f9` / `0x4a5a72` 的阈值，
但那会连原版的强制推进设计一起去掉，目前没这个必要。

## 111. ★★★★★ 「走到地图最右边不传送、角色卡住、鼠标变沙漏」= 换图链没接

用户实机反馈：**岩浆巨龙关卡走到地图最右边本该传送到第二张地图，
现在角色卡住不动、鼠标变成沙漏、不传送。**

根因和 §108（血量归零不死）是**同一类病**：闯关的换图判定也在服务端，
客户端把请求发上来就置一个「等服务端」的标志然后干等，而我们从来没接过这条链。
**沙漏就是那个标志位画出来的**，不是客户端卡死。

### 完整链路

```text
① 触发       0x4e65a5   地图脚本喊 `nextmap`（宽字符串常量 @0x6808b8）
                        走到边界由关卡脚本发出；旁边的 `forcewalk` @0x6808a4 同族。
                        另有调试热键 VK_LSHIFT+VK_N（0x4a423b）走同一条路。
             ★ 只有 [LobbyStage+0x1cc]（我的座位）== [LobbyStage+0x34]（房主座位）
               才发 —— 单机永远成立。
② 查下一张   0x4083e1   LobbyStage::ReqChangeToNextMap(wstring)
                        if ([this+0x3f9]) return;              // 已经在等了
                        名字为空 -> 0x405669 取当前地图名
                                    （[this+0x3fc] 非空则用它，否则用 Session 的 [+0x10]）
                                 -> 0x40b595 在全局地图目录 [0x72e3d8] 里按名字查记录
                                    （记录 +0x08 = 本图名，+0x0c = 下一张图名）
                                 -> 查不到就**根本不发包**（0x40842e 直接 ret）
③ 发请求     0x408475   push 0x411 -> **opcode 0x0411 gcpReqChangeToNextMap**
                        载荷 = 一个 wstring（下一张地图名）
             0x4084cb   [LobbyStage+0x3f9] = 1     ★ 「等服务端」= 鼠标沙漏
                 ────────  服务端必须回 0x0417  ────────
④ 放行换图   0x408526   opcode **0x0417 gspRepChangeToNextMap** 的处理器
                        反序列化一个 wstring
                        [+0x3f9]=0、[+0x3fa]=0
                        0x4083c9: [+0x3fc] = 地图名; [+0x400] += 1
                        0x47900a: 真正换图
⑤ 加载       0x47900a   卸掉六个座位的角色对象 -> 0x479278 起**后台加载线程**
                        （`_beginthread(0x5c530b, 0, &完成标志)`）
                        主线程进加载循环 0x47928d..0x479628：
                          PeekMessage/Translate/Dispatch 泵消息 + 画进度 + Sleep(500)
                          加载完成后每 5000ms 发一次 **opcode 0x0412**（空包，0x4084e1）
                        循环条件 0x47961d: `cmp [LobbyStage+0x3fa],0 / je 回到循环头`
                 ────────  服务端必须回 0x0418  ────────
⑥ 出加载画面 0x406302   opcode **0x0418** 的处理器，整个函数只有一句
                        `mov byte [LobbyStage+0x3fa], 1` -> 循环退出 -> 新地图开打
```

`[LobbyStage+0x3fc]` = 当前地图名、`[+0x400]` = 本局换过几次图，
两者在 `0x4057b7`（`ResetNextMap`，由 `ResetSession` 调）一起清掉。

### 两个 opcode 的线格式

`0x0411`（客户端→服务端）和 `0x0417`（服务端→客户端）**共用同一对读写器**
（序列化 `0x404f49`→`0x5d5a5a`，反序列化 `0x419388`→`0x5d5b3a`），
包体只有一个字段：

```text
wstring  地图名   u16 字符数 + UTF-16LE，无结尾 NUL
```

`0x0412` / `0x0418` 都是 **RawPacket，空载荷**（`0x0412` 由 `0x5bba4f`
直接带 opcode 构造，没有序列化调用；`0x0418` 的处理器不读任何字段）。

### 修法：原样回显地图名（同 D046 的理由）

**服务端手上没有任何地图数据，也不需要有** —— 下一张地图叫什么是客户端
在②里从自己的地图目录查出来的，请求包里就带着。原样发回去即可。

★ **`0x0417` 绝不能用回显 `0x0411` 的方式实现**：服务端方向的 `0x0411`
是 `gspEndGame`（结算），回显等于在关卡中途把玩家踢进结算界面。
又一次印证 D028「同号不同向就是两个包」。

★ **`0x0418` 必须等客户端的 `0x0412` 轮询到了再发，不能跟在 `0x0417` 后面
一起发**：⑤的加载循环是**前置**判断（`0x47961d` 先比较再进循环体），
标志位要是在进循环之前就被置 1，客户端会跳过整段加载等待，
而后台加载线程还在跑。这正是 D035「一次只推一个 stage、且必须等客户端报到」
要防的事故，和开局链 `0x0403 -> 0x0402` 是同一个形状。

### 三个 opcode 的方向表（很容易记混，D028 的又一批）

| opcode | 客户端 → 服务端 | 服务端 → 客户端 |
|---|---|---|
| `0x0411` | **gcpReqChangeToNextMap**（本节） | `gspEndGame`（结算，§87） |
| `0x0412` | **换图加载完成轮询**（本节） | `gspRepCountDown`（游戏跳表里无处理器，GameContext 表 `0x493755` 有） |
| `0x0417` | `gcpMarkQuestSuccess`（4B，`0x040f` 后 20ms） | **gspRepChangeToNextMap**（本节） |
| `0x0418` | —— | **换图放行**（本节） |

### 复用的定位手法

这次全程静态，没用探针，路径值得记下来：

1. `re/rtti_types.txt` 里按语义搜类名 —— `ChangeToNextMap` 一搜就出来
   `Packet_gcpReqChangeToNextMap` / `Packet_gspRepChangeToNextMap` 一对。
   **120 个包类的名字本身就是最好的索引**，比从行为反推快得多。
2. `re/vftables.json` 拿到两个类的 vftable（`0x65e170` / `0x65e124`）。
3. `re_bs.py xref <vftable>` 找到唯一引用点：发送函数里 `mov [ebp-0x14], vft`
   紧跟着 `push <opcode>` + `call 0x5bba0a`（= `RawPacket::SetType`，
   写 `[header+8]`）—— **opcode 就在这句 push 上**。
4. 接收侧同理：引用 vftable 的那个函数就是处理器，
   再 `xref` 它落在大厅分发链 `0x4062cd` 的哪一格，就得到服务端方向的 opcode。
5. `[reg+偏移]` 的读写点用 capstone 线性扫全 `.text` 过滤 `op_str`
   （比如 `"0x3fa]"`）—— 一次扫描就把「谁置的、谁读的」全找齐。

# 第十七批发现 —— 结算「未完成」与掉落物（2026-08-07 会话 17）

## 112. ★★★★★ 结算界面的「完成 / 未完成」标签在 `0x0309` 的**尾部数组**里

用户实机反馈：**真通关了岩浆巨龙，结算页面正常弹出，但标签写着「未完成」。**

§99 当时只留了一句「成功/失败标志在 `0x0309` 里，不是 `0x0411` 的 `success`」，
没查是哪一格。这次查到了 —— **不在那 13 个业务字段里，在尾部数组里。**

### 从贴图名倒着找过去（可复用的路径）

```text
0x670f78  "Images/Chinese/ImgTxt-MsgsInClearResult.smf"   ← 结算界面的中文字图
  xref -> 0x4a4b68 -> 所在函数 0x4a4af5 = 「画一个玩家槽」
```

`0x4a4af5(座位, ...)` 开头就把 `[GameContextQuest + 座位*0x34 + 0x3ec]`
（= `0x0411 gspEndGame` 那 13 个 dword，§92）搬到栈上，然后：

```asm
004a4b4c  mov eax,[0x72e320] / mov eax,[eax] / cmp eax,2   ; 2 = 闯关房
004a4b62  jne 0x4a4bf1                                     ; 非闯关走对战的胜/负文本
004a4b68  push 0x670f78                     ; ImgTxt-MsgsInClearResult.smf
004a4ba3  call [GameContext_vft+0x10c](座位)
004a4ba9  cmp eax,1
004a4bac  jne 0x4a4bd8                      ; != 1 -> 「未完成」
004a4bb5  call [GameContext_vft+0xa4](座位)
004a4bbb  test eax,eax
004a4bbd  jle 0x4a4bd8                      ; <= 0 -> 「未完成」
004a4bca  push 0x2b / push 3 / push 3 ...   ; ★「完成」那一帧（x=43）
004a4bdf  push 0x1e / push 3 / push 4 ...   ; ★「未完成」那一帧（x=30，串更宽所以更靠左）
```

两个虚函数都只有几条指令：

```asm
GameContext::vf_10c = 0x48c9ff:
    mov eax,[esp+4]                     ; 座位
    mov eax,[ecx + eax*4 + 0x184]       ; ★ 就这一句
    ret 4

GameContext::vf_a4  = 0x4913c6:
    [this+0x384] == 0 ? 0x7fffffff : QuestVictoryCondition::vf10(座位)
QuestVictoryCondition::vf10 = 0x55e0a3:
    最大生命 - [this + 座位*0x2c + 0x60]（死亡次数），负数夹成 0 = 剩余生命
```

### `[GameContext + 座位*4 + 0x184]` 的唯一写入点就是 `0x0309` 的尾部数组

```asm
0x55210d gspRepGameResult 处理器：
005521cc  lea eax,[ebx + 0x184]          ; ebx = [0x72e2dc] = GameContext
005521d2  循环 0x18 字节 = 6 个 dword，从包尾那个 int32 数组搬过来
```

全 `.text` 扫 `0x184]` 只有这一处写 `[GameContext+0x184]`。所以：

> **`0x0309` 尾部数组的第 i 项 == 1 ⇔ 第 i 号座位「完成」。**
> 我们一直发 6 个 0，于是无论怎么打都是「未完成」。

第二个条件「剩余生命 > 0」是客户端本地状态，正常通关时成立；
三条命用完那种「死出去」的收场剩余生命正好是 0，写「未完成」是对的。

⚠ `0x552231` 处同一个 `vf_10c` 还被拿去 `setge`（`>= 0` 就播胜利文本），
所以**聊天栏的胜负提示和结算界面的标签判据不是同一个阈值**，别混。

### 通关信号：客户端的 `0x0417 gcpMarkQuestSuccess`（早就在发了）

```text
GameContextQuest::vf_e4 = 0x4a3faa   MarkQuestSuccess(bool)
    [ctx+0x558] 保证一局只发一次；载荷是 1 个 bool（线上 4 字节）
    push 0x417 @ 0x4a3fdc
```

**两条收场路径的时序完全不同，这一点决定了服务端能不能用它：**

| 收场 | 顺序 | 实测间隔 |
|---|---|---|
| 打死关底 | 关卡脚本 `vf_e4(1)` → …金币雨… → `EndQuest()` 发 `0x040f` | `0x0417` **早 30 秒** |
| 时间到 / 生命耗尽 | `0x4a3dac` 先 `EndQuest()` 再 `vf_e4(0)` | `0x0417` 晚 20 毫秒 |

也就是说**服务端在收到 `0x040f` 时，通关的那条路一定已经收到 `0x0417(1)`，
没通关的那条路一定还没收到** —— 直接拿它当结算时的通关标志即可，
不需要延后结算，也不需要「下一局补正」（§5b 那条待办可以销掉了）。

实测（用户那一局，`logs/gameserver.out`）：

```text
21:53:27.034  0x0408 HP 归零上报  句柄=0x0010c963 座位=-1（关底）
21:53:27.077  0x0417 gcpMarkQuestSuccess  载荷 01 00 00 00
21:53:27.387  0x0406 × N  ← 金币雨（见 §113）
21:53:57.562  0x040f gcpEndQuest
```

★ 服务端方向的同号 `0x0417` 是 `gspRepChangeToNextMap`（换图放行，§111），
**绝对不能回显**，否则会在关卡结束时触发一次换图。

## 113. ★★★★★ `0x0406` 客户端方向不是「位置同步」，是 `gcpCreateItem`（掉落物）

用户实机反馈：**通关后 boss 应该掉金币和道具，什么都没掉。**

### §108 / §109 把这个包记错了，这里勘误

整个镜像里 `push 0x406` + `call 0x5bba0a`（`RawPacket::SetType`）
**只有一个调用点**：

```text
0x493a57  在 GameContext::SendCreateItem(0x4939c0) 里
          vft = 0x670c64 = .?AUPacket_gcpCreateItem@@
          序列化 0x48c84f 写 8 个字段 = 32 字节
```

线格式（和实测载荷逐字段对得上）：

```text
+0x00  int32  物件 id
+0x04  float  X
+0x08  float  Y
+0x0c  float  速度 X
+0x10  float  速度 Y
+0x14  int32  实测恒为 3
+0x18  int32  实测恒为 -1
+0x1c  int32  实测恒为 -1
```

```text
da 27 00 00 | 00 60 19 45 | 00 80 97 43 | 0 | 0 | 03 | -1 | -1
└ 10202 水炮 └ x=2454.0     └ y=303.0
```

「位置同步」那个旧读法之所以一直没露馅，是因为第 2、3 个 dword **确实是坐标**
（掉落点就在怪物脚下），拿去当 `respawn` 的兜底坐标不会出事。

### 应答是 `0x0404 gspCreatedItem`，不回就什么都不掉

```text
分发链 0x54e091: movzx eax,[包头+8] / cmp eax,0x404 / je 0x54e300
0x54e300  push edi / call 0x551a11        ← gspCreatedItem 处理器
          vft 0x69188c = .?AUPacket_gspCreatedItem@@
          反序列化 0x54c523 读 **9** 个 4 字节字段 = 36 字节
```

处理器 `0x551a11` 的用法（`ebp-0x6c` = 包对象）：

```text
+0x00  int32  ★ 实例句柄 -> 物件对象的 [obj+0xd0]（0x511ba7），
              World::Add(0x473e7c) 拿它当 map 的 key —— 撞了会互相覆盖
+0x04  int32  物件 id -> 0x513278 ObjectFactory 的分支选择
+0x08  float  X ┐ 先按地图记录的地面高度 [map+0x50] 夹一次（0x551a96），
+0x0c  float  Y ┘ 再用 0x473969 逐步找一个不卡在地形里的落点；找不到就放弃
+0x10  float  速度 X -> [obj+0x120]
+0x14  float  速度 Y -> [obj+0x124]
+0x18  int32  == 1 时走「宠物掉落」的音效/特效分支（Pet06_*，客户端发 3，不进）
+0x1c  int32  座位号 0..5，配合上一格用；客户端发 -1
+0x20  int32  **处理器整个函数都没读过它**
```

**所以应答 = 在客户端自报的 8 个字段前面插一个服务端分配的实例句柄**，
9 个字段 36 字节。服务端不需要知道任何物件数据 —— 掉什么、掉在哪、
初速多少全是关卡脚本算好了报上来的（同 D046）。

★ **绝不能回显同号** —— 服务端方向的 `0x0406` 是死亡广播（§108），
回显等于随机杀角色。这是 D028 的又一例。

### 物件 id 全表（`0x513278 ObjectFactory`，类名按构造函数里的 vftable 反查）

| id | 类 | 说明 |
|---|---|---|
| 10000 | `ItemBox` | 宝箱 |
| 10001 | `LuckBag` | 幸运袋 |
| 10100 | `HeartItem` | 心（回血）|
| 10101 | `CoinItem1` | **金币 ×1** |
| 10102 | `CoinItem5` | **金币 ×5** |
| 10103 | `ItemCard` | **称号卡片**（结算界面那一格）|
| 10104 | `ItemEventFruit` | 活动果实 |
| 10200 | `NukeLauncherItem` | 核弹发射器 |
| 10201 | `FireThrowerItem` | 火焰喷射器 |
| 10202 | `WaterCannonItem` | 水炮 |
| 10300 | `ShieldItem` | 护盾 |
| 10301 | `SpeedUpItem` | 加速 |

小 id 101..111 / 200..210 是地图编辑器摆的场景物件
（`SpawningArea` / `RegionObj` / `BreakableObj` / `JumpingObj` …），
和 `gspCreateObject`（另一个包）共用同一个工厂。

### 通关后的「金币雨」是这条链最显眼的用途

`GameContextQuest::Update` 里的 `0x4a546e` 循环：

```text
限额 = [ctx+0x588] * (已过时间 / 总时间)，上限 [ctx+0x588]
每次取一个随机偏移（±60）和随机初速（vx ∈ [-70,-30], vy ∈ [-30,30]）
发一件 **10101 CoinItem1**，[ctx+0x58c]++ 直到追上限额
```

实测节奏 ~290 毫秒一件，从 `0x0417` 之后一直发到 `0x040f`（30 秒）。
每一件都在等 `0x0404`。**打死普通怪也走同一条链**（死后约 60 毫秒一发）。

### 顺带确认

* 捡起掉落物**不发任何包**（把 `RawPacket::SetType` 的 109 个调用点全列了一遍，
  `Item` 那一片 `0x51f000..0x526000` 一个都没有）—— 纯客户端行为。
* 所以结算界面「金币」那一格和地上捡的金币没有直接关系，
  金币仍然只能由服务端在 `0x0411`(`+0x1c`) / `0x0309` 里下发。

### 一次把客户端 opcode 表列全的办法（这次新用的）

```python
# 扫 `68 <imm32>` 后面 5 字节内跟着 `call 0x5bba0a` 的地方
# = RawPacket::SetType(opcode)，一次拿到全部 109 个客户端方向发送点
```

结果见 `re/packets.txt` 旁边的记录；`0x0406` 只有一个发送点这件事
就是这么一眼看出来的。

## 114. 会话 17 的实机验收

客户端 pid 9144，关卡「神秘岛 简单」（`GameContextQuest03`），`start.bat`（精简日志）。

### 掉落物（§113）

控制通道一次推四件，**四件全部出现在地上**
（`.claude/sessions/2026-08-07-17-drops.png`）：

```bash
python tools/gs_ctl.py drop 10101 700 560   # CoinItem1  -> 金币
python tools/gs_ctl.py drop 10102 740 560   # CoinItem5  -> 大金币
python tools/gs_ctl.py drop 10103 780 560   # ItemCard   -> 卡片
python tools/gs_ctl.py drop 10100 620 560   # HeartItem  -> 红心
```

自然路径同样通过 —— 客户端自己发的那一发被接住并回复：

```text
22:41:04.889 #1 ★ 游戏包 opcode=0x0406 (?) 载荷 32 字节
22:41:04.896 #1 ← 回 gspCreatedItem(0x0404) 句柄=0x40000004
                  物件=10202 WaterCannonItem 水炮 @ (3014, 580)
```

全程 **0 个 `0x0106 gcpReportHack`**（§88 的免费正确性检查器）。

### 结算标签（§112）

战斗中手推一发尾部数组 `[1,0,0,0,0,0]` 的 `0x0309`，再推一发裸的 `0x0411`：

```bash
python tools/gs_ctl.py raw 0309 00000000<12×00000000>0600000001000000<5×00000000>
python tools/gs_ctl.py endgame 0 1
```

结算界面左上角的红黄标签从「未完成」变成 **「完成」**
（`.claude/sessions/2026-08-07-17-clear.png`）。**假设一次坐实。**

⚠ 这样试的时候要用 `endgame <seat> <success>` 这个**裸**形式 ——
不带参数的 `endgame` 会自己再发一发 `0x0309`（尾部全 0），把手推的那发覆盖掉。
裸形式不置 `settled`，所以客户端随后发的 `0x0405` 不会被当「看完了」，
需要手动 `back-to-room` + `sync-account` 收场。

### 顺带纠正 PROGRESS 的一条

旧记法：「生命耗尽死出去时我们照样发 `success=True`，客户端自己报的
`0x0417` 还没接」。本次实测**客户端报的是 False**：

```text
22:41:57.752 #1 ★ 游戏包 opcode=0x040f (gcpEndQuest)
22:41:57.761 #1 ← 回 gspRepGameResult(seat=0, 未完成)
22:41:57.761 #1    客户端报 gcpMarkQuestSuccess(False)     ← 结算之后才到，且是 False
```

也就是说**两条路径的 `0x0417` 值和时序都是对的**，服务端直接信它即可（D051）。

# 第十八批发现 —— 拾取链与结算数值（2026-08-08 会话 18）

## 115. ★★★★★ 掉落物捡不起来 = `0x0407 gcpGetItem` 没人应答

用户实机反馈：**会话 17 之后东西是掉出来了，但角色走过去捡不起来。**

§113 末尾那句「捡起掉落物**不发任何包**，纯客户端行为」**是错的**，这里勘误。
当时只扫了 `Item` 那一片地址（`0x51f000..0x526000`）的 `RawPacket::SetType`
调用点，而拾取包是 `Character` 发的（`0x515567` -> `GameContext::SendGetItem`
`0x493a99` -> 序列化 `0x558e9a`），地址不在那一段里。

### 完整链路

```text
Character::CheckItemPickup 0x5154d3          ← 每帧跑
    [char+0x2b4] != 0 -> return              ; 只有本地操控的角色才检测
    遍历 World 的物件表（0x4764ca(2, World+0xdc) 取「第 2 类」列表）
      item = dynamic_cast<Item*>(节点)        ; 0x515516 推 .?AVItem@@ = 0x6e2328
      [item+0x2a8] != 0 -> 跳过               ; ★ 这件已经报过一次了
      [item+0x2aa] == 0 -> 跳过               ; 「可拾取」标志，构造时置 1（0x51f2cc）
      碰撞 0x50f410(char, item) 不成立 -> 跳过
      SendGetItem([char+0x2ac] = 座位, [item+0xd0] = 实例句柄)   ; C→S 0x0407
      [item+0x2a8] = 1                        ; ★★ 防重发，一件一局只报一次
   ★ 服务端回 0x0405 -> 0x551d35
        0x5590bb 读两个 int32
        0x404ff6  座位 -> [LobbyStage + 座位*4 + 0x1d0] = 角色对象（越界返回 0）
        0x474225  句柄 -> World::Find -> dynamic_cast<Item*>
        0x551d89  两个都非空才 item->vf_d4(角色)
   Item::vf_d4 = 0x51f447
        if [item+0x2a9] == 0: vf_11c(角色)    ; 生效（CoinItem1 加钱 / HeartItem 回血…）
        vf_20()                               ; 把物件从世界里删掉
```

**`[item+0x2a8] = 1` 是这条链最要命的地方**：客户端发完就把这件标成「已上报」，
服务端不回的话它既不生效、也不会再报第二次 —— 所以现象不是「偶尔捡不到」，
而是**那件东西彻底作废，站在上面反复走也没用**。用户报的就是这个。

### 两个 opcode 的线格式（收发完全对称，原样回显即可）

```text
C→S 0x0407 gcpGetItem      序列化 0x558e9a （8 字节）
S→C 0x0405                 反序列化 0x5590bb（8 字节）
    +0x00  int32  座位号    = [Character+0x2ac]，单机固定 0
    +0x04  int32  实例句柄  = [Item+0xd0]，就是服务端在 0x0404 里发的那个
```

实测载荷（会话 17 的日志里其实早就有，只是当时不知道是什么）：

```text
23:02:46.617  0x0407  00 00 00 00 | 03 00 00 40   座位 0，句柄 0x40000003
23:02:47.993  0x0407  00 00 00 00 | 06 00 00 40   座位 0，句柄 0x40000006
```

★ **`0x0405` 又是一对同号反向**（D028 的第 N 例）：
客户端方向的 `0x0405` 是 `rawLeaveGameResult`（**空载荷**，「结算界面看完了」），
服务端方向的 `0x0405` 是拾取放行（8 字节）。只能靠方向区分。
服务端方向的 `0x0407` 在跳表 `0x54e5ae` 里落到默认分支，没有处理器。

### 定位手法（复用了 §112 / §113 那两条，值得记）

1. 从**类的虚表**入手：`re/vftables.json` 里 `CoinItem1` / `HeartItem` /
   `GeneralItem` / `Item` 各有 5 个 vftable（多重继承），最后一个才是主表
   （72~74 项）。把几个类的主表**并排打印**，只有 `+0x11c` 那一格逐类不同
   —— 那就是「拾取时生效」的虚函数。
2. 反过来扫 `call [reg+0x11c]`，落在 `Item` 那一片的只有 `0x51f459`，
   所在函数 `0x51f447` 就是「被捡起来了」。
3. 再扫 `call [reg+0xd4]`（`0x51f447` 是主表的第 53 项 = `+0xd4`），
   在**包处理器那一片**（`0x55xxxx`）命中 `0x551d89`。
4. `0x551d89` 所在函数 `0x551d35` 的唯一调用点是 `0x54e34e`，
   而 `0x54e34e` 是跳表 `0x54e5ae` 的**第 0 项**，跳表基址对应 `opcode - 0x405`
   —— opcode 当场就出来了。**跳表比 `cmp/je` 链好读，直接按下标数。**

```python
# 读 0x54e036 分发器的 0x405..0x413 跳表
for i in range(15):
    t = struct.unpack('<I', read(0x54e5ae + i*4, 4))[0]
    print(f'opcode 0x{0x405+i:04x} -> {t:08x}')     # 0x54e546 = 默认分支 = 无处理器
```

### 实机验收（会话 18）

```text
00:56:39.852  [ctl] drop 10101 @ (4413, 640)   ← 掉在角色脚下
00:56:39.860  C→S 0x0407  00000000 05000040
00:56:39.860  S→C 0x0405  座位=0 句柄=0x40000005
```

红心那一发是**最硬的证据**：`probe_death.py` 读到 HP **53 → 68**，
说明 `vf_11c` 真的跑了，不只是包对上了。全程 **0 个 `0x0106 gcpReportHack`**。

## 116. ★★★★ 结算界面四格数值的来源（分数来自 `0x0411`，其余来自 `0x0309`）

用户实机反馈：**结算左上角已经是「完成」了，但经验/金币/分数全是 0。**

### 「分数 / 生命」在 `0x0411 gspEndGame` 里

`0x551804` 把包里的字段搬成一张 **13 个 dword 的结算表**：

```text
0x55189f  push 0xd / rep movsd
0x5518af  call 0x4a4096         ; eax = 座位号
0x4a40b2  lea edi,[座位*0x34 + ctx + 0x3ec] / rep movsd
```

结算界面画玩家槽的 `0x4a4af5` 在 `0x4a4b45` 把这张表整个搬到栈上
（`[ebp-0x78]` 起 13 个 dword），最后在 `0x4a4e40`：

```asm
004a4e08  call [GameContext_vft+0xa4](座位)   ; = 剩余生命
004a4e40  mov ecx,[ebp-0x5c] / add ecx,[ebp-0x60] / add ecx,[ebp-0x64]
                                             ; = 表[7] + 表[6] + 表[5]
004a4e5b  push 0x668114                      ; L"%d / %d"
004a4e65  push 0x12d / push 5                ; 画在 (0x12d, 0xa1)
```

标签串是 `0x673e44` = `L"SCORE / LIFE"`，画在 `y=0x119`（正上方一行）。

`0x551804` 那段搬运是**逐字段 mov**，不是整块 `rep movsd`，中间有两个洞：

| 结算表 | 来自 | 业务值下标 |
|---|---|---|
| 表[0] | `pkt+0x08`（success，只搬 1 字节）| — |
| 表[1]..表[3] | `pkt+0x0c` / `+0x10` / `+0x14` | 0 / 1 / 2 |
| 表[4] | `pkt+0x1c` | 4（**跳过了 `pkt+0x18`**）|
| **表[5]/表[6]/表[7]** | **`pkt+0x20` / `+0x24` / `+0x28`** | **5 / 6 / 7 ← 分数** |
| 表[8] | **没有任何指令写它**（栈上的残留）| — |
| 表[9]..表[12] | `pkt+0x2c` .. `pkt+0x38` | 8 / 9 / 10 / 11 |

`gspEndGame` 的**内存布局和线序 1:1**（反序列化 `0x54cea3` 顺着 `+0x04`
一路读到 `+0x38`），所以「业务值下标 k」= `pkt + 0x0c + 4k`，可以直接换算。

界面显示的是三格**之和**，原版多半拆成击杀分/时间分/收集分之类；
单机把本局总分全放进第一格就行。

### 「经验值 / 金币 / 竞技场分数」在 `0x0309 gspRepGameResult` 里

`0x55210d` 把三个字段搬进 GameContext，结算界面的**基类**画法
`GameContext::vf_44`（`0x48db6c`，`0x4a4af5` 开头 `call 0x48db6c` 调的就是它）
再读出来画成三行 `+%d`（`0x66fb08` = `L"+%d"`）：

| 界面行 | GameContext 字段 | 读于 | 画在 y | 标签串 |
|---|---|---|---|---|
| 经验值 | `[ctx + 座位*4 + 0x2c]` | `0x48e239` | `0x15b` | — |
| 竞技场分数 | `[ctx + 座位*4 + 0x44]` | `0x48e3e6` | `0x189` | `0x670f2c` `L"LADDER POINT"` |
| 金币 | `[ctx + 座位*4 + 0x5c]` | `0x48e4e7` | `0x1b8` | `0x660848` `L"PIXEL"` |

y 坐标和截图逐行对得上（面板原点差 ~74 像素：`0x119`→354、`0x12d`→376、
`0x15b`→422、`0x175`→446、`0x189`→468、`0x1a4`→492、`0x1b8`→515）。

### ⚠ `0x0309` 的「线序」比「结构体偏移」少 6 字节

反序列化 `0x54c6b4` 的顺序是：

```text
+0x04 i32 | +0x08 i32 | +0x0c bool | +0x10 i32 | +0x14 i32 | +0x18 i32
| +0x1c i32 | +0x20 i32 | +0x24 bool | +0x25 bool | +0x28 i32 | +0x2c i32
| +0x30 i32 | +0x34 = int32 数组
```

**`+0x24` 和 `+0x25` 是两个挨着的 1 字节 bool**（`0x5d59de` 线上读 4 字节、
只存 1 字节），所以从这里往后结构体偏移比线序少 6。换算成「座位号之后的
第 k 个业务值」：

```text
值 9  -> pkt+0x28 -> [ctx + 座位*4 + 0x2c]  经验值
值 10 -> pkt+0x2c -> [ctx + 座位*4 + 0x5c]  金币
值 11 -> pkt+0x30 -> [ctx + 座位*4 + 0x44]  竞技场分数
```

（PROGRESS 里「values[9..11] 落到 0x2c/0x5c/0x44」这条旧记法是对的，
这次把**哪一格对应界面哪一行**补齐了。）

⚠ 剩下 9 个值仍然全填 0：§100 那次 `gameresult-probe`（12 个值填 201..212）
会让客户端 **20 毫秒内主动断链**，至今没查出是哪一格干的。

### 还没解出来的两格

「合成材料」和「称号卡片」是两个**物件槽**（画的是图，不是数字），
不在上面这些字段里。`0x48e5a8` 起那段按 `[ebp-0x20]`（房间类型 0x66/0x68/
0x69/0x6b/0x6c/0x6e）分支去读 `[对象+0x24 + 下标*4 + 0x14/0x24/0x50]`，
数据源还没跟到。`10103 ItemCard` 就叫「称号卡片」，多半和 §113 的物件 id 对得上。

### 实机验收（会话 18）

`gs_ctl.py endgame`（走的是客户端发 `0x040f` 时的同一条 `send_end_game`），
本局分数 247：

```text
01:00:57.490 ← 回 gspRepGameResult(seat=0, 未完成)（经验值 +247 / 金币 +247 / 竞技场分数 +0）
```

结算界面（`.claude/sessions/2026-08-08-18-result.png`）：

```text
分数 / 生命    247 / 2
经验值         +247
竞技场分数     +0
金币           +247
```

★ 这个界面是**逐行动画展开**的（`0x4a4af5` 的 `[ebp+0x10]` 就是「展到第几行」，
`cmp [ebp+0x10], 2/3/4/5` 一层层放行）。结算弹出后 **6.4 秒**才展到「金币」那行，
截图早了会以为金币还是空的 —— 会话 18 第一次就这么误判了一回。

## 117. ★★★★ 便携运行时与精简发布包

### 确定的运行时边界

* `tools\launch.ps1` 原先把 Python 写死成 `C:\Python314\python.exe`，这是把成果物
  复制到干净电脑后唯一确定会立即失败的外部路径。
* 两个生产服务端只依赖标准库；官方 CPython 3.14.3 x64 embeddable 足够。
  本次下载 `python-3.14.3-embeddable-amd64.zip`，SHA-256：
  `e69d3609130b1c06948620651d0f0ab2183ff978c2b174ddf3d3cae7ff226b89`，
  和 python.org 的 `windows-3.14.3.json` 完全一致。
* `python314._pth` 保持上游默认：只含 `python314.zip` 和运行时自身目录，不启用
  `site`。所以它不会读取系统 Python；代价是旧的 `python -m unittest ...` 也不会
  自动把当前目录加进 `sys.path`。生产入口自己在导入本地模块前插入 `server\`，
  不受影响；测试需要显式把 `server\` 加进路径。
* `bsloader.exe` / `bshook.dll` 是 `/MT`，静态导入只需系统 DLL；实机模块快照里
  游戏没有加载 `d3dx9_43.dll`，也没有版本化的 `MSVCRxxx/VCRUNTIMExxx`。
  这降低了干净 Win10/11 缺旧运行库的风险，但最终结论仍以干净系统终验为准。

### 发布物与验证

`tools\build-portable.ps1` 明确收集：三个启动脚本、`README.md`、`runtime`、
`hook\bin`、生产服务端六个 `.py`、`launch/shutdown`、D3D9 探针，以及已经实机
跑过的 `game_patched`。排除开发工具、日志、`Debug` 和约 195 MiB 的 `Dump`。

默认不复制 `accounts.json`，因为它可能含明文登录口令；`-IncludeSave` 才复制。
发布目录仍然保留 `server\data` 和 `logs` 空目录，首次运行会自动建存档。

本次结果：

```text
dist\PopShot-portable-win64       389.3 MiB
dist\PopShot-portable-win64.zip   363.4 MiB / 212 files
ZIP SHA-256  5e8b490ab5c072cfea7c5d6d5f41c062c92e16f0a0db6ba50569ce5ba907df00
```

验证：内置 Python 运行 **176 项测试全过**；发布目录里的认证服和游戏服分别在
备用端口 47612 / 27899 启动并接受 TCP 连接；ZIP 经 `7z t` 完整性检查通过。
当前 `BigShot.exe`/`bsloader.exe` 正在跑会话 18 的实机，因此本次没有为了打包
强杀它；发布目录的客户端整条启动留到关闭当前游戏后或干净系统上终验。

---

# 第十九批发现 —— 难度解锁（2026-08-08 会话 20）

## 118. ★★★★★ 「只能选简单，选普通/困难就说『无法进行的难度』」= `0x020c` 从来没发过

用户现象：任务房间里难度选「普通」或「困难」，点 F5 立刻弹
**「无法进行的难度，请降低难度。」**，只有「简单」能开局。

韩文原串 `0x66a758` = `플레이할 수 없는 난이도 입니다. 난이도를 낮춰주세요`
（D034 的老套路，一搜就中）。它是准入校验 `0x468176` 的**错误码 5**
—— 这个函数 §77 已经解过一半（错误码 3 是「等级太低」），当时把
「难度」那一路一笔带过，说「所以难度『简单』(1) 永远过」，
**没意识到那句话的另一半是「其余难度永远不过」**。

### 判定链（`0x4683ba`，闯关房专用分支）

```asm
00468399  mov eax, [edi+0x1c]          ; edi = LobbyStage，+0x1c = 描述符类型
0046839c  dec eax / je 0x46841d        ;   1 普通房
0046839f  dec eax / je 0x4683ba        ;   2 闯关房 ★
004683a2  sub eax,3 / je 0x46841d      ;   5 天梯房

004683ba  push [edi+0x20]              ; 描述符参数 1 = 关卡 id
004683bd  mov  eax, 0x72e328
004683bf  call 0x40119f                ; = map[关卡 id]，查不到返回 0
004683c4  inc  eax                     ; 「已达成难度 + 1」
004683c5  cmp  eax, 4 / jl / mov eax,4 ; 夹到 4
004683cd  cmp  [edi+0x24], eax         ; 描述符参数 2 = 房间选的难度
004683d0  jle  0x4683a7                ; <= 上限 -> 返回 0 放行
                                       ; 否则 -> 弹 0x66a758，返回错误码 5
```

一句话：**能开的最高难度 = `min(已达成难度 + 1, 4)`**。
map 空 → 已达成 0 → 上限 1 → 只有「简单」。

`0x40119f(eax=对象, 关卡 id)` 里 `esi = eax + 0x34`，所以
§「死路清单（会话 07）」记的 `0x72e35c` 和 §77 记的 `0x72e328`
**是同一张表**：`0x72e328` 是那个全局对象，`+0x34` 是它的 map 成员。

### 那张 map 只有两个写入点，都是**服务端下发的包**

```text
0x020c gspQuestReachedDifficulty  处理器 0x5539c2   ★ 全量快照（先清空再灌）
0x0416 gspUpdateQuestDifficulty   处理器 0x553a38     单条更新 + 一句恭喜提示
```

全镜像对 `0x72e35c` 只有 `0x5539e2` / `0x553a63` 两处引用，**客户端自己永远
不会往里写一个字节**。我们两个包都没发过 → map 恒空 → 恒定只有「简单」。

`0x020c` 的服务端方向落在跳表 `@0x54e56a` 的下标 7（`0x54e1e0 → 0x5539c2`）；
客户端方向**没有**这个 opcode（它不会来要，只能服务端主动推）。

### `0x020c` 的线格式（`0x54cf4a → 0x555315`）

```text
int32                        条目数           0x5d5984
条目数 × {                                    0x5558f2 读两个 int32
    int32  关卡 id
    int32  已达成难度
}
```

处理器 `0x5539c2` 先 `0x401312` 把 map **清空**，再逐条 `0x47197a` 插入 ——
所以它是幂等的全量快照，重发一次就是整张表的新版本，不需要做增量。
包体不带任何 UI 副作用（不弹框、不播聊天、不碰 LobbyStage），
**任何时刻发都是安全的**。

### `0x0416` 的线格式和副作用（这次没用，记下来备查）

```text
int32  关卡 id
int32  已达成难度
```

处理器 `0x553a38`：`0x554ce0` 就是 `map::operator[]`（返回 `迭代器+0x14` =
值的地址），末尾 `0x553b6b` 处 `[eax] = 新值` —— **会覆盖**。
另外当「旧值 == 0 且新值 != 0 且关卡 id < 7」时，它还会用
`0x691930` = `축하합니다!\n\n이제 무투전에서 %s맵을 선택하여 플레이하실 수
있습니다.`（恭喜！现在可以在武斗战里选 %s 地图了）走 `0x424b96` 弹一个通知。
`0x424b96` 我们没读，所以本次**只用 `0x020c`**，不碰这条。

### 难度枚举和「能选几个」

建房对话框的难度表在 `0x72e528`，每条 12 字节 `{名字指针, 值, 要求等级}`，
**4 条：值 1 / 2 / 3 / 4，要求等级全是 0**。

待机房间那一路（`0x4662dc`）算法一样，但多一句：

```asm
004662fb  mov eax, [0x72e320] / mov eax,[eax]   ; 区域号，本机实测 = 2
00466304  dec ecx / dec ecx / push 3 / pop ebx
00466309  jne ...  /  mov esi, ebx              ; 区域 == 2 -> 条目数固定 3
```

所以国服（区域 2）UI 里只有 **简单 / 普通 / 困难** 三档，第 4 档看不到。
存档里记到 4 就等于全开（`min(4+1,4) = 4 >= 3`）。

### 服务端实现

```text
server/account_store.py   新增 quest_difficulty（{关卡 id: 已通关的最高难度}）
                          + quest_unlock_all（默认 True）+ set_quest_cleared()
                          + quest_difficulty_records() / quest_cleared_difficulty()
server/gameserver.py      OP_QUEST_REACHED_DIFFICULTY / build_quest_reached_difficulty
                          Conn.send_quest_reached_difficulty / current_quest
                          / record_quest_clear
                          下发点：登录后、sync-account、通关入账后
tools/gs_ctl.py           新命令 quest-difficulty [id 难度 ...]
```

通关入账走 `0x0417 gcpMarkQuestSuccess`（D051 已有的信号）：
`send_end_game(success=True)` 时把 `(房间的关卡 id, 房间的难度)` 记进存档，
只往上记不往下改，记完立刻重发一次 `0x020c` —— 结算完回房间就能直接选新难度。

### 实机验收（A/B 对照，本次会话）

```text
存档 {1..7: 4}  -> 建「神秘岛 / 困难」房 -> F5 -> 直接进加载 -> 战斗画面顶部
                   写着「神秘岛 困难」，12:52 倒计时，可玩
                   （logs/shot_s20_diff_hard.png / shot_s20_battle_hard.png）
ctl 推 {3: 0}   -> 同一个房间同一个难度 F5 -> 立刻弹「无法进行的难度，请降低难度。」
                   （logs/shot_s20_locked_again.png）★ 复现出用户报的原始现象
ctl 推回存档    -> 同一个房间 F5 -> 又能进了（logs/shot_s20_unlocked_again.png）
```

**这组对照把因果钉死在这一个包上**，不是「改了之后好像好了」。
全程 0 个 `0x0106 gcpReportHack`。

### 一条可复用的操作手法

★ **这个客户端的下拉框只在按住左键时展开**（松开就收）。
会话 07 记的「点箭头只高亮不展开」有一半是这个原因，不全是列表空。
选项要用「按住箭头 → 拖到条目 → 松开」，单纯 click 两次是选不中的。
待机房间右侧还有「难度 ◀ 困难 ▶」的左右箭头，那一路是普通点击。

### 一条记录

§77 写「所以难度『简单』(1) 永远过」时，**判定链已经完整读出来了**，
但只回答了当时的问题（等级），没有回头问「那别的难度靠什么过」。
读准入校验这类**多错误码**的函数时，把每个错误码的触发条件都记一遍，
比事后按现象重新找一遍便宜得多 —— 这次是用户报了才回头看的。

### 便携包已跟着重新生成（会话 20）

会话 19 那份 `dist/` 是改 `server/` **之前**打的，里面是旧代码。
改完之后重跑了 `tools\build-portable.ps1 -Zip`：

```text
dist\PopShot-portable-win64       389.3 MiB
dist\PopShot-portable-win64.zip   363.7 MiB / 209 files（7z t: Everything is Ok）
ZIP SHA-256  8b70ff0f0a21096f4fa201c7266fce84a4be61e99f432bf5bb354a24929ac766
```

**规矩**：以后每次改 `server/` / `hook/` / `tools/launch.ps1` 都要重跑一次，
否则 `dist/` 会静悄悄地留着旧代码 —— 而下一步恰恰是拿它去干净电脑上终验。

## 119. ★★★★★ 隐藏角色 / 隐藏关卡：两把完全不同的锁

**用户报的现象**：`Pack_decrypt` 里有十几个角色、6 个 boss 的完整素材，
但待机房间只能选 3 个角色，「任务」下拉框只有 4 关。

**结论：不是资源缺失，是两把独立的锁。**

| 缺什么 | 锁在哪 | 谁能开 |
|---|---|---|
| 11 个角色（id 100~110）| **背包物品判定**（客户端问服务端要清单）| 服务端发 `0x030b` |
| 4 个关卡（QuestId 5/6/7/8）| **`map.ini` 的 `OpenLocale` 地区掩码** | 客户端 patch |

### A. 角色：`0x030b gspSlotEquippedList` 是唯一开关

```text
CharacterChanger 建按钮 0x4f586c
  -> 0x40713a(LobbyStage, 0)   数按钮个数
         for id in 100..110:  0x4070c2(id) 为真就 +1
         return 计数 + 3       ★ 0/1/2 三个基础角色白送
  -> 0x4070c2(id) = [LobbyStage+0x1cc](我的座位) 已占用
                    且 [LobbyStage + 座位*4 + 0x250](物品清单) 非空
                    且 0x55853c(清单, id)
  -> 0x55853c(清单, id):  id < 3 -> return true
                          否则 key = (id+1) * 1000000（0xf4240）
                          在 清单+0x18 的 vector<int32> 里 find
                          谓词 0x55851f 认 [key, key+1000000) 这个左闭右开区间
```

**那份清单只有 `0x030b` 能填**（处理器 `0x406ea1`，跳表 `@0x406332` 索引 0x0b
→ `0x40628a`）。它整体替换 `[LobbyStage + 座位*4 + 0x250]`：删旧的、
`0x5f399e` 分配 0x50 字节新的、`0x414d95` 拷进去，最后 `0x406f42` 把清单
套到该座位的角色对象上。客户端自己永远不写它，服务端不发就恒空。

线格式（`Packet_gspSlotEquippedList::Deserialize 0x404f1e` → 清单 `0x404c3f`）：

```text
int32       座位号                     -> 包 +0x04（处理器拿它算 0x250 的下标）
12 字节     槽位掩码 ×3                -> 清单 +0x0c（0x5d59c1 原样读 12 字节）
int32       物品数                     -> 0x5d5984
物品数 × int32  物品 id                -> 清单 +0x18 的 vector<int32>
```

那 12 个字节是「哪几个装备槽被占了」的位掩码，只在客户端**自己**往清单里
加/删物品时才碰（`0x5583ab` 进 / `0x558423` 出），下发全 0 即可。

**物品 id 直接抄真实商城条目**：`ShopItem.ini` 的
`[Item-101400001]`…`[Item-111400001]` = `(角色 id + 1) * 1000000 + 400001`。
判定只要求落在区间里，但抄真 id 才能让 `0x505bb9` 在物品表里查到定义。

**安全性（静态确认过，不是赌的）**：

- `0x505bb9(Character, itemId)` 先 `itemDb[0x72e1e0]` 查不到就 `return false`，
  再 `itemId/1000000-1 != [char+0x2ac]`（当前角色）也 `return false`
  —— 把 11 件全给一个座位，只有当前角色那件会真正生效。
- `0x4150eb` 建每角色装备对象时，`map[itemId]` 查不到得到 NULL，
  而消费它的 `0x41363b` 开头就是 `test ecx,ecx / jne` —— NULL 安全。

**顺序是硬约束：`0x030b` 必须排在 `0x0300` 之后**，持有判定第一步
（`0x4045f9`）查的「我的座位已占用」只有 `0x0300` 会写。
而按钮是在**房间 UI 构造时一次性建出来的**，后发不会重建 ——
不过 `ChangeStage`(`0x40e47f`) 只是记下工厂函数、下一帧才真建，
所以和 `0x0300` 一起在 `0x0201` 之后发就赶得上。

**角色 id ↔ 名字**（`Data/ChrProps.ini`，`Models/Characters/chNN`）：

```text
0 타이泰尔 / 1 카실卡希尔 / 2 프로코布洛克          ← 原本就有的三个
100 엘리어스 / 101 진 / 102 발키리瓦尔基里 / 103 화이트 엘리어스 /
104 발키리 로터스 / 105 발키리 재규어 / 106 시리아 / 107 라스 /
108 라스 티타늄 / 109 파이크 / 110 시리아 마스      ← 商城角色，本次解锁
3 아이린 / 98 쉐도우 타이 / 99 랜덤                 ← 放不出来，见下
```

`3` 和 `98` 被按钮循环 `0x4f58f1` / `0x4f58e8` **显式跳过**；
`99 랜덤` 要 `0x407168` 的第三个参数为真，而 `CharacterChanger` 传的是 0。
这三个不是被我们挡的，是客户端写死的。

### B. 关卡：`map.ini` 的 `OpenLocale` 地区掩码

`Data/map.ini` 每张地图一行 `OpenLocale`（文件里的注释：
`1 - 한국, 2 - 일본, 4 - 중국`，按位或）。中国版 `[[0x72e320]] = 2`，
客户端**两处**拿 `1 << 2 = 4` 去 test：

```text
0x40b419  地图目录加载（0x40b2a1，启动时读 map.ini）
          掩码不匹配 -> 0x40b47a 把记录 delete 掉，目录里根本没有它
0x4368cf  「建立房间(任务)」填「任务」下拉框（0x4365e1）
          掩码不匹配 -> 跳过这一条
```

map.ini 里的 7+1 个关卡：

```text
QuestId 1 불프로그机械青蛙       Boss00                OpenLocale=7  ✅ 原本可见
QuestId 2 드라카岩浆巨龙         Quest02_1/_2          OpenLocale=7  ✅
QuestId 3 비밀의 섬神秘岛        Quest03_1/_6          OpenLocale=7  ✅
QuestId 4 자미로건쉽鲸鱼战舰     Quest04               OpenLocale=7  ✅
QuestId 5 다크나이트黑骑士       Quest05_stage/_boss   OpenLocale=3  ← 本次解锁
QuestId 6 브레그마太阳齿轮       Quest06_stage/_boss   OpenLocale=3  ← 本次解锁
QuestId 7 자미로 비밀 연구소     Quest07_Intro/_1/_2/_3 OpenLocale=3 ← 本次解锁
QuestId 8 푸른 하늘              Quest08/_1            OpenLocale=0  ✗ 全区关闭，且
                                                       0x6dc52c 的 id 表里也没有它
```

**四个关卡的地图文件、剧情文本、boss 数据全都在包里** ——
`Data/Quest/Quest05..07/` 有完整的 `bossNN-*.ini` / `mob-*.ini` / 剧情 `.uni`，
`Maps/` 里 `Quest05_stage#Easy..#Extreme.map` 等一个不缺。
中国版只是当年没上线这几关。**剧情对白也早就翻译好了**（实机确认，
只有关卡名 `자미로 비밀 연구소` 没进中文文本表，下拉框里显示韩文）。

### C. 一个非做不可的连带 patch：角色 110 的战斗内图标

服务端把 11 个角色全放出来之后，**进关卡瞬间必崩**：

```text
C0000005 @ 0x430857
0x40bd40(stage 7 工厂) -> 0x477bab -> 0x4f5970 -> 0x4f682a -> 0x430857
```

战斗内的 `CharacterChanger` 给每个可选角色建按钮，图标下标由 `0x4f676e`
起的 switch 按角色 id 硬编码：

```text
0/1/2 -> (id*2, id*2+1)   100 -> (6,7)     101 -> (8,9)     103 -> (0x0a,0x0b)
102 -> (0x0c,0x0d)  104 -> (0x0c,0x0e)  105 -> (0x0c,0x0f)  106 -> (0x10,0x11)
107 -> (0x12,0x13)  108 -> (0x14,0x15)  109 -> (0x16,0x17)
110 -> (0x18,0x19)  3 -> (0x1a,0x1b)
```

而图集 `Images/NewUI2/BigChrIcons.smf` 是**按地区换的**
（`0x558916` 把路径映射到 `Images/Chinese/BigChrIcons_CN.smf`）：

```text
Images/NewUI2/BigChrIcons.smf        28 帧（0..27）  韩国版
Images/Chinese/BigChrIcons_CN.smf    24 帧（0..23）  ★ 中国版
Images/Japanese/BigChrIcons.smf      24 帧
```

`.smf` 的格式很简单：`int32 版本(2) + int32 帧数 + 帧数×32 字节`。
下标 0x18/0x19 越界，`0x430854 mov edx,[eax+edx]` 从数组外取到垃圾指针，
下一句 `0x430857 mov edi,[edx+0x10]` 就炸。**图是真没有**。
（角色 3 要的 0x1a/0x1b 同样越界，但它压根不建按钮，不用管。）

`Chinese/ChrIcons_CN.smf` 是 28 帧，所以**房间里**的「人物选择」14 个头像
全都画得出来 —— 只有战斗内那条换人条会崩。

### 复现「原版行为」

- 角色：`python tools/gs_ctl.py equipped 0`（把座位 0 的清单清空）→ 重进房间，
  「人物选择」变回 3 个头像。
- 关卡：`BSHOOK_KEEP_REGION_LOCK=1` 起客户端 → 下拉框变回 4 关。
  ⚠ 这时服务端也必须把角色 110 关掉（存档 `character_unlock_all=false`），
  否则进关卡还是会撞 §C 的崩溃。

### 新工具

`tools/probe_char_list.py <pid>` —— 把角色判定链每一格都读出来：
LobbyStage、我的座位、六个座位的物品清单指针、清单里的物品 id 及其归属角色、
最后算出「人物选择会出现哪几个角色」。
`tools/probe_quest_list.py` 现在会顺带报「下拉框的地区判定有没有被 patch」，
免得「掩码通过」那一列照原版的 2 算、把刚解锁的关卡误报成不通过。

### 实机验收（会话 21）

- 「任务」下拉框 **7 项全出**：神秘岛 / 岩浆巨龙 / 机械青蛙 / 鲸鱼战舰 /
  **黑骑士** / **太阳齿轮** / **자미로 비밀 연구소**
  （`logs/shot_s21_questlist.png`）
- 目录记录数 **32 → 101**，其中 `Quest` 记录 **6 → 14**（`probe_quest_list.py`）
- 「人物选择」**3 → 14 个头像**，出现滚动条（`logs/shot_s21_room2.png`）；
  `probe_char_list.py` 逐件列出 11 个物品并解出 14 个角色
- 选中 **102 발키리瓦尔基里**，房间模型 + 3D 预览 + 数值面板全部切过去
- 进 **QuestId 5 黑骑士**：中文剧情演出 → 地下城地图 → 可玩战斗
  （`logs/shot_s21_quest5_battle.png`）
- 进 **QuestId 7 자미로 비밀 연구소**：中文剧情 → 实验室地图 →
  **boss「DARK KNIGHT」带血条和弱点圈**（`logs/shot_s21_quest7_battle.png`）
- 全程 0 个 `0x0106 gcpReportHack`，无崩溃报告

### 记一笔

**「素材在包里」和「客户端认这个内容」是两码事，而且锁可能不止一把。**
这次两个现象（角色少、关卡少）看起来是同一类问题，实际上一个在服务端
（背包物品）、一个在客户端（地区掩码），**只修一边都只解决一半**。
先分别找「谁是这个列表的数据源」，再问「数据源被谁挡住」，别急着归因。

### 便携包已跟着重新生成（会话 21）

改了 `server/`（`0x030b`）和 `hook/`（地区差异 patch）之后重跑了
`tools\build-portable.ps1 -Zip`：

```text
dist\PopShot-portable-win64       389.3 MiB
dist\PopShot-portable-win64.zip   363.7 MiB / 209 files（7z t: Everything is Ok）
ZIP SHA-256  d22d782de4a0e4ea0b2fe39d90d44bcf6a1941f3a7a7728a6a96b48a1ab89c1c
```

已核对发布目录里的 `hook\bin\bshook.dll` 与源树同哈希、
`server\gameserver.py` 含 `OP_SLOT_EQUIPPED_LIST = 0x030b`。

### 一处**没**踩到但留了防御的坑（会话 21）

`0x406e4e` = 「把某个座位的物品清单重建成空的」，五个调用点里
`0x40f55c` / `0x40f619` / `0x40f7b9` 都在切 stage 的路上（其中 `0x40f619`
紧跟着 `ChangeStage(6)`）。担心「进关卡 / 结算回房间会把清单清掉」，
所以 `Conn.leave_game_result` 里补了一发 `0x030b`。

**实测这条路清单没被清**（`probe_char_list.py` 在房间里、关卡里、
结算回房间后各读一次，都是 11 件 / 14 个角色，
`logs/shot_s21_backroom.png`）。那一发因此是**防御性**的，不是必需的 ——
但整份替换是幂等的，成本一个包，漏了的代价是「人物选择」缩回 3 个头像。

---

## 120. ★★★★★ 房间里的两个后续 bug：角色偶尔缩回 3 个 / 新关卡换不过去

会话 21 解锁了 14 个角色和 7 个关卡之后，用户报了两个现象。
**它们看起来又是「同一类问题」，实际上还是两把完全不同的锁**（和 §119 一样）：

| 现象 | 真凶 | 修在哪 |
|---|---|---|
| 进房间**小概率**只剩 3 个角色 | 建房四连发被客户端的 recv 切开 | 服务端合并成一次 `sendall` |
| 新关卡进房间就换不过去 | `DlgSelectQuestMap` 里**按地区写死**的关卡环 | 客户端第 4 个 patch |

### A. 「有时候只有 3 个角色」= 一个 6% 左右的收包竞态

**客户端的 stage 切换是延迟一帧的**：

```text
0x0201 的处理器 0x54f747 最后调 ChangeStage(0x40e47f)
   -> 它只做两件事：[app+0x58] = 工厂函数、[app+0x5c] = stage id
   -> RoomStage 构造函数 0x466979 要到**下一帧**才跑
        0x466ea3  call 0x4698af(RoomStage, 1)   ★ 建「人物选择」的头像按钮
                  按钮个数写进 [RoomStage+0xf4]，一次建完
   ★ 全镜像只有两个地方调 0x4698af：这里，和滚动条的回调 0x4698a4。
     也就是说**除了拖滚动条，按钮永远不会重建** —— 而只有 3 个角色时
     根本没有滚动条，这一局再也回不来。
```

所以「0x0201 和 0x030b 是否落在同一帧的那一次 recv 里」直接决定
房间里有几个头像。服务端本来是**四次独立的 `sendall`**，
中间还各夹着一次 `log()`（`print(flush=True)` + 文件 flush）：

```text
[00:15:44.970] ← 回 0x0303 …
[00:15:44.970] ← 回 gspRepCreateSession
[00:15:44.971] ← 回 0x0300 房间座位快照     ★ 0x0201 和这里之间约 1 ms
[00:15:44.971] ← 回 0x030b 座位 0 物品清单
```

客户端一帧 16 ms，缝是 ~1 ms → **约 6% 的概率**中招。
和用户说的「小概率、有时候又正常」完全对得上。

**修法**：`Conn.send_batch()` 把这一串攒起来、**一次 `sendall`** 发出去
（259 字节，loopback 上一定进同一个接收缓冲）。SimpleCipher 是逐字节流密码，
`encrypt(a+b) == encrypt(a)+encrypt(b)`，**下发字节一个都没变**，
只是从 4 次写变成 1 次写。「结算看完回房间」那条链（`0x0403` 也会
`ChangeStage(5)` 重建房间 UI）同因同修。

**A/B 实测（因果钉死在这一件事上）**：

```text
--room-burst-delay 30   逐包发、每包间隔 30ms
   -> 「人物选择」只有 3 个头像、没有滚动条，角色还被打回 0 泰尔
      logs/shot_s22_ab_broken.png          ★ 复现出用户报的原始现象
默认（合并成一次 259 字节的写）
   -> 6 个头像 + 滚动条，角色是存档里的 2 布洛克
      logs/shot_s22_ab_fixed.png
```

★ `probe_char_list.py` **不能**当这条的判据：它算的是「**现在**问
`0x40713a` 会得到几个」，包全到齐之后永远是 14。真正的判据是
`[RoomStage+0xf4]`（构造时冻住的按钮数），也就是截图。

### B. 「新关卡只能在建房界面选」= 房间里的关卡环是写死的

待机房间右侧 `DlgSelectQuestMap` 的 `stageLBtn` / `stageRBtn`
**不查 map.ini，也不查 OpenLocale**。`DlgSelectQuestMap::OnEvent`
（`0x466264` = vftable 槽 30）当场按地区把关卡 id 塞进一个环形数组
（`0x466727` 是 push_back）：

```text
0x466318  eax = [[0x72e320]]（地区序号）
0x46631d  je  0x466364    locale 0 韩 -> [3,2,1,4,5,6,7,3]
0x466320  je  0x466364    locale 1 日 -> 同上
0x466323  jne 0x4663ec    locale 其它 -> 不建表
0x466329                  locale 2 中 -> [3,2,1,4,3]      ★ 只有 4 关
   首元素 3 在末尾重复一次，◀ 和 ▶ 才都能绕回去
   0x466400 stageLBtn: 倒着找当前 id，取 [i-1]
   0x466429 stageRBtn: 正着找当前 id，取 [i+1]
```

**这就是为什么建房下拉框已经有 7 关（§119 patch 了 `0x4368cf`），
进了房间却只在 3→2→1→4 里打转** —— 两处是两套完全独立的数据源。
会话 21 之前的日志里房间按 ◀ ▶ 发出的 `0x0302` 正好循环
`Quest03_1 → Quest02_1 → Boss00 → Quest04 → Quest03_1`，写在脸上。

**修法**：`0x46631d` 的 `je 0x466364`(`74 45`) → `jmp`(`EB 45`)，
让中国区也走韩/日那条 7 关分支，2 字节换 2 字节（D058）。

**实测**：房间里按 ▶ 走出
`Quest03_1 → Quest02_1 → Boss00 → Quest04 → Quest05_stage →
Quest06_stage → Quest07_Intro`，面板依次显示
神秘岛 / 岩浆巨龙 / 机械青蛙 / 鲸鱼战舰 / **黑骑士** / 太阳齿轮 /
**자미로 비밀 연구소**（`logs/shot_s22_room_q5.png` / `shot_s22_room_q7.png`），
在房间里切到黑骑士后按 F5 一路进到可玩战斗
（`logs/shot_s22_quest5_from_room.png` / `shot_s22_quest5_battle.png`）。
全程 0 个 `0x0106`、无崩溃报告。

### 同一个函数里另外两处地区差异（**故意没动**）

`DlgSelectQuestMap` 里还有两处按 `locale == 2` 分叉的地方，都跟难度有关：

```text
0x466309   esi = min(map[关卡 id] + 1, 4)；中国版无条件压成 3
0x46649c   difficultyRBtn 转到 4 时，中国版拨回 1
```

也就是**中国版 3 档难度、韩/日 4 档（多一个「극한」）**。
用户报的是关卡不是难度，而第 4 档会牵扯到
`#Extreme.map` 是否齐、准入校验 `0x468176` 的上限（§118）等一串东西
—— 不在这次范围内，保持原样。要开的话是同一个函数里再加一个 patch。

### 记一笔

**「同一个列表在两个界面里，数据源可能完全不同。」**
建房对话框的关卡下拉框是查 map.ini 目录**过滤**出来的，
房间里的关卡环是**硬编码**的。修好了前者一点都不代表后者会跟着好 ——
和 §119「角色锁在服务端、关卡锁在客户端」是同一个教训的第二次出现。

**「间歇性 bug 先去数时间窗口。」** 这次不用探针也不用抓包：
`0x0201` 和 `0x030b` 两行日志的时间戳差 1 ms，客户端一帧 16 ms，
6% 一算就出来了，正好对上「小概率」。然后用一个能把窗口拉大的开关
（`--room-burst-delay`）把它变成 100% 复现，A/B 一跑因果就钉死了。

### 便携包已跟着重新生成（会话 22）

改了 `server/`（`send_batch`）和 `hook/`（第 4 处地区 patch）之后重跑了
`tools\build-portable.ps1 -Zip`：

```text
dist\PopShot-portable-win64       389.3 MiB
dist\PopShot-portable-win64.zip   363.7 MiB / 209 files（7z t: Everything is Ok）
ZIP SHA-256  8f59b530f6701597f505a6fb6eac94988bf7e6895195e7807c3b2781436cf03c
```

已核对发布目录里的 `hook\bin\bshook.dll` 与源树同哈希、
`server\gameserver.py` 含 `def send_batch` 和 `--room-burst-delay`。
★ 这个脚本**不覆盖已有目录**，重跑前要先删 `dist\PopShot-portable-win64`
和同名 zip（会话 22 踩了一次）。

---

## 121. ★★★★★ GameGuard 绕过不再依赖 +2.5 秒：DR0 + VEH 在校验执行瞬间返回成功

### 旧实现真正依赖的两个时刻

旧版 `patch_thread` 做的是：

```text
进程启动
  -> 固定 Sleep 到 +2.5s
  -> 轮询 0x54b0fc 是否已解成 E8 CF 60 01 00
  -> 改成 B8 55 07 00 00（mov eax,0x755）
  -> ~+5s 执行 GameGuard 校验
```

轮询特征字节只能证明**目标页已经解壳**，不能证明 ASProtect 的后台 CRC 已经结束。
§32 实测 +0.7 秒写入有 3/4 概率触发 `Protection Error / Error: 15`；
+2.5 秒只是本机 5/5 成功的经验窗口。换机器后 CPU、驱动、杀软扫描都会改变相对时序，
所以它不适合作为便携发布版的必经启动条件。

### 最终实现：执行事件代替时间窗口

共享常量在 `hook/gg_bypass.h`；两个二进制必须配套构建：

```text
bsloader
  1. CreateProcess(CREATE_SUSPENDED)
  2. 建 Local\\PopShotBshookReady_<pid>_<tick>，名字放 BSHOOK_READY_EVENT
  3. QueueUserAPC(LoadLibraryA, bshook.dll) -> ResumeThread
  4. 等“DR0 已武装”事件；等不到或客户端先退就明确失败

bshook.dll（LoadLibrary APC 在客户端主线程上执行）
  1. DllMain 记录主线程 tid，AddVectoredExceptionHandler(first=1)
  2. 武装线程等待主线程 EIP 离开 ntdll/kernel32/kernelbase/bshook
     —— 这表示注入 APC 的 CONTEXT 恢复路径已经结束
  3. SuspendThread -> CONTEXT_DEBUG_REGISTERS -> DR0=0x54b0fc
     DR7.L0=1, RW0=00, LEN0=00 -> ResumeThread -> SetEvent

主线程真正到 0x54b0fc
  EXCEPTION_SINGLE_STEP -> VEH
  -> 核对 EIP=0x54b0fc 且字节仍为 E8 CF 60 01 00
  -> 清 DR0/DR6/DR7.L0
  -> EAX=0x755, EIP+=5
  -> 从 0x54b101 `mov esi,eax` 继续，走原版成功分支
```

硬件断点不需要调试器附加；它只属于主线程，命中一次即撤。
`0x54b0fc` 的代码页**从头到尾没有被修改**，所以 ASProtect 的 CRC 早跑、晚跑或并行跑
都看不出差异。签名不符时 VEH 只撤断点并让未知指令正常执行，不会在其它客户端版本上
盲目跳 5 字节；后台日志会明确写“指令签名不符”。

### 第一版握手失败：APC 会恢复旧 CONTEXT，把刚写的 DR0 抹掉

第一次实现让 DLL 在 DllMain 装完 VEH 后立刻 `SetEvent`，启动器醒来后暂停主线程、
`SetThreadContext(DR0)`。API 返回成功，但当时主线程仍在 `LoadLibraryA` APC 内：

```text
进入 APC 时保存 CONTEXT（Dr0=0）
  -> LoadLibraryA -> DllMain -> SetEvent
  -> bsloader 把当前 CONTEXT.Dr0 改成 0x54b0fc
  -> APC 返回，KiUserApcDispatcher/NtContinue 恢复进入前的 CONTEXT
  -> Dr0 又变回 0                         ★ 断点消失
```

证据 `logs/bshook_20260809_022348_pid32164.log`：有
`GameGuard VEH 已安装`，却没有“DR0 命中”；约 4.46 秒后进入原失败分支，
记录「Game guard文件不存在或已变更」，进程 exit code 1。

最终版把设置 DR0 移进 DLL 的专用线程，握手含义改成“主线程已退出 APC 且 DR0 已武装”。
成功日志 `logs/bshook_20260809_022603_pid31292.log`：

```text
02:26:03.712  GameGuard VEH 已安装
02:26:03.820  主线程已离开注入 APC（EIP=753F587C），DR0=0054B0FC 已武装
02:26:08.310  ★GameGuard 校验：DR0 命中，EAX=0x755，跳过状态取值调用
02:26:08.933  WINDOW visible=1 class=[#32770] title="PopShot"
```

### 验证结果

- 有效的 5 次启动（`02:26:03 / 02:26:53 / 02:26:59 / 02:27:05 / 02:27:40`）
  全部依次出现“已武装 → DR0 命中 → PopShot 登录框”，**5/5**。
- 5 次均无 `Protection Error`，也没有 GameGuard 错误框。
- 中间一次在上一个客户端尚未完全退出时重启，被原版单实例互斥体挡掉；没有计入 5 次，
  与 §9 的已知行为一致，不是 DR0 失败。
- `C:\Python314\python.exe -m unittest test_account_store test_gameserver`：
  **228 项全过**。

### 仍保留的 +2.5 秒不是漏改

`CODE_PATCH_DELAY_MS=2500` 现在只保护仍会改游戏代码的项目：地区锁四处 patch、
挂机计时器、SnowCipher detour、RendererInit 诊断 hook。GameGuard 绕过已经完全移出
`patch_thread`。如果以后要消除其它 patch 的时序依赖，需要分别为它们找因果事件，
不能因为 D060 就把这条延时整体删掉。

### 便携发布物已同步

源树重新编译后，`bsloader.exe`、`bshook.dll` 和 README 已同步进
`dist\PopShot-portable-win64`。从该目录直接启动再次得到
“DR0 已武装 → 命中 → PopShot 登录框”，排除了只在开发目录成功的可能。

```text
dist\PopShot-portable-win64.zip
大小      380,353,394 bytes（约 362.7 MiB）
SHA-256   ab9c02246cb94c531115726991e62510726374c7e0f14bf434067b96efac7b72
7z t      Everything is Ok
```
