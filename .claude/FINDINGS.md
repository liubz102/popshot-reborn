# FINDINGS — V0.2 硬事实

> 只记「**是什么**」。「为什么这么选」记在 `DECISIONS.md`。
> **失败的尝试也要记**，避免下一个会话重复踩坑。
> 编号**接着 V0.1 往下排，从 §122 起**（跨版本全局唯一，引用不会歧义）。

---

# 第零批 —— V0.1 关键结论速查（索引，不是原文）

V0.1 的原文在 `develop_history/V0.1_基础单机功能开发/.claude/FINDINGS.md`（§1~§121，5517 行）。
下面这张表是**索引**：先在这里定位，再决定要不要翻原文。写「V0.1 §NN」就是指那个文件。

## A. 联机开发最可能要翻的条目

| 想知道什么 | 去看 |
|---|---|
| **客户端→服务端 opcode 全表**（94 个，含类名） | V0.1 §45 |
| **服务端→客户端分发跳表**（`0x54e036`，opcode 就是下标） | V0.1 §46 / §81 |
| 认证服（47611）NMCO 协议、帧格式、16 个消息类 | V0.1 §35 / §36 / §38 / §39 |
| **认证握手完整报文**（`authserver.py --reply login` 实测跑通） | V0.1 §39 |
| 游戏服（27799）握手序列 + `gcpReqLogin` 载荷逐字段 | V0.1 §44 |
| 游戏服帧格式（`0xFE` 控制帧 / `0xFF` 游戏帧）+ SimpleCipher | V0.1 §42 / §43 |
| `gspRepLogin` 全部 8 个业务 int32 的落位 | V0.1 §95 |
| 金币 / 经验 / 等级只能靠 `0x0600 gspRepMoney` 下发 | V0.1 §96 |
| **房间座位 `SessionSlot` 线格式**（12 个字段） | V0.1 §78 |
| **`0x0300` 房间座位快照** | V0.1 §79 |
| **`0x0301` 换角色要服务端广播回来才生效**（action 4） | V0.1 §103 |
| `Session` / `SessionDescriptor` 线格式；房间类型 `1 普通 / 2 闯关 / 5 天梯` | V0.1 §69 / §59 |
| 建房那一串包的**顺序硬约束**（`0x0303` 必须在 `0x0201` 之前） | V0.1 §65 / §66 / §67 |
| **一次 `sendall` 合并**，否则「人物选择」小概率缩回 3 个头像 | V0.1 §120 |
| 大厅标签页切换 `0x020b → 0x0701` | V0.1 §62 |
| 「不符合等级要求，无法连接」= 账号等级**恰好等于 1** | V0.1 §83 |
| 「等级太低，无法选择任务」= 读的是**房主座位里的 u16 等级** | V0.1 §77 |
| 开局链 `0x0402 → 0x0401 / 0x0400 → 0x0403 → 0x0402` | V0.1 §84 / §85 |
| 死亡 / 重生链 `0x0408 → 0x0406`、`0x0413 → 0x0419` | V0.1 §108 |
| 掉落 `0x0406 → 0x0404`、拾取 `0x0407 → 0x0405` | V0.1 §113 / §115 |
| 换图 `0x0411 → 0x0417`、加载放行 `0x0412 → 0x0418` | V0.1 §111 |
| 结算 `0x040f → 0x0309 + 0x0411`（顺序是硬约束）+ 四格数值来源 | V0.1 §98 / §99 / §116 |
| 「完成 / 未完成」标签在 `0x0309` 的尾部数组里 | V0.1 §112 |
| `0x0106 gcpReportHack` = 免费的正确性检查器 | V0.1 §88 |
| 难度解锁只能靠 `0x020c` 全量快照；`0x0416` 会弹通知窗 | V0.1 §118 |
| 隐藏角色 / 隐藏关卡两把锁（`0x030b` + 地区 patch） | V0.1 §119 |
| GameGuard 绕过：DR0 + VEH 在 `0x54b0fc` 执行瞬间返回 `0x755` | V0.1 §121 |
| 登录框控件 id（`1004`/`1005`/`1006`/`1011`/`1012`） | V0.1 §24 |
| 客户端命令行 `/serverip:` `/serverport:` + 已死的官方地址 | V0.1 §14 / §3 |
| 日志刷盘拖慢启动那件事（`bslog` 每条 2 毫秒） | V0.1 §105 |
| `.ps1` 必须 UTF-8 with BOM | V0.1 §106 |

## B. 绝对不要做的事（V0.1 血泪，V0.2 照样成立）

- **不要发 `0x0409`**：两条分支都是「开始失败」提示框（V0.1 §76）。
- **不要在客户端报 `0x0403` 之前发 `0x0402`**：空指针崩溃（V0.1 §82）。
- **不要发服务端方向的 `0x0408`**：那是「与 %s 的网络状态不佳」告警（V0.1 §89）。
- **不要回显客户端的 `0x0406`**（那是掉落请求，服务端方向是死亡广播 = 随机杀角色）。
- **不要回显客户端的 `0x0411`**（那是换图请求，服务端方向是结算 = 中途踢进结算界面）。
- **不要回显客户端的 `0x0417`**（那是通关上报，服务端方向是换图放行）。
- **不要在收到 `0x0412` 之前发 `0x0418`**：客户端换图加载循环是**前置**判断。
- **不要给 `0x0407` 的应答加过滤**：漏回一发，那件掉落物**永久作废**（`[item+0x2a8]=1`）。
- **不要在 `0x0300` 之前发 `0x030b`**：角色持有判定第一步查「座位已占用」。
- **不要用 `0x0416` 改难度解锁**：会弹一个没读过的通知窗。
- **不要按进程名杀 Python**（`Get-Process python | Stop-Process`）：用户机器上还有别的活儿。
  一律按端口的 `OwningProcess` 精确停。
- ★ **同号反向是这套协议的常态**：同一个 opcode 在两个方向上语义完全不同，
  只能靠方向区分。加任何新应答之前先查 V0.1 §45 + §46 两张表。

## C. 账号存档 JSON 的形状（V0.1 建立，V0.2 在此之上扩展）

```json
{ "schema_version": 1, "active_account": null,
  "accounts": { "<用户名>": {
      "password": "", "display_name": "",
      "tutorial_completed": false, "tutorial_progress": 0,
      "level": 1, "experience": 0, "money": 0, "character": 0,
      "quest_difficulty": {}, "quest_unlock_all": true,
      "character_unlock_all": true, "owned_characters": [] } } }
```

- 等级由经验推出（`EXPERIENCE_PER_LEVEL = 100`，本地自定，不是逆出来的）。
- `quest_difficulty` / `quest_unlock_all` 只能靠 `0x020c` 下发。
- `character_unlock_all` / `owned_characters` 只能靠 `0x030b` 下发，且要排在 `0x0300` 之后。
- **`active_account` 是 V0.1 的单机遗物，V0.2 要删掉**（换成票据）。

---

# 第一批 —— V0.2 联机（2026-08-10 起）

## 122. ★★★★ 战斗内的角色位置同步**不在 gcp/gsp 协议里**（线索汇总，待 J.1 证实）

V0.1 结束时留了一句「真正的角色位置同步包还没找到」，嫌疑列表是
`0x040a` / `0x040b` / `0x040d`。逆向产物里有一组类名指向另一个答案：

```text
re/packets.txt      Packet_gcpStartTcpRelay        (客户端 -> 服务端)
                    Packet_gspJoinRelay            (服务端 -> 客户端)
                    Packet_rcpRegister             ← rcp = relay client packet
                    Packet_rcpRepPing
                    Packet_gspToggleUdpClientCommunication
                    Packet_gspToggleSendReplay
re/rtti_types.txt   RelayConnection   RelayAuthData   UDPBinder   UdpPacket
re/vftables.json    0x65e0d4 Packet_gcpStartTcpRelay   0x691814 Packet_gspJoinRelay
                    0x69152c RelayConnection           0x691580 RelayAuthData
V0.1 §45            0x00xx 段 = 「RelayConnection 用」（0x0000 / 0x0001 / 0x0003）
V0.1 §44            gcpReqLogin 的载荷里带着客户端的**内网 IP**（实测 192.168.11.215）
                    和疑似网卡 MAC —— 服务端要这个只有一个用途：告诉别的客户端去哪找它
boost 绑定类名      bind_t<..., GameSession, UDPBinder, ..., RawPacket&>
                    → GameSession 有一个吃 RawPacket 的 UDP 回调
```

**推论**（尚未实测，J.1 的任务就是证实或推翻）：
战斗内同步走**客户端之间的 UDP 直连**，服务端用 `gspToggleUdpClientCommunication`
开关它；打不通时走 `gcpStartTcpRelay` / `gspJoinRelay` 让双方连到一台中继服务器
（`RelayConnection` + `rcpRegister`，opcode 在 `0x00xx` 段）。

**为什么这对 V0.2 是头等大事**：
- 内网 IP 上报意味着 **P2P 只在同一局域网内可用**；云服务器在 NAT 后大概率打不通。
- 如果结论成立，云端联机必须由我们实现原版那套 TCP 中继 —— 那套协议一个字节都还没逆过。

**J.1 的验证手段**（成本从低到高）：
1. `bshook` 加 `sendto` / `recvfrom` / `WSASendTo` / `WSARecvFrom` / `bind` 的 hook，
   打一局单机看有没有 UDP 流量、绑在哪个端口；
2. `re/rtti_types.txt` 搜类名 → `re/vftables.json` 拿 vftable →
   `tools/re_bs.py xref` 找发送点 → 紧跟的 `push <opcode>` 就是包号（V0.1 §111 的手法）；
3. 读 `RelayConnection` 的虚表槽，对照 `ServerConnection`（V0.1 §47）看两者帧格式差在哪。

## 123. ★★★★★ 票据走 `CULoginReplyPacket` 的**第二个**字符串（实测钉死）

`server/protocol.py` 的 `build_login_reply(result, s1, s2, a, b, c)` 对应
`CULoginReplyPacket`（opcode `0x000c`）。客户端随后向游戏服发 `gcpReqLogin`，
**载荷第一个字段就是一个 wstring**，V0.1 §44 的实测抓包里它是**空的**
—— 因为我们一直回的是空串。

```text
认证服 0x000f CULogin2Packet(用户名, 密码)
   → 0x000c CULoginReplyPacket(result, s1, s2, a, b, c)
客户端 → 游戏服 0x0100 gcpReqLogin
   [0] wstring  ← ★ 就是从上面某个字符串字段转发过来的「票据」
   [1] 本机内网 IP（按字节）
   ...
```

### ★ 实测结论：是 **`s2`（第二个字符串）**

做法比预想的更省事 —— 第一版把票据放 `s1`、中文说明放 `s2` 就直接跑了一次，
游戏服的日志把答案写在了脸上：

```text
连接#1 ★ 认证通过 用户名='alice' 票据=124853ee…
#1 ✗ gcpReqLogin 的票据无效或已过期（登录成功）；回 gspRepLogin(result=3) 断开
                                    ↑↑↑↑
             客户端转发上来的 wstring 是「登录成功」= 我们放在 s2 的那句中文
```

所以线上顺序是 `int32 结果码 / string s1 / string s2 / int32 ×3`，
**客户端转发给 `gcpReqLogin` 的是 `s2`**。`s1` 客户端没有转发（多半是显示用的）。

改法：`authserver.make_reply()` 里 `s1=中文说明, s2=票据`，
`--ticket-field` 默认 `s2`（`s1` 只留给回归排查）。
`test_online.py` 有一条 `test_the_ticket_goes_into_the_second_string_field`
钉住这件事，别再改回去。

**实机验收**（V0.2 会话 01）：注册页建 `alice` → 游戏里填 alice/pw123 →
`认证通过 票据=d9a3c3e1…` → `gspRepLogin(result=0) 账号='alice' 票据=d9a3c3e1…
stored_level=1 下发等级=2`。多账号隔离这条链**通了**。

## 124. ★★★★★ 启动「时有时无地失败」= DR0 武装循环把**毫秒当成了次数**

**现象**：`start.bat` 有时正常进登录框，有时游戏窗口一闪就没了，
`logs\bsloader.err` 写着：

```text
[bsloader] 失败: 等待 bshook.dll 初始化握手 (GetLastError=1460)   ← 1460 = ERROR_TIMEOUT
```

而对应的 `logs\bshook_*.log` 里**只有 VEH 安装那一行，没有「DR0 已武装」**，
日志在 `install_hooks()` 的 `HOOK 主模块范围` 之后就断了。
V0.2 会话 01 实测 6 次冷启动里失败 2 次。

**根因**（`bshook.c` 的 `arm_gameguard_breakpoint_thread`）：

```c
/* 改之前 */
for (ticks = 0; !g_stop && ticks < POPSHOT_BSHOOK_READY_TIMEOUT; ticks++) {
    SuspendThread(main_thread);  ... GetThreadContext ...
    if (main_thread_left_injection_apc(ctx.Eip)) { 设置 DR0; break; }
    ResumeThread(main_thread);
    Sleep(1);
}
```

`POPSHOT_BSHOOK_READY_TIMEOUT` 是给 `bsloader` 用的**毫秒数**（10000），
这里却被当成了**循环次数**。Windows 的 `Sleep(1)` 按系统定时器精度取整，
默认 15.6 毫秒 —— 也就是说：

| 以为 | 实际 |
|---|---|
| 循环最多跑 10 秒 | 循环最多跑 10000 × 15.6 ms ≈ **156 秒** |
| bsloader 等 10 秒够用 | 10 秒内循环**只跑了六百来轮** |

主线程慢一点离开注入 APC（机器忙、ASProtect 解壳久），
`bsloader` 就先判超时把子进程杀了，而 DLL 那边还在慢悠悠地转圈，
连一行失败原因都没来得及写。**这不是概率问题，是两边用了不同的时间基准。**

**顺带解释了日志为什么断在 `GetModuleHandleA`**：武装循环每一轮都
`SuspendThread(主线程)`。主线程如果正好在 loader 里持着 loader lock 被挂起，
`watch_thread` 的 `GetModuleHandleA("user32.dll")` 就会一直等 ——
日志看起来像「卡死」，其实是被自己人按住了。

**修法**：循环上限改成按 `GetTickCount()` 算的**时间**，并留 2 秒余量让 DLL
先写下明确的失败原因；`POPSHOT_BSHOOK_READY_TIMEOUT` 10 秒 → 15 秒
（机器忙的时候光解壳就要好几秒）。失败分支现在会打印「试了几轮 / 几毫秒」。

**验收**：改完后干净冷启动 `武装=True 命中=True 登录框=True`。

⚠ **一个会骗人的复现方式**：连续「杀掉 BigShot → 立刻重启」不要间隔太短。
那样失败的原因是**另一件事** —— `BigShot_Assa` 互斥体还没释放，新实例秒退，
`bsloader.err` 写的是「bshook.dll 初始化前游戏已经退出」而不是握手超时
（V0.1 §9）。两条错误信息要分清楚，别把互斥体的问题当成握手的问题。
两次启动之间至少留 5 秒。

## 125. ★★★★ 登录框（`#32770` / `PopShot`）的完整控件表和几何

`tools\gui_probe.py enum <pid>` 实测（V0.2 会话 01，已给它加上位置输出）。
**对话框客户区 530×527**：

| id | class | x, y, w, h | 原文 |
|---|---|---|---|
| 1013 | Static | 21, 275, 133×19 | 开始火枪手之旅! |
| 1014 | Static | 32, 312, 54×14 | 选择分区: |
| **1011** | **Button** | **98, 310, 126×18** | **炮火连天(电信)** |
| **1012** | **Button** | **98, 331, 126×18** | **枪林弹雨(网通)** |
| 1015 | Static | 245, 310, 49×14 | 用户名: |
| 1016 | Static | 245, 338, 49×14 | 密码: |
| 1004 | Edit | 305, 308, 117×21 | （用户名）|
| 1005 | Edit | 305, 336, 117×21 | （密码）|
| 1006 | Button | 434, 308, 74×51 | 开始 |
| 1007 | Button | 305, 362, 96×18 | 记住帐号 |
| 1009 | Static | 305, 385, 135×18 | 您忘记密码了吗? |
| 1017 | Static | 21, 415, 200×19 | 您还没有注册成为世纪天成用户吗? |
| 1018 | Static | 32, 446, 403×14 | 尽情享受炮炮火枪手带给您的乐趣, |
| 1019 | Static | 32, 460, 420×14 | 请赶紧注册成为世纪天成会员,让我们用游戏创造乐趣! |
| **1010** | **Static** | **32, 492, 152×16** | **注册成为世纪天成用户**（那条蓝色链接）|
| -1 | Button | 4, 256, 522×264 | （分组框）|
| -1 | Static | 21, 296 / 434, 490×2 | （两条分隔线）|
| 0 | MPlay.Control.MiniBrowser | -1, 0, 533×260 | 内嵌 IE 公告页 |

**V0.2 靠这张表定的两个改法**：

1. **`1012` 的新文案装不下**。「联机(服务器地址请改:server.config)」比
   「枪林弹雨(网通)」长一倍多，126 像素会被裁掉；右边 `x=245` 就是「用户名:」，
   横着加宽会压上去。→ 加 `BS_MULTILINE` 再放高成 **200×36**，
   左边这一列 `y=349..415` 之间是空的，两行正好放得下。
2. **`1010` 那条链接根本点不动**，原版就点不动：
   - 它**没有 `SS_NOTIFY`** —— 没这个样式的 Static 对 `WM_NCHITTEST` 返回
     `HTTRANSPARENT`，鼠标消息压根到不了它身上；
   - 而且它在 **z 序上被分组框压着**（`EnumChildWindows` 按 z 序返回，
     那个 `id=-1` 的分组 Button 排在所有 Static **前面**），
     `WindowFromPoint` 在链接位置拿到的是分组框。
   - 实测：真鼠标点上去，**没有任何 `ShellExecute` 调用**（我们 hook 了
     `ShellExecuteW/A` 全程监听，一条都没有）。

   → 所以不去猜原版怎么处理的：加 `SS_NOTIFY` + `SetWindowPos(HWND_TOP)`
   把它提到最上面 + 子类化窗口过程，自己在 `WM_LBUTTONUP` 里开我们的注册页。
   实机验收：点下去日志出现 `LOGIN 点了注册链接 -> "http://localhost:27810/"`，
   服务端 `[web] ::1 "GET / HTTP/1.1" 200`（顺带证明注册页的双栈监听是通的）。

## 126. ⚠ 注入期的线程里**不要调 `LoadLibrary`**

`install_shell_hooks()` 第一版用 `LoadLibraryA("shell32.dll")` 去拿
`ShellExecuteW` 的地址。本 DLL 是在 EXE 入口点**之前**用 APC 注入的，
`watch_thread` 跑起来时主线程还在 ntdll 的 loader 里 —— 旁边的线程调
`LoadLibrary` 会卡在 loader 锁上，实测让 `bsloader` 等不到握手而超时
（现象和 §124 一模一样，两个原因叠在一起排查了半天）。

**规矩**：注入期只用 `GetModuleHandleA`，拿不到就下一轮再试
（`watch_thread` 每 100 毫秒一轮）。真要 `LoadLibrary`，等到一个
「进程肯定已经起完了」的时刻再做 —— 比如用户点了某个按钮的回调里。

## 127. HTTP/1.1 keep-alive：**回 404 之前也必须把请求体读干净**

注册页的 `test_an_unknown_api_is_a_clean_404` 一开始是**偶发**失败（约 1/6），
报的是客户端侧的 `ConnectionAbortedError [WinError 10053]` 而不是 404。

根因不在测试里：`web/server.py` 的 `do_POST` 对认不出的路径直接回 404，
**没有读那次 POST 的 body**。我们用的是 `protocol_version = "HTTP/1.1"`，
连接默认保持；剩在缓冲里的 body 会被 `BaseHTTPRequestHandler` 当成**下一个请求的请求行**
去解析，解析失败连接就被掐掉 —— 客户端那边表现成「响应读到一半连接断了」，
而且取决于 TCP 分片时机，所以时有时无。

**规矩**：`do_POST` 一进来就把 body 读完（`_read_body()`），**再**去查路由表。
body 超限（>1 MB）时除了回 400 还要 `self.close_connection = True` ——
那种情况下我们本来就没打算读完它。

改完连跑 15 轮 289 项测试，0 次失败。

## 128. ★★★★★ 登录失败的中文提示**客户端自己会显示** —— 只要回对 NMCO 错误码

需求要的是「不存在这个用户 → 提示需要注册；密码不正确 → 如实报错」。
一开始以为要靠 `bshook` hook `MessageBoxW` 换文案，实际上**一行客户端代码都不用改**。

`CULoginReplyPacket`（opcode `0x000c`）的第一个 int32 结果码不是随便一个「非 0 = 失败」，
它会被 `nmconew.dll` 的 `0x10077000` 映射成 **NM 错误码**，客户端再拿这个码去
`Data/Chinese.ini`（UTF-16LE 的**韩→中对照表**）查中文，最后弹：

```text
MessageBoxW(text = "<中文> (<码>)", caption = "登录失败")
```

**实测三组**（`bshook` 的 MSGBOXW hook 直接把原文抓下来）。
⚠ 这三条能被玩家看见的前提是 **§129 那个「吞框」的 detour 已经拆掉** ——
在那之前框照弹、日志照记，但 `det_MessageBoxW` 直接 `return IDOK`，屏幕上什么都没有：

| 服务端回的结果码 | 客户端弹的框 |
|---|---|
| `20025` | 登录失败 / **`不存在的帐号 (20025)`** |
| `20026` | 登录失败 / **`密码错误 (20026)`** |
| 其它任何非零值 | 登录失败 / `认证服务器失败 (20000)`（一句没用的笼统话）|

`caller=00423E1F` —— 和 V0.1 §24 里「连不上认证服时弹 `认证服务器失败 (20000)`」
是**同一个调用点**，当年那个 20000 就是「落进 default 分支」的样子。

`Chinese.ini` 里对应的三条（键是韩文原串，值是中文）：

```text
ID가 존재하지 않습니다        = 不存在的帐号
비밀번호가 일치하지 않습니다   = 密码错误
인증 서버 실패                = 认证服务器失败
```

**所以 `authserver.py` 必须发 20025 / 20026**，不能用 1 / 2 这种自己编的码 ——
编的码会全部落进 default，玩家只看得到「认证服务器失败」。
`test_online.py` 的 `test_the_failure_codes_are_the_ones_the_client_understands`
钉住这三个常量。

★ 顺带作废了一条计划：**里程碑 F.6 的「bshook 换 MessageBoxW 文案」不用做了**。
但 **bshook 还是得改一处** —— 那个框本来根本弹不出来，见 §129。

### 映射表原文（`nmconew.dll` `0x10077000`，输入 = 包里的结果码）

```text
0                -> 0（成功）        0x4e22/0x4e23    -> 原样
0x4e24/0x4e25    -> 0x4e20           0x4e26~0x4e2a    -> 原样
0x4e2b(20011)    -> 0x4e20           0x4e2c(20012)    -> 0x4e26
0x4e2d(20013)    -> 0x4e20           0x4e2e/0x4e2f    -> 0x4e2f
0x4e33(20019)    -> **0（成功！）**   0x4e35(20021)    -> 原样
0x4e39(20025) / 0x4e3a(20026) / 0x4e3b(20027) / 0x4e3c(20028) -> 原样
其它一切          -> 0x4e20(20000)
```

调用链（全部静态读出来的，`0x1006ae4e` 那两行是关键）：

```text
nmconew 0x1006ad40  登录 RPC：把回包的 [pkt+0x34]（= 结果码）写进出参 out[4]，
                    传输层状态写进 out[0]
        0x1006afb0  out[0]==0 && out[4]==0 才算成功
        0x10076fd9  失败且 out[0]==0 -> MapError(out) 用的就是 out[4]
        0x10077000  上面那张映射表
BigShot 0x5332a7    错误码 -> 韩文键 -> Chinese.ini -> 中文串
        0x423de0    sprintf("%s (%d)")，0x423e19 弹框
```

## 129. ★★★★★ 登录失败框弹不出来 = **被我们自己的 `MessageBoxW` hook 吞了**

用户报「输入错误密码和不存在的用户都没有任何反应」。服务端日志明明回了失败码，
`bshook` 日志里也**明明白白记着那个框**：

```text
★MSGBOXW caller=00423E1F type=00000000 cap="登录失败"
         text="认证服务器失败 (20000)"
```

—— 框确实弹了，只是 `det_MessageBoxW` **`return IDOK` 了事，从来没调真的 MessageBox**。

那是 V0.1 阶段 2 留下的：当时要抓「谁调用了 GameGuard 的错误框」，而且自动化跑起来
没人点确定，所以直接抑制。**GameGuard 早就绕过了，这个抑制却一直留着**，
于是 V0.2 新加的登录失败提示一出生就被自己人掐死。

**修法**：两个 detour 改成「记完日志转发给真函数」。翻遍历史日志，客户端这辈子只弹过
两种框（`登录失败` 和 `图像引擎初始化失败`），都该让玩家看见，没有需要抑制的。

**教训**：观测期的「抑制 / 拦截」类 hook 是**借来的**，进入功能开发期要逐个还回去。
一个返回值写死的 detour 不会报错，只会让上层功能安静地失效。

## 130. ★★★★ 登录框的两个坑：注册链接开**两个**网页 / 分区单选钮登录后被禁用

**（1）一次点击开两个网页。** `SS_NOTIFY` 加上去之后，Static 的默认窗口过程会在
**`WM_LBUTTONDOWN`** 那一刻给对话框发 `WM_COMMAND/STN_CLICKED` ——
而客户端的对话框过程里**真的有**这条链接的处理器，它去开那个早就停机的
`member.tiancity.com`。我们的子类化只吃了 `WM_LBUTTONUP`，按下那一半照样漏给了原过程。

⚠ 这**不推翻 §125**：原版点不动是因为没有 `SS_NOTIFY` + z 序被分组框压着，
鼠标消息根本到不了那条 Static；处理器一直在，只是从来没被触发过。
另外那次点击**没有任何 `ShellExecute` 调用**（我们全程 hook 着），
说明客户端开浏览器走的不是 `ShellExecuteW/A` —— 具体走哪个 API 没继续查，
因为把消息吃掉就够了。

**修法**：`link_wndproc` 把 `WM_LBUTTONDOWN` / `WM_LBUTTONDBLCLK` / `WM_NCLBUTTONDOWN`
一并 `return 0` 吃掉，对话框过程收不到 `STN_CLICKED`，死链接自然不会被打开。
**实测**：点一次 → Edge 标签页 13 → 14（只多一个），新标签就是我们的注册页。

**（2）第一次点「开始」之后，`1011` / `1012` 两个单选钮被客户端永久 `EnableWindow(FALSE)`。**
原版的想法是「服务器选定了就不许再换」，但它**登录失败时也不解禁** ——
玩家想从「单机」改成「联机」只能重启游戏。
**修法**：`poll_login_dialog` 每轮检查，发现被禁用就解禁回来
（对话框还在 = 还没登录成功，这时候换分区没有任何副作用）。

## 131. ★★★ 存档里的 `level` 是**派生字段**，手改它必须连经验一起补

用户在另一台机器上导出 `testuser1` 的存档，把 `level` 从 1 改成 5 再上传，
提示上传成功，但 `accounts.json` 里等级还是 1。

**不是导入丢字段，是等级压根不存**：`level` 由经验推出来（D024），
`_merged_account()` **每次读盘都重算一遍**，写进去多少都会被算回去。

**修法**（`experience_for_import()`，只在导入这一刻生效）：
存档里的等级**高于**经验推出来的等级 -> 把经验补到那一级的起点（`(lv-1)*100`）；
否则以经验为准。也就是**等级只能往上抬，想降级得连经验一起改小**（D070）。

其余字段（password / display_name / tutorial_* / experience / money / character /
quest_difficulty / quest_unlock_all / character_unlock_all / owned_characters）
**本来就都能正常更新**，`test_import_updates_every_field_of_an_existing_account`
逐个钉住了，还顺带断言「`NEW_ACCOUNT_DEFAULTS` 加了新字段这条用例必须跟着补」。

## 132. ★★★★ 被顶号的客户端会**自动重连并重放旧票据** —— 那句怪提示就是这么来的

用户报：第二台电脑用同一个账号登录，第一台被挤掉，弹的却是
**「在无法连接的地方尝试了连接。」**。

**这句话是客户端自带的**，不是我们写的。`Pack_decrypt\Data\Chinese.ini`：

```text
접속할 수 없는 곳에서 접속하셨습니다.=在无法连接的地方尝试了连接。      ← 韩文直译，本来就拗口
기존 연결을 끊습니다. 다시 로그인해 주십시오.=现有连接已断开。请重新尝试连接。
로그인에 실패하였습니다. 아이디와 비밀번호를 확인하세요.=登录失败。请重新确认帐号和密码。
```

**这三句由 `gspRepLogin` 的结果码挑**（处理器 `0x54f2cc`，V0.1 §44 只记了跳转地址，
没记文案）。三条失败分支**结构完全一样**（`[esi+0x898]=0` → `call 0x5bc415` 断开
→ 查 `Data/Chinese.ini` → `call 0x40f304` 弹「公告」框），只有字符串指针不同：

| result | 分支 | 字符串 VA | 弹出来的中文 |
|---|---|---|---|
| 0 | `0x54f4af` | `0x692464` | （成功，进大厅）|
| 1 | `0x54f468` | `0x6924e0` | 登录失败。请重新确认帐号和密码。 |
| **2** | `0x54f416` | `0x6924a4` | **现有连接已断开。请重新尝试连接。** |
| 3 | `0x54f3cf` | `0x692478` | 在无法连接的地方尝试了连接。 |

★ 这个「公告」框**不是 `MessageBoxW`**（`0x40f304` 是客户端自绘的 D3D 弹窗），
所以 bshook 的 MessageBox hook 抓不到它，只能截图看。

### 为什么被顶号会走到 result=3

`gameserver` 顶号时只是 `close_now()` 把旧连接的 socket 关掉，**什么包都不发**。
但客户端不会安静地退回登录框 —— 它**立刻自动重连**（实测 9 毫秒后），
并把**同一张票据**原样重放。而那张票据在第二台电脑登录时已经被
`TicketStore.issue()` 作废了，于是服务端回 `result=3`（票据无效）。
用户看到的就是 result=3 的那句话。

用户那次实测的服务端日志（`logs/server.out`，V0.2 会话 02 遗留）：

```text
[23:55:40.020] #4 gcpReqLogin                        ← 第二台电脑 192.168.11.79
[23:55:40.022] #3 ⚠ 账号 'testuser' 在别处重新登录，本连接被顶掉
[23:55:40.031] #5 +++ 连接来自 ::ffff:127.0.0.1:5828  ← 被踢的客户端 9ms 后自己重连
[23:55:40.194] #5 ★ 游戏包 opcode=0x0100 (gcpReqLogin) 载荷 98 字节  ← 重放旧票据
[23:55:40.195] #5 ✗ 票据无效或已过期；回 gspRepLogin(result=3) 断开
```

**修法**：`TicketStore` 记一张 `_revoked` 表（票据 -> 用户名 + 原因 + 时刻，
跟着同一个 TTL 过期），游戏服 `resolve()` 落空后再问一句 `revoked_reason()`：
是被顶掉的就回 **`result=2`**，别的（乱码票据 / 真过期）仍然回 `result=3`。
D071 记了为什么不干脆全都回 2。

**实机验证**（会话 03，本机）：用 `tools/gs_ctl.py raw 0100 <payload>` 直接把
result=2 / result=3 推给真客户端，截图对比 ——
截图在 `.claude/sessions/2026-08-11-03/`，两句话和上表完全对上。

⚠ **重连不是每次都发生**：本机复现时被踢的客户端重连后**没有**重放票据，
而是给认证服发了 `0x000d` 登出就退出了。触发条件（大厅状态？房间里？）没继续查，
因为两条路都已经被覆盖：重放 -> result=2 有提示；不重放 -> 客户端自己退，也没错。

## 133. 连接事件日志和 `--verbose` 必须解耦

`--verbose` 一开就是逐包 hexdump + 每条连接两个抓包文件，日志按 MB 涨，
云端长期开服不可能开着。可「谁连上了 / 谁断开了 / 从哪个 IP」恰恰是长期要留的东西。

`server/eventlog.py` 因此独立于两个 `log()`：统一前缀 `[online]`，
`grep online logs/server.out` 就是一份完整流水；同时**追加**写 `logs/online.log`
（`server.out` 每次启动被覆盖，这份不会）。落盘失败一律吞掉，绝不能因为
磁盘满/目录只读把服务端拖垮。

★ IPv6 双栈监听（D063）收到 IPv4 连接时 `getpeername` 给的是 `::ffff:192.168.11.79`。
日志里剥掉 `::ffff:` 前缀 —— 玩家报 IP 时说的是 `192.168.11.79`，带前缀对不上号。
真 IPv6 地址加方括号，否则分不清哪一截是端口。

## 134. ★★★★★ 「主线程离开注入 APC」这个判据是错的 —— 别人的电脑上登录界面出不来

用户把 V0.1 的包发给别人，那台电脑上启动脚本不报错，**游戏登录界面不出现**，
弹「Game guard文件不存在或已变更，请重新安装Game guard。」

对方的 `logs\bshook_*.log`（`hook_merge\logs_aaa\`），6 次启动全一个样：

```text
[20:49:52.185] HWBP  GameGuard VEH 已安装，等待 DR0 命中 0054B0FC
[20:49:52.190] HWBP  主线程已离开注入 APC（EIP=007B27B3），DR0=0054B0FC 已武装   ← 注入后 5 毫秒
[20:49:55.981] ★MSGBOXW cap="公告" text="Game guard文件不存在或已变更…"
                        #02 frame=0019FB1C ret=0054B562 <<< 主模块
```

**日志说「已武装」，但 `DR0 命中` 那一行永远不出现。**

### 根因

旧判据 `main_thread_left_injection_apc()` 是「主线程 EIP 不在
ntdll / kernel32 / kernelbase / bshook 这四个模块里」。
**主模块自己不在排除表里** —— 而注入 APC 一返回，主线程落回的正是
主模块尾部的 **ASProtect 壳**（`EIP=0x007b27xx`，SizeOfImage 到 `0x7c6200`）。
于是判据在注入后 5 毫秒就成立，DR0 在**壳还在跑的时候**写下去，
随后壳 `NtContinue` 恢复旧 CONTEXT，把 `Dr0` 清回 0。

我这台机器一直没炸，纯属**运气**：这里的采样要 720 毫秒才第一次落在
排除表外（`EIP=5E921DB0`，某个后加载的 DLL），那时壳已经跑完
（同一份日志里 `UNPACK base+0x1000 initial` 直接就是明文 `e9 15 02 00 00`）。
对方机器上 `initial` 是密文 `20 03 cc b2 19 11 a3 00`，`changed (#1)` 才变明文
—— 两台机器的解壳时机差了几百毫秒，判据的对错就翻面了。

### 修法（两条，缺一不可）

1. **换成因果性判据**：等 `0x54b0fc` 那 5 个字节变成已知明文
   （原版 `E8 CF 60 01 00`，或兼容旧内存 patch 的 `B8 55 07 00 00`）。
   ASProtect 是**整段一次性**解壳的（UNPACK 日志里一步从密文变明文），
   字节对上就说明壳已经真的跑过了。这是「壳跑完了没有」的直接证据，
   不是对 EIP 的猜测。
   读之前先 `VirtualQuery` 确认页已提交、可读、没有 `PAGE_GUARD`
   —— 别用 `IsBadReadPtr`，它会真去踩一脚，可能把壳的 guard page 异常吃掉。
2. **加 10 毫秒的守护回合**：命中之前反复复查 `Dr0`/`Dr7`，被清掉就补回去。
   有些 Windows / 驱动 / 安全软件组合会在 `SetThreadContext` 成功**之后**
   再恢复一份旧 CONTEXT，只写一次照样会「显示已武装但永不命中」。
   `g_gg_break_state != 0`（VEH 已命中）就退出，开销只有几秒钟。

★ **还留了兜底**：等不到已知签名也照样武装 + 守护（日志写明「兜底已武装」）。
不这么做的话，遇到一个我们没预料到的 exe 变体会**比改之前更差** ——
旧代码好歹还武装了。命中后 VEH 自己会做签名判定，对不上就只撤断点、
让原指令正常跑，不会瞎改 EIP。

### 挂起主线程期间**绝对不能调 `bslog()`**

主线程可能正拿着日志的锁，那是必死的死锁。
`ensure_gameguard_breakpoint()` 全程不打日志，所有 `bslog` 都在 `ResumeThread` 之后。

### 回归夹具

`hook\test_gg_watchdog.c` + `hook\test-watchdog.bat`：编一个固定 ImageBase 的
32 位小程序，在 `0x54b0fc` 摆上原版 `call`，等 DLL 武装 DR0 后**主动清掉一次**，
再执行那个地址。守护补回来 -> VEH 返回 `0x755` -> exit 0；没补回来 -> exit 5。

```bash
hook\test-watchdog.bat
```

**实测**（会话 03，本机）：`[test] PASS`，且 DLL 日志里真的有
`!! DR0 被清掉了（原 Dr0=00000000 Dr7=00000000），已补回（第 1 次）`。
真游戏冷启动 3/3 全过：`目标指令已解壳（0 ms / 0 轮，原始 call）` → `DR0 命中`
→ 登录框改造完成。

## 135. ★★★★ `chcp 65001` 下 **`.bat` 里的中文会把后面的命令行拦腰截断** —— 连 `rem` 注释里的也算

**现象**：`start.bat` 里删掉几行中文 `echo` 之后，运行时冒出

```text
'…行拦腰截断（见' is not recognized as an internal or external command,
```

那串东西是**我写在 `rem` 注释里的文字**。注释不该被执行，但它被当成命令了。

**根因**：`cmd.exe` 执行批处理时会记住「当前读到文件的哪个偏移」，执行完一行再 seek 回去
接着读。`chcp 65001` 之后它把文件按 UTF-8 解码成字符再算偏移，而 seek 用的是**字节**
—— 一个汉字 3 字节记成 1，偏移一路往前漂。漂到某一刻 seek 落在一个命令行的**中间**，
cmd 就把后半截当成一条新命令去执行。

**关键点，别记错**：

- 这和 CRLF **无关**。全局规范里那条「.bat 必须 CRLF」是另一个坑，两个都要防，但修一个
  不解决另一个。本次三个 `.bat` 全程都是 CRLF + UTF-8 无 BOM，照炸。
- **不限于 `if (...) else (...)` 括号块**，顶层的 `rem` 一样中招。最初以为是括号块专属，
  改成 `goto` 标签后仍然报错，才定位到是全文件的偏移漂移。
- 漂移量取决于**它前面累计了多少个多字节字符**，所以「改之前好好的，只删了几行就炸」
  完全正常 —— 原来的版本只是恰好没漂到断点上。这类 bug 靠「看起来没问题」根本防不住。
- 中文 `pause` 提示（`请按任意键继续. . .`）是 cmd 自己出的，不受影响。

**定论（V0.2 会话 04 起执行）**：`start.bat` / `start-debug.bat` / `stop.bat`
**只允许 ASCII**，一个汉字都不留（注释也不行）。所有中文提示由
`tools\launch.ps1` / `tools\shutdown.ps1` 打印 —— `.ps1` 是 UTF-8 with BOM，
PowerShell 整文件读进来再解析，没有这个 seek 问题。
失败路径的「[启动失败] …」也一并搬进 `launch.ps1` 的 `trap`，bat 只负责转发退出码。
里程碑 K 生成服务端包的 `.bat` 时同样照办。见 D074。

**非交互验证手法**（含 `pause` 的 bat 也能自动跑）：

```bash
sed 's|^powershell .*|rem (stub)|; s|^set "RC=%ERRORLEVEL%"|set "RC=0"|' start.bat > t.bat
# 转 CRLF 后
cmd //c "t.bat <nul"
```

`<nul` 让 `pause` 读到 EOF 自己结束。RC 分别替成 0 和 1 就能把两条分支都走一遍。

---

# 第二批 —— 里程碑 I 大厅联机（2026-08-11 起）

## 136. ★★★★★ 流读写原语全表（`0x5d59xx` / `0x5d5axx`），逆包格式先查这张表

所有 `Packet_*::Serialize` / `::Deserialize` 都只调这十几个 helper。
认出它们，任何包的线格式都能直接读出来，不用再猜。

**读（Deserialize，`ecx` = 流）**

| va | 读什么 | 返回/落位 |
|---|---|---|
| `0x5d5942` | u8 | `al` |
| `0x5d5956` | int32 → bool | `al` |
| `0x5d596f` | **u16** | `ax`（调用方常跟一句 `movsx eax, ax`）|
| `0x5d5984` | int32 | `eax` |
| `0x5d5998` | float32 | st0 |
| `0x5d59c1` | n 个原始字节 | 写进指针参数 |
| `0x5d59d0` | 1 字节 | 写进指针参数 |
| `0x5d59de` | int32 → bool | 写进指针参数 |
| `0x5d59f1` | 2 字节 | 写进指针参数 |
| `0x5d59ff` | 4 字节 | 写进指针参数 |
| `0x5d5a0d` | 8 字节 | 写进指针参数 |
| `0x5d5b3a` | **字符串**（u16 字符数 + UTF-16LE）| 写进指针参数 |

**写（Serialize，`ecx` = 流）**

| va | 写什么 |
|---|---|
| `0x5d5901` | 1 字节 |
| `0x5d5910` | 2 字节 |
| `0x5d591f` | 4 字节 |
| `0x5d592e` | 8 字节 |
| `0x5d5a4c` | 1 字节**零扩展成 4 字节**再写（线上是 int32）|
| `0x5d5a5a` | 字符串（u16 字符数 + UTF-16LE）|

★ `re/vftables.json` 的 key 就是**类的 vftable 首地址**，
**槽 0 = Serialize、槽 1 = Deserialize**（实测：`0x6915f0` = `Packet_gcpReqListSession`
的 vft，客户端在 `0x54e6c8` 处 `mov [ebp-0x1c], 0x6915f0` 建对象，
随后 `call [eax]` 调的就是 `0x54c0e2` = Serialize）。
只是**相邻类的 vft 挨着排**，肉眼按 3 个一组去数很容易错一格 —— 要认某个函数属于谁，
拿它的 va 去镜像里搜 dword，看它落在哪个 key 后面 4 字节。

## 137. ★★★★★ `Session` 的线格式 = `0x0303` 的载荷（不含尾部 u16）；房间列表条目就是它

`Session` 对象 0x30 字节，构造函数 `0x4052ff`，反序列化 **`0x556e80`**
（`esi` = this，`edi` = 流 —— 这个函数用寄存器传参，不是标准 thiscall）：

```text
int32              -> +0x04   房间状态（2 = 待机中，其余 = 游戏中，见 V0.1 §102）
string             -> +0x08   房间标题
int32              -> +0x0c   ★ 人数分子，见 §138
string             -> +0x10   地图名
int32（存 1 字节）  -> +0x14   语义未定
SessionDescriptor  -> +0x18   房间类型 + 参数（vft 0x65e09c，反序列化 0x557401）
```

`0x556ed1` = `0x556e80` + 再读一个 u16 存 `+0x3c` —— **这就是 `0x0303` 的完整载荷**，
`server/gameserver.py` 的 `build_update_session()` 已经和它逐字段对上。

**所以房间列表里每一项的房间信息，可以直接复用 `build_update_session()` 的前半段。**
V0.1 §63 已经指出「`LobbyStage` 和房间列表条目是同一套布局」，这里是逐字段的确认。

## 138. ★★★★★ `0x0200 gspRepListSession` 的完整线格式（每项 0x30 字节的字段全部查明）

反序列化 `0x559009`（`ecx` = 流，`ebx` = 包对象）。**这是 V0.2 PROGRESS 里
「每项 0x30 字节的字段还没逆过」那条待办的答案。**

```text
u16   n                       房间数；n <= 0 时 0x559023 的 jle 跳过整个循环
n 次 {
    Session                   见 §137（new(0x30) + 构造 0x4052ff + 反序列化 0x556e80）
    u16    房间号             -> 包+0x0c 的 vector
    u8     人数分母           -> 包+0x18 的 vector
    int32(bool)  ?            -> 包+0x24 的 vector
}
u16   ?                       -> 包+0x38
```

**每个字段是干什么的**（处理器 `0x54f596` → 大厅列表模型 `0x72e618`
的 `SetSessionList` `0x43a543`；模型 +0x0c/+0x18/+0x24/+0x30/+0x44 依次收下上面五样）：

| 字段 | 用途 | 证据 |
|---|---|---|
| `Session+0x04` | 「待机中」/「게임중(游戏中)」 | `0x43e5de: cmp [session+4], 2` |
| `Session+0x08` | 房间标题 | —— |
| `Session+0x0c` | **`%s(%d/%d)` 的第一个 %d** | `0x43e63f: mov ecx,[session+0xc]` |
| 每项 u16 | **房间号**，UI 上显示 `值+1`（`'%d번'`）| `0x43a435` 取 `[模型+0x18 + i*2]`，`0x43e565: inc eax` |
| 每项 u8 | **`%s(%d/%d)` 的第二个 %d** | `0x43e5cf: movzx eax, byte [模型+0x24 + i]` |
| 每项 bool | 未定（列表 UI 里还没找到读它的地方）| —— |
| 尾部 u16 | -> 模型 +0x44，疑似总房间数/分页用 | —— |

格式串：`0x6656e4 = '%s(%d/%d)'`、`0x6656f8 = '게임중'`、`0x665700 = '대기중'`、
`0x665708 = '%d번'`。`Chinese.ini`：`대기중=待机中`。

⚠ **`(%d/%d)` 两个数谁是当前人数、谁是上限，光看反汇编分不出来** —— printf 顺序只说明
`Session+0x0c` 在前、每项 u8 在后。实机各填一个不同的数看一眼就知道，先按
「`Session+0x0c` = 当前人数，u8 = 上限」实现，实机一眼看错就对调。

## 139. ★★★★ `0x0200 gcpReqListSession` 请求（12 字节）的字段

序列化 `0x54c0e2`（vft `0x6915f0` 槽 0），构造 `0x54e68c`，调用链
`0x43a6a5 → 0x43a78e → 0x54e68c`：

```text
u16   起始房间号     0x43a6c1 传的是「当前列表里 index 0 的房间号」-> 分页锚点
u16   ?             恒 0
u16   ?             来自列表模型 +0x54
u8    ?             来自列表模型 +0x58
u8    ?             来自列表模型 +0x59
int32 游戏类型       ★ 由 [conn+0x89c] 频道码翻译：7->2 8->6 9->5 其余->1
```

`0x43a78e` 里那个 `[ebp+0x1c]` **不进包**，它是轮询节流的毫秒数
（`cmp edi, [模型+0x48] + 间隔`，另有 10000 毫秒的强制上限）。

**服务端先按「忽略前五个字段，只用 game_type 过滤」实现**，把实收的 12 字节记进日志，
等实机看到真值再决定要不要做分页。

## 140. ★★★★★ 加入房间 `0x0202 gcpReqMoveInto` 的请求 / 应答 / 结果码

**请求**（客户端 -> 服务端，序列化 `0x558d9d`）：

```text
int32   房间号
string  密码
int32（由 1 字节零扩展）  ?
```

**应答**（服务端 -> 客户端，反序列化 `0x5590d5` = 连读 4 个 int32，处理器 `0x54fd07`）：

```text
int32 result
int32 房间号     -> LobbyStage+0x1c8
int32 我的座位号  -> LobbyStage+0x1cc（经 0x405a1f；**-2 = 观战**）
int32 ?          成功分支没用到
```

| result | 客户端行为 |
|---|---|
| **0** | 成功：写房间号/座位号，`座位.occupied=1`、`座位.character_id=0`，`ChangeStage(5)` 进房间 |
| 1 | 弹「进入失败 / 此房间已开始游戏。」 |
| 2 | 弹「进入失败 / 已超出人数限制的房间。」 |
| 3 | 弹「进入失败 / 没有符合条件的房间。」 |
| 4 | 弹「进入失败 / 密码错误。」 |
| **5** | 先 `0x40889b`（把座位 0/1/2 的 `+0x14` 清 0）**再走成功分支** —— 疑似「进观战席」|
| 其它 | 弹「进入失败 / 无法进入房间。」 |

文案都是客户端自带的（`Chinese.ini`），和 D069 / D071 同一条路子：**回对结果码即可，
一个字都不用我们写**。

★ 成功之后客户端立刻 `ChangeStage(5)` 建 `RoomStage`，而 `RoomStage` 构造函数
`0x466979` 读的是 `LobbyStage` 里的 Session（状态/描述符/地图名）。所以服务端必须
**先 `0x0303` 再 `0x0202` 应答，然后 `0x0300`、最后 `0x030b`**，并且和建房那一路一样
**合并成一次 `sendall`**（V0.1 §120 / D058）。

## 141. ★★★★★ 聊天：`0x0305` 两个方向的线格式

**客户端 -> 服务端 `Packet_gcpSendChatMsg`**（vft `0x6916bc`，序列化 `0x54c26c`）：

```text
u8      聊天类型
string  正文
```

**服务端 -> 客户端 `Packet_gspReceiveChatMsg`**（vft `0x65e164`，
序列化 `0x404e3b` / 反序列化 `0x404e76`，处理器 `0x406adb`）：

```text
u16     发言者座位号     0..5；用 0x4045f9 判座位是否占用，占用就从座位里取昵称
string  发言者显示名     ★ 非空 -> 渲染成 '%s : %s' % (显示名, 正文)
                          空   -> 只渲染 '%s' % 正文（系统提示就走这条）
string  正文
int32   聊天类型         传给 0x40605d 决定颜色
```

也就是说**「谁说的」由包里的第二个字符串决定，不是从座位查的** ——
座位号只用于「点名字」之类的交互。系统消息把显示名留空即可。

## 142. 大厅包分发跳表 `@0x406332`（`0x0300`~`0x0311`，索引 = opcode 低字节）

```text
0x0300 -> 0x406232   0x0301 -> 0x40623f   0x0302 -> 0x40624b   0x0303 -> 0x406258
0x0304 -> 0x4062ea   0x0305 -> 0x406263   0x0306 -> 0x406272   0x0307 -> 0x40632d
0x0308 -> 0x40627e   0x0309 -> 0x4062ea   0x030a -> 0x4062ea   0x030b -> 0x40628a
0x030c -> 0x406295   0x030d -> 0x40629f   0x030e -> 0x4062ac   0x030f -> 0x4062b3
0x0310 -> 0x4062ea   0x0311 -> 0x4062bc
```

`0x0304 / 0x0309 / 0x030a / 0x0310` 都指向同一个 `0x4062ea`（结算那条路，V0.1 §98）。

## 143. ★★★★★ 里程碑 I 的实机确认（会话 04，单机一个客户端 + 假房间）

`tools/gs_ctl.py fakeroom` 造「没有连接的房间」，就能在**一台机器一个客户端**上
把房间列表、加入房间、聊天全部验完 —— 不必等第二台电脑。

**§138 里那两个「⚠ 还没实机确认」的问题，答案是**：

| 问题 | 实测结论 |
|---|---|
| `%s(%d/%d)` 哪个是当前人数 | **`Session+0x0c` = 当前人数，列表项的 u8 = 上限**。发 (2, 6) 显示成 `待机中(2/6)`，假设正确，不用对调 |
| 房间号从 0 开始分配对不对 | **对**。room_id=1 的房间显示成「2号」，room_id=0 显示成「1号」。`'%d번'` 确实是 `值+1` |

**一次跑通的整条链**（截图在 `.claude/sessions/2026-08-11-04/`）：

```text
05  大厅列表：「组队战 / 무투전 / 待机中(2/6) / 2号 对战房」—— 标题、状态、人数、房号全对
06  双击房间 -> 进房：三个座位的角色、昵称（测试玩家1/测试玩家2/alice）、
    等级（Lv.9/Lv.9/Lv.2）、「房主」标记、人物选择的 6+ 个头像 全部正确
10  聊天：消息条 `Bob : hello from bob`，且座位号对应的角色头上冒气泡
11  系统提示（显示名留空）：`someone joined the room.`，**没有「谁 : 」前缀**
14  客户端自己建房 -> 立刻出现在大厅列表里
15  退房 -> 房间从列表消失
```

**服务端日志同时确认的**：

- `0x0202` 请求载荷 **10 字节** = `int32 房间号 + wstr(空) + int32` —— 和 §140 对上
- 客户端发的 `0x0305` 解析正确：`聊天(type=0) 'alice': 'hibob' 座位=2`
- 建房后客户端紧跟着发 `0x0302`，服务端把地图名同步进大厅：
  `map='Domir_Newbie:NewPvp'` —— 房间列表里后进来的人看到的就是这个

⚠ **两点没验到**（需要第二台电脑，见 PROGRESS 的 ⏳ 区）：
真人加入时的 `0x0301` action 0 广播、房主离开时的房主转移。
单机只能靠单元测试（`test_room.py` 覆盖了这两条的**包序列**，但不是实机）。

## 144. ★★★ 进了房间之后客户端每 10 秒发**两发** `0x0310 gcpStartTcpRelay`（8 字节）

会话 04 实测：`alice` 进房间之后，服务端日志里稳定出现

```text
[10:23:20.923] #2 ★ 游戏包 opcode=0x0310 (gcpStartTcpRelay) 载荷 8 字节
[10:23:20.925] #2 ★ 游戏包 opcode=0x0310 (gcpStartTcpRelay) 载荷 8 字节
[10:23:30.941] ... （每 10 秒一对，一直发到退房）
```

**这是里程碑 J 的头等线索**（§122 的推论第一次有了实测支撑）：
战斗还没开始、只是待在房间里，客户端就已经在**主动要求建中继通道**了。
不回它也不影响进房 / 聊天 / 退房（本次全程没回过）。

J.1 探查从这里入手：先把这 8 字节 dump 出来（`start-debug.bat` 就有 hexdump），
再看 `Packet_gcpStartTcpRelay`（vft `0x65e0d4`）的序列化函数写了哪两个 int32。

## 145. 单机验证房间功能的手法（`fakeroom` / `rooms` / `delroom`）

```bash
runtime\python\python.exe tools\gs_ctl.py fakeroom 对战房 1 2   # 标题 类型 人数
runtime\python\python.exe tools\gs_ctl.py rooms                 # 看现在有哪些房间
runtime\python\python.exe tools\gs_ctl.py delroom 1             # 强行解散
```

假房间的座位 `conn=None`，广播时被 `Room.members()` 过滤掉，所以它只影响**列表和
座位快照**，不会往任何 socket 上发东西。

★ 这里踩到一个真缺陷：`Lobby._leave_unlocked` 原本按 `remaining`（还剩几条**连接**）
判「房间空了没有」，导致最后一个真人退出时把还坐着假座位的房间也解散了。
正常游玩时两者等价，但意图应该是「**座位**还有没有人」—— 已改成 `room.is_empty()`，
并补了回归测试。**这类「测试替身暴露出的真 bug」值得记一笔**：假房间不是玩具，
它逼出了一个真实场景里迟早会遇到的语义混淆。

## 146. 登录框自动化的两个坑（会话 04 又踩了一遍）

1. **`tools/click.py` 的坐标是客户区坐标，而 `tools/screenshot.py` 抓的是整个窗口
   矩形**（含边框和标题栏）。两者差 **(3, 26)**：截图上量到 `(x, y)`，
   要点的是 `click.py <pid> (x-3) (y-26)`。直接把截图坐标喂进去会点空。
   ★ 窗口被移动过之后这个差值**不变**（边框尺寸是固定的），不用重新量。
2. **没做过教程的账号一登录就被拉进教学关**，房间/大厅一个都点不到。
   跑大厅相关的验证前先把存档标成教程已完成：

   ```python
   for acc in json.load(open('server/data/accounts.json'))['accounts'].values():
       acc['tutorial_completed'] = True
   ```
